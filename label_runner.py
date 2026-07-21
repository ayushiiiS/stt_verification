"""Background auto-labeling jobs for dataset tabs."""

from __future__ import annotations

import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from json_format import dump_numbered
from label_client import build_transcript_text, classify_transcript

DEFAULT_WORKERS = int(__import__("os").environ.get("LABEL_PARALLEL_WORKERS", "3"))

_jobs_lock = threading.Lock()
_running: dict[str, bool] = {}
_cancel: dict[str, threading.Event] = {}
_executors: dict[str, ThreadPoolExecutor] = {}


def is_running(dataset: str) -> bool:
    with _jobs_lock:
        return bool(_running.get(dataset))


def clear_stale_progress(uploads_dir: Path, dataset: str) -> dict:
    progress = read_progress(uploads_dir, dataset)
    if progress.get("running") and not is_running(dataset):
        return write_progress(uploads_dir, dataset, running=False)
    return progress


def stop_label_job(*, dataset: str, uploads_dir: Path) -> dict:
    with _jobs_lock:
        active = bool(_running.get(dataset))
        cancel = _cancel.get(dataset)
        executor = _executors.get(dataset)

    if cancel:
        cancel.set()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)

    if active:
        return {"ok": True, "stopped": True, "dataset": dataset, "running": True}

    progress = clear_stale_progress(uploads_dir, dataset)
    if progress.get("running"):
        return {"ok": True, "stopped": True, "dataset": dataset, "wasStale": True}
    return {"ok": False, "error": "Labeling is not running for this dataset", "running": False}


def progress_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "label_progress.json"


def labels_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "call_labels.json"


def lock_path(uploads_dir: Path, dataset: str) -> Path:
    return uploads_dir / dataset / "call_labels.lock"


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
        import json

        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {"running": False, "total": 0, "percent": 0}


def write_progress(uploads_dir: Path, dataset: str, **fields) -> dict:
    import json

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

        gcs_storage.push_dataset_file(uploads_dir, dataset, "label_progress.json")
    except Exception as exc:  # noqa: BLE001
        print(f"GCS label progress sync failed: {exc}", flush=True)
    return current


class LabelStore:
    def __init__(self, path: Path, lock: Path, call_order: list[str]) -> None:
        self.path = path
        self.lock_path = lock
        self.call_order = call_order
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        import json

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

    def get_entry(self, call_id: str) -> dict | None:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                data = self._read()
                entry = data.get(call_id)
                return entry if isinstance(entry, dict) else None
            finally:
                lock_handle.close()

    def has_auto_label(self, call_id: str) -> bool:
        entry = self.get_entry(call_id)
        return bool(entry and entry.get("domain"))

    def is_human_labeled(self, call_id: str) -> bool:
        entry = self.get_entry(call_id)
        return bool(entry and entry.get("labeledBy") == "human")

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

            dataset = self.path.parent.name
            gcs_storage.push_labels(dataset, call_id, entry)
        except Exception as exc:  # noqa: BLE001
            print(f"GCS labels sync failed: {exc}", flush=True)

    def count(self) -> int:
        with self._lock:
            lock_handle = self._file_lock()
            try:
                return len(self._read())
            finally:
                lock_handle.close()


def _label_one(call: dict) -> dict:
    call_id = call["id"]
    transcript_text = build_transcript_text(call.get("messages") or [])
    if not transcript_text.strip():
        raise ValueError("insufficient_transcript")
    result = classify_transcript(transcript_text)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "callLogId": call_id,
        "domain": result["domain"],
        "subdomain": result["subdomain"],
        "isCustom": False,
        "labeledBy": "auto",
        "labeledByUser": "",
        "updatedAt": now,
        "source": "original",
        "auto": {
            "domain": result["domain"],
            "subdomain": result["subdomain"],
            "domainConfidence": result.get("domainConfidence"),
            "subdomainConfidence": result.get("subdomainConfidence"),
            "rationale": result.get("rationale") or "",
            "model": result.get("model") or "",
            "labeledAt": now,
        },
    }


def start_label_job(
    *,
    dataset: str,
    uploads_dir: Path,
    calls: list[dict],
    call_order: list[str],
    on_saved,
    workers: int | None = None,
    resume: bool = True,
    force: bool = False,
) -> dict:
    with _jobs_lock:
        if _running.get(dataset):
            return {
                "ok": False,
                "error": "Labeling already running for this dataset",
                "running": True,
            }
        _running[dataset] = True

    worker_count = max(1, workers or DEFAULT_WORKERS)
    store = LabelStore(
        labels_path(uploads_dir, dataset),
        lock_path(uploads_dir, dataset),
        call_order,
    )

    pending: list[dict] = []
    skipped = 0
    for call in calls:
        call_id = call["id"]
        if resume and not force and store.is_human_labeled(call_id):
            skipped += 1
            continue
        if resume and not force and store.has_auto_label(call_id):
            skipped += 1
            continue
        messages = call.get("messages") or []
        if not build_transcript_text(messages).strip():
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

    cancel = threading.Event()
    with _jobs_lock:
        _cancel[dataset] = cancel

    def runner() -> None:
        completed = 0
        failed = 0
        cancelled = 0
        executor: ThreadPoolExecutor | None = None
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

            executor = ThreadPoolExecutor(max_workers=worker_count)
            with _jobs_lock:
                _executors[dataset] = executor
            futures = {
                executor.submit(_label_one, call): call
                for call in pending
            }
            for future in as_completed(futures):
                if cancel.is_set():
                    for pending_future in futures:
                        if pending_future.cancel():
                            cancelled += 1
                    break
                call = futures[future]
                call_id = call["id"]
                try:
                    entry = future.result()
                    entry["number"] = (
                        call_order.index(call_id) + 1 if call_id in call_order else None
                    )
                    if force and store.is_human_labeled(call_id):
                        existing = store.get_entry(call_id) or {}
                        entry["labeledBy"] = "human"
                        entry["labeledByUser"] = existing.get("labeledByUser") or ""
                        entry["domain"] = existing.get("domain") or entry["domain"]
                        entry["subdomain"] = existing.get("subdomain") or entry["subdomain"]
                        entry["isCustom"] = bool(existing.get("isCustom"))
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
                    pending=max(0, len(pending) - completed - failed - cancelled),
                    completed=completed,
                    failed=failed,
                    savedTotal=store.count(),
                    workers=worker_count,
                )
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
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
                _cancel.pop(dataset, None)
                _executors.pop(dataset, None)

    threading.Thread(target=runner, daemon=True, name=f"label-{dataset}").start()
    return {
        "ok": True,
        "running": True,
        "dataset": dataset,
        "total": total,
        "pending": len(pending),
        "skipped": skipped,
        "workers": worker_count,
    }


def label_single_call(call: dict, *, existing: dict | None = None) -> dict:
    """Classify one call from original messages; optional existing human entry to preserve."""
    entry = _label_one(call)
    if existing and existing.get("labeledBy") == "human":
        entry["domain"] = existing.get("domain") or entry["domain"]
        entry["subdomain"] = existing.get("subdomain") or entry["subdomain"]
        entry["isCustom"] = bool(existing.get("isCustom"))
        entry["labeledBy"] = "human"
        entry["labeledByUser"] = existing.get("labeledByUser") or ""
    return entry
