"""Audit stage S46 (node Q7, plan 2026-07-06T20-15): the realized tree must be
visible to the IR build DURING decomposition, not only after it. Root cause of
the v168 false-hollow on L0: the decomposer builds the tree by attaching
children to `root` in place, but `project["tree"] = root` was published only
AFTER `_visit` returned — while the incremental IR writes (`_write_ir` /
`_write_ir_incremental`) fire INSIDE `_visit`, at leaf realization. At that
moment `_tree_nodes(engine)` read an empty tree, so every branch (L0 included)
serialized childless, and the Q2 hollow-spec check (S38) correctly flagged a
childless carrier-less node as hollow — a FALSE red born of a stale tree datum.

  * S46.1 — `_tree_nodes` reads the tree from `_project_meta["tree"]`; a node
    with children there classifies as a BRANCH, so `_hollow_node_reason`
    returns None (a branch is never hollow).
  * S46.2 — the realized tree is PUBLISHED to `project["tree"]` BEFORE
    `self._visit(root, …)`, so the in-place-growing tree is visible to every
    incremental IR write during decomposition (`root` is mutated in place, so
    the pre-visit publish stays valid as children are attached).

Deterministic: source-order assertion + pure classifier, no run, no network.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_ir  # noqa: E402


# ── S46.1: a branch in the tree is never hollow ─────────────────────────────

def test_branch_with_children_is_not_hollow():
    # L0 is an empty wrapper (no carrier) but has children -> a branch.
    l0 = {"children": ["db", "core"]}
    assert spec_ir._node_class(l0) == "branch"
    assert spec_ir._hollow_node_reason("L0", l0) is None, \
        "a branch delegates to children and must never be flagged hollow"


def test_childless_carrierless_node_is_hollow():
    # the SAME empty node WITHOUT the children link IS hollow — proving the
    # verdict hinges on the tree datum being present.
    orphan = {}
    assert spec_ir._hollow_node_reason("L0", orphan) is not None, \
        "a carrier-less node with no children is genuinely hollow"


def test_tree_nodes_reads_project_meta_tree():
    class _Eng:
        _project_meta = {"tree": {"id": "L0", "children": [
            {"id": "db"}, {"id": "core"}]}}
    tree = spec_ir._tree_nodes(_Eng())
    assert "L0" in tree and [c["id"] for c in tree["L0"]["children"]] == \
        ["db", "core"], "the realized tree must expose L0's children"


# ── S46.2: the tree is published BEFORE the visit ───────────────────────────

def test_tree_published_before_visit():
    src = pathlib.Path(
        pathlib.Path(__file__).resolve().parents[2] / "spec_flow_runner.py"
    ).read_text(encoding="utf-8")
    publish = src.find('project["tree"] = root')
    visit = src.find("self._visit(root, depth=0")
    assert publish != -1 and visit != -1, "both anchors must exist"
    assert publish < visit, (
        "the realized tree must be published to project['tree'] BEFORE "
        "self._visit(root, …) so incremental IR writes during decomposition "
        "see the growing tree (else branches serialize childless -> false "
        "hollow)")
