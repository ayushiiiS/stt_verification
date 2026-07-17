"""Google Cloud Storage persistence for golden_set.

Bucket layout (default bucket: gotldenset):
  users.json
  <dataset>/calls.json
  <dataset>/corrected_transcripts.json
  <dataset>/sarvam_transcripts.json
  <dataset>/stt_progress.json
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_BUCKET = "gotldenset"

_client = None
_bucket = None
_init_error: str | None = None


def bucket_name() -> str:
    return (os.environ.get("GCS_BUCKET") or DEFAULT_BUCKET).strip()


def is_enabled() -> bool:
    """True when GCS is configured and the client initialized successfully."""
    _ensure_client()
    return _bucket is not None


def status() -> dict:
    _ensure_client()
    return {
        "enabled": _bucket is not None,
        "bucket": bucket_name(),
        "error": _init_error,
    }


def _credentials_from_env():
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None, None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        # Allow base64-encoded JSON
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
            # ADC / GOOGLE_APPLICATION_CREDENTIALS
            _client = storage.Client()

        name = bucket_name()
        try:
            _bucket = _client.get_bucket(name)
        except NotFound:
            location = os.environ.get("GCS_LOCATION", "asia-south1")
            _bucket = _client.create_bucket(name, location=location)
            print(f"Created GCS bucket gs://{name} in {location}", flush=True)
        print(f"GCS persistence enabled: gs://{name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _init_error = str(exc)
        _client = None
        _bucket = None
        print(f"GCS persistence unavailable: {exc}", flush=True)


def dataset_key(dataset: str, filename: str) -> str:
    return f"{dataset.strip().lower().strip('/')}/{filename.lstrip('/')}"


def users_key() -> str:
    return "users.json"


def upload_text(key: str, text: str, *, content_type: str = "application/json") -> bool:
    _ensure_client()
    if _bucket is None:
        return False
    blob = _bucket.blob(key)
    blob.upload_from_string(text, content_type=content_type)
    return True


def upload_file(key: str, path: Path, *, content_type: str = "application/json") -> bool:
    _ensure_client()
    if _bucket is None or not path.exists():
        return False
    blob = _bucket.blob(key)
    blob.upload_from_filename(str(path), content_type=content_type)
    return True


def download_text(key: str) -> str | None:
    _ensure_client()
    if _bucket is None:
        return None
    blob = _bucket.blob(key)
    if not blob.exists():
        return None
    return blob.download_as_text(encoding="utf-8")


def download_to_file(key: str, path: Path) -> bool:
    text = download_text(key)
    if text is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)
    return True


def mirror_local(path: Path, key: str) -> bool:
    """Write a local file up to GCS."""
    if not path.exists():
        return False
    return upload_file(key, path)


def hydrate_local(path: Path, key: str, *, prefer_remote: bool = True) -> bool:
    """
    Ensure local path has content.
    If prefer_remote, overwrite local from GCS when remote exists.
    Otherwise only download when local is missing.
    """
    if not prefer_remote and path.exists():
        return False
    return download_to_file(key, path)


def list_prefix(prefix: str) -> list[str]:
    _ensure_client()
    if _bucket is None:
        return []
    return [blob.name for blob in _client.list_blobs(_bucket, prefix=prefix)]


def sync_dataset_dir(uploads_dir: Path, dataset: str, *, prefer_remote: bool = True) -> dict:
    """Pull standard dataset files from GCS into uploads/<dataset>/."""
    files = (
        "calls.json",
        "corrected_transcripts.json",
        "sarvam_transcripts.json",
        "stt_progress.json",
    )
    result = {"dataset": dataset, "downloaded": [], "missing": []}
    for name in files:
        local = uploads_dir / dataset / name
        key = dataset_key(dataset, name)
        if hydrate_local(local, key, prefer_remote=prefer_remote):
            result["downloaded"].append(name)
        elif not local.exists():
            result["missing"].append(name)
    return result


def push_dataset_file(uploads_dir: Path, dataset: str, filename: str) -> bool:
    local = uploads_dir / dataset / filename
    return mirror_local(local, dataset_key(dataset, filename))


def push_users_file(path: Path) -> bool:
    return mirror_local(path, users_key())


def hydrate_users_file(path: Path, *, prefer_remote: bool = True) -> bool:
    return hydrate_local(path, users_key(), prefer_remote=prefer_remote)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)
