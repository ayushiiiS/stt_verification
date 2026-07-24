#!/usr/bin/env python3
"""Batch per-speaker forced alignment for spinny-karan calls.

spinny-karan.json is a list of calls, each carrying live signed URLs for a
human-only track and an agent-only track. For every call we:
  1. Download human.ogg (user turns) and agent.ogg (assistant turns) from the
     signed URLs (no GCS credentials needed while the URLs are valid).
  2. Run MMS_FA forced alignment on each track against only that speaker's
     turns, so overlapping speech across tracks never competes.
  3. Merge the per-turn timings (keyed by the message-order turn index) and
     write them to an output JSON, saved incrementally so the run is resumable.

Usage:
  ./align_service/.venv/bin/python align_service/batch_align_spinny.py \
      --input all_data/spinny-karan.json \
      --out all_data/spinny_aligned_timings.json
"""

from __future__ import annotations

# Optional heavy deps (torch/numpy) live in align_service/.venv — see requirements.txt.
# pyright: reportMissingImports=false

import argparse
import io
import json
import re
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from forced_align import Turn, assign_forced_alignment_segments

TAG_RE = re.compile(r"<[^>]+>")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean_text(text: str | None) -> str:
    """Strip SSML/HTML tags (e.g. <break time="1.0s" />) and collapse spaces."""
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", text or "")).strip()


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    try:
        pcm, sr = sf.read(io.BytesIO(data), dtype="float32")
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        return np.ascontiguousarray(pcm), int(sr)
    except Exception:
        import torchaudio

        waveform, sr = torchaudio.load(io.BytesIO(data))
        pcm = waveform.mean(dim=0).numpy().astype(np.float32)
        return np.ascontiguousarray(pcm), int(sr)


def call_id(entry: dict) -> str | None:
    cid = entry.get("callLogId")
    if isinstance(cid, dict):
        return cid.get("$oid")
    return cid if isinstance(cid, str) else None


# Per-speaker URL field names differ across datasets (spinny vs indiamart).
HUMAN_FIELDS = ("humanRecordingUrl", "human")
AGENT_FIELDS = ("agentRecordingUrl", "agent")
RECORDING_FIELDS = ("recordingUrl", "recording_url", "public_url")


def track_url(entry: dict, fields: tuple[str, ...]) -> str | None:
    for f in fields:
        v = entry.get(f)
        if isinstance(v, str) and v.strip():
            return v
    return None


def split_turns(messages: list[dict]) -> tuple[list[Turn], list[Turn]]:
    """Index turns by their order among type=='message' rows.

    Returns (user_turns, assistant_turns). The turn index is the position in
    the filtered message stream so it lines up 1:1 with the transcript order.
    """
    user: list[Turn] = []
    assistant: list[Turn] = []
    idx = 0
    for m in messages:
        if m.get("type") != "message":
            continue
        content = clean_text(m.get("content"))
        role = m.get("role")
        if content and role in ("assistant", "user"):
            turn = Turn(turn=idx, reference=content)
            (assistant if role == "assistant" else user).append(turn)
        idx += 1
    return user, assistant


