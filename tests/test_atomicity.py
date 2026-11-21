"""Atomicity and concurrency guarantees.

* A batch whose second operation conflicts must roll back its first
  operation as well: no partial commit, no version bump, no negative
  multiplicity is ever observable.
* With many writer threads submitting batches concurrently (each with a
  unique client batch id) while readers continuously take snapshots, every
  observed snapshot must show the three result tables at one shared version
  with non-negative multiplicities, and duplicate batch ids must remain
  idempotent no-pops.
"""

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from app.engine import BatchConflict, Engine  # noqa: E402
from app.store import Store  # noqa: E402


def _cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.unlink(p)


def test_batch_rollback():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    engine = Engine(Store(path))
    try:
        engine.set_config(["a"], ["x"], "cfg")
        engine.commit_batch(
            [{"op": "add", "side": "left",
              "row": {"a": {"t": "int", "v": 1}}}],
            "seed",
        )
        before = engine.store.current_version()
        left_rows_before = engine.store.count_active(
            engine.store._connect(), "left"
        )

        # First op is valid, second op references a phantom row.
        try:
            engine.commit_batch([
                {"op": "add", "side": "right",
                 "row": {"x": {"t": "text", "v": "z"}}},
                {"op": "retract", "side": "left", "row_id": 987654},
            ], "half-fails")
            raise AssertionError("expected BatchConflict")
        except BatchConflict:
            pass

        after = engine.store.current_version()
        assert after == before, "version must not advance on failed batch"
        conn = engine.store._connect()
        try:
            assert engine.store.count_active(conn, "left") == left_rows_before
            assert engine.store.count_active(conn, "right") == 0, \
                "the valid first op must have been rolled back too"
            assert engine.store.get_batch(conn, "half-fails") is None
        finally:
            conn.close()

        snap = engine.store.read_all_results_snapshot()
        for kind in ("union", "intersect", "except"):
            assert all(r["multiplicity"] >= 0 for r in snap["results"][kind])
            assert all(r["left_count"] >= 0 and r["right_count"] >= 0
                       for r in snap["results"][kind])
        print("atomic rollback: OK")
    finally:
        _cleanup(path)


def test_concurrent_writers_and_readers():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    engine = Engine(Store(path))
    engine.set_config(["a", "b"], ["x", "y"], "cfg")

    stop = threading.Event()
    errors = []
    n_writers = 4
    per_writer = 40

    def writer(wid):
        try:
            for i in range(per_writer):
                bid = f"w{wid}-{i}"
                side = "left" if i % 2 == 0 else "right"
                c0, c1 = ("a", "b") if side == "left" else ("x", "y")
                ops = [{
                    "op": "add",
                    "side": side,
                    "row": {
                        c0: {"t": "int", "v": i % 5},
                        c1: {"t": "null", "v": None},
                    },
                }]
                first = engine.commit_batch(ops, bid)
                # Immediate replay must be idempotent under concurrency too.
                second = engine.commit_batch(ops, bid)
                assert second["replayed"] is True
                assert second["version"] == first["version"]
        except Exception as exc:  # pragma: no cover - failure reporter
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                snap = engine.store.read_all_results_snapshot()
                versions = set()
                for kind in ("union", "intersect", "except"):
                    rows = snap["results"][kind]
                    assert all(r["multiplicity"] >= 0 for r in rows)
                    versions.add(
                        engine.store.read_results(kind, snap["version"])[0]
                    )
                assert versions == {snap["version"]}, \
                    "mixed versions inside one snapshot"
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    threads = [threading.Thread(target=writer, args=(w,))
               for w in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors, errors
    snap = engine.store.read_all_results_snapshot()
    total = sum(r["multiplicity"] for r in snap["results"]["union"])
    assert total == n_writers * per_writer, total
    # One durable batch row per unique client id, no duplicates.
    batches = engine.store.list_batches(limit=1000)
    ids = [b["client_batch_id"] for b in batches if b["client_batch_id"] != "cfg"]
    assert len(ids) == len(set(ids)) == n_writers * per_writer
    print(f"concurrency: {n_writers} writers x {per_writer} batches + "
          f"3 readers, final union copies={total}: OK")
    _cleanup(path)


if __name__ == "__main__":
    test_batch_rollback()
    test_concurrent_writers_and_readers()
    print("atomicity/concurrency: ALL CHECKS PASSED")
