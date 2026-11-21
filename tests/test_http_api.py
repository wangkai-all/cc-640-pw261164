"""End-to-end checks through the real HTTP layer.

Spins the stdlib server up on an ephemeral port and exercises it with
urllib (no third-party client): CSV import on both sides, configuration,
mixed batches, duplicate replay (same id), conflict status codes, versioned
reads and a restart that reopens the same database file.
"""

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from app.engine import Engine  # noqa: E402
from app.server import build_handler  # noqa: E402
from app.store import Store  # noqa: E402


class ServerHarness:
    def __init__(self, db_path):
        self.db_path = db_path
        self.engine = Engine(Store(db_path))
        handler = build_handler(self.engine)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def restart(self):
        """Drop all in-memory state and reopen the same database file."""
        self.stop()
        self.__init__(self.db_path)


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    harness = ServerHarness(db_path)
    try:
        # Health and empty state.
        status, body = harness.request("GET", "/api/health")
        assert status == 200 and body["ok"] is True
        status, state = harness.request("GET", "/api/state")
        assert status == 200 and state["version"] == 0

        # Configure comparison columns first.
        status, body = harness.request("POST", "/api/config", {
            "client_batch_id": "http-cfg",
            "left_columns": ["a", "b"],
            "right_columns": ["x", "y"],
        })
        assert status == 200 and body["version"] == 1, body

        # Import both sides.
        left_csv = "a,b\n1,a\n1,a\n1,a\n2,\n"
        right_csv = "x,y\n1,a\n1,a\n2,\n,z\n"
        status, body = harness.request("POST", "/api/import", {
            "client_batch_id": "http-left", "side": "left",
            "csv": left_csv, "auto_configure": False,
        })
        assert status == 200 and body["version"] == 2, body
        status, body = harness.request("POST", "/api/import", {
            "client_batch_id": "http-right", "side": "right",
            "csv": right_csv, "auto_configure": False,
        })
        assert status == 200 and body["version"] == 3, body

        # Snapshot read: one shared version across all three tables.
        status, snap = harness.request("GET", "/api/results")
        assert status == 200 and snap["version"] == 3
        mults = {
            kind: {r["tuple_canon"]: r["multiplicity"]
                   for r in snap["results"][kind]}
            for kind in ("union", "intersect", "except")
        }
        key = 'i:1|s:"a"'
        assert mults["union"][key] == 5
        assert mults["intersect"][key] == 2
        assert mults["except"][key] == 1
        null_key = "i:2|n:∅"
        assert mults["intersect"][null_key] == 1
        print("snapshot multiplicities:", json.dumps(mults, ensure_ascii=False))

        # Versioned read of an older version returns that point in time.
        status, old = harness.request("GET", "/api/results?version=2")
        assert status == 200 and old["version"] == 2
        assert all(
            row["right_count"] == 0
            for row in old["results"]["union"]
        ), "version 2 predates the right-side import"

        # A mixed batch touching both sides switches atomically.
        status, source = harness.request("GET", "/api/source/left")
        first_id = source["rows"][0]["row_id"]
        status, body = harness.request("POST", "/api/batch", {
            "client_batch_id": "http-mixed",
            "operations": [
                {"op": "retract", "side": "left", "row_id": first_id},
                {"op": "add", "side": "right",
                 "row": {"x": {"t": "int", "v": 9},
                         "y": {"t": "text", "v": "q"}}},
            ],
        })
        assert status == 200 and body["version"] == 4, body

        # Replaying the same batch id is an idempotent no-op.
        status, replay = harness.request("POST", "/api/batch", {
            "client_batch_id": "http-mixed",
            "operations": [
                {"op": "retract", "side": "left", "row_id": first_id},
                {"op": "add", "side": "right",
                 "row": {"x": {"t": "int", "v": 9},
                         "y": {"t": "text", "v": "q"}}},
            ],
        })
        assert status == 200 and replay["replayed"] is True
        assert replay["version"] == 4

        # A phantom retract is a 409 conflict and does not move the version.
        status, conflict = harness.request("POST", "/api/batch", {
            "client_batch_id": "http-conflict",
            "operations": [
                {"op": "retract", "side": "left", "row_id": 999999},
            ],
        })
        assert status == 409 and conflict["conflict"] is True, conflict
        status, state = harness.request("GET", "/api/state")
        assert state["version"] == 4

        # Malformed payloads are 400.
        status, bad = harness.request("POST", "/api/batch", {
            "client_batch_id": "http-bad", "operations": [],
        })
        assert status == 400, bad

        # Static frontend is served.
        with urllib.request.urlopen(harness.url("/"), timeout=10) as resp:
            html = resp.read().decode("utf-8")
            assert "<!doctype html>" in html.lower() or "<html" in html.lower()

        # Restart: durable state survives and new batches keep working.
        harness.restart()
        status, state = harness.request("GET", "/api/state")
        assert status == 200 and state["version"] == 4
        status, body = harness.request("POST", "/api/batch", {
            "client_batch_id": "http-after-restart",
            "operations": [
                {"op": "add", "side": "left",
                 "row": {"a": {"t": "int", "v": 7},
                         "b": {"t": "text", "v": "z"}}},
            ],
        })
        assert status == 200 and body["version"] == 5, body
        status, snap = harness.request("GET", "/api/results")
        assert snap["version"] == 5
        assert 'i:7|s:"z"' in {
            r["tuple_canon"] for r in snap["results"]["union"]
        }

        print("http end-to-end: ALL CHECKS PASSED")
    finally:
        harness.stop()
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.unlink(p)


if __name__ == "__main__":
    main()
