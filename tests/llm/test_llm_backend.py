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


def test_5xx_fails_over_fast_without_retrying_same_model(fake_openai):
    """v063 hang: mimo-v2.5 returned 504 (~121s) and was retried 6x (~12min)
    before the chain advanced. A 5xx means THIS model/gateway is down — try it
    once and raise so ask() falls over to the next chain model immediately."""
    import pytest
    srv = fake_openai([(504, "gateway timeout")], retries=6)
    with pytest.raises(RuntimeError, match="HTTP 504"):
        lb._ask_openai("hi", "m")
    assert srv.call_count == 1, \
        f"5xx must NOT retry the same model; made {srv.call_count} calls"


def test_429_still_retries_after_5xx_change(fake_openai):
    """Regression guard: the 5xx fast-fail must not break 429 backoff/retry —
    a throttled (not down) model is fine, just rate-limited."""
    srv = fake_openai([(429, "rate"), (200, ok("recovered"))], retries=6)
    assert lb._ask_openai("hi", "m") == "recovered"
    assert srv.call_count == 2


def test_per_model_breaker_retires_a_5xx_model(fake_openai):
    """v063 cost sink: a 504'ing model was re-tried on every later call, each
    paying a full timeout. After N 5xx in a run the model must be dropped from
    the chain so subsequent calls skip it and go straight to the healthy one."""
    def router(payload):
        m = payload.get("model", "")
        return (504, "gw timeout") if "bad" in m else (200, ok("fine"))
    srv = fake_openai(router, retries=1)
    lb.configure_workers({"model_breaker_5xx": 2})
    lb.BASE_URL = srv.base_url
    bad, good = "openrouter/bad:free", "openrouter/good:free"
    for _ in range(3):
        assert lb.ask("hi", model=bad, role="implementer", step="s",
                      fallbacks=(good,)) == "fine"
    bad_calls = sum(1 for r in srv.requests if "bad" in r.get("model", ""))
    assert bad in lb._MODEL_DOWN, "model should be circuit-broken after 2x 5xx"
    assert bad_calls == 2, f"retired model must not be retried; got {bad_calls}"


def test_per_model_breaker_retires_a_429_exhausted_free_pool(fake_openai):
    """A free pool that keeps 429ing is exhausted for the DAY — waiting on it is
    futile and every call crawls through dead quota rounds. After N cumulative
    429s the model must be retired for the run so every role falls straight
    through to the healthy subscription fallback (claude via Meridian), not the
    corpse. Systemic provider-health routing, independent of any case config."""
    # the exhausted free pool 429s; the healthy fallback is claude via Meridian —
    # here modelled as claude routed through a gateway pointed at the fake server
    # (the same claude-http path a real run uses), so it answers deterministically
    # AND bypasses the global free-pool cooldown (a free stand-in could not — the
    # cooldown blocks EVERY free model once one 429s).
    def router(payload):
        m = payload.get("model", "")
        return (429, "rate limited") if "xiaomimimo" in m else (200, ok("fine"))
    srv = fake_openai(router, retries=1, backoff=0)   # one attempt, no backoff
    lb.configure_workers({"model_breaker_429": 2, "quota_wait_s": 0,
                          "claude_gateway": {"base_url": srv.base_url,
                                             "model_map": {"sonnet": "healthy"}}})
    lb.BASE_URL = srv.base_url
    exhausted, good = "xiaomimimo/mimo:free", "claude/sonnet"
    outs = [lb.ask("hi", model=exhausted, role="tester", step="s",
                   fallbacks=(good,)) for _ in range(3)]
    assert all(o == "fine" for o in outs), outs
    assert exhausted in lb._MODEL_DOWN, "429-exhausted pool must be circuit-broken"
    assert lb.last_call.get("backend") == "claude-http", lb.last_call


def test_429_retired_single_chain_rolls_to_claude(fake_openai, monkeypatch):
    """The exact live crawl (v142): the tester was pinned to the free pool with no
    per-call fallback; once the free daily quota was spent every call kept 429ing.
    After retirement the single-model chain must roll to the rotation (claude via
    Meridian) — the emergency picker must NOT re-pick the 429-retired corpse (it
    has 5xx=0 and would otherwise win the failure-count tie)."""
    monkeypatch.setenv("SPEC_FLOW_LLM_BACKOFF", "0")
    def router(payload):
        m = payload.get("model", "")
        return (429, "rate limited") if "xiaomimimo" in m else (200, ok("fine"))
    srv = fake_openai(router, retries=1, backoff=0)
    exhausted = "xiaomimimo/mimo:free"
    lb.configure_workers({"model_breaker_429": 1, "quota_retries": 0,
                          "quota_wait_s": 0, "fallback_models": ["claude/sonnet"],
                          "claude_gateway": {"base_url": srv.base_url,
                                             "model_map": {"sonnet": "healthy"}}})
    lb.BASE_URL = srv.base_url
    outs = [lb.ask("hi", model=exhausted, role="tester", step="s")
            for _ in range(4)]
    assert all(o == "fine" for o in outs), outs
    assert exhausted in lb._MODEL_DOWN
    assert lb.last_call.get("backend") == "claude-http", lb.last_call


def test_retired_single_model_chain_rolls_to_healthy_fallback(fake_openai):
    """v133 cost sink (#83): a one-model weak tier whose only model retired kept
    paying its 75s timeout EVERY call — `or chain[-1:]` forced the dead model
    back even though a healthy cross-provider fallback existed. A fully-retired
    chain must roll to the least-failed survivor (the rotation), not the corpse."""
    def router(payload):
        m = payload.get("model", "")
        return (504, "gw timeout") if "bad" in m else (200, ok("fine"))
    srv = fake_openai(router, retries=1)
    bad, good = "openrouter/bad:free", "openrouter/good:free"
    # single-model chain (no per-call fallbacks); `good` is the global rotation.
    lb.configure_workers({"model_breaker_5xx": 1,
                          "fallback_models": [good]})
    lb.BASE_URL = srv.base_url
    outs = [lb.ask("hi", model=bad, role="implementer", step="s")
            for _ in range(4)]
    assert all(o == "fine" for o in outs), outs
    bad_calls = sum(1 for r in srv.requests if "bad" in r.get("model", ""))
    # bad retires after its 1st 504; the remaining 3 calls must skip it and use
    # the healthy rotation rather than re-paying the dead model's timeout.
    assert bad in lb._MODEL_DOWN
    assert bad_calls == 1, f"retired model re-tried {bad_calls}x (should be 1)"
