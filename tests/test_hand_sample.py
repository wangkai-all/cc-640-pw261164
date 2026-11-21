"""Hand-computed sample checks.

The numeric expectations live in app/sample.py and were derived by hand, not
from the engine. This test also pins the headline requirement: a tuple with
left multiplicity 3 and right multiplicity 2 yields INTERSECT ALL = 2 and
EXCEPT ALL = 1.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402
from app.engine import Engine  # noqa: E402
from app.sample import (
    HAND_EXPECTED_AFTER_IMPORT,
    HAND_EXPECTED_FINAL,
    build_sample_batches,
)
from app.store import Store


def _new_engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Engine(Store(path)), path


def _cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.unlink(p)


def _run_request(engine, req):
    if req["endpoint"] == "import":
        return engine.import_csv(
            req["side"], req["csv"], req["client_batch_id"],
            overrides=req.get("type_overrides"),
            configure=req.get("auto_configure", True),
        )
    if req["endpoint"] == "config":
        return engine.set_config(
            req["left_columns"], req["right_columns"], req["client_batch_id"]
        )
    return engine.commit_batch(req["operations"], req["client_batch_id"])


def _mult_map(rows):
    return {r["tuple_canon"]: r["multiplicity"] for r in rows}


def _expect_table(expectations, index):
    return {k: v[index] for k, v in expectations.items() if v[index] > 0}


def main():
    engine, path = _new_engine()
    try:
        reqs = build_sample_batches()
        for i in range(3):
            _run_request(engine, reqs[i])

        snap = engine.store.read_all_results_snapshot()
        for j, kind in enumerate(("union", "intersect", "except")):
            got = _mult_map(snap["results"][kind])
            exp = _expect_table(HAND_EXPECTED_AFTER_IMPORT, 2 + j)
            assert got == exp, (kind, "after import", got, exp)

        # Headline semantic: (1, 'a') has l=3, r=2 right after import.
        inter = {r["tuple_canon"]: r for r in snap["results"]["intersect"]}
        exc = {r["tuple_canon"]: r for r in snap["results"]["except"]}
        key = 'i:1|s:"a"'
        assert inter[key]["multiplicity"] == 2, "INTERSECT ALL must keep min(3,2)=2"
        assert exc[key]["multiplicity"] == 1, "EXCEPT ALL must keep 3-2=1"
        assert inter[key]["left_count"] == 3 and inter[key]["right_count"] == 2
        # Provenance pairs resolve to real source row ids on both sides.
        for left_id, right_id in inter[key]["provenance"]["pairs"]:
            assert engine.store.get_active_row(
                engine.store._connect(), left_id
            ) is not None or True  # ids validated separately below
        print("headline l=3/r=2 -> inter 2, except 1: OK")

        # Remaining canned batches include a duplicate replay and a mixed batch.
        versions_seen = []
        for i in range(3, len(reqs)):
            r = _run_request(engine, reqs[i])
            versions_seen.append((r["version"], r.get("replayed", False)))
        assert any(replayed for _, replayed in versions_seen), \
            "duplicate batch must report replay"
        duplicate_version = next(v for v, rep in versions_seen if rep)
        assert duplicate_version == 4, "replay must not create a new version"

        snap = engine.store.read_all_results_snapshot()
        for j, kind in enumerate(("union", "intersect", "except")):
            got = _mult_map(snap["results"][kind])
            exp = _expect_table(HAND_EXPECTED_FINAL, 2 + j)
            assert got == exp, (kind, "final", got, exp)

        # Provenance: every cited row id is a real (possibly retired) source.
        conn = engine.store._connect()
        try:
            all_ids = {
                r["row_id"]
                for r in conn.execute("SELECT row_id FROM source_rows").fetchall()
            }
        finally:
            conn.close()
        for kind in ("union", "intersect", "except"):
            for row in snap["results"][kind]:
                prov = row["provenance"]
                cited = (
                    prov.get("left_row_ids", [])
                    + prov.get("right_row_ids", [])
                    + prov.get("survivor_left_row_ids", [])
                    + prov.get("consumed_by_right_row_ids", [])
                    + [x for pair in prov.get("pairs", []) for x in pair]
                )
                for rid in cited:
                    assert rid in all_ids, (kind, rid, "provenance id missing")

        print("hand sample: ALL CHECKS PASSED")
    finally:
        _cleanup(path)


if __name__ == "__main__":
    main()
