#!/usr/bin/env python3
"""Tighten FA turn bounds by snapping them to real speaker-track speech.

MMS_FA often stretches start_s backward through preceding silence and/or
end_s forward through following silence (sometimes into the next turn).
This post-process finds the main speech island inside each FA window on the
matching speaker track and snaps both bounds to that island.

Usage:
  ./align_service/.venv/bin/python align_service/tighten_starts.py \
      --input "all_data/indiamart (3).json" \
      --timings all_data/indiamart_aligned_timings.before_tighten.json \
      --out all_data/indiamart_aligned_timings.json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

HUMAN_FIELDS = ("humanRecordingUrl", "human")
AGENT_FIELDS = ("agentRecordingUrl", "agent")
TAG_RE = re.compile(r"<[^>]+>")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def call_id(entry: dict) -> str | None:
    cid = entry.get("callLogId")
    if isinstance(cid, dict):
        return cid.get("$oid")
    return cid if isinstance(cid, str) else None


def track_url(entry: dict, fields: tuple[str, ...]) -> str | None:
    for f in fields:
        v = entry.get(f)
        if isinstance(v, str) and v.strip():
            return v
    return None


def clean_text(text: str) -> str:
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


def _resample_to_16k(pcm: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == 16_000:
        return pcm.astype(np.float32)
    n = max(1, int(len(pcm) / sample_rate * 16_000))
    x_old = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, pcm).astype(np.float32)


_SILERO: dict = {}


def silero_runs(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    threshold: float = 0.5,
    min_speech_ms: int = 80,
    min_silence_ms: int = 80,
) -> list[tuple[float, float]]:
    """Speech islands from the Silero neural VAD (robust to noise vs speech)."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    if "model" not in _SILERO:
        _SILERO["model"] = load_silero_vad()
    model = _SILERO["model"]
    wav = torch.from_numpy(np.ascontiguousarray(_resample_to_16k(pcm, sample_rate))).float()
    ts = get_speech_timestamps(
        wav,
        model,
        threshold=threshold,
        sampling_rate=16_000,
        min_speech_duration_ms=min_speech_ms,
        min_silence_duration_ms=min_silence_ms,
        return_seconds=True,
    )
    return [(float(t["start"]), float(t["end"])) for t in ts]


def speech_db(pcm: np.ndarray, sample_rate: int, hop_s: float = 0.02) -> tuple[np.ndarray, float]:
    hop = max(1, int(sample_rate * hop_s))
    rms = np.array(
        [
            np.sqrt(np.mean(pcm[i : i + hop] ** 2) + 1e-12)
            for i in range(0, len(pcm), hop)
        ],
        dtype=np.float64,
    )
    db = 20.0 * np.log10(rms + 1e-9)
    return db, hop_s


def speech_runs(
    db: np.ndarray,
    hop_s: float,
    *,
    threshold_db: float = -45.0,
    min_run_s: float = 0.08,
    bridge_s: float = 0.12,
) -> list[tuple[float, float]]:
    active = db > threshold_db
    bridge = max(1, int(round(bridge_s / hop_s)))
    i = 0
    while i < len(active):
        if active[i]:
            i += 1
            continue
        j = i
        while j < len(active) and not active[j]:
            j += 1
        if i > 0 and j < len(active) and j - i <= bridge:
            active[i:j] = True
        i = j

    runs: list[tuple[float, float]] = []
    start = None
    for idx, is_on in enumerate(active):
        if is_on and start is None:
            start = idx
        if (not is_on or idx == len(active) - 1) and start is not None:
            end = idx if not is_on else idx + 1
            s = start * hop_s
            e = end * hop_s
            if e - s >= min_run_s:
                runs.append((s, e))
            start = None
    return runs


def merge_groups(
    runs: list[tuple[float, float]], *, max_internal_gap_s: float = 1.25
) -> list[tuple[float, float]]:
    if not runs:
        return []
    groups = [list(runs[0])]
    for s, e in runs[1:]:
        if s - groups[-1][1] <= max_internal_gap_s:
            groups[-1][1] = e
        else:
            groups.append([s, e])
    return [(s, e) for s, e in groups]


def plausible_dur_s(n_words: int, *, per_word_s: float = 0.12, floor_s: float = 0.18) -> float:
    """Rough minimum spoken duration for a turn of ``n_words`` words.

    Used to tell a real (short) utterance apart from a spurious noise blip:
    "It's a winter type like" (5 words) cannot be a 0.3s island, but "हां हां"
    (2 words) plausibly is a 0.5s island.
    """
    return max(floor_s, per_word_s * max(1, n_words))


