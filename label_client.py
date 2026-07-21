"""LLM client for open-ended call domain / subdomain labeling."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from taxonomy import normalize_label
from transcript_utils import visible_messages

LABEL_PROVIDER = (os.environ.get("LABEL_PROVIDER") or "gemini").strip().lower()
LABEL_MODEL = (os.environ.get("LABEL_MODEL") or "").strip()
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or ""
).strip()
LABEL_API_KEY = (
    os.environ.get("LABEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
).strip()
LABEL_API_BASE = (
    os.environ.get("LABEL_API_BASE") or "https://api.openai.com/v1"
).rstrip("/")
GEMINI_API_BASE = (
    os.environ.get("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")
LABEL_MAX_TRANSCRIPT_CHARS = int(os.environ.get("LABEL_MAX_TRANSCRIPT_CHARS", "3000"))
LABEL_MAX_TURNS = int(os.environ.get("LABEL_MAX_TURNS", "8"))
LABEL_MAX_CHARS_PER_TURN = int(os.environ.get("LABEL_MAX_CHARS_PER_TURN", "180"))
LABEL_INCLUDE_RATIONALE = (os.environ.get("LABEL_INCLUDE_RATIONALE") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}
LABEL_REQUEST_TIMEOUT_SEC = float(os.environ.get("LABEL_REQUEST_TIMEOUT_SEC", "60"))

_DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def label_api_key() -> str:
    if LABEL_PROVIDER == "openai":
        return LABEL_API_KEY
    return GEMINI_API_KEY or LABEL_API_KEY


def effective_model() -> str:
    if LABEL_MODEL:
        return LABEL_MODEL
    if LABEL_PROVIDER == "openai":
        return _DEFAULT_OPENAI_MODEL
    return _DEFAULT_GEMINI_MODEL


def _truncate_turn(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _sample_messages(messages: list[dict]) -> list[dict]:
    """Keep enough context for intent without sending the full call."""
    visible = visible_messages(messages)
    max_turns = max(2, LABEL_MAX_TURNS)
    if len(visible) <= max_turns:
        return visible
    head = max(2, max_turns - 2)
    return [*visible[:head], *visible[-2:]]


def build_transcript_text(messages: list[dict]) -> str:
    sampled = _sample_messages(messages)
    lines: list[str] = []
    for msg in sampled:
        role = "A" if msg.get("role") == "assistant" else "U"
        content = _truncate_turn(msg.get("content") or "", LABEL_MAX_CHARS_PER_TURN)
        if content:
            lines.append(f"{role}: {content}")
    text = "\n".join(lines).strip()
    if len(text) > LABEL_MAX_TRANSCRIPT_CHARS:
        text = text[:LABEL_MAX_TRANSCRIPT_CHARS].rstrip() + "…"
    return text


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty LLM response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM response is not JSON") from None
        return json.loads(match.group(0))


def _gemini_generate(system_prompt: str, user_prompt: str) -> str:
    api_key = label_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or LABEL_API_KEY) is required for Gemini auto-labeling. "
            "Add it to .env (see .env.example)."
        )
    model = effective_model()
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    with httpx.Client(timeout=LABEL_REQUEST_TIMEOUT_SEC) as client:
        response = client.post(url, params={"key": api_key}, json=payload)
        response.raise_for_status()
        data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_parts = [str(part.get("text") or "") for part in parts if part.get("text")]
    content = "\n".join(text_parts).strip()
    if not content:
        raise RuntimeError("Gemini returned empty content")
    return content


def _openai_generate(system_prompt: str, user_prompt: str) -> str:
    api_key = label_api_key()
    if not api_key:
        raise RuntimeError(
            "LABEL_API_KEY or OPENAI_API_KEY is required for OpenAI auto-labeling. "
            "Add it to .env (see .env.example)."
        )
    url = f"{LABEL_API_BASE}/chat/completions"
    payload = {
        "model": effective_model(),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=LABEL_REQUEST_TIMEOUT_SEC) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("OpenAI returned empty content")
    return str(content)


def _chat_completion(system_prompt: str, user_prompt: str) -> str:
    if LABEL_PROVIDER == "openai":
        return _openai_generate(system_prompt, user_prompt)
    return _gemini_generate(system_prompt, user_prompt)


_SYSTEM_PROMPT = """Classify phone calls. Return JSON only:
{"domain":"snake_case","subdomain":"snake_case","confidence":0.0}
domain = industry/business area; subdomain = specific topic/intent.
Use unknown/unknown if unclear. Hindi/English/Hinglish OK."""


def classify_transcript(transcript_text: str) -> dict[str, Any]:
    """Single LLM call: infer domain + subdomain from original transcript text."""
    if not (transcript_text or "").strip():
        raise ValueError("insufficient_transcript")

    user_prompt = f"Transcript:\n{transcript_text}"
    parsed = _extract_json(_chat_completion(_SYSTEM_PROMPT, user_prompt))

    domain = normalize_label(str(parsed.get("domain") or "unknown")) or "unknown"
    subdomain = normalize_label(str(parsed.get("subdomain") or "unknown")) or "unknown"
    confidence = float(parsed.get("confidence") or 0.5)
    confidence = max(0.0, min(1.0, confidence))
    rationale = (
        str(parsed.get("rationale") or "").strip() if LABEL_INCLUDE_RATIONALE else ""
    )
    model = effective_model()

    return {
        "domain": domain,
        "subdomain": subdomain,
        "domainConfidence": confidence,
        "subdomainConfidence": confidence,
        "rationale": rationale,
        "model": f"{LABEL_PROVIDER}:{model}",
    }
