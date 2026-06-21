"""424: complexity routing on spawn. The engine maps a node's leaf_check
complexity (leaf_small / leaf_big / branch) to a power tier and spawns the
implementer on that tier's chain — a hard node on a stronger model, a trivial
one on the cheap chain. Offline, no LLM call; decided purely by structure."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from harness import llm_backend as lb        # noqa: E402
from spec_flow_runner import Engine   # noqa: E402


def _cfg():
    return {
        "tiers": {
            "weak": {"models": ["lmstudio/gpt-oss-20b"]},
            "medium": {"models": ["openrouter/q:free"]},
            "strong": {"models": ["xiaomimimo/mimo-v2.5-pro"]},
        },
        "complexity_to_tier": {"leaf_small": "weak",
                               "leaf_big": "medium", "branch": "strong"},
        "implementer": {"models": ["claude/haiku"]},
        "defaults": {"models": ["claude/haiku"]},
    }


def test_tier_param_wins_over_role_models():
    lb.configure_workers(_cfg())
    # a KNOWN tier overrides even the role's explicit models (the engine sets
    # it deliberately at spawn from the node's measured complexity)
    assert lb.chain_for("implementer", tier="strong") == ["xiaomimimo/mimo-v2.5-pro"]
    # unknown / unset tier is ignored -> normal role resolution
    assert lb.chain_for("implementer", tier="nope") == ["claude/haiku"]
    assert lb.chain_for("implementer") == ["claude/haiku"]


def _runner():
    r = Engine.__new__(Engine)
    r._project_meta = {"workers": _cfg()}
    r.review_policy = {"simple_max_loc": 60}
    return r


def test_small_leaf_routes_weak():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 30, "open_decisions": 0}}
    assert r._node_tier(node) == "weak"


def test_big_leaf_routes_medium():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 200, "open_decisions": 0}}
    assert r._node_tier(node) == "medium"


def test_open_decisions_promote_to_big():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 20, "open_decisions": 2}}
    assert r._node_tier(node) == "medium"


def test_branch_routes_strong():
    r = _runner()
    node = {"id": "n", "children": [{"id": "c"}], "metrics": {}}
    assert r._node_tier(node) == "strong"


def test_no_map_yields_empty_tier():
    r = Engine.__new__(Engine)
    r._project_meta = {}          # no complexity_to_tier override
    r.review_policy = {}
    # falls back to the packaged default map, still resolves a tier name;
    # an EMPTY map (forced) must yield '' so routing degrades to role default
    import spec_flow_remedies as rem
    saved = rem._DEFAULT_COMPLEXITY
    rem._DEFAULT_COMPLEXITY = {}
    try:
        assert r._node_tier({"id": "n", "metrics": {}}) == ""
    finally:
        rem._DEFAULT_COMPLEXITY = saved
