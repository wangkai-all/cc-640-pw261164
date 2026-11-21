"""HTTP layer: JSON API plus static frontend, stdlib only.

All writes take a client-generated batch id so retries are idempotent. The
three result warehouse tables are read through one snapshot transaction and
therefore always returned at a single shared version.
"""

from __future__ import annotations

import json
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import BatchConflict, Engine, EngineError
from .store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
RESULT_KINDS = ("union", "intersect", "except")


def build_handler(engine: Engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MultisetSets/1.0"

        def log_message(self, fmt, *args):  # quieter, structured logging
            pass

        # ------------------------------------------------------------ helpers

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, rel: str):
            rel = rel.lstrip("/") or "index.html"
            target = (WEB_DIR / rel).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                self._send_json({"error": "not found"}, 404)
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(target.suffix, "application/octet-stream")
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                raise EngineError("request body is required")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise EngineError(f"invalid JSON body: {exc}") from exc
            if not isinstance(data, dict):
                raise EngineError("JSON body must be an object")
            return data

        @staticmethod
        def _batch_id(data: dict) -> str:
            return data.get("client_batch_id") or f"auto-{uuid.uuid4()}"

        def _run(self, fn):
            try:
                self._send_json(fn())
            except EngineError as exc:
                self._send_json({"error": str(exc)}, 400)
            except BatchConflict as exc:
                self._send_json({"error": str(exc), "conflict": True}, 409)
            except FileNotFoundError as exc:
                self._send_json({"error": str(exc)}, 404)
            except Exception:  # pragma: no cover - defensive
                traceback.print_exc()
                self._send_json({"error": "internal server error"}, 500)

        # ------------------------------------------------------------- routing

        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._run(lambda: self._handle_get(path, qs))
            else:
                self._send_static(path)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, 404)
                return
            self._run(lambda: self._handle_post(parsed.path))

        def do_DELETE(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                def reset():
                    engine.store.wipe()
                    return {"ok": True, "state": engine.state()}
                self._run(reset)
            else:
                self._send_json({"error": "not found"}, 404)

        # ---------------------------------------------------------------- GET

        def _handle_get(self, path, qs):
            if path == "/api/health":
                return {"ok": True, "version": engine.store.current_version()}
            if path == "/api/state":
                return engine.state()
            if path == "/api/results":
                version = qs.get("version", [None])[0]
                version = int(version) if version is not None else None
                return engine.store.read_all_results_snapshot(version)
            if path in ("/api/results/union", "/api/results/intersect",
                        "/api/results/except"):
                kind = path.rsplit("/", 1)[-1]
                version = qs.get("version", [None])[0]
                version = int(version) if version is not None else None
                v, rows = engine.store.read_results(kind, version)
                return {"version": v, "kind": kind, "rows": rows}
            if path == "/api/source/left" or path == "/api/source/right":
                side = path.rsplit("/", 1)[-1]
                return self._source_payload(side)
            if path == "/api/versions":
                return {"versions": engine.store.list_versions()}
            if path == "/api/events":
                return {"events": engine.store.list_events()}
            if path == "/api/batches":
                return {"batches": engine.store.list_batches()}
            raise FileNotFoundError(path)

        def _source_payload(self, side):
            conn = engine.store._connect()
            try:
                conn.execute("BEGIN")
                rows = [
                    {"row_id": rid, "row": typed}
                    for rid, typed in engine.store.all_active_rows(conn, side)
                ]
                conn.execute("COMMIT")
                return {"side": side, "rows": rows}
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

        # ---------------------------------------------------------------- POST

        def _handle_post(self, path):
            data = self._read_json()
            if path == "/api/import":
                side = data.get("side")
                text = data.get("csv")
                if not isinstance(text, str):
                    raise EngineError("'csv' must be a string")
                return engine.import_csv(
                    side, text, self._batch_id(data),
                    overrides=data.get("type_overrides"),
                    configure=bool(data.get("auto_configure", True)),
                )
            if path == "/api/config":
                return engine.set_config(
                    data.get("left_columns"), data.get("right_columns"),
                    self._batch_id(data),
                )
            if path == "/api/batch":
                ops = data.get("operations")
                if not isinstance(ops, list):
                    raise EngineError("'operations' must be a list")
                return engine.commit_batch(ops, self._batch_id(data))
            if path == "/api/sample":
                return self._load_sample()
            raise FileNotFoundError(path)

        def _load_sample(self):
            from .sample import build_sample_batches

            results = []
            for req in build_sample_batches():
                if req["endpoint"] == "import":
                    results.append(engine.import_csv(
                        req["side"], req["csv"], req["client_batch_id"],
                        overrides=req.get("type_overrides"),
                        configure=req.get("auto_configure", True),
                    ))
                elif req["endpoint"] == "config":
                    results.append(engine.set_config(
                        req["left_columns"], req["right_columns"],
                        req["client_batch_id"],
                    ))
                else:  # batch
                    results.append(engine.commit_batch(
                        req["operations"], req["client_batch_id"]
                    ))
            return {"loaded": True, "commits": results,
                    "state": engine.state()}

    return Handler


def serve(db_path: str, host: str = "0.0.0.0", port: int = 8000) -> None:
    store = Store(db_path)
    engine = Engine(store)
    handler = build_handler(engine)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"multiset set-ops server on http://{host}:{port} (db={db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
