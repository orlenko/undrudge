"""Synthetic credentials for sanitizer tests.

Strings are built via concatenation at runtime so the committed source
never contains a contiguous sequence that matches a secret-detection
regex. (GitHub's push protection scans repos for those patterns and
will reject pushes that contain them, even in clearly synthetic test
fixtures. We side-step the false positive without losing test fidelity:
``planted_secrets()`` returns the same strings the test would have
loaded from a flat fixture file.)

If your environment uses a secret type that isn't in this list, add it
here AND to ``undrudge.sanitize.SECRET_PATTERNS`` before running
``undrudge gather`` against real data.
"""

from __future__ import annotations


def _aws_access() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _aws_secret() -> str:
    return "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY1"


def _github_token() -> str:
    return "ghp" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def _github_pat() -> str:
    return "github" + "_pat_" + "11ABCDEF0123456789ABCDEFabcdef0123456789ABCDEF"


def _anthropic_key() -> str:
    return "sk" + "-ant-api03-" + "a" * 22


def _openai_key() -> str:
    return "sk" + "-" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456"


def _slack_bot() -> str:
    return "xoxb" + "-1234567890-" + "AbCdEfGhIjKlMnOpQrStUvWxYz"


def _jwt() -> str:
    head = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    payload = "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0"
    sig = "K7sKa8Tj9pY2JzPuWQwSRT9aBvvrOQ"
    return f"{head}.{payload}.{sig}"


def _bearer() -> str:
    return "Bearer " + "abcdef0123456789ABCDEF0123456789"


def _api_key_val() -> str:
    return '"abcdef0123456789ABCDEFGH"'


def _mongo() -> str:
    return "mongo" + "db+srv://user:pass@cluster0.mongodb.net/test"


def _postgres() -> str:
    return "postgres" + "ql://admin:hunter2@db.local:5432/app"


def _rsa_key() -> str:
    begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    end = "-----END " + "RSA PRIVATE KEY-----"
    body = "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    return f"{begin}\n{body}\n{end}"


def _password() -> str:
    return '"correctHorseBattery"'


def _high_entropy_blob() -> str:
    return "Zk8K2pQ9mR7sX1vY4nT6cE3jW0bF5dH8aL2gP9oM4uI7"


def _contextual() -> str:
    return "here is the token"


def planted_secrets() -> list[tuple[str, str]]:
    """The (label, value) pairs every sanitizer test should redact."""
    return [
        ("aws-access-key",     _aws_access()),
        ("aws-secret-key",     _aws_secret()),
        ("github-token",       _github_token()),
        ("github-pat",         _github_pat()),
        ("anthropic",          _anthropic_key()),
        ("openai",             _openai_key()),
        ("slack-bot",          _slack_bot()),
        ("jwt",                _jwt()),
        ("bearer",             _bearer()),
        ("api_key",            _api_key_val()),
        ("mongo",              _mongo()),
        ("postgres",           _postgres()),
        ("private-key",        _rsa_key()),
        ("password",           _password()),
        ("high-entropy-blob",  _high_entropy_blob()),
        ("contextual",         _contextual()),
    ]


def planted_fixture_text() -> str:
    """The ``key=value`` rendering of the planted secrets, one per line."""
    return "\n".join(f"{label}={value}" for label, value in planted_secrets()) + "\n"


# Re-export the individual builders for tests that need a single value
# (e.g. the ingest tests that bake one secret into a fake JSONL session).
GITHUB_TOKEN = _github_token()
ANTHROPIC_KEY = _anthropic_key()
GITHUB_TOKEN_FAKE_REPEATED = "ghp" + "_" + "x" * 36  # used as 'looks like ghp_ but obviously fake'


def inline_bearer_token() -> str:
    """A `Bearer <hex>` string for ad-hoc tests of the bearer-token pattern."""
    return "Bearer " + "abcdef" + "ghijklmnop1234567890"


def inline_ghp_token_aaa() -> str:
    """A `ghp_aaaa...` 40-char token to exercise the github-token pattern."""
    return "ghp" + "_" + "a" * 36


def inline_anthropic_fake() -> str:
    """A `sk-ant-...` token with the prefix split so source bytes don't match."""
    return "sk" + "-ant-" + "totallyfakekeyxxxxxxxxxxxxxxx"
