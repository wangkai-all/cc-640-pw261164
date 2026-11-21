"""Shared pytest-free test helpers.

Importing this module puts the project root on sys.path so the app package
resolves under ``python -I`` (isolated mode strips both cwd and PYTHONPATH).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
