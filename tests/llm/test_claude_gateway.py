"""Phase 4 enabler: the claude subscription model can ride an OpenAI-compatible
HTTP GATEWAY (e.g. bifrost serving anthropic/claude-haiku-4-5) instead of the
local CLI, so the claude fallback arm shares ONE real transport and is
deterministically exercisable. Production keeps the CLI by leaving the gateway
unset — verified here against a REAL local server (no monkeypatch of our code).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import llm_backend as lb  # noqa: E402
from harness_fakeapi import ok          # noqa: E402


def test_claude_routes_through_gateway_when_configured(fake_openai):
    # a real local server stands in for the gateway; claude/haiku must reach it
    # over HTTP with the mapped model id, no CLI involved.
    srv = fake_openai([(200, ok("via gateway"))])
    try:
        lb.configure_workers({"claude_gateway": {
            "base_url": srv.base_url,
            "model_map": {"haiku": "anthropic/claude-haiku-4-5"}}})
        out = lb._ask_one("p", "claude/haiku", None, fallback=False)
        assert out == "via gateway"
        assert srv.requests[0]["model"] == "anthropic/claude-haiku-4-5"
        assert srv.requests[0]["path"].endswith("/chat/completions")
        assert lb.last_call["backend"] == "claude-http"
    finally:
        lb.configure_workers(None)


def test_claude_falls_back_arm_is_real_http_through_gateway(fake_openai):
    # the genuine unblock: a primary 429 rolls to the claude fallback, which now
    # answers over real HTTP (the gateway), so the whole chain runs for real.
    srv = fake_openai([(429, "rate limited"), (200, ok("claude saved it"))])
    try:
        lb.configure_workers({
            "quota_wait_s": 0, "quota_retries": 0,
            "claude_gateway": {"base_url": srv.base_url},
            "providers": [{"name": "orf", "kind": "openai",
                           "model_prefix": "openrouter/",
                           "require_suffix": ":free"}]})
        # primary free model 429s (first scripted reply), then the claude
        # fallback hits the same server and gets the 200.
        out = lb.ask("p", model="openrouter/a:free", fallbacks=["claude/haiku"],
                     role="decomposer", step="")
        assert out == "claude saved it"
        assert srv.call_count == 2
    finally:
        lb.configure_workers(None)


def test_no_gateway_means_cli_path():
    # default (no config, no env): the gateway resolver is None, so _ask_one
    # keeps the subscription CLI route.
    try:
        lb.configure_workers(None)
        assert lb._claude_gateway() is None
    finally:
        lb.configure_workers(None)
