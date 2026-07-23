#!/usr/bin/env python3
"""Transcript review UI for multi-client call recordings."""

from __future__ import annotations

import load_env  # noqa: F401

import csv
import json
import os
import re
import shutil
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from auth import (
    authenticate,
    can_manage_sarvam_stt,
    can_manage_label_llm,
    current_user,
    register_user,
    require_login_before_request,
)
import gcs_storage
from json_format import dump_numbered, dumps_numbered
from stt_runner import (
    clear_stale_progress,
    is_running as stt_is_running,
    read_progress as read_stt_progress,
    sarvam_path as dataset_sarvam_path,
    start_stt_job,
    stop_stt_job,
)
from label_client import label_api_key
from label_runner import (
    clear_stale_progress as clear_stale_label_progress,
    is_running as label_is_running,
    label_single_call,
    labels_path as dataset_labels_path,
    read_progress as read_label_progress,
    start_label_job,
    stop_label_job,
)
from taxonomy import normalize_label, validate_label


from transcript_utils import (
    align_stt_segments,
    match_segments_to_turns,
    timings_from_created_at,
    timings_from_matched_segments,
    timings_from_stt_messages,
    timings_from_stt_segments,
    clean_saved_messages,
    clean_saved_timings,
    default_final_messages,
    preview_text,
    visible_messages,
)

BASE_DIR = Path(__file__).resolve().parent
ALL_DATA_DIR = BASE_DIR / "all_data"


def _resolve_uploads_dir() -> Path:
    """Use /tmp on Vercel (read-only deploy FS); otherwise local uploads/."""
    if (os.environ.get("VERCEL") or "").strip() or (
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or ""
    ).strip():
        path = Path("/tmp/golden_set_uploads")
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = BASE_DIR / "uploads"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except OSError:
        fallback = Path("/tmp/golden_set_uploads")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


UPLOADS_DIR = _resolve_uploads_dir()
# Keep auth users.json on the same writable root.
os.environ.setdefault("GOLDEN_SET_UPLOADS_DIR", str(UPLOADS_DIR))

DATASET_META = (
    {"id": "indiamart", "label": "IndiaMART"},
    {"id": "abhfl", "label": "ABHFL"},
    {"id": "amber", "label": "Amber"},
    {"id": "muthoot", "label": "Muthoot"},
)
DATASETS = tuple(item["id"] for item in DATASET_META)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or "golden-set-dev-secret"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if (os.environ.get("VERCEL") or "").strip():
    app.config["SESSION_COOKIE_SECURE"] = True
app.before_request(require_login_before_request)

calls_by_id: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
call_order: dict[str, list[str]] = {name: [] for name in DATASETS}
corrections: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
_transliterate_cache: dict[str, list[str]] = {}
_transliterate_http: Any | None = None
sarvam_by_dataset: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
labels_by_dataset: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
phrase_cache: dict[str, list[dict]] = {}
_data_loaded = False


@app.before_request
def ensure_data_loaded():
    """Keep login/signup fast — only ensure users; datasets load on demand."""
    if request.endpoint in {"login", "signup", "static"}:
        return None
    path = request.path or ""
    if path.startswith("/static/") or path.startswith("/login"):
        return None
    # Users file only — datasets hydrate in resolve_dataset / ensure_dataset_loaded.
    try:
        from auth import load_user_store

        load_user_store()
    except Exception as exc:  # noqa: BLE001
        print(f"user store warm failed: {exc}", flush=True)
    return None


def oid(value) -> str:
    if isinstance(value, dict) and "$oid" in value:
        return value["$oid"]
    return str(value)


def call_id_from_url(url: str) -> str | None:
    match = re.search(r"/recording/([a-f0-9]{24})/", url or "")
    return match.group(1) if match else None


def first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def resolve_dataset(raw: str | None = None) -> str:
    name = (raw or request.args.get("dataset") or "indiamart").strip().lower()
    if name not in DATASETS:
        name = "indiamart"
    ensure_dataset_loaded(name)
    return name


def recording_number(dataset: str, call_id: str) -> int | None:
    try:
        return call_order[dataset].index(call_id) + 1
    except ValueError:
        return None


def corrections_path_for(dataset: str) -> Path:
    """Only UI-saved finals count as saved. Bootstrapped golden files are not auto-loaded."""
    return UPLOADS_DIR / dataset / "corrected_transcripts.json"


def upload_calls_path(dataset: str) -> Path:
    return UPLOADS_DIR / dataset / "calls.json"


def ensure_dataset_dirs(dataset: str) -> None:
    (UPLOADS_DIR / dataset).mkdir(parents=True, exist_ok=True)


def neighbor_ids(dataset: str, call_id: str) -> tuple[str | None, str | None]:
    order = call_order[dataset]
    try:
        idx = order.index(call_id)
    except ValueError:
        return None, None
    prev_id = order[idx - 1] if idx > 0 else None
    next_id = order[idx + 1] if idx + 1 < len(order) else None
    return prev_id, next_id


def save_corrections(dataset: str, call_id: str | None = None) -> None:
    ensure_dataset_dirs(dataset)
    dump_numbered(
        corrections_path_for(dataset),
        corrections[dataset],
        call_order[dataset],
    )
    phrase_cache.pop(dataset, None)
    try:
        if call_id and call_id in corrections[dataset]:
            gcs_storage.push_transcript_final(
                dataset, call_id, corrections[dataset][call_id]
            )
            # Mirror aggregate in the background — don't block the save response.
            threading.Thread(
                target=gcs_storage.push_dataset_file,
                args=(UPLOADS_DIR, dataset, "corrected_transcripts.json"),
                daemon=True,
                name=f"gcs-agg-{dataset}",
            ).start()
        elif call_id and call_id not in corrections[dataset]:
            # Reset / cleared unfit-only record — wipe remote final
            gcs_storage.push_transcript_final(
                dataset,
                call_id,
                {"callLogId": call_id, "messages": [], "cleared": True},
            )
        else:
            gcs_storage.push_dataset_file(
                UPLOADS_DIR, dataset, "corrected_transcripts.json"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"GCS corrections sync failed ({dataset}/{call_id}): {exc}", flush=True)
        raise RuntimeError(f"Failed to save to cloud storage: {exc}") from exc


def ensure_correction_loaded(dataset: str, call_id: str) -> dict | None:
    """Load a single final from GCS when memory/local aggregate is cold."""
    existing = corrections[dataset].get(call_id)
    if existing:
        return existing
    try:
        remote = gcs_storage.download_transcript_final(dataset, call_id)
    except Exception as exc:  # noqa: BLE001
        print(f"GCS final fetch failed ({dataset}/{call_id}): {exc}", flush=True)
        return None
    if isinstance(remote, dict) and remote.get("cleared"):
        return None
    if isinstance(remote, dict) and (
        remote.get("messages") or remote.get("editedBy") or remote.get("unfit")
    ):
        corrections[dataset][call_id] = remote
        return remote
    return None


def load_corrections_file(dataset: str, path: Path | None) -> None:
    if not path or not path.exists():
        corrections[dataset] = {}
        return
    try:
        with path.open(encoding="utf-8") as handle:
            corrections[dataset] = json.load(handle)
    except json.JSONDecodeError:
        corrections[dataset] = {}


def load_corrections_file(dataset: str, path: Path | None) -> None:
    if not path or not path.exists():
        corrections[dataset] = {}
        return
    try:
        with path.open(encoding="utf-8") as handle:
            corrections[dataset] = json.load(handle)
    except json.JSONDecodeError:
        corrections[dataset] = {}


def labels_path_for(dataset: str) -> Path:
    return UPLOADS_DIR / dataset / "call_labels.json"


def load_labels_file(dataset: str, path: Path | None = None) -> None:
    label_path = path or labels_path_for(dataset)
    if not label_path.exists():
        labels_by_dataset[dataset] = {}
        return
    try:
        with label_path.open(encoding="utf-8") as handle:
            labels_by_dataset[dataset] = json.load(handle)
    except json.JSONDecodeError:
        labels_by_dataset[dataset] = {}


def refresh_labels_store(dataset: str) -> None:
    """Reload label aggregate from disk/GCS so export includes latest labels."""
    try:
        gcs_storage.hydrate_aggregate_local(
            UPLOADS_DIR, dataset, "call_labels.json", prefer_remote=True
        )
    except Exception as exc:  # noqa: BLE001
        print(f"GCS label aggregate hydrate failed ({dataset}): {exc}", flush=True)
    load_labels_file(dataset)


