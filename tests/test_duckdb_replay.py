"""Random change replay against a DuckDB reference implementation.

A stream of add / retract / correct batches (including mixed batches touching
both sides, duplicate batch replays and deliberately invalid retractions) is
fed to two systems:

1. the incremental engine backed by SQLite;
2. a DuckDB oracle that keeps the full left/right multisets and recomputes
   ``UNION ALL`` / ``INTERSECT ALL`` / ``EXCEPT ALL`` from scratch at every
   version using DuckDB's own SQL operators.

DuckDB stores our canonical cell encodings in VARCHAR columns, so its set
operations apply exactly the semantics the engine implements: typed-tuple
identity plus SQL null-equality (NULLs merge) inside the set operations.

After every committed batch the three result tables are compared row by row,
including multiplicities and per-side counts. Mid-run the engine process is
"restarted" by dropping the in-memory Engine and reopening the same database
file; subsequent changes must keep matching.
"""

import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402
import duckdb  # noqa: E402

from app import types as T  # noqa: E402
from app.engine import BatchConflict, Engine  # noqa: E402
from app.store import Store  # noqa: E402

KINDS = ("union", "intersect", "except")
LEFT_COLS = ["a", "b"]
RIGHT_COLS = ["x", "y"]
TAGS = [T.INT, T.TEXT, T.NULL, T.FLOAT, T.BOOL]


def random_cell(rng: random.Random) -> dict:
    tag = rng.choices(TAGS, weights=[40, 30, 18, 6, 6], k=1)[0]
    if tag == T.NULL:
        return {"t": T.NULL, "v": None}
    if tag == T.INT:
        return {"t": T.INT, "v": rng.randint(0, 4)}
    if tag == T.FLOAT:
        return {"t": T.FLOAT, "v": float(rng.randint(0, 4))}
    if tag == T.BOOL:
        return {"t": T.BOOL, "v": rng.choice([True, False])}
    return {"t": T.TEXT, "v": rng.choice(["a", "b", "c"])}


def random_row(rng: random.Random, cols: list[str]) -> dict:
    return {c: random_cell(rng) for c in cols}


def canon_of(row: dict, cols: list[str]) -> str:
    return T.tuple_canon(T.project_tuple(row, cols))


class DuckDBOracle:
    """Full-recompute reference for the three multiset operations."""

    def __init__(self):
        self.con = duckdb.connect()
        self.con.execute("CREATE TABLE l(c0 VARCHAR, c1 VARCHAR)")
        self.con.execute("CREATE TABLE r(c0 VARCHAR, c1 VARCHAR)")
        # Active source rows: row_id -> (side, full typed row)
        self.rows: dict[int, tuple[str, dict]] = {}
        self.next_id = 1

    def _encoded(self, row: dict, cols):
        cells = T.project_tuple(row, cols)
        return [T.cell_to_duckdb(c) for c in cells]

    def add(self, side, row):
        rid = self.next_id
        self.next_id += 1
        self.rows[rid] = (side, row)
        table = "l" if side == "left" else "r"
        cols = LEFT_COLS if side == "left" else RIGHT_COLS
        self.con.execute(
            f"INSERT INTO {table} VALUES (?, ?)", self._encoded(row, cols)
        )
        return rid

    def retract(self, rid):
        side, row = self.rows.pop(rid)
        table = "l" if side == "left" else "r"
        cols = LEFT_COLS if side == "left" else RIGHT_COLS
        enc = self._encoded(row, cols)
        # Delete one physical copy of this encoded tuple.
        self.con.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE c0=? AND c1=? LIMIT 1)",
            enc,
        )

    def correct(self, rid, new_row):
        side, _ = self.rows[rid]
        self.retract(rid)
        new_id = self.add(side, new_row)
        return new_id

    def _multiset(self, sql: str) -> dict[str, int]:
        rows = self.con.execute(sql).fetchall()
        out = {}
        for c0, c1, n in rows:
            # Rebuild the engine's canonical encoding for comparison.
            # VARCHAR cells are exactly cell_canon outputs already.
            out[f"{c0}|{c1}"] = int(n)
        return out

    def expected(self) -> dict[str, dict[str, int]]:
        union_sql = (
            "SELECT c0, c1, count(*) FROM ("
            "SELECT * FROM l UNION ALL SELECT * FROM r) "
            "GROUP BY c0, c1"
        )
        inter_sql = (
            "SELECT c0, c1, count(*) FROM ("
            "SELECT * FROM l INTERSECT ALL SELECT * FROM r) "
            "GROUP BY c0, c1"
        )
        except_sql = (
            "SELECT c0, c1, count(*) FROM ("
            "SELECT * FROM l EXCEPT ALL SELECT * FROM r) "
            "GROUP BY c0, c1"
        )
        return {
            "union": self._multiset(union_sql),
            "intersect": self._multiset(inter_sql),
            "except": self._multiset(except_sql),
        }

    def side_counts(self) -> dict[str, tuple[int, int]]:
        out = {}
        for table, cols in (("l", LEFT_COLS), ("r", RIGHT_COLS)):
            rows = self.con.execute(
                f"SELECT c0, c1, count(*) FROM {table} GROUP BY c0, c1"
            ).fetchall()
            for c0, c1, n in rows:
                key = f"{c0}|{c1}"
                l, r = out.get(key, (0, 0))
                if table == "l":
                    out[key] = (l + int(n), r)
                else:
                    out[key] = (l, r + int(n))
        return out


