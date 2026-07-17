"""Background Sarvam STT jobs for any dataset tab."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from json_format import dump_numbered
from sarvam_stt import SARVAM_MODE, SARVAM_MODEL, transcribe_audio_url
from transcript_utils import align_stt_segments, visible_messages

DEFAULT_WORKERS = int(os.environ.get("SARVAM_PARALLEL_WORKERS", "3"))

_jobs_lock = threading.Lock()
_running: dict[str, bool] = {}


def is_running(dataset: str) -> bool:
    with _jobs_lock:
        return bool(_running.get(dataset))


def progress_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "stt_progress.json"


def sarvam_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "sarvam_transcripts.json"


def lock_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "sarvam_transcripts.lock"


def read_progress(uploads_dir: Path, dataset: str) -> dict:
    path = progress_path(uploads_dir, dataset)
    if not path.exists():
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
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {"running": False, "total": 0, "percent": 0}


def write_progress(uploads_dir: Path, dataset: str, **fields) -> dict:
    path = progress_path(uploads_dir, dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_progress(uploads_dir, dataset)
    current.update(fields)
    current["updatedAt"] = datetime.now(timezone.utc).isoformat()
    total = int(current.get("total") or 0)
    saved_total = int(current.get("savedTotal") or 0)
    current["percent"] = round((saved_total / total) * 100, 1) if total else 0
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)
    try:
        import gcs_storage

        gcs_storage.push_dataset_file(uploads_dir, dataset, "stt_progress.json")
    except Exception as exc:  # noqa: BLE001
        print(f"GCS stt progress sync failed: {exc}", flush=True)
    return current


class SarvamStore:
    def __init__(self, path: Path, lock: Path, call_order: list[str]) -> None:
        self.path = path
        self.lock_path = lock
        self.call_order = call_order
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            return {}

    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("w")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def has_messages(self, call_id: str) -> bool:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                data = self._read()
                entry = data.get(call_id)
                return bool(entry and (entry.get("messages") or entry.get("segments")))
            finally:
                lock_handle.close()

    def save_entry(self, call_id: str, entry: dict) -> None:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                data = self._read()
                data[call_id] = entry
                dump_numbered(self.path, data, self.call_order)
            finally:
                lock_handle.close()
        try:
            import gcs_storage

            # path is uploads/<dataset>/sarvam_transcripts.json
            dataset = self.path.parent.name
            gcs_storage.push_dataset_file(self.path.parent.parent, dataset, "sarvam_transcripts.json")
        except Exception as exc:  # noqa: BLE001
            print(f"GCS sarvam sync failed: {exc}", flush=True)

    def count(self) -> int:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                return len(self._read())
            finally:
                lock_handle.close()

    def load_all(self) -> dict:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                return self._read()
            finally:
                lock_handle.close()


def _transcribe_one(call: dict) -> dict:
    call_id = call["id"]
    url = call.get("public_url") or ""
    if not url:
        raise ValueError("Missing public audio URL")
    original_messages = visible_messages(call.get("messages") or [])
    segments, raw_payload = transcribe_audio_url(url)
    stt_messages = (
        align_stt_segments(original_messages, segments) if original_messages else []
    )
    if not stt_messages and segments:
        stt_messages = [
            {
                "_id": f"stt-{i + 1}",
                "role": seg.get("role", "assistant"),
                "content": seg.get("content", ""),
                "type": "message",
                "createdAt": "",
            }
            for i, seg in enumerate(segments)
        ]
    return {
        "callLogId": call_id,
        "model": SARVAM_MODEL,
        "mode": SARVAM_MODE,
        "segments": segments,
        "messages": stt_messages,
        "raw": raw_payload,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def start_stt_job(
    *,
    dataset: str,
    uploads_dir: Path,
    calls: list[dict],
    call_order: list[str],
    on_saved,
    workers: int | None = None,
    resume: bool = True,
) -> dict:
    """Start a background STT job. `on_saved(call_id, entry)` updates in-memory store."""
    with _jobs_lock:
        if _running.get(dataset):
            return {"ok": False, "error": "STT already running for this dataset", "running": True}
        _running[dataset] = True

    worker_count = max(1, workers or DEFAULT_WORKERS)
    store = SarvamStore(
        sarvam_path(uploads_dir, dataset),
        lock_path(uploads_dir, dataset),
        call_order,
    )

    pending: list[dict] = []
    skipped = 0
    for call in calls:
        if resume and store.has_messages(call["id"]):
            skipped += 1
            continue
        if not call.get("public_url"):
            skipped += 1
            continue
        pending.append(call)

    total = len(calls)
    write_progress(
        uploads_dir,
        dataset,
        running=True,
        total=total,
        skipped=skipped,
        pending=len(pending),
        completed=0,
        failed=0,
        savedTotal=store.count(),
        workers=worker_count,
    )

    def runner() -> None:
        completed = 0
        failed = 0
        try:
            if not pending:
                write_progress(
                    uploads_dir,
                    dataset,
                    running=False,
                    total=total,
                    skipped=skipped,
                    pending=0,
                    completed=0,
                    failed=0,
                    savedTotal=store.count(),
                    workers=worker_count,
                )
                return

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(_transcribe_one, call): call for call in pending
                }
                for future in as_completed(futures):
                    call = futures[future]
                    call_id = call["id"]
                    try:
                        entry = future.result()
                        entry["number"] = call_order.index(call_id) + 1 if call_id in call_order else None
                        store.save_entry(call_id, entry)
                        on_saved(call_id, entry)
                        completed += 1
                    except Exception:
                        failed += 1
                    write_progress(
                        uploads_dir,
                        dataset,
                        running=True,
                        total=total,
                        skipped=skipped,
                        pending=len(pending),
                        completed=completed,
                        failed=failed,
                        savedTotal=store.count(),
                        workers=worker_count,
                    )
        finally:
            write_progress(
                uploads_dir,
                dataset,
                running=False,
                total=total,
                skipped=skipped,
                pending=0,
                completed=completed,
                failed=failed,
                savedTotal=store.count(),
                workers=worker_count,
            )
            with _jobs_lock:
                _running[dataset] = False

    threading.Thread(target=runner, daemon=True, name=f"stt-{dataset}").start()
    return {
        "ok": True,
        "running": True,
        "dataset": dataset,
        "total": total,
        "pending": len(pending),
        "skipped": skipped,
        "workers": worker_count,
    }
