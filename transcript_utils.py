"""Shared transcript filtering and comparison helpers."""

from __future__ import annotations

import re

CALL_LIMIT = 1000

EXCLUDED_ROLES = {"tool", "tool_output"}
EXCLUDED_TYPES = {
    "agent_config_update",
    "function_call",
    "function_call_output",
    "language_switch",
}

# Leading SSML pause tags like <break time="1.0s" />
_LEADING_BREAK_RE = re.compile(
    r"^(?:\s*<break\b[^>]*/>\s*)+",
    re.IGNORECASE,
)


def clean_message_content(content: str) -> str:
    """Strip leading SSML break tags and surrounding whitespace."""
    text = str(content or "")
    text = _LEADING_BREAK_RE.sub("", text)
    return text.strip()


def visible_messages(messages: list[dict]) -> list[dict]:
    result = []
    for msg in messages:
        role = msg.get("role", "unknown")
        msg_type = msg.get("type", "message")
        if role in EXCLUDED_ROLES or msg_type in EXCLUDED_TYPES:
            continue

        if msg_type != "message" or role not in {"user", "assistant"}:
            continue

        content = clean_message_content(msg.get("content") or "")
        if not content:
            continue

        result.append(
            {
                "_id": msg.get("_id", ""),
                "role": role,
                "content": content,
                "type": "message",
                "createdAt": msg.get("createdAt", ""),
            }
        )
    return result


def preview_text(messages: list[dict], limit: int = 80) -> str:
    for msg in visible_messages(messages):
        content = (msg.get("content") or "").strip()
        if content:
            return content[:limit] + ("…" if len(content) > limit else "")
    return "(no transcript text)"


def align_stt_segments(
    original_messages: list[dict], segments: list[dict]
) -> list[dict]:
    aligned: list[dict] = []
    segment_idx = 0
    for orig in original_messages:
        entry = {**orig}
        if segment_idx < len(segments):
            seg = segments[segment_idx]
            entry["content"] = str(seg.get("content", "")).strip()
            if seg.get("start_s") is not None:
                entry["start_s"] = seg.get("start_s")
            if seg.get("end_s") is not None:
                entry["end_s"] = seg.get("end_s")
            segment_idx += 1
        else:
            entry["content"] = ""
        aligned.append(entry)
    return aligned


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def timings_from_created_at(messages: list[dict]) -> list[dict]:
    """Derive per-turn [start, end] seconds from message createdAt timestamps."""
    if not messages:
        return []

    stamps: list[float | None] = []
    for msg in messages:
        stamps.append(_as_float(msg.get("createdAt")))

    valid = [t for t in stamps if t is not None and t >= 0]
    if not valid:
        return [{"start": None, "end": None} for _ in messages]

    base = min(valid)
    # Absolute unix times → relative to first turn; already-relative times stay as-is.
    relative: list[float | None] = []
    for t in stamps:
        if t is None:
            relative.append(None)
        elif t >= 1_000_000_000:  # epoch seconds
            relative.append(max(0.0, t - base))
        else:
            relative.append(max(0.0, t))

    result: list[dict] = []
    for i, start in enumerate(relative):
        if start is None:
            result.append({"start": None, "end": None})
            continue
        end: float | None = None
        for later in relative[i + 1 :]:
            if later is not None and later > start:
                end = later
                break
        if end is None:
            content_len = len(str(messages[i].get("content") or ""))
            end = start + max(1.5, min(12.0, content_len / 12.0))
        result.append({"start": float(start), "end": float(end)})
    return result


def timings_from_stt_segments(segments: list[dict], turn_count: int) -> list[dict]:
    """Map STT segment start_s/end_s (or Sarvam entry fields) onto turn slots."""
    timings: list[dict] = []
    for i in range(turn_count):
        if i >= len(segments):
            timings.append({"start": None, "end": None})
            continue
        seg = segments[i] or {}
        start = _as_float(
            seg.get("start_s")
            if seg.get("start_s") is not None
            else seg.get("start_time_seconds")
            if seg.get("start_time_seconds") is not None
            else seg.get("start")
        )
        end = _as_float(
            seg.get("end_s")
            if seg.get("end_s") is not None
            else seg.get("end_time_seconds")
            if seg.get("end_time_seconds") is not None
            else seg.get("end")
        )
        timings.append({"start": start, "end": end})
    return timings


def default_final_messages(
    original_messages: list[dict],
    stt_messages: list[dict] | None,
    *,
    has_stt: bool,
) -> list[dict]:
    """Seed the Final editor from the Original transcript (not STT)."""
    del stt_messages, has_stt  # kept for call-site compatibility
    return [{**msg, "content": msg.get("content", "")} for msg in original_messages]


def clean_saved_messages(edited_messages: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for i, edited in enumerate(edited_messages, start=1):
        role = edited.get("role", "assistant")
        if role not in {"user", "assistant"}:
            role = "assistant"
        cleaned.append(
            {
                "n": i,
                "_id": edited.get("_id") or f"added-{i}",
                "role": role,
                "type": "message",
                "createdAt": edited.get("createdAt", ""),
                "content": str(edited.get("content", "")),
            }
        )
    return cleaned
