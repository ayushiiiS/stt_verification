"""Shared Sarvam STT batch progress tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROGRESS_PATH = BASE_DIR / "stt_progress.json"


def read_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {
            "running": False,
            "total": 0,
            "skipped": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "savedTotal": 0,
            "workers": 0,
            "percent": 0,
            "updatedAt": None,
        }
    try:
        with PROGRESS_PATH.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {
            "running": False,
            "total": 0,
            "skipped": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "savedTotal": 0,
            "workers": 0,
            "percent": 0,
            "updatedAt": None,
        }


def write_progress(**fields) -> dict:
    current = read_progress() if PROGRESS_PATH.exists() else {}
    current.update(fields)
    current["updatedAt"] = datetime.now(timezone.utc).isoformat()
    total = int(current.get("total") or 0)
    saved_total = int(current.get("savedTotal") or 0)
    current["percent"] = round((saved_total / total) * 100, 1) if total else 0
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=2)
    tmp.replace(PROGRESS_PATH)
    return current
