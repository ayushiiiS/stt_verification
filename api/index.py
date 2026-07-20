import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import app, load_data

# Lazy/safe boot: never crash the whole function if GCS hydrate is slow.
try:
    load_data()
except Exception as exc:  # noqa: BLE001
    print(f"load_data failed at import (will retry on demand): {exc}", flush=True)
