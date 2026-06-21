"""evStatus (client JS) must mark a reworked-then-green node as FIXED, not leave
its triggering fails as 'незакрытые проблемы'. Regression for the note_search
case: spec_scope FAIL has no same-gate PASS (the gate emits only on failure);
its resolution is a downstream spec_review PASS. The real source JS is extracted
and run under node so the test pins the shipped function, not a copy."""
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "lib" / "live_dashboard.py"
node = shutil.which("node")
pytestmark = pytest.mark.skipif(not node, reason="node not available")


def _extract(name: str) -> str:
    txt = _SRC.read_text(encoding="utf-8")
    m = re.search(r"(function " + name + r"\(.*?\n\})", txt, re.S)
    assert m, f"{name} not found in dashboard source"
    return m.group(1)


def _status(e, allev):
    js = (_extract("evBad") + "\n" + _extract("evStatus") + "\n"
          + "const e=" + json.dumps(e) + ";const all=" + json.dumps(allev) + ";"
          + "process.stdout.write(evStatus(e,all));")
    return subprocess.run([node, "-e", js], capture_output=True, text=True,
                          timeout=20).stdout.strip()


def test_spec_scope_fail_fixed_by_later_spec_review_pass():
    fail = {"tick": 108, "gate": "spec_scope", "verdict": "FAIL"}
    allev = [fail,
             {"tick": 109, "gate": "doctor:empty_delta", "verdict": "PASS"},
             {"tick": 112, "gate": "spec_review", "verdict": "PASS"}]
    assert _status(fail, allev) == "fixed"


def test_spec_scope_fail_stays_open_without_later_pass():
    fail = {"tick": 108, "gate": "spec_scope", "verdict": "FAIL"}
    assert _status(fail, [fail]) == "open"


def test_same_gate_later_pass_is_fixed():
    fail = {"tick": 5, "gate": "integrate_verify", "verdict": "FAIL"}
    allev = [fail, {"tick": 9, "gate": "integrate_verify", "verdict": "PASS"}]
    assert _status(fail, allev) == "fixed"


def test_open_fail_not_masked_by_unrelated_pass():
    # an integrate FAIL must NOT be cleared by a spec_review PASS (different phase)
    fail = {"tick": 20, "gate": "integrate_verify", "verdict": "FAIL"}
    allev = [{"tick": 12, "gate": "spec_review", "verdict": "PASS"}, fail]
    assert _status(fail, allev) == "open"