def ensure_label_loaded(dataset: str, call_id: str) -> dict | None:
    """Resolve a call label from memory, aggregate file, or per-call GCS."""
    store = labels_by_dataset.setdefault(dataset, {})
    entry = store.get(call_id)
    if isinstance(entry, dict) and (entry.get("domain") or entry.get("subdomain")):
        return entry

    refresh_labels_store(dataset)
    entry = labels_by_dataset.get(dataset, {}).get(call_id)
    if isinstance(entry, dict) and (entry.get("domain") or entry.get("subdomain")):
        return entry

    try:
        remote = gcs_storage.download_labels(dataset, call_id)
    except Exception as exc:  # noqa: BLE001
        print(f"GCS label fetch failed ({dataset}/{call_id}): {exc}", flush=True)
        remote = None
    if isinstance(remote, dict) and (remote.get("domain") or remote.get("subdomain")):
        labels_by_dataset[dataset][call_id] = remote
        return remote
    return entry if isinstance(entry, dict) else None


def save_label_entry(dataset: str, call_id: str) -> None:
    ensure_dataset_dirs(dataset)
    entry = labels_by_dataset[dataset].get(call_id)
    dump_numbered(
        labels_path_for(dataset),
        labels_by_dataset[dataset],
        call_order[dataset],
    )
    if entry:
        try:
            gcs_storage.push_labels(dataset, call_id, entry)
            threading.Thread(
                target=gcs_storage.push_dataset_file,
                args=(UPLOADS_DIR, dataset, "call_labels.json"),
                daemon=True,
                name=f"gcs-labels-{dataset}",
            ).start()
        except Exception as exc:  # noqa: BLE001
            print(f"GCS labels sync failed ({dataset}/{call_id}): {exc}", flush=True)


def label_status(entry: dict | None) -> str:
    if not entry or not entry.get("domain"):
        return "unlabeled"
    if entry.get("labeledBy") == "human":
        return "custom" if entry.get("isCustom") else "human"
    return "auto"


def label_public_view(entry: dict | None) -> dict | None:
    if not entry or not entry.get("domain"):
        return None
    return {
        "domain": entry.get("domain") or "",
        "subdomain": entry.get("subdomain") or "",
        "isCustom": bool(entry.get("isCustom")),
        "labeledBy": entry.get("labeledBy") or "auto",
        "labeledByUser": entry.get("labeledByUser") or "",
        "updatedAt": entry.get("updatedAt"),
        "source": entry.get("source") or "original",
        "status": label_status(entry),
        "auto": entry.get("auto"),
    }


def label_suggestions_for_dataset(dataset: str) -> dict:
    """Distinct domains/subdomains already used in this dataset (for filter UI)."""
    store = labels_by_dataset.get(dataset) or {}
    domains: set[str] = set()
    subdomains: set[str] = set()
    by_domain: dict[str, set[str]] = {}
    for entry in store.values():
        if not isinstance(entry, dict):
            continue
        domain = normalize_label(str(entry.get("domain") or ""))
        subdomain = normalize_label(str(entry.get("subdomain") or ""))
        if not domain:
            continue
        domains.add(domain)
        if subdomain:
            subdomains.add(subdomain)
            by_domain.setdefault(domain, set()).add(subdomain)
    return {
        "domains": sorted(domains),
        "subdomains": sorted(subdomains),
        "byDomain": {key: sorted(values) for key, values in sorted(by_domain.items())},
    }


def ingest_call_entry(
    dataset: str,
    call_id: str,
    *,
    messages: list | None = None,
    public_url: str = "",
    recording_url: str = "",
    stt_messages: list | None = None,
    stt_segments: list | None = None,
    timings: list | None = None,
    extra: dict | None = None,
) -> None:
    call_id = str(call_id).strip()
    if not call_id:
        return

    existing = calls_by_id[dataset].get(call_id, {"id": call_id})
    if messages is not None:
        existing["messages"] = messages
        existing["transcript"] = {
            "callLogId": call_id,
            "messages": messages,
        }
    if public_url:
        existing["public_url"] = public_url
    if recording_url:
        existing["recordingUrl"] = recording_url
    human_url = (extra or {}).get("humanRecordingUrl") or (extra or {}).get("human") or ""
    agent_url = (extra or {}).get("agentRecordingUrl") or (extra or {}).get("agent") or ""
    if human_url:
        existing["humanRecordingUrl"] = human_url
    if agent_url:
        existing["agentRecordingUrl"] = agent_url
    existing.setdefault("public_url", "")
    existing.setdefault("recordingUrl", "")
    existing.setdefault("messages", [])
    if extra:
        existing.update(extra)
    calls_by_id[dataset][call_id] = existing

    if stt_messages is not None or stt_segments is not None or timings is not None:
        entry = sarvam_by_dataset[dataset].setdefault(
            call_id,
            {
                "callLogId": call_id,
                "messages": [],
                "segments": [],
                "raw": {},
            },
        )
        if stt_messages is not None:
            entry["messages"] = stt_messages
        if stt_segments is not None:
            entry["segments"] = stt_segments
        if timings is not None:
            entry["raw"] = {
                **(entry.get("raw") or {}),
                "diarized_transcript": {"entries": timings},
            }


def normalize_uploaded_payload(payload) -> list[dict]:
    """Accept dict-by-id or list of call objects; return normalized list."""
    items: list[dict] = []

    if isinstance(payload, dict):
        # Possibly {"calls": [...]} wrapper
        if "calls" in payload and isinstance(payload["calls"], list):
            payload = payload["calls"]
        else:
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                entry = dict(value)
                entry.setdefault("callLogId", value.get("callLogId") or value.get("id") or key)
                items.append(entry)
            return items

    if isinstance(payload, list):
        for value in payload:
            if isinstance(value, dict):
                items.append(value)
        return items

    raise ValueError("JSON must be an object keyed by call ID or an array of calls")


def apply_uploaded_calls(dataset: str, payload, *, replace: bool = False) -> int:
    items = normalize_uploaded_payload(payload)
    if replace:
        calls_by_id[dataset] = {}
        call_order[dataset] = []
        corrections[dataset] = {}
        sarvam_by_dataset[dataset] = {}
        labels_by_dataset[dataset] = {}
        phrase_cache.pop(dataset, None)
    count = 0
    for item in items:
        call_id = oid(
            item.get("callLogId")
            or item.get("id")
            or item.get("_id")
            or ""
        )
        if not call_id:
            # try extract from URL
            call_id = call_id_from_url(
                item.get("public_url") or item.get("url") or ""
            ) or ""
        if not call_id:
            continue

        messages = item.get("messages")
        if messages is None and isinstance(item.get("transcript"), dict):
            messages = item["transcript"].get("messages")

        public_url = (
            item.get("public_url")
            or item.get("url")
            or item.get("recordingUrl")
            or item.get("recordings")
            or item.get("human")
            or ""
        )
        recording_url = item.get("recordingUrl") or item.get("recordings") or ""
        human_url = item.get("humanRecordingUrl") or item.get("human") or ""
        agent_url = item.get("agentRecordingUrl") or item.get("agent") or ""

        stt_messages = item.get("stt_messages") or item.get("sarvam_messages")
        stt_segments = item.get("segments") or item.get("stt_segments")
        timings = item.get("timings")
        if timings is None:
            raw = item.get("raw") or {}
            if isinstance(raw, dict):
                timings = (raw.get("diarized_transcript") or {}).get("entries")

        # Uploads never mark finals as saved — user must explicitly Save.
        ingest_call_entry(
            dataset,
            call_id,
            messages=messages if isinstance(messages, list) else None,
            public_url=str(public_url or ""),
            recording_url=str(recording_url or ""),
            stt_messages=stt_messages if isinstance(stt_messages, list) else None,
            stt_segments=stt_segments if isinstance(stt_segments, list) else None,
            timings=timings if isinstance(timings, list) else None,
            extra={
                "humanRecordingUrl": human_url,
                "agentRecordingUrl": agent_url,
            },
        )

        count += 1

    call_order[dataset] = sorted(calls_by_id[dataset].keys())
    for call_id, entry in corrections[dataset].items():
        entry["number"] = recording_number(dataset, call_id)
    phrase_cache.pop(dataset, None)
    return count


def persist_uploaded_calls(dataset: str, payload) -> None:
    ensure_dataset_dirs(dataset)
    path = upload_calls_path(dataset)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    # Writes meta + transcript_original (+ queues human/agent/recording audio)
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "calls.json")


_hydrated_datasets: set[str] = set()


def hydrate_persistence(datasets: tuple[str, ...] | None = None) -> None:
    """Pull users + selected tab uploads from GCS before loading into memory."""
    gcs_storage.hydrate_users_file(UPLOADS_DIR / "users.json", prefer_remote=True)
    targets = datasets if datasets is not None else DATASETS
    for dataset in targets:
        gcs_storage.sync_dataset_dir(UPLOADS_DIR, dataset, prefer_remote=True)


