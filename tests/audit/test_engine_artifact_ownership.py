"""Audit rule S10.29 (v157/v158 zombie): an ENGINE-written support module must
have a legitimate owner in the realized tree — never an ownerless file on disk.

The writer is `_harvest_entry_handlers` (entry synthesis relocates a
monolithic LLM entry's business handlers into src/_product_logic.py so the
resolver can wire them — the `from _product_logic import *` synth pattern
left in v124, the WRITER stayed). layer1-findings.md flagged the file as
[orphan_src_file] in v157 AND v158: it exists, it is live (the synthesized
entry imports harvested handlers from it), yet no tree node owns it.

Contract:
  * a successful harvest RECORDS the module as an `artifacts` entry on the
    node owning the declared entry (the assembly leaf) in the exported tree;
  * `owned_modules` (layer-1 ownership reader) honours node `artifacts` —
    ownership stays TREE DATA, never a hardcoded name exemption;
  * the orphan_src_file checker itself stays intact: the SAME file without an
    artifacts record is still flagged (historical runs keep their finding);
  * a synthesized entry that no longer references the harvest module drops
    the stale file (no dead weight from an earlier in-run cycle).

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

from . import consistency  # noqa: E402

_ENTRY = textwrap.dedent("""\
    import json


    def get_about(payload, query):
        return 200, {"about": "a tiny notes service"}


    def wsgi_app(environ, start_response):
        start_response("200 OK", [])
        return [b"{}"]


    application = wsgi_app
""")


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    src = pathlib.Path(ws.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(_ENTRY, encoding="utf-8")
    eng._project_meta = {"tree": {
        "id": "L0", "title": "root", "children": [
            {"id": "core", "title": "core"},
            {"id": "product_entry", "title": "Assemble product entry",
             "code_target": "src/app.py"},
        ]}}
    return eng


def test_harvest_records_owner_in_tree(tmp_path):
    eng = _engine(tmp_path)
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"]}
    assert eng._harvest_entry_handlers(contract) is True
    assert (pathlib.Path(eng.workspace.root)
            / "src" / "_product_logic.py").is_file()
    tree = eng._project_meta["tree"]
    node = tree["children"][1]
    assert "src/_product_logic.py" in (node.get("artifacts") or []), (
        "the harvest module is engine-delivered code — the assembly leaf "
        "must adopt it as an artifact, or every later audit flags the "
        "ownerless zombie (v157/v158 layer-1 orphan_src_file)")
    assert "_product_logic.py" in consistency.owned_modules(tree), (
        "the layer-1 ownership reader must honour node artifacts")


def test_orphan_checker_stays_intact_without_artifact(tmp_path):
    # the SAME file with NO artifacts record must STILL be flagged —
    # historical runs (v157/v158) keep their honest finding
    run = tmp_path / "run"
    (run / "workspace" / "src").mkdir(parents=True)
    (run / "workspace" / "src" / "_product_logic.py").write_text(
        "def get_about(payload, query):\n    return 200, {}\n",
        encoding="utf-8")
    tree = {"id": "L0", "children": [
        {"id": "product_entry", "code_target": "src/app.py"}]}
    found = consistency.check_src_orphans(run, tree)
    assert [f for f in found if f.kind == "orphan_src_file"], (
        "the orphan checker must stay intact: no artifacts record = flagged")


def test_orphan_checker_honours_artifact_record(tmp_path):
    run = tmp_path / "run"
    (run / "workspace" / "src").mkdir(parents=True)
    (run / "workspace" / "src" / "_product_logic.py").write_text(
        "def get_about(payload, query):\n    return 200, {}\n",
        encoding="utf-8")
    tree = {"id": "L0", "children": [
        {"id": "product_entry", "code_target": "src/app.py",
         "artifacts": ["src/_product_logic.py"]}]}
    found = consistency.check_src_orphans(run, tree)
    assert not [f for f in found if f.kind == "orphan_src_file"], (
        "an engine-adopted artifact has an owner — not an orphan")


def test_v158_run_still_flagged(tmp_path):
    # regression pin on the REAL evidence: v158's tree carries no artifacts
    # record, so its layer-1 finding must survive this change untouched
    run = (pathlib.Path(__file__).resolve().parents[1]
           / "runs-out" / "2026-07-03T13-30-25__v158__p6-micro-notes")
    if not (run / "tree.json").is_file():
        import pytest
        pytest.skip("v158 evidence run not present")
    tree = json.loads((run / "tree.json").read_text(encoding="utf-8"))
    found = consistency.check_src_orphans(run, tree)
    assert [f for f in found if f.kind == "orphan_src_file"], (
        "v158's ownerless _product_logic.py must keep its honest finding")


def test_stale_unreferenced_harvest_is_dropped_by_synthesis(tmp_path):
    """A harvest module a LATER synthesis pass no longer references (every
    handler resolved from real leaves) is dead weight from an earlier cycle —
    the entry synthesis removes it instead of leaving a zombie."""
    eng = _engine(tmp_path)
    root = pathlib.Path(eng.workspace.root)
    # a stale harvest from an earlier cycle
    (root / "src" / "_product_logic.py").write_text(
        "def get_ping(payload, query):\n    return 200, {}\n",
        encoding="utf-8")
    entry_code = "def wsgi_app(environ, start_response):\n    return []\n"
    dropped = eng._drop_stale_harvest(entry_code)
    assert dropped and not (root / "src" / "_product_logic.py").is_file(), (
        "a harvest module the synthesized entry does not reference must be "
        "removed")
    # green edge: a REFERENCED harvest module stays
    (root / "src" / "_product_logic.py").write_text(
        "def get_about(payload, query):\n    return 200, {}\n",
        encoding="utf-8")
    entry_code = ("from _product_logic import get_about as _h0\n"
                  "def wsgi_app(environ, start_response):\n    return []\n")
    assert not eng._drop_stale_harvest(entry_code)
    assert (root / "src" / "_product_logic.py").is_file()
