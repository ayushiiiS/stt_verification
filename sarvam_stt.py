"""Sarvam speech-to-text helpers."""

from __future__ import annotations

import load_env  # noqa: F401

import json
import os
import tempfile
import time
import threading
from pathlib import Path

import requests

SARVAM_MODEL = os.environ.get("SARVAM_MODEL", "saaras:v3")
SARVAM_MODE = os.environ.get("SARVAM_STT_MODE", "codemix")
SARVAM_REQUEST_INTERVAL_SEC = float(os.environ.get("SARVAM_REQUEST_INTERVAL_SEC", "1"))


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_at
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_at = time.monotonic()


_request_limiter = RateLimiter(SARVAM_REQUEST_INTERVAL_SEC)


def _map_speaker_role(speaker_id: str, first_speaker_id: str) -> str:
    if speaker_id == first_speaker_id:
        return "assistant"
    return "user"


def parse_sarvam_payload(payload: dict) -> list[dict]:
    diarized = payload.get("diarized_transcript") or {}
    entries = diarized.get("entries") or []
    if entries:
        first_speaker = str(entries[0].get("speaker_id", "0"))
        segments: list[dict] = []
        for entry in entries:
            content = str(entry.get("transcript", "")).strip()
            if not content:
                continue
            speaker_id = str(entry.get("speaker_id", "0"))
            segments.append(
                {
                    "role": _map_speaker_role(speaker_id, first_speaker),
                    "content": content,
                }
            )
        return segments

    transcript = str(payload.get("transcript", "")).strip()
    if transcript:
        return [{"role": "assistant", "content": transcript}]
    return []


def transcribe_audio_file(audio_path: Path) -> tuple[list[dict], dict]:
    _request_limiter.wait()

    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY environment variable is not set")

    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=api_key)
    job = client.speech_to_text_job.create_job(
        model=SARVAM_MODEL,
        mode=SARVAM_MODE,
        with_diarization=True,
        num_speakers=2,
    )
    job.upload_files(file_paths=[str(audio_path)])
    job.start()
    job.wait_until_complete(poll_interval=5, timeout=1800)

    if job.is_failed():
        raise RuntimeError("Sarvam transcription job failed")

    with tempfile.TemporaryDirectory() as tmpdir:
        job.download_outputs(output_dir=tmpdir)
        output_files = sorted(Path(tmpdir).glob("*.json"))
        if not output_files:
            raise ValueError("Sarvam job completed but returned no JSON output")
        with output_files[0].open(encoding="utf-8") as handle:
            payload = json.load(handle)

    return parse_sarvam_payload(payload), payload


def transcribe_audio_url(audio_url: str) -> tuple[list[dict], dict]:
    if not audio_url:
        raise ValueError("Missing audio URL")

    response = requests.get(audio_url, timeout=120)
    response.raise_for_status()

    suffix = ".ogg"
    content_type = response.headers.get("content-type", "")
    if "audio/mpeg" in content_type:
        suffix = ".mp3"
    elif "audio/wav" in content_type:
        suffix = ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(response.content)
        tmp_path = Path(tmp.name)

    try:
        return transcribe_audio_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