def engine_multisets(engine: Engine) -> dict[str, dict[str, int]]:
    snap = engine.store.read_all_results_snapshot()
    out = {}
    for kind in KINDS:
        out[kind] = {r["tuple_canon"]: r["multiplicity"]
                     for r in snap["results"][kind]}
    return out


def assert_matches(engine: Engine, oracle: DuckDBOracle, step: int):
    expected = oracle.expected()
    actual = engine_multisets(engine)
    for kind in KINDS:
        assert actual[kind] == expected[kind], (
            f"step {step} {kind} mismatch:\n"
            f"  engine={sorted(actual[kind].items())}\n"
            f"  duckdb={sorted(expected[kind].items())}"
        )
    # Per-side counts and the shared version must line up too.
    snap = engine.store.read_all_results_snapshot()
    side_counts = oracle.side_counts()
    for row in snap["results"]["union"]:
        key = row["tuple_canon"]
        assert (row["left_count"], row["right_count"]) == side_counts[key], (
            step, key, row["left_count"], row["right_count"], side_counts[key]
        )
    # All three tables must carry the same single version.
    versions = {
        engine.store.read_results(kind, snap["version"])[0] for kind in KINDS
    }
    assert versions == {snap["version"]}, "mixed-version read detected"


def main(seed: int = 2611, iterations: int = 120):
    rng = random.Random(seed)
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    store = Store(db_path)
    engine = Engine(store)
    engine.set_config(LEFT_COLS, RIGHT_COLS, "replay-config")
    oracle = DuckDBOracle()
    assert_matches(engine, oracle, 0)

    batch_seq = 0
    conflict_count = 0
    replay_count = 0

    def new_batch_id():
        nonlocal batch_seq
        batch_seq += 1
        return f"replay-batch-{seed}-{batch_seq}"

    for step in range(1, iterations + 1):
        ops = []
        oracle_mutations = []  # (kind, payload) applied only on commit
        n_ops = rng.choices([1, 2, 3, 4], weights=[50, 30, 15, 5], k=1)[0]
        # Ids consumed earlier in this same batch cannot be touched again.
        touched: set[int] = set()
        for _ in range(n_ops):
            side = rng.choice(["left", "right"])
            cols = LEFT_COLS if side == "left" else RIGHT_COLS
            active = [
                rid for rid, (s, _) in oracle.rows.items()
                if s == side and rid not in touched
            ]
            choice = rng.random()
            if active and choice < 0.35:
                rid = rng.choice(active)
                touched.add(rid)
                if rng.random() < 0.5:
                    ops.append({"op": "retract", "side": side, "row_id": rid})
                    oracle_mutations.append(("retract", rid))
                else:
                    new_row = random_row(rng, cols)
                    ops.append({"op": "correct", "side": side,
                                "row_id": rid, "row": new_row})
                    oracle_mutations.append(("correct", rid, new_row))
            else:
                row = random_row(rng, cols)
                ops.append({"op": "add", "side": side, "row": row})
                oracle_mutations.append(("add", side, row))

        batch_id = new_batch_id()

        # Sprinkle in requests that must fail as conflicts: retract an id the
        # engine cannot know. The oracle is untouched and the version must not
        # move.
        if rng.random() < 0.08:
            bad_id = max(oracle.next_id + 1000, 99999)
            before = engine.store.current_version()
            try:
                engine.commit_batch(
                    [{"op": "retract", "side": "left", "row_id": bad_id}],
                    f"replay-invalid-{seed}-{step}",
                )
                raise AssertionError("expected conflict for phantom retract")
            except BatchConflict:
                conflict_count += 1
            assert engine.store.current_version() == before, \
                "conflict must not advance the version"

        before = engine.store.current_version()
        result = engine.commit_batch(ops, batch_id)
        if not result.get("replayed"):
            assert result["version"] == before + 1
            for mutation in oracle_mutations:
                if mutation[0] == "add":
                    oracle.add(mutation[1], mutation[2])
                elif mutation[0] == "retract":
                    oracle.retract(mutation[1])
                else:
                    oracle.correct(mutation[1], mutation[2])

        # Replay the exact same batch: idempotent no-op.
        replay = engine.commit_batch(ops, batch_id)
        assert replay["replayed"] is True
        assert replay["version"] == result["version"]
        replay_count += 1

        assert_matches(engine, oracle, step)

        # Simulate a service restart twice during the run: throw away every
        # in-memory object, reopen the same file, keep accepting changes.
        if step in (iterations // 3, 2 * iterations // 3):
            del engine
            del store
            store = Store(db_path)
            engine = Engine(store)
            assert_matches(engine, oracle, step)
            print(f"restart at step {step}: state matched DuckDB")

    assert conflict_count > 0, "test never exercised the conflict path"
    assert replay_count == iterations, "every batch should have been replayed"

    # Final restart after the stream ends.
    del engine
    del store
    engine = Engine(Store(db_path))
    assert_matches(engine, oracle, iterations)
    print(f"duckdb replay: {iterations} steps, {conflict_count} conflicts, "
          f"{replay_count} idempotent replays, 3 restarts - ALL PASSED")
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


if __name__ == "__main__":
    main()
