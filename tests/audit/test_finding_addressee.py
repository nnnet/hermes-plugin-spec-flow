"""Audit rule S12.12 (v163): a gate finding is ADDRESSED TO THE ARTIFACT
OWNER — the node that owns the offending FILE — never to the owner of the
route the file merely touches.

v163 evidence (2026-07-03T21-53-39__v163__p6-micro-notes): the assembled
product was FULLY green (every v149-v162 class gone), yet the terminal was
NOT READY on one open cause `about_page:request_shape` (trace event 159).
The finding (events 126-127): "tests/test_core.py sends payload field(s)
'irrelevant' to `GET /about` outside the contracted request shape []". The
finding is HONEST and the gate works — but the offending ARTIFACT is
tests/test_core.py (CORE's test file, the amend's test_rel) while the cause
was opened on about_page (the ROUTE owner). Rework of about_page can never
fix core's file, so the cause could never close: an eternal root red over a
green product.

Contract pinned here (all deterministic, no LLM):
  * RED A (request-shape): a request-shape finding whose offending artifact
    is tests/test_core.py — even when the violated route (GET /about) is
    owned by the amend node about_page — opens its doctor cause on CORE,
    journals the rework loop with task=core, and registers the S12.5
    recheck under core; about_page's ledger stays untouched.
  * RED B (test-status, same bug): a wrong-status assertion found in
    tests/test_core.py by the amend's gate run lands on core the same way.
  * RED C (body-shape, same bug): an HTML-marker assertion on a JSON route
    found in tests/test_core.py lands on core too (body-shape findings flow
    through the test-status gate).
  * the S12.5 close: after core's file is fixed and CORE reaches DONE,
    `_prune_stale_causes` re-runs the opening gate and closes the cause
    attributably — the exact close v163 could never reach.
  * GREEN: a violation in a leaf's OWN test file still lands on that leaf
    itself (core judging tests/test_core.py blames core; about_page judging
    tests/test_about_page.py blames about_page) — addressing by artifact
    ownership never re-routes a node's own defects elsewhere.

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_GOAL = (
    'A tiny notes service. POST /notes accepts {"text": "..."} and stores '
    'it, returning {"id": <int>}; GET /notes returns {"items": [...]}; '
    'GET /about serves a small HTML about page.')
_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [("GET", "/about")],
    "media": {"/notes": "json", "/health": "json", "/about": "html"},
}
# the v163 shape: about_page is an AMEND of src/core.py that the engine
# bound to GET /about (S12.11) — its code_rel/test_rel are CORE's files
_AMEND = {"id": "about_page", "title": "About page",
          "code_target": "src/core.py", "binds_route": ["GET", "/about"],
          "requirement": "Serve GET /about with a small HTML page"}
_CORE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}

_CODE_REL, _TEST_REL = "src/core.py", "tests/test_core.py"

_CLEAN_CODE = """\
    def get_about(payload, query):
        return 200, "<html><body>about</body></html>"
"""

# the offending artifact: CORE's test file sends a junk field to the route
# the AMEND owns — the _call(...) form (not a direct canonical-handler call)
# so the finding stays a red finding, never a mechanical autofix (S12.13)
_BAD_TEST = """\
    def _call(method, path, payload):
        return 200, ""


    def test_about():
        code, body = _call("GET", "/about", {"irrelevant": 1})
        assert code == 200
"""

_FIXED_TEST = """\
    def _call(method, path, payload):
        return 200, ""


    def test_about():
        code, body = _call("GET", "/about", {})
        assert code == 200
