"""Typed values and typed tuples.

Identity of a record in every multiset operation is a *typed tuple*:

* every cell carries a runtime type tag, so ``1`` (int) and ``1.0`` (float)
  are different tuples;
* NULL follows SQL's set-operation null semantics (the semantics of
  ``IS NOT DISTINCT FROM``): two NULLs of the same column compare equal,
  whereas ordinary equi-joins would never match NULL to anything;
* the canonical encoding is deterministic JSON so a tuple hashes and
  serializes identically everywhere (storage, engine, DuckDB oracle).
"""

from __future__ import annotations

import json
import math
from typing import Any

# Supported cell type tags.
NULL = "null"
BOOL = "bool"
INT = "int"
FLOAT = "float"
TEXT = "text"
TAGS = (NULL, BOOL, INT, FLOAT, TEXT)


class TypeError_(ValueError):
    """Raised when a value cannot be coerced to the declared cell type."""


def coerce(raw: Any, tag: str) -> Any:
    """Coerce an external (JSON / CSV-string) value to the typed internal form.

    The internal form of a cell is ``{"t": tag, "v": value}`` where ``v`` for
    NULL is always ``None`` regardless of the declared column type.
    """
    if tag not in TAGS:
        raise TypeError_(f"unknown type tag {tag!r}")
    if raw is None:
        return {"t": NULL, "v": None}
    if isinstance(raw, dict) and "t" in raw:
        # Already a typed cell coming from a JSON client.
        tag = raw["t"]
        raw = raw.get("v")
        if tag not in TAGS:
            raise TypeError_(f"unknown type tag {tag!r}")
        if raw is None:
            return {"t": NULL, "v": None}

    if isinstance(raw, str) and raw == "":
        # Empty CSV cell: NULL under every column type (SQL/CSV convention).
        # A genuine empty string must be supplied as an explicit typed cell.
        return {"t": NULL, "v": None}
    if tag == NULL:
        # A declared-null column can only hold NULL.
        if raw is None or raw == "":
            return {"t": NULL, "v": None}
        raise TypeError_(f"column typed NULL cannot hold {raw!r}")
    if tag == BOOL:
        if isinstance(raw, bool):
            return {"t": BOOL, "v": raw}
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("true", "1", "yes", "y", "t"):
                return {"t": BOOL, "v": True}
            if low in ("false", "0", "no", "n", "f"):
                return {"t": BOOL, "v": False}
        raise TypeError_(f"cannot coerce {raw!r} to bool")
    if tag == INT:
        if isinstance(raw, bool):
            raise TypeError_(f"cannot coerce bool {raw!r} to int")
        if isinstance(raw, int):
            return {"t": INT, "v": raw}
        if isinstance(raw, str):
            try:
                return {"t": INT, "v": int(raw.strip())}
            except ValueError as exc:
                raise TypeError_(f"cannot coerce {raw!r} to int") from exc
        raise TypeError_(f"cannot coerce {raw!r} to int")
    if tag == FLOAT:
        # An int and a float stay distinct: only explicit floats land here.
        if isinstance(raw, bool):
            raise TypeError_(f"cannot coerce bool {raw!r} to float")
        if isinstance(raw, float):
            val = raw
        elif isinstance(raw, int):
            raise TypeError_(f"integer {raw!r} must be explicitly float-typed")
        elif isinstance(raw, str):
            try:
                val = float(raw.strip())
            except ValueError as exc:
                raise TypeError_(f"cannot coerce {raw!r} to float") from exc
        else:
            raise TypeError_(f"cannot coerce {raw!r} to float")
        if math.isnan(val) or math.isinf(val):
            # Non-finite floats have no strict JSON encoding and no useful
            # multiset identity here; reject them at the boundary.
            raise TypeError_(f"non-finite float {raw!r} is not supported")
        return {"t": FLOAT, "v": val}
    # text
    if isinstance(raw, str):
        return {"t": TEXT, "v": raw}
    if isinstance(raw, (int, float, bool)):
        raise TypeError_(f"value {raw!r} must be explicitly text-typed")
    raise TypeError_(f"cannot coerce {raw!r} to text")


def cell_canon(cell: dict) -> str:
    """Deterministic string encoding of one typed cell."""
    tag = cell["t"]
    val = cell["v"]
    if tag == NULL:
        return "n:∅"
    if tag == BOOL:
        return f"b:{1 if val else 0}"
    if tag == INT:
        return f"i:{val}"
    if tag == FLOAT:
        # NaN equals NaN here (typed identity); sign/format normalized.
        if math.isnan(val):
            return "f:NaN"
        return f"f:{val!r}"
    return f"s:{json.dumps(val, ensure_ascii=False, sort_keys=True)}"


