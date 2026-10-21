import pytest

from enguru_lib.redact import is_env_file, redact


@pytest.mark.parametrize("text, expected", [
    ("key AKIAIOSFODNN7EXAMPLE end", "key [REDACTED] end"),
    ("sk-abcdefghijklmnopqrstuvwx", "[REDACTED]"),
    ("ghp_" + "a1" * 15, "[REDACTED]"),
    ("github_pat_" + "A_b1" * 10, "[REDACTED]"),
    ("xoxb-1234567890-abcdefghij", "[REDACTED]"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123==",
     "Authorization: [REDACTED]"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ\n-----END RSA PRIVATE KEY-----",
     "[REDACTED]"),
    ("postgres://user:hunter2@db.example.com/app",
     "postgres://[REDACTED]@db.example.com/app"),
    ("password: hunter2hunter2", "password: [REDACTED]"),
    ('api_key = "abcdefgh12345678"', 'api_key = "[REDACTED]"'),
])
def test_each_pattern_is_redacted(text, expected):
    assert redact(text) == expected


def test_plain_text_untouched():
    assert redact("token count is 42") == "token count is 42"


@pytest.mark.parametrize("path, expected", [
    (".env", True),
    (".env.local", True),
    ("prod.env", True),
    ("env.py", False),
    ("environment.ts", False),
])
def test_env_files(path, expected):
    assert is_env_file(path) is expected