"""


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC,
                     doctor_project={"doctor": {"enabled": True}})
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    # the run processed the core leaf before the amend: its module name is
    # registered (the engine's node->module datum the addressee is read from)
    eng._module_for("core")
    eng._leaf_owned_routes(dict(_CORE))
    return eng


def _write(eng, rel: str, body: str) -> None:
    p = pathlib.Path(eng.workspace.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def _mark(eng, nid: str, status: str) -> None:
    eng.tasks[nid] = sfr.Task(id=nid, title=nid, kind="impl",
                              profile="", skill="", status=status)


def _open_causes(eng) -> dict:
    return {n: c for n, c in eng._doctor_open_causes()}


# --- RED A: the v163 defect — request-shape finding on core's test file -------

def _run_shape_gate(tmp_path, test_body: str):
    eng = _engine(tmp_path)
    _write(eng, _CODE_REL, _CLEAN_CODE)
    _write(eng, _TEST_REL, test_body)
    ok = eng._leaf_request_shape_gate(dict(_AMEND), "about_page", 1,
                                      _CODE_REL, _TEST_REL)
    return eng, ok


def test_request_shape_cause_opens_on_artifact_owner_not_route_owner(tmp_path):
    eng, ok = _run_shape_gate(tmp_path, _BAD_TEST)
    assert ok is False, "the junk field IS a real finding — the gate reds"
    causes = _open_causes(eng)
    assert "about_page" not in causes, (
        "v163 eternal red: the cause was opened on about_page (the ROUTE "
        "owner) whose rework can never edit tests/test_core.py — the "
        "offending ARTIFACT is core's file, so core is the addressee")
    assert causes.get("core") == "request_shape", (
        f"the cause must open on core (owner of {_TEST_REL}), got {causes}")


def test_request_shape_rework_is_directed_at_artifact_owner(tmp_path):
    eng, _ok = _run_shape_gate(tmp_path, _BAD_TEST)
    loops = [l for l in eng.loops if l["type"] == "request-shape-mismatch"]
    assert loops and loops[0]["task"] == "core", (
        "the rework loop must route to the node that OWNS the offending "
        f"file (core), not the route owner; got {loops}")
    assert not [l for l in eng.loops
                if l.get("task") == "about_page"
                and "mismatch" in str(l.get("type", ""))], (
        "about_page delivered nothing wrong — its ledger stays untouched")


def test_request_shape_cause_closes_after_owner_fix(tmp_path):
    # the S12.5 recheck re-runs against the RIGHT node's artifacts: fixing
    # core's test file and completing CORE closes the cause — the close
    # v163 could never reach because the cause sat on about_page
    eng, _ok = _run_shape_gate(tmp_path, _BAD_TEST)
    _write(eng, _TEST_REL, _FIXED_TEST)      # core's rework fixed ITS file
    _mark(eng, "core", "done")
    eng._prune_stale_causes()
    assert eng._doctor_open_causes() == [], (
        "core's file is clean and core is DONE — the cause must close; an "
        "open entry here is the v163 eternal red over a green product")
    res = [l for l in eng.loops if l.get("type") == "doctor"
           and l.get("outcome") == "resolved"]
    assert res and "request_shape_gate" in str(res[-1].get("detail", "")), (
        f"the close must be attributable to the opening gate, got {res}")


def test_request_shape_cause_stays_open_while_owner_file_unfixed(tmp_path):
    # HONEST twin: attribution is not a softener — an unfixed file keeps
    # the cause open on core even after core claims DONE
    eng, _ok = _run_shape_gate(tmp_path, _BAD_TEST)
    _mark(eng, "core", "done")
    eng._prune_stale_causes()
    assert _open_causes(eng).get("core") == "request_shape", (
        "the offending field is still in core's test — no blind close")


# --- RED B: the test-status gate has the SAME addressing bug ------------------

def test_status_finding_in_foreign_test_file_lands_on_file_owner(tmp_path):
    eng = _engine(tmp_path)
    _write(eng, _TEST_REL, """\
        def _call(method, path):
            return 200


        def test_about():
            code = _call("GET", "/about")
            assert code == 201
    """)
    ok = eng._leaf_test_status_gate(dict(_AMEND), "about_page", 1, _TEST_REL)
    assert ok is False, "GET /about contracts 200 — asserting 201 reds"
    causes = _open_causes(eng)
    assert "about_page" not in causes and "core" in causes, (
        "the wrong assert lives in tests/test_core.py — the cause belongs "
        f"to core (the file owner), got {causes}")
    loops = [l for l in eng.loops if l["type"] == "test-status-mismatch"]
    assert loops and loops[0]["task"] == "core"


# --- RED C: body-shape findings (same gate) land on the file owner too --------

def test_body_shape_finding_in_foreign_test_file_lands_on_file_owner(tmp_path):
    eng = _engine(tmp_path)
    _write(eng, _TEST_REL, """\
        def _call(method, path):
            return "<ul></ul>"


        def test_notes_html():
            body = _call("GET", "/notes")
            assert "<ul" in body
    """)
    ok = eng._leaf_test_status_gate(dict(_AMEND), "about_page", 1, _TEST_REL)
    assert ok is False, "GET /notes contracts a JSON body — HTML marker reds"
    causes = _open_causes(eng)
    assert "about_page" not in causes and "core" in causes, (
        "the HTML assertion lives in core's test file — core is the "
        f"addressee, got {causes}")


# --- GREEN: a node's OWN test defect still lands on the node itself -----------

def test_own_test_violation_still_lands_on_the_leaf_itself(tmp_path):
    eng = _engine(tmp_path)
    _write(eng, "src/core.py", """\
        def post_notes(payload, query):
            return 201, {"id": 1}
    """)
    _write(eng, _TEST_REL, """\
        from core import post_notes


        def test_post():
            code, body = post_notes({"text": "hi", "junk": 1}, {})
            assert code == 201
    """)
    ok = eng._leaf_request_shape_gate(dict(_CORE), "core", 1,
                                      "src/core.py", _TEST_REL)
    assert ok is False
    assert _open_causes(eng).get("core") == "request_shape", (
        "core judging its OWN test file blames itself — artifact-ownership "
        "addressing never re-routes a node's own defects elsewhere")


def test_own_leaf_violation_lands_on_that_leaf_not_core(tmp_path):
    # the exact green from the defect statement: a violation in about_page's
    # OWN test (a real leaf with its own files) lands on about_page
    eng = _engine(tmp_path)
    node = {"id": "about_page", "title": "About page",
            "requirement": "own GET /about"}
    eng._module_for("about_page")
    _write(eng, "src/about_page.py", _CLEAN_CODE)
    _write(eng, "tests/test_about_page.py", _BAD_TEST)
    ok = eng._leaf_request_shape_gate(node, "about_page", 1,
                                      "src/about_page.py",
                                      "tests/test_about_page.py")
    assert ok is False
    causes = _open_causes(eng)
    assert causes.get("about_page") == "request_shape", (
        f"about_page's own test defect belongs to about_page, got {causes}")
    assert "core" not in causes
