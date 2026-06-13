"""Per-role worker configuration from the case YAML `workers:` block.

The block is the SINGLE source of provider/model truth when present:
providers form the available pool (lists with parameters), each role
carries an ordered model chain (primary + quota fallbacks), and every
chain entry must match a declared provider."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb        # noqa: E402


@pytest.fixture(autouse=True)
def clean_cfg():
    lb.configure_workers(None)
    yield
    lb.configure_workers(None)


CFG = {
    "providers": [
        {"name": "openrouter-free", "kind": "openai",
         "model_prefix": "openrouter/", "require_suffix": ":free"},
        {"name": "claude-cli", "kind": "claude", "model_prefix": "claude/"},
    ],
    "defaults": {"models": ["openrouter/qwen/qwen3-coder:free",
                            "claude/haiku"]},
    "reviewer": {"models": ["openrouter/meta-llama/llama-3.3:free",
                            "claude/haiku"]},
}


def test_chain_resolution_role_over_defaults():
    lb.configure_workers(CFG)
    assert lb.chain_for("reviewer")[0] == "openrouter/meta-llama/llama-3.3:free"
    assert lb.chain_for("implementer")[0] == "openrouter/qwen/qwen3-coder:free"
    assert lb.chain_for("implementer")[1] == "claude/haiku"
    assert lb.model_for("reviewer") == lb.chain_for("reviewer")[0]


def test_yaml_block_beats_env(monkeypatch):
    # models come STRICTLY from the case YAML when the block exists
    monkeypatch.setenv("SPEC_FLOW_REVIEWER_MODEL", "openrouter/other:free")
    lb.configure_workers(CFG)
    assert lb.model_for("reviewer") == "openrouter/meta-llama/llama-3.3:free"


def test_env_applies_without_block(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REVIEWER_MODEL", "openrouter/other:free")
    assert lb.model_for("reviewer") == "openrouter/other:free"


def test_scalar_model_reads_as_one_element_chain():
    lb.configure_workers({"defaults": {"model": "openrouter/solo:free"}})
    assert lb.chain_for("implementer") == ["openrouter/solo:free"]


def test_model_outside_provider_pool_rejected():
    bad = dict(CFG, defaults={"models": ["openai/gpt-4o"]})
    lb.configure_workers(bad)
    with pytest.raises(ValueError, match="matches no provider"):
        lb.chain_for("implementer")


def test_provider_suffix_requirement_enforced():
    # an openrouter model WITHOUT ':free' must not pass the pool
    bad = dict(CFG, defaults={"models": ["openrouter/qwen/qwen3-coder"]})
    lb.configure_workers(bad)
    with pytest.raises(ValueError, match="matches no provider"):
        lb.chain_for("implementer")


def test_ask_walks_chain_on_quota_exhaustion(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    calls = []

    def fake_openai(prompt, model, system=None):
        calls.append(("openai", model))
        raise lb.QuotaExhausted("429")

    def fake_claude(prompt, model, system=None, direct=False):
        calls.append(("claude", model, direct))
        return "chain answer"

    monkeypatch.setattr(lb, "_ask_openai", fake_openai)
    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    out = lb.ask("q", model="openrouter/a:free",
                 fallbacks=["claude/haiku"])
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    assert out == "chain answer"
    assert calls == [("openai", "openrouter/a:free"),
                     ("claude", "haiku", True)]
    assert lb.last_call == {"backend": "claude", "model": "haiku",
                            "fallback": True}


def test_explicit_claude_model_bypasses_free_gate(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")

    def fake_claude(prompt, model, system=None, direct=False):
        return f"{model} direct={direct}"

    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    assert lb.ask("q", model="claude/haiku") == "haiku direct=True"


def test_paid_model_still_forbidden(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    with pytest.raises(ValueError, match="forbidden"):
        lb.ask("q", model="openrouter/gpt-4o")


# ─── run-wide LLM-call budget (quota limiter, off by default) ─────────

def _stub_ok(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "_ask_openai",
                        lambda prompt, model, system=None: "ok")


def test_budget_off_by_default(monkeypatch):
    _stub_ok(monkeypatch)
    for _ in range(5):
        assert lb.ask("q", model="openrouter/a:free") == "ok"
    assert lb.calls_made() == 5


def test_budget_from_workers_block(monkeypatch):
    _stub_ok(monkeypatch)
    lb.configure_workers({"budget": 2})
    lb.ask("q", model="openrouter/a:free")
    lb.ask("q", model="openrouter/a:free")
    with pytest.raises(lb.BudgetExhausted):
        lb.ask("q", model="openrouter/a:free")


def test_budget_from_env_without_block(monkeypatch):
    _stub_ok(monkeypatch)
    monkeypatch.setenv("SPEC_FLOW_LLM_BUDGET", "1")
    lb.ask("q", model="openrouter/a:free")
    with pytest.raises(lb.BudgetExhausted):
        lb.ask("q", model="openrouter/a:free")


def test_chain_attempts_each_spend_budget(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)

    def exhausted(prompt, model, system=None):
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_openai", exhausted)
    monkeypatch.setattr(
        lb, "_ask_claude",
        lambda prompt, model, system=None, direct=False: "ok")
    lb.configure_workers({"budget": 10})
    lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"])
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    assert lb.calls_made() == 2          # one free attempt + one fallback


def test_configure_resets_budget_counter(monkeypatch):
    _stub_ok(monkeypatch)
    lb.configure_workers({"budget": 3})
    lb.ask("q", model="openrouter/a:free")
    assert lb.calls_made() == 1
    lb.configure_workers({"budget": 3})
    assert lb.calls_made() == 0


def test_mixed_429_raises_quota_for_fallback(monkeypatch):
    # one 429 among the retries is a quota signal — the old `all retries
    # throttled` rule let a final 429 surface as RuntimeError and killed
    # a whole live run with no fallback
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "BACKOFF", 0.0)
    answers = [(500, "boom"), (429, '{"retry_after_seconds": 0}'),
               (502, "bad gateway")]
    monkeypatch.setattr(lb, "_http_post",
                        lambda url, payload, headers: answers.pop(0))
    with pytest.raises(lb.QuotaExhausted, match="throttled"):
        lb._ask_openai("q", "openrouter/a:free")


def test_chain_absorbs_plain_provider_failure(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)

    def broken(prompt, model, system=None):
        raise RuntimeError("openai backend failed after 3 tries: HTTP 500")

    monkeypatch.setattr(lb, "_ask_openai", broken)
    monkeypatch.setattr(
        lb, "_ask_claude",
        lambda prompt, model, system=None, direct=False: "fallback answer")
    out = lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"])
    assert out == "fallback answer"


def test_paid_gate_error_still_aborts_the_chain(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    with pytest.raises(ValueError, match="forbidden"):
        lb.ask("q", model="openrouter/gpt-4o", fallbacks=["claude/haiku"])


def test_exhausted_chain_waits_then_recovers(monkeypatch):
    # v18 death class: free pool out + terminal fallback capped killed
    # the RUN. With quota_retries the chain sleeps and tries again.
    lb.configure_workers({"quota_wait_s": 0.01, "quota_retries": 2,
                          "providers": [{"name": "openrouter-free",
                                         "kind": "openai",
                                         "model_prefix": "openrouter/",
                                         "require_suffix": ":free"}]})
    monkeypatch.setattr(lb, "FALLBACK_MODEL", None)
    calls = {"n": 0}

    def flaky(prompt, m, system, fallback=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise lb.QuotaExhausted("429")
        return "recovered"

    monkeypatch.setattr(lb, "_ask_one", flaky)
    try:
        assert lb.ask("q", model="openrouter/a:free",
                      fallbacks=["openrouter/b:free"]) == "recovered"
        assert calls["n"] == 3, "two failures absorbed by one wait round"
    finally:
        lb.configure_workers(None)


def test_exhausted_chain_raises_without_optin(monkeypatch):
    # default stays fail-fast: tests and ad-hoc calls never sleep
    lb.configure_workers({"providers": [{"name": "openrouter-free",
                                         "kind": "openai",
                                         "model_prefix": "openrouter/",
                                         "require_suffix": ":free"}]})
    monkeypatch.setattr(lb, "FALLBACK_MODEL", None)

    def dead(prompt, m, system, fallback=False):
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_one", dead)
    try:
        import pytest as _pt
        with _pt.raises(lb.QuotaExhausted):
            lb.ask("q", model="openrouter/a:free")
    finally:
        lb.configure_workers(None)