def align_track(
    url: str | None,
    turns: list[Turn],
    pad_s: float,
    *,
    fallback_url: str | None = None,
    label: str = "track",
) -> dict[int, dict]:
    """Align one speaker track. Tries ``url`` first, then ``fallback_url`` (mixed recording)."""
    if not turns:
        return {}
    for attempt, source in ((url, "diarized"), (fallback_url, "recording")):
        if not attempt:
            continue
        try:
            resp = httpx.get(attempt, timeout=120.0, follow_redirects=True)
            if resp.status_code == 404:
                log(f"  {label}: {source} audio missing (404), trying fallback")
                continue
            resp.raise_for_status()
            pcm, sr = decode_audio(resp.content)
            aligned = assign_forced_alignment_segments(pcm, sr, turns, pad_s=pad_s)
            timings = {
                t.turn: {"start_s": t.start_s, "end_s": t.end_s}
                for t in aligned
                if t.start_s is not None and t.end_s is not None
            }
            if timings and source == "recording":
                log(f"  {label}: aligned {len(timings)} turn(s) via mixed recording fallback")
            return timings
        except Exception as exc:  # noqa: BLE001
            log(f"  {label}: {source} failed ({type(exc).__name__}: {exc})")
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="spinny-karan.json")
    ap.add_argument("--out", required=True, help="Output timings JSON")
    ap.add_argument("--pad-s", type=float, default=0.15)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--save-every", type=int, default=1)
    ap.add_argument("--num-shards", type=int, default=1, help="Split work into N shards")
    ap.add_argument("--shard-index", type=int, default=0, help="Which shard this worker runs (0-based)")
    ap.add_argument("--skip", default="", help="JSON file whose top-level call ids to skip")
    args = ap.parse_args(argv)

    # Use all available cores for the CPU model forward (default is often half).
    try:
        import os

        import torch

        torch.set_num_threads(os.cpu_count() or 4)
        log(f"torch threads: {torch.get_num_threads()}")
    except Exception:
        pass

    calls: list[dict] = json.loads(Path(args.input).read_text(encoding="utf-8"))
    by_id = {cid: e for e in calls if (cid := call_id(e))}

    out_path = Path(args.out)
    results: dict = {}
    if out_path.exists():
        try:
            results = json.loads(out_path.read_text(encoding="utf-8"))
            log(f"Resuming: {len(results)} call(s) already in {out_path.name}")
        except Exception:
            results = {}

    if args.ids.strip():
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    else:
        ids = list(by_id.keys())

    # Deterministic sharding: worker i handles every Nth call.
    if args.num_shards > 1:
        ids = [cid for pos, cid in enumerate(ids) if pos % args.num_shards == args.shard_index]
        log(f"Shard {args.shard_index}/{args.num_shards}: {len(ids)} call(s) in this shard.")

    if args.limit:
        ids = ids[: args.limit]

    # Ids already completed elsewhere (e.g. the merged main output) to skip.
    skip: set[str] = set()
    if args.skip and Path(args.skip).exists():
        try:
            done = json.loads(Path(args.skip).read_text(encoding="utf-8"))
            skip = {k for k in done if not k.startswith("_")}
        except Exception:
            skip = set()

    todo = [cid for cid in ids if cid not in results and cid not in skip]
    log(f"{len(todo)} call(s) to process ({len(ids) - len(todo)} already done).")

    ok = fail = 0
    for i, cid in enumerate(todo, 1):
        entry = by_id.get(cid, {})
        user_turns, asst_turns = split_turns(entry.get("messages") or [])
        rec_url = track_url(entry, RECORDING_FIELDS)
        merged: dict[int, dict] = {}
        merged.update(
            align_track(
                track_url(entry, HUMAN_FIELDS),
                user_turns,
                args.pad_s,
                fallback_url=rec_url,
                label="human",
            )
        )
        merged.update(
            align_track(
                track_url(entry, AGENT_FIELDS),
                asst_turns,
                args.pad_s,
                fallback_url=rec_url,
                label="agent",
            )
        )

        expected = len(user_turns) + len(asst_turns)
        if merged:
            ordered = sorted(
                (
                    {"turn": n, "start_s": v["start_s"], "end_s": v["end_s"]}
                    for n, v in merged.items()
                ),
                key=lambda x: (x["start_s"] if x["start_s"] is not None else 0.0),
            )
            results[cid] = ordered
            results.get("_errors", {}).pop(cid, None)
            if not results.get("_errors"):
                results.pop("_errors", None)
            ok += 1
            note = ""
            if len(ordered) < expected:
                note = f" (partial: {len(ordered)}/{expected} turns)"
            log(f"[{i}/{len(todo)}] {cid}: aligned {len(ordered)} turn(s){note}")
        else:
            fail += 1
            msg = "no timings from diarized or recording audio"
            results.setdefault("_errors", {})[cid] = msg
            log(f"[{i}/{len(todo)}] {cid}: FAILED {msg}")

        if i % args.save_every == 0 or i == len(todo):
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Done. ok={ok} fail={fail} total_saved={len([k for k in results if not k.startswith('_')])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
