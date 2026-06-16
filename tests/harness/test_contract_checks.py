"""Unit tests for the pre-integrate CONTRACT checks (B1) and the B2 boot-gate.

Why: these checks are the floor under the hollow-green class — they must fire
on the un-buildable / self-mocked / stubbed trees and stay SILENT on a clean
assembled product. A regression here re-opens the false-PASS hole.
What: drives each ``contract_checks`` function on synthetic ``src/``/``tests/``
trees (positive + negative) under ``tmp_path``, and the boot-gate on a tiny
real WSGI app (entry present + endpoints OK -> PASS; entry missing -> FAIL).
Test: hermetic — tmp_path only, no network, no LLM. The boot-gate spawns a
fresh python subprocess (same interpreter) but imports only the synthetic app.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import contract_checks as cc  # noqa: E402


def _src(tmp, name, body):
    d = tmp / "src"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def _test(tmp, name, body):
    d = tmp / "tests"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


# --- imported_but_unbuilt -------------------------------------------------

def test_imported_but_unbuilt_fires_on_missing_local_module(tmp_path):
    _src(tmp_path, "api.py", "import registry\n\ndef handle():\n    return 1\n")
    viol = cc.imported_but_unbuilt(str(tmp_path))
    assert any("registry" in v for v in viol), viol


def test_imported_but_unbuilt_silent_when_module_exists(tmp_path):
    _src(tmp_path, "registry.py", "def register(*a, **k):\n    return None\n")
    _src(tmp_path, "api.py", "import registry\n\ndef handle():\n    return 1\n")
    assert cc.imported_but_unbuilt(str(tmp_path)) == []


def test_imported_but_unbuilt_ignores_stdlib(tmp_path):
    _src(tmp_path, "api.py", "import json\nimport sqlite3\n\ndef h():\n    return json\n")
    assert cc.imported_but_unbuilt(str(tmp_path)) == []


# --- mocks_local_module ---------------------------------------------------

def test_mocks_local_module_fires_on_sysmodules_mock(tmp_path):
    _src(tmp_path, "registry.py", "def register(*a, **k):\n    return None\n")
    _test(tmp_path, "test_api.py",
          "import sys\nfrom unittest.mock import MagicMock\n"
          "sys.modules['registry'] = MagicMock()\n\n"
          "def test_ok():\n    assert True\n")
    viol = cc.mocks_local_module(str(tmp_path))
    assert any("registry" in v for v in viol), viol


def test_mocks_local_module_fires_on_patch_decorator(tmp_path):
    _src(tmp_path, "db.py", "def save(x):\n    return x\n")
    _test(tmp_path, "test_db.py",
          "from unittest.mock import patch\n\n"
          "@patch('db.save')\ndef test_x(m):\n    assert True\n")
    viol = cc.mocks_local_module(str(tmp_path))
    assert any("db" in v for v in viol), viol


def test_mocks_local_module_silent_on_stdlib_patch(tmp_path):
    _src(tmp_path, "db.py", "def save(x):\n    return x\n")
    _test(tmp_path, "test_db.py",
          "from unittest.mock import patch\n\n"
          "@patch('time.time')\ndef test_x(m):\n    assert True\n")
    assert cc.mocks_local_module(str(tmp_path)) == []


# --- stub_bodies ----------------------------------------------------------

def test_stub_bodies_fires_on_pass_only(tmp_path):
    _src(tmp_path, "h.py", "def handle():\n    pass\n")
    viol = cc.stub_bodies(str(tmp_path))
    assert any("handle" in v for v in viol), viol


def test_stub_bodies_fires_on_notimplemented_and_ellipsis(tmp_path):
    _src(tmp_path, "h.py",
         "def a():\n    raise NotImplementedError\n\n"
         "def b():\n    ...\n\n"
         'def c():\n    """doc only"""\n')
    viol = cc.stub_bodies(str(tmp_path))
    assert sum(any(n in v for v in viol) for n in ("a", "b", "c")) == 3, viol


def test_stub_bodies_silent_on_real_body(tmp_path):
    _src(tmp_path, "h.py", "def handle():\n    x = 1\n    return x + 1\n")
    assert cc.stub_bodies(str(tmp_path)) == []


def test_stub_bodies_ignores_private(tmp_path):
    _src(tmp_path, "h.py", "def _helper():\n    pass\n")
    assert cc.stub_bodies(str(tmp_path)) == []


# --- tests_collect --------------------------------------------------------

def test_tests_collect_passes_on_collectable_tree(tmp_path):
    _test(tmp_path, "test_ok.py", "def test_ok():\n    assert 1 == 1\n")
    assert cc.tests_collect(str(tmp_path)) == []


def test_tests_collect_fires_on_collection_error(tmp_path):
    _test(tmp_path, "test_bad.py", "import does_not_exist_local_module_xyz\n")
    viol = cc.tests_collect(str(tmp_path))
    assert viol and "collection" in viol[0].lower(), viol


# --- run_all aggregates ---------------------------------------------------

def test_run_all_silent_on_clean_tree(tmp_path):
    _src(tmp_path, "db.py", "def save(x):\n    return x\n")
    _test(tmp_path, "test_db.py",
          "import sys, pathlib\n"
          "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
          "import db\n\ndef test_save():\n    assert db.save(2) == 2\n")
    assert cc.run_all(str(tmp_path)) == []


def test_run_all_fires_on_hollow_green(tmp_path):
    _src(tmp_path, "api.py", "import registry\n\ndef handle():\n    pass\n")
    _test(tmp_path, "test_api.py",
          "import sys\nfrom unittest.mock import MagicMock\n"
          "sys.modules['registry'] = MagicMock()\n\n"
          "def test_ok():\n    assert True\n")
    assert cc.run_all(str(tmp_path)), "hollow-green tree must produce violations"


# --- constitution-keyed condition -----------------------------------------

def test_constitution_declares_entry_detected(tmp_path):
    rules = ["Standard library ONLY: HTTP through a WSGI app "
             "(src/app.py exposes wsgi_app)"]
    assert cc.constitution_declares_entry(rules) == "src/app.py"


def test_constitution_declares_entry_none_when_absent(tmp_path):
    assert cc.constitution_declares_entry([]) is None
    assert cc.constitution_declares_entry(["build a CLI tool"]) is None


def test_goal_wants_notes(tmp_path):
    assert cc.goal_wants_notes(["POST /notes accepts ..."]) is True
    assert cc.goal_wants_notes(["seller directory"], goal="a marketplace") is False


# --- B2 boot-gate ---------------------------------------------------------

_GOOD_APP = '''
import json
_NOTES = []
def wsgi_app(environ, start_response):
    m = environ["REQUEST_METHOD"]; p = environ["PATH_INFO"]
    def reply(code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        start_response(str(code) + " OK", [("Content-Type", ctype)])
        return [data]
    if p == "/health":
        return reply(200, {"ok": True})
    if p == "/ui":
        return reply(200, b"<html><body>app</body></html>", "text/html")
    if p == "/notes" and m == "POST":
        n = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        _NOTES.append({"id": len(_NOTES) + 1, "text": json.loads(n)["text"]})
        return reply(201, _NOTES[-1])
    if p == "/notes":
        return reply(200, {"items": list(reversed(_NOTES))})
    return reply(404, {"error": "no route"})
'''

_ENTRY_CONST = ["HTTP through a WSGI app (src/app.py exposes wsgi_app)",
                "POST /notes then GET /notes round-trips a note"]


def test_boot_gate_passes_on_assembled_app(tmp_path):
    _src(tmp_path, "app.py", _GOOD_APP)
    ok, detail = cc.boot_gate(str(tmp_path), _ENTRY_CONST)
    assert ok, detail


def test_boot_gate_discovers_entry_in_non_app_module(tmp_path):
    # The worker may name the WSGI module anything (e.g. wsgi_application.py)
    # instead of the constitution's src/app.py. The boot-gate must DISCOVER the
    # callable and still boot+drive it — a runnable product is green regardless
    # of filename. Guards the v014 escape (entry was wsgi_application.py).
    _src(tmp_path, "wsgi_application.py", _GOOD_APP)
    ok, detail = cc.boot_gate(str(tmp_path), _ENTRY_CONST)
    assert ok, detail


def test_boot_gate_fails_when_entry_missing(tmp_path):
    # empty src/ — discovery finds no module exposing a WSGI callable, so the
    # boot-gate is RED (the product cannot be assembled or booted at all).
    (tmp_path / "src").mkdir()
    ok, detail = cc.boot_gate(str(tmp_path), _ENTRY_CONST)
    assert not ok and "no module under src" in detail.lower(), detail


def test_boot_gate_fails_when_no_wsgi_app(tmp_path):
    _src(tmp_path, "app.py", "x = 1\n")  # no wsgi_app
    ok, detail = cc.boot_gate(str(tmp_path), _ENTRY_CONST)
    assert not ok and "wsgi_app" in detail.lower(), detail


def test_boot_gate_fails_on_500_health(tmp_path):
    _src(tmp_path, "app.py",
         "def wsgi_app(environ, start_response):\n"
         "    start_response('500 ERR', [('Content-Type','text/plain')])\n"
         "    return [b'boom']\n")
    ok, detail = cc.boot_gate(str(tmp_path), _ENTRY_CONST)
    assert not ok, detail


def test_boot_gate_skipped_when_no_entry_declared(tmp_path):
    # no entry in the constitution -> boot-gate is a no-op PASS (keeps p4/p5)
    _src(tmp_path, "app.py", "x = 1\n")
    ok, detail = cc.boot_gate(str(tmp_path), ["build a CLI tool"])
    assert ok and "skipped" in detail.lower(), detail
