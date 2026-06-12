"""on_integrate_fail policy (plan P11): record | rework | halt.

What a FAILed branch integrate does next is the CASE's choice, not the
engine's hardcode:
  * record — today's behaviour: a loop entry, the run continues;
  * rework — re-invoke the verifier (fresh repair budget) up to
    integrate_max_rework rounds before recording;
  * halt   — stop the run on the spot (IntegrateFailHalt)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng   # noqa: E402

_METRICS_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2,
                   "estimated_loc": 400, "open_decisions": 0,
                   "single_concern": False, "testable_criteria": True}
_METRICS_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1,
                 "estimated_loc": 80, "open_decisions": 0,
                 "single_concern": True, "testable_criteria": True}

PROJECT = {
    "name": "integrate-policy-case",
    "goal": "tiny service whose branch integrate fails",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Tiny", "metrics": dict(_METRICS_BRANCH),
        "children": [
            {"id": "a", "title": "Leaf A", "metrics": dict(_METRICS_LEAF)},
            {"id": "b", "title": "Leaf B", "metrics": dict(_METRICS_LEAF)},
        ],
    },
}


def _failing_verifier(calls):
    def verify(ctx):
        calls.append(ctx["node"])
        return {"status": "FAIL", "detail": "suite is red"}
    return verify


def _run(tmp_path, policy=None, verifier=None, **extra):
    project = dict(PROJECT, **extra)
    if policy is not None:
        project["on_integrate_fail"] = policy
    agents = {"verifier": verifier} if verifier else None
    return eng.run_project(project, workspace=str(tmp_path / "wk"),
                           depth="spec", agents=agents)


def test_record_is_the_default_and_continues(tmp_path):
    calls = []
    res = _run(tmp_path, verifier=_failing_verifier(calls))
    fails = [l for l in res.loops if l["type"] == "integrate-fail"]
    assert fails, "a FAIL verdict must be recorded"
    assert not [l for l in res.loops if l["type"] == "integrate-rework"]
    assert len(calls) == 1, "record policy never re-invokes the verifier"


def test_rework_reinvokes_verifier_with_rounds(tmp_path):
    calls = []
    res = _run(tmp_path, policy="rework", verifier=_failing_verifier(calls),
               integrate_max_rework=2)
    rework = [l for l in res.loops if l["type"] == "integrate-rework"]
    assert [l["round"] for l in rework] == [1, 2]
    assert len(calls) == 3        # initial + 2 rework rounds
    assert [l for l in res.loops if l["type"] == "integrate-fail"]


def test_rework_stops_at_first_pass(tmp_path):
    calls = []

    def flaky(ctx):
        calls.append(1)
        return ({"status": "FAIL", "detail": "red"} if len(calls) == 1
                else {"status": "PASS"})

    res = _run(tmp_path, policy="rework", verifier=flaky,
               integrate_max_rework=3)
    rework = [l for l in res.loops if l["type"] == "integrate-rework"]
    assert len(rework) == 1 and len(calls) == 2
    assert not [l for l in res.loops if l["type"] == "integrate-fail"]


def test_halt_stops_the_run(tmp_path):
    with pytest.raises(eng._runner.IntegrateFailHalt, match="demands a halt"):
        _run(tmp_path, policy="halt", verifier=_failing_verifier([]))


def test_unknown_policy_rejected(tmp_path):
    with pytest.raises(ValueError, match="record|rework|halt"):
        _run(tmp_path, policy="explode", verifier=_failing_verifier([]))


def test_pass_runs_clean_under_every_policy(tmp_path):
    for i, policy in enumerate(("record", "rework", "halt")):
        res = eng.run_project(
            dict(PROJECT, on_integrate_fail=policy),
            workspace=str(tmp_path / f"wk{i}"), depth="spec",
            agents={"verifier": lambda ctx: {"status": "PASS"}})
        assert not [l for l in res.loops
                    if l["type"].startswith("integrate-")]
