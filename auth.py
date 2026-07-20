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
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

_BASE_DIR = Path(__file__).resolve().parent


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


_lock = threading.Lock()


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
        username = username.strip().lower()
        password = password.strip()
        if username and password:
            overrides[username] = password
    return overrides


def _default_hashes() -> dict[str, str]:
    overrides = _parse_password_overrides()
    store: dict[str, str] = {}
    for username in DEFAULT_USERS:
        password = overrides.get(username) or username
        store[username] = generate_password_hash(password)
    return store


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

        gcs_storage.push_users_file(path)
    except Exception as exc:  # noqa: BLE001
        print(f"GCS users sync failed: {exc}", flush=True)


def _read_users_file() -> dict[str, str]:
    path = _users_path()
    try:
        import gcs_storage

        gcs_storage.hydrate_users_file(path, prefer_remote=False)
    except Exception:
        pass
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        return {
            str(user).strip().lower(): str(password_hash)
            for user, password_hash in data.items()
            if user and password_hash
        }
    except (json.JSONDecodeError, OSError):
        return {}


def load_user_store() -> dict[str, str]:
    with _lock:
        store = _default_hashes()
        stored = _read_users_file()
        store.update(stored)
        # Ensure seeded users exist on disk for restarts
        if not stored:
            _write_users_file(store)
        return store


def user_exists(username: str) -> bool:
    user = (username or "").strip().lower()
    return user in load_user_store()


def authenticate(username: str, password: str) -> str | None:
    user = (username or "").strip().lower()
    store = load_user_store()
    password_hash = store.get(user)
    if not password_hash:
        return None
    if not check_password_hash(password_hash, password or ""):
        return None
    return user


def register_user(username: str, password: str) -> tuple[str | None, str | None]:
    """Create a user. Returns (username, error)."""
    user = (username or "").strip().lower()
    password = password or ""

    if not USERNAME_RE.match(user):
        return None, "Username must be 3–32 chars, start with a letter, and use a–z, 0–9, _"
    if len(password) < 4:
        return None, "Password must be at least 4 characters"

    with _lock:
        store = _default_hashes()
        store.update(_read_users_file())
        if user in store:
            return None, "That username is already taken"
        store[user] = generate_password_hash(password)
        _write_users_file(store)
    return user, None


def current_user() -> str | None:
    user = session.get("user")
    if isinstance(user, str) and user_exists(user):
        return user
    return None


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