def ensure_dataset_loaded(dataset: str) -> None:
    """Hydrate + load a single dataset on first use (cuts cold-start latency)."""
    global _data_loaded
    key = (dataset or "").strip().lower()
    if key not in DATASETS:
        return
    if key in _hydrated_datasets and call_order.get(key):
        pulled = gcs_storage.refresh_companion_aggregates(UPLOADS_DIR, key)
        if pulled or not sarvam_by_dataset.get(key):
            if "sarvam_transcripts.json" in pulled:
                _sarvam_mtime.pop(key, None)
            reload_sarvam_transcripts(key)
        if "call_labels.json" in pulled or not labels_by_dataset.get(key):
            load_labels_file(key)
        return
    try:
        gcs_storage.sync_dataset_dir(UPLOADS_DIR, key, prefer_remote=True)
        calls_by_id[key] = {}
        call_order[key] = []
        corrections[key] = {}
        sarvam_by_dataset[key] = {}
        labels_by_dataset[key] = {}
        phrase_cache.pop(key, None)
        if key == "indiamart":
            load_indiamart()
            load_labels_file(key)
        else:
            load_corrections_file(key, corrections_path_for(key))
            load_labels_file(key)
            load_uploaded_dataset(key)
            path = dataset_sarvam_path(UPLOADS_DIR, key)
            if path.exists():
                try:
                    with path.open(encoding="utf-8") as handle:
                        sarvam_by_dataset[key] = json.load(handle)
                except json.JSONDecodeError:
                    sarvam_by_dataset[key] = {}
            reload_sarvam_transcripts(key)
            if not call_order[key]:
                call_order[key] = sorted(calls_by_id[key].keys())
        _hydrated_datasets.add(key)
        if any(call_order.values()):
            _data_loaded = True
        print(
            f"Loaded dataset {key}: {len(call_order.get(key) or [])} calls, "
            f"{len(sarvam_by_dataset.get(key) or {})} sarvam",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ensure_dataset_loaded({key}) failed: {exc}", flush=True)


def load_data() -> None:
    """Bootstrap users; datasets load lazily via ensure_dataset_loaded."""
    for name in DATASETS:
        calls_by_id[name] = {}
        call_order[name] = []
        corrections[name] = {}
        sarvam_by_dataset[name] = {}
        labels_by_dataset[name] = {}
    phrase_cache.clear()
    _hydrated_datasets.clear()
    gcs_storage.hydrate_users_file(UPLOADS_DIR / "users.json", prefer_remote=True)
    # Warm the default tab only — other tabs hydrate on first request.
    ensure_dataset_loaded("indiamart")
    for name in DATASETS:
        clear_stale_progress(UPLOADS_DIR, name)
        clear_stale_label_progress(UPLOADS_DIR, name)
    print(f"Storage: {gcs_storage.status()}", flush=True)


def load_uploaded_dataset(dataset: str) -> None:
    path = upload_calls_path(dataset)
    if not path.exists():
        bootstrap = BASE_DIR / "data" / f"{dataset}.json"
        if bootstrap.exists():
            ensure_dataset_dirs(dataset)
            shutil.copy2(bootstrap, path)
        else:
            return
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        apply_uploaded_calls(dataset, payload)
    except (json.JSONDecodeError, ValueError):
        return


def load_indiamart() -> None:
    # Prefer an explicit upload over bootstrapped all_data (full replace).
    upload_path = upload_calls_path("indiamart")
    if upload_path.exists():
        load_corrections_file("indiamart", corrections_path_for("indiamart"))
        load_uploaded_dataset("indiamart")
        sarvam_upload = dataset_sarvam_path(UPLOADS_DIR, "indiamart")
        if sarvam_upload.exists():
            try:
                with sarvam_upload.open(encoding="utf-8") as handle:
                    sarvam_by_dataset["indiamart"] = json.load(handle)
            except json.JSONDecodeError:
                sarvam_by_dataset["indiamart"] = {}
        return

    csv_path = first_existing(
        BASE_DIR / "indiamart_final63_public_urls.csv",
        ALL_DATA_DIR / "indiamart_final63_public_urls.csv",
    )
    transcripts_path = first_existing(
        BASE_DIR / "indiamart_63_transcripts.json",
        ALL_DATA_DIR / "indiamart_63_transcripts.json",
    )
    sarvam_path = first_existing(
        dataset_sarvam_path(UPLOADS_DIR, "indiamart"),
        BASE_DIR / "indiamart_sarvam_transcripts.json",
        ALL_DATA_DIR / "indiamart_sarvam_transcripts.json",
    )

    url_by_id: dict[str, str] = {}
    if csv_path:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                call_id = call_id_from_url(
                    row.get("public_url") or row.get("url") or ""
                )
                if not call_id:
                    continue
                url_by_id[call_id] = row.get("public_url") or row.get("url") or ""

    if transcripts_path:
        with transcripts_path.open(encoding="utf-8") as handle:
            transcripts = json.load(handle)
        for item in transcripts:
            call_id = oid(item["callLogId"])
            ingest_call_entry(
                "indiamart",
                call_id,
                messages=item.get("messages", []),
                public_url=url_by_id.get(call_id, ""),
            )

    # Merge public URLs from final export without marking finals as saved
    final_path = first_existing(
        ALL_DATA_DIR / "indiamart_final_with_public_urls.json",
        BASE_DIR / "indiamart_final_with_public_urls.json",
    )
    if final_path:
        with final_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        for item in normalize_uploaded_payload(payload):
            call_id = oid(item.get("callLogId") or item.get("id") or "")
            if not call_id:
                continue
            public_url = item.get("public_url") or item.get("url") or ""
            if public_url:
                ingest_call_entry("indiamart", call_id, public_url=str(public_url))

    call_order["indiamart"] = sorted(calls_by_id["indiamart"].keys())
    load_corrections_file("indiamart", corrections_path_for("indiamart"))

    if sarvam_path and sarvam_path.exists():
        with sarvam_path.open(encoding="utf-8") as handle:
            sarvam_by_dataset["indiamart"] = json.load(handle)

    load_uploaded_dataset("indiamart")


def load_empty_clients() -> None:
    for dataset in DATASETS:
        if dataset == "indiamart":
            continue
        ensure_dataset_loaded(dataset)


def read_dataset_progress(dataset: str) -> dict:
    progress = clear_stale_progress(UPLOADS_DIR, dataset)
    if progress.get("total") or progress.get("running") or progress.get("savedTotal"):
        progress["running"] = stt_is_running(dataset) or bool(progress.get("running"))
        return progress
    if dataset == "indiamart":
        path = first_existing(
            BASE_DIR / "indiamart_stt_progress.json",
            ALL_DATA_DIR / "indiamart_stt_progress.json",
        )
        if path:
            try:
                with path.open(encoding="utf-8") as handle:
                    return json.load(handle)
            except json.JSONDecodeError:
                return {}
    return progress


def read_dataset_label_progress(dataset: str) -> dict:
    progress = clear_stale_label_progress(UPLOADS_DIR, dataset)
    if progress.get("total") or progress.get("running") or progress.get("savedTotal"):
        progress["running"] = label_is_running(dataset) or bool(progress.get("running"))
        return progress
    return progress


_sarvam_mtime: dict[str, float] = {}


def reload_sarvam_transcripts(dataset: str | None = None) -> None:
    targets = [dataset] if dataset in DATASETS else list(DATASETS)
    for name in targets:
        upload_path = dataset_sarvam_path(UPLOADS_DIR, name)
        if gcs_storage.is_enabled() and gcs_storage.aggregate_needs_hydrate(
            upload_path
        ):
            gcs_storage.hydrate_aggregate_local(
                UPLOADS_DIR,
                name,
                "sarvam_transcripts.json",
                prefer_remote=True,
            )

        legacy_paths = []
        if name == "indiamart":
            legacy_paths = [
                BASE_DIR / "indiamart_sarvam_transcripts.json",
                ALL_DATA_DIR / "indiamart_sarvam_transcripts.json",
            ]
        paths = [p for p in [upload_path, *legacy_paths] if p.exists()]
        mtime = max((p.stat().st_mtime for p in paths), default=0.0)
        in_memory = sarvam_by_dataset.get(name) or {}
        if (
            name in sarvam_by_dataset
            and _sarvam_mtime.get(name) == mtime
            and mtime > 0
            and in_memory
        ):
            continue

        merged = dict(sarvam_by_dataset.get(name) or {})
        for path in paths:
            try:
                with path.open(encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    merged.update(loaded)
            except json.JSONDecodeError:
                continue
        sarvam_by_dataset[name] = merged
        _sarvam_mtime[name] = mtime


def _messages_from_stt_entry(
    entry: dict | None, original_messages: list[dict]
) -> list[dict] | None:
    if not entry:
        return None

    # Prefer live alignment from segments so leftover utterance text isn't dropped.
    segments = entry.get("segments") or []
    if segments:
        return align_stt_segments(original_messages, segments)

    messages = [
        msg
        for msg in entry.get("messages", [])
        if msg.get("type") != "language_switch"
    ]
    if messages:
        return messages
    return None


def get_stt_messages(
    dataset: str, call_id: str, original_messages: list[dict]
) -> list[dict] | None:
    entry = sarvam_by_dataset.get(dataset, {}).get(call_id)
    return _messages_from_stt_entry(entry, original_messages)


def get_turn_timings(
    dataset: str,
    call_id: str,
    turn_count: int,
    original_messages: list[dict] | None = None,
) -> list[dict]:
    """Prefer text-matched Sarvam segment bounds; createdAt is fallback."""
    originals = original_messages or []
    entry = sarvam_by_dataset.get(dataset, {}).get(call_id) or {}
    segments = entry.get("segments") or []
    if not segments:
        raw = entry.get("raw") or {}
        diarized = raw.get("diarized_transcript") or {}
        segments = diarized.get("entries") or []

    if segments and originals:
        turn_segments = match_segments_to_turns(originals, segments)
        matched = timings_from_matched_segments(turn_segments)
        matched_hits = sum(1 for t in matched if t.get("start") is not None)
        if matched_hits >= max(1, int(turn_count * 0.5)):
            while len(matched) < turn_count:
                matched.append({"start": None, "end": None})
            return matched[:turn_count]

    stt_messages = _messages_from_stt_entry(entry, originals)
    if stt_messages:
        aligned = timings_from_stt_messages(stt_messages)
        aligned_hits = sum(1 for t in aligned if t.get("start") is not None)
        if aligned_hits >= max(1, int(turn_count * 0.6)):
            while len(aligned) < turn_count:
                aligned.append({"start": None, "end": None})
            return aligned[:turn_count]

    from_created = timings_from_created_at(originals)
    created_hits = sum(1 for t in from_created if t.get("start") is not None)
    if from_created and created_hits >= max(1, int(turn_count * 0.6)):
        while len(from_created) < turn_count:
            from_created.append({"start": None, "end": None})
        return from_created[:turn_count]

    segments = entry.get("segments") or []
    if not segments:
        raw = entry.get("raw") or {}
        diarized = raw.get("diarized_transcript") or {}
        segments = diarized.get("entries") or []

    stt_timings = timings_from_stt_segments(segments, turn_count) if segments else []
    stt_hits = sum(1 for t in stt_timings if t.get("start") is not None)
    if stt_hits and stt_hits >= max(1, int(turn_count * 0.6)):
        return stt_timings

    if stt_timings:
        return stt_timings
    return [{"start": None, "end": None} for _ in range(turn_count)]


def review_status(saved: dict | None) -> str:
    if not saved:
        return "pending"
    if saved.get("unfit"):
        return "unfit"
    once_by = (saved.get("verifiedOnceBy") or "").strip()
    once_at = saved.get("verifiedOnceAt")
    final_by = (saved.get("verifiedBy") or "").strip()
    final_at = saved.get("verifiedAt")
    if final_by and final_at and once_by and once_at:
        return "verified"
    if once_by and once_at:
        return "verified_once"
    # Legacy single-verify records count as first verification only.
    if final_by and final_at:
        return "verified_once"
    if saved.get("messages") or saved.get("editedBy"):
        return "edited"
    return "pending"


def normalize_legacy_verification(saved: dict) -> None:
    """Map old single-verifier saves onto the two-step verification fields."""
    once_by = (saved.get("verifiedOnceBy") or "").strip()
    final_by = (saved.get("verifiedBy") or "").strip()
    if final_by and saved.get("verifiedAt") and not once_by:
        saved["verifiedOnceBy"] = final_by
        saved["verifiedOnceAt"] = saved.get("verifiedAt")
        saved["verifiedBy"] = ""
        saved["verifiedAt"] = None


def _same_reviewer(left: str, right: str) -> bool:
    return bool(left.strip()) and left.strip().lower() == right.strip().lower()


def filter_call_ids(
    dataset: str,
    *,
    search: str = "",
    status: str = "all",
    domain_filter: str = "",
    subdomain_filter: str = "",
    label_status_filter: str = "all",
) -> list[str]:
    order = call_order[dataset]
    dataset_corrections = corrections[dataset]
    sarvam_store = sarvam_by_dataset.get(dataset, {})
    label_store = labels_by_dataset.get(dataset, {})
    filtered = list(order)

    search = (search or "").strip().lower()
    if search:
        if search.isdigit():
            num = int(search)
            filtered = [
                cid
                for i, cid in enumerate(filtered, start=1)
                if i == num or search in cid.lower()
            ]
        else:
            filtered = [cid for cid in filtered if search in cid.lower()]

    if status == "edited":
        filtered = [
            cid
            for cid in filtered
            if review_status(dataset_corrections.get(cid)) == "edited"
        ]
    elif status == "pending":
        filtered = [
            cid
            for cid in filtered
            if review_status(dataset_corrections.get(cid)) == "pending"
        ]
    elif status == "verified_once":
        filtered = [
            cid
            for cid in filtered
            if review_status(dataset_corrections.get(cid)) == "verified_once"
        ]
    elif status == "verified":
        filtered = [
            cid
            for cid in filtered
            if review_status(dataset_corrections.get(cid)) == "verified"
        ]
    elif status == "unfit":
        filtered = [
            cid
            for cid in filtered
            if review_status(dataset_corrections.get(cid)) == "unfit"
        ]
    elif status == "stt":
        filtered = [cid for cid in filtered if cid in sarvam_store]
    elif status == "no_stt":
        filtered = [cid for cid in filtered if cid not in sarvam_store]

    domain_filter = (domain_filter or "").strip().lower()
    if domain_filter:
        filtered = [
            cid
            for cid in filtered
            if (label_store.get(cid) or {}).get("domain", "").lower() == domain_filter
        ]
    subdomain_filter = (subdomain_filter or "").strip().lower()
    if subdomain_filter:
        filtered = [
            cid
            for cid in filtered
            if (label_store.get(cid) or {}).get("subdomain", "").lower() == subdomain_filter
        ]
    label_status_filter = (label_status_filter or "all").strip().lower()
    if label_status_filter == "labeled":
        filtered = [cid for cid in filtered if label_store.get(cid, {}).get("domain")]
    elif label_status_filter == "unlabeled":
        filtered = [cid for cid in filtered if not label_store.get(cid, {}).get("domain")]
    elif label_status_filter in {"auto", "human", "custom"}:
        filtered = [
            cid
            for cid in filtered
            if label_status(label_store.get(cid)) == label_status_filter
        ]
    return filtered


def resolve_export_call_ids(
    dataset: str,
    *,
    call_ids: list[str] | None = None,
    search: str = "",
    status: str = "all",
    domain_filter: str = "",
    subdomain_filter: str = "",
    label_status_filter: str = "all",
) -> list[str]:
    order_set = set(call_order[dataset])
    if call_ids:
        seen: set[str] = set()
        resolved: list[str] = []
        for call_id in call_ids:
            cid = str(call_id or "").strip()
            if not cid or cid not in order_set or cid in seen:
                continue
            seen.add(cid)
            resolved.append(cid)
        if status == "all":
            return resolved
        return [
            cid
            for cid in resolved
            if cid in filter_call_ids(dataset, status=status)
        ]
    return filter_call_ids(
        dataset,
        search=search,
        status=status,
        domain_filter=domain_filter,
        subdomain_filter=subdomain_filter,
        label_status_filter=label_status_filter,
    )


def build_export_entry(dataset: str, call_id: str, saved: dict) -> dict:
    label_entry = ensure_label_loaded(dataset, call_id) or {}
    domain = str(label_entry.get("domain") or "").strip()
    subdomain = str(label_entry.get("subdomain") or "").strip()
    return {
        "callLogId": call_id,
        "domain": domain,
        "subdomain": subdomain,
        "messages": saved.get("messages") or [],
        "updatedAt": saved.get("updatedAt"),
        "editedBy": saved.get("editedBy") or "",
        "verifiedOnceBy": saved.get("verifiedOnceBy") or "",
        "verifiedOnceAt": saved.get("verifiedOnceAt"),
        "verifiedBy": saved.get("verifiedBy") or "",
        "verifiedAt": saved.get("verifiedAt"),
        "status": review_status(saved),
        "public_url": calls_by_id[dataset].get(call_id, {}).get("public_url", ""),
        "label": label_public_view(label_entry if domain or subdomain else None),
    }


def turn_layout_was_edited(
    saved: dict | None, original_messages: list[dict], final_messages: list[dict]
) -> bool:
    """True when the reviewer intentionally added/removed turns (not a partial save)."""
    if not saved:
        return False
    if saved.get("turnLayoutEdited"):
        return True
    if len(final_messages) >= len(original_messages):
        return False
    orig_ids = [msg.get("_id") for msg in original_messages]
    saved_ids = [msg.get("_id") for msg in final_messages]
    # Partial/corrupt saves keep the first N original turns in order.
    return saved_ids != orig_ids[: len(saved_ids)]


def build_call_payload(dataset: str, call_id: str) -> dict:
    call = calls_by_id[dataset][call_id]
    original_messages = visible_messages(call.get("messages", []))
    stt_messages = get_stt_messages(dataset, call_id, original_messages)
    has_stt = stt_messages is not None
    prev_id, next_id = neighbor_ids(dataset, call_id)

    saved = ensure_correction_loaded(dataset, call_id) or corrections[dataset].get(
        call_id
    )
    if saved:
        final_messages = [
            msg
            for msg in saved.get("messages", [])
            if msg.get("type") != "language_switch"
        ]
        if not final_messages:
            final_messages = default_final_messages(
                original_messages, stt_messages, has_stt=has_stt
            )
        elif len(final_messages) < len(original_messages) and not turn_layout_was_edited(
            saved, original_messages, final_messages
        ):
            # Partial/corrupt saves (e.g. a single test turn) must not hide the
            # rest of the transcript — pad missing turns from Original.
            # Skipped when the reviewer intentionally deleted turns.
            final_messages = list(final_messages) + [
                {
                    **msg,
                    "content": msg.get("content", ""),
                }
                for msg in original_messages[len(final_messages) :]
            ]
    else:
        final_messages = default_final_messages(
            original_messages, stt_messages, has_stt=has_stt
        )

    turn_count = max(
        len(original_messages),
        len(final_messages),
        len(stt_messages or []),
    )
    sarvam_entry = sarvam_by_dataset.get(dataset, {}).get(call_id) or {}
    stt_segments = sarvam_entry.get("segments") or []
    if not stt_segments:
        raw = sarvam_entry.get("raw") or {}
        diarized = raw.get("diarized_transcript") or {}
        stt_segments = diarized.get("entries") or []

    saved_timings = (
        clean_saved_timings(saved.get("timings"), len(final_messages))
        if saved and isinstance(saved.get("timings"), list)
        else None
    )
    turn_segment_groups = (
        match_segments_to_turns(original_messages, stt_segments)
        if stt_segments and original_messages
        else []
    )
    while len(turn_segment_groups) < len(final_messages):
        turn_segment_groups.append([])
    turn_segment_groups = turn_segment_groups[: len(final_messages)]

    if saved_timings and len(saved_timings) == len(final_messages):
        timings = saved_timings
    else:
        if turn_segment_groups and any(group for group in turn_segment_groups):
            timings = timings_from_matched_segments(turn_segment_groups)
        else:
            timings = get_turn_timings(
                dataset, call_id, turn_count, original_messages=original_messages
            )
        while len(timings) < len(final_messages):
            timings.append({"start": None, "end": None})
        timings = timings[: len(final_messages)]

    return {
        "id": call_id,
        "dataset": dataset,
        "number": recording_number(dataset, call_id),
        "public_url": call.get("public_url", ""),
        "recordingUrl": call.get("recordingUrl", ""),
        "hasStt": has_stt,
        "messages": original_messages,
        "stt_messages": stt_messages,
        "stt_segments": stt_segments,
        "final_messages": final_messages,
        "timings": timings,
        "timing_segments": turn_segment_groups,
        "edited": call_id in corrections[dataset],
        "turnLayoutEdited": turn_layout_was_edited(
            saved, original_messages, final_messages
        ),
        "status": review_status(saved),
        "editedBy": (saved or {}).get("editedBy") or "",
        "verifiedOnceBy": (saved or {}).get("verifiedOnceBy") or "",
        "verifiedOnceAt": (saved or {}).get("verifiedOnceAt"),
        "verifiedBy": (saved or {}).get("verifiedBy") or "",
        "verifiedAt": (saved or {}).get("verifiedAt"),
        "unfitBy": (saved or {}).get("unfitBy") or "",
        "unfitAt": (saved or {}).get("unfitAt"),
        "unfitReason": (saved or {}).get("unfitReason") or "",
        "updatedAt": saved.get("updatedAt") if saved else None,
        "sttGeneratedAt": sarvam_by_dataset.get(dataset, {})
        .get(call_id, {})
        .get("generatedAt")
        if has_stt
        else None,
        "prevId": prev_id,
        "nextId": next_id,
        "label": label_public_view(labels_by_dataset.get(dataset, {}).get(call_id)),
    }


PHRASE_STOPWORDS = {
    "aap",
    "aur",
    "hai",
    "hain",
    "haan",
    "ji",
    "ko",
    "ka",
    "ki",
    "ke",
    "se",
    "to",
    "me",
    "main",
    "the",
    "and",
    "for",
    "you",
}

PHRASE_DROP_TOKENS = {"break", "time", "sec", "secs", "second", "seconds"}

ROMAN_QUERY_ALIASES = {
    "aap": ["आप", "आपको", "आपका", "आपकी", "आपके"],
    "aapko": ["आपको"],
    "aapka": ["आपका"],
    "aapki": ["आपकी"],
    "aapke": ["आपके"],
    "kya": ["क्या"],
    "hai": ["है"],
    "hain": ["हैं"],
    "nahi": ["नहीं"],
    "nhi": ["नहीं"],
    "haan": ["हाँ"],
    "han": ["हाँ"],
    "ji": ["जी"],
    "namaste": ["नमस्ते"],
    "dhanyawad": ["धन्यवाद", "धन्यवाद्"],
}


def normalize_phrase_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\u0900-\u097F\s'-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def tokenize_for_phrases(text: str) -> list[str]:
    text = normalize_phrase_text(text)
    if not text:
        return []
    return [
        tok
        for tok in text.split(" ")
        if tok and tok not in PHRASE_DROP_TOKENS and not tok.isdigit()
    ]


def phrase_query_variants(query: str) -> list[str]:
    variants = [query]
    lowered = query.lower()
    if lowered in ROMAN_QUERY_ALIASES:
        variants.extend(ROMAN_QUERY_ALIASES[lowered])
    # Preserve order, remove duplicates.
    seen = set()
    result = []
    for item in variants:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def collect_phrase_sources(dataset: str) -> list[tuple[str, int, str]]:
    """Return (text, weight, source) from strongest to weakest sources."""
    sources: list[tuple[str, int, str]] = []

    for entry in corrections[dataset].values():
        status = review_status(entry)
        weight = 12 if status == "verified" else 10 if status == "verified_once" else 9
        label = "verified" if status == "verified" else "verified_once" if status == "verified_once" else "saved"
        for msg in entry.get("messages") or []:
            sources.append((msg.get("content") or "", weight, label))

    for entry in sarvam_by_dataset.get(dataset, {}).values():
        for msg in entry.get("messages") or []:
            sources.append((msg.get("content") or "", 4, "stt"))
        for segment in entry.get("segments") or []:
            sources.append((segment.get("content") or "", 3, "stt"))

    for call_id in call_order.get(dataset, []):
        call = calls_by_id[dataset].get(call_id) or {}
        for msg in visible_messages(call.get("messages", [])):
            sources.append((msg.get("content") or "", 1, "original"))

    return sources


def build_phrase_index(dataset: str) -> list[dict]:
    if dataset in phrase_cache:
        return phrase_cache[dataset]

    counter: Counter[str] = Counter()
    source_counter: dict[str, Counter[str]] = {}
    length_counter: Counter[str] = Counter()

    for text, weight, source in collect_phrase_sources(dataset):
        tokens = tokenize_for_phrases(text)
        if not tokens:
            continue
        max_n = min(8, len(tokens))
        for n in range(1, max_n + 1):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i : i + n])
                if len(phrase) < 3 or len(phrase) > 110:
                    continue
                if n == 1 and (phrase in PHRASE_STOPWORDS or len(phrase) < 4):
                    continue
                # Prefer useful multi-word snippets over isolated words.
                phrase_weight = weight + (n * 2 if n > 1 else 0)
                counter[phrase] += phrase_weight
                length_counter[phrase] = max(length_counter[phrase], n)
                source_counter.setdefault(phrase, Counter())[source] += weight

    ranked: list[dict] = []
    for phrase, score in counter.most_common(4000):
        if score < 4:
            continue
        source_counts = source_counter.get(phrase) or Counter()
        best_source = source_counts.most_common(1)[0][0] if source_counts else "history"
        ranked.append(
            {
                "phrase": phrase,
                "count": int(score),
                "score": int(score),
                "source": best_source,
                "words": int(length_counter.get(phrase, 1)),
            }
        )

    phrase_cache[dataset] = ranked
    return ranked


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))

    error = ""
    success = ""
    mode = (request.args.get("mode") or "login").strip().lower()
    if mode not in {"login", "signup"}:
        mode = "login"

    if request.method == "POST":
        mode = (request.form.get("mode") or mode).strip().lower()
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if mode == "signup":
            if password != confirm:
                error = "Passwords do not match"
            else:
                user, reg_error = register_user(username, password)
                if reg_error:
                    error = reg_error
                else:
                    session.clear()
                    session["user"] = user
                    session.permanent = True
                    return redirect(url_for("index"))
        else:
            user = authenticate(username, password)
            if user:
                session.clear()
                session["user"] = user
                session.permanent = True
                next_url = request.args.get("next") or url_for("index")
                if not next_url.startswith("/"):
                    next_url = url_for("index")
                return redirect(next_url)
            error = "Invalid username or password"

    return render_template(
        "login.html",
        error=error,
        success=success,
        mode=mode,
    )


