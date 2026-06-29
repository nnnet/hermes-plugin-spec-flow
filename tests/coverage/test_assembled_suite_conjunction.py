"""Phase 7 — honest conjunction: the assembled product's FULL test suite is the
final authority for READY. A red corpus must surface as failing test ids, so the
terminal verdict becomes NOT READY (the v119 false green: narrow acceptance green
over a corpus where a /ui ordering test was red).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine_with_ws(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    (tmp_path / "wk" / "tests").mkdir(parents=True, exist_ok=True)
    return eng, pathlib.Path(ws.root)


def test_red_corpus_returns_failing_ids(tmp_path):
    eng, root = _engine_with_ws(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert 1 == 1\n"
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    fails = eng._assembled_suite_failures()
    assert fails, "a failing test must be reported"
    assert any("test_bad" in f for f in fails)


def test_green_corpus_returns_empty(tmp_path):
    eng, root = _engine_with_ws(tmp_path)
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    assert eng._assembled_suite_failures() == []


def test_no_tests_dir_returns_none(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk2"), depth=sfr.DEPTH_SPEC)
    eng.workspace.enabled = True
    eng.workspace.root = str(tmp_path / "wk2")
    (tmp_path / "wk2").mkdir(parents=True, exist_ok=True)
    assert eng._assembled_suite_failures() is None
