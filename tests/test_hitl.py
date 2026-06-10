"""Battle test — human-in-the-loop (HITL).

The plugin puts a human in the loop both ways:
  * from the human side — an injected `approver` agent can APPROVE or REJECT;
  * during spec & implementation — a spec checkpoint (constitution + target)
    before decomposition, node-level checkpoints (e.g. payouts above the cap),
    and a large-blast respec confirmation.

A rejection sends work back for rework (version bump + a `hitl-reject` loop).
Without an injected approver the autonomous default approves but records that
no human was attached — so the checkpoint is never silently skipped.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

HITL_GOAL = {
    "name": "hitl-case",
    "goal": "payments service with a human-gated payout",
    "target": "payouts correct",
    "constitution": ["Payouts above $50 need human approval."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 50,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Payments",
        "metrics": {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
                    "open_decisions": 0, "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "ledger", "title": "Ledger",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
            {"id": "payouts", "title": "Seller payouts",
             "hitl": {"kind": "spend", "reason": "payout above the $50 cap needs human approval"},
             "metrics": {"modules": 1, "tasks": 4, "interfaces": 1, "estimated_loc": 95,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
        ],
    },
}


def _run(plugin, tmp_path, approver=None):
    agents = {"approver": approver} if approver else None
    return eng.run_project(dict(HITL_GOAL), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools, agents=agents)


def test_default_approver_runs_checkpoints(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    # spec checkpoint (human_in_loop) + node checkpoint (payouts) both fired
    hitl_events = [e for e in res.events if e.gate == "hitl"]
    kinds = {e.detail and e.action.split()[1] for e in hitl_events}  # 'spec' / 'spend'
    assert res.gate_calls.get("hitl", 0) >= 2
    assert any("spec" in e.action for e in hitl_events)
    assert any("spend" in e.action for e in hitl_events)


def test_default_approver_approves(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    assert all(e.verdict == "approved" for e in res.events if e.gate == "hitl")
    assert not any(l["type"] == "hitl-reject" for l in res.loops)


def test_injected_human_can_reject(plugin, tmp_path):
    # a human rejects the payout checkpoint -> rework + a hitl-reject loop
    def picky(ctx):
        if ctx["kind"] == "spend":
            return {"approved": False, "reason": "cap policy not wired — fix before shipping"}
        return {"approved": True, "reason": "ok"}

    res = _run(plugin, tmp_path, approver=picky)
    rejects = [l for l in res.loops if l["type"] == "hitl-reject"]
    assert rejects and rejects[0]["kind"] == "spend"
    assert res.tasks["payouts"].version >= 2     # node sent back for rework


def test_approver_profile_is_exercised(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    assert "approver" in res.profiles_used


def test_no_human_in_loop_no_spec_checkpoint(plugin, tmp_path):
    proj = dict(HITL_GOAL)
    proj["policy"] = dict(proj["policy"], human_in_loop=False)
    # drop the node-level hitl too
    proj["tree"] = {**proj["tree"],
                    "children": [proj["tree"]["children"][0],
                                 {k: v for k, v in proj["tree"]["children"][1].items()
                                  if k != "hitl"}]}
    res = eng.run_project(proj, workspace=str(tmp_path / "wk2"), tools=plugin.tools)
    assert res.gate_calls.get("hitl", 0) == 0


def test_oracle_sees_hitl_episode(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
    rep = plugin.tools.check_oracle(res, {"expected_episodes": ["hitl"]}, summary)
    assert rep.ok, plugin.tools.render_oracle(rep)
