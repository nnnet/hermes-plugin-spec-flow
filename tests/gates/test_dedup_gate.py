"""Dedup gate: a branch must not re-create work that already exists
elsewhere in the tree.

Reproduces the live bug from the p4 b2b-marketplace run: the L1 node
"research_analogs" was re-proposed at L6 inside the architecture branch as
"research_marketplace_analogs" — a near-identical spec that went through
the full decompose+implement pipeline twice.

Asserts:
  * the duplicate child is PRUNED (never visited, no task, no spec),
  * the proposing node gets a ``depends_on`` link to the original,
  * the engine records a ``dedup-gate`` loop + a PRUNED gate event,
  * legitimate refinement of the node's OWN ancestor line is NOT pruned,
  * the decomposer agent receives ``ancestors`` + ``existing_nodes``.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

GOAL_ONLY = {
    "name": "dedup-goal",
    "goal": "B2B marketplace for refurbished equipment with seller payouts",
    "target": "GMV >= $20k in 90d",
    "constitution": ["No card data stored."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}

BIG = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
       "open_decisions": 0, "single_concern": False, "testable_criteria": True}
SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

SEEN_CTX: list[dict] = []


def duplicating_decomposer(ctx):
    """Root -> research + architecture; the architecture branch then
    proposes a DUPLICATE of the research node (the live L6 bug) plus one
    legitimate unique child."""
    SEEN_CTX.append(ctx)
    nid = ctx["node"]["id"]
    if ctx["depth"] == 0:
        return {"metrics": dict(BIG),
                "children": [
                    {"id": "research_analogs",
                     "title": "Research analog marketplaces, build-vs-reuse, differentiation"},
                    {"id": "architecture_nfr",
                     "title": "Define system architecture and baseline NFR"},
                ]}
    if nid == "architecture_nfr":
        return {"metrics": dict(BIG),
                "children": [
                    {"id": "research_marketplace_analogs",
                     "title": "Research analog marketplaces and build-vs-reuse decisions"},
                    {"id": "component_decomposition",
                     "title": "Component decomposition and scalability plan"},
                ]}
    return {"metrics": dict(SMALL)}


@pytest.fixture
def run(plugin, tmp_path):
    SEEN_CTX.clear()
    return eng.run_project(dict(GOAL_ONLY), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools,
                           agents={"decomposer": duplicating_decomposer})


def test_duplicate_child_is_pruned(run):
    assert "research_analogs" in run.tasks            # the original lives
    assert "research_marketplace_analogs" not in run.tasks   # the dup does not
    assert "component_decomposition" in run.tasks     # unique sibling survives


def test_pruned_dup_becomes_depends_on(run):
    arch = next(t for t in run.project["tree"]["children"]
                if t["id"] == "architecture_nfr")
    assert "research_analogs" in arch.get("depends_on", [])


def test_dedup_gate_is_recorded(run):
    loops = [l for l in run.loops if l["type"] == "dedup-gate"]
    assert loops and "research_marketplace_analogs" in loops[0]["detail"]
    pruned = [e for e in run.events if e.gate == "dedup_gate" and e.verdict == "PRUNED"]
    assert pruned


def test_decomposer_receives_tree_context(run):
    # every call after the root sees the registry; the architecture call
    # must have seen the research node it tried to duplicate
    arch_ctx = next(c for c in SEEN_CTX if c["node"]["id"] == "architecture_nfr")
    ids = {n["id"] for n in arch_ctx["existing_nodes"]}
    assert "research_analogs" in ids
    assert arch_ctx["ancestors"]            # root title present


def test_own_lineage_refinement_not_pruned(plugin, tmp_path):
    # a child refining its OWN parent shares tokens with it — legitimate
    def refining(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(BIG),
                    "children": [{"id": "research_analogs",
                                  "title": "Research analog marketplaces"}]}
        if ctx["node"]["id"] == "research_analogs":
            return {"metrics": dict(BIG),
                    "children": [{"id": "analog_marketplace_research",
                                  "title": "Research analog marketplaces shortlist"},
                                 {"id": "build_vs_reuse",
                                  "title": "Build vs reuse analysis"}]}
        return {"metrics": dict(SMALL)}
    res = eng.run_project(dict(GOAL_ONLY), workspace=str(tmp_path / "wk2"),
                          tools=plugin.tools, agents={"decomposer": refining})
    assert "analog_marketplace_research" in res.tasks
    assert not [l for l in res.loops if l["type"] == "dedup-gate"]
