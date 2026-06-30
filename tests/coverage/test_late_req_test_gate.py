"""Phase 5 (deterministic) — a LATE requirement must SHIP a new test of its own.

Compared to the test-function names captured before the leaf implemented, a NEW
``def test_*`` must appear. If none does, the requirement is unverifiable (the
v119 note_search hole: it amended get_notes, added no test, the dropped filter
went unnoticed) -> the delta gate fails it as an empty delta.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    root = pathlib.Path(ws.root)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    # a real, non-empty owner module with NO declared route (route check skipped)
    (root / "src" / "thing.py").write_text(
        "def do_thing():\n    return 1\n", encoding="utf-8")
    # isolate the gate from the doctor/trace plumbing
    eng.emit = lambda *a, **k: None
    eng._doctor_advise = lambda *a, **k: None
    eng._doctor_resolve = lambda *a, **k: None
    eng.loops = []
    eng._doctor_states = {}
    eng.review_policy = {"min_delta_lines": 1}
    return eng, root


def test_func_names_collects_tests(tmp_path):
    eng, root = _engine(tmp_path)
    (root / "tests" / "test_a.py").write_text(
        "def test_one():\n    pass\ndef test_two():\n    pass\n", encoding="utf-8")
    assert eng._test_func_names() == {"test_one", "test_two"}


def test_late_req_without_new_test_fails(tmp_path):
    eng, root = _engine(tmp_path)
    node = {"id": "late1", "_late_req": True,
            "_test_baseline": sorted(eng._test_func_names())}   # baseline = {} (no tests)
    # leaf implemented code but shipped NO test
    ok = eng._late_req_delta_gate(node, "late1", 1, "src/thing.py")
    assert ok is False
    assert any(l.get("type") == "empty-delta" for l in eng.loops)


def test_late_req_with_new_test_passes(tmp_path):
    eng, root = _engine(tmp_path)
    node = {"id": "late1", "_late_req": True,
            "_test_baseline": sorted(eng._test_func_names())}   # baseline = {}
    # the leaf shipped a NEW test after the baseline snapshot
    (root / "tests" / "test_thing.py").write_text(
        "def test_thing_behaviour():\n    assert True\n", encoding="utf-8")
    ok = eng._late_req_delta_gate(node, "late1", 1, "src/thing.py")
    assert ok is True
    assert not any(l.get("type") == "empty-delta" for l in eng.loops)


def test_non_late_node_unaffected(tmp_path):
    eng, root = _engine(tmp_path)
    node = {"id": "plain"}            # no _late_req, no baseline
    ok = eng._late_req_delta_gate(node, "plain", 1, "src/thing.py")
    assert ok is True


def test_baseline_is_json_serializable(tmp_path):
    # the node is persisted to meta/checkpoints — a set baseline crashed the
    # JSON writer at end of run (v121). The stored form must be a plain list.
    import json
    eng, root = _engine(tmp_path)
    (root / "tests" / "test_a.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8")
    node = {"id": "late1", "_late_req": True,
            "_test_baseline": sorted(eng._test_func_names())}
    json.dumps(node)            # must not raise
    assert isinstance(node["_test_baseline"], list)
