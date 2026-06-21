"""LLM power tiers: chain_for_tier + tier fallback in chain_for (prereq for the
doctor's escalate_tier and the complexity->tier router). Offline, no LLM call."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb       # noqa: E402


def _cfg():
    return {
        "tiers": {
            "weak": {"models": ["lmstudio/gpt-oss-20b", "claude/haiku"]},
            "strong": {"models": ["xiaomimimo/mimo-v2.5-pro"]},
        },
        "defaults": {"models": ["openrouter/q:free"]},
        "reviewer": {"tier": "strong"},      # role names a TIER, not models
        "implementer": {"models": ["claude/haiku"]},  # explicit models still win
    }


def test_chain_for_tier_resolves():
    lb.configure_workers(_cfg())
    assert lb.chain_for_tier("strong") == ["xiaomimimo/mimo-v2.5-pro"]
    assert lb.chain_for_tier("weak")[0] == "lmstudio/gpt-oss-20b"


def test_unknown_tier_falls_back_to_defaults():
    lb.configure_workers(_cfg())
    assert lb.chain_for_tier("nope") == ["openrouter/q:free"]


def test_role_tier_used_when_no_models():
    lb.configure_workers(_cfg())
    assert lb.chain_for("reviewer") == ["xiaomimimo/mimo-v2.5-pro"]


def test_explicit_models_beat_tier():
    lb.configure_workers(_cfg())
    assert lb.chain_for("implementer") == ["claude/haiku"]


def test_default_low_temperature(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_WORKER_TEMPERATURE", "0.1")
    # no params -> low default injected
    assert lb._with_default_params(None, {})["temperature"] == 0.1
    # explicit caller value wins (evaluator's 0.0 must survive)
    assert lb._with_default_params({"temperature": 0.0}, {})["temperature"] == 0.0
    # workers.temperature overrides the env floor
    assert lb._with_default_params(None, {"temperature": 0.3})["temperature"] == 0.3