def tuple_canon(cells: list[dict]) -> str:
    return "|".join(cell_canon(c) for c in cells)


def tuple_hash(cells: list[dict]) -> str:
    import hashlib

    return hashlib.sha256(tuple_canon(cells).encode("utf-8")).hexdigest()


def project_tuple(typed_row: dict[str, dict], columns: list[str]) -> list[dict]:
    """Pick the compared columns from a full stored row; missing => NULL."""
    out: list[dict] = []
    for col in columns:
        cell = typed_row.get(col)
        out.append(cell if cell is not None else {"t": NULL, "v": None})
    return out


def cell_to_duckdb(cell: dict) -> str:
    """Encode one typed cell as a TEXT value for the DuckDB oracle.

    The oracle compares these encodings inside VARCHAR columns, which gives
    DuckDB's own INTERSECT/EXCEPT ALL the exact typed-tuple + null equality
    semantics implemented here. Real SQL NULLs never occur: a null-typed cell
    is the encoded string ``n:∅`` instead, so NULL=NULL merging is explicit.
    """
    return cell_canon(cell)


def describe_cell(cell: dict) -> Any:
    """JSON-friendly rendering for API responses (keeps the type tag)."""
    return cell


def _looks_int(s: str) -> bool:
    try:
        int(s.strip())
        return True
    except ValueError:
        return False


def _looks_float(s: str) -> bool:
    try:
        float(s.strip())
        return True
    except ValueError:
        return False


def _looks_bool(s: str) -> bool:
    return s.strip().lower() in ("true", "false", "yes", "no", "t", "f")


def infer_column_tag(values: list[str]) -> str:
    """Infer one column's type from its non-empty string values."""
    seen = [v for v in values if v != ""]
    if not seen:
        return TEXT
    if all(_looks_bool(v) for v in seen):
        return BOOL
    if all(_looks_int(v) for v in seen):
        return INT
    if all(_looks_float(v) for v in seen):
        return FLOAT
    return TEXT


def parse_csv(text: str, overrides: dict[str, str] | None = None):
    """Parse CSV text into typed rows.

    Returns ``(schema, rows)`` where schema maps column name -> type tag and
    every row maps column name -> typed cell. Empty cells become NULL.

    In inferred columns a cell may force its own type with a prefix:

    * ``i:7``      -> integer 7
    * ``f:1.0``    -> float 1.0 (so int ``1`` and float ``1.0`` coexist in
                      one column and stay distinct typed tuples)
    * ``t:hi``     -> text
    * ``b:true``   -> boolean
    * ``n:``       -> explicit NULL

    Column-level ``overrides`` pin the whole column to one tag instead.
    """
    import csv
    import io

    overrides = overrides or {}
    reader = csv.reader(io.StringIO(text))
    rows_raw = list(reader)
    if not rows_raw:
        return {}, []
    header = rows_raw[0]
    if len(set(header)) != len(header):
        raise ValueError("duplicate column names in CSV header")
    body = rows_raw[1:]
    columns = {name: [] for name in header}
    for r in body:
        for idx, name in enumerate(header):
            columns[name].append(r[idx] if idx < len(r) else "")

    schema: dict[str, str] = {}
    for name in header:
        if name in overrides:
            if overrides[name] not in TAGS:
                raise ValueError(f"unknown override type {overrides[name]!r}")
            schema[name] = overrides[name]
        else:
            # Prefix-forced cells are excluded from inference.
            prefixes = ("i", "f", "t", "b", "n")
            inferrable = [
                v for v in columns[name]
                if v != "" and not (
                    ":" in v and v.split(":", 1)[0] in prefixes
                )
            ]
            schema[name] = infer_column_tag(inferrable) if inferrable else TEXT

    prefix_tag = {"i": INT, "f": FLOAT, "t": TEXT, "b": BOOL, "n": NULL}
    typed_rows: list[dict[str, dict]] = []
    for r in body:
        typed: dict[str, dict] = {}
        for idx, name in enumerate(header):
            raw = r[idx] if idx < len(r) else ""
            if name not in overrides and ":" in raw and raw.split(":", 1)[0] in prefix_tag:
                pfx, rest = raw.split(":", 1)
                typed[name] = coerce(rest, prefix_tag[pfx])
            else:
                typed[name] = coerce(raw, schema[name])
        typed_rows.append(typed)
    return schema, typed_rows
