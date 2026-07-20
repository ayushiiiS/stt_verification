"""Sarvam speech-to-text helpers."""

from __future__ import annotations

try:
    import load_env  # noqa: F401
except ImportError:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

SARVAM_MODEL = os.environ.get("SARVAM_MODEL", "saaras:v3")
SARVAM_MODE = os.environ.get("SARVAM_STT_MODE", "codemix")
SARVAM_REQUEST_INTERVAL_SEC = float(os.environ.get("SARVAM_REQUEST_INTERVAL_SEC", "1"))
SARVAM_NUM_SPEAKERS = int(os.environ.get("SARVAM_NUM_SPEAKERS", "2"))
# Who the first diarized speaker_id maps to: "assistant" | "user"
SARVAM_FIRST_SPEAKER_ROLE = os.environ.get(
    "SARVAM_FIRST_SPEAKER_ROLE", "assistant"
).strip().lower()
SARVAM_DOWNLOAD_MAX_BYTES = int(
    os.environ.get("SARVAM_DOWNLOAD_MAX_BYTES", str(200 * 1024 * 1024))
)
SARVAM_DOWNLOAD_RETRIES = int(os.environ.get("SARVAM_DOWNLOAD_RETRIES", "3"))
_RATE_LOCK_PATH = Path(
    os.environ.get(
        "SARVAM_RATE_LOCK_PATH",
        str(Path(tempfile.gettempdir()) / "sarvam_stt_rate.lock"),
    )
)


