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


def preview_text(messages: list[dict], limit: int = 160) -> str:
    for msg in visible_messages(messages):
        content = (msg.get("content") or "").strip()
        if content:
            return content[:limit] + ("…" if len(content) > limit else "")
    return "(no transcript text)"


def align_stt_segments(
    original_messages: list[dict], segments: list[dict]
) -> list[dict]:
    """Map STT segments onto original turns.

    Prefer role-aware matching (next same-role segment) when segments carry
    assistant/user roles — important for human/agent track merges. Fall back
    to sequential index mapping otherwise.

    When a role has more STT segments than original turns, adjacent segments
    are merged so no transcript text is dropped.
    """
    role_aware = any(
        str(seg.get("role") or "") in {"assistant", "user"} for seg in segments
    )
    if role_aware:
        pools: dict[str, list[dict]] = {"assistant": [], "user": []}
        for seg in segments:
            role = str(seg.get("role") or "")
            if role in pools:
                pools[role].append(seg)
        role_targets = {"assistant": 0, "user": 0}
        for orig in original_messages:
            role = str(orig.get("role") or "")
            if role in role_targets:
                role_targets[role] += 1
        for role, target in role_targets.items():
            pools[role] = _fit_segments_to_count(pools.get(role) or [], target)

        cursor = {"assistant": 0, "user": 0}
        aligned: list[dict] = []
        for orig in original_messages:
            entry = {**orig}
            role = str(orig.get("role") or "")
            pool = pools.get(role) or []
            idx = cursor.get(role, 0)
            if idx < len(pool):
                seg = pool[idx]
                entry["content"] = str(seg.get("content", "")).strip()
                if seg.get("start_s") is not None:
                    entry["start_s"] = seg.get("start_s")
                if seg.get("end_s") is not None:
                    entry["end_s"] = seg.get("end_s")
                cursor[role] = idx + 1
            else:
                entry["content"] = ""
            aligned.append(entry)
        return aligned

    fitted = _fit_segments_to_count(list(segments), len(original_messages))
    aligned = []
    for i, orig in enumerate(original_messages):
        entry = {**orig}
        if i < len(fitted):
            seg = fitted[i]
            entry["content"] = str(seg.get("content", "")).strip()
            if seg.get("start_s") is not None:
                entry["start_s"] = seg.get("start_s")
            if seg.get("end_s") is not None:
                entry["end_s"] = seg.get("end_s")
        else:
            entry["content"] = ""
        aligned.append(entry)
    return aligned


def _fit_segments_to_count(segments: list[dict], target: int) -> list[dict]:
    """Merge adjacent segments until count <= target (preserves all text)."""
    if target <= 0:
        return []
    if len(segments) <= target:
        return segments

    segs = [dict(seg) for seg in segments]
    while len(segs) > target:
        # Merge the pair with the smallest time gap (or last pair as fallback).
        best_i = len(segs) - 2
        best_gap = float("inf")
        for i in range(len(segs) - 1):
            left_end = segs[i].get("end_s")
            right_start = segs[i + 1].get("start_s")
            try:
                gap = float(right_start) - float(left_end)
            except (TypeError, ValueError):
                gap = 0.0
            if gap < best_gap:
                best_gap = gap
                best_i = i
        left = segs[best_i]
        right = segs[best_i + 1]
        merged = {
            **left,
            "content": f"{left.get('content', '')} {right.get('content', '')}".strip(),
            "start_s": left.get("start_s")
            if left.get("start_s") is not None
            else right.get("start_s"),
            "end_s": right.get("end_s")
            if right.get("end_s") is not None
            else left.get("end_s"),
        }
        segs[best_i : best_i + 2] = [merged]
    return segs


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def timings_from_created_at(messages: list[dict]) -> list[dict]:
    """Map turns onto audio using message createdAt as absolute-ish anchors.

    Uses relative createdAt as each turn's *start* (monotonic). End is the next
    turn's start (or a short content estimate for the last turn). Avoids the
    previous role-based expansion that drifted far from the recording.
    """
    if not messages:
        return []

    stamps: list[float | None] = [_as_float(msg.get("createdAt")) for msg in messages]
    valid = [t for t in stamps if t is not None and t >= 0]
    if not valid:
        return [{"start": None, "end": None} for _ in messages]

    base = min(valid)
    relative: list[float | None] = []
    for t in stamps:
        if t is None:
            relative.append(None)
        elif t >= 1_000_000_000:  # epoch seconds
            relative.append(max(0.0, t - base))
        else:
            relative.append(max(0.0, t))

    starts: list[float | None] = []
    prev = 0.0
    for rel in relative:
        if rel is None:
            starts.append(None)
            continue
        start = float(rel)
        if starts and prev is not None and start < prev:
            start = prev
        # Near-duplicate stamps: keep a tiny forward step so turns stay distinct.
        if starts and prev is not None and abs(start - prev) < 0.05:
            start = prev + 0.05
        starts.append(start)
        prev = start

    result: list[dict] = []
    for i, start in enumerate(starts):
        if start is None:
            result.append({"start": None, "end": None})
            continue
        end: float | None = None
        for later in starts[i + 1 :]:
            if later is not None and later > start:
                end = float(later)
                break
        if end is None:
            content_len = len(str(messages[i].get("content") or "").strip())
            end = start + max(1.2, min(10.0, content_len / 14.0 if content_len else 1.5))
        if end <= start:
            end = start + 0.2
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
