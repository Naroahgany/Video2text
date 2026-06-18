"""Shared utility helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Iterable


SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"(Authorization\s*[:=]\s*)(Bearer\s+)?[^\s,;]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*)[^\s,;]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


def utc_now_iso() -> str:
    """Return an ISO timestamp with timezone."""

    return datetime.now(UTC).isoformat()


def redact_secrets(value: object, extra_secrets: Iterable[str] | None = None) -> str:
    """Redact API keys, Authorization headers, Bearer tokens and known secrets."""

    text = str(value)

    for secret in extra_secrets or ():
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")

    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: _redact_match(match), text)

    return text


def _redact_match(match: re.Match[str]) -> str:
    prefix = match.group(1) if match.lastindex else ""
    return f"{prefix}[REDACTED]" if prefix else "[REDACTED]"
