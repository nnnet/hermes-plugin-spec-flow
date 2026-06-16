"""The unified live-model backend: provider/model are CONFIG, not code.

Verifies the openai-compatible path against a REAL local server (no monkeypatch,
no stub of our code — only the upstream API's replies are scripted, see
``fake_openai``): it parses a completion, retries through a 429 throttle (free
OpenRouter pools throttle hard), surfaces a loud error when the budget is
exhausted, and the agents' _ask delegates here so no harness file shells out to
a hard-coded ``claude -p`` anymore.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb  # noqa: E402
from harness_fakeapi import ok          # noqa: E402


def test_openai_path_parses_completion(fake_openai):
    fake_openai([(200, ok("ok!"))])
    assert lb._ask_openai("hi", "any/model") == "ok!"


def test_openai_retries_through_429(fake_openai):
    """Why: free-pool models throttle; one 429 must not kill a 50-node run."""
    srv = fake_openai([(429, "rate limited"), (200, ok("after-retry"))])
    assert lb._ask_openai("hi", "m") == "after-retry"
    assert srv.call_count == 2          # it really retried over real HTTP


def test_openai_exhausted_budget_is_loud(fake_openai):
    fake_openai([(500, "boom")], retries=2)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        lb._ask_openai("hi", "m")


def test_model_goes_into_payload(fake_openai):
    srv = fake_openai([(200, ok())])
    lb._ask_openai("question", "openrouter/qwen/qwen3-coder:free")
    assert srv.requests[0]["model"] == "openrouter/qwen/qwen3-coder:free"
    assert srv.requests[0]["path"].endswith("/chat/completions")


def test_agents_delegate_to_backend():
    """Why: the whole point — agents must have NO direct claude -p anymore."""
    from harness import llm_decomposer, llm_implementer, llm_judge
    for mod in (llm_decomposer, llm_implementer, llm_judge):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "llm_backend" in src
        assert '"claude", "-p"' not in src and "claude_cmd" not in src


def test_failure_reason_classifies_http_codes():
    """The failure report must read a remote-agent error legibly: a 404 (no such
    hermes/MC agent) surfaces its code instead of a generic 'error', while 5xx
    keeps its bucket and quota/timeout/empty are unchanged."""
    from harness import llm_backend as lb
    assert lb._failure_reason(
        RuntimeError("hermes agent tester returned HTTP 404: Not Found")) \
        == "http 404"
    assert lb._failure_reason("a2a tasks/send HTTP 502: bad gateway") == "5xx"
    assert lb._failure_reason("rate limited 429 quota") == "429"
    assert lb._failure_reason("connection timed out") == "timeout"
    assert lb._failure_reason("some opaque failure") == "error"
