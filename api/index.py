import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import app

# Datasets hydrate lazily on first API/page request — keeps cold starts fast.
