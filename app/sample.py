"""Small hand-computed sample dataset and canned change batches.

The dataset deliberately contains:

* duplicated rows on both sides;
* NULLs in compared columns (they must merge under SQL set-operation null
  semantics, i.e. NULL = NULL here);
* an integer ``1`` and a float ``1.0`` in the same compared column to prove
  typed-tuple identity - the two never merge;
* a tuple that appears three times on the left and twice on the right -
  INTERSECT ALL keeps two copies, EXCEPT ALL keeps one.

Hand-computed state after the two import batches (columns a/b on the left,
x/y on the right):

  tuple           left  right | union  inter  except
  (1, 'a')          3      2  |   5      2      1
  (1.0, 'a')        1      0  |   1      0      1
  (2, NULL)         2      1  |   3      1      1
  (3, 'c')          1      1  |   2      1      0
  (NULL, 'z')       0      2  |   2      0      0
"""

from __future__ import annotations

LEFT_CSV = (
    "a,b\n"
    "1,a\n"
    "1,a\n"
    "1,a\n"
    "f:1.0,a\n"
    "2,\n"
    "2,\n"
    "3,c\n"
)

RIGHT_CSV = (
    "x,y\n"
    "1,a\n"
    "1,a\n"
    "2,\n"
    "3,c\n"
    ",z\n"
    ",z\n"
)

# Columns participating in the three operations (one per side, same arity).
LEFT_COMPARE = ["a", "b"]
RIGHT_COMPARE = ["x", "y"]

# Hand-computed expectation keyed by canonical tuple, as (l, r, union, inter,
# except).  Canonical order matches app.types.cell_canon.
HAND_EXPECTED_AFTER_IMPORT = {
    "i:1|s:\"a\"": (3, 2, 5, 2, 1),
    "f:1.0|s:\"a\"": (1, 0, 1, 0, 1),
    "i:2|n:\u2205": (2, 1, 3, 1, 1),
    "i:3|s:\"c\"": (1, 1, 2, 1, 0),
    "n:\u2205|s:\"z\"": (0, 2, 2, 0, 0),
}


def build_sample_batches() -> list[dict]:
    """Canned batches for the UI 'load sample' button and the test suite."""
    return [
        {
            # Declare comparison columns first; an empty side accepts any
            # column names as a schema promise.
            "endpoint": "config",
            "left_columns": LEFT_COMPARE,
            "right_columns": RIGHT_COMPARE,
            "client_batch_id": "sample-config-v1",
        },
        {
            "endpoint": "import",
            "side": "left",
            "csv": LEFT_CSV,
            "client_batch_id": "sample-import-left-v1",
            "auto_configure": False,
        },
        {
            "endpoint": "import",
            "side": "right",
            "csv": RIGHT_CSV,
            "client_batch_id": "sample-import-right-v1",
            "auto_configure": False,
        },
        {
            # One more left copy of (2, NULL): l goes 2 -> 3 against r = 1.
            "endpoint": "batch",
            "client_batch_id": "sample-add-null-copy",
            "operations": [
                {"op": "add", "side": "left",
                 "row": {"a": {"t": "int", "v": 2},
                         "b": {"t": "null", "v": None}}},
            ],
        },
        {
            # Same id replayed: must be an idempotent no-op.
            "endpoint": "batch",
            "client_batch_id": "sample-add-null-copy",
            "operations": [
                {"op": "add", "side": "left",
                 "row": {"a": {"t": "int", "v": 2},
                         "b": {"t": "null", "v": None}}},
            ],
        },
        {
            # One batch touching both sides atomically: retract a left (1,a)
            # and correct a right (NULL,z) into (3,c). Row ids come from one
            # global sequence: left import takes 1..7, right import 8..13.
            "endpoint": "batch",
            "client_batch_id": "sample-mixed-batch",
            "operations": [
                {"op": "retract", "side": "left", "row_id": 1},
                {"op": "correct", "side": "right", "row_id": 12,
                 "row": {"x": {"t": "int", "v": 3},
                         "y": {"t": "text", "v": "c"}}},
            ],
        },
    ]


# Expected multiplicities after all canned batches, keyed by canonical tuple:
# (left, right, union, intersect, except).
HAND_EXPECTED_FINAL = {
    "i:1|s:\"a\"": (2, 2, 4, 2, 0),
    "f:1.0|s:\"a\"": (1, 0, 1, 0, 1),
    "i:2|n:\u2205": (3, 1, 4, 1, 2),
    "i:3|s:\"c\"": (1, 2, 3, 1, 0),
    "n:\u2205|s:\"z\"": (0, 1, 1, 0, 0),
}
