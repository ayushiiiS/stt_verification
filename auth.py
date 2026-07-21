"""Simple session auth for the transcript review app."""

from __future__ import annotations

import json
import os
import re
import threading
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_USERS = ("ayushi", "kriti", "akash", "yash")
SARVAM_STT_ADMIN_USERS = frozenset({"ayushi"})
LABEL_LLM_ADMIN_USERS = frozenset({"ayushi"})


def _label_llm_admin_users() -> frozenset[str]:
    raw = (os.environ.get("LABEL_LLM_ADMINS") or "ayushi").strip()
    return frozenset(
        normalize_identity(part)
        for part in raw.split(",")
        if normalize_identity(part)
    )
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", re.IGNORECASE)

_BASE_DIR = Path(__file__).resolve().parent
_lock = threading.Lock()
_store_cache: dict[str, str] | None = None
_default_hashes_cache: dict[str, str] | None = None


def _uploads_root() -> Path:
    env = (os.environ.get("GOLDEN_SET_UPLOADS_DIR") or "").strip()
    if env:
        return Path(env)
    if (os.environ.get("VERCEL") or "").strip() or (
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or ""
    ).strip():
        return Path("/tmp/golden_set_uploads")
    return _BASE_DIR / "uploads"


def _users_path() -> Path:
    return _uploads_root() / "users.json"


def _is_serverless() -> bool:
    return bool(
        (os.environ.get("VERCEL") or "").strip()
        or (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
    )


def normalize_identity(value: str) -> str:
    return (value or "").strip().lower()


def is_valid_identity(value: str) -> bool:
    user = normalize_identity(value)
    if not user:
        return False
    if "@" in user:
        return bool(EMAIL_RE.match(user))
    return bool(USERNAME_RE.match(user))


def _parse_password_overrides() -> dict[str, str]:
    raw = (os.environ.get("AUTH_PASSWORDS") or "").strip()
    overrides: dict[str, str] = {}
    if not raw:
        return overrides
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        username, password = part.split(":", 1)
        username = normalize_identity(username)
        password = password.strip()
        if username and password:
            overrides[username] = password
    return overrides


def _default_hashes() -> dict[str, str]:
    """Generate seeded user hashes once (bcrypt is intentionally slow)."""
    global _default_hashes_cache
    if _default_hashes_cache is not None:
        return dict(_default_hashes_cache)
    overrides = _parse_password_overrides()
    store: dict[str, str] = {}
    for username in DEFAULT_USERS:
        password = overrides.get(username) or username
        store[username] = generate_password_hash(password)
    _default_hashes_cache = store
    return dict(store)


def invalidate_user_cache() -> None:
    global _store_cache
    with _lock:
        _store_cache = None


def _write_users_file(store: dict[str, str]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)
    try:
        import gcs_storage

        ok = gcs_storage.push_users_file(path)
        if not ok and _is_serverless():
            raise RuntimeError("Failed to persist users to cloud storage")
    except Exception as exc:  # noqa: BLE001
        print(f"GCS users sync failed: {exc}", flush=True)
        if _is_serverless():
            raise


def _read_users_file(*, prefer_remote: bool | None = None) -> dict[str, str]:
    path = _users_path()
    if prefer_remote is None:
        # On serverless always pull remote when missing; otherwise keep local.
        prefer_remote = _is_serverless() or not path.exists()
    try:
        import gcs_storage

        gcs_storage.hydrate_users_file(path, prefer_remote=prefer_remote)
    except Exception as exc:  # noqa: BLE001
        print(f"GCS users hydrate failed: {exc}", flush=True)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        return {
            normalize_identity(str(user)): str(password_hash)
            for user, password_hash in data.items()
            if user and password_hash
        }
    except (json.JSONDecodeError, OSError):
        return {}


def load_user_store(*, force_refresh: bool = False) -> dict[str, str]:
    global _store_cache
    with _lock:
        if _store_cache is not None and not force_refresh:
            return _store_cache
        store = _default_hashes()
        stored = _read_users_file(
            prefer_remote=True if force_refresh or _is_serverless() else None
        )
        store.update(stored)
        if not stored and not path_exists_safe():
            try:
                _write_users_file(store)
            except Exception as exc:  # noqa: BLE001
                print(f"seed users write failed: {exc}", flush=True)
        _store_cache = store
        return _store_cache


def path_exists_safe() -> bool:
    try:
        return _users_path().exists()
    except OSError:
        return False


def user_exists(username: str) -> bool:
    user = normalize_identity(username)
    if user in load_user_store():
        return True
    # One forced remote refresh (covers cold /tmp on Vercel after signup elsewhere).
    store = load_user_store(force_refresh=True)
    return user in store


def authenticate(username: str, password: str) -> str | None:
    user = normalize_identity(username)
    store = load_user_store(force_refresh=_is_serverless())
    password_hash = store.get(user)
    if not password_hash:
        return None
    if not check_password_hash(password_hash, password or ""):
        return None
    return user


def register_user(username: str, password: str) -> tuple[str | None, str | None]:
    """Create a user (email or username). Returns (identity, error)."""
    user = normalize_identity(username)
    password = password or ""

    if not is_valid_identity(user):
        return (
            None,
            "Enter a valid email (you@company.com) or username (3–32 chars, a–z, 0–9, _)",
        )
    if len(password) < 4:
        return None, "Password must be at least 4 characters"

    with _lock:
        # Fresh remote merge so we don't overwrite other signups on another instance.
        store = _default_hashes()
        store.update(_read_users_file(prefer_remote=True))
        if user in store:
            return None, "That email or username is already taken"
        store[user] = generate_password_hash(password)
        try:
            _write_users_file(store)
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not save account: {exc}"
        global _store_cache
        _store_cache = store
    return user, None


def current_user() -> str | None:
    user = session.get("user")
    if not isinstance(user, str):
        return None
    user = normalize_identity(user)
    store = load_user_store()
    if user in store:
        return user
    # Session user missing locally — refresh once from GCS.
    store = load_user_store(force_refresh=True)
    return user if user in store else None


def can_manage_sarvam_stt(user: str | None = None) -> bool:
    """Only designated admins may start/stop Sarvam STT jobs from the UI."""
    identity = normalize_identity(user if user is not None else (current_user() or ""))
    return identity in SARVAM_STT_ADMIN_USERS


def can_manage_label_llm(user: str | None = None) -> bool:
    """Only designated admins may run LLM auto-labeling (batch or per-call)."""
    identity = normalize_identity(user if user is not None else (current_user() or ""))
    return identity in _label_llm_admin_users()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Login required"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapped


def require_login_before_request():
    """Use as Flask before_request handler."""
    if request.endpoint in {"login", "signup", "static"}:
        return None
    if request.path.startswith("/static/"):
        return None
    if current_user():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Login required"}), 401
    return redirect(url_for("login", next=request.path))
