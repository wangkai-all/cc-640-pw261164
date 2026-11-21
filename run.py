#!/usr/bin/env python3
"""Entry point: python run.py [--db data/multiset.db] [--port 8000]."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Multiset set-operations server")
    parser.add_argument("--db", default="data/multiset.db",
                        help="SQLite database file (created if absent)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.db, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
