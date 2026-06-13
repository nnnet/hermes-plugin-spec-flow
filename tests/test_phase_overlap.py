"""П3 measurement tool: phase_overlap reconstructs leaf concurrency and the
implement∩review pipeline overlap from a trace, proving the pipeline effect
without new engine logic."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import phase_overlap as po  # noqa: E402


def _trace(tmp_path, rows):
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _ev(t, task, phase):
    return {"t": t, "task": task, "phase": phase, "verdict": "", "level": 1}


# ─── span + overlap math ──────────────────────────────────────────────

def test_base_node_strips_role_suffix():
    assert po._base_node("cart_api:impl") == "cart_api"
    assert po._base_node("cart_api:review") == "cart_api"
    assert po._base_node("cart_api") == "cart_api"


def test_overlap_of_disjoint_intervals_is_zero():
    assert po._overlap((0, 5), (10, 15)) == 0.0
    assert po._overlap((0, 10), (5, 15)) == 5.0
    assert po._overlap(None, (0, 5)) == 0.0


# ─── sequential run: NO phase overlap ─────────────────────────────────

def test_sequential_run_has_no_overlap(tmp_path):
    # leaf A fully (impl 0-5, review 5-10) THEN leaf B (impl 10-15, rev 15-20)
    rows = [
        _ev(0, "a:impl", "implement"), _ev(5, "a:impl", "implement"),
        _ev(5, "a:review", "review"), _ev(10, "a:review", "review"),
        _ev(10, "b:impl", "implement"), _ev(15, "b:impl", "implement"),
        _ev(15, "b:review", "review"), _ev(20, "b:review", "review"),
    ]
    rep = po.analyze(_trace(tmp_path, rows))
    assert rep["leaves"] == 2
    assert rep["peak_concurrency"] == 1      # never two at once
    assert rep["phase_overlap_s"] == 0.0     # no pipeline effect
    assert rep["saved_s"] == 0.0


# ─── pipelined run: implement of B overlaps review of A ───────────────

def test_pipelined_run_reports_overlap(tmp_path):
    # A: impl 0-10, review 10-20 ;  B starts impl at 12 (while A is in review)
    rows = [
        _ev(0, "a:impl", "implement"), _ev(10, "a:impl", "implement"),
        _ev(10, "a:review", "review"), _ev(20, "a:review", "review"),
        _ev(12, "b:impl", "implement"), _ev(22, "b:impl", "implement"),
        _ev(22, "b:review", "review"), _ev(30, "b:review", "review"),
    ]
    rep = po.analyze(_trace(tmp_path, rows))
    assert rep["peak_concurrency"] == 2
    # A.review [10,20] ∩ B.impl [12,22] = 8s
    assert rep["phase_overlap_s"] == 8.0
    assert rep["saved_s"] > 0
    assert rep["overlapping_pairs"][0]["reviewA_implB_s"] == 8.0


# ─── degenerate inputs never crash ────────────────────────────────────

def test_empty_trace(tmp_path):
    rep = po.analyze(_trace(tmp_path, []))
    assert rep["leaves"] == 0 and rep["peak_concurrency"] == 0


def test_structural_events_are_ignored(tmp_path):
    # decompose/integrate carry no leaf impl/review phases → not counted
    rows = [_ev(0, "L0:decompose", "decompose"),
            _ev(1, "root:integrate", "integrate")]
    rep = po.analyze(_trace(tmp_path, rows))
    assert rep["leaves"] == 0