def assign_track_monotonic(
    turn_items: list[dict],
    runs: list[tuple[float, float]],
    *,
    lead_pad_s: float = 0.35,
    trail_pad_s: float = 0.25,
    min_dur_s: float = 0.12,
    max_internal_gap_s: float = 1.25,
    max_drift_s: float = 4.0,
    back_pad_s: float = 1.6,
    fwd_pad_s: float = 0.6,
) -> dict[int, dict]:
    """Snap a single speaker's turns to speech islands, in order.

    ``turn_items`` are one speaker's turns in transcript order, each with the
    raw FA window (``start_s``/``end_s``) and ``words`` count. ``runs`` are that
    track's speech islands. Islands are consumed left-to-right so a turn can
    never grab audio that belongs to an earlier turn (the root cause of
    neighbour-stealing). Within its FA window (widened to tolerate the
    aligner's drift, especially during cross-speaker overlap) a turn takes the
    *earliest* island long enough to plausibly hold its words; noise blips are
    skipped, and if nothing qualifies we fall back to the longest candidate.
    """
    results: dict[int, dict] = {}
    ri = 0
    n_runs = len(runs)

    for it in turn_items:
        tn = it["turn"]
        fs, fe = it.get("start_s"), it.get("end_s")
        if fs is None or fe is None:
            results[tn] = {"start_s": fs, "end_s": fe, "flag": "no_fa"}
            continue
        fs, fe = float(fs), float(fe)
        fc = 0.5 * (fs + fe)
        need = plausible_dur_s(it.get("words", 1))

        # Search a window widened around the FA span: MMS_FA can place a word
        # up to a couple seconds off when speakers overlap. Looking earlier is
        # safe because earlier turns' islands are already consumed.
        lo, hi = fs - back_pad_s, fe + fwd_pad_s
        avail = runs[ri:]
        overlapping = [(s, e) for s, e in avail if e > lo and s < hi]
        groups = merge_groups(overlapping, max_internal_gap_s=max_internal_gap_s)

        chosen = None
        flag = "ok"
        if groups:
            plausible = [g for g in groups if (g[1] - g[0]) >= need]
            if plausible:
                chosen = plausible[0]  # earliest island that fits the words
            else:
                chosen = max(groups, key=lambda g: g[1] - g[0])  # longest available
                flag = "short_island"
        else:
            # FA window drifted off all remaining speech: snap to the nearest
            # unconsumed island if it is close, else keep the FA window.
            if avail:
                cand = min(avail, key=lambda g: abs(0.5 * (g[0] + g[1]) - fc))
                if abs(0.5 * (cand[0] + cand[1]) - fc) <= max_drift_s:
                    chosen = cand
                    flag = "drift_snap"
            if chosen is None:
                results[tn] = {"start_s": fs, "end_s": fe, "flag": "no_island"}
                continue

        s, e = chosen
        new_start = max(0.0, s - lead_pad_s)
        new_end = e + trail_pad_s
        if new_end - new_start < min_dur_s:
            new_end = new_start + min_dur_s
        results[tn] = {"start_s": float(new_start), "end_s": float(new_end), "flag": flag}

        # Consume every island up to and including the chosen one.
        while ri < n_runs and runs[ri][1] <= e + 1e-6:
            ri += 1

    return results


def visible_turns(messages: list[dict]) -> list[dict]:
    out = []
    for msg in messages:
        if msg.get("type") != "message":
            continue
        role = msg.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = clean_text(msg.get("content") or "")
        if not content:
            continue
        out.append({"role": role, "content": content, "words": len(content.split())})
    return out


