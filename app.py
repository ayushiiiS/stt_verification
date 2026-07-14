#!/usr/bin/env python3
"""Transcript review UI for Muthoot and IndiaMART call recordings."""

from __future__ import annotations

import load_env  # noqa: F401

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from stt_progress import read_progress
from json_format import dump_numbered
from transcript_utils import (
    CALL_LIMIT,
    align_stt_segments,
    clean_saved_messages,
    default_final_messages,
    preview_text,
    visible_messages,
)

BASE_DIR = Path(__file__).resolve().parent

MUTHOOT_TRANSCRIPTS_PATH = (
    BASE_DIR / "data_transcripts.json"
    if (BASE_DIR / "data_transcripts.json").exists()
    else BASE_DIR / "ai-agents-production.transcripts.json"
)
MUTHOOT_CSV_PATH = (
    BASE_DIR / "data_calls.csv"
    if (BASE_DIR / "data_calls.csv").exists()
    else BASE_DIR / "muthoot_with_public_urls .csv"
)
MUTHOOT_CORRECTIONS_PATH = BASE_DIR / "corrected_transcripts.json"
SARVAM_PATH = BASE_DIR / "sarvam_transcripts.json"

INDIAMART_TRANSCRIPTS_PATH = BASE_DIR / "indiamart_63_transcripts.json"
INDIAMART_CSV_PATH = BASE_DIR / "indiamart_final63_public_urls.csv"
INDIAMART_CORRECTIONS_PATH = BASE_DIR / "indiamart_corrected_transcripts.json"
INDIAMART_SARVAM_PATH = BASE_DIR / "indiamart_sarvam_transcripts.json"
INDIAMART_PROGRESS_PATH = BASE_DIR / "indiamart_stt_progress.json"

DATASETS = ("muthoot", "indiamart")

app = Flask(__name__)

calls_by_id: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
call_order: dict[str, list[str]] = {name: [] for name in DATASETS}
corrections: dict[str, dict[str, dict]] = {name: {} for name in DATASETS}
sarvam_by_dataset: dict[str, dict[str, dict]] = {"muthoot": {}, "indiamart": {}}


def oid(value) -> str:
    if isinstance(value, dict) and "$oid" in value:
        return value["$oid"]
    return str(value)


def call_id_from_indiamart_url(url: str) -> str | None:
    match = re.search(r"/recording/([a-f0-9]{24})/", url or "")
    return match.group(1) if match else None


def resolve_dataset(raw: str | None = None) -> str:
    name = (raw or request.args.get("dataset") or "muthoot").strip().lower()
    if name not in DATASETS:
        return "muthoot"
    return name


def recording_number(dataset: str, call_id: str) -> int | None:
    try:
        return call_order[dataset].index(call_id) + 1
    except ValueError:
        return None


def corrections_path_for(dataset: str) -> Path:
    if dataset == "indiamart":
        return INDIAMART_CORRECTIONS_PATH
    return MUTHOOT_CORRECTIONS_PATH


def load_muthoot() -> None:
    with MUTHOOT_CSV_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            call_id = row["_id"]
            calls_by_id["muthoot"][call_id] = {
                "id": call_id,
                "recordingUrl": row.get("recordingUrl", ""),
                "public_url": row.get("public_url", ""),
            }

    with MUTHOOT_TRANSCRIPTS_PATH.open(encoding="utf-8") as handle:
        transcripts = json.load(handle)

    for item in transcripts:
        call_id = oid(item["callLogId"])
        if call_id not in calls_by_id["muthoot"]:
            calls_by_id["muthoot"][call_id] = {
                "id": call_id,
                "recordingUrl": "",
                "public_url": "",
            }
        calls_by_id["muthoot"][call_id]["transcript"] = item
        calls_by_id["muthoot"][call_id]["messages"] = item.get("messages", [])

    call_order["muthoot"] = sorted(calls_by_id["muthoot"].keys())[:CALL_LIMIT]

    if MUTHOOT_CORRECTIONS_PATH.exists():
        with MUTHOOT_CORRECTIONS_PATH.open(encoding="utf-8") as handle:
            corrections["muthoot"] = json.load(handle)

    if SARVAM_PATH.exists():
        with SARVAM_PATH.open(encoding="utf-8") as handle:
            sarvam_by_dataset["muthoot"] = json.load(handle)


