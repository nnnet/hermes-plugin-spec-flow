"""Audit rule (revision P7, v150, spec_flow_runner.py ~6416): a HITL rejection
must send the node into REWORK — never fall through to status='done'.

v150 code path: the rejection branch emitted 'rework after HITL rejection'
and bumped version/runs, then control fell through unconditionally to
``self.tasks[nid].status = "done"`` and ``self._completed += 1`` — a
human-REJECTED node was counted successfully completed.

Contract enforced here:
  * a rejection triggers a rework round and the human is ASKED AGAIN;
  * approval after rework completes the node normally (done);
  * a node still rejected after the rework budget is an honest failure,
    never done.

Deterministic: injected approver agents, simulated depth, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import run_engine as eng  # noqa: E402

GOAL = {
    "name": "hitl-honesty",
    "goal": "payments service with a human-gated payout",
    "target": "payouts correct",
    "constitution": ["Payouts above $50 need human approval."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 50,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Payments",
        "metrics": {"modules": 2, "tasks": 8, "interfaces": 2,
                    "estimated_loc": 400, "open_decisions": 0,
                    "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "ledger", "title": "Ledger",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1,
                         "estimated_loc": 80, "open_decisions": 0,
                         "single_concern": True, "testable_criteria": True}},
            {"id": "payouts", "title": "Seller payouts",
             "hitl": {"kind": "spend",
                      "reason": "payout above the $50 cap needs human approval"},
             "metrics": {"modules": 1, "tasks": 4, "interfaces": 1,
                         "estimated_loc": 95, "open_decisions": 0,
                         "single_concern": True, "testable_criteria": True}},
        ],
    },
}


def _run(plugin, tmp_path, approver):
    return eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools, agents={"approver": approver})


def test_terminally_rejected_node_is_never_done(plugin, tmp_path):
    def veto(ctx):
        if ctx["kind"] == "spend":
            return {"approved": False, "reason": "cap policy not wired"}
        return {"approved": True, "reason": "ok"}

    res = _run(plugin, tmp_path, veto)
    assert res.tasks["payouts"].status != "done", (
        "a node the human kept rejecting was counted DONE (v150 fall-through)")


def test_rejection_reasks_the_human_after_rework(plugin, tmp_path):
    asks = {"spend": 0}

    def approve_second_time(ctx):
        if ctx["kind"] == "spend":
            asks["spend"] += 1
            return {"approved": asks["spend"] >= 2, "reason": "rework first"}
        return {"approved": True, "reason": "ok"}

    res = _run(plugin, tmp_path, approve_second_time)
    assert asks["spend"] >= 2, (
        "a rejection must lead to rework + a re-ask, not a single verdict")
    assert res.tasks["payouts"].status == "done"
    assert res.tasks["payouts"].version >= 2  # the rework bumped the node


def test_approved_node_still_completes_normally(plugin, tmp_path):
    res = _run(plugin, tmp_path,
               lambda ctx: {"approved": True, "reason": "ok"})
    assert res.tasks["payouts"].status == "done"
    assert not any(l["type"] == "hitl-reject" for l in res.loops)
