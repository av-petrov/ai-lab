"""Redaction and truncation."""

from __future__ import annotations

import json

import pytest

from argocd_observability.sanitize import REDACTED, redact, redact_text, shrink, tail_lines, to_llm_json, truncate_text


# Fake secrets are assembled at runtime so the source contains no secret-shaped literals
# (keeps secret scanners such as the repo's gitleaks pre-commit hook quiet without allow-lists).
FAKE_JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjg" + "NryP4J3jVmNHl0w5N"])
FAKE_API_KEY = "abcd" "1234" "efgh"


@pytest.mark.parametrize("text,secret", [
    ("connecting with password=hunter2 to db", "hunter2"),
    ('{"api_key": "' + FAKE_API_KEY + '"}', FAKE_API_KEY),
    ("DB_PASSWORD: s3cr3t!", "s3cr3t!"),
    ("Authorization: Bearer " + FAKE_JWT, FAKE_JWT[-22:-10]),
    ("token " + FAKE_JWT + " here", FAKE_JWT.split(".")[1][:8]),
    ("redis://admin:topsecret@redis:6379/0", "topsecret"),
    ("export OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA", "sk-proj-AAAA"),
    ("using key sk-AAAAAAAAAAAAAAAAAAAAAAAA", "sk-AAAAAAAA"),
    ("gh token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdef"),
    ("aws AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("client_secret='zzz-123'", "zzz-123"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----", "MIIEow"),
])
def test_redact_text(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


def test_redact_keeps_diagnostics():
    msg = 'Failed to pull image "gcr.io/x/web:v0-nope": not found; secret "db-creds" not found'
    assert redact_text(msg) == msg
    assert redact_text("FATAL: required environment variable REDIS_URL is not set").endswith("not set")


def test_redact_structures():
    pod = {
        "metadata": {"name": "p", "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"},
                     "managedFields": [{"x": 1}]},
        "spec": {"containers": [{"name": "c", "env": [
            {"name": "DB_PASSWORD", "value": "hunter2"},
            {"name": "LOG_LEVEL", "value": "debug"},
            {"name": "API_TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        ]}]},
        "data": {"password": "aHVudGVyMg=="},
        "token": "abc",
        "status": {"message": "auth failed: password=hunter2"},
    }
    out = redact(pod)
    s = json.dumps(out)
    assert "hunter2" not in s and "aHVudGVyMg" not in s
    assert "managedFields" not in out["metadata"]
    assert "kubectl.kubernetes.io/last-applied-configuration" not in out["metadata"]["annotations"]
    env = out["spec"]["containers"][0]["env"]
    assert env[0]["value"] == REDACTED and env[1]["value"] == "debug"
    assert env[2]["valueFrom"]["secretKeyRef"]["name"] == "s"  # reference names are kept
    assert out["token"] == REDACTED


def test_truncate_and_tail():
    text = "\n".join(f"line {i}" for i in range(1000))
    t = tail_lines(text, 10)
    assert t.splitlines()[-1] == "line 999" and "990 earlier lines omitted" in t
    assert len(t.splitlines()) == 11
    cut = truncate_text(text, 100)
    assert cut.endswith("line 999") and "truncated" in cut
    head = truncate_text(text, 100, keep="head")
    assert head.startswith("line 0")
    assert truncate_text("short", 100) == "short"


def test_shrink_and_llm_json_bounds():
    big = {"items": [{"name": f"app-{i}", "message": "x" * 5000} for i in range(500)]}
    s = shrink(big, max_list=5, max_str=50)
    assert len(s["items"]) == 6 and "495 more" in s["items"][-1]
    assert len(s["items"][0]["message"]) < 100
    for limit in (2000, 8000):
        out = to_llm_json(big, limit)
        assert len(out) <= limit + 60
    json.loads(to_llm_json({"a": [1, 2, 3]}, 1000))


def test_redaction_is_linear_on_huge_tokens():
    """Regression: an unbounded key=value regex backtracked catastrophically on long tokens."""
    import time

    blobs = ["x" * 200_000, "password" * 20_000, "a=" * 50_000, "tokentokentoken" * 10_000 + "=v",
             "http://" + "u" * 100_000]
    t = time.perf_counter()
    for b in blobs:
        redact_text(b)
    assert time.perf_counter() - t < 2.0