@app.route("/signup", methods=["GET"])
def signup():
    return redirect(url_for("login", mode="signup"))


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/me")
def me():
    user = current_user()
    return jsonify(
        {
            "user": user,
            "canManageSarvamStt": can_manage_sarvam_stt(user),
            "canManageLabelLlm": can_manage_label_llm(user),
        }
    )


@app.route("/api/storage")
def storage_status():
    return jsonify(gcs_storage.status())


@app.route("/")
def index():
    # Do not hydrate datasets here — cold starts were timing out on GCS
    # aggregates and making the post-login redirect look "broken". The
    # client loads counts/calls via /api/stats and /api/calls.
    totals = {
        name: len(call_order.get(name) or []) for name in DATASETS
    }
    return render_template(
        "index.html",
        datasets=DATASET_META,
        totals=totals,
        current_user=current_user(),
        can_manage_sarvam=can_manage_sarvam_stt(),
        can_manage_label=can_manage_label_llm(),
    )


@app.route("/api/datasets")
def list_datasets():
    return jsonify(
        {
            "datasets": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "total": len(call_order[item["id"]]),
                }
                for item in DATASET_META
            ]
        }
    )


@app.route("/api/stats")
def stats():
    dataset = resolve_dataset()
    reload_sarvam_transcripts(dataset)

    order = call_order[dataset]
    dataset_corrections = corrections[dataset]
    edited = sum(1 for cid in order if review_status(dataset_corrections.get(cid)) == "edited")
    verified_once = sum(
        1 for cid in order if review_status(dataset_corrections.get(cid)) == "verified_once"
    )
    verified = sum(
        1 for cid in order if review_status(dataset_corrections.get(cid)) == "verified"
    )
    unfit = sum(1 for cid in order if review_status(dataset_corrections.get(cid)) == "unfit")
    pending = len(order) - edited - verified_once - verified - unfit
    sarvam_store = sarvam_by_dataset.get(dataset, {})
    stt_generated = sum(1 for call_id in order if call_id in sarvam_store)
    label_store = labels_by_dataset.get(dataset, {})
    labeled = sum(1 for call_id in order if label_store.get(call_id, {}).get("domain"))
    label_human = sum(
        1 for call_id in order if label_status(label_store.get(call_id)) in {"human", "custom"}
    )

    progress = read_dataset_progress(dataset)
    if not progress.get("total"):
        progress = {
            **progress,
            "total": len(order),
            "savedTotal": stt_generated,
            "percent": round((stt_generated / len(order)) * 100, 1) if order else 0,
        }

    label_progress = read_dataset_label_progress(dataset)
    if not label_progress.get("total"):
        label_progress = {
            **label_progress,
            "total": len(order),
            "savedTotal": labeled,
            "percent": round((labeled / len(order)) * 100, 1) if order else 0,
        }

    return jsonify(
        {
            "dataset": dataset,
            "total": len(order),
            "edited": edited,
            "verifiedOnce": verified_once,
            "verified": verified,
            "unfit": unfit,
            "pending": pending,
            "sttGenerated": stt_generated,
            "sttProgress": progress,
            "labeled": labeled,
            "labelHuman": label_human,
            "labelProgress": label_progress,
            "hasStt": True,
        }
    )


