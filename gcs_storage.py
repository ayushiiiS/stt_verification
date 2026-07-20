"""Google Cloud Storage persistence for golden_set.

Bucket layout (default: goldenset1)::

    gs://goldenset1/
      users.json
      IndiaMART/<call_id>/
        meta.json
        recording.<ext>          # original full recording
        human.<ext>
        agent.<ext>
        transcript_original.json
        transcript_sarvam.json
        transcript_final.json
      AMC/...
      ABHFL/...
      Amber/...
      <Agent>/stt_progress.json

Local ``uploads/<dataset>/*.json`` aggregates are still used by the app;
they are rebuilt from the per-call layout on hydrate, and pushed
per-call on every write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

DEFAULT_BUCKET = "goldenset1"

# dataset id -> GCS agent folder name
AGENT_FOLDERS: dict[str, str] = {
    "indiamart": "IndiaMART",
    "abhfl": "ABHFL",
    "amber": "Amber",
}

FOLDER_TO_DATASET: dict[str, str] = {v.lower(): k for k, v in AGENT_FOLDERS.items()}
FOLDER_TO_DATASET.update({k: k for k in AGENT_FOLDERS})

ORIGINAL_NAME = "transcript_original.json"
SARVAM_NAME = "transcript_sarvam.json"
FINAL_NAME = "transcript_final.json"
META_NAME = "meta.json"

_client = None
_bucket = None
_init_error: str | None = None
_audio_executor: ThreadPoolExecutor | None = None
_audio_lock = threading.Lock()


def bucket_name() -> str:
    return (os.environ.get("GCS_BUCKET") or DEFAULT_BUCKET).strip()


def agent_folder(dataset: str) -> str:
    key = (dataset or "").strip().lower()
    return AGENT_FOLDERS.get(key, key.capitalize() if key else "Unknown")


def dataset_from_folder(folder: str) -> str:
    return FOLDER_TO_DATASET.get((folder or "").strip().lower(), (folder or "").strip().lower())


def is_enabled() -> bool:
    _ensure_client()
    return _bucket is not None


def status() -> dict:
    _ensure_client()
    return {
        "enabled": _bucket is not None,
        "bucket": bucket_name(),
        "layout": "agent/call_id/{recording,human,agent,transcripts}",
        "agents": list(AGENT_FOLDERS.values()),
        "error": _init_error,
    }


def _credentials_from_env():
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None, None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        import base64

        try:
            info = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {exc}") from exc

    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(info)
    project = info.get("project_id")
    return credentials, project


def _ensure_client():
    global _client, _bucket, _init_error
    if _client is not None or _init_error is not None:
        return
    if (os.environ.get("GCS_DISABLED") or "").strip().lower() in {"1", "true", "yes"}:
        _init_error = "GCS_DISABLED=true"
        return

    try:
        from google.cloud import storage
        from google.cloud.exceptions import NotFound

        credentials, project = _credentials_from_env()
        if credentials is not None:
            _client = storage.Client(credentials=credentials, project=project)
        else:
            _client = storage.Client()

        name = bucket_name()
        try:
            _bucket = _client.get_bucket(name)
        except NotFound:
            location = os.environ.get("GCS_LOCATION", "asia-south1")
            _bucket = _client.create_bucket(name, location=location)
            print(f"Created GCS bucket gs://{name} in {location}", flush=True)
        except Exception as get_exc:  # noqa: BLE001
            # Some SAs can write objects but lack storage.buckets.get.
            from google.api_core import exceptions as api_exceptions

            if isinstance(get_exc, (api_exceptions.Forbidden, PermissionError)):
                _bucket = _client.bucket(name)
                print(
                    f"GCS persistence enabled (no bucket.get): gs://{name}",
                    flush=True,
                )
            else:
                raise
        else:
            print(f"GCS persistence enabled: gs://{name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _init_error = str(exc)
        _client = None
        _bucket = None
        print(f"GCS persistence unavailable: {exc}", flush=True)


def call_prefix(dataset: str, call_id: str) -> str:
    return f"{agent_folder(dataset)}/{str(call_id).strip().strip('/')}"


def call_object_key(dataset: str, call_id: str, filename: str) -> str:
    return f"{call_prefix(dataset, call_id)}/{filename.lstrip('/')}"


def users_key() -> str:
    return "users.json"


def stt_progress_key(dataset: str) -> str:
    return f"{agent_folder(dataset)}/stt_progress.json"


# --- low-level IO ---


def upload_text(
    key: str,
    text: str,
    *,
    content_type: str = "application/json",
    overwrite: bool = True,
) -> bool:
    _ensure_client()
    if _bucket is None:
        return False
    blob = _bucket.blob(key)
    if not overwrite and blob.exists():
        return True
    try:
        blob.upload_from_string(text, content_type=content_type)
        return True
    except Exception as exc:  # noqa: BLE001
        # Overwrite often needs storage.objects.delete; treat existing as success.
        from google.api_core import exceptions as api_exceptions

        if isinstance(exc, api_exceptions.Forbidden) and blob.exists():
            return True
        raise


def upload_file(
    key: str,
    path: Path,
    *,
    content_type: str | None = None,
    overwrite: bool = True,
) -> bool:
    _ensure_client()
    if _bucket is None or not path.exists():
        return False
    blob = _bucket.blob(key)
    if not overwrite and blob.exists():
        return True
    kwargs = {}
    if content_type:
        kwargs["content_type"] = content_type
    try:
        blob.upload_from_filename(str(path), **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        from google.api_core import exceptions as api_exceptions

        if isinstance(exc, api_exceptions.Forbidden) and blob.exists():
            return True
        raise


def upload_bytes(
    key: str,
    data: bytes,
    *,
    content_type: str,
    overwrite: bool = True,
) -> bool:
    _ensure_client()
    if _bucket is None:
        return False
    blob = _bucket.blob(key)
    if not overwrite and blob.exists():
        return True
    try:
        blob.upload_from_string(data, content_type=content_type)
        return True
    except Exception as exc:  # noqa: BLE001
        from google.api_core import exceptions as api_exceptions

        if isinstance(exc, api_exceptions.Forbidden) and blob.exists():
            return True
        raise


def push_json(key: str, payload: Any, *, overwrite: bool = True) -> bool:
    return upload_text(
        key,
        json.dumps(payload, ensure_ascii=False, indent=2),
        overwrite=overwrite,
    )


def push_call_meta(dataset: str, call_id: str, meta: dict, *, overwrite: bool = True) -> bool:
    return push_json(call_object_key(dataset, call_id, META_NAME), meta, overwrite=overwrite)


def push_transcript_original(
    dataset: str, call_id: str, payload: dict, *, overwrite: bool = True
) -> bool:
    return push_json(
        call_object_key(dataset, call_id, ORIGINAL_NAME), payload, overwrite=overwrite
    )


def push_transcript_sarvam(
    dataset: str, call_id: str, payload: dict, *, overwrite: bool = True
) -> bool:
    return push_json(
        call_object_key(dataset, call_id, SARVAM_NAME), payload, overwrite=overwrite
    )


def push_transcript_final(
    dataset: str, call_id: str, payload: dict, *, overwrite: bool = True
) -> bool:
    return push_json(
        call_object_key(dataset, call_id, FINAL_NAME), payload, overwrite=overwrite
    )


def download_text(key: str) -> str | None:
    _ensure_client()
    if _bucket is None:
        return None
    blob = _bucket.blob(key)
    if not blob.exists():
        return None
    return blob.download_as_text(encoding="utf-8")


def download_json(key: str) -> Any | None:
    text = download_text(key)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def download_to_file(key: str, path: Path) -> bool:
    text = download_text(key)
    if text is None:
        return False
    atomic_write_text(path, text)
    return True


def mirror_local(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    return upload_file(key, path)


def hydrate_local(path: Path, key: str, *, prefer_remote: bool = True) -> bool:
    if not prefer_remote and path.exists():
        return False
    return download_to_file(key, path)


def list_prefix(prefix: str) -> list[str]:
    _ensure_client()
    if _bucket is None:
        return []
    return [blob.name for blob in _client.list_blobs(_bucket, prefix=prefix)]


def delete_prefix(prefix: str) -> int:
    """Delete all objects under a prefix. Returns count deleted."""
    _ensure_client()
    if _bucket is None:
        return 0
    names = list_prefix(prefix)
    deleted = 0
    for name in names:
        try:
            _bucket.blob(name).delete()
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"GCS delete failed {name}: {exc}", flush=True)
    return deleted


def delete_call(dataset: str, call_id: str) -> int:
    return delete_prefix(call_prefix(dataset, call_id) + "/")


def sync_dataset_keep_only(
    dataset: str,
    keep_call_ids: set[str],
    *,
    delete_extras: bool = True,
) -> dict:
    """Delete call folders not in keep_call_ids under the agent prefix."""
    existing = set(list_call_ids(dataset))
    extra = sorted(existing - keep_call_ids)
    removed = 0
    if delete_extras:
        for call_id in extra:
            removed += delete_call(dataset, call_id)
    return {
        "dataset": dataset,
        "existing": len(existing),
        "keep": len(keep_call_ids),
        "extra_calls": len(extra),
        "objects_deleted": removed,
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _suffix_from_url_or_type(url: str, content_type: str = "") -> str:
    content_type = (content_type or "").lower()
    if "mpeg" in content_type or "mp3" in content_type:
        return ".mp3"
    if "wav" in content_type:
        return ".wav"
    if "ogg" in content_type or "opus" in content_type:
        return ".ogg"
    if "webm" in content_type:
        return ".webm"
    if "mp4" in content_type or "m4a" in content_type:
        return ".m4a"
    path = urlparse(url).path.lower()
    for ext in (".ogg", ".mp3", ".wav", ".m4a", ".webm", ".mp4"):
        if path.endswith(ext):
            return ext
    return ".ogg"


def _content_type_for_suffix(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
    }.get(suffix.lower(), "application/octet-stream")


def push_audio_from_url(dataset: str, call_id: str, kind: str, url: str) -> str | None:
    """Download audio from URL and store as <kind>.<ext>. Returns object key or None."""
    if not url or not is_enabled():
        return None
    kind = kind.strip().lower()
    if kind not in {"recording", "human", "agent"}:
        raise ValueError(f"Unsupported audio kind: {kind}")

    # Skip if a common extension already exists (avoids needing objects.delete)
    for ext in (".ogg", ".mp3", ".wav", ".m4a", ".webm"):
        existing_key = call_object_key(dataset, call_id, f"{kind}{ext}")
        blob = _bucket.blob(existing_key) if _bucket is not None else None
        if blob is not None and blob.exists():
            return existing_key

    try:
        import httpx
    except ImportError:
        import requests

        response = requests.get(url, timeout=120)
        response.raise_for_status()
        data = response.content
        content_type = response.headers.get("content-type", "")
    else:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                max_bytes = 200 * 1024 * 1024
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError("Audio download exceeded 200MB")
                    chunks.append(chunk)
                data = b"".join(chunks)

    suffix = _suffix_from_url_or_type(url, content_type)
    key = call_object_key(dataset, call_id, f"{kind}{suffix}")
    if not upload_bytes(key, data, content_type=_content_type_for_suffix(suffix), overwrite=False):
        return None
    return key


def _audio_pool() -> ThreadPoolExecutor:
    global _audio_executor
    with _audio_lock:
        if _audio_executor is None:
            workers = int(os.environ.get("GCS_AUDIO_WORKERS", "4"))
            _audio_executor = ThreadPoolExecutor(
                max_workers=max(1, workers), thread_name_prefix="gcs-audio"
            )
        return _audio_executor


def push_call_audio_async(
    dataset: str,
    call_id: str,
    *,
    recording_url: str = "",
    human_url: str = "",
    agent_url: str = "",
) -> None:
    """Fire-and-forget audio mirrors for a call."""
    if not is_enabled():
        return
    jobs = [
        ("recording", recording_url),
        ("human", human_url),
        ("agent", agent_url),
    ]

    def _one(kind: str, url: str) -> None:
        if not url:
            return
        try:
            key = push_audio_from_url(dataset, call_id, kind, url)
            if key:
                print(f"GCS audio uploaded gs://{bucket_name()}/{key}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"GCS audio upload failed {dataset}/{call_id}/{kind}: {exc}", flush=True)

    pool = _audio_pool()
    for kind, url in jobs:
        if url:
            pool.submit(_one, kind, url)


def push_call_bundle(
    dataset: str,
    call_id: str,
    *,
    messages: list | None = None,
    number: int | None = None,
    public_url: str = "",
    recording_url: str = "",
    human_url: str = "",
    agent_url: str = "",
    sarvam: dict | None = None,
    final: dict | None = None,
    upload_audio: bool = True,
) -> dict:
    """Write meta + transcripts for one call; optionally queue audio uploads."""
    result = {"call_id": call_id, "uploaded": [], "skipped": []}
    if not is_enabled():
        result["skipped"].append("gcs_disabled")
        return result

    recording_url = recording_url or public_url or ""
    meta = {
        "callLogId": call_id,
        "dataset": dataset,
        "agent": agent_folder(dataset),
        "number": number,
        "urls": {
            "recording": recording_url or None,
            "human": human_url or None,
            "agent": agent_url or None,
            "public_url": public_url or recording_url or None,
        },
    }
    if push_call_meta(dataset, call_id, meta):
        result["uploaded"].append(META_NAME)

    if messages is not None:
        original = {
            "callLogId": call_id,
            "number": number,
            "messages": messages,
        }
        if push_transcript_original(dataset, call_id, original):
            result["uploaded"].append(ORIGINAL_NAME)

    if sarvam is not None:
        if push_transcript_sarvam(dataset, call_id, sarvam):
            result["uploaded"].append(SARVAM_NAME)

    if final is not None:
        if push_transcript_final(dataset, call_id, final):
            result["uploaded"].append(FINAL_NAME)

    if upload_audio:
        push_call_audio_async(
            dataset,
            call_id,
            recording_url=recording_url,
            human_url=human_url,
            agent_url=agent_url,
        )
        result["uploaded"].append("audio:queued")

    return result


# --- dataset sync (hydrate / migrate) ---


def list_call_ids(dataset: str) -> list[str]:
    """List call ids that have any object under the agent prefix."""
    _ensure_client()
    if _bucket is None:
        return []
    prefix = f"{agent_folder(dataset)}/"
    call_ids: set[str] = set()
    for name in list_prefix(prefix):
        rest = name[len(prefix) :]
        if not rest or "/" not in rest:
            continue
        call_id = rest.split("/", 1)[0]
        if call_id and call_id not in {"stt_progress.json"} and not call_id.endswith(".json"):
            call_ids.add(call_id)
    return sorted(call_ids)


def _load_call_side(dataset: str, call_id: str) -> dict:
    """Fetch per-call objects into a dict used to rebuild local aggregates."""
    out: dict[str, Any] = {"id": call_id}
    meta = download_json(call_object_key(dataset, call_id, META_NAME)) or {}
    original = download_json(call_object_key(dataset, call_id, ORIGINAL_NAME))
    sarvam = download_json(call_object_key(dataset, call_id, SARVAM_NAME))
    final = download_json(call_object_key(dataset, call_id, FINAL_NAME))

    urls = (meta.get("urls") or {}) if isinstance(meta, dict) else {}
    out["number"] = meta.get("number") if isinstance(meta, dict) else None
    out["public_url"] = urls.get("public_url") or urls.get("recording") or ""
    out["recordingUrl"] = urls.get("recording") or ""
    out["recordings"] = urls.get("recording") or ""
    out["human"] = urls.get("human") or ""
    out["agent"] = urls.get("agent") or ""

    # Prefer GCS audio object public path references for local playback?
    # Keep source URLs in meta; app already has signed URLs until they expire.
    if original and isinstance(original, dict):
        out["messages"] = original.get("messages") or []
        out["callLogId"] = original.get("callLogId") or call_id
    else:
        out["messages"] = []
        out["callLogId"] = call_id

    if sarvam and isinstance(sarvam, dict):
        out["_sarvam"] = sarvam
    if final and isinstance(final, dict):
        out["_final"] = final
    return out


def rebuild_local_dataset(uploads_dir: Path, dataset: str) -> dict:
    """Pull per-call GCS objects and rewrite local aggregate JSON files."""
    result = {"dataset": dataset, "calls": 0, "sarvam": 0, "finals": 0}
    if not is_enabled():
        return result

    call_ids = list_call_ids(dataset)
    if not call_ids:
        return result

    calls: list[dict] = []
    sarvam_map: dict[str, dict] = {}
    finals_map: dict[str, dict] = {}

    for call_id in call_ids:
        entry = _load_call_side(dataset, call_id)
        call_row = {
            "callLogId": entry.get("callLogId") or call_id,
            "number": entry.get("number"),
            "messages": entry.get("messages") or [],
            "public_url": entry.get("public_url") or "",
            "recordingUrl": entry.get("recordingUrl") or "",
            "recordings": entry.get("recordings") or "",
            "human": entry.get("human") or "",
            "agent": entry.get("agent") or "",
        }
        calls.append(call_row)
        if entry.get("_sarvam"):
            sarvam_map[call_id] = entry["_sarvam"]
        if entry.get("_final"):
            finals_map[call_id] = entry["_final"]

    # Stable order by number then id
    def sort_key(row: dict) -> tuple:
        num = row.get("number")
        try:
            n = int(num) if num is not None else 10**9
        except (TypeError, ValueError):
            n = 10**9
        return (n, str(row.get("callLogId") or ""))

    calls.sort(key=sort_key)

    ds_dir = uploads_dir / dataset
    ds_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ds_dir / "calls.json", calls)
    atomic_write_json(ds_dir / "sarvam_transcripts.json", sarvam_map)
    atomic_write_json(ds_dir / "corrected_transcripts.json", finals_map)

    progress = download_json(stt_progress_key(dataset))
    if progress is not None:
        atomic_write_json(ds_dir / "stt_progress.json", progress)

    result["calls"] = len(calls)
    result["sarvam"] = len(sarvam_map)
    result["finals"] = len(finals_map)
    return result


def sync_dataset_dir(uploads_dir: Path, dataset: str, *, prefer_remote: bool = True) -> dict:
    """Hydrate local dataset from per-call GCS layout (legacy flat files as fallback).

    If local ``calls.json`` already exists and ``prefer_remote`` is False, skip.
    When ``prefer_remote`` is True but local calls.json exists, keep local and
    do not rebuild from GCS (avoids slow per-object downloads on every boot).
    Set env ``GCS_FORCE_HYDRATE=1`` to force a full rebuild from GCS.
    """
    result = {
        "dataset": dataset,
        "layout": "per-call",
        "downloaded": [],
        "missing": [],
        "rebuilt": {},
        "skipped": False,
    }
    local_calls = uploads_dir / dataset / "calls.json"
    force = (os.environ.get("GCS_FORCE_HYDRATE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    if local_calls.exists() and not force:
        result["skipped"] = True
        result["downloaded"].append("local-calls.json")
        return result

    if not prefer_remote and local_calls.exists():
        result["skipped"] = True
        return result

    rebuilt = rebuild_local_dataset(uploads_dir, dataset)
    result["rebuilt"] = rebuilt
    if rebuilt.get("calls"):
        result["downloaded"].append("per-call-layout")
        return result

    # Legacy fallback: flat <dataset>/*.json
    files = (
        "calls.json",
        "corrected_transcripts.json",
        "sarvam_transcripts.json",
        "stt_progress.json",
    )
    for name in files:
        local = uploads_dir / dataset / name
        legacy_key = f"{dataset.strip().lower()}/{name}"
        if hydrate_local(local, legacy_key, prefer_remote=True):
            result["downloaded"].append(f"legacy:{name}")
        elif not local.exists():
            result["missing"].append(name)
    return result


def push_dataset_file(uploads_dir: Path, dataset: str, filename: str) -> bool:
    """
    Compatibility shim.
    - stt_progress.json -> agent/stt_progress.json
    - other aggregates are expanded into per-call objects when possible
    """
    local = uploads_dir / dataset / filename
    if not local.exists():
        return False

    if filename == "stt_progress.json":
        return mirror_local(local, stt_progress_key(dataset))

    if filename == "calls.json":
        try:
            payload = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(push_calls_payload(dataset, payload, upload_audio=True))

    if filename == "sarvam_transcripts.json":
        try:
            payload = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        ok = True
        for call_id, entry in payload.items():
            if isinstance(entry, dict):
                ok = push_transcript_sarvam(dataset, str(call_id), entry) and ok
        return ok

    if filename == "corrected_transcripts.json":
        try:
            payload = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        ok = True
        for call_id, entry in payload.items():
            if isinstance(entry, dict):
                ok = push_transcript_final(dataset, str(call_id), entry) and ok
        return ok

    # Unknown file: store under agent root
    return mirror_local(local, f"{agent_folder(dataset)}/{filename}")


def push_calls_payload(
    dataset: str,
    payload: Any,
    *,
    upload_audio: bool = True,
    finals: dict | None = None,
    sarvam: dict | None = None,
) -> int:
    """Push every call in an upload payload into the per-call GCS layout."""
    if not is_enabled():
        return 0

    items: list[dict]
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        if "calls" in payload and isinstance(payload["calls"], list):
            items = [x for x in payload["calls"] if isinstance(x, dict)]
        else:
            items = []
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                entry = dict(value)
                entry.setdefault("callLogId", value.get("callLogId") or value.get("id") or key)
                items.append(entry)
    else:
        return 0

    finals = finals or {}
    sarvam = sarvam or {}
    count = 0
    for item in items:
        call_id = _oid(item.get("callLogId") or item.get("id") or item.get("_id") or "")
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
        recording_url = item.get("recordingUrl") or item.get("recordings") or public_url or ""
        human_url = item.get("human") or ""
        agent_url = item.get("agent") or ""
        number = item.get("number")
        try:
            number_i = int(number) if number is not None else None
        except (TypeError, ValueError):
            number_i = None

        push_call_bundle(
            dataset,
            call_id,
            messages=messages if isinstance(messages, list) else [],
            number=number_i,
            public_url=str(public_url or ""),
            recording_url=str(recording_url or ""),
            human_url=str(human_url or ""),
            agent_url=str(agent_url or ""),
            sarvam=sarvam.get(call_id) if isinstance(sarvam.get(call_id), dict) else None,
            final=finals.get(call_id) if isinstance(finals.get(call_id), dict) else None,
            upload_audio=upload_audio,
        )
        count += 1
    return count


def _oid(value: Any) -> str:
    if isinstance(value, dict) and "$oid" in value:
        return str(value["$oid"])
    return str(value or "").strip()


def push_users_file(path: Path) -> bool:
    return mirror_local(path, users_key())


def hydrate_users_file(path: Path, *, prefer_remote: bool = True) -> bool:
    return hydrate_local(path, users_key(), prefer_remote=prefer_remote)


def migrate_local_uploads_to_gcs(
    uploads_dir: Path,
    datasets: Iterable[str],
    *,
    upload_audio: bool = True,
) -> dict:
    """One-shot: push existing local aggregates into the new per-call layout."""
    summary: dict[str, Any] = {}
    for dataset in datasets:
        calls_path = uploads_dir / dataset / "calls.json"
        sarvam_path = uploads_dir / dataset / "sarvam_transcripts.json"
        finals_path = uploads_dir / dataset / "corrected_transcripts.json"
        progress_path = uploads_dir / dataset / "stt_progress.json"

        calls_payload: Any = []
        if calls_path.exists():
            try:
                calls_payload = json.loads(calls_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                calls_payload = []

        sarvam = {}
        if sarvam_path.exists():
            try:
                loaded = json.loads(sarvam_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    sarvam = loaded
            except json.JSONDecodeError:
                pass

        finals = {}
        if finals_path.exists():
            try:
                loaded = json.loads(finals_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    finals = loaded
            except json.JSONDecodeError:
                pass

        n = push_calls_payload(
            dataset,
            calls_payload,
            upload_audio=upload_audio,
            finals=finals,
            sarvam=sarvam,
        )
        if progress_path.exists():
            mirror_local(progress_path, stt_progress_key(dataset))

        summary[dataset] = {
            "agent": agent_folder(dataset),
            "calls_pushed": n,
            "sarvam": len(sarvam),
            "finals": len(finals),
        }
        print(f"Migrated {dataset} -> gs://{bucket_name()}/{agent_folder(dataset)}/ ({n} calls)", flush=True)
    return summary
