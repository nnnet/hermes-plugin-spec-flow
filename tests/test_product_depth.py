"""Depth 'product' — build+run the materialised product and assert readiness.

Why: depth 'execute' only proves real code & tests exist; 'product' proves the
integrated whole meets an acceptance spec, writing a READY / NOT READY verdict
to PRODUCT-RESULTS.md. These tests pin three properties: the artifact is
written, the verdict is HONEST (stub modules + no real entrypoint -> NOT READY,
never silently green), and the new depth still completes the run while depths
below 'product' never emit PRODUCT-RESULTS.md.

How to test: run_project at depth='product' with the bundled auto_implementer
plus a deterministic decomposer (an inline goal-only project), and inspect the
workspace.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import auto_implementer, run_engine as eng  # noqa: E402


# Inline goal-only project: no predefined tree, so a decomposer builds it; the
# bundled auto_implementer then materialises real (stub) modules per leaf.
GOAL_ONLY = {
    "name": "todo-cli",
    "goal": "A tiny command-line todo list",
    "target": "add/list/done in < 1s; persists to a local file",
    "constitution": ["No network access."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}

BIG = {"modules": 3, "tasks": 12, "interfaces": 2, "estimated_loc": 600,
       "open_decisions": 0, "single_concern": False, "testable_criteria": True}
SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def fake_decomposer(ctx):
    """Deterministic stand-in for the LLM: root -> 2 leaf children."""
    if ctx["depth"] == 0:
        return {"metrics": dict(BIG),
                "children": [{"id": "store", "title": "Todo store"},
                             {"id": "cli", "title": "CLI commands"}]}
    return {"metrics": dict(SMALL)}


def _configure_validator(plugin):
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]


def _agents():
    return {"implementer": auto_implementer.implement,
            "decomposer": fake_decomposer}


def _run(plugin, root, *, depth, acceptance=None):
    project = dict(GOAL_ONLY)
    if acceptance is not None:
        project["acceptance"] = acceptance
    _configure_validator(plugin)
    return eng.run_project(project, workspace=str(root), depth=depth,
                           tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                           agents=_agents())


def test_product_depth_writes_product_results(plugin, tmp_path):
    """A small acceptance block at depth 'product' produces PRODUCT-RESULTS.md."""
    root = tmp_path / "wk"
    acceptance = {"smoke": ["process starts and exits 0"],
                  "e2e": ["add then list shows the item"]}
    _run(plugin, root, depth="product", acceptance=acceptance)
    assert (root / "PRODUCT-RESULTS.md").exists()


def test_product_honesty_no_entrypoint_is_not_ready(plugin, tmp_path):
    """HONESTY: stub implementer + a smoke check that cannot pass (no real
    entrypoint) must yield NOT READY, never a silent green."""
    root = tmp_path / "wk"
    acceptance = {"smoke": ["server answers GET /health with 200"]}
    _run(plugin, root, depth="product", acceptance=acceptance)
    body = (root / "PRODUCT-RESULTS.md").read_text(encoding="utf-8")
    assert "NOT READY" in body
    assert "READY" not in body.replace("NOT READY", "")  # not even partly green
    # the failing check is named in the failed-checks section
    assert "server answers GET /health" in body


def test_no_acceptance_does_not_fail_and_states_not_asserted(plugin, tmp_path):
    """No acceptance spec: still writes PRODUCT-RESULTS.md, states readiness is
    not asserted, and does not fail the run."""
    root = tmp_path / "wk"
    res = _run(plugin, root, depth="product", acceptance=None)
    assert res.tasks["L0:integrate"].status == "done"
    body = (root / "PRODUCT-RESULTS.md").read_text(encoding="utf-8")
    assert "not asserted" in body.lower()


def test_entrypoint_is_run_and_can_pass(plugin, tmp_path):
    """When a real entrypoint exits cleanly, runtime checks can pass and the
    verdict is READY — proving the path is not hardwired to NOT READY."""
    root = tmp_path / "wk"
    acceptance = {"entrypoint": "true", "smoke": ["the build succeeds"]}
    _run(plugin, root, depth="product", acceptance=acceptance)
    body = (root / "PRODUCT-RESULTS.md").read_text(encoding="utf-8")
    assert "READY" in body and "NOT READY" not in body


def test_entrypoint_failure_is_not_ready(plugin, tmp_path):
    """A failing entrypoint (returncode != 0) yields NOT READY honestly."""
    root = tmp_path / "wk"
    acceptance = {"entrypoint": "false", "smoke": ["the build succeeds"]}
    _run(plugin, root, depth="product", acceptance=acceptance)
    body = (root / "PRODUCT-RESULTS.md").read_text(encoding="utf-8")
    assert "NOT READY" in body


def test_product_depth_completes_run(plugin, tmp_path):
    """Depth 'product' still drives the run to completion (L0 integrate done)."""
    root = tmp_path / "wk"
    res = _run(plugin, root, depth="product",
               acceptance={"smoke": ["starts"]})
    assert res.tasks["L0:integrate"].status == "done"
    not_done = [t.id for t in res.tasks.values() if t.status != "done"]
    assert not not_done, f"tasks left unfinished: {not_done}"


@pytest.mark.parametrize("depth", ["spec", "scaffold", "verify", "execute"])
def test_below_product_does_not_write_product_results(plugin, tmp_path, depth):
    """Depths below 'product' never emit PRODUCT-RESULTS.md, even when an
    acceptance block is present."""
    root = tmp_path / "wk"
    _run(plugin, root, depth=depth, acceptance={"smoke": ["starts"]})
    assert not (root / "PRODUCT-RESULTS.md").exists()