@app.route("/api/calls")
def list_calls():
    dataset = resolve_dataset()
    reload_sarvam_transcripts(dataset)

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(10, request.args.get("per_page", 50, type=int)))
    search = (request.args.get("search") or "").strip().lower()
    status = request.args.get("status", "all")
    domain_filter = (request.args.get("domain") or "").strip().lower()
    subdomain_filter = (request.args.get("subdomain") or "").strip().lower()
    label_status_filter = (request.args.get("label_status") or "all").strip().lower()

    dataset_corrections = corrections[dataset]
    sarvam_store = sarvam_by_dataset.get(dataset, {})
    label_store = labels_by_dataset.get(dataset, {})
    filtered = filter_call_ids(
        dataset,
        search=search,
        status=status,
        domain_filter=domain_filter,
        subdomain_filter=subdomain_filter,
        label_status_filter=label_status_filter,
    )

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = filtered[start:end]

    items = []
    for call_id in page_ids:
        call = calls_by_id[dataset][call_id]
        messages = call.get("messages", [])
        saved = dataset_corrections.get(call_id)
        preview_src = (saved or {}).get("messages") or messages
        label_entry = label_store.get(call_id)
        items.append(
            {
                "id": call_id,
                "number": recording_number(dataset, call_id),
                "preview": preview_text(preview_src),
                "messageCount": len(visible_messages(messages)),
                "edited": call_id in dataset_corrections,
                "status": review_status(saved),
                "editedBy": (saved or {}).get("editedBy") or "",
                "verifiedOnceBy": (saved or {}).get("verifiedOnceBy") or "",
                "verifiedBy": (saved or {}).get("verifiedBy") or "",
                "hasAudio": bool(call.get("public_url")),
                "hasStt": call_id in sarvam_store,
                "domain": (label_entry or {}).get("domain") or "",
                "subdomain": (label_entry or {}).get("subdomain") or "",
                "labelStatus": label_status(label_entry),
                "isCustom": bool((label_entry or {}).get("isCustom")),
            }
        )

    return jsonify(
        {
            "dataset": dataset,
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "hasStt": True,
        }
    )


