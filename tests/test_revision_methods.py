"""Battle test — the TWO revision methods (from the design discussion).

  1. level_return — fires the moment a branch folds up and control returns one
     level up (the `on_level_return` trigger);
  2. internal — a continuous lane firing DURING the run by accumulated work
     (every_n_tasks / m_test_errors), reopening an affected node mid-flight.

Both must: run the research trigger, respec (version-bump) the invalidated
node, re-derive its subtree, and be distinguishable in the trace by `method`.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

GOAL = {
    "name": "rev-methods",
    "goal": "service with two revision lanes",
    "target": "works",
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Root",
        "metrics": {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
                    "open_decisions": 0, "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "alpha", "title": "Alpha branch",
             "metrics": {"modules": 2, "tasks": 6, "interfaces": 2, "estimated_loc": 300,
                         "open_decisions": 0, "single_concern": False, "testable_criteria": True},
             "children": [
                 {"id": "a1", "title": "Alpha leaf 1",
                  "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                              "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
                 {"id": "a2", "title": "Alpha leaf 2",
                  "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                              "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
             ]},
            {"id": "beta", "title": "Beta leaf",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
        ],
    },
    "revisions": [
        {"method": "level_return", "trigger": "on_level_return",
         "finding": "downstream rule change invalidates alpha", "invalidates": "alpha",
         "effect": "version-bump alpha, re-derive its leaves"},
        # internal fires mid-run and REOPENS an already-completed node (beta),
        # which is what "continuous revision during the run" means.
        {"method": "internal", "trigger": "every_n_tasks", "after_completed": 2,
         "finding": "mid-run finding invalidates beta", "invalidates": "beta",
         "effect": "version-bump beta"},
    ],
}


@pytest.fixture
def run(plugin, tmp_path):
    return eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools)


def test_both_methods_fire(run):
    methods = {l.get("method") for l in run.loops if l["type"] == "revision-respec"}
    assert "level_return" in methods, "level-return revision never fired"
    assert "internal" in methods, "internal revision never fired"


def test_level_return_bumps_its_target(run):
    assert run.tasks["alpha"].version >= 2


def test_internal_bumps_its_target(run):
    assert run.tasks["beta"].version >= 2


def test_each_revision_fires_once(run):
    revs = [l for l in run.loops if l["type"] == "revision-respec"]
    targets = [l["task"] for l in revs]
    assert targets.count("alpha") == 1 and targets.count("beta") == 1


def test_research_trigger_invoked(run):
    assert run.gate_calls.get("research_trigger_check", 0) >= 2


def test_legacy_single_revision_still_works(plugin, tmp_path):
    proj = dict(GOAL)
    proj.pop("revisions")
    proj["revision"] = {"trigger": "on_level_return",
                        "finding": "x", "invalidates": "alpha", "effect": "y"}
    res = eng.run_project(proj, workspace=str(tmp_path / "wk2"), tools=plugin.tools)
    methods = {l.get("method") for l in res.loops if l["type"] == "revision-respec"}
    assert "level_return" in methods  # inferred from the on_level_return trigger


def test_oracle_detects_both_methods(plugin, tmp_path):
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk3"), tools=plugin.tools)
    summary = plugin.tools.summarize_trace(
        [eng.asdict_event(e) for e in res.events] if hasattr(eng, "asdict_event")
        else [vars(e) for e in res.events])
    spec = {"expected_episodes": ["revision_internal", "revision_level_return"]}
    rep = plugin.tools.check_oracle(res, spec, summary)
    assert rep.ok, plugin.tools.render_oracle(rep)
