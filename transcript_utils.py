"""Shared transcript filtering and comparison helpers."""

from __future__ import annotations

CALL_LIMIT = 1000

EXCLUDED_ROLES = {"tool", "tool_output"}
EXCLUDED_TYPES = {
    "agent_config_update",
    "function_call",
    "function_call_output",
    "language_switch",
}


def visible_messages(messages: list[dict]) -> list[dict]:
    result = []
    for msg in messages:
        role = msg.get("role", "unknown")
        msg_type = msg.get("type", "message")
        if role in EXCLUDED_ROLES or msg_type in EXCLUDED_TYPES:
            continue

        if msg_type != "message" or role not in {"user", "assistant"}:
            continue

        content = (msg.get("content") or "").strip()
        if not content:
            continue

        result.append(
            {
                "_id": msg.get("_id", ""),
                "role": role,
                "content": msg.get("content", ""),
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
            entry["content"] = str(segments[segment_idx].get("content", "")).strip()
            segment_idx += 1
        else:
            entry["content"] = ""
        aligned.append(entry)
    return aligned


def default_final_messages(
    original_messages: list[dict],
    stt_messages: list[dict] | None,
    *,
    has_stt: bool,
) -> list[dict]:
    if has_stt and stt_messages and len(stt_messages) == len(original_messages):
        final: list[dict] = []
        for orig, stt in zip(original_messages, stt_messages):
            content = (stt.get("content") or "").strip() or orig.get("content", "")
            final.append({**orig, "content": content})
        return final
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