@app.route("/api/calls/<call_id>")
def get_call(call_id: str):
    dataset = resolve_dataset()
    reload_sarvam_transcripts(dataset)
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404
    return jsonify(build_call_payload(dataset, call_id))


@app.route("/api/calls/<call_id>/correct", methods=["POST"])
def save_correct(call_id: str):
    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    reviewer = current_user()
    if not reviewer:
        return jsonify({"error": "Login required"}), 401

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages array required"}), 400

    cleaned = clean_saved_messages(messages)
    raw_timings = payload.get("timings")
    timings = (
        clean_saved_timings(raw_timings, len(cleaned))
        if isinstance(raw_timings, list)
        else None
    )
    try:
        entry = {
            "number": recording_number(dataset, call_id),
            "callLogId": call_id,
            "messages": cleaned,
            "turnLayoutEdited": bool(payload.get("turnLayoutEdited")),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "editedBy": reviewer,
            # Re-saving clears verification so two reviewers must re-verify
            "verifiedOnceBy": "",
            "verifiedOnceAt": None,
            "verifiedBy": "",
            "verifiedAt": None,
            # Saving a final also clears unfit
            "unfit": False,
            "unfitBy": "",
            "unfitAt": None,
        }
        if timings is not None:
            entry["timings"] = timings
        corrections[dataset][call_id] = entry
        save_corrections(dataset, call_id=call_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    saved = corrections[dataset][call_id]
    return jsonify(
        {
            "ok": True,
            "updatedAt": saved["updatedAt"],
            "editedBy": reviewer,
            "status": "edited",
            "messages": saved["messages"],
            "timings": saved.get("timings") or [],
            "turnLayoutEdited": bool(saved.get("turnLayoutEdited")),
        }
    )


@app.route("/api/calls/<call_id>/verify", methods=["POST", "DELETE"])
def verify_correct(call_id: str):
    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    saved = ensure_correction_loaded(dataset, call_id) or corrections[dataset].get(
        call_id
    )
    if not saved:
        return jsonify({"error": "Save the final transcript before verifying"}), 400
    if saved.get("unfit"):
        return jsonify({"error": "Clear unfit status before verifying"}), 400

    user = current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    normalize_legacy_verification(saved)
    status = review_status(saved)
    editor = (saved.get("editedBy") or "").strip()
    once_by = (saved.get("verifiedOnceBy") or "").strip()
    final_by = (saved.get("verifiedBy") or "").strip()

    if _same_reviewer(editor, user):
        return jsonify(
            {
                "error": "The person who saved cannot verify or unverify — ask another reviewer",
            }
        ), 403

    # Unverify — step back one verification level
    if request.method == "DELETE":
        if status == "verified":
            saved["verifiedBy"] = ""
            saved["verifiedAt"] = None
        elif status == "verified_once":
            saved["verifiedOnceBy"] = ""
            saved["verifiedOnceAt"] = None
        else:
            return jsonify({"error": "Nothing to unverify"}), 400
        corrections[dataset][call_id] = saved
        try:
            save_corrections(dataset, call_id=call_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        return jsonify(
            {
                "ok": True,
                "verifiedOnceBy": saved.get("verifiedOnceBy") or "",
                "verifiedOnceAt": saved.get("verifiedOnceAt"),
                "verifiedBy": saved.get("verifiedBy") or "",
                "verifiedAt": saved.get("verifiedAt"),
                "status": review_status(saved),
            }
        )

    if status == "edited":
        saved["verifiedOnceBy"] = user
        saved["verifiedOnceAt"] = datetime.now(timezone.utc).isoformat()
    elif status == "verified_once":
        if _same_reviewer(once_by, user):
            return jsonify(
                {
                    "error": "A different reviewer must complete the second verification",
                }
            ), 403
        saved["verifiedBy"] = user
        saved["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    else:
        return jsonify({"error": "Already fully verified"}), 400

    corrections[dataset][call_id] = saved
    try:
        save_corrections(dataset, call_id=call_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "ok": True,
            "verifiedOnceBy": saved.get("verifiedOnceBy") or "",
            "verifiedOnceAt": saved.get("verifiedOnceAt"),
            "verifiedBy": saved.get("verifiedBy") or "",
            "verifiedAt": saved.get("verifiedAt"),
            "status": review_status(saved),
        }
    )


@app.route("/api/calls/<call_id>/unfit", methods=["POST", "DELETE"])
def mark_unfit(call_id: str):
    """Mark or clear a call as unfit for the golden set."""
    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    user = current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    existing = (
        ensure_correction_loaded(dataset, call_id) or corrections[dataset].get(call_id) or {}
    )

    try:
        if request.method == "DELETE":
            if not existing:
                return jsonify({"ok": True, "status": "pending"})
            existing.pop("unfit", None)
            existing.pop("unfitBy", None)
            existing.pop("unfitAt", None)
            existing.pop("unfitReason", None)
            # Drop empty unfit-only records
            if not existing.get("messages") and not existing.get("editedBy"):
                corrections[dataset].pop(call_id, None)
                save_corrections(dataset, call_id=call_id)
                return jsonify({"ok": True, "status": "pending"})
            corrections[dataset][call_id] = existing
            save_corrections(dataset, call_id=call_id)
            return jsonify({"ok": True, "status": review_status(existing)})

        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "").strip()

        entry = {
            **existing,
            "number": recording_number(dataset, call_id),
            "callLogId": call_id,
            "messages": existing.get("messages") or [],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "unfit": True,
            "unfitBy": user,
            "unfitAt": datetime.now(timezone.utc).isoformat(),
            "unfitReason": reason,
            "verifiedOnceBy": "",
            "verifiedOnceAt": None,
            "verifiedBy": "",
            "verifiedAt": None,
        }
        corrections[dataset][call_id] = entry
        save_corrections(dataset, call_id=call_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "ok": True,
            "status": "unfit",
            "unfitBy": user,
            "unfitAt": entry["unfitAt"],
            "unfitReason": reason,
        }
    )


