#!/usr/bin/env python3
"""Transcript review UI for multi-client call recordings."""

from __future__ import annotations

import load_env  # noqa: F401

import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from auth import (
    authenticate,
    current_user,
    register_user,
    require_login_before_request,
)
import gcs_storage
from json_format import dump_numbered, dumps_numbered
from stt_runner import (
    is_running as stt_is_running,
    read_progress as read_stt_progress,
    sarvam_path as dataset_sarvam_path,
    start_stt_job,
)


from transcript_utils import (
    align_stt_segments,
    timings_from_created_at,
    timings_from_stt_segments,
    clean_saved_messages,
    default_final_messages,
    preview_text,
    visible_messages,
)

BASE_DIR = Path(__file__).resolve().parent
ALL_DATA_DIR = BASE_DIR / "all_data"
UPLOADS_DIR = BASE_DIR / "uploads"

DATASET_META = (
    {"id": "indiamart", "label": "IndiaMART"},
    {"id": "abhfl", "label": "ABHFL"},
    {"id": "amber", "label": "Amber"},
)
DATASETS = tuple(item["id"] for item in DATASET_META)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or "golden-set-dev-secret"
app.before_request(require_login_before_request)

calls_by_id: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
call_order: dict[str, list[str]] = {name: [] for name in DATASETS}
corrections: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
sarvam_by_dataset: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
phrase_cache: dict[str, list[dict]] = {}


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
        return "indiamart"
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


def save_corrections(dataset: str) -> None:
    ensure_dataset_dirs(dataset)
    dump_numbered(
        corrections_path_for(dataset),
        corrections[dataset],
        call_order[dataset],
    )
    phrase_cache.pop(dataset, None)
    # Expand into per-call transcript_final.json under gs://…/<Agent>/<call_id>/
    gcs_storage.push_dataset_file(UPLOADS_DIR, dataset, "corrected_transcripts.json")


def load_corrections_file(dataset: str, path: Path | None) -> None:
    if not path or not path.exists():
        corrections[dataset] = {}
        return
    try:
        with path.open(encoding="utf-8") as handle:
            corrections[dataset] = json.load(handle)
    except json.JSONDecodeError:
        corrections[dataset] = {}


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


def hydrate_persistence() -> None:
    """Pull users + per-tab uploads from GCS before loading into memory."""
    gcs_storage.hydrate_users_file(UPLOADS_DIR / "users.json", prefer_remote=True)
    for dataset in DATASETS:
        gcs_storage.sync_dataset_dir(UPLOADS_DIR, dataset, prefer_remote=True)


def load_uploaded_dataset(dataset: str) -> None:
    path = upload_calls_path(dataset)
    if not path.exists():
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
        load_corrections_file(dataset, corrections_path_for(dataset))
        load_uploaded_dataset(dataset)
        path = dataset_sarvam_path(UPLOADS_DIR, dataset)
        if path.exists():
            try:
                with path.open(encoding="utf-8") as handle:
                    sarvam_by_dataset[dataset] = json.load(handle)
            except json.JSONDecodeError:
                sarvam_by_dataset[dataset] = {}
        if not call_order[dataset]:
            call_order[dataset] = sorted(calls_by_id[dataset].keys())


def load_data() -> None:
    for name in DATASETS:
        calls_by_id[name] = {}
        call_order[name] = []
        corrections[name] = {}
        sarvam_by_dataset[name] = {}
    phrase_cache.clear()
    hydrate_persistence()
    load_indiamart()
    load_empty_clients()
    print(f"Storage: {gcs_storage.status()}", flush=True)


def read_dataset_progress(dataset: str) -> dict:
    progress = read_stt_progress(UPLOADS_DIR, dataset)
    if progress.get("total") or progress.get("running") or progress.get("savedTotal"):
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


def reload_sarvam_transcripts(dataset: str | None = None) -> None:
    targets = [dataset] if dataset in DATASETS else list(DATASETS)
    for name in targets:
        merged = dict(sarvam_by_dataset.get(name) or {})
        upload_path = dataset_sarvam_path(UPLOADS_DIR, name)
        legacy_paths = []
        if name == "indiamart":
            legacy_paths = [
                BASE_DIR / "indiamart_sarvam_transcripts.json",
                ALL_DATA_DIR / "indiamart_sarvam_transcripts.json",
            ]
        for path in [upload_path, *legacy_paths]:
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    merged.update(loaded)
            except json.JSONDecodeError:
                continue
        sarvam_by_dataset[name] = merged


