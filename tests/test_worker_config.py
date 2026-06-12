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
