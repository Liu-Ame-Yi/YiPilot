"""Small, reusable redaction for values that may reach durable output."""

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

_AUTHORIZATION = re.compile(
    r"(?i)(?:authorization|proxy-authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;\"']+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"']+")
_URL_CREDENTIALS = re.compile(r"(?i)\b(https?://)[^\s/@:]+(?::[^\s/@]*)?@")
_ENVIRONMENT_ASSIGNMENT = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)=([^\s,;]+)"
)
_SECRET_KEYS = {"authorization", "proxy-authorization", "api-key", "api_key", "password", "secret", "token", "x-api-key"}


class SecretRedactor:
    """Redact known values plus common credentials from text and JSON-like values."""

    def __init__(self, known_values: Sequence[str] = ()) -> None:
        self._known_values = tuple(
            sorted((value for value in known_values if value), key=len, reverse=True)
        )

    def redact_text(self, value: str) -> str:
        for secret in self._known_values:
            value = value.replace(secret, "[REDACTED]")
        value = _AUTHORIZATION.sub("Authorization: [REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
        return _ENVIRONMENT_ASSIGNMENT.sub(r"\1=[REDACTED]", value)

    def redact(self, value: object) -> object:
        if isinstance(value, Mapping):
            return {
                key: "[REDACTED]" if isinstance(key, str) and key.casefold() in _SECRET_KEYS else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def redact_url(self, value: str) -> str:
        """Return a safe diagnostic URL while retaining its endpoint."""
        parsed = urlsplit(value)
        if parsed.username is None and parsed.password is None:
            return self.redact_text(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


_COMMAND_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@")
_COMMAND_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+")
_COMMAND_BEARER = re.compile(r"(?i)(bearer\s+)\S+")
_COMMAND_SECRET_PARAMETER = re.compile(
    r"(?i)(\b(?:access[_-]?token|api[_-]?key|password|secret|token)=)[^\s&]+"
)

def safe_stderr_summary(stderr: str) -> str:
    summary = " ".join(stderr.strip().split())
    summary = _COMMAND_URL_CREDENTIALS.sub(r"\1[REDACTED]@", summary)
    summary = _COMMAND_AUTHORIZATION.sub(r"\1[REDACTED]", summary)
    summary = _COMMAND_BEARER.sub(r"\1[REDACTED]", summary)
    summary = _COMMAND_SECRET_PARAMETER.sub(r"\1[REDACTED]", summary)
    return summary[:500]
