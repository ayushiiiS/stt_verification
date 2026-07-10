#!/usr/bin/env python3
"""Transcript review UI for Muthoot call recordings."""

from __future__ import annotations

import load_env  # noqa: F401

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from stt_progress import read_progress
from transcript_utils import (
    CALL_LIMIT,
    clean_saved_messages,
    default_final_messages,
    preview_text,
    visible_messages,
)

BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPTS_PATH = BASE_DIR / "ai-agents-production.transcripts.json"
CSV_PATH = BASE_DIR / "muthoot_with_public_urls .csv"
CORRECTIONS_PATH = BASE_DIR / "corrected_transcripts.json"
SARVAM_PATH = BASE_DIR / "sarvam_transcripts.json"

app = Flask(__name__)

calls_by_id: dict[str, dict] = {}
call_order: list[str] = []
corrections: dict[str, dict] = {}
sarvam_transcripts: dict[str, dict] = {}


def oid(value) -> str:
    if isinstance(value, dict) and "$oid" in value:
        return value["$oid"]
    return str(value)


def load_data() -> None:
    global calls_by_id, call_order, corrections, sarvam_transcripts

    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            call_id = row["_id"]
            calls_by_id[call_id] = {
                "id": call_id,
                "recordingUrl": row.get("recordingUrl", ""),
                "public_url": row.get("public_url", ""),
            }

    with TRANSCRIPTS_PATH.open(encoding="utf-8") as handle:
        transcripts = json.load(handle)

    for item in transcripts:
        call_id = oid(item["callLogId"])
        if call_id not in calls_by_id:
            calls_by_id[call_id] = {"id": call_id, "recordingUrl": "", "public_url": ""}
        calls_by_id[call_id]["transcript"] = item
        calls_by_id[call_id]["messages"] = item.get("messages", [])

    call_order = sorted(calls_by_id.keys())[:CALL_LIMIT]

    if CORRECTIONS_PATH.exists():
        with CORRECTIONS_PATH.open(encoding="utf-8") as handle:
            corrections = json.load(handle)

    if SARVAM_PATH.exists():
        with SARVAM_PATH.open(encoding="utf-8") as handle:
            sarvam_transcripts = json.load(handle)


def reload_sarvam_transcripts() -> None:
    global sarvam_transcripts
    if SARVAM_PATH.exists():
        try:
            with SARVAM_PATH.open(encoding="utf-8") as handle:
                sarvam_transcripts = json.load(handle)
        except json.JSONDecodeError:
            sarvam_transcripts = {}
    else:
        sarvam_transcripts = {}


def save_corrections() -> None:
    with CORRECTIONS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(corrections, handle, ensure_ascii=False, indent=2)


def get_stt_messages(call_id: str, original_messages: list[dict]) -> list[dict] | None:
    entry = sarvam_transcripts.get(call_id)
    if not entry:
        return None
    messages = entry.get("messages", [])
    if len(messages) != len(original_messages):
        return None
    return messages


def build_call_payload(call_id: str) -> dict:
    call = calls_by_id[call_id]
    original_messages = visible_messages(call.get("messages", []))
    stt_messages = get_stt_messages(call_id, original_messages)
    has_stt = stt_messages is not None

    saved = corrections.get(call_id)
    if saved:
        final_messages = saved.get("messages", [])
        if len(final_messages) != len(original_messages):
            final_messages = default_final_messages(
                original_messages, stt_messages, has_stt=has_stt
            )
    else:
        final_messages = default_final_messages(
            original_messages, stt_messages, has_stt=has_stt
        )

    return {
        "id": call_id,
        "public_url": call.get("public_url", ""),
        "recordingUrl": call.get("recordingUrl", ""),
        "hasStt": has_stt,
        "messages": original_messages,
        "stt_messages": stt_messages,
        "final_messages": final_messages,
        "edited": call_id in corrections,
        "updatedAt": saved.get("updatedAt") if saved else None,
        "sttGeneratedAt": sarvam_transcripts.get(call_id, {}).get("generatedAt")
        if has_stt
        else None,
    }


@app.route("/")
def index():
    return render_template("index.html", total_calls=len(call_order))


@app.route("/api/stats")
def stats():
    reload_sarvam_transcripts()
    edited = len(corrections)
    stt_generated = sum(1 for call_id in call_order if call_id in sarvam_transcripts)
    progress = read_progress()
    if not progress.get("total"):
        progress["total"] = len(call_order)
        progress["savedTotal"] = stt_generated
        progress["percent"] = round((stt_generated / len(call_order)) * 100, 1) if call_order else 0
    return jsonify(
        {
            "total": len(call_order),
            "edited": edited,
            "pending": len(call_order) - edited,
            "sttGenerated": stt_generated,
            "sttProgress": progress,
        }
    )


@app.route("/api/calls")
def list_calls():
    reload_sarvam_transcripts()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(10, request.args.get("per_page", 50, type=int)))
    search = (request.args.get("search") or "").strip().lower()
    status = request.args.get("status", "all")

    filtered = call_order
    if search:
        filtered = [cid for cid in filtered if search in cid.lower()]
    if status == "edited":
        filtered = [cid for cid in filtered if cid in corrections]
    elif status == "pending":
        filtered = [cid for cid in filtered if cid not in corrections]

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = filtered[start:end]

    items = []
    for call_id in page_ids:
        call = calls_by_id[call_id]
        messages = call.get("messages", [])
        items.append(
            {
                "id": call_id,
                "preview": preview_text(messages),
                "messageCount": len(visible_messages(messages)),
                "edited": call_id in corrections,
                "hasAudio": bool(call.get("public_url")),
                "hasStt": call_id in sarvam_transcripts,
            }
        )

    return jsonify(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }
    )


@app.route("/api/calls/<call_id>")
def get_call(call_id: str):
    reload_sarvam_transcripts()
    if call_id not in calls_by_id:
        return jsonify({"error": "Call not found"}), 404
    return jsonify(build_call_payload(call_id))


@app.route("/api/calls/<call_id>/correct", methods=["POST"])
def save_correct(call_id: str):
    if call_id not in calls_by_id:
        return jsonify({"error": "Call not found"}), 404

    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return jsonify({"error": "messages array required"}), 400

    original = visible_messages(calls_by_id[call_id].get("messages", []))
    if len(messages) != len(original):
        return jsonify({"error": "message count must match transcript structure"}), 400

    cleaned = clean_saved_messages(original, messages)
    corrections[call_id] = {
        "callLogId": call_id,
        "messages": cleaned,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_corrections()

    return jsonify({"ok": True, "updatedAt": corrections[call_id]["updatedAt"]})


@app.route("/api/calls/<call_id>/correct", methods=["DELETE"])
def reset_correct(call_id: str):
    if call_id in corrections:
        del corrections[call_id]
        save_corrections()
    payload = build_call_payload(call_id)
    return jsonify({"ok": True, "final_messages": payload["final_messages"]})


if __name__ == "__main__":
    print("Loading data…")
    load_data()
    print(f"Loaded {len(call_order)} calls (first {CALL_LIMIT})")
    print(f"Sarvam STT generated: {len(sarvam_transcripts)} transcripts")
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=True)
