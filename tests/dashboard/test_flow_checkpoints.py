"""Checkpoints are run-wide MARKERS on the execution-flow time axis (a ★ on the
Y axis + a dashed line across the whole chart), NOT a lane/flow of their own."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
import live_dashboard as d   # noqa: E402


def _ev(t, phase, task, action="x", **kw):
    return {"level": 1, "t": float(t), "phase": phase, "task": task,
            "action": action, **kw}


def test_checkpoints_render_as_axis_stars_not_lane_boxes():
    events = [
        _ev(100, "decompose", "L0", "built"),
        _ev(101, "integrate", "checkpoint", "checkpoint saved"),
        _ev(102, "review", "notes_api", "reviewed", verdict="PASS"),
        _ev(103, "integrate", "checkpoint", "checkpoint saved"),
    ]
    tree = {"id": "L0", "children": [{"id": "notes_api", "children": []}]}
    html = d._flow_timeaxis(events, tree)
    # one ★ + one dashed line per checkpoint, across the whole chart
    assert html.count("★") == 2
    assert html.count("dashed") == 2
    # the checkpoint is NOT drawn as a milestone box in a lane
    assert "checkpoint</b>" not in html and "· checkpoint<" not in html
    # real milestones still render as boxes
    assert "notes_api" in html


def test_checkpoint_only_flow_is_empty():
    # nothing but checkpoints -> no flow chart at all (they are overlays)
    events = [_ev(100, "integrate", "checkpoint", "checkpoint saved")]
    assert d._flow_timeaxis(events, {"id": "L0", "children": []}) is None
