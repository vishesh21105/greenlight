"""Secret scrubbing: hide anything that looks like a key, token or password.

Runs on every piece of text before it is sent to the AI model or stored.
"""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                               # AWS access key
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),          # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),                          # OpenAI-style keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),                         # Google API keys
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),                   # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWTs
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
]
# key = value / key: value where the key name sounds secret.
_ASSIGNMENT = re.compile(
    r"(?i)\b([\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|token|private[_-]?key)[\w.-]*)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{4,})(\3)"
)

REDACTED = "[REDACTED]"


def scrub(text: str) -> tuple[str, int]:
    """Return (clean_text, number_of_secrets_hidden)."""
    if not text:
        return text, 0
    count = 0
    for pattern in _PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        count += n

    def _assign(m: re.Match) -> str:
        nonlocal count
        if m.group(4) == REDACTED:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(5)}"

    text = _ASSIGNMENT.sub(_assign, text)
    return text, count


def scrub_text(text: str) -> str:
    return scrub(text)[0]
