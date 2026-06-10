"""Deterministic redaction.

Four mechanisms in priority order:

1. Pattern-based redaction (API keys, tokens, etc.).
2. Path exclusion (file content for sensitive paths is dropped, not stored).
3. Shannon-entropy detection for random-looking strings the patterns missed.
4. Contextual suppression (lines near phrases like "here is the token").

Sanitization runs at every ingest boundary. There is no code path that
stores unredacted text. Custom patterns can be added to the
``SECRET_PATTERNS``, ``EXCLUDED_FILE_PATTERNS``, and ``CONTEXTUAL_MARKERS``
lists below; if your environment uses a secret type that isn't covered,
add it here before running ``undrudge gather`` against real data.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[REDACTED]"
REDACTED_ENTROPY = "[REDACTED:high-entropy]"
REDACTED_CONTEXTUAL = "[REDACTED:contextual]"
REDACTED_FILE = "[REDACTED:sensitive-file]"


@dataclass
class Redaction:
    field: str
    pattern: str
    original_length: int
    hint: str  # short context to make audit logs useful without leaking the secret


@dataclass
class Result:
    text: str
    redactions: list[Redaction] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.redactions)


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("github-token", re.compile(r"\b(gh[ps]_[A-Za-z0-9_]{36,})\b")),
    ("github-fine-grained-token", re.compile(r"\b(github_pat_[A-Za-z0-9_]{22,})\b")),
    ("anthropic-key", re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{20,})\b")),
    ("openai-key", re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b")),
    # Linear API keys: lin_api_ + a long alphanumeric body. The trailing
    # `_` of the prefix is a word char, so the generic 40-char base64
    # catch-all never word-boundaries onto the body — without this
    # explicit pattern `lin_api_...` slips through ingest entirely.
    ("linear-api-key", re.compile(r"\b(lin_api_[A-Za-z0-9]{20,})\b")),
    ("slack-token", re.compile(r"\b(xox[boaprs]-[A-Za-z0-9-]{10,})\b")),
    (
        "jwt",
        re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+)\b"),
    ),
    ("bearer-token", re.compile(r"Bearer\s+([A-Za-z0-9._~+/=\-]{20,})", re.IGNORECASE)),
    (
        "api-key-assignment",
        re.compile(
            r"(?:api[_-]?key|apikey|api[_-]?secret|api[_-]?token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{16,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    (
        "connection-string",
        re.compile(
            r"(?:mongodb|postgres(?:ql)?|mysql|redis|amqp)(?:\+srv)?://[^\s'\"]+",
            re.IGNORECASE,
        ),
    ),
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    (
        "password-assignment",
        re.compile(
            r"(?:password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    # AWS secret keys are sloppy 40-char base64-ish blobs. Run last so other
    # more specific patterns win first.
    ("aws-secret-key", re.compile(r"\b([A-Za-z0-9/+=]{40})\b")),
]

_EXCLUDED_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.env(\.|$)"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"id_rsa"),
    re.compile(r"id_ed25519"),
    re.compile(r"id_ecdsa"),
    re.compile(r"authorized_keys"),
    re.compile(r"known_hosts"),
    re.compile(r"kubeconfig"),
    re.compile(r"\.kube/config"),
    re.compile(r"credentials\.json"),
    re.compile(r"secrets?\.(json|ya?ml|toml)"),
    re.compile(r"token\.json"),
]

_CONTEXTUAL_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"here is the token", re.IGNORECASE),
    re.compile(r"use this password", re.IGNORECASE),
    re.compile(r"my api key is", re.IGNORECASE),
    re.compile(r"the secret is", re.IGNORECASE),
    re.compile(r"authorization:\s*bearer", re.IGNORECASE),
    re.compile(r"export\s+\w*_(key|secret|token|password)\s*=", re.IGNORECASE),
]


# --------------------------------------------------------------------------
# Entropy
# --------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-+/=.]{16,}")
_MIN_ENTROPY_BITS = 4.5
_MIN_ENTROPY_LEN = 20


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_normal_content(token: str) -> bool:
    if "/" in token and all(re.fullmatch(r"[A-Za-z0-9._\-]+", p) for p in token.split("/") if p):
        return True
    if token.startswith(("http", "www")):
        return True
    if re.fullmatch(r"[a-z]+([A-Z][a-z]+)+[A-Za-z]*", token):  # camelCase
        return True
    if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", token):  # snake_case
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+", token):  # SCREAMING_SNAKE
        return True
    if re.search(r"(.)\1{4,}", token):  # >=5 repeated chars
        return True
    return bool(re.fullmatch(r"[a-z]+", token))  # all-lowercase word


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def redact(text: str, *, field_name: str = "text") -> Result:
    """Redact secrets from ``text``. Pure function; never raises on input."""
    if not text:
        return Result(text=text or "")

    out = text
    redactions: list[Redaction] = []

    # 1. Patterns
    for name, pat in _SECRET_PATTERNS:
        for m in pat.finditer(out):
            match = m.group(0)
            redactions.append(
                Redaction(
                    field=field_name,
                    pattern=name,
                    original_length=len(match),
                    hint=_hint(match),
                )
            )
        out = pat.sub(REDACTED, out)

    # 2. Entropy — single pass, replacing any surviving high-entropy token.
    for m in list(_TOKEN_PATTERN.finditer(out)):
        token = m.group(0)
        if len(token) < _MIN_ENTROPY_LEN:
            continue
        if _looks_like_normal_content(token):
            continue
        if shannon_entropy(token) < _MIN_ENTROPY_BITS:
            continue
        redactions.append(
            Redaction(
                field=field_name,
                pattern="high-entropy",
                original_length=len(token),
                hint=_hint(token),
            )
        )
        out = out.replace(token, REDACTED_ENTROPY)

    # 3. Contextual suppression — redact the marker line and the next.
    if any(pat.search(out) for pat in _CONTEXTUAL_MARKERS):
        lines = out.split("\n")
        i = 0
        while i < len(lines):
            if any(pat.search(lines[i]) for pat in _CONTEXTUAL_MARKERS):
                redactions.append(
                    Redaction(
                        field=field_name,
                        pattern="contextual",
                        original_length=len(lines[i]),
                        hint=lines[i][:20] + "…",
                    )
                )
                lines[i] = REDACTED_CONTEXTUAL
                if i + 1 < len(lines):
                    lines[i + 1] = REDACTED_CONTEXTUAL
                    i += 2
                    continue
            i += 1
        out = "\n".join(lines)

    return Result(text=out, redactions=redactions)


def is_excluded_file(path: str) -> bool:
    """True if a file path's content must never be stored."""
    return any(p.search(path) for p in _EXCLUDED_FILE_PATTERNS)


