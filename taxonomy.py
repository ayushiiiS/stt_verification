"""Label normalization helpers (no fixed taxonomy — LLM/human define labels)."""

from __future__ import annotations

import re


def normalize_label(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^\w\u0900-\u097F]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def validate_label(
    domain: str,
    subdomain: str,
    *,
    is_custom: bool = False,
) -> tuple[str, str, bool]:
    del is_custom
    domain_norm = normalize_label(domain)
    subdomain_norm = normalize_label(subdomain)
    if not domain_norm or not subdomain_norm:
        raise ValueError("domain and subdomain are required")
    return domain_norm, subdomain_norm, True