def _messages_from_stt_entry(
    entry: dict | None, original_messages: list[dict]
) -> list[dict] | None:
    if not entry:
        return None

    messages = [
        msg
        for msg in entry.get("messages", [])
        if msg.get("type") != "language_switch"
    ]
    if messages and len(messages) == len(original_messages):
        return messages

    segments = entry.get("segments") or []
    if segments:
        return align_stt_segments(original_messages, segments)
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
    """Prefer call createdAt timings; fall back to Sarvam STT segment times."""
    originals = original_messages or []
    from_created = timings_from_created_at(originals)
    if from_created and any(t.get("start") is not None for t in from_created):
        timings = list(from_created)
        while len(timings) < turn_count:
            timings.append({"start": None, "end": None})
        return timings[:turn_count]

    entry = sarvam_by_dataset.get(dataset, {}).get(call_id) or {}
    segments = entry.get("segments") or []
    if not segments:
        raw = entry.get("raw") or {}
        diarized = raw.get("diarized_transcript") or {}
        segments = diarized.get("entries") or []
    if segments:
        return timings_from_stt_segments(segments, turn_count)

    return [{"start": None, "end": None} for _ in range(turn_count)]


def review_status(saved: dict | None) -> str:
    if not saved:
        return "pending"
    if saved.get("unfit"):
        return "unfit"
    if saved.get("verifiedBy") and saved.get("verifiedAt"):
        return "verified"
    if saved.get("messages") or saved.get("editedBy"):
        return "edited"
    return "pending"


