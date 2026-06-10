"""The plugin builds the task tree ITSELF from the goal via a decomposer agent.

No predefined tree is given — only goal/constitution/target. A deterministic
fake decomposer agent (standing in for the LLM / Hermes worker) answers each
node request; the engine drives the recursion and gates every node with the
REAL leaf_check. Asserts: the tree exists, converges to leaves, every node was
gated, and without any agent the run fails loudly (no silent canned tree).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

GOAL_ONLY = {
    "name": "goal-only",
    "goal": "Self-serve invoicing for freelancers",
    "target": "first invoice in < 3 min; 100 invoices/day",
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


def fake_decomposer(ctx):
    """Deterministic stand-in for the LLM: root -> 3 children, children are
    leaves; the first child is upfront research."""
    if ctx["depth"] == 0:
        return {"metrics": dict(BIG),
                "children": [
                    {"id": "research_analogs", "title": "Analogs & build-vs-reuse"},
                    {"id": "invoice_editor", "title": "Invoice editor"},
                    {"id": "delivery", "title": "Invoice delivery (email/PDF)"},
                ],
                "spike": {"question": "Reuse an OSS invoicing core?",
                          "recommendation": "Build thin core, reuse PDF lib"}}
    return {"metrics": dict(SMALL)}


@pytest.fixture
def run(plugin, tmp_path):
    return eng.run_project(dict(GOAL_ONLY), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools,
                           agents={"decomposer": fake_decomposer})


def test_tree_is_built_from_goal(run):
    # no 'tree' was given — the run still produced a real multi-node board
    assert "research_analogs" in run.tasks
    assert "invoice_editor" in run.tasks and "delivery" in run.tasks
    assert run.tasks["L0:integrate"].status == "done"


def test_every_built_node_was_gated(run):
    # the agent only proposes; leaf/branch is decided by the REAL leaf_check
    gated = {e.task for e in run.events if e.gate == "leaf_check"}
    assert {"L0", "research_analogs", "invoice_editor", "delivery"} <= gated
    assert run.gate_calls["leaf_check"] >= 4


def test_agent_calls_are_logged(run):
    built = [e for e in run.events if "decomposer agent built" in e.action]
    assert len(built) == 4  # root + 3 children


def test_no_agent_and_no_tree_fails_loudly(plugin, tmp_path):
    with pytest.raises(NotImplementedError):
        eng.run_project(dict(GOAL_ONLY), workspace=str(tmp_path / "wk"),
                        tools=plugin.tools)


def test_runaway_decomposition_is_capped(plugin, tmp_path):
    def endless(ctx):  # always branches -> must hit the call ceiling
        return {"metrics": dict(BIG),
                "children": [{"id": f"{ctx['node']['id']}_a", "title": "a"},
                             {"id": f"{ctx['node']['id']}_b", "title": "b"}]}
    with pytest.raises(RuntimeError, match="does not converge"):
        eng.run_project(dict(GOAL_ONLY), workspace=str(tmp_path / "wk"),
                        tools=plugin.tools, agents={"decomposer": endless})