class RateLimiter:
    """Process-local limiter; optional cross-process lock via lock file."""

    def __init__(self, min_interval: float, lock_path: Path | None = None) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_at = 0.0
        self._lock_path = lock_path

    def wait(self) -> None:
        with self._lock:
            if self._lock_path is not None:
                self._wait_with_file_lock()
            else:
                self._wait_in_memory()

    def _wait_in_memory(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_at = time.monotonic()

    def _wait_with_file_lock(self) -> None:
        assert self._lock_path is not None
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                self._wait_in_memory()
                return

            try:
                handle.seek(0)
                raw = handle.read().strip()
                last_at = float(raw) if raw else 0.0
                now = time.time()
                elapsed = now - last_at
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
                handle.seek(0)
                handle.truncate()
                handle.write(str(time.time()))
                handle.flush()
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass


_request_limiter = RateLimiter(SARVAM_REQUEST_INTERVAL_SEC, _RATE_LOCK_PATH)


def _map_speaker_role(speaker_id: str, first_speaker_id: str) -> str:
    first_role = (
        SARVAM_FIRST_SPEAKER_ROLE
        if SARVAM_FIRST_SPEAKER_ROLE in ("assistant", "user")
        else "assistant"
    )
    other_role = "user" if first_role == "assistant" else "assistant"
    if speaker_id == first_speaker_id:
        return first_role
    return other_role


def _entry_times(entry: dict[str, Any]) -> tuple[float | None, float | None]:
    """Accept common Sarvam / vendor timestamp field names."""
    start_keys = (
        "start_time_seconds",
        "start_time_sec",
        "start_seconds",
        "start_sec",
        "start",
        "start_time",
    )
    end_keys = (
        "end_time_seconds",
        "end_time_sec",
        "end_seconds",
        "end_sec",
        "end",
        "end_time",
    )

    def _as_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    start_s: float | None = None
    for key in start_keys:
        if key in entry:
            start_s = _as_float(entry.get(key))
            if start_s is not None:
                break

    end_s: float | None = None
    for key in end_keys:
        if key in entry:
            end_s = _as_float(entry.get(key))
            if end_s is not None:
                break

    return start_s, end_s


def parse_sarvam_payload(
    payload: dict[str, Any],
    *,
    include_empty: bool = False,
) -> list[dict[str, Any]]:
    """Parse Sarvam job JSON into chat-like segments with timestamps.

    Each segment includes: role, content, speaker_id, start_s, end_s, empty.
    When diarization is missing, returns one segment with role=\"unknown\".
    """
    diarized = payload.get("diarized_transcript") or {}
    entries = diarized.get("entries") or []
    if entries:
        first_speaker = str(entries[0].get("speaker_id", "0"))
        segments: list[dict[str, Any]] = []
        for entry in entries:
            content = str(entry.get("transcript", "")).strip()
            empty = not content
            if empty and not include_empty:
                continue
            speaker_id = str(entry.get("speaker_id", "0"))
            start_s, end_s = _entry_times(entry)
            segments.append(
                {
                    "role": _map_speaker_role(speaker_id, first_speaker),
                    "content": content,
                    "speaker_id": speaker_id,
                    "start_s": start_s,
                    "end_s": end_s,
                    "empty": empty,
                    "diarized": True,
                }
            )
        return segments

    transcript = str(payload.get("transcript", "")).strip()
    if transcript:
        return [
            {
                "role": "unknown",
                "content": transcript,
                "speaker_id": None,
                "start_s": None,
                "end_s": None,
                "empty": False,
                "diarized": False,
            }
        ]
    return []


def _score_output_payload(path: Path) -> tuple[int, int]:
    """Prefer JSON with the richest diarized transcript."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (-1, -1)
    diarized = payload.get("diarized_transcript") or {}
    entries = diarized.get("entries") or []
    transcript = str(payload.get("transcript", "") or "")
    return (len(entries), len(transcript))


def _pick_output_json(output_dir: Path) -> Path:
    output_files = sorted(Path(output_dir).glob("*.json"))
    if not output_files:
        raise ValueError("Sarvam job completed but returned no JSON output")
    return max(output_files, key=_score_output_payload)


def _job_failure_detail(job: Any) -> str:
    parts: list[str] = []
    for attr in ("job_id", "id", "error", "error_message", "status", "failure_reason"):
        value = getattr(job, attr, None)
        if value is None and isinstance(job, dict):
            value = job.get(attr)
        if value:
            parts.append(f"{attr}={value}")
    return ", ".join(parts) if parts else "unknown error"


def transcribe_audio_file(audio_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        num_speakers=SARVAM_NUM_SPEAKERS,
    )
    job.upload_files(file_paths=[str(audio_path)])
    job.start()
    job.wait_until_complete(poll_interval=5, timeout=1800)

    if job.is_failed():
        raise RuntimeError(f"Sarvam transcription job failed ({_job_failure_detail(job)})")

    with tempfile.TemporaryDirectory() as tmpdir:
        job.download_outputs(output_dir=tmpdir)
        output_path = _pick_output_json(Path(tmpdir))
        with output_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)

    return parse_sarvam_payload(payload), payload


def _suffix_from_response(url: str, content_type: str) -> str:
    content_type = (content_type or "").lower()
    if "audio/mpeg" in content_type or "audio/mp3" in content_type:
        return ".mp3"
    if "audio/wav" in content_type or "wav" in content_type:
        return ".wav"
    if "audio/ogg" in content_type or "opus" in content_type:
        return ".ogg"
    lower = url.lower().split("?", 1)[0]
    for ext in (".ogg", ".mp3", ".wav", ".m4a", ".webm"):
        if lower.endswith(ext):
            return ext
    return ".ogg"


def _download_audio(url: str, dest: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(1, SARVAM_DOWNLOAD_RETRIES + 1):
        try:
            with httpx.stream(
                "GET", url, timeout=120.0, follow_redirects=True
            ) as response:
                response.raise_for_status()
                total = 0
                with dest.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > SARVAM_DOWNLOAD_MAX_BYTES:
                            raise RuntimeError(
                                f"Audio download exceeded "
                                f"{SARVAM_DOWNLOAD_MAX_BYTES} bytes"
                            )
                        handle.write(chunk)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < SARVAM_DOWNLOAD_RETRIES:
                time.sleep(min(2**attempt, 8))
                continue
            raise RuntimeError(
                f"Failed to download audio after {SARVAM_DOWNLOAD_RETRIES} attempts: {exc}"
            ) from last_error


def transcribe_audio_url(audio_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not audio_url:
        raise ValueError("Missing audio URL")

    head_type = ""
    try:
        head = httpx.head(audio_url, timeout=30.0, follow_redirects=True)
        if head.is_success:
            head_type = head.headers.get("content-type", "")
    except httpx.HTTPError:
        head_type = ""

    suffix = _suffix_from_response(audio_url, head_type)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        _download_audio(audio_url, tmp_path)
        return transcribe_audio_file(tmp_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
