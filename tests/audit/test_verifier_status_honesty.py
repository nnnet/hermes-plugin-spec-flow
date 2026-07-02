"""Audit rule (revision P7, v150, spec_flow_runner.py ~6364): the integrate
verifier's status is trusted VERBATIM only for PASS/FAIL — anything else
(ERROR, UNKNOWN, SKIP, an exception message) must never be promoted to PASS.

v150 mapping: ``"FAIL" if status == "FAIL" else "PASS"`` — a verifier
signalling an error condition (not a clean determination) contributed a
false PASS to the integration conjunction.

Contract enforced here:
  * an unrecognised status is surfaced as ERROR (a failure with the reason),
    it feeds the rework/record path exactly like a FAIL;
  * an explicit PASS still passes; an explicit FAIL still fails.

Deterministic: injected verifier agents, simulated depth, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import run_engine as eng  # noqa: E402

GOAL = {
    "name": "verifier-status-honesty",
    "goal": "small service",
    "target": "service works",
    "constitution": ["Keep it simple."],
    "tree": {
        "id": "L0", "title": "Service",
        "metrics": {"modules": 2, "tasks": 6, "interfaces": 1,
                    "estimated_loc": 300, "open_decisions": 0,
                    "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "core", "title": "Core",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1,
                         "estimated_loc": 80, "open_decisions": 0,
                         "single_concern": True, "testable_criteria": True}},
        ],
    },
}


def _run(plugin, tmp_path, verifier):
    return eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools, agents={"verifier": verifier})


def _integrate_verdicts(res):
    return [e.verdict for e in res.events
            if e.gate == "integrate_verify" and e.verdict]


def test_error_status_is_never_promoted_to_pass(plugin, tmp_path):
    res = _run(plugin, tmp_path,
               lambda ctx: {"status": "ERROR", "detail": "verifier crashed"})
    verdicts = _integrate_verdicts(res)
    assert verdicts and "PASS" not in verdicts, (
        f"an ERROR verifier status was counted green: {verdicts}")
    assert any(l["type"] == "integrate-fail" for l in res.loops), (
        "a non-PASS integrate verdict must be recorded as an integrate-fail")


def test_unknown_status_is_never_promoted_to_pass(plugin, tmp_path):
    res = _run(plugin, tmp_path,
               lambda ctx: {"status": "SKIPPED", "detail": "did not run"})
    verdicts = _integrate_verdicts(res)
    assert verdicts and "PASS" not in verdicts, (
        f"an unknown verifier status was counted green: {verdicts}")


def test_explicit_pass_and_fail_still_map_verbatim(plugin, tmp_path):
    ok = _run(plugin, tmp_path, lambda ctx: {"status": "PASS", "detail": "ok"})
    assert set(_integrate_verdicts(ok)) == {"PASS"}
    bad = _run(plugin, tmp_path, lambda ctx: {"status": "FAIL", "detail": "red"})
    assert "FAIL" in _integrate_verdicts(bad)
