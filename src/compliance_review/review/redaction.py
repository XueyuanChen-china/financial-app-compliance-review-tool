from __future__ import annotations

import re
from typing import Any

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL
)
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|client[_-]?secret|private[_-]?key|aws_secret_access_key|openai_api_key)\b\s*[=:]\s*)([^\s,;]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|client[_-]?secret|private[_-]?key)"\s*:\s*")[^"]*(")'
)


def redact_sensitive_text(text: str) -> str:
    """Redact common credentials from serialized or model-visible text only."""
    redacted = _PEM_RE.sub("[REDACTED]", text)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGNMENT_RE.sub(r"\1[REDACTED]", redacted)
    return _JSON_SECRET_RE.sub(r"\1[REDACTED]\2", redacted)


def redact_value(value: Any) -> Any:
    """Recursively sanitize event/error payloads without touching source files."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value