def build_call_payload(dataset: str, call_id: str) -> dict:
    call = calls_by_id[dataset][call_id]
    original_messages = visible_messages(call.get("messages", []))
    stt_messages = get_stt_messages(dataset, call_id, original_messages)
    has_stt = stt_messages is not None
    dataset_corrections = corrections[dataset]
    prev_id, next_id = neighbor_ids(dataset, call_id)

    saved = dataset_corrections.get(call_id)
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
    else:
        final_messages = default_final_messages(
            original_messages, stt_messages, has_stt=has_stt
        )

    turn_count = max(
        len(original_messages),
        len(final_messages),
        len(stt_messages or []),
    )
    timings = get_turn_timings(
        dataset, call_id, turn_count, original_messages=original_messages
    )

    return {
        "id": call_id,
        "dataset": dataset,
        "number": recording_number(dataset, call_id),
        "public_url": call.get("public_url", ""),
        "recordingUrl": call.get("recordingUrl", ""),
        "hasStt": has_stt,
        "messages": original_messages,
        "stt_messages": stt_messages,
        "final_messages": final_messages,
        "timings": timings,
        "edited": call_id in dataset_corrections,
        "status": review_status(saved),
        "editedBy": (saved or {}).get("editedBy") or "",
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
        weight = 12 if status == "verified" else 9
        label = "verified" if status == "verified" else "saved"
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
    return jsonify({"user": user})


@app.route("/api/storage")
def storage_status():
    return jsonify(gcs_storage.status())


@app.route("/")
def index():
    totals = {name: len(call_order[name]) for name in DATASETS}
    return render_template(
        "index.html",
        datasets=DATASET_META,
        totals=totals,
        current_user=current_user(),
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
    verified = sum(
        1 for cid in order if review_status(dataset_corrections.get(cid)) == "verified"
    )
    unfit = sum(1 for cid in order if review_status(dataset_corrections.get(cid)) == "unfit")
    pending = len(order) - edited - verified - unfit
    sarvam_store = sarvam_by_dataset.get(dataset, {})
    stt_generated = sum(1 for call_id in order if call_id in sarvam_store)

    progress = read_dataset_progress(dataset)
    if not progress.get("total"):
        progress = {
            **progress,
            "total": len(order),
            "savedTotal": stt_generated,
            "percent": round((stt_generated / len(order)) * 100, 1) if order else 0,
        }

    return jsonify(
        {
            "dataset": dataset,
            "total": len(order),
            "edited": edited,
            "verified": verified,
            "unfit": unfit,
            "pending": pending,
            "sttGenerated": stt_generated,
            "sttProgress": progress,
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

    order = call_order[dataset]
    dataset_corrections = corrections[dataset]
    sarvam_store = sarvam_by_dataset.get(dataset, {})
    filtered = order

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

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = filtered[start:end]

    items = []
    for call_id in page_ids:
        call = calls_by_id[dataset][call_id]
        messages = call.get("messages", [])
        saved = dataset_corrections.get(call_id)
        items.append(
            {
                "id": call_id,
                "number": recording_number(dataset, call_id),
                "preview": preview_text(messages),
                "messageCount": len(visible_messages(messages)),
                "edited": call_id in dataset_corrections,
                "status": review_status(saved),
                "editedBy": (saved or {}).get("editedBy") or "",
                "verifiedBy": (saved or {}).get("verifiedBy") or "",
                "hasAudio": bool(call.get("public_url")),
                "hasStt": call_id in sarvam_store,
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
    corrections[dataset][call_id] = {
        "number": recording_number(dataset, call_id),
        "callLogId": call_id,
        "messages": cleaned,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "editedBy": reviewer,
        # Re-saving clears verification so a second person must re-verify
        "verifiedBy": "",
        "verifiedAt": None,
        # Saving a final also clears unfit
        "unfit": False,
        "unfitBy": "",
        "unfitAt": None,
    }
    save_corrections(dataset)

    return jsonify(
        {
            "ok": True,
            "updatedAt": corrections[dataset][call_id]["updatedAt"],
            "editedBy": reviewer,
            "status": "edited",
        }
    )


@app.route("/api/calls/<call_id>/verify", methods=["POST"])
def verify_correct(call_id: str):
    dataset = resolve_dataset()
    if call_id not in calls_by_id[dataset]:
        return jsonify({"error": "Call not found"}), 404

    saved = corrections[dataset].get(call_id)
    if not saved:
        return jsonify({"error": "Save the final transcript before verifying"}), 400
    if saved.get("unfit"):
        return jsonify({"error": "Clear unfit status before verifying"}), 400

    verifier = current_user()
    if not verifier:
        return jsonify({"error": "Login required"}), 401

    editor = (saved.get("editedBy") or "").strip().lower()
    if editor and editor == verifier.lower():
        return jsonify(
            {"error": "Verification must be done by a different user than the editor"}
        ), 400

    saved["verifiedBy"] = verifier
    saved["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    corrections[dataset][call_id] = saved
    save_corrections(dataset)

    return jsonify(
        {
            "ok": True,
            "verifiedBy": verifier,
            "verifiedAt": saved["verifiedAt"],
            "status": "verified",
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

    existing = corrections[dataset].get(call_id) or {}

    if request.method == "DELETE":
        if not existing:
            return jsonify({"ok": True, "status": "pending"})
        existing.pop("unfit", None)
        existing.pop("unfitBy", None)
        existing.pop("unfitAt", None)
        # Drop empty unfit-only records
        if not existing.get("messages") and not existing.get("editedBy"):
            corrections[dataset].pop(call_id, None)
            save_corrections(dataset)
            return jsonify({"ok": True, "status": "pending"})
        corrections[dataset][call_id] = existing
        save_corrections(dataset)
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
        "verifiedBy": "",
        "verifiedAt": None,
    }
    corrections[dataset][call_id] = entry
    save_corrections(dataset)

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
        save_corrections(dataset)
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

    return jsonify(
        {
            "ok": True,
            "dataset": dataset,
            "imported": count,
            "total": len(call_order[dataset]),
        }
    )


@app.route("/api/export/verified")
def export_verified():
    dataset = resolve_dataset()
    order = call_order[dataset]
    export_data: dict[str, dict] = {}
    verified_order: list[str] = []

    for call_id in order:
        saved = corrections[dataset].get(call_id)
        if review_status(saved) != "verified":
            continue
        verified_order.append(call_id)
        export_data[call_id] = {
            "callLogId": call_id,
            "messages": saved.get("messages") or [],
            "updatedAt": saved.get("updatedAt"),
            "editedBy": saved.get("editedBy") or "",
            "verifiedBy": saved.get("verifiedBy") or "",
            "verifiedAt": saved.get("verifiedAt"),
            "public_url": calls_by_id[dataset].get(call_id, {}).get("public_url", ""),
        }

    if not export_data:
        return jsonify({"error": "No verified transcripts to export"}), 404

    body = dumps_numbered(export_data, verified_order)
    filename = f"{dataset}_verified_transcripts.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/stt/start", methods=["POST"])
def start_sarvam_stt():
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
                "public_url": call.get("public_url") or "",
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
    import requests as http_requests

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True, "text": "", "candidates": []})

    # Tiny process-local cache to make repeated words fast
    cache = getattr(transliterate, "_cache", None)
    if cache is None:
        transliterate._cache = {}
        cache = transliterate._cache
    cache_key = text.lower()
    if cache_key in cache:
        candidates = cache[cache_key]
        return jsonify(
            {
                "ok": True,
                "text": candidates[0] if candidates else text,
                "candidates": candidates,
            }
        )

    session = getattr(transliterate, "_session", None)
    if session is None:
        transliterate._session = http_requests.Session()
        session = transliterate._session

    try:
        response = session.get(
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
        cache[cache_key] = candidates
        if len(cache) > 1000:
            cache.pop(next(iter(cache)))
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
