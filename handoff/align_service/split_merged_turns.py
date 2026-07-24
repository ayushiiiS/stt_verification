#!/usr/bin/env python3
"""Detect & split transcript turns that merge several utterances into one box.

Diarization sometimes glues distinct utterances into a single turn (e.g.
"Hello. जी. Six hundred." spoken across three moments while the agent talked in
between). This is a *transcript* problem, not a timing one, so we only split
when the audio unambiguously agrees:

  A turn is split iff its text breaks into N>=2 sentence segments AND the
  speaker's own track shows exactly N speech clusters (islands separated by a
  real pause) inside the turn's alignment window.

When the sentence count and the acoustic cluster count disagree we DO NOT
guess -- the turn is left untouched (and reported), so we never invent a bad
split. Timing is regenerated afterwards by the normal FA + tighten pipeline.

Usage (dry-run):
  ./align_service/.venv/bin/python align_service/split_merged_turns.py \
      --input "all_data/indiamart (3).json" \
      --timings all_data/indiamart_aligned_timings.before_tighten.json

Add --apply to write the splits back into --input, and --changed <path> to
record the ids of calls that changed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from batch_align_spinny import (
    AGENT_FIELDS,
    HUMAN_FIELDS,
    call_id,
    clean_text,
    decode_audio,
    track_url,
)
from tighten_starts import download, silero_runs

SENT_SPLIT = re.compile(r"(?<=[।.?!])\s+")


def sentence_segments(text: str) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    parts = [p.strip() for p in SENT_SPLIT.split(text)]
    return [p for p in parts if re.search(r"\w", p)]


def cluster_islands(
    islands: list[tuple[float, float]],
    lo: float,
    hi: float,
    *,
    gap_s: float = 1.5,
) -> list[tuple[float, float]]:
    inside = [(s, e) for s, e in islands if e > lo and s < hi]
    if not inside:
        return []
    clusters = [list(inside[0])]
    for s, e in inside[1:]:
        if s - clusters[-1][1] <= gap_s:
            clusters[-1][1] = e
        else:
            clusters.append([s, e])
    return [(s, e) for s, e in clusters]


def visible_indices(messages: list[dict]) -> list[int]:
    """Raw-message indices of visible (user/assistant) turns, in order."""
    out = []
    idx = 0
    for pos, m in enumerate(messages):
        if m.get("type") != "message":
            continue
        role = m.get("role")
        if role in ("user", "assistant") and clean_text(m.get("content")):
            out.append(pos)
        idx += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--timings", required=True, help="before_tighten FA windows (for turn windows)")
    ap.add_argument("--apply", action="store_true", help="write splits back into --input")
    ap.add_argument("--changed", default="", help="write JSON list of changed call ids")
    ap.add_argument("--gap-s", type=float, default=1.5)
    ap.add_argument("--ids", default="")
    args = ap.parse_args(argv)

    calls = json.loads(Path(args.input).read_text(encoding="utf-8"))
    timings = json.loads(Path(args.timings).read_text(encoding="utf-8"))
    want = {x.strip() for x in args.ids.split(",") if x.strip()}

    changed_ids: list[str] = []
    total_candidates = 0
    total_split = 0
    examples: list[str] = []

    for entry in calls:
        cid = call_id(entry)
        if not cid or cid.startswith("_"):
            continue
        if want and cid not in want:
            continue
        rows = {int(r["turn"]): r for r in timings.get(cid, []) if "turn" in r}
        if not rows:
            continue
        messages = entry.get("messages") or []
        vis_pos = visible_indices(messages)

        # Pre-scan: any visible USER turn with multiple sentence segments?
        # Only user turns can be diarization-merged; assistant turns come from
        # the bot's own message log and are authoritative single utterances.
        multi = []
        for turn_idx, pos in enumerate(vis_pos):
            if messages[pos].get("role") != "user":
                continue
            segs = sentence_segments(messages[pos].get("content"))
            if len(segs) >= 2:
                multi.append((turn_idx, pos, segs))
        if not multi:
            continue

        # Only download audio if there is at least one multi-sentence turn.
        human_runs = agent_runs = None
        try:
            hu = track_url(entry, HUMAN_FIELDS)
            au = track_url(entry, AGENT_FIELDS)
        except Exception:
            continue

        plan: list[tuple[int, list[str], list[tuple[float, float]]]] = []
        for turn_idx, pos, segs in multi:
            total_candidates += 1
            role = messages[pos].get("role")
            row = rows.get(turn_idx)
            if not row or row.get("start_s") is None:
                continue
            fs, fe = float(row["start_s"]), float(row["end_s"])
            if role == "user":
                if human_runs is None and hu:
                    pcm, sr = decode_audio(download(hu))
                    human_runs = silero_runs(pcm, sr)
                runs = human_runs or []
            else:
                if agent_runs is None and au:
                    pcm, sr = decode_audio(download(au))
                    agent_runs = silero_runs(pcm, sr)
                runs = agent_runs or []
            clusters = cluster_islands(runs, fs - 1.6, fe + 0.6, gap_s=args.gap_s)
            if len(clusters) == len(segs) and len(segs) >= 2:
                plan.append((pos, segs, clusters))
                if len(examples) < 12:
                    examples.append(
                        f"  {cid} turn{turn_idx}: {len(segs)}x -> "
                        + " | ".join(s[:22] for s in segs)
                    )

        if not plan:
            continue

        # Apply splits to raw messages, from last position to first so indices
        # stay valid.
        for pos, segs, _clusters in sorted(plan, key=lambda p: p[0], reverse=True):
            orig = messages[pos]
            new_msgs = []
            for j, seg in enumerate(segs):
                d = dict(orig)
                if j > 0:
                    d["_id"] = f"{orig.get('_id','item')}_msplit_{j+1}"
                d["content"] = seg
                new_msgs.append(d)
            messages[pos : pos + 1] = new_msgs
            total_split += 1
        entry["messages"] = messages
        changed_ids.append(cid)

    print(f"multi-sentence candidate turns: {total_candidates}")
    print(f"turns auto-split (audio agrees on count): {total_split}")
    print(f"calls changed: {len(changed_ids)}")
    print("examples:")
    print("\n".join(examples) if examples else "  (none)")

    if args.apply and changed_ids:
        Path(args.input).write_text(
            json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nAPPLIED splits to {args.input}")
    if args.changed:
        Path(args.changed).write_text(
            json.dumps(changed_ids, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
