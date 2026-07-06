"""STAGE 18 (S18.1-S18.6): interface tests are COMPILED from the IR — the
LLM tester is retired from interface coverage (node B3, plan 2026-07-04T00-45).

Why: an LLM tester re-guesses facts the IR already carries. v149 ("the tester
guessed a status": coder returned 201 while the tester asserted 200) and v150
("success asserted on a foreign route", "assertIn(code, (200, 201))" smears)
are ONE class — tests inventing interface facts. Compiling the tests FROM the
node's IR entry kills the class by construction: the compiler can only emit
what the IR declares, an undeclared status cannot appear in the output because
no code path reads it from anywhere else.

What is pinned here:
  * S18.1 compiled file contains ONLY the node's contracted routes — the
    v150 foreign-route class is dead for IR leaves;
  * S18.2 the compiler REFUSES foreign data: an IR failing spec_ir.validate_ir
    (the v149 undeclared-status scenario) raises before any content exists;
  * S18.3 status asserts are EXACT (`== 201`) — a smeared membership set of
    statuses cannot appear in a compiled file;
  * S18.4 a route or scenario lacking an IR datum compiles to a VISIBLE
    pytest skip whose reason names the gap — never an invented value;
  * S18.5 engine wiring: a leaf with an IR entry gets its tests WRITTEN by
    the engine (journal `leaf_tests_source: ir-compiled`), the worker context
    carries the flag, the orchestra drops the tester step, worker writes to
    the compiled file are refused, and the engine re-asserts the file over a
    worker rewrite; a leaf without an IR entry keeps the historical LLM path
    (journal `leaf_tests_source: llm`) — the GREEN legacy direction;
  * S18.6 the existing leaf gates (S10.6 status, S12.1 request shape) pass
    over a compiled file — they are trivially satisfied, never weakened.

Deterministic: hand-built known-answer IR dicts; the only LLM-shaped agents
are local stubs.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))

import spec_conformance  # noqa: E402  (RED before code: module absent)
import spec_ir  # noqa: E402
from harness import role_worker as rw  # noqa: E402
from harness import run_engine as eng  # noqa: E402


# ── known-answer IR fixtures ────────────────────────────────────────────────

def _op(status="200", media="application/json", required=None, const=None):
    resp: dict = {"description": "contracted"}
    if media:
        schema = {"const": const} if const is not None else {}
        resp["content"] = {media: {"schema": schema}}
    op: dict = {"responses": {str(status): resp}}
    if required is not None:
        op["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {f: {} for f in required},
                "required": list(required),
                "additionalProperties": False}}}}
    return op


def _doc(nid, paths):
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": paths}


def _error_op():
    """A minimal non-2xx operation response — closes the N6 errors_edges aspect
    (a route that lists only its success status leaves the failure surface
    unspecified). Carries a JSON body so it is not a missing-media gap."""
    return {"description": "error",
            "content": {"application/json": {"schema": {}}}}


def _notes_node():
    post = _op("201", required=["text"])
    post["responses"]["400"] = _error_op()
    get = _op("200")
    get["responses"]["404"] = _error_op()
    return {
        "files": ["src/notes_api.py"],
        "effects": [],  # explicit placement/side-effect envelope (N6)
        "openapi": _doc("notes_api", {"/notes": {"post": post, "get": get}}),
        "scenarios": [{
            "requirement": "notes-roundtrip",
            "given": {"state": [{"method": "POST", "path": "/notes",
                                 "body": {"text": "hello"}}]},
            "when": {"method": "GET", "path": "/notes"},
            "then": {"status": "200", "media": "application/json",
                     "body_check": {"json_subset": [{"text": "hello"}]}},
        }],
    }


def _health_node():
    get = _op("200", const={"status": "ok"})
    get["responses"]["503"] = _error_op()
    return {"files": ["src/health.py"], "effects": [],
            "openapi": _doc("health", {"/health": {"get": get}}),
            "scenarios": [{
                "requirement": "health-ok",
                "when": {"method": "GET", "path": "/health"},
                "then": {"status": "200", "media": "application/json",
                         "body_check": {"json_subset": [{"status": "ok"}]}},
            }]}


def _ir(nodes):
    return {"format": spec_ir.IR_FORMAT, "product": {}, "nodes": nodes}


# ── S18.1 only contracted routes / values traced to IR datums ───────────────

def test_compiled_file_contains_only_contracted_routes():
    out = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": _notes_node(), "health": _health_node()}),
        "notes_api")
    ast.parse(out)                       # a compiled file always parses
    assert "/notes" in out, "the node's own contracted route must appear"
    assert "/health" not in out and "get_health" not in out, (
        "the v150 foreign-route class: a compiled file may never touch a "
        "route owned by another node")


def test_compiled_tests_cover_every_contracted_success_status():
    out = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": _notes_node()}), "notes_api")
    assert "== 201" in out and "== 200" in out, (
        "one test per contracted (route, status): POST /notes 201 and "
        "GET /notes 200 must both be asserted EXACTLY")
    assert "post_notes" in out and "get_notes" in out, (
        "the tests exercise the engine-declared canonical handlers")
    assert out.startswith("# code: src/notes_api.py\n"
                          "# test: tests/test_notes_api.py\n"), (
        "the compiled file carries the standard provenance header")


def test_request_values_come_from_scenario_bodies():
    out = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": _notes_node()}), "notes_api")
    assert "hello" in out, (
        "the POST /notes request value must be the scenario's when/state "
        "body datum — the ONLY recorded value for the required field")


# ── S18.2 the compiler refuses foreign data ─────────────────────────────────

def test_v149_undeclared_status_scenario_is_refused():
    n = _notes_node()
    n["scenarios"][0]["then"]["status"] = "418"     # never declared
    with pytest.raises(ValueError) as ei:
        spec_conformance.compile_leaf_tests(_ir({"notes_api": n}),
                                            "notes_api")
    assert "418" in str(ei.value), (
        "the refusal must name the undeclared status (P4) — the v149 "
        "status-guess is impossible by construction, not filtered later")


def test_unknown_node_is_refused():
    with pytest.raises(ValueError):
        spec_conformance.compile_leaf_tests(
            _ir({"notes_api": _notes_node()}), "ghost")


# ── S18.3 exact status asserts — a smear cannot appear ──────────────────────

def test_status_asserts_are_exact_never_membership():
    out = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": _notes_node(), "health": _health_node()}),
        "notes_api")
    for node in ast.walk(ast.parse(out)):
        if not isinstance(node, ast.Compare):
            continue
        for op, cmp_ in zip(node.ops, node.comparators):
            if not isinstance(op, ast.In):
                continue
            consts = [c.value for c in getattr(cmp_, "elts", [])
                      if isinstance(c, ast.Constant)]
            assert not any(isinstance(v, int) and 100 <= v <= 599
                           for v in consts), (
                "a compiled test may never smear statuses into a membership "
                "set (the v150 'assertIn(code, (200, 201))' class)")


# ── S18.4 a missing datum is a VISIBLE named skip, never a guess ────────────

def test_unvalued_required_body_compiles_to_named_skip():
    n = _notes_node()
    n["scenarios"] = []                  # nothing values POST's `text`
    out = spec_conformance.compile_leaf_tests(_ir({"notes_api": n}),
                                              "notes_api")
    ast.parse(out)
    assert "pytest.mark.skip" in out, "the gap must stay VISIBLE"
    assert "POST /notes" in out and "text" in out, (
        "the skip reason names the route and the unvalued required fields")
    assert "_invoke(post_notes" not in out, (
        "the unvalued route is never invoked with an invented value")


def test_scenario_step_on_foreign_route_is_named_skip():
    n = _notes_node()
    n["scenarios"][0]["given"]["state"].insert(
        0, {"method": "GET", "path": "/health"})
    out = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": n, "health": _health_node()}), "notes_api")
    ast.parse(out)
    assert "pytest.mark.skip" in out
    assert "GET /health" in out, "the skip reason names the foreign step"
    assert "get_health" not in out, (
        "the foreign handler is never imported or called from this file")


# ── S18.5 engine wiring: the engine authors the file, the tester retires ────

def _engine(tmp_path):
    return eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)


def test_engine_writes_ir_compiled_tests_and_flags_worker(tmp_path):
    e = _engine(tmp_path)
    e.__dict__["_decomposer_ir_nodes"] = {"notes_api": _notes_node()}
    ictx: dict = {}
    e._compile_ir_leaf_tests({"id": "notes_api", "title": "Notes API"},
                             "notes_api", "notes_api",
                             "tests/test_notes_api.py", ictx)
    p = pathlib.Path(e.workspace.root) / "tests/test_notes_api.py"
    assert p.is_file(), "the ENGINE writes the leaf's interface tests"
    want = spec_conformance.compile_leaf_tests(
        _ir({"notes_api": _notes_node()}), "notes_api")
    assert p.read_text(encoding="utf-8") == want, (
        "the written file IS the compiler output — no engine-side edits")
    assert ictx.get("tests_precompiled") == "tests/test_notes_api.py", (
        "the worker context must carry the flag that retires the tester")
    evs = [ev for ev in e.events
           if "leaf_tests_source: ir-compiled" in ev.detail]
    assert evs and "notes_api" in evs[0].task, (
        "the journal must record leaf_tests_source: ir-compiled for the node")


def test_leaf_without_ir_entry_keeps_the_llm_path(tmp_path):
    # GREEN legacy direction: no fragment -> no compiled file, no flag,
    # the source is journaled honestly as llm.
    e = _engine(tmp_path)
    ictx: dict = {}
    e._compile_ir_leaf_tests({"id": "notes_api", "title": "Notes API"},
                             "notes_api", "notes_api",
                             "tests/test_notes_api.py", ictx)
    assert not (pathlib.Path(e.workspace.root)
                / "tests/test_notes_api.py").is_file()
    assert "tests_precompiled" not in ictx
    evs = [ev for ev in e.events if "leaf_tests_source: llm" in ev.detail]
    assert evs and "notes_api" in evs[0].task


def test_compiler_refusal_falls_back_to_llm_path(tmp_path):
    e = _engine(tmp_path)
    bad = _notes_node()
    bad["scenarios"][0]["then"]["status"] = "418"
    e.__dict__["_decomposer_ir_nodes"] = {"notes_api": bad}
    ictx: dict = {}
    e._compile_ir_leaf_tests({"id": "notes_api", "title": "Notes API"},
                             "notes_api", "notes_api",
                             "tests/test_notes_api.py", ictx)
    assert "tests_precompiled" not in ictx, (
        "a refused compile never half-lands: the leaf keeps the LLM path")
    evs = [ev for ev in e.events if "leaf_tests_source: llm" in ev.detail]
    assert evs and "418" in evs[0].detail, (
        "the fallback journal carries the refusal reason (attributable, P4)")


def test_engine_reasserts_compiled_tests_over_worker_rewrite(tmp_path):
    e = _engine(tmp_path)
    e.__dict__["_decomposer_ir_nodes"] = {"notes_api": _notes_node()}
    ictx: dict = {}
    e._compile_ir_leaf_tests({"id": "notes_api", "title": "Notes API"},
                             "notes_api", "notes_api",
                             "tests/test_notes_api.py", ictx)
    p = pathlib.Path(e.workspace.root) / "tests/test_notes_api.py"
    want = p.read_text(encoding="utf-8")
    p.write_text("def test_worker_junk():\n    assert True\n",
                 encoding="utf-8")
    e._reassert_ir_leaf_tests("notes_api", "tests/test_notes_api.py")
    assert p.read_text(encoding="utf-8") == want, (
        "the compiled file is ENGINE authority — a worker rewrite is "
        "restored in code, not hoped away by prompts")
    evs = [ev for ev in e.events if ev.gate == "ir_tests_authority"]
    assert evs and evs[0].verdict == "ENFORCED"


def test_reassert_is_inert_without_a_compiled_file(tmp_path):
    # GREEN direction: an llm-path leaf is never touched by the re-assert.
    e = _engine(tmp_path)
    rel = "tests/test_notes_api.py"
    p = pathlib.Path(e.workspace.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def test_by_llm():\n    assert True\n", encoding="utf-8")
    e._reassert_ir_leaf_tests("notes_api", rel)
    assert p.read_text(encoding="utf-8") == (
        "def test_by_llm():\n    assert True\n")


# ── S18.5 harness seams: the tester step and the write door ─────────────────

def test_tester_step_dropped_for_ir_compiled_leaf(monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    team = [{"role": "coder"}, {"role": "tester"}, {"role": "fixer"}]
    out = rw._active_team(team, {"tests_precompiled": "tests/test_x.py"},
                          "n1")
    assert [s["role"] for s in out] == ["coder", "fixer"], (
        "the tester step is retired when the engine compiled the tests")
    assert any(ev.get("event") == "orchestra_step_skipped"
               and ev.get("role") == "tester" for ev in logged), (
        "the retirement is journaled, never silent")
    assert rw._active_team(team, {}, "n1") == team, (
        "GREEN direction: without the flag the team runs unchanged")


def test_orchestra_consults_the_team_filter():
    import inspect
    src = inspect.getsource(rw._orchestra_run)
    assert "_active_team(" in src, (
        "_orchestra_run must resolve its steps through _active_team — "
        "otherwise the retirement seam is dead code")


class _WS:
    def __init__(self):
        self.writes: list = []

    def _write(self, rel, body, kind):
        self.writes.append((rel, kind))
        return rel


def test_worker_write_to_compiled_tests_is_refused(monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    files = {"src/x.py": "def f():\n    return 1\n",
             "tests/test_x.py": "def test_f():\n    assert True\n"}
    ws = _WS()
    wrote = rw._write_reply_files(ws, files, "x", protect_tests=True)
    assert wrote and [r for r, _ in ws.writes] == ["src/x.py"], (
        "src is written, the compiled test file is not")
    assert any(ev.get("event") == "write_refused"
               and ev.get("path") == "tests/test_x.py" for ev in logged), (
        "the refusal is journaled with the offending path")
    # GREEN direction: without protection both files land as before
    ws2 = _WS()
    assert rw._write_reply_files(ws2, dict(files), "x")
    assert {r for r, _ in ws2.writes} == {"src/x.py", "tests/test_x.py"}


def test_diff_repair_to_compiled_tests_is_refused(tmp_path, monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_f():\n    assert True\n", encoding="utf-8")
    reply = ("FILE: tests/test_x.py\n"
             "<<<<<<< SEARCH\n"
             "    assert True\n"
             "=======\n"
             "    assert False\n"
             ">>>>>>> REPLACE\n")
    ws = _WS()
    wrote = rw._apply_diff_repair(ws, str(tmp_path), "x", reply,
                                  protect_tests=True)
    assert not wrote and not ws.writes, (
        "a surgical diff aimed at the compiled tests must be refused too")


# ── S18.5 end-to-end: the leaf pipeline authors tests from the IR ───────────

_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}

_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True,
          "testable_criteria": True}

_IMPL_CODE = (
    "_ITEMS = []\n\n\n"
    "def post_notes(payload, query):\n"
    "    _ITEMS.append({\"text\": payload[\"text\"]})\n"
    "    return 201, {\"id\": len(_ITEMS)}\n\n\n"
    "def get_notes(payload, query):\n"
    "    return 200, list(_ITEMS)\n")


def test_leaf_pipeline_authors_tests_from_ir(plugin, tmp_path):
    seen: dict = {}

    def dec(ctx):
        if ctx["depth"] == 0:
            return {"atomic": True, "metrics": dict(_SMALL),
                    "acceptance": ["Given a note, When POSTed to /notes,"
                                   " Then GET /notes returns it"],
                    "ir": _ir({"notes_api": _notes_node()})}
        return {"metrics": dict(_SMALL)}

    dec.emits_ir = True

    def impl(ctx):
        seen.update(ctx)
        ws = ctx["workspace"]
        ws._write("src/notes_api.py", _IMPL_CODE, "code")
        # a worker that still rewrites the compiled file loses to the engine
        ws._write("tests/test_notes_api.py",
                  "def test_worker_junk():\n    assert True\n", "test")

    project = {
        "name": "micro-notes-ir-tests",
        "goal": ("A tiny notes service over WSGI: POST /notes stores {text}"
                 " and returns {id}; GET /notes returns the items."),
        "target": "POST then GET round-trips a note; stdlib only",
        "constitution": ["Standard library ONLY."],
        "acceptance": {"smoke": ["the build succeeds"]},
        "policy": dict(_POLICY),
        "tree": {"id": "notes_api", "title": "notes api"},
    }
    eng.run_project(project, workspace=str(tmp_path / "wk"),
                    depth="product", tools=plugin.tools,
                    contracts_dir=str(eng.CONTRACTS),
                    agents={"decomposer": dec, "implementer": impl})
    assert seen.get("tests_precompiled") == "tests/test_notes_api.py", (
        "the implementer context must carry the retirement flag — the "
        "pipeline compiled the tests before invoking the worker")
    text = (tmp_path / "wk" / "tests" / "test_notes_api.py").read_text(
        encoding="utf-8")
    assert "test_worker_junk" not in text, (
        "the engine re-asserts the compiled file over the worker rewrite")
    assert "compile_leaf_tests" in text and "== 201" in text, (
        "the landed file is the IR-compiled artifact")


# ── S18.6 the existing leaf gates hold over a compiled file ─────────────────

def test_existing_leaf_gates_pass_over_compiled_tests(tmp_path):
    e = _engine(tmp_path)
    e.__dict__["_decomposer_ir_nodes"] = {"notes_api": _notes_node()}
    node = {"id": "notes_api", "title": "Notes API"}
    rel = "tests/test_notes_api.py"
    e._compile_ir_leaf_tests(node, "notes_api", "notes_api", rel, {})
    assert e._leaf_test_status_gate(node, "notes_api", 1, rel), (
        "S10.6/S12.7 must hold trivially over a compiled file")
    assert not [l for l in e.loops if l["type"] == "test-status-mismatch"]
    assert e._leaf_request_shape_gate(node, "notes_api", 1,
                                      "src/notes_api.py", rel), (
        "S12.1 must hold trivially over a compiled file")
