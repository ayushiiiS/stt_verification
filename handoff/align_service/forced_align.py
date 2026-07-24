from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Turn:
    turn: int
    reference: str
    start_s: float | None = None
    end_s: float | None = None


_CACHE: dict = {}


def _load_mms_fa() -> dict:
    """Lazily load + cache the torchaudio MMS_FA stack (heavy, ~1GB model)."""
    if _CACHE:
        return _CACHE
    import os

    import torch
    import torchaudio
    import uroman as uroman_mod

    # Device selection: prefer CUDA, then Apple Metal (MPS), else CPU.
    # ALIGN_DEVICE env var can force a specific device (e.g. "cpu").
    forced = os.getenv("ALIGN_DEVICE")
    if forced:
        device = torch.device(forced)
    elif torch.cuda.is_available():
        device = torch.device("cuda")
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model()
    model.eval()
    model.to(device)
    dictionary = bundle.get_dict()
    _CACHE.update(
        torch=torch,
        model=model,
        device=device,
        tokenizer=bundle.get_tokenizer(),
        aligner=bundle.get_aligner(),
        allowed={c for c in dictionary if c.isalpha()},
        star_id=dictionary.get("*"),
        uroman=uroman_mod.Uroman(),
    )
    return _CACHE


def _resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return pcm
    n = max(1, int(len(pcm) / src * dst))
    x_old = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, pcm).astype(np.float32)


def _normalize(word: str, uroman, allowed: set[str]) -> str:
    romanized = uroman.romanize_string(word).lower()
    return "".join(ch for ch in romanized if ch in allowed)


def _emit_chunked(model, waveform, device, *, chunk_s: int = 20):
    """Run the model forward over the waveform in fixed time-chunks.

    Computing emissions over a whole multi-minute call at once OOMs low-memory
    GPUs (e.g. M1 8GB) and needs a lot of RAM on CPU. Processing 16 kHz audio in
    contiguous, non-overlapping chunks keeps peak memory bounded, and because
    the chunks are contiguous the concatenated emission frames map cleanly back
    to time. Returns a CPU tensor of shape [1, num_frames, num_labels].
    """
    import torch

    rate = 16_000
    chunk = chunk_s * rate
    # The conv feature extractor needs a minimum number of samples to produce a
    # valid frame; a tiny trailing chunk otherwise errors. Pad short segments.
    min_len = 4000
    total = waveform.size(1)
    parts = []
    with torch.inference_mode():
        for start in range(0, total, chunk):
            seg = waveform[:, start : start + chunk]
            if seg.size(1) < min_len:
                pad = min_len - seg.size(1)
                seg = torch.nn.functional.pad(seg, (0, pad))
            em, _ = model(seg)
            parts.append(em.cpu())
            # Free the GPU/MPS scratch between chunks.
            if device.type == "mps":
                torch.mps.empty_cache()
    return torch.cat(parts, dim=1)


def assign_forced_alignment_segments(
    pcm: np.ndarray,
    sample_rate: int,
    turns: list[Turn],
    *,
    pad_s: float = 0.15,
) -> list[Turn]:
    if not turns:
        return turns
    d = _load_mms_fa()
    torch, model, tokenizer, aligner = d["torch"], d["model"], d["tokenizer"], d["aligner"]
    uroman, allowed = d["uroman"], d["allowed"]
    device = d["device"]
    star_id = d.get("star_id")

    rate = 16_000
    pcm16 = _resample(pcm, sample_rate, rate) if sample_rate != rate else pcm
    waveform = torch.from_numpy(np.ascontiguousarray(pcm16)).float().unsqueeze(0).to(device)

    words: list[str] = []
    word_turn: list[int] = []
    for t in turns:
        for raw in (t.reference or "").split():
            norm = _normalize(raw, uroman, allowed)
            if norm:
                words.append(norm)
                word_turn.append(t.turn)
    if not words:
        raise RuntimeError("No alignable words after normalization")

    emission = _emit_chunked(model, waveform, device)
    # The <star> token (matches any frame) lets the aligner absorb silence and
    # non-transcript audio between utterances, instead of smearing a short word
    # across the surrounding silence. It only affects timing, never the words.
    import os as _os

    use_star = star_id is not None and _os.getenv("ALIGN_STAR", "0") == "1"
    with torch.inference_mode():
        if use_star:
            word_tokens = tokenizer(words)
            augmented: list[list[int]] = [[star_id]]
            owners: list[int | None] = [None]
            for wi, toks in enumerate(word_tokens):
                augmented.append(toks)
                owners.append(wi)
                augmented.append([star_id])
                owners.append(None)
            raw_spans = aligner(emission[0], augmented)
            token_spans = [sp for owner, sp in zip(owners, raw_spans) if owner is not None]
        else:
            token_spans = aligner(emission[0], tokenizer(words))

    ratio = waveform.size(1) / emission.size(1)
    bounds: dict[int, list[float]] = {}
    for spans, tn in zip(token_spans, word_turn):
        s = ratio * spans[0].start / rate
        e = ratio * spans[-1].end / rate
        if tn not in bounds:
            bounds[tn] = [s, e]
        else:
            bounds[tn][0] = min(bounds[tn][0], s)
            bounds[tn][1] = max(bounds[tn][1], e)

    duration_s = len(pcm) / sample_rate
    out: list[Turn] = []
    for t in turns:
        if t.turn in bounds:
            s, e = bounds[t.turn]
            s = max(0.0, s - pad_s)
            e = min(duration_s, e + pad_s)
        else:
            s, e = t.start_s, t.end_s
        out.append(Turn(turn=t.turn, reference=t.reference, start_s=s, end_s=e))
    return out
