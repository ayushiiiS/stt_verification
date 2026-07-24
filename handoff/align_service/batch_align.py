#!/usr/bin/env python3
"""Batch per-speaker forced alignment for Muthoot calls.

For every call it:
  1. Downloads human.wav (user turns) and agent.wav (assistant turns) straight
     from GCS using a service-account key (no expiring signed URLs).
  2. Runs MMS_FA forced alignment on each track against only that speaker's
     turns, so overlapping speech across tracks never competes.
  3. Merges the per-turn timings back together (keyed by the transcript turn
     index ``n``) and writes them to an output JSON, saved incrementally so the
     run is resumable.

Usage:
  ./align_service/.venv/bin/python align_service/batch_align.py \
      --creds service-account.json \
      --transcripts all_data/Muthoot_final.json \
      --csv all_data/Muthoot_human_agent_public_urls.csv \
      --out all_data/Muthoot_aligned_timings.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import soundfile as sf

from forced_align import Turn, assign_forced_alignment_segments

BUCKET = "cadence-audio"
# Deterministic object layout, used as a fallback if the CSV URL is missing.
OBJ_TMPL = "voice-isolation/muthoot/{id}/{track}.wav"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Bytes -> mono float32 PCM. libsndfile first, torchaudio fallback."""
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


def object_path_from_url(url: str | None, call_id: str, track: str) -> str:
    """Parse the GCS object path from a stored URL; fall back to the template."""
    if url:
        path = urlparse(url).path.lstrip("/")
        # Path is "/<bucket>/<object...>"; drop the leading bucket segment.
        parts = path.split("/", 1)
        if len(parts) == 2 and parts[0] == BUCKET:
            return unquote(parts[1])
    return OBJ_TMPL.format(id=call_id, track=track)


def load_csv(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cid = (row.get("id") or "").strip()
            if cid:
                rows[cid] = {"human": row.get("human") or "", "agent": row.get("agent") or ""}
    return rows


def split_turns(messages: list[dict]) -> tuple[list[Turn], list[Turn]]:
    """Return (user_turns, assistant_turns) keyed by the transcript index n."""
    user: list[Turn] = []
    assistant: list[Turn] = []
    for m in messages:
        if m.get("type") != "message":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        n = m.get("n")
        if n is None:
            continue
        turn = Turn(turn=int(n), reference=content)
        if m.get("role") == "assistant":
            assistant.append(turn)
        elif m.get("role") == "user":
            user.append(turn)
    return user, assistant


def align_track(blob, turns: list[Turn], pad_s: float) -> dict[int, dict]:
    """Download + align a single track. Returns {turn_index: {start_s, end_s}}."""
    if not turns:
        return {}
    data = blob.download_as_bytes()
    pcm, sr = decode_audio(data)
    aligned = assign_forced_alignment_segments(pcm, sr, turns, pad_s=pad_s)
    return {
        t.turn: {"start_s": t.start_s, "end_s": t.end_s}
        for t in aligned
        if t.start_s is not None and t.end_s is not None
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--creds", required=True, help="Path to GCS service-account JSON")
    ap.add_argument("--transcripts", required=True, help="Muthoot_final.json")
    ap.add_argument("--csv", required=True, help="CSV with id,human,agent columns")
    ap.add_argument("--out", required=True, help="Output timings JSON")
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--pad-s", type=float, default=0.15)
    ap.add_argument("--limit", type=int, default=0, help="Only process N calls (0 = all)")
    ap.add_argument("--ids", default="", help="Comma-separated call ids to process")
    ap.add_argument("--save-every", type=int, default=1, help="Flush output every N calls")
    args = ap.parse_args(argv)

    from google.cloud import storage
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(args.creds)
    client = storage.Client(credentials=creds, project=creds.project_id)
    bucket = client.bucket(args.bucket)

    transcripts: dict = json.loads(Path(args.transcripts).read_text(encoding="utf-8"))
    csv_rows = load_csv(Path(args.csv))

    out_path = Path(args.out)
    results: dict = {}
    if out_path.exists():
        try:
            results = json.loads(out_path.read_text(encoding="utf-8"))
            log(f"Resuming: {len(results)} call(s) already in {out_path.name}")
        except Exception:
            results = {}

    # Build the work list: calls that have both a transcript and audio URLs.
    if args.ids.strip():
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    else:
        ids = [cid for cid in csv_rows if cid in transcripts]
    if args.limit:
        ids = ids[: args.limit]

    todo = [cid for cid in ids if cid not in results]
    log(f"{len(todo)} call(s) to process ({len(ids) - len(todo)} already done).")

    ok = fail = 0
    for i, cid in enumerate(todo, 1):
        entry = transcripts.get(cid) or {}
        messages = entry.get("messages") or []
        user_turns, asst_turns = split_turns(messages)
        row = csv_rows.get(cid, {})
        try:
            human_blob = bucket.blob(object_path_from_url(row.get("human"), cid, "human"))
            agent_blob = bucket.blob(object_path_from_url(row.get("agent"), cid, "agent"))

            merged: dict[int, dict] = {}
            merged.update(align_track(human_blob, user_turns, args.pad_s))
            merged.update(align_track(agent_blob, asst_turns, args.pad_s))

            # Store sorted by start time for convenience downstream.
            ordered = sorted(
                (
                    {"turn": n, "start_s": v["start_s"], "end_s": v["end_s"]}
                    for n, v in merged.items()
                ),
                key=lambda x: (x["start_s"] if x["start_s"] is not None else 0.0),
            )
            results[cid] = ordered
            ok += 1
            log(f"[{i}/{len(todo)}] {cid}: aligned {len(ordered)} turn(s)")
        except Exception as e:  # noqa: BLE001
            fail += 1
            results.setdefault("_errors", {})[cid] = f"{type(e).__name__}: {e}"
            log(f"[{i}/{len(todo)}] {cid}: FAILED {type(e).__name__}: {e}")

        if i % args.save_every == 0 or i == len(todo):
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Done. ok={ok} fail={fail} total_saved={len([k for k in results if not k.startswith('_')])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
