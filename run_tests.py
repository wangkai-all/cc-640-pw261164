#!/usr/bin/env python3
"""Run the whole test suite with the project virtualenv's interpreter.

Usage:  .venv/bin/python run_tests.py
Every test file is a standalone script (no pytest dependency).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = [
    "tests/test_hand_sample.py",
    "tests/test_duckdb_replay.py",
    "tests/test_atomicity.py",
    "tests/test_http_api.py",
]


def main() -> int:
    failures = []
    for rel in TESTS:
        print(f"\n=== {rel} ===")
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / rel)],
            cwd=ROOT,
        )
        if proc.returncode != 0:
            failures.append(rel)
    print("\n----------------------------------------")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print(f"ALL {len(TESTS)} TEST MODULES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
