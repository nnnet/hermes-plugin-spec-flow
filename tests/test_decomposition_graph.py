"""Battle tests — decomposition graph per real project.

For each project fixture we walk the decomposition tree, classify every node
through the plugin's REAL ``leaf_check``, build the realized kanban DAG, and
compare the resulting branching graph against the project's ground-truth
expectations. On failure the assertion prints the actual ASCII tree so the
discrepancy is obvious.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import simulator as sim  # noqa: E402

PROJECTS = sim.all_projects()
IDS = [p.name for p in PROJECTS]


@pytest.fixture(params=PROJECTS, ids=IDS)
def project(request):
    return request.param


def _run(plugin, project):
    return sim.simulate(project, plugin.tools)


class TestDecompositionGraph:
    def test_verdicts_match_ground_truth(self, plugin, project):
        result = _run(plugin, project)
        assert not result.mismatches, (
            f"\nProject {project.name}: leaf_check verdicts diverge from the "
            f"expected design:\n  " + "\n  ".join(result.mismatches)
            + "\n\nActual tree:\n" + sim.render_tree(result)
        )

    def test_no_fixture_inconsistencies(self, plugin, project):
        result = _run(plugin, project)
        assert not result.fixture_errors, "\n".join(result.fixture_errors)

    def test_dag_summary_matches_expected(self, plugin, project):
        result = _run(plugin, project)
        exp = project.expect_dag
        got = result.summary
        diffs = [f"{k}: got {got[k]}, expected {v}" for k, v in exp.items() if got.get(k) != v]
        assert not diffs, (
            f"\nProject {project.name}: realized DAG differs:\n  " + "\n  ".join(diffs)
            + "\n\nTree:\n" + sim.render_tree(result)
        )

    def test_structural_invariants(self, plugin, project):
        result = _run(plugin, project)
        tasks = result.tasks
        for nid, verdict in result.decisions.items():
            if verdict == "branch":
                assert f"{nid}:integrate" in tasks, f"{nid} branch has no integrate node"
            else:
                assert f"{nid}:impl" in tasks, f"{nid} leaf has no impl task"
                assert f"{nid}:review" in tasks, f"{nid} leaf has no review task"
                # review depends on impl
                assert tasks[f"{nid}:review"].parents == [f"{nid}:impl"]

    def test_leaf_under_contract_depends_on_contract(self, plugin, project):
        result = _run(plugin, project)
        # every contract node must be a parent of at least one impl in its subtree
        contracts = [tid for tid, t in result.tasks.items() if t.kind == "contract"]
        for cid in contracts:
            dependent_impls = [
                t for t in result.tasks.values()
                if t.kind == "impl" and cid in t.parents
            ]
            assert dependent_impls, f"contract {cid} governs no implementation leaf"


class TestGateCatchesCoupling:
    """The gate must SPLIT a coupled step the architect was tempted to leaf."""

    def test_coupled_checkout_form_is_branched(self, plugin):
        project = next(p for p in PROJECTS if p.name == "ecommerce-checkout")
        result = _run(plugin, project)
        assert result.decisions["checkout_form"] == "branch"
        # ... specifically because it is not single-concern
        out = plugin.tools._handle_leaf_check(
            dict(_find(project.root, "checkout_form").metrics)
        )
        import json
        assert any("coupled" in r for r in json.loads(out)["reasons"])

    def test_open_decision_forces_expand(self, plugin):
        project = next(p for p in PROJECTS if p.name == "data-pipeline")
        result = _run(plugin, project)
        # 'transform' is small by LOC but has an open decision -> must branch
        assert result.decisions["transform"] == "branch"


def _find(node, nid):
    if node.id == nid:
        return node
    for c in node.children:
        hit = _find(c, nid)
        if hit:
            return hit
    return None
