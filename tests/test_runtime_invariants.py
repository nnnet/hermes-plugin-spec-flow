"""Battle test — R1-R9 invariants as a RUNTIME guard (roadmap C3).

The methodology invariants (policy gate ran, no impl without a leaf verdict,
no silent drift, every impl reviewed, branches integrate, revisions re-derive,
green-on-completion) were previously only REPORTED by build_run_report after
the fact. C3 enforces them live: ``assert_invariants`` raises
``InvariantViolation`` on a hard breach, and the engine runs that guard at the
end of a run when ``runtime_guard=True``.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

CLEAN = {
    "name": "clean",
    "goal": "small service",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Service",
        "metrics": {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
                    "open_decisions": 0, "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "a", "title": "A",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
            {"id": "b", "title": "B",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
        ],
    },
}


# ─── the guard primitive ──────────────────────────────────────────────


def test_assert_invariants_passes_on_clean_run(plugin, tmp_path):
    res = eng.run_project(dict(CLEAN), workspace=str(tmp_path / "wk"), tools=plugin.tools)
    findings = plugin.tools.assert_invariants([vars(e) for e in res.events])
    # returns the (non-blocking) findings instead of raising
    assert all(f["severity"] != "error" for f in findings)


def test_assert_invariants_raises_on_missing_policy_gate(plugin):
    # a trace with no passing policy_gate violates R1
    bad = [{"tick": 1, "phase": "decompose", "profile": "spec-decomposer",
            "skill": "spec-flow-decompose", "task": "L0", "action": "leaf_check",
            "detail": "", "gate": "leaf_check", "verdict": "leaf", "level": 1}]
    with pytest.raises(plugin.tools.InvariantViolation) as exc:
        plugin.tools.assert_invariants(bad)
    assert any("R1" in r for r in exc.value.findings[0]["rule"].split()) or \
        "R1" in exc.value.findings[0]["rule"]


def test_violation_carries_findings(plugin):
    bad = [{"tick": 1, "phase": "decompose", "task": "L0", "action": "x",
            "detail": "", "gate": "", "verdict": "", "level": 1,
            "profile": "", "skill": ""}]
    try:
        plugin.tools.assert_invariants(bad)
        assert False, "should have raised"
    except plugin.tools.InvariantViolation as exc:
        assert exc.findings and "rule" in exc.findings[0]


# ─── enforced by the engine at runtime ────────────────────────────────


def test_engine_runtime_guard_passes_clean(plugin, tmp_path):
    # a clean run with the guard ON completes normally
    res = eng.run_project(dict(CLEAN), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools, runtime_guard=True)
    assert res.tasks["L0:req"].status == "done"


def test_engine_runtime_guard_catches_breach(plugin, tmp_path):
    # suppress the policy verdict -> R1 breach -> the guard raises mid-run
    proj = dict(CLEAN, _suppress_policy=True)
    with pytest.raises(plugin.tools.InvariantViolation):
        eng.run_project(proj, workspace=str(tmp_path / "wk"),
                        tools=plugin.tools, runtime_guard=True)


def test_breach_is_only_reported_when_guard_off(plugin, tmp_path):
    # same breach, guard OFF -> run completes; the audit still records R1
    proj = dict(CLEAN, _suppress_policy=True)
    res = eng.run_project(proj, workspace=str(tmp_path / "wk"), tools=plugin.tools)
    report = plugin.tools.build_run_report([vars(e) for e in res.events])
    assert "R1" in report