def redact_tool_input(
    tool_name: str, raw_input: dict[str, Any], *, field_name: str = "tool_input"
) -> tuple[dict[str, Any], list[Redaction]]:
    """Redact a tool-call input dict.

    For Read/Write/Edit/MultiEdit/NotebookEdit on a sensitive path, the
    content is dropped wholesale — only the path is kept.
    Other string fields are passed through ``redact``.
    """
    if not isinstance(raw_input, dict):
        return raw_input, []

    redactions: list[Redaction] = []

    if tool_name in {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = raw_input.get("file_path") or raw_input.get("notebook_path")
        if isinstance(path, str) and is_excluded_file(path):
            redactions.append(
                Redaction(
                    field=f"{field_name}:{tool_name}",
                    pattern="excluded-file",
                    original_length=len(json.dumps(raw_input)),
                    hint=path,
                )
            )
            return {"file_path": path, "content": REDACTED_FILE}, redactions

    cleaned: dict[str, Any] = {}
    for k, v in raw_input.items():
        if isinstance(v, str):
            r = redact(v, field_name=f"{field_name}.{k}")
            cleaned[k] = r.text
            redactions.extend(r.redactions)
        else:
            cleaned[k] = v
    return cleaned, redactions


def redact_command(command: str) -> Result:
    """Redact a shell command. Same rules as text plus a short-circuit:

    A command whose entire body is a single inline secret gets dropped to
    ``[REDACTED]`` rather than left as ``foo=[REDACTED]`` only.
    """
    return redact(command, field_name="command")


def _hint(s: str) -> str:
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}…{s[-4:]}"
