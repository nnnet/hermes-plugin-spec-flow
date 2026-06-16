"""The integrate verdict is a REAL pytest run; smoke gates only the root."""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv        # noqa: E402
from harness_fakeapi import ok                   # noqa: E402

GREEN = "def test_ok():\n    assert True\n"
RED = "def test_no():\n    assert False\n"
SMOKE_RED = "def test_smoke():\n    assert False, 'product not assembled'\n"


def _ws(tmp_path, files):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_green_suite_is_pass(tmp_path):
    root = _ws(tmp_path, {"tests/test_a.py": GREEN})
    out = pv.make_verifier(max_repair=0)({"node": "n1",
                                          "workspace_root": root})
    assert out["status"] == "PASS"


def test_smoke_gates_only_the_root(tmp_path):
    root = _ws(tmp_path, {"tests/test_a.py": GREEN,
                          "tests/smoke/test_mvp_smoke.py": SMOKE_RED})
    branch = pv.make_verifier(max_repair=0)({"node": "catalog",
                                             "workspace_root": root})
    assert branch["status"] == "PASS", "partial tree must skip the smoke"
    rootv = pv.make_verifier(max_repair=0)({"node": "L0",
                                            "workspace_root": root})
    assert rootv["status"] == "FAIL", "the root integrate runs the smoke"


def test_red_suite_triggers_repair_and_repasses(tmp_path, fake_openai):
    root = _ws(tmp_path, {"tests/test_a.py": RED})
    # the repair patch is served by a real local server; the suite itself is a
    # real pytest run, so the green-up is genuine, not asserted into existence.
    fake_openai([(200, ok(json.dumps({"files": {"tests/test_a.py": GREEN}})))])
    out = pv.make_verifier(model="openrouter/x:free", max_repair=1)(
        {"node": "n1", "workspace_root": root})
    assert out["status"] == "PASS"
    assert "repair round" in out["detail"]


def test_unsafe_paths_are_refused(tmp_path):
    assert not pv._safe_rel("../escape.py")
    assert not pv._safe_rel("/abs/path.py")
    assert not pv._safe_rel("src/x.txt")
    assert pv._safe_rel("src/x.py") and pv._safe_rel("tests/test_x.py")


def test_worsening_repair_is_rolled_back(tmp_path, fake_openai):
    # the model "repair" breaks collection — the round must be undone
    root = _ws(tmp_path, {"tests/test_a.py": RED})
    fake_openai([(200, ok(json.dumps({"files": {
        "tests/test_a.py": "import missing_module\n"}})))])
    out = pv.make_verifier(model="openrouter/x:free", max_repair=1)(
        {"node": "n1", "workspace_root": root})
    assert out["status"] == "FAIL"
    body = (tmp_path / "tests" / "test_a.py").read_text(encoding="utf-8")
    assert body == RED, "the worsening write must be rolled back"


def test_protected_seed_files_refuse_writes(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PROTECTED_FILES",
                       json.dumps(["src/app.py", "tests/smoke/test_mvp_smoke.py"]))
    assert not pv._safe_rel("src/app.py"), "seeded skeleton must be immutable"
    assert pv._safe_rel("src/feature.py")


def test_implicated_falls_back_to_all_src_when_only_protected(tmp_path, monkeypatch):
    # the smoke (protected) fails on a feature handler; the traceback names
    # no writable file — repair must still receive the src modules
    monkeypatch.setenv("SPEC_FLOW_PROTECTED_FILES",
                       json.dumps(["tests/smoke/test_mvp_smoke.py"]))
    _ws(tmp_path, {"src/feature.py": "def h(payload):\n    return 201, {}\n",
                   "tests/smoke/test_mvp_smoke.py": SMOKE_RED})
    out = "FAILED tests/smoke/test_mvp_smoke.py::test_x - TypeError: h()..."
    files = pv._implicated_files(str(tmp_path), out)
    assert "src/feature.py" in files
    assert "tests/smoke/test_mvp_smoke.py" not in files


def test_readonly_context_inlines_protected_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PROTECTED_FILES",
                       json.dumps(["tests/smoke/test_mvp_smoke.py"]))
    _ws(tmp_path, {"tests/smoke/test_mvp_smoke.py": "EXPECTED-API-MARKER"})
    out = "FAILED tests/smoke/test_mvp_smoke.py::test_x - AssertionError"
    ro = pv._readonly_context(str(tmp_path), out)
    assert "EXPECTED-API-MARKER" in ro and "READ-ONLY" in ro


def test_platform_internal_mutations_are_refused():
    assert not pv.content_allowed("def t():\n    db._SCHEMAS.clear()\n")
    assert not pv.content_allowed("registry.ROUTES.clear()")
    assert pv.content_allowed("import db\ndb.register_schema('CREATE...')\n")
