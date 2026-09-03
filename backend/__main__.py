"""Start the API from inside this folder (portable: no parent-repo imports).

    cd backend
    python __main__.py

Or from the parent of this folder (folder must still be named `backend`):

    python -m backend
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from paths import apply_model_cache_env  # noqa: E402

apply_model_cache_env()

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