def download(url: str) -> bytes:
    resp = httpx.get(url, timeout=120.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="Source calls JSON with human/agent URLs")
    ap.add_argument("--timings", required=True, help="Existing FA timings JSON")
    ap.add_argument("--out", required=True, help="Output timings JSON")
    ap.add_argument("--threshold-db", type=float, default=-45.0)
    ap.add_argument("--lead-pad-s", type=float, default=0.35)
    ap.add_argument("--trail-pad-s", type=float, default=0.05)
    ap.add_argument("--ids", default="", help="Comma-separated call ids (default: all)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", default="", help="Optional path to write a QA report of flagged turns")
    ap.add_argument("--vad", choices=["energy", "silero"], default="energy",
                    help="Speech-island detector: 'energy' (dB threshold) or 'silero' (neural VAD)")
    args = ap.parse_args(argv)

    def detect_runs(pcm: np.ndarray, sr: int) -> list[tuple[float, float]]:
        if args.vad == "silero":
            return silero_runs(pcm, sr)
        db, hop = speech_db(pcm, sr)
        return speech_runs(db, hop, threshold_db=args.threshold_db)

    calls = json.loads(Path(args.input).read_text(encoding="utf-8"))
    timings = json.loads(Path(args.timings).read_text(encoding="utf-8"))
    by_id = {cid: e for e in calls if (cid := call_id(e))}

    if args.ids.strip():
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    else:
        ids = [cid for cid in timings if not str(cid).startswith("_") and cid in by_id]
    if args.limit:
        ids = ids[: args.limit]

    log(f"Tightening start+end for {len(ids)} call(s)")
    moved_turns = 0
    moved_calls = 0
    fail = 0
    report: dict[str, list] = {}

    for i, cid in enumerate(ids, 1):
        entry = by_id[cid]
        turns = visible_turns(entry.get("messages") or [])
        rows = timings.get(cid) or []
        by_turn = {int(r["turn"]): dict(r) for r in rows if "turn" in r}
        try:
            human_url = track_url(entry, HUMAN_FIELDS)
            agent_url = track_url(entry, AGENT_FIELDS)
            human_runs: list[tuple[float, float]] = []
            agent_runs: list[tuple[float, float]] = []
            if human_url and any(t["role"] == "user" for t in turns):
                pcm, sr = decode_audio(download(human_url))
                human_runs = detect_runs(pcm, sr)
            if agent_url and any(t["role"] == "assistant" for t in turns):
                pcm, sr = decode_audio(download(agent_url))
                agent_runs = detect_runs(pcm, sr)

            # Build ordered per-speaker turn lists carrying the raw FA window.
            user_items: list[dict] = []
            asst_items: list[dict] = []
            for idx, turn in enumerate(turns):
                item = by_turn.get(idx)
                if not item or item.get("start_s") is None or item.get("end_s") is None:
                    continue
                payload = {
                    "turn": idx,
                    "start_s": item["start_s"],
                    "end_s": item["end_s"],
                    "words": turn.get("words", 1),
                }
                (user_items if turn["role"] == "user" else asst_items).append(payload)

            assigned = {}
            assigned.update(
                assign_track_monotonic(
                    user_items, human_runs,
                    lead_pad_s=args.lead_pad_s, trail_pad_s=args.trail_pad_s,
                )
            )
            assigned.update(
                assign_track_monotonic(
                    asst_items, agent_runs,
                    lead_pad_s=args.lead_pad_s, trail_pad_s=args.trail_pad_s,
                )
            )

            call_moved = 0
            for idx, res in assigned.items():
                item = by_turn.get(idx)
                if not item:
                    continue
                old_s, old_e = item.get("start_s"), item.get("end_s")
                item["start_s"] = res["start_s"]
                item["end_s"] = res["end_s"]
                by_turn[idx] = item
                if res.get("flag") not in (None, "ok"):
                    report.setdefault(cid, []).append(
                        {
                            "turn": idx,
                            "flag": res["flag"],
                            "text": turns[idx]["content"][:60],
                            "start_s": res["start_s"],
                            "end_s": res["end_s"],
                        }
                    )
                if old_s is None or abs(res["start_s"] - float(old_s)) >= 0.05 or abs(res["end_s"] - float(old_e)) >= 0.05:
                    call_moved += 1

            timings[cid] = sorted(by_turn.values(), key=lambda x: x.get("turn", 0))
            moved_turns += call_moved
            if call_moved:
                moved_calls += 1
            flagged = len(report.get(cid, []))
            log(f"[{i}/{len(ids)}] {cid}: tightened {call_moved} turn(s), flagged {flagged}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(f"[{i}/{len(ids)}] {cid}: FAILED {type(e).__name__}: {e}")

        if i % 5 == 0 or i == len(ids):
            Path(args.out).write_text(
                json.dumps(timings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    Path(args.out).write_text(
        json.dumps(timings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    log(
        f"Done. calls_changed={moved_calls}/{len(ids)} turns_tightened={moved_turns} "
        f"flagged_calls={len(report)} fail={fail}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
