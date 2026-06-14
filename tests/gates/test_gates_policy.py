"""Unified gate policies (`gates:` block) + review-exhausted escalation.

gates.review.exhausted: record (default, today's behaviour) | halt | ask
— 'ask' consults the HITL channel: the OPERATOR decides whether an
unresolved REJECT records the debt or stops the run."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}

PROJECT = {
    "name": "gates-case", "goal": "one-leaf service", "target": "x",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
             "children": [{"id": "disc", "title": "Discovery",
                           "metrics": dict(_LEAF)}]},
}


def _always_reject(ctx):
    return {"verdict": "REJECT", "reasons": ["never satisfied"]}


def _run(tmp_path, gates=None, agents=None, **extra):
    project = dict(PROJECT, **extra)
    if gates is not None:
        project["gates"] = gates
    return eng.run_project(project, workspace=str(tmp_path / "wk"),
                           depth="spec", agents=agents, **(
                               {} if "human_ask" not in extra else {}))


def test_record_default_keeps_running(tmp_path):
    res = _run(tmp_path, gates={"review": {"rework": 1,
                                           "exhausted": "record"}},
               agents={"reviewer": _always_reject})
    assert "disc" in res.tasks            # the run carried on
    assert any("rework budget exhausted" in e.action for e in res.events)


def test_halt_stops_on_unresolved_reject(tmp_path):
    with pytest.raises(eng._runner.ReviewExhaustedHalt,
                       match="unresolved review REJECT"):
        _run(tmp_path, gates={"review": {"rework": 1, "exhausted": "halt"}},
             agents={"reviewer": _always_reject})


def test_ask_operator_says_halt(tmp_path):
    asked = []

    def operator(role, node, question):
        asked.append((role, node, question))
        return "halt"

    with pytest.raises(eng._runner.ReviewExhaustedHalt):
        eng.run_project(
            dict(PROJECT, gates={"review": {"rework": 1,
                                            "exhausted": "ask"}}),
            workspace=str(tmp_path / "wk"), depth="spec",
            agents={"reviewer": _always_reject}, human_ask=operator)
    assert asked and "review budget exhausted" in asked[0][2]


def test_ask_operator_silent_records_and_continues(tmp_path):
    res = eng.run_project(
        dict(PROJECT, gates={"review": {"rework": 1, "exhausted": "ask"}}),
        workspace=str(tmp_path / "wk"), depth="spec",
        agents={"reviewer": _always_reject},
        human_ask=lambda role, node, q: None)
    assert "disc" in res.tasks
    assert any(e.action == "review exhausted — operator consulted"
               for e in res.events)


def test_ask_without_channel_degrades_to_record(tmp_path):
    res = _run(tmp_path, gates={"review": {"rework": 1,
                                           "exhausted": "ask"}},
               agents={"reviewer": _always_reject})
    assert "disc" in res.tasks


def test_gates_integrate_maps_to_rework_policy(tmp_path):
    calls = []

    def verifier(ctx):
        calls.append(1)
        return {"status": "FAIL", "detail": "red"}

    res = _run(tmp_path, gates={"integrate": {"rework": 2,
                                              "exhausted": "record"}},
               agents={"verifier": verifier})
    rework = [l for l in res.loops if l["type"] == "integrate-rework"]
    assert len(rework) == 2 and len(calls) == 3


def test_gates_integrate_exhausted_halt(tmp_path):
    with pytest.raises(eng._runner.IntegrateFailHalt):
        _run(tmp_path, gates={"integrate": {"rework": 1,
                                            "exhausted": "halt"}},
             agents={"verifier":
                     lambda ctx: {"status": "FAIL", "detail": "red"}})


def test_unknown_exhausted_rejected(tmp_path):
    with pytest.raises(ValueError, match="record|halt|ask"):
        _run(tmp_path, gates={"review": {"exhausted": "explode"}})
