"""#8 (П8): auto-spike for hard nodes. A node with many open decisions or
high estimated LOC gets a research spike before freeze even when the
decomposer didn't request one; the researcher role (config'd to a stronger
free model) runs it. Off by default; a decomposer-authored spike always wins."""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_HARD_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 600,
              "open_decisions": 5, "single_concern": True,
              "testable_criteria": True}
_EASY_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 50,
              "open_decisions": 0, "single_concern": True,
              "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 1,
           "human_in_loop": False, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


def _engine(auto_spike):
    e = eng.Engine(workspace=tempfile.mkdtemp())
    # drive __init__-equivalent thresholds the way run() would
    e._spike_open = int((auto_spike or {}).get("open_decisions", 0) or 0)
    e._spike_loc = int((auto_spike or {}).get("estimated_loc", 0) or 0)
    return e


def test_off_by_default():
    e = eng.Engine(workspace=tempfile.mkdtemp())
    assert e._spike_open == 0 and e._spike_loc == 0


def test_hard_node_by_open_decisions_gets_spike():
    e = _engine({"open_decisions": 3})
    node = {"id": "auth", "title": "Auth flow", "metrics": dict(_HARD_LEAF)}
    # mimic the auto-spike decision the engine makes in _visit
    if not node.get("spike") and (e._spike_open or e._spike_loc):
        m = node["metrics"]
        hard = ((e._spike_open and m["open_decisions"] >= e._spike_open)
                or (e._spike_loc and m["estimated_loc"] >= e._spike_loc))
        if hard:
            node["spike"] = {"question": "x", "recommendation": "y"}
    assert "spike" in node


def test_easy_node_gets_no_spike():
    e = _engine({"open_decisions": 3, "estimated_loc": 400})
    m = dict(_EASY_LEAF)
    hard = ((e._spike_open and m["open_decisions"] >= e._spike_open)
            or (e._spike_loc and m["estimated_loc"] >= e._spike_loc))
    assert not hard


def test_loc_threshold_triggers():
    e = _engine({"estimated_loc": 400})
    m = dict(_HARD_LEAF)   # 600 loc
    hard = ((e._spike_open and m["open_decisions"] >= e._spike_open)
            or (e._spike_loc and m["estimated_loc"] >= e._spike_loc))
    assert hard


# ─── end-to-end: a hard leaf triggers the researcher ──────────────────

def _decomp(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BRANCH), "children": [
            {"id": "hard_one", "title": "Hard one"},
            {"id": "easy_one", "title": "Easy one"}]}
    return {"metrics": dict(_HARD_LEAF if ctx["node"]["id"] == "hard_one"
                            else _EASY_LEAF)}


def test_engine_auto_spikes_hard_leaf_only(tmp_path):
    researched = []

    def researcher(ctx):
        researched.append(ctx["node"].split(":")[0])
        return {"recommendation": "do X"}

    def impl(ctx):
        ws = ctx["workspace"]
        fn = ctx["module"]
        ws._write(f"src/{fn}.py", "X = 1\n", "code")
        ws._write(f"tests/test_{fn}.py", "def test_x():\n    assert True\n", "test")

    e = eng.Engine(workspace=str(tmp_path / "wk"), depth="execute",
                   agents={"decomposer": _decomp, "implementer": impl,
                           "researcher": researcher})
    # auto_spike threshold travels in the project dict (run() reads it)
    e.run({"name": "c", "goal": "g", "target": "x", "policy": _POLICY,
           "auto_spike": {"open_decisions": 3}})
    assert "hard_one" in researched
    assert "easy_one" not in researched
