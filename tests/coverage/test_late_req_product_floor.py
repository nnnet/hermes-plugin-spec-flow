"""/ui orphan repro — a late standing requirement that arrives DURING the
engine-synthesized assembly node must still materialize as a root child.

Background (live v131/v132): at product depth the small-product floor (#122/#123)
collapses a micro-service to ONE ``core`` leaf and the engine appends ONE
``product_entry`` assembly leaf. The root reads standing requirements EXACTLY
ONCE — in the depth-0 child-placement window, which runs AFTER the ``core`` child
but BEFORE the assembly leaf. The live HITL dispatcher fires ``web_ui`` off a
trace event emitted around assembly, so in the fast floor path ``web_ui`` becomes
available only AFTER that single window: ``GET /ui`` enters the product contract
(boot-gate RED) yet no leaf owns it → an orphan 404, and the run never reaches an
honest READY.

This test mirrors the live dispatcher deterministically: a ``sink`` flips the
requirement ON when the ``product_entry`` node is first visited (its decompose
event), i.e. strictly after the placement window. The fix re-polls standing
requirements at root integrate and materializes any uncovered one before the
verdict, so ``web_ui`` becomes a root child. Asserting that materialization is
RED before the fix and GREEN after.

The spec-depth injection tests pass because there is no post-placement assembly
node there — the bug is specific to product depth + floor.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import spec_flow_runner as sfr  # noqa: E402


def _all_node_ids(tree):
    """Every node id in the realized tree (BFS)."""
    out, stack = [], [tree]
    while stack:
        n = stack.pop()
        out.append(n.get("id"))
        stack.extend(n.get("children", []) or [])
    return out


def _children_ids(tree, node_id):
    stack = [tree]
    while stack:
        n = stack.pop()
        if n.get("id") == node_id:
            return [c.get("id") for c in (n.get("children", []) or [])]
        stack.extend(n.get("children", []) or [])
    return []


# A micro-service contract in PURE HUMAN TEXT: 3 base routes (<= floor threshold
# 5) so the small-product floor collapses the product to one core leaf, and a
# named entry+callable so the engine synthesizes the product_entry assembly leaf.
_GOAL = ("Build a notes micro-service. GET /health returns 200. "
         "POST /notes creates a note and GET /notes lists the notes. "
         "src/app.py exposes wsgi_app.")


def _project():
    return {
        "id": "L0",
        "goal": _GOAL,
        "target": "a runnable notes micro-service",
        "constitution": [_GOAL],
        # a single bare root node — the floor forces it into a branch with one
        # core child (no children/atomic declared here so the floor owns it).
        "tree": {"id": "L0", "title": "Notes service",
                 "metrics": {"open_decisions": 0, "estimated_loc": 40,
                             "modules": 1, "tasks": 3, "interfaces": 3,
                             "single_concern": True,
                             "testable_criteria": True}},
        "small_product_routes": 5,
    }


def _implementer(ctx):
    # trivial offline builder: write ONE stub module per leaf so the
    # product-depth run proceeds without a provider. Content is irrelevant — the
    # orphan is about NODE materialization (we inspect tasks/tree, not files).
    ws = ctx["workspace"]
    module = ctx.get("module") or ctx.get("node") or "mod"
    ws._write("src/%s.py" % module, "def noop():\n    return None\n", "code")


def _run_with_late_web_ui(tmp_path, monkeypatch):
    # SPEC_FLOW_PRE_GATE on so the engine appends the product_entry assembly leaf.
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")

    # The web_ui requirement is OFF until the assembly node is reached — mirrors
    # the live dispatcher, which fires off a trace event around assembly. The
    # placement window has already passed by then.
    box = {"on": False}

    def sink(ev):
        # the product_entry node's first (decompose) event runs strictly after
        # the depth-0 placement window — flip the requirement ON there.
        if getattr(ev, "task", "") == "product_entry":
            box["on"] = True

    def standing():
        if box["on"]:
            return [("web_ui",
                     "GET /ui returns an HTML page rendered in the browser",
                     None)]
        return []

    return sfr.run_project(
        _project(), workspace=str(tmp_path / "wk"),
        depth=sfr.DEPTH_PRODUCT, sink=sink,
        agents={"implementer": _implementer},
        standing_requirements=standing)


def test_floor_collapses_to_core_plus_assembly(tmp_path, monkeypatch):
    # guardrail for the repro setup: the floor must actually fire (core leaf) and
    # the assembly leaf must be appended — otherwise the repro is vacuous.
    res = _run_with_late_web_ui(tmp_path, monkeypatch)
    ids = _all_node_ids(res.project["tree"])
    assert "core" in ids, ids
    assert "product_entry" in ids, ids


def test_late_web_ui_at_assembly_still_materializes(tmp_path, monkeypatch):
    # the orphan: web_ui arrives during assembly (after the single placement
    # window). It MUST still become a root child so /ui has an owning leaf.
    res = _run_with_late_web_ui(tmp_path, monkeypatch)
    assert "web_ui" in res.tasks, sorted(res.tasks)
    assert "web_ui" in _children_ids(res.project["tree"], "L0"), \
        _children_ids(res.project["tree"], "L0")
