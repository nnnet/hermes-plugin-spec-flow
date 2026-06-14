"""The live-run safety caps actually bite (offline, no quota).

Three bounds keep a live decomposition from running away; each is verified
here with a stubbed model reply:

  * ``LEAF_DEPTH``   — at depth >= N the harness FORCES a leaf (children are
    stripped, atomic declared) no matter what the model proposed;
  * ``MAX_CHILDREN`` — fan-out per node is trimmed to the cap;
  * engine ``max_decompose_calls`` — a never-converging decomposer dies loudly
    (covered in test_agent_decomposition; referenced here for the budget env).

Below the caps the FLEXIBLE path decides: the model's ``atomic`` judgment
reconciled by leaf_check (covered in test_atomicity_guardrail). These tests pin
the boundary between "flexible decides" and "the cap overrides".
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_decomposer as ld  # noqa: E402

CTX = {"project": {"goal": "g", "target": "t", "constitution": []},
       "node": {"id": "n", "title": "T"}, "parent": None}


def _stub(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(ld.llm_log, "timed_ask",
                        lambda ask, **kw: json.dumps(payload))
    monkeypatch.setattr(ld.llm_log, "log_outcome", lambda **kw: None)


def test_below_leaf_depth_model_branching_is_kept(monkeypatch):
    """Why: under the cap the FLEXIBLE judgment rules — a branch proposal
    must pass through untouched (the engine's leaf_check arbitrates later)."""
    monkeypatch.setenv("SPEC_FLOW_LLM_LEAF_DEPTH", "3")
    _stub(monkeypatch, {"atomic": False, "metrics": {"modules": 3},
                        "children": [{"id": "a", "title": "A"}]})
    out = ld.decompose({**CTX, "depth": 1})
    assert out["atomic"] is False
    assert [c["id"] for c in out["children"]] == ["a"]


def test_leaf_depth_forces_atomic_and_strips_children(monkeypatch):
    """Why: at the cap convergence is enforced, not hoped for — even if the
    model still proposes children they are dropped and the node is a leaf."""
    monkeypatch.setenv("SPEC_FLOW_LLM_LEAF_DEPTH", "3")
    _stub(monkeypatch, {"atomic": False, "metrics": {"modules": 9},
                        "children": [{"id": "a", "title": "A"},
                                     {"id": "b", "title": "B"}]})
    out = ld.decompose({**CTX, "depth": 3})
    assert out["atomic"] is True
    assert "children" not in out


def test_max_children_trims_fanout(monkeypatch):
    """Why: a wide tree must not blow the call budget — fan-out is capped."""
    monkeypatch.setenv("SPEC_FLOW_LLM_LEAF_DEPTH", "9")
    monkeypatch.setenv("SPEC_FLOW_LLM_MAX_CHILDREN", "2")
    kids = [{"id": f"c{i}", "title": str(i)} for i in range(6)]
    _stub(monkeypatch, {"atomic": False, "metrics": {}, "children": kids})
    out = ld.decompose({**CTX, "depth": 1})
    assert [c["id"] for c in out["children"]] == ["c0", "c1"]


def test_children_never_carry_metrics(monkeypatch):
    """Why: children are sized on their own visit — pre-supplied metrics from
    the model would smuggle the parent's view into the child's gate."""
    monkeypatch.setenv("SPEC_FLOW_LLM_LEAF_DEPTH", "9")
    _stub(monkeypatch, {"atomic": False, "metrics": {},
                        "children": [{"id": "a", "title": "A",
                                      "metrics": {"modules": 1}}]})
    out = ld.decompose({**CTX, "depth": 1})
    assert "metrics" not in out["children"][0]
