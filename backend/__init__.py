"""Standalone FastAPI product-detection service.

This package is self-contained: it does not import anything from a parent repo.
Put this folder on PYTHONPATH (or cd into it) and run `python -m backend` from
the parent of this folder, or `python __main__.py` from inside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
