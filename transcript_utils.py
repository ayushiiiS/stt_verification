"""Shared transcript filtering and comparison helpers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

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


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_match_text(text: str) -> str:
    text = str(text or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _text_similarity(left: str, right: str) -> float:
    a = _normalize_match_text(left)
    b = _normalize_match_text(right)
    if not a or not b:
        return 0.0
    greetings = {"hello", "hi", "हेलो", "हैलो", "haan", "हाँ", "हां", "han"}
    a_greet = a.strip("?.!, ")
    b_greet = b.strip("?.!, ")
    if a_greet in greetings and b_greet in greetings:
        return 0.96
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        return 0.88 + 0.12 * (shorter / longer)
    return SequenceMatcher(None, a, b).ratio()


def _segment_start(seg: dict) -> float | None:
    return _as_float(
        seg.get("start_s")
        if seg.get("start_s") is not None
        else seg.get("start_time_seconds")
        if seg.get("start_time_seconds") is not None
        else seg.get("start")
    )


def _segment_end(seg: dict) -> float | None:
    return _as_float(
        seg.get("end_s")
        if seg.get("end_s") is not None
        else seg.get("end_time_seconds")
        if seg.get("end_time_seconds") is not None
        else seg.get("end")
    )


def _segment_content(seg: dict) -> str:
    return str(seg.get("content") or seg.get("transcript") or "").strip()


def match_segments_to_turns(
    original_messages: list[dict],
    segments: list[dict],
    *,
    lookahead: int = 12,
    min_score: float = 0.3,
) -> list[list[dict]]:
    """Match each Original turn to STT segments by text similarity + time order."""
    if not original_messages or not segments:
        return [[] for _ in original_messages]

    sorted_segs = sorted(
        segments,
        key=lambda seg: (_segment_start(seg) or 1e18, _segment_end(seg) or 1e18),
    )

    assignments: list[list[dict]] = []
    used: set[int] = set()
    last_anchor = -1.0

    for orig in original_messages:
        content = str(orig.get("content") or "").strip()
        role = str(orig.get("role") or "")
        matched: list[dict] = []

        if not content:
            assignments.append(matched)
            continue

        best_i: int | None = None
        word_count = len(content.split())
        threshold = 0.22 if word_count <= 2 else min_score
        candidates: list[tuple[float, float, int]] = []

        for i, seg in enumerate(sorted_segs):
            if i in used:
                continue
            seg_role = str(seg.get("role") or "")
            if (
                role in {"assistant", "user"}
                and seg_role in {"assistant", "user"}
                and seg_role != role
            ):
                continue
            seg_text = _segment_content(seg)
            score = _text_similarity(content, seg_text)
            seg_words = max(1, len(seg_text.split()))
            if word_count > 3 and seg_words < word_count * 0.2:
                score *= 0.45
            if score < threshold:
                continue
            start = _segment_start(seg) or 0.0
            candidates.append((score, start, i))

        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            best_score = candidates[0][0]
            top = [item for item in candidates if item[0] >= best_score - 0.04]
            if last_anchor >= 0:
                lookback = 18.0 if word_count <= 3 else 0.5
                ordered = [item for item in top if item[1] >= last_anchor - lookback]
                pick_from = ordered or top
            else:
                pick_from = top
            pick_from.sort(key=lambda item: (item[1], -item[0]))
            best_i = pick_from[0][2]
        else:
            for i, seg in enumerate(sorted_segs):
                if i in used:
                    continue
                seg_role = str(seg.get("role") or "")
                if (
                    role in {"assistant", "user"}
                    and seg_role in {"assistant", "user"}
                    and seg_role != role
                ):
                    continue
                if last_anchor >= 0 and (_segment_start(seg) or 0) < last_anchor - 18.0:
                    continue
                best_i = i
                break

        if best_i is not None:
            used.add(best_i)
            matched.append(sorted_segs[best_i])
            seg_start = _segment_start(sorted_segs[best_i])
            seg_end = _segment_end(sorted_segs[best_i])
            if seg_end is not None:
                last_anchor = seg_end
            elif seg_start is not None:
                last_anchor = seg_start
            combined = _segment_content(sorted_segs[best_i])
            orig_words = len(content.split())

            while len(matched) < 4 and len(combined.split()) < orig_words * 0.55:
                nxt_i = best_i + len(matched)
                if nxt_i >= len(sorted_segs) or nxt_i in used:
                    break
                seg = sorted_segs[nxt_i]
                seg_role = str(seg.get("role") or "")
                if (
                    role in {"assistant", "user"}
                    and seg_role in {"assistant", "user"}
                    and seg_role != role
                ):
                    break
                prev_end = _segment_end(matched[-1])
                nxt_start = _segment_start(seg)
                if (
                    prev_end is not None
                    and nxt_start is not None
                    and nxt_start - prev_end > 2.5
                ):
                    break
                used.add(nxt_i)
                matched.append(seg)
                combined = f"{combined} {_segment_content(seg)}".strip()
                end = _segment_end(seg)
                if end is not None:
                    last_anchor = max(last_anchor, end)

        assignments.append(matched)

    return assignments


def timings_from_matched_segments(turn_segments: list[list[dict]]) -> list[dict]:
    timings: list[dict] = []
    for segs in turn_segments:
        if not segs:
            timings.append({"start": None, "end": None})
            continue
        starts = [value for seg in segs if (value := _segment_start(seg)) is not None]
        ends = [value for seg in segs if (value := _segment_end(seg)) is not None]
        timings.append(
            {
                "start": min(starts) if starts else None,
                "end": max(ends) if ends else None,
            }
        )
    return timings


def align_stt_segments(
    original_messages: list[dict], segments: list[dict]
) -> list[dict]:
    """Map STT segments onto original turns using text similarity."""
    assignments = match_segments_to_turns(original_messages, segments)
    aligned: list[dict] = []
    for orig, segs in zip(original_messages, assignments):
        entry = {**orig}
        if segs:
            entry["content"] = " ".join(
                text for seg in segs if (text := _segment_content(seg))
            ).strip()
            starts = [value for seg in segs if (value := _segment_start(seg)) is not None]
            ends = [value for seg in segs if (value := _segment_end(seg)) is not None]
            if starts:
                entry["start_s"] = min(starts)
            if ends:
                entry["end_s"] = max(ends)
        else:
            entry["content"] = ""
        aligned.append(entry)
    return aligned


def _align_stt_segments_role_pool(
    original_messages: list[dict], segments: list[dict]
) -> list[dict]:
    """Legacy role-pool alignment (kept for reference)."""
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


def timings_from_stt_messages(messages: list[dict]) -> list[dict]:
    """Turn timings from role-aligned STT messages (merged segment boundaries)."""
    timings: list[dict] = []
    for msg in messages:
        start = _as_float(
            msg.get("start_s")
            if msg.get("start_s") is not None
            else msg.get("start_time_seconds")
            if msg.get("start_time_seconds") is not None
            else msg.get("start")
        )
        end = _as_float(
            msg.get("end_s")
            if msg.get("end_s") is not None
            else msg.get("end_time_seconds")
            if msg.get("end_time_seconds") is not None
            else msg.get("end")
        )
        timings.append({"start": start, "end": end})
    return timings


def timings_from_stt_segments(segments: list[dict], turn_count: int) -> list[dict]:
    """Map STT segment start_s/end_s onto turn slots by index (fallback only).

    Prefer timings_from_stt_messages when aligned STT messages exist — raw
    segment count often differs from turn count after role-aware merging.
    """
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


def clean_saved_timings(raw_timings: list | None, turn_count: int) -> list[dict]:
    """Normalize per-turn timings saved from the review UI."""
    timings: list[dict] = []
    items = raw_timings if isinstance(raw_timings, list) else []
    for i in range(turn_count):
        entry = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        start = _as_float(entry.get("start"))
        end = _as_float(entry.get("end"))
        if start is not None:
            start = round(start, 3)
        if end is not None:
            end = round(end, 3)
        timings.append({"start": start, "end": end})
    return timings
