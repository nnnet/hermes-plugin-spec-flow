"""Late-injected requirement nodes must be VISIBLE. The engine adds them at
root integrate, not as decomposer children, so the decompose-based live tree
never knows them — _attach_orphan_nodes re-attaches any node that ran (has
lifecycle events) but is missing from the tree, under the root, marked
'attached' so the UI badges it."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _ev(task, phase="", gate="", **kw):
    return {"task": task, "phase": phase, "gate": gate, **kw}


def test_orphan_with_lifecycle_events_is_attached():
    tree = {"id": "L0", "children": [{"id": "cart", "children": []}]}
    events = [_ev("web_ui", phase="review"),
              _ev("web_ui:integrate", phase="integrate")]
    added = dash._attach_orphan_nodes(tree, events)
    assert added == ["web_ui"]
    kids = {c["id"]: c for c in tree["children"]}
    assert "web_ui" in kids and kids["web_ui"]["attached"] is True


def test_known_nodes_are_not_reattached():
    tree = {"id": "L0", "children": [{"id": "cart", "children": []}]}
    events = [_ev("cart", phase="implement"), _ev("cart:review", phase="review")]
    assert dash._attach_orphan_nodes(tree, events) == []
    assert len(tree["children"]) == 1


def test_structural_noise_is_not_attached():
    # a task id that only appears in non-node phases (decompose) is not a node
    tree = {"id": "L0", "children": []}
    events = [_ev("L0", phase="decompose"), _ev("", phase="review")]
    assert dash._attach_orphan_nodes(tree, events) == []


def test_attach_dedupes_and_preserves_order():
    tree = {"id": "L0", "children": []}
    events = [_ev("web_ui", phase="review"),
              _ev("seller_directory", phase="implement"),
              _ev("web_ui:integrate", phase="integrate")]
    added = dash._attach_orphan_nodes(tree, events)
    assert added == ["web_ui", "seller_directory"]


def test_tree_view_carries_attached_marker():
    tree = {"id": "L0", "children": [{"id": "web_ui", "children": [],
                                      "attached": True}]}
    meta = {}
    dash._flatten(tree, 0, meta, None)
    view = dash._tree_view(tree, meta)
    child = view["children"][0]
    assert child["id"] == "web_ui" and child.get("attached") is True
    # a normal node carries no marker
    assert "attached" not in view
