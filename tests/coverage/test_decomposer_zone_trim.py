"""433: the decomposer's view is trimmed to the node's ZONE (ancestors + the
subtree under its parent); the rest of the project is referenced by count, not
enumerated. Conservative: when everything fits under the cap the list is
unchanged, so small trees are byte-identical."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine   # noqa: E402


def _engine(registry, cap):
    e = Engine.__new__(Engine)
    e._goal, e._target, e._constitution = "g", "t", []
    e._node_registry = dict(registry)
    e._project_meta = {"workers": {"decomposer_zone_cap": cap}}
    return e


def test_small_tree_unchanged():
    reg = {"a": "A", "b": "B", "c": "C"}
    e = _engine(reg, cap=150)            # under cap -> no trim
    ctx = e._decomposer_ctx({"id": "b"}, depth=2, parent="a", ancestors=(("a", "A"),))
    ids = {n["id"] for n in ctx["existing_nodes"]}
    assert ids == {"a", "b", "c"}
    assert "other_nodes_count" not in ctx


def test_large_tree_keeps_zone_and_counts_rest():
    reg = {"par": "Parent", "par.x": "Child X", "self": "Self",
           "anc": "Ancestor",
           "far1": "Far 1", "far2": "Far 2", "far3": "Far 3", "far4": "Far 4"}
    e = _engine(reg, cap=4)              # 8 nodes > cap -> trim to zone
    ctx = e._decomposer_ctx({"id": "self"}, depth=3, parent="par",
                            ancestors=(("anc", "Ancestor"), ("par", "Parent")))
    ids = {n["id"] for n in ctx["existing_nodes"]}
    # zone = ancestors (anc, par) + self + parent-subtree (par.x) all kept
    assert {"anc", "par", "par.x", "self"} <= ids
    # the cap is respected and the remainder is summarised, not enumerated
    assert len(ctx["existing_nodes"]) <= 4 + 1   # zone may exceed cap slightly
    assert ctx.get("other_nodes_count", 0) >= 1
    # at least one far node is NOT enumerated
    assert not {"far1", "far2", "far3", "far4"} <= ids
