"""Regression — reports must build for a run with NO predefined tree.

In live (decomposer) mode the case carries no ``tree`` — the plugin builds it
from the goal. The renderers used to do ``proj["tree"]`` and KeyError, crashing
report generation after the run completed (found during live p1 calibration).
The engine now persists the REALIZED tree into ``project["tree"]`` and the
renderers degrade gracefully when it is still absent.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402


def _stub_decomposer():
    """A tiny deterministic decomposer: L0 -> two leaves, then leaves."""
    LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
            "open_decisions": 0, "single_concern": True, "testable_criteria": True}
    BRANCH = {"modules": 3, "tasks": 9, "interfaces": 3, "estimated_loc": 500,
              "open_decisions": 0, "single_concern": False, "testable_criteria": True}

    def decompose(ctx):
        if ctx["depth"] == 0:
            return {"metrics": BRANCH,
                    "children": [{"id": "alpha", "title": "Alpha"},
                                 {"id": "beta", "title": "Beta"}]}
        return {"metrics": LEAF}

    return decompose


GOAL = {
    "name": "no-tree-case",
    "goal": "a service the plugin must decompose itself",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    # NB: no "tree" — the decomposer builds it
}


def test_realized_tree_is_persisted(plugin, tmp_path):
    proj = dict(GOAL)
    res = eng.run_project(proj, workspace=str(tmp_path / "wk"), tools=plugin.tools,
                          agents={"decomposer": _stub_decomposer()})
    # the engine wrote the tree it actually built back into the project
    assert res.project.get("tree")
    assert res.project["tree"]["id"] == "L0"
    kids = {c["id"] for c in res.project["tree"].get("children", [])}
    assert kids == {"alpha", "beta"}


def test_render_report_does_not_crash_without_predefined_tree(plugin, tmp_path):
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"), tools=plugin.tools,
                          agents={"decomposer": _stub_decomposer()})
    # these are exactly the calls run_cases makes after a run — must not raise
    report = eng.render_report(res, level=eng.L_DETAIL)
    assert "alpha" in report or "Alpha" in report
    mermaid = eng.render_mermaid(res)
    assert "flowchart" in mermaid


def test_renderers_degrade_when_tree_truly_absent(plugin, tmp_path):
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"), tools=plugin.tools,
                          agents={"decomposer": _stub_decomposer()})
    # simulate an old/loaded result that never captured a tree
    res.project.pop("tree", None)
    assert "no task tree" in eng.render_tree(res)
    assert "no task tree" in eng.render_mermaid(res)
