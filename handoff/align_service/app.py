from __future__ import annotations

import io
import os
import threading

import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from forced_align import Turn, assign_forced_alignment_segments, _load_mms_fa

app = FastAPI(title="MMS_FA Alignment Service")

# Serialize model access — one heavy CPU forward at a time per worker.
_LOCK = threading.Lock()
# Optional shared-secret auth for internal use. Set ALIGN_API_KEY in the env.
_API_KEY = os.getenv("ALIGN_API_KEY")


class TurnIn(BaseModel):
    turn: int
    reference: str


class AlignRequest(BaseModel):
    audio_url: str
    turns: list[TurnIn]
    pad_s: float = 0.15


class TurnOut(BaseModel):
    turn: int
    reference: str
    start_s: float | None
    end_s: float | None


class AlignResponse(BaseModel):
    turns: list[TurnOut]


@app.on_event("startup")
def _warm() -> None:
    # Load the model once so the first request isn't slow.
    _load_mms_fa()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": bool(_load_mms_fa())}


def _load_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Decode audio bytes to mono float32 PCM.

    Tries libsndfile (soundfile) first; falls back to torchaudio (ffmpeg
    backend) which handles mp3/m4a that libsndfile builds often can't.
    """
    try:
        pcm, sr = sf.read(io.BytesIO(data), dtype="float32")
        if pcm.ndim > 1:  # stereo -> mono
            pcm = pcm.mean(axis=1)
        return np.ascontiguousarray(pcm), sr
    except Exception:
        import torchaudio

        waveform, sr = torchaudio.load(io.BytesIO(data))
        pcm = waveform.mean(dim=0).numpy().astype(np.float32)
        return np.ascontiguousarray(pcm), int(sr)


@app.post("/align", response_model=AlignResponse)
def align(req: AlignRequest, authorization: str | None = Header(default=None)) -> AlignResponse:
    if _API_KEY and authorization != f"Bearer {_API_KEY}":
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        resp = httpx.get(req.audio_url, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        pcm, sr = _load_audio(resp.content)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"could not fetch/decode audio: {e}")

    turns = [Turn(turn=t.turn, reference=t.reference) for t in req.turns]
    try:
        with _LOCK:
            aligned = assign_forced_alignment_segments(pcm, sr, turns, pad_s=req.pad_s)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"alignment failed: {e}")

    return AlignResponse(
        turns=[
            TurnOut(turn=t.turn, reference=t.reference, start_s=t.start_s, end_s=t.end_s)
            for t in aligned
        ]
    )
