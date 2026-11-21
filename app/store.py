"""SQLite storage layer.

One database file holds every durable fact:

* ``meta``            - small key/value table (including per-side row-id counters)
* ``compare_config``  - the columns participating in the three operations
* ``batches``         - one row per submitted change batch (idempotent, append-only)
* ``source_rows``     - live multiset of typed rows on each side
* ``events``          - individual add / retract / correct ledger entries
* ``versions``        - version bookkeeping; every committed write bumps a version
* ``union_result`` / ``intersect_result`` / ``except_result``
                      - queryable warehouse tables, appended per version

Result rows are versioned, so a reader always sees all three tables at exactly
the same version. Writers use ``BEGIN IMMEDIATE``; the engine additionally
serializes writes in-process, which also makes duplicate batch detection
race-free.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compare_config (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    left_cols  TEXT NOT NULL,
    right_cols TEXT NOT NULL,
    version    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    client_batch_id TEXT PRIMARY KEY,
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('committed')),
    summary         TEXT NOT NULL,
    committed_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS source_rows (
    row_id     INTEGER PRIMARY KEY,
    side       TEXT NOT NULL CHECK (side IN ('left', 'right')),
    typed_row  TEXT NOT NULL,
    active     INTEGER NOT NULL CHECK (active IN (0, 1)),
    batch_id   TEXT NOT NULL,
    created_ver INTEGER NOT NULL,
    retired_ver INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_active ON source_rows(side, active);

CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    version   INTEGER NOT NULL,
    batch_id  TEXT NOT NULL,
    side      TEXT NOT NULL CHECK (side IN ('left', 'right')),
    op        TEXT NOT NULL CHECK (op IN ('add', 'retract', 'correct')),
    row_id    INTEGER,
    old_row_id INTEGER,
    new_row_id INTEGER,
    payload   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version     INTEGER PRIMARY KEY,
    reason      TEXT NOT NULL,
    batch_id    TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS union_result (
    version    INTEGER NOT NULL,
    tuple_hash TEXT NOT NULL,
    tuple_canon TEXT NOT NULL,
    cells      TEXT NOT NULL,
    multiplicity INTEGER NOT NULL,
    left_count INTEGER NOT NULL,
    right_count INTEGER NOT NULL,
    provenance TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_union_ver ON union_result(version);

CREATE TABLE IF NOT EXISTS intersect_result (
    version    INTEGER NOT NULL,
    tuple_hash TEXT NOT NULL,
    tuple_canon TEXT NOT NULL,
    cells      TEXT NOT NULL,
    multiplicity INTEGER NOT NULL,
    left_count INTEGER NOT NULL,
    right_count INTEGER NOT NULL,
    provenance TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intersect_ver ON intersect_result(version);

CREATE TABLE IF NOT EXISTS except_result (
    version    INTEGER NOT NULL,
    tuple_hash TEXT NOT NULL,
    tuple_canon TEXT NOT NULL,
    cells      TEXT NOT NULL,
    multiplicity INTEGER NOT NULL,
    left_count INTEGER NOT NULL,
    right_count INTEGER NOT NULL,
    provenance TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_except_ver ON except_result(version);
"""

