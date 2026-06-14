"""The autonomous HITL dispatcher must replay a declarative `injections:` script
without a human: fire a late requirement only once its trigger is met, copy the
requirement folder verbatim into <run>/hitl/requirements/<name>/, and answer a
blocked worker from the declared answers. It writes the SAME files a human would
and never touches the engine."""
import json
import time
from pathlib import Path

import pytest

import hitl_dispatcher  # tests/lib is on sys.path (conftest)

_REQ = {"requirements": [
            {"name": "web_ui",
             "when": {"integrate_passes": 2},
             "statement": "MINIMAL WEB INTERFACE — serve GET /ui as an HTML page."}],
        "answers": {"default": "do the simplest correct thing",
                    "faq": [{"match": "port", "reply": "any free localhost port"}]}}


def _append(trace: Path, **ev):
    with open(trace, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")


def _wait(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "hitl").mkdir()
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    return tmp_path


def test_requirement_fires_only_after_trigger(run_dir):
    trace = run_dir / "trace.jsonl"
    req = run_dir / "hitl" / "requirements" / "web_ui"
    d = hitl_dispatcher.Dispatcher(run_dir, _REQ)
    assert d.enabled()
    d.start()
    try:
        _append(trace, gate="integrate_verify", verdict="PASS")
        # one PASS is below the threshold — must NOT materialise
        assert not _wait(lambda: (req / "REQUIREMENT.md").exists(), timeout=2.0)
        _append(trace, gate="integrate_verify", verdict="PASS")
        # second PASS crosses integrate_passes:2 — the requirement materialises
        # from the INLINE prose: statement → REQUIREMENT.md, and NOTHING else
        assert _wait(lambda: (req / "REQUIREMENT.md").exists())
        assert "MINIMAL WEB INTERFACE" in (req / "REQUIREMENT.md").read_text()
        # the human hands no test — building+proving the feature is the system's job
        assert not (req / "test_web_ui.py").exists()
    finally:
        d.stop()


def test_worker_question_is_answered_from_faq(run_dir):
    d = hitl_dispatcher.Dispatcher(run_dir, _REQ)
    d.start()
    try:
        (run_dir / "hitl" / "questions.md").write_text(
            "## 10:00 implementer @ n1 asks: which port should the service bind?\n",
            encoding="utf-8")
        ans = run_dir / "hitl" / "answer.md"
        assert _wait(lambda: ans.exists())
        assert ans.read_text(encoding="utf-8") == "any free localhost port"
    finally:
        d.stop()


def test_disabled_when_no_injections(run_dir):
    assert not hitl_dispatcher.Dispatcher(run_dir, None).enabled()
    assert not hitl_dispatcher.Dispatcher(run_dir, {}).enabled()
