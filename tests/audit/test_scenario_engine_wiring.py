"""Audit rule S14.5: the scenario oracle is WIRED into the engine.

Node B1 (plan 2026-07-04T00-45), the engine side:

  * the final verification judges the IR scenarios alongside the boot probe:
    ANY scenario violation makes the build NOT READY, and the finding is
    routed to the OWNER node (loop entry + `scenario_gate` FAIL event
    carrying the node id — P4, attributable failure);
  * ir.json is RE-DUMPED whenever the realized route set GROWS (a late
    requirement binding a new method on an owned path — the v156 seam):
    the workspace copy is always current and `ir_written` fires again with
    a reason (closes Phase A open question #4 — "the dump happens once");
  * a late injection is a TDD loop EXPRESSIBLE IN THE JOURNAL: the new
    requirement's scenarios enter the IR and are RED before the rework
    (`scenario_red` event naming the node) and GREEN after
    (`scenario_green` event) — red-then-green is evidence, not narrative.

Deterministic: engine unit calls over a tmp workspace, no LLM. The runner
itself executes in a hermetic subprocess (python3 -I) over the workspace.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONSTITUTION = [
    "The product entry src/app.py exposes wsgi_app (stdlib WSGI).",
    "POST /notes takes {text} and responds {id}.",
    "GET /notes responds {items: [{id, text}]} newest-first.",
    "GET /health responds 200.",
]

_DELETE_REQ = (
    "ALLOW REMOVING A NOTE (added by the human mid-run; binding). "
    "A reader must be able to delete a single note by its id. Extend the "
    "existing notes capability - do not add a separate store. Deleting a "
    "note then listing must no longer show it.")

# a late requirement that refines an owned surface WITHOUT a new route —
# the pure-refinement shape that must NOT trigger a re-dump
_NICE_REQ = (
    "MAKE THE NOTES NICE TO READ. Improve the readability of the existing "
    "notes list output. Keep the current behaviour.")

_CORE_BEFORE = '''\
def post_notes(payload, query):
    return 201, {"id": 1}


def get_notes(payload, query):
    return 200, {"items": []}


def get_health(payload, query):
    return 200, {"status": "ok"}
'''

_CORE_DELETE = '''

def delete_notes(payload, query):
    return 200, {"deleted": True}
'''

# deterministic hand-wired entry (the engine's own synthesis is covered by
# its own stages; here only the ORACLE wiring is under test)
_APP_AFTER = '''\
import json

import core


def wsgi_app(environ, start_response):
    table = {("POST", "/notes"): core.post_notes,
             ("GET", "/notes"): core.get_notes,
             ("GET", "/health"): core.get_health,
             ("DELETE", "/notes"): core.delete_notes}
    fn = table.get((environ.get("REQUEST_METHOD"),
                    environ.get("PATH_INFO")))
    if fn is None:
        start_response("404 Not Found",
                       [("Content-Type", "application/json")])
        return [b'{"error": "not found"}']
    n = int(environ.get("CONTENT_LENGTH") or 0)
    payload = json.loads(environ["wsgi.input"].read(n) or b"{}") if n else {}
    status, doc = fn(payload, {})
    reasons = {200: "200 OK", 201: "201 Created"}
    start_response(reasons.get(status, "%d STATUS" % status),
                   [("Content-Type", "application/json")])
    return [json.dumps(doc).encode()]
'''


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._constitution = list(_CONSTITUTION)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "core.py").write_text(_CORE_BEFORE, encoding="utf-8")
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes and GET /health"}
    assert ("POST", "/notes") in set(eng._leaf_owned_routes(core))
    return eng


def _attach(eng, nid, title, req):
    extra = {"id": nid, "title": title, "requirement": req,
             "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"   # deterministic router seam
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    return extra


def _ir_events(eng):
    return [e for e in eng.events if e.gate == "ir_written"]


def _ir_doc(eng):
    return json.loads((pathlib.Path(eng.workspace.root) / "ir.json")
                      .read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# ir.json re-dump on route growth (Phase A open question #4)
# --------------------------------------------------------------------------

def test_ir_redumped_with_reason_when_the_route_set_grows(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir()
    doc = _ir_doc(eng)
    assert "delete" not in json.dumps(doc.get("nodes", {})).lower()
    assert len(_ir_events(eng)) == 1
    _attach(eng, "delete_note", "ALLOW REMOVING A NOTE", _DELETE_REQ)
    doc = _ir_doc(eng)
    node = doc["nodes"].get("delete_note") or {}
    assert "delete" in ((node.get("openapi") or {}).get("paths") or {}) \
        .get("/notes", {}), (
        "the late-bound DELETE /notes must be IN the workspace ir.json — "
        "the copy on disk is stale otherwise: %s" % sorted(doc["nodes"]))
    evs = _ir_events(eng)
    assert len(evs) == 2, ("ir_written must fire AGAIN when the realized "
                           "route set grows: %d event(s)" % len(evs))
    assert "reason" in evs[-1].detail, (
        "the re-dump event must carry a reason field: %r" % evs[-1].detail)


def test_no_route_growth_means_no_redump(tmp_path):
    eng = _engine(tmp_path)
    eng._write_ir()
    _attach(eng, "nice_view", "MAKE THE NOTES NICE TO READ", _NICE_REQ)
    assert len(_ir_events(eng)) == 1, (
        "a pure refinement binds no route — re-dumping would churn the "
        "artifact for nothing")


# --------------------------------------------------------------------------
# the scenario gate at final verification (violation => NOT READY)
# --------------------------------------------------------------------------

def _hand_ws(tmp_path, app_body):
    """Workspace with a HAND-written ir.json + entry: the gate is judged in
    isolation from contract derivation and entry synthesis."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    # ONLY the entry is declared: every other gate (boot probe, unserved
    # routes) stays green, so a red verdict is attributable to the scenario
    # gate alone
    eng._constitution = [
        "The product entry src/app.py exposes wsgi_app (stdlib WSGI)."]
    root = pathlib.Path(eng.workspace.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(app_body, encoding="utf-8")
    ir = {"format": "spec-flow ir v1",
          "product": {"entry": "src/app.py", "callable": "wsgi_app"},
          "nodes": {"about_page": {
              "openapi": {
                  "openapi": "3.1.0",
                  "info": {"title": "t", "version": "1"},
                  "paths": {"/about": {"get": {"responses": {"200": {
                      "description": "ok",
                      "content": {"text/html": {"schema": {}}}}}}}}},
              "scenarios": [{
                  "requirement": "about_page",
                  "when": {"method": "GET", "path": "/about"},
                  "then": {"status": 200, "media": "text/html"}}]}}}
    (root / "ir.json").write_text(json.dumps(ir), encoding="utf-8")
    return eng


_APP_JSON_ABOUT = '''\
def wsgi_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "application/json")])
    return [b'{"name": "notes-service", "version": "1.0"}']
'''

_APP_HTML_ABOUT = '''\
def wsgi_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/html")])
    return [b"<html><body>about</body></html>"]
'''


def test_scenario_violation_reds_the_build_and_names_the_owner(tmp_path):
    eng = _hand_ws(tmp_path, _APP_JSON_ABOUT)
    ok, detail = eng._ir_scenario_gate()
    assert ok is False, "a live media drift must red the gate (v164 class)"
    assert "text/html" in detail and "application/json" in detail, detail
    assert any(lp.get("type") == "scenario-fail"
               and lp.get("task") == "about_page" for lp in eng.loops), (
        "the finding must be ROUTED to the owner node: %r" % eng.loops)
    assert any(e.gate == "scenario_gate" and e.verdict == "FAIL"
               and e.task == "about_page" for e in eng.events)


def test_conforming_product_passes_the_scenario_gate(tmp_path):
    eng = _hand_ws(tmp_path, _APP_HTML_ABOUT)
    ok, detail = eng._ir_scenario_gate()
    assert ok is True, detail
    assert not any(lp.get("type") == "scenario-fail" for lp in eng.loops)


def test_final_verification_calls_the_scenario_gate(tmp_path):
    """_verify_tests must consult the oracle: green suite + green boot is
    still NOT READY when a scenario reds (honest conjunction, P7)."""
    eng = _hand_ws(tmp_path, _APP_JSON_ABOUT)
    root = pathlib.Path(eng.workspace.root)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_trivial.py").write_text(
        "def test_trivial():\n    assert True\n", encoding="utf-8")
    eng._verify_tests()
    results = (root / "TEST-RESULTS.md").read_text(encoding="utf-8")
    assert "FAIL" in results.split("\n\n")[1], (
        "a scenario violation must make the verdict NOT READY: %s"
        % results[:400])
    assert any(e.gate == "scenario_gate" and e.verdict == "FAIL"
               for e in eng.events), (
        "the red must come FROM the scenario gate — the oracle ran inside "
        "the final verification")


