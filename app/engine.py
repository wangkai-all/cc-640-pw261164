"""Incremental multiset engine for UNION ALL / INTERSECT ALL / EXCEPT ALL.

Correctness contract implemented here:

* identity is a full typed tuple (see :mod:`app.types`); NULLs merge like SQL
  set operations (``IS NOT DISTINCT FROM``), not like equi-join predicates;
* per distinct tuple, with left multiplicity ``l`` and right multiplicity
  ``r``: UNION ALL keeps ``l+r``, INTERSECT ALL keeps ``min(l, r)`` and
  EXCEPT ALL keeps ``max(l-r, 0)``;
* a batch touching both sides commits in one transaction and all three result
  tables switch to the new version atomically - no intermediate state and no
  negative multiplicity is ever observable;
* retracting/correcting an unknown source row is a conflict; replaying a batch
  with the same client batch id is an idempotent no-op;
* source row ids are never reused, so result provenance always resolves to
  real input rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import types as T
from .store import Store


class EngineError(ValueError):
    """400-class error: malformed request."""


class BatchConflict(Exception):
    """409-class error: the batch contradicts durable state."""


@dataclass
class CountEntry:
    cells: list[dict]
    canon: str
    left: int = 0
    right: int = 0
    left_ids: list = field(default_factory=list)
    right_ids: list = field(default_factory=list)


class Engine:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        conn = self.store._connect()
        try:
            conn.execute("BEGIN")
            version = int(self.store.get_meta(conn, "version"))
            config = self.store.get_config(conn)
            left_n = self.store.count_active(conn, "left")
            right_n = self.store.count_active(conn, "right")
            columns = {"left": set(), "right": set()}
            samples = {"left": [], "right": []}
            for side in ("left", "right"):
                for rid, row in self.store.all_active_rows(conn, side):
                    columns[side].update(row.keys())
                    if len(samples[side]) < 5:
                        samples[side].append({"row_id": rid, "row": row})
            conn.execute("COMMIT")
            return {
                "version": version,
                "config": config,
                "left_count": left_n,
                "right_count": right_n,
                "known_columns": {k: sorted(v) for k, v in columns.items()},
                "samples": samples,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- configuration

    def set_config(self, left_columns: list[str], right_columns: list[str],
                   client_batch_id: str) -> dict:
        if not left_columns or not right_columns:
            raise EngineError("both sides need at least one comparison column")
        if len(left_columns) != len(right_columns):
            raise EngineError(
                f"column arity differs: {len(left_columns)} vs {len(right_columns)}"
            )

        def mutate(conn, ctx):
            self._apply_config(conn, ctx, list(left_columns), list(right_columns))

        return self._transact(
            client_batch_id,
            reason="compare-column configuration change",
            mutate=mutate,
        )

    def _apply_config(self, conn, ctx, left_columns, right_columns) -> None:
        # Columns must exist in stored data (unless the side is still empty,
        # in which case the declaration is accepted as a schema promise).
        for side, cols in (("left", left_columns), ("right", right_columns)):
            known = self._known_columns(conn, side)
            missing = [c for c in cols if c not in known]
            if known and missing:
                raise EngineError(f"unknown {side} comparison columns: {missing}")
        ctx["config"] = {"left_columns": left_columns,
                         "right_columns": right_columns}

    # -------------------------------------------------------------------- import

    def import_csv(self, side: str, text: str, client_batch_id: str,
                   overrides: dict | None = None,
                   configure: bool = True) -> dict:
        if side not in ("left", "right"):
            raise EngineError("side must be 'left' or 'right'")
        schema, typed_rows = T.parse_csv(text, overrides)
        if not typed_rows:
            raise EngineError("CSV contains no data rows")

        def mutate(conn, ctx):
            header = list(schema.keys())
            if configure:
                current = ctx["config"]
                other = "right" if side == "left" else "left"
                other_cols = self._known_columns(conn, other)
                if current is None:
                    # First import: declare both sides from this header,
                    # refined by whatever already exists on the other side.
                    if other_cols and len(other_cols) != len(header):
                        raise EngineError(
                            "cannot auto-configure: column arity differs "
                            "across sides"
                        )
                    if side == "left":
                        ctx["config"] = {
                            "left_columns": header,
                            "right_columns": list(other_cols or header),
                        }
                    else:
                        ctx["config"] = {
                            "left_columns": list(other_cols or header),
                            "right_columns": header,
                        }
                else:
                    # Later imports: align this side's columns to the new
                    # header, keeping the opposite side and the arity fixed.
                    target = "left_columns" if side == "left" else "right_columns"
                    if len(header) != len(current[target]):
                        raise EngineError(
                            "imported CSV column arity "
                            f"({len(header)}) does not match the configured "
                            f"arity ({len(current[target])}); reconfigure "
                            "explicitly via /api/config"
                        )
                    ctx["config"] = {**current, target: header}
            elif ctx["config"] is None:
                raise EngineError(
                    "comparison columns are not configured; call /api/config "
                    "or enable auto_configure"
                )
            for typed_row in typed_rows:
                self._apply_add(conn, ctx, side, typed_row)
            ctx["summary"]["imported"] = len(typed_rows)
            ctx["summary"]["schema"] = schema

        return self._transact(
            client_batch_id, reason=f"CSV import into {side}", mutate=mutate
        )

    def _known_columns(self, conn, side: str) -> list[str]:
        cols = set()
        for _, row in self.store.all_active_rows(conn, side):
            cols.update(row.keys())
        return sorted(cols)

    # ------------------------------------------------------------- change batches

    def commit_batch(self, operations: list[dict], client_batch_id: str) -> dict:
        if not operations:
            raise EngineError("batch must contain at least one operation")

        def mutate(conn, ctx):
            for idx, op in enumerate(operations):
                kind = op.get("op")
                side = op.get("side")
                if side not in ("left", "right"):
                    raise EngineError(f"operation #{idx}: side must be left/right")
                if kind == "add":
                    self._apply_add(conn, ctx, side, self._validate_row(op, idx))
                elif kind == "retract":
                    self._apply_retract(conn, ctx, side, op, idx)
                elif kind == "correct":
                    self._apply_correct(conn, ctx, side, op, idx)
                else:
                    raise EngineError(
                        f"operation #{idx}: unknown op {kind!r} "
                        "(expected add/retract/correct)"
                    )
                ctx["summary"]["ops"] = ctx["summary"].get("ops", 0) + 1

        return self._transact(
            client_batch_id, reason="explicit change batch", mutate=mutate
        )

    def _validate_row(self, op: dict, idx: int) -> dict:
        row = op.get("row")
        if not isinstance(row, dict) or not row:
            raise EngineError(f"operation #{idx}: 'row' must be a non-empty object")
        typed = {}
        for col, cell in row.items():
            if isinstance(cell, dict) and "t" in cell:
                typed[col] = T.coerce(cell.get("v"), cell["t"])
            elif cell is None:
                typed[col] = T.coerce(None, T.NULL)
            else:
                raise EngineError(
                    f"operation #{idx}: cell {col!r} must be typed "
                    "{'t': ..., 'v': ...}"
                )
        return typed

    # ------------------------------------------------------------- primitive ops

    def _project(self, ctx, side: str, typed_row: dict):
        config = ctx["config"]
        if config is None:
            raise EngineError(
                "comparison columns are not configured; set them before adding rows"
            )
        cols = config["left_columns"] if side == "left" else config["right_columns"]
        missing = [c for c in cols if c not in typed_row]
        if missing:
            raise EngineError(f"row is missing comparison columns: {missing}")
        cells = T.project_tuple(typed_row, cols)
        return T.tuple_hash(cells), T.tuple_canon(cells), cells

    def _counts_bump(self, ctx, side, cells, canon, row_id, delta):
        counts: dict[str, CountEntry] = ctx["counts"]
        entry = counts.get(canon)
        if entry is None:
            if delta < 0:
                raise BatchConflict(
                    "internal: negative multiplicity on unseen tuple"
                )
            entry = CountEntry(cells=cells, canon=canon)
            counts[canon] = entry
        if side == "left":
            entry.left += delta
            if delta > 0:
                entry.left_ids.append(row_id)
            else:
                entry.left_ids.remove(row_id)
        else:
            entry.right += delta
            if delta > 0:
                entry.right_ids.append(row_id)
            else:
                entry.right_ids.remove(row_id)
        if entry.left < 0 or entry.right < 0:
            raise BatchConflict(f"multiplicity would go negative for {canon}")
        if entry.left == 0 and entry.right == 0:
            del counts[canon]

    def _apply_add(self, conn, ctx, side, typed_row) -> int:
        h, canon, cells = self._project(ctx, side, typed_row)
        version = ctx["version"]
        row_id = self.store.alloc_row_id(conn, side)
        self.store.insert_source_row(
            conn, row_id, side, typed_row, ctx["batch_id"], version
        )
        self._counts_bump(ctx, side, cells, canon, row_id, +1)
        self.store.record_event(
            conn, version, ctx["batch_id"], side, "add",
            {"row": typed_row, "tuple_hash": h}, row_id=row_id
        )
        ctx["summary"]["adds"] = ctx["summary"].get("adds", 0) + 1
        return row_id

    def _apply_retract(self, conn, ctx, side, op, idx) -> None:
        row_id = op.get("row_id")
        if not isinstance(row_id, int):
            raise EngineError(f"operation #{idx}: retract needs integer row_id")
        active = self.store.get_active_row(conn, row_id)
        if active is None or active["side"] != side:
            raise BatchConflict(
                f"operation #{idx}: no active {side} source row with id {row_id}; "
                "it never existed or was already retracted"
            )
        typed_row = json.loads(active["typed_row"])
        h, canon, cells = self._project(ctx, side, typed_row)
        version = ctx["version"]
        self.store.retire_source_row(conn, row_id, version)
        self._counts_bump(ctx, side, cells, canon, row_id, -1)
        self.store.record_event(
            conn, version, ctx["batch_id"], side, "retract",
            {"row_id": row_id, "tuple_hash": h}, row_id=row_id
        )
        ctx["summary"]["retracts"] = ctx["summary"].get("retracts", 0) + 1

    def _apply_correct(self, conn, ctx, side, op, idx) -> None:
        row_id = op.get("row_id")
        if not isinstance(row_id, int):
            raise EngineError(f"operation #{idx}: correct needs integer row_id")
        active = self.store.get_active_row(conn, row_id)
        if active is None or active["side"] != side:
            raise BatchConflict(
                f"operation #{idx}: cannot correct {side} row {row_id}: "
                "no such active source row"
            )
        new_row = self._validate_row(op, idx)
        old_row = json.loads(active["typed_row"])
        old_h, old_canon, old_cells = self._project(ctx, side, old_row)
        new_h, new_canon, new_cells = self._project(ctx, side, new_row)
        version = ctx["version"]
        # Atomic remove-old + add-new inside the same batch transaction.
        self.store.retire_source_row(conn, row_id, version)
        self._counts_bump(ctx, side, old_cells, old_canon, row_id, -1)
        new_id = self.store.alloc_row_id(conn, side)
        self.store.insert_source_row(
            conn, new_id, side, new_row, ctx["batch_id"], version
        )
        self._counts_bump(ctx, side, new_cells, new_canon, new_id, +1)
        self.store.record_event(
            conn, version, ctx["batch_id"], side, "correct",
            {"old_row_id": row_id, "new_row_id": new_id,
             "old_tuple_hash": old_h, "new_tuple_hash": new_h,
             "old_row": old_row, "new_row": new_row},
            old_row_id=row_id, new_row_id=new_id
        )
        ctx["summary"]["corrects"] = ctx["summary"].get("corrects", 0) + 1

    # ------------------------------------------------------------- materialization

    def _build_counts(self, conn, config) -> dict[str, CountEntry]:
        counts: dict[str, CountEntry] = {}
        sides = (("left", config["left_columns"]),
                 ("right", config["right_columns"]))
        for side, cols in sides:
            for row_id, typed_row in self.store.all_active_rows(conn, side):
                cells = T.project_tuple(typed_row, cols)
                canon = T.tuple_canon(cells)
                entry = counts.get(canon)
                if entry is None:
                    entry = CountEntry(cells=cells, canon=canon)
                    counts[canon] = entry
                if side == "left":
                    entry.left += 1
                    entry.left_ids.append(row_id)
                else:
                    entry.right += 1
                    entry.right_ids.append(row_id)
        return counts

    def _materialize(self, conn, version, counts) -> None:
        union_rows, intersect_rows, except_rows = [], [], []
        for canon, e in sorted(counts.items()):
            h = T.tuple_hash(e.cells)
            common = {
                "tuple_hash": h,
                "tuple_canon": canon,
                "cells": e.cells,
                "left_count": e.left,
                "right_count": e.right,
            }
            # UNION ALL: l + r copies, each traceable to a concrete source row.
            union_rows.append({
                **common,
                "multiplicity": e.left + e.right,
                "provenance": {
                    "left_row_ids": list(e.left_ids),
                    "right_row_ids": list(e.right_ids),
                    "reason": (
                        f"UNION ALL 保留 left {e.left} 份 + right {e.right} 份 "
                        f"= {e.left + e.right} 份"
                    ),
                },
            })
            # INTERSECT ALL: min(l, r) paired copies.
            m = min(e.left, e.right)
            if m > 0:
                pairs = list(zip(e.left_ids[:m], e.right_ids[:m]))
                intersect_rows.append({
                    **common,
                    "multiplicity": m,
                    "provenance": {
                        "pairs": [[a, b] for a, b in pairs],
                        "left_row_ids": list(e.left_ids[:m]),
                        "right_row_ids": list(e.right_ids[:m]),
                        "reason": (
                            "INTERSECT ALL 按出现顺序左右配对，"
                            f"min({e.left}, {e.right}) = {m} 份"
                        ),
                    },
                })
            # EXCEPT ALL: max(l - r, 0) surviving left copies.
            diff = e.left - e.right
            if diff > 0:
                survivors = e.left_ids[e.right:]
                except_rows.append({
                    **common,
                    "multiplicity": diff,
                    "provenance": {
                        "survivor_left_row_ids": survivors,
                        "consumed_by_right_row_ids": list(e.right_ids),
                        "reason": (
                            f"EXCEPT ALL: left {e.left} 份被 right 抵消 "
                            f"{e.right} 份，剩余 {e.left} - {e.right} = {diff} 份"
                        ),
                    },
                })
        self.store.replace_result_version(conn, "union", version, union_rows)
        self.store.replace_result_version(conn, "intersect", version, intersect_rows)
        self.store.replace_result_version(conn, "except", version, except_rows)

    # ---------------------------------------------------------------- transaction

    def _transact(self, client_batch_id: str, reason: str, mutate) -> dict:
        if not client_batch_id or not isinstance(client_batch_id, str):
            raise EngineError("client_batch_id is required")
        with self.store._write_lock:
            conn = self.store._connect()
            try:
                # BEGIN IMMEDIATE takes the write lock up front: concurrent
                # writers serialize in SQLite, duplicate detection is stable.
                conn.execute("BEGIN IMMEDIATE")
                existing = self.store.get_batch(conn, client_batch_id)
                if existing is not None:
                    # Duplicate batch: the exact same change is already durable,
                    # so replay is an idempotent no-op.
                    conn.execute("COMMIT")
                    return {
                        "replayed": True,
                        "client_batch_id": client_batch_id,
                        "version": existing["version"],
                        "summary": json.loads(existing["summary"]),
                    }

                config = self.store.get_config(conn)
                # In-memory guard counters: only used to reject a batch that
                # would produce negative multiplicity before commit. Durable
                # counts for materialization are always rebuilt from rows.
                guard = self._build_counts(conn, config) if config else {}
                # Allocate the new version before applying ops so ledger events
                # carry their final version; a rollback undoes the counter too.
                version = self.store.next_version(conn)
                ctx = {
                    "batch_id": client_batch_id,
                    "config": config,
                    "counts": guard,
                    "summary": {},
                    "version": version,
                }
                # Validation and source-row mutations run first; any conflict
                # rolls the whole batch back before results are touched.
                mutate(conn, ctx)

                new_config = ctx["config"]
                if new_config is not None:
                    if new_config != config:
                        self.store.put_config(
                            conn, new_config["left_columns"],
                            new_config["right_columns"], version
                        )
                    # Rebuild the full multiset from durable source rows after
                    # every batch: counts can never drift, and the same code
                    # path handles configuration changes and initial imports.
                    final_counts = self._build_counts(conn, new_config)
                    self._materialize(conn, version, final_counts)
                else:
                    # No configuration yet: results stay empty, the version
                    # still advances so readers have a consistent point in time.
                    self._materialize(conn, version, {})

                self.store.record_version(conn, version, reason, client_batch_id)
                self.store.record_batch(
                    conn, client_batch_id, version, ctx["summary"]
                )
                conn.execute("COMMIT")
                return {
                    "replayed": False,
                    "client_batch_id": client_batch_id,
                    "version": version,
                    "summary": ctx["summary"],
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
