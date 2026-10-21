"""Secret redaction for transcript text and env-file detection."""
import os
import re

_PRIVATE_KEY = r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"

_TOKEN_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"sk-[A-Za-z0-9_-]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}=*",
]

_PATTERNS = [re.compile(_PRIVATE_KEY)] + [re.compile(p) for p in _TOKEN_PATTERNS]

_URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@")

# Group 1 keeps the key name, separator and optional opening quote.
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?)[^\s'\"]{8,}"
)


def redact(text: str) -> str:
    """Replace every detected secret in text with [REDACTED]."""
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    return _ASSIGNMENT.sub(r"\1[REDACTED]", text)


def is_env_file(path: str) -> bool:
    """True for .env, .env.* and *.env files (by basename)."""
    name = os.path.basename(path)
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")
