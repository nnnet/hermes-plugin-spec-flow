"""Audit rule S10.25: code (src/ or tests/) that reads an attribute on a
workspace-local imported module which that module does NOT define is refused
at the ONE write door — a phantom dependency symbol never lands.

v156 (p6-micro-notes): the integrate-time module repair ('module repair:
rework core', trace events 139-143) shipped ``db.list_notes()`` while
src/db.py defines only insert_note/get_notes, and tests/test_core.py calling
``db.connect()`` — no gate refused either write. The assembled product then
answered GET /notes -> 500 ('module db has no attribute list_notes'), which
failed EVERY smoke/e2e acceptance check through the shared boot oracle
(PRODUCT-RESULTS.md: '[smoke] ... GET /health -> 200 — assembled product does
not serve its contract: GET /notes -> 500').

Contract pinned here (deterministic AST, both write doors, no LLM):
  * ``import X`` + ``X.attr`` where <root>/src/X.py exists and defines no
    module-level ``attr`` -> the door REFUSES (refused_* artifact, file not
    written) naming the phantom symbol;
  * a defined attribute, a stdlib import, a not-yet-built dependency, a
    dependency using ``import *``, and a self-import are all CLEAN — the
    check can never false-red on build order or dynamic modules;
  * ``from X import Y`` stays out of scope — assembly import repair (S10.2)
    owns that seam and re-points it to the real owner.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_DB = '''\
import os
import sqlite3


def insert_note(text):
    return 1


def get_notes():
    return []
'''


def _ws(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    src = pathlib.Path(ws.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "db.py").write_text(_DB, encoding="utf-8")
    return ws


def _refusals(ws, rel):
    return [a for a in ws.artifacts
            if a.get("path") == rel and str(a.get("type", "")).startswith(
                "refused_")]


def test_phantom_dep_attr_in_src_is_refused(tmp_path):
    ws = _ws(tmp_path)
    rel = "src/core.py"
    ws._write(rel, textwrap.dedent("""\
        import db


        def get_notes(payload, query):
            return 200, {"items": db.list_notes()}
    """), "code")
    bad = _refusals(ws, rel)
    assert bad, (
        "db.list_notes() with src/db.py defining only get_notes is the v156 "
        "rework regression — the write door must refuse it")
    assert "list_notes" in bad[0]["reason"]
    assert not (pathlib.Path(ws.root) / rel).is_file(), (
        "a refused write must not land")


def test_phantom_dep_attr_in_tests_is_refused(tmp_path):
    ws = _ws(tmp_path)
    rel = "tests/test_core.py"
    ws._write(rel, textwrap.dedent("""\
        import db


        def test_connects():
            db.connect()
    """), "test")
    assert _refusals(ws, rel), (
        "tests/test_core.py calling db.connect() (v156) is the same phantom "
        "class — the tests/ door shares the check")


def test_defined_attr_and_stdlib_are_clean(tmp_path):
    ws = _ws(tmp_path)
    rel = "src/core.py"
    ws._write(rel, textwrap.dedent("""\
        import json

        import db


        def get_notes(payload, query):
            return 200, {"items": db.get_notes(), "raw": json.dumps([])}
    """), "code")
    assert not _refusals(ws, rel)
    assert (pathlib.Path(ws.root) / rel).is_file()


def test_not_yet_built_dependency_is_clean(tmp_path):
    # bottom-up build order: the consumer may land before its dependency
    ws = _ws(tmp_path)
    rel = "src/core.py"
    ws._write(rel, "import storage\n\n\ndef f(payload, query):\n"
                   "    return 200, storage.everything()\n", "code")
    assert not _refusals(ws, rel), "a missing dep file is build order, not a phantom"


def test_star_import_dependency_is_skipped(tmp_path):
    ws = _ws(tmp_path)
    (pathlib.Path(ws.root) / "src" / "dyn.py").write_text(
        "from os.path import *\n", encoding="utf-8")
    rel = "src/core.py"
    ws._write(rel, "import dyn\n\n\ndef f(payload, query):\n"
                   "    return 200, dyn.join('a', 'b')\n", "code")
    assert not _refusals(ws, rel), (
        "a dependency built on `import *` has an unknowable surface — never "
        "false-red it")


def test_self_import_is_skipped(tmp_path):
    ws = _ws(tmp_path)
    rel = "src/core.py"
    ws._write(rel, "import core\n\n\ndef f(payload, query):\n"
                   "    return 200, core.g()\n\n\ndef g():\n    return {}\n",
              "code")
    assert not _refusals(ws, rel), (
        "the on-disk old version must never veto the new body of the SAME "
        "module")


def test_from_import_stays_s102_scope(tmp_path):
    # S10.2 (assembly import repair) owns from-imports — the door must not
    # pre-empt the repair that re-points them to the real owner
    ws = _ws(tmp_path)
    rel = "src/core.py"
    ws._write(rel, "from db import list_notes\n\n\ndef f(payload, query):\n"
                   "    return 200, list_notes()\n", "code")
    assert not _refusals(ws, rel)
