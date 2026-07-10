#!/usr/bin/env python3
"""Generate Sarvam STT transcripts for the first N calls."""

from __future__ import annotations

import load_env  # noqa: F401

import argparse
import csv
import json
import os
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from stt_progress import read_progress, write_progress
from sarvam_stt import SARVAM_MODEL, SARVAM_MODE, transcribe_audio_url
from transcript_utils import CALL_LIMIT, align_stt_segments, visible_messages

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "muthoot_with_public_urls .csv"
TRANSCRIPTS_PATH = BASE_DIR / "ai-agents-production.transcripts.json"
SARVAM_PATH = BASE_DIR / "sarvam_transcripts.json"
SARVAM_LOCK_PATH = BASE_DIR / "sarvam_transcripts.lock"
DEFAULT_WORKERS = int(os.environ.get("SARVAM_PARALLEL_WORKERS", "5"))


class TranscriptStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = SARVAM_LOCK_PATH
        self._lock = threading.Lock()
        self.data = self._read()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            print(f"Warning: {self.path} is corrupted; starting from empty store", flush=True)
            return {}

    def _acquire_file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("w")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def has_messages(self, call_id: str) -> bool:
        with self._lock:
            lock_handle = self._acquire_file_lock()
            try:
                self.data = self._read()
                entry = self.data.get(call_id)
                return bool(entry and entry.get("messages"))
            finally:
                lock_handle.close()

    def save_entry(self, call_id: str, entry: dict) -> None:
        with self._lock:
            lock_handle = self._acquire_file_lock()
            try:
                self.data = self._read()
                self.data[call_id] = entry
                tmp = self.path.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                tmp.replace(self.path)
            finally:
                lock_handle.close()

    def count(self) -> int:
        with self._lock:
            lock_handle = self._acquire_file_lock()
            try:
                self.data = self._read()
                return len(self.data)
            finally:
                lock_handle.close()


def load_calls(limit: int) -> list[dict]:
    audio_by_id: dict[str, str] = {}
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            audio_by_id[row["_id"]] = row.get("public_url", "")

    with TRANSCRIPTS_PATH.open(encoding="utf-8") as handle:
        transcripts = json.load(handle)

    calls: list[dict] = []
    for item in transcripts:
        call_id = item["callLogId"]["$oid"]
        calls.append(
            {
                "id": call_id,
                "public_url": audio_by_id.get(call_id, ""),
                "messages": item.get("messages", []),
            }
        )

    calls.sort(key=lambda row: row["id"])
    return calls[:limit]


def transcribe_call(call: dict) -> dict:
    call_id = call["id"]
    original_messages = visible_messages(call["messages"])
    segments, raw_payload = transcribe_audio_url(call["public_url"])
    stt_messages = align_stt_segments(original_messages, segments)
    return {
        "callLogId": call_id,
        "model": SARVAM_MODEL,
        "mode": SARVAM_MODE,
        "segments": segments,
        "messages": stt_messages,
        "raw": raw_payload,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Sarvam STT transcripts")
    parser.add_argument("--limit", type=int, default=CALL_LIMIT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--resume", action="store_true", help="Skip calls already generated")
    parser.add_argument("--call-id", help="Generate for a single call ID")
    args = parser.parse_args()

    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")

    calls = load_calls(args.limit)
    store = TranscriptStore(SARVAM_PATH)

    if args.call_id:
        calls = [call for call in calls if call["id"] == args.call_id]
        if not calls:
            raise SystemExit(f"Call {args.call_id} not found in first {args.limit} calls")

    pending = []
    skipped = 0
    for call in calls:
        if args.resume and store.has_messages(call["id"]):
            skipped += 1
            continue
        pending.append(call)

    total = len(calls)
    pending_total = len(pending)
    print(
        f"Total calls: {total} | already done: {skipped} | to process: {pending_total} | workers: {args.workers}",
        flush=True,
    )

    if not pending:
        write_progress(
            running=False,
            total=total,
            skipped=skipped,
            pending=0,
            completed=0,
            failed=0,
            savedTotal=store.count(),
            workers=args.workers,
        )
        print(f"Nothing to do. {store.count()} Sarvam transcripts in {SARVAM_PATH}", flush=True)
        return

    write_progress(
        running=True,
        total=total,
        skipped=skipped,
        pending=pending_total,
        completed=0,
        failed=0,
        savedTotal=store.count(),
        workers=args.workers,
    )

    completed = 0
    failed = 0
    print_lock = threading.Lock()

    def report_progress() -> None:
        write_progress(
            running=True,
            total=total,
            skipped=skipped,
            pending=pending_total,
            completed=completed,
            failed=failed,
            savedTotal=store.count(),
            workers=args.workers,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(transcribe_call, call): call for call in pending}
        for future in as_completed(futures):
            call = futures[future]
            call_id = call["id"]
            try:
                entry = future.result()
                store.save_entry(call_id, entry)
                completed += 1
                report_progress()
                with print_lock:
                    print(
                        f"[{completed + failed}/{pending_total}] saved {call_id} "
                        f"({len(entry['segments'])} segments)",
                        flush=True,
                    )
            except Exception as exc:
                failed += 1
                report_progress()
                with print_lock:
                    print(
                        f"[{completed + failed}/{pending_total}] failed {call_id}: {exc}",
                        flush=True,
                    )

    write_progress(
        running=False,
        total=total,
        skipped=skipped,
        pending=0,
        completed=completed,
        failed=failed,
        savedTotal=store.count(),
        workers=args.workers,
    )

    print(
        f"Done. saved={completed} failed={failed} total_in_file={store.count()} -> {SARVAM_PATH}",
        flush=True,
    )


if __name__ == "__main__":
    main()