RESULT_TABLES = {
    "union": "union_result",
    "intersect": "intersect_result",
    "except": "except_result",
}


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                # executescript() issues an implicit COMMIT before running, so
                # DDL goes first; the seed rows run in an explicit transaction.
                conn.executescript(SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES "
                    "('next_row_id', '1'), "
                    "('version', '0')"
                )
                conn.execute("INSERT OR IGNORE INTO versions(version, reason, batch_id) "
                             "VALUES (0, 'initial empty state', NULL)")
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            finally:
                conn.close()

    # -- meta --------------------------------------------------------------

    def get_meta(self, conn: sqlite3.Connection, key: str, default=None):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, conn: sqlite3.Connection, key: str, value) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def next_version(self, conn: sqlite3.Connection) -> int:
        v = int(self.get_meta(conn, "version")) + 1
        self.set_meta(conn, "version", v)
        return v

    def current_version(self) -> int:
        conn = self._connect()
        try:
            return int(self.get_meta(conn, "version"))
        finally:
            conn.close()

    def alloc_row_id(self, conn: sqlite3.Connection, side: str) -> int:
        # One global monotonic counter for both sides: row_id is the PRIMARY
        # KEY of source_rows and must never be reused, so provenance stays
        # resolvable even after retractions and corrections.
        rid = int(self.get_meta(conn, "next_row_id", default=1))
        self.set_meta(conn, "next_row_id", rid + 1)
        return rid

    # -- config ------------------------------------------------------------

    def get_config(self, conn: sqlite3.Connection):
        row = conn.execute("SELECT * FROM compare_config WHERE id=1").fetchone()
        if not row:
            return None
        return {
            "left_columns": json.loads(row["left_cols"]),
            "right_columns": json.loads(row["right_cols"]),
            "version": row["version"],
        }

    def put_config(self, conn, left_cols, right_cols, version) -> None:
        conn.execute(
            "INSERT INTO compare_config(id, left_cols, right_cols, version) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET left_cols=excluded.left_cols, "
            "right_cols=excluded.right_cols, version=excluded.version",
            (json.dumps(left_cols), json.dumps(right_cols), version),
        )

    # -- batches -----------------------------------------------------------

    def get_batch(self, conn: sqlite3.Connection, client_batch_id: str):
        row = conn.execute(
            "SELECT * FROM batches WHERE client_batch_id=?", (client_batch_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def record_batch(self, conn, client_batch_id, version, summary) -> None:
        conn.execute(
            "INSERT INTO batches(client_batch_id, version, status, summary) "
            "VALUES (?, ?, 'committed', ?)",
            (client_batch_id, version, json.dumps(summary)),
        )

    def list_batches(self, limit=100):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY version DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- source rows -------------------------------------------------------

    def insert_source_row(self, conn, row_id, side, typed_row, batch_id, version) -> None:
        conn.execute(
            "INSERT INTO source_rows(row_id, side, typed_row, active, batch_id, "
            "created_ver) VALUES (?, ?, ?, 1, ?, ?)",
            (row_id, side, json.dumps(typed_row), batch_id, version),
        )

    def retire_source_row(self, conn, row_id, version) -> None:
        conn.execute(
            "UPDATE source_rows SET active=0, retired_ver=? WHERE row_id=? AND active=1",
            (version, row_id),
        )

    def get_active_row(self, conn, row_id):
        row = conn.execute(
            "SELECT * FROM source_rows WHERE row_id=? AND active=1", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    def all_active_rows(self, conn, side):
        rows = conn.execute(
            "SELECT row_id, typed_row FROM source_rows WHERE side=? AND active=1 "
            "ORDER BY row_id",
            (side,),
        ).fetchall()
        return [(r["row_id"], json.loads(r["typed_row"])) for r in rows]

    def count_active(self, conn, side) -> int:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM source_rows WHERE side=? AND active=1", (side,)
        ).fetchone()["c"]

    # -- events ------------------------------------------------------------

    def record_event(self, conn, version, batch_id, side, op, payload,
                     row_id=None, old_row_id=None, new_row_id=None) -> None:
        conn.execute(
            "INSERT INTO events(version, batch_id, side, op, row_id, old_row_id, "
            "new_row_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version, batch_id, side, op, row_id, old_row_id, new_row_id,
             json.dumps(payload)),
        )

    def list_events(self, limit=200):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- versions ----------------------------------------------------------

    def record_version(self, conn, version, reason, batch_id) -> None:
        conn.execute(
            "INSERT INTO versions(version, reason, batch_id) VALUES (?, ?, ?)",
            (version, reason, batch_id),
        )

    def list_versions(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM versions ORDER BY version")]
        finally:
            conn.close()

    # -- results -----------------------------------------------------------

    def replace_result_version(self, conn, kind: str, version: int,
                               rows: list[dict]) -> None:
        table = RESULT_TABLES[kind]
        conn.execute(f"DELETE FROM {table} WHERE version=?", (version,))
        conn.executemany(
            f"INSERT INTO {table}(version, tuple_hash, tuple_canon, cells, "
            f"multiplicity, left_count, right_count, provenance) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    version,
                    r["tuple_hash"],
                    r["tuple_canon"],
                    json.dumps(r["cells"]),
                    r["multiplicity"],
                    r["left_count"],
                    r["right_count"],
                    json.dumps(r["provenance"]),
                )
                for r in rows
            ],
        )

    def read_results(self, kind: str, version: int | None = None):
        """Read one result table at an explicit version (or the latest)."""
        table = RESULT_TABLES[kind]
        conn = self._connect()
        try:
            if version is None:
                version = int(self.get_meta(conn, "version"))
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE version=? ORDER BY tuple_canon",
                (version,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["cells"] = json.loads(d["cells"])
                d["provenance"] = json.loads(d["provenance"])
                out.append(d)
            return version, out
        finally:
            conn.close()

    def read_all_results_snapshot(self, version: int | None = None):
        """Read the three result tables at one shared version.

        A single deferred transaction takes a read snapshot, so the three
        tables can never be observed at mixed versions.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            if version is None:
                version = int(self.get_meta(conn, "version"))
            snapshot = {"version": version, "results": {}}
            for kind, table in RESULT_TABLES.items():
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE version=? ORDER BY tuple_canon",
                    (version,),
                ).fetchall()
                parsed = []
                for r in rows:
                    d = dict(r)
                    d["cells"] = json.loads(d["cells"])
                    d["provenance"] = json.loads(d["provenance"])
                    parsed.append(d)
                snapshot["results"][kind] = parsed
            conn.execute("COMMIT")
            return snapshot
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- maintenance -------------------------------------------------------

    def wipe(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for t in ["events", "union_result", "intersect_result",
                          "except_result", "source_rows", "batches", "versions",
                          "compare_config", "meta"]:
                    conn.execute(f"DELETE FROM {t}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        self._init()