def load_indiamart() -> None:
    url_by_id: dict[str, str] = {}
    if INDIAMART_CSV_PATH.exists():
        with INDIAMART_CSV_PATH.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                call_id = call_id_from_indiamart_url(
                    row.get("public_url") or row.get("url") or ""
                )
                if not call_id:
                    continue
                url_by_id[call_id] = row.get("public_url") or row.get("url") or ""

    if not INDIAMART_TRANSCRIPTS_PATH.exists():
        return

    with INDIAMART_TRANSCRIPTS_PATH.open(encoding="utf-8") as handle:
        transcripts = json.load(handle)

    for item in transcripts:
        call_id = oid(item["callLogId"])
        calls_by_id["indiamart"][call_id] = {
            "id": call_id,
            "recordingUrl": "",
            "public_url": url_by_id.get(call_id, ""),
            "transcript": item,
            "messages": item.get("messages", []),
        }

    call_order["indiamart"] = sorted(calls_by_id["indiamart"].keys())

    if INDIAMART_CORRECTIONS_PATH.exists():
        with INDIAMART_CORRECTIONS_PATH.open(encoding="utf-8") as handle:
            corrections["indiamart"] = json.load(handle)

    if INDIAMART_SARVAM_PATH.exists():
        with INDIAMART_SARVAM_PATH.open(encoding="utf-8") as handle:
            sarvam_by_dataset["indiamart"] = json.load(handle)


def load_data() -> None:
    for name in DATASETS:
        calls_by_id[name] = {}
        call_order[name] = []
        corrections[name] = {}
        sarvam_by_dataset[name] = {}
    load_muthoot()
    load_indiamart()


def read_dataset_progress(dataset: str) -> dict:
    if dataset == "indiamart":
        path = INDIAMART_PROGRESS_PATH
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            return {}
    return read_progress()


def reload_sarvam_transcripts(dataset: str | None = None) -> None:
    targets = [dataset] if dataset in DATASETS else list(DATASETS)
    for name in targets:
        path = SARVAM_PATH if name == "muthoot" else INDIAMART_SARVAM_PATH
        if path.exists():
            try:
                with path.open(encoding="utf-8") as handle:
                    sarvam_by_dataset[name] = json.load(handle)
            except json.JSONDecodeError:
                sarvam_by_dataset[name] = {}
        else:
            sarvam_by_dataset[name] = {}


def save_corrections(dataset: str) -> None:
    dump_numbered(
        corrections_path_for(dataset),
        corrections[dataset],
        call_order[dataset],
    )


def get_stt_messages(
    dataset: str, call_id: str, original_messages: list[dict]
) -> list[dict] | None:
    entry = sarvam_by_dataset.get(dataset, {}).get(call_id)
    if not entry:
        return None

    messages = [
        msg
        for msg in entry.get("messages", [])
        if msg.get("type") != "language_switch"
    ]
    if len(messages) == len(original_messages):
        return messages

    segments = entry.get("segments") or []
    if segments:
        return align_stt_segments(original_messages, segments)
    return None


def neighbor_ids(dataset: str, call_id: str) -> tuple[str | None, str | None]:
    order = call_order[dataset]
    try:
        idx = order.index(call_id)
    except ValueError:
        return None, None
    prev_id = order[idx - 1] if idx > 0 else None
    next_id = order[idx + 1] if idx + 1 < len(order) else None
    return prev_id, next_id


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
        "edited": call_id in dataset_corrections,
        "updatedAt": saved.get("updatedAt") if saved else None,
        "sttGeneratedAt": sarvam_by_dataset.get(dataset, {})
        .get(call_id, {})
        .get("generatedAt")
        if has_stt
        else None,
        "prevId": prev_id,
        "nextId": next_id,
    }


@app.route("/")
def index():
    return render_template(
        "index.html",
        muthoot_total=len(call_order["muthoot"]),
        indiamart_total=len(call_order["indiamart"]),
    )


@app.route("/api/stats")
def stats():
    dataset = resolve_dataset()
    reload_sarvam_transcripts(dataset)

    order = call_order[dataset]
    edited = len(corrections[dataset])
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
            "pending": len(order) - edited,
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
        filtered = [cid for cid in filtered if cid in dataset_corrections]
    elif status == "pending":
        filtered = [cid for cid in filtered if cid not in dataset_corrections]

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = filtered[start:end]

    items = []
    for call_id in page_ids:
        call = calls_by_id[dataset][call_id]
        messages = call.get("messages", [])
        items.append(
            {
                "id": call_id,
                "number": recording_number(dataset, call_id),
                "preview": preview_text(messages),
                "messageCount": len(visible_messages(messages)),
                "edited": call_id in dataset_corrections,
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
    }
    save_corrections(dataset)

    return jsonify(
        {"ok": True, "updatedAt": corrections[dataset][call_id]["updatedAt"]}
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


if __name__ == "__main__":
    print("Loading data…")
    load_data()
    print(f"Muthoot: {len(call_order['muthoot'])} calls")
    print(f"IndiaMART: {len(call_order['indiamart'])} calls")
    print(f"Muthoot Sarvam: {len(sarvam_by_dataset['muthoot'])}")
    print(f"IndiaMART Sarvam: {len(sarvam_by_dataset['indiamart'])}")
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