@app.route("/api/calls/<call_id>/correct", methods=["DELETE"])
def reset_correct(call_id: str):
    dataset = resolve_dataset()
    if call_id in corrections[dataset]:
        del corrections[dataset][call_id]
        try:
            save_corrections(dataset, call_id=call_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404
    payload = build_call_payload(dataset, call_id)
    return jsonify({"ok": True, "final_messages": payload["final_messages"]})


@app.route("/api/upload", methods=["POST"])
def upload_dataset_json():
    dataset = resolve_dataset()
    payload = None

    if request.files.get("file"):
        raw = request.files["file"].read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "Uploaded file is not valid JSON"}), 400
    else:
        payload = request.get_json(silent=True)

    if payload is None:
        return jsonify({"error": "Provide a JSON file or JSON body"}), 400

    try:
        count = apply_uploaded_calls(dataset, payload, replace=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    persist_uploaded_calls(dataset, payload)
    # Clear prior finals / STT so the tab only has the new originals.
    ensure_dataset_dirs(dataset)
    corrections_path = corrections_path_for(dataset)
    with corrections_path.open("w", encoding="utf-8") as handle:
        json.dump({}, handle)
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "corrected_transcripts.json")
    sarvam_path = dataset_sarvam_path(UPLOADS_DIR, dataset)
    with sarvam_path.open("w", encoding="utf-8") as handle:
        json.dump({}, handle)
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "sarvam_transcripts.json")
    progress_path = UPLOADS_DIR / dataset / "stt_progress.json"
    with progress_path.open("w", encoding="utf-8") as handle:
        json.dump({}, handle)
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "stt_progress.json")
    labels_path = dataset_labels_path(UPLOADS_DIR, dataset)
    with labels_path.open("w", encoding="utf-8") as handle:
        json.dump({}, handle)
    labels_by_dataset[dataset] = {}
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "call_labels.json")
    label_progress_path = UPLOADS_DIR / dataset / "label_progress.json"
    with label_progress_path.open("w", encoding="utf-8") as handle:
        json.dump({}, handle)
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "label_progress.json")

    return jsonify(
        {
            "ok": True,
            "dataset": dataset,
            "imported": count,
            "total": len(call_order[dataset]),
        }
    )


@app.route("/api/export/preview", methods=["POST"])
def export_preview():
    dataset = resolve_dataset()
    payload = request.get_json(silent=True) or {}
    call_ids = payload.get("call_ids")
    if call_ids is not None and not isinstance(call_ids, list):
        return jsonify({"error": "call_ids must be an array"}), 400
    ids = resolve_export_call_ids(
        dataset,
        call_ids=call_ids,
        search=str(payload.get("search") or ""),
        status=str(payload.get("status") or "all"),
        domain_filter=str(payload.get("domain") or ""),
        subdomain_filter=str(payload.get("subdomain") or ""),
        label_status_filter=str(payload.get("label_status") or "all"),
    )
    count = 0
    for call_id in ids:
        saved = ensure_correction_loaded(dataset, call_id) or corrections[dataset].get(
            call_id
        )
        if saved and saved.get("messages"):
            count += 1
    return jsonify({"count": count})


@app.route("/api/export", methods=["POST"])
def export_transcripts():
    dataset = resolve_dataset()
    refresh_labels_store(dataset)
    payload = request.get_json(silent=True) or {}
    call_ids = payload.get("call_ids")
    if call_ids is not None and not isinstance(call_ids, list):
        return jsonify({"error": "call_ids must be an array"}), 400
    ids = resolve_export_call_ids(
        dataset,
        call_ids=call_ids,
        search=str(payload.get("search") or ""),
        status=str(payload.get("status") or "all"),
        domain_filter=str(payload.get("domain") or ""),
        subdomain_filter=str(payload.get("subdomain") or ""),
        label_status_filter=str(payload.get("label_status") or "all"),
    )
    export_data: dict[str, dict] = {}
    for call_id in ids:
        saved = ensure_correction_loaded(dataset, call_id) or corrections[dataset].get(
            call_id
        )
        if not saved or not saved.get("messages"):
            continue
        export_data[call_id] = build_export_entry(dataset, call_id, saved)

    if not export_data:
        return jsonify({"error": "No transcripts match this export"}), 404

    body = dumps_numbered(export_data, list(export_data.keys()))
    status_slug = str(payload.get("status") or "export").replace(" ", "_")
    filename = f"{dataset}_{status_slug}_transcripts.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/export/verified")
def export_verified():
    dataset = resolve_dataset()
    refresh_labels_store(dataset)
    ids = resolve_export_call_ids(dataset, status="verified")
    export_data: dict[str, dict] = {}
    for call_id in ids:
        saved = corrections[dataset].get(call_id)
        if review_status(saved) != "verified" or saved is None:
            continue
        export_data[call_id] = build_export_entry(dataset, call_id, saved)

    if not export_data:
        return jsonify({"error": "No verified transcripts to export"}), 404

    body = dumps_numbered(export_data, ids)
    filename = f"{dataset}_verified_transcripts.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/stt/start", methods=["POST"])
def start_sarvam_stt():
    if not can_manage_sarvam_stt():
        return jsonify({"error": "Only admins can start Sarvam STT"}), 403

    dataset = resolve_dataset()
    if not call_order[dataset]:
        return jsonify({"error": "No calls in this dataset. Upload JSON first."}), 400

    if stt_is_running(dataset):
        return jsonify({"error": "Sarvam STT is already running for this dataset", "running": True}), 409

    payload = request.get_json(silent=True) or {}
    workers = int(payload.get("workers") or 3)
    resume = payload.get("resume", True)

    calls = []
    for call_id in call_order[dataset]:
        call = calls_by_id[dataset][call_id]
        calls.append(
            {
                "id": call_id,
                "public_url": call.get("public_url") or call.get("recordingUrl") or "",
                "human": call.get("humanRecordingUrl") or "",
                "agent": call.get("agentRecordingUrl") or "",
                "messages": call.get("messages") or [],
            }
        )

    def on_saved(call_id: str, entry: dict) -> None:
        sarvam_by_dataset[dataset][call_id] = entry

    result = start_stt_job(
        dataset=dataset,
        uploads_dir=UPLOADS_DIR,
        calls=calls,
        call_order=call_order[dataset],
        on_saved=on_saved,
        workers=workers,
        resume=bool(resume),
    )
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/stt/stop", methods=["POST"])
def stop_sarvam_stt():
    if not can_manage_sarvam_stt():
        return jsonify({"error": "Only admins can stop Sarvam STT"}), 403

    dataset = resolve_dataset()
    result = stop_stt_job(dataset=dataset, uploads_dir=UPLOADS_DIR)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/stt/status")
