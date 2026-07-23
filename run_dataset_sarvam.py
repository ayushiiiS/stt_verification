#!/usr/bin/env python3
"""Run Sarvam STT for a dataset from uploads/<dataset>/calls.json."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import load_env  # noqa: F401

from stt_runner import start_stt_job, is_running, read_progress

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"


def _oid(value) -> str:
    if isinstance(value, dict) and "$oid" in value:
        return str(value["$oid"])
    return str(value or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Dataset id (e.g. karan-spinny)")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    calls_path = UPLOADS_DIR / args.dataset / "calls.json"
    if not calls_path.exists():
        raise SystemExit(f"Missing {calls_path}")

    payload = json.loads(calls_path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else []
    calls: list[dict] = []
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        call_id = _oid(item.get("callLogId") or item.get("id") or item.get("_id"))
        if not call_id:
            continue
        order.append(call_id)
        calls.append(
            {
                "id": call_id,
                "public_url": item.get("recordingUrl") or item.get("public_url") or "",
                "human": item.get("humanRecordingUrl") or item.get("human") or "",
                "agent": item.get("agentRecordingUrl") or item.get("agent") or "",
                "messages": item.get("messages") or [],
            }
        )

    result = start_stt_job(
        dataset=args.dataset,
        uploads_dir=UPLOADS_DIR,
        calls=calls,
        call_order=order,
        on_saved=lambda _cid, _entry: None,
        workers=max(1, args.workers),
        resume=not args.no_resume,
    )
    print("Started:", result, flush=True)
    if not result.get("ok"):
        raise SystemExit(1)

    while is_running(args.dataset):
        progress = read_progress(UPLOADS_DIR, args.dataset)
        print(
            f"  {progress.get('completed', 0)}/{progress.get('total', 0)} done, "
            f"{progress.get('failed', 0)} failed, "
            f"{progress.get('savedTotal', 0)} saved",
            flush=True,
        )
        time.sleep(15)

    progress = read_progress(UPLOADS_DIR, args.dataset)
    print("Finished:", progress, flush=True)


if __name__ == "__main__":
    main()
