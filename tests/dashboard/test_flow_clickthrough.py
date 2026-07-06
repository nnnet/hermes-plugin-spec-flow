"""Flow-tab (and other nid sites) click through to the node's spec (node M4, S30).

Why: the flow tab's column headers ARE branch-node ids (L0, db, core, ...) and
the milestone boxes carry a node id (task), yet a reader who spots a branch in
the flow had no way to jump to that node's spec — the id was inert text. The
tree already navigates on click (SEL=id; NTAB='spec'; render()); the flow tab,
the flow milestones and the IR node heading did NOT. A node id shown anywhere
the jump is meaningful should be a click-through to its spec, via ONE shared
client helper (not a per-site re-implementation). This audit reds on the current
code (no navigation hook on the flow headers / boxes / IR heading, no shared
helper) and greens once every chosen nid site carries the hook and the page
exposes the single navigation helper.

What: exercises `_flow_timeaxis` (column headers + milestone boxes),
`_ir_node_html` (node heading) and asserts the shared `goToNodeSpec` helper and
its delegated `data-gospec` handler live in `_PAGE`. The nav hook is the
`data-gospec="<nid>"` attribute carrying the CORRECT node id that the client
resolves to that node's spec (SEL=<nid>, spec tab).
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


# A two-lane tree: L0 root with two L1 branches (db, core). core owns a leaf.
_TREE = {
    "id": "L0",
    "children": [
        {"id": "db", "children": []},
        {"id": "core", "children": [{"id": "req_a54f9144", "children": []}]},
    ],
}
# Milestone events (level<=1, real timestamps, task=<nid>) — one per branch.
_EVENTS = [
    {"level": 1, "t": 1000.0, "task": "db", "phase": "implement", "action": "x"},
    {"level": 1, "t": 1001.0, "task": "core", "phase": "implement", "action": "y"},
    {"level": 1, "t": 1002.0, "task": "req_a54f9144:leaf", "phase": "test",
     "action": "z", "verdict": "PASS"},
]


def _gospec_ids(html: str) -> list[str]:
    """Collect every data-gospec="..." target id present in an HTML fragment."""
    return re.findall(r'data-gospec="([^"]+)"', html)


# ── (a) THE headline requirement: flow-tab column headers jump to the spec ────

def test_flow_column_headers_carry_nav_hook_to_node_spec():
    """S30.1 RED: each branch column header on the flow time-axis carries a
    `data-gospec="<nid>"` navigation hook whose id is the branch node id — a
    click on the 'core' header lands on the spec of node 'core'."""
    html = dash._flow_timeaxis(_EVENTS, _TREE)
    assert html, "flow time-axis produced no fragment"
    ids = _gospec_ids(html)
    for lane in ("db", "core"):
        assert lane in ids, (
            "flow column header %r carries no click-through to its node spec "
            "(no data-gospec=%r)" % (lane, lane))


# ── (b) the same treatment where a nid is meaningful: milestones + IR heading ─

def test_flow_milestone_boxes_carry_nav_hook_to_their_node_spec():
    """S30.2: a milestone box's node id is itself a jump target — clicking the
    box lands on that node's spec. The box for the leaf event resolves to the
    bare node id (task before ':'), never the ':leaf' suffix."""
    html = dash._flow_timeaxis(_EVENTS, _TREE)
    ids = _gospec_ids(html)
    assert "req_a54f9144" in ids, (
        "milestone box carries no click-through to its node spec")
    assert "req_a54f9144:leaf" not in ids, (
        "nav hook must use the bare node id, not the task:role suffix")


def test_ir_node_heading_carries_nav_hook_to_node_spec():
    """S30.3: the IR tab renders each node under a heading that is its id; that
    heading is a click-through to the same node's spec panel."""
    node = {"openapi": {"openapi": "3.1.0",
                        "info": {"title": "x", "version": "1"}, "paths": {}}}
    html = dash._ir_node_html("core", node)
    assert 'data-gospec="core"' in html, (
        "IR node heading carries no click-through to the node spec")


# ── (c) ONE shared client helper, not a per-site re-implementation ────────────

def test_page_exposes_single_shared_navigation_helper():
    """S30.4: the client page defines ONE `goToNodeSpec` helper and a delegated
    `data-gospec` handler — every nid site reuses the same door (SEL=<nid>,
    spec tab), so navigation behaviour cannot drift between sites."""
    page = dash._PAGE
    # the helper is DEFINED (not merely called) — a bare call site would leave a
    # ReferenceError on click, so pin the definition itself.
    assert "function goToNodeSpec" in page, (
        "client page lacks the shared goToNodeSpec navigation helper definition")
    # wired through the ONE delegated document click handler on data-gospec
    assert "closest('[data-gospec]')" in page, (
        "client page has no delegated handler for the data-gospec nav hook")