def stt_status():
    dataset = resolve_dataset()
    progress = read_dataset_progress(dataset)
    return jsonify(
        {
            "dataset": dataset,
            "running": stt_is_running(dataset) or bool(progress.get("running")),
            "progress": progress,
        }
    )


@app.route("/api/label/suggestions")
def label_suggestions():
    dataset = resolve_dataset()
    return jsonify(label_suggestions_for_dataset(dataset))


@app.route("/api/taxonomy")
def get_taxonomy():
    """Legacy alias — returns labels seen in this dataset, not a fixed taxonomy."""
    dataset = resolve_dataset()
    return jsonify(label_suggestions_for_dataset(dataset))


@app.route("/api/label/start", methods=["POST"])
def start_call_labeling():
    if not can_manage_label_llm():
        return jsonify({"error": "Only ayushi can start auto-labeling"}), 403

    dataset = resolve_dataset()
    if not call_order[dataset]:
        return jsonify({"error": "No calls in this dataset. Upload JSON first."}), 400

    if not label_api_key():
        return jsonify(
            {
                "error": "GEMINI_API_KEY (or LABEL_API_KEY) is required for auto-labeling. Add it to .env.",
            }
        ), 400

    if label_is_running(dataset):
        return jsonify(
            {"error": "Auto-labeling is already running for this dataset", "running": True}
        ), 409

    payload = request.get_json(silent=True) or {}
    workers = int(payload.get("workers") or 3)
    resume = payload.get("resume", True)
    force = bool(payload.get("force", False))

    calls = []
    for call_id in call_order[dataset]:
        call = calls_by_id[dataset][call_id]
        calls.append({"id": call_id, "messages": call.get("messages") or []})

    def on_saved(call_id: str, entry: dict) -> None:
        labels_by_dataset[dataset][call_id] = entry

    result = start_label_job(
        dataset=dataset,
        uploads_dir=UPLOADS_DIR,
        calls=calls,
        call_order=call_order[dataset],
        on_saved=on_saved,
        workers=workers,
        resume=bool(resume),
        force=force,
    )
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/label/stop", methods=["POST"])
def stop_call_labeling():
    if not can_manage_label_llm():
        return jsonify({"error": "Only ayushi can stop auto-labeling"}), 403

    dataset = resolve_dataset()
    result = stop_label_job(dataset=dataset, uploads_dir=UPLOADS_DIR)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/label/status")
def label_status_api():
    dataset = resolve_dataset()
    progress = read_dataset_label_progress(dataset)
    return jsonify(
        {
            "dataset": dataset,
            "running": label_is_running(dataset) or bool(progress.get("running")),
            "progress": progress,
        }
    )


@app.route("/api/calls/<call_id>/label", methods=["GET", "PUT", "DELETE"])
def call_label(call_id: str):
    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    if request.method == "GET":
        entry = labels_by_dataset[dataset].get(call_id)
        return jsonify(
            {
                "callLogId": call_id,
                "label": label_public_view(entry),
                "suggestions": label_suggestions_for_dataset(dataset),
            }
        )

    user = current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    if request.method == "DELETE":
        if call_id in labels_by_dataset[dataset]:
            del labels_by_dataset[dataset][call_id]
            save_label_entry(dataset, call_id)
        return jsonify({"ok": True, "label": None})

    payload = request.get_json(silent=True) or {}
    domain_raw = str(payload.get("domain") or "").strip()
    subdomain_raw = str(payload.get("subdomain") or "").strip()
    is_custom = bool(payload.get("isCustom", False))
    try:
        domain, subdomain, is_custom = validate_label(
            domain_raw,
            subdomain_raw,
            is_custom=is_custom,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    existing = labels_by_dataset[dataset].get(call_id) or {}
    entry = {
        **existing,
        "callLogId": call_id,
        "number": recording_number(dataset, call_id),
        "domain": domain,
        "subdomain": subdomain,
        "isCustom": is_custom,
        "labeledBy": "human",
        "labeledByUser": user,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "original",
    }
    labels_by_dataset[dataset][call_id] = entry
    save_label_entry(dataset, call_id)
    return jsonify({"ok": True, "label": label_public_view(entry)})


@app.route("/api/calls/<call_id>/label/auto", methods=["POST"])
def auto_label_call(call_id: str):
    if not can_manage_label_llm():
        return jsonify({"error": "Only ayushi can re-run auto-labeling"}), 403

    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    if not label_api_key():
        return jsonify(
            {
                "error": "GEMINI_API_KEY (or LABEL_API_KEY) is required for auto-labeling. Add it to .env.",
            }
        ), 400

    call = calls_by_id[dataset][call_id]
    existing = labels_by_dataset[dataset].get(call_id)
    try:
        entry = label_single_call(
            {"id": call_id, "messages": call.get("messages") or []},
            existing=existing,
        )
        entry["number"] = recording_number(dataset, call_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    labels_by_dataset[dataset][call_id] = entry
    save_label_entry(dataset, call_id)
    return jsonify({"ok": True, "label": label_public_view(entry)})


@app.route("/api/phrases")
def recommend_phrases():
    dataset = resolve_dataset()
    query = normalize_phrase_text(request.args.get("q") or "")
    limit = min(20, max(1, request.args.get("limit", 8, type=int)))

    ranked = build_phrase_index(dataset)
    if not query:
        suggestions = ranked[:limit]
        return jsonify({"dataset": dataset, "suggestions": suggestions})

    matches_by_phrase: dict[str, dict] = {}
    queries = phrase_query_variants(query)
    for item in ranked:
        phrase = item["phrase"]
        score = int(item.get("score") or item.get("count") or 0)
        words = int(item.get("words") or 1)

        best: tuple[int, str] | None = None
        for q in queries:
            query_tokens = q.split()
            if phrase.startswith(q):
                match_score = score + 1000 + words * 30
                match_type = "starts"
            elif f" {q}" in phrase:
                match_score = score + 650 + words * 22
                match_type = "word"
            elif q in phrase:
                match_score = score + 250 + words * 12
                match_type = "contains"
            elif query_tokens and all(token in phrase for token in query_tokens):
                match_score = score + 150 + words * 8
                match_type = "tokens"
            else:
                continue

            if words == 1:
                match_score -= 450
            if best is None or match_score > best[0]:
                best = (match_score, match_type)

        if best is None:
            continue

        existing = matches_by_phrase.get(phrase)
        candidate = {**item, "matchScore": best[0], "match": best[1]}
        if not existing or candidate["matchScore"] > existing["matchScore"]:
            matches_by_phrase[phrase] = candidate

    matches = list(matches_by_phrase.values())

    matches.sort(
        key=lambda item: (
            item.get("matchScore", 0),
            item.get("words", 0),
            item.get("score", 0),
        ),
        reverse=True,
    )
    matches = matches[:limit]

    return jsonify({"dataset": dataset, "suggestions": matches})


@app.route("/api/transliterate", methods=["POST"])
def transliterate():
    """Latin Hindi → Devanagari via Google Input Tools."""
    global _transliterate_http
    import requests as http_requests

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True, "text": "", "candidates": []})

    cache_key = text.lower()
    if cache_key in _transliterate_cache:
        candidates = _transliterate_cache[cache_key]
        return jsonify(
            {
                "ok": True,
                "text": candidates[0] if candidates else text,
                "candidates": candidates,
            }
        )

    if _transliterate_http is None:
        _transliterate_http = http_requests.Session()

    try:
        response = _transliterate_http.get(
            "https://inputtools.google.com/request",
            params={
                "text": text,
                "itc": "hi-t-i0-und",
                "num": "5",
                "cp": "0",
                "cs": "1",
                "ie": "utf-8",
                "oe": "utf-8",
                "app": "demopage",
            },
            timeout=2,
        )
        response.raise_for_status()
        data = response.json()
        candidates: list[str] = []
        if isinstance(data, list) and len(data) >= 2 and data[0] == "SUCCESS":
            block = data[1][0] if data[1] else None
            if block and len(block) >= 2 and isinstance(block[1], list):
                candidates = [str(c) for c in block[1] if c]
        _transliterate_cache[cache_key] = candidates
        if len(_transliterate_cache) > 1000:
            _transliterate_cache.pop(next(iter(_transliterate_cache)))
        best = candidates[0] if candidates else text
        return jsonify({"ok": True, "text": best, "candidates": candidates})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc), "text": text, "candidates": []}), 502


if __name__ == "__main__":
    print("Loading data…")
    load_data()
    for name in DATASETS:
        print(f"{name}: {len(call_order[name])} calls")
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
