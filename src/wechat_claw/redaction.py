"""Shared redaction for every text sent outside the local machine."""

from __future__ import annotations

import re


_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)([^\n'\"]+)"
)
_COOKIE_VALUE = re.compile(r"(?i)(cookie\s*[:=]\s*)([^\n'\"]+)")
_SECRET_ARGUMENT = re.compile(
    r"(?i)(--(?:access[-_]?token|token|secret|password|passwd|api[-_]?key|cookie))"
    r"(?:\s*=\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_SECRET_VALUE = re.compile(
    r"(?i)((?:access[_-]?)?token|secret|password|passwd|api[_-]?key)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{12,}|github_pat_[a-z0-9_]{12,})"
)


def redact_sensitive_text(text: str) -> str:
    redacted = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", text)
    redacted = _COOKIE_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_ARGUMENT.sub(r"\1=[REDACTED]", redacted)
    redacted = _SECRET_VALUE.sub(r"\1\2[REDACTED]", redacted)
    return _KNOWN_TOKEN.sub("[REDACTED]", redacted)
