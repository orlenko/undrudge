"""Privacy golden tests.

The single non-negotiable invariant: no planted secret survives sanitize.
If anything here fails, the system is unsafe to run.
"""

from __future__ import annotations

import json

import pytest
from fixtures import (
    inline_anthropic_fake,
    inline_bearer_token,
    inline_ghp_token_aaa,
    planted_fixture_text,
    planted_secrets,
)

from undrudge.sanitize import (
    REDACTED,
    REDACTED_ENTROPY,
    REDACTED_FILE,
    is_excluded_file,
    redact,
    redact_command,
    redact_tool_input,
    shannon_entropy,
)

SECRETS = planted_secrets()


def test_fixture_loads():
    assert SECRETS, "fixture parse produced no rows"
    labels = {label for label, _ in SECRETS}
    expected = {
        "aws-access-key", "aws-secret-key", "github-token", "github-pat",
        "anthropic", "openai", "slack-bot", "jwt", "bearer", "api_key",
        "mongo", "postgres", "private-key", "password", "high-entropy-blob",
        "contextual",
    }
    missing = expected - labels
    assert not missing, f"fixture missing: {missing}"


@pytest.mark.parametrize("label,secret", SECRETS, ids=[s[0] for s in SECRETS])
def test_pattern_or_entropy_redacts_each_secret(label: str, secret: str):
    haystack = f"prefix\n{label}={secret}\nsuffix"
    result = redact(haystack)
    assert secret not in result.text, (
        f"{label!r} secret survived in: {result.text!r}"
    )
    assert result.redactions, f"{label!r} produced no redaction entry"


def test_full_fixture_blanket():
    """Run the whole fixture through redact at once and assert every literal vanishes."""
    raw = planted_fixture_text()
    out = redact(raw, field_name="fixture").text
    for label, secret in SECRETS:
        if not secret:
            continue
        assert secret not in out, f"{label} survived blanket redact"


def test_excluded_file_paths():
    for path in [
        "/Users/vlad/.env",
        "/etc/.env.production",
        "secrets.toml",
        "/repo/secrets.yaml",
        "/repo/secret.json",
        "/home/foo/.ssh/id_rsa",
        "/home/foo/.ssh/id_ed25519",
        "/home/foo/.ssh/authorized_keys",
        "/home/foo/.kube/config",
        "kubeconfig",
        "/path/to/credentials.json",
        "/x/some.pem",
        "/x/server.key",
        "store.p12",
        "id_ecdsa",
    ]:
        assert is_excluded_file(path), path

    for path in [
        "/repo/main.py",
        "README.md",
        "src/lib.ts",
    ]:
        assert not is_excluded_file(path), path


def test_redact_tool_input_drops_excluded_file_content():
    cleaned, redactions = redact_tool_input(
        "Read",
        {"file_path": "/Users/vlad/.env", "limit": 100},
    )
    assert cleaned["content"] == REDACTED_FILE
    assert cleaned["file_path"] == "/Users/vlad/.env"
    assert any(r.pattern == "excluded-file" for r in redactions)


def test_redact_tool_input_passes_through_safe_paths():
    cleaned, redactions = redact_tool_input(
        "Read",
        {"file_path": "/repo/main.py"},
    )
    assert cleaned == {"file_path": "/repo/main.py"}
    assert redactions == []


def test_redact_tool_input_redacts_string_args():
    bearer = inline_bearer_token()
    cleaned, redactions = redact_tool_input(
        "Bash",
        {"command": f"curl -H 'Authorization: {bearer}' x"},
    )
    assert bearer not in json.dumps(cleaned)
    assert any(r.pattern == "bearer-token" for r in redactions)


def test_redact_command_runs_redact():
    token = inline_ghp_token_aaa()
    r = redact_command(f"export GITHUB_TOKEN={token}")
    assert token not in r.text


def test_contextual_marker_redacts_following_line():
    secret = inline_anthropic_fake()
    text = f"blah\nhere is the token\n{secret}\ntail"
    out = redact(text).text
    assert secret not in out
    assert "here is the token" not in out


def test_entropy_threshold():
    # Random base64-ish blob — should trip entropy detection.
    blob = "Zk8K2pQ9mR7sX1vY4nT6cE3jW0bF5dH8aL2gP9oM4uI7"
    out = redact(blob).text
    assert blob not in out

    # Common identifier — must NOT be redacted.
    benign = "averyboringidentifierthatisnotasecret"
    assert benign in redact(benign).text


def test_shannon_entropy_basic():
    assert shannon_entropy("") == 0
    assert shannon_entropy("aaaa") == 0
    assert shannon_entropy("abcd") > 1.5
    # Every-char-distinct, length 4 → log2(4) = 2.0
    assert abs(shannon_entropy("abcd") - 2.0) < 1e-9


def test_redact_idempotent_on_clean_text():
    text = "Just a normal log line about reading src/main.py — no secrets here."
    r = redact(text)
    assert r.text == text
    assert r.redactions == []


def test_redacted_marker_preserved_when_text_already_clean():
    """Surviving REDACTED markers shouldn't trigger entropy redaction."""
    text = f"value: {REDACTED} and more {REDACTED_ENTROPY}"
    r = redact(text)
    assert REDACTED in r.text