# --------------------------------------------------------------------------
# late injection TDD loop: scenario_red before rework, scenario_green after
# --------------------------------------------------------------------------

def test_late_injection_is_red_then_green_in_the_journal(tmp_path):
    eng = _engine(tmp_path)
    root = pathlib.Path(eng.workspace.root)
    eng._write_ir()
    # the late requirement lands: its scenario enters the IR and MUST be
    # red — nothing serves DELETE /notes yet
    _attach(eng, "delete_note", "ALLOW REMOVING A NOTE", _DELETE_REQ)
    reds = [e for e in eng.events if e.gate == "scenario_red"]
    assert any(e.task == "delete_note" for e in reds), (
        "the landing must be journalled RED before any rework: %r"
        % [(e.gate, e.task) for e in eng.events if "scenario" in e.gate])
    # the rework: the handler appears and the entry wires it
    core_py = root / "src" / "core.py"
    core_py.write_text(core_py.read_text(encoding="utf-8") + _CORE_DELETE,
                       encoding="utf-8")
    (root / "src" / "app.py").write_text(_APP_AFTER, encoding="utf-8")
    ok, detail = eng._ir_scenario_gate()
    assert ok is True, detail
    greens = [e for e in eng.events if e.gate == "scenario_green"]
    assert any(e.task == "delete_note" for e in greens), (
        "after the rework the SAME node must be journalled GREEN — the TDD "
        "loop is evidence, not narrative")
