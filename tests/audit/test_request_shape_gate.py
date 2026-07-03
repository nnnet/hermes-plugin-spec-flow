"""Audit rule S12.1 (v159): the REQUEST shape of a route is engine data —
config surface (env vars) must never be re-read as request-body validation.

v159 (p6-micro-notes): the constitution says "Every test sets the NOTES_DB
env var" (a CONFIG surface); the assembled product answered POST /notes and
GET /notes with 400 {"error": "missing required field: 'NOTES_DB'"} — a
config KeyError surfaced as request validation, and NOTHING could red the
confusion earlier because the per-route REQUEST FIELDS lived nowhere as an
engine datum: coder and tester could agree on the same wrong reading and
stay green until product e2e.

Contract enforced (all deterministic, S10.27/S11 spirit):
  * per-route REQUEST FIELDS are ONE engine datum
    (``_route_request_fields``) derived from the HUMAN prose ("POST /notes
    accepts {\"text\": ...}"); bodyless-method routes (GET/DELETE) contract
    the EMPTY shape; a body route the human never shaped is absent
    (lenient);
  * ENV VAR names the constitution states ("the NOTES_DB env var") are
    CONFIG data (``_constitution_env_vars``, the S10.22 pinned-path
    analogue);
  * the owner leaf's binding PRINTS both (fields list + 'NOTES_DB is an
    environment variable, never a request field');
  * the machine contract (contracts/interface.json) carries
    ``request_fields`` per route;
  * the leaf gate (``_leaf_request_shape_gate``) reds a HANDLER whose
    required-field checks (payload[...] subscripts / membership tests) name
    a field outside the contracted shape — naming the config-vs-payload
    confusion when the field is a constitution env var — and reds a leaf
    TEST that sends payload fields outside the contract;
  * GREEN edges: a handler requiring only contracted fields passes; a leaf
    with no contracted request shape is untouched (one false positive sinks
    a run, v151).

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_GOAL = (
    'A tiny notes service. Two HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first.')
_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`), storage through sqlite3 in src/db.py.",
    "The product API contract is FROZEN: POST /notes takes {text} and "
    "responds {id}; GET /notes responds {items} newest-first.",
    "Every test sets the NOTES_DB env var to a fresh temp path BEFORE "
    "connecting (db.connect reads it per call).",
]
_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
    "media": {"/notes": "json", "/health": "json"},
}
_NODE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}


def _engine(tmp_path, goal=_GOAL, constitution=_CONSTITUTION):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = goal
    eng._constitution = list(constitution)
    return eng


def _gate(tmp_path, code_body: str, test_body: str = "",
          goal=_GOAL, constitution=_CONSTITUTION):
    eng = _engine(tmp_path, goal, constitution)
    root = pathlib.Path(eng.workspace.root)
    code_rel, test_rel = "src/core.py", "tests/test_core.py"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / code_rel).write_text(textwrap.dedent(code_body), encoding="utf-8")
    (root / test_rel).write_text(textwrap.dedent(test_body), encoding="utf-8")
    ok = eng._leaf_request_shape_gate(dict(_NODE), "core", 1,
                                      code_rel, test_rel)
    return ok, [l for l in eng.loops if l["type"] == "request-shape-mismatch"]


# ---- the datum --------------------------------------------------------------

def test_request_fields_derived_from_human_prose(tmp_path):
    reqf = _engine(tmp_path)._route_request_fields()
    assert reqf.get(("POST", "/notes")) == ["text"], (
        "the p6 goal literally says 'POST /notes accepts {\"text\": ...}' — "
        "the per-route request shape must be derived as engine data")
    assert reqf.get(("GET", "/notes")) == [], (
        "a GET route carries no request body — the contracted shape is EMPTY")
    assert reqf.get(("GET", "/health")) == []


def test_unshaped_body_route_is_absent(tmp_path):
    # lenient: a body route the human never shaped has NO datum (never guessed)
    eng = _engine(tmp_path, goal="a service with POST /jobs and GET /jobs",
                  constitution=[])
    eng._product_contract = lambda: {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"json_roundtrip": "/jobs"}, "routes": [], "media": {}}
    assert ("POST", "/jobs") not in eng._route_request_fields()


def test_env_var_extracted_from_constitution(tmp_path):
    got = dict(_engine(tmp_path)._constitution_env_vars())
    assert "NOTES_DB" in got, (
        "'the NOTES_DB env var' names CONFIG data — the S10.22 analogue for "
        "environment variables")
    for word in ("ONLY", "FROZEN", "HTTP", "API", "BEFORE", "GET", "POST"):
        assert word not in got, (
            "plain ALL-CAPS prose words must never be extracted as env vars "
            "(false positive class, v151 lesson): %r" % word)


# ---- RED: the v159 defect shape ---------------------------------------------

def test_handler_requiring_env_var_in_payload_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            path = payload["NOTES_DB"]
            text = payload["text"]
            return 201, {"id": 1}
    """)
    assert not ok and loops, (
        "a handler demanding the NOTES_DB env var as a REQUEST field while "
        "the contract shapes POST /notes = ['text'] is the v159 e2e killer "
        "— it must red at the leaf")
    detail = loops[0]["detail"]
    assert "NOTES_DB" in detail
    assert re.search(r"environment variable", detail, re.IGNORECASE), (
        "the finding must NAME the config-vs-payload confusion")
    assert "text" in detail, "the finding must show the contracted shape"


def test_handler_requiring_foreign_plain_field_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            if "title" not in payload:
                return 400, {"error": "missing required field: 'title'"}
            return 201, {"id": 1}
    """)
    assert not ok and loops, (
        "a required-field check naming a field outside the contracted "
        "request shape must red — the request shape is engine data")
    assert "title" in loops[0]["detail"]


def test_get_handler_requiring_payload_field_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def get_notes(payload, query):
            payload["NOTES_DB"]
            return 200, {"items": []}
    """)
    assert not ok and loops, (
        "a GET route contracts NO request body — a handler requiring any "
        "payload field on it is the same v159 confusion")


def test_leaf_test_sending_foreign_field_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            return 201, {"id": 1}
    """, """\
        from core import post_notes


        def test_post():
            code, body = post_notes({"NOTES_DB": "/tmp/x", "text": "hi"}, {})
            assert code == 201
    """)
    assert not ok and loops, (
        "a leaf test SENDING a field outside the contracted request shape "
        "reds — two guesses agreeing on a wrong reading stayed green in v159")
    assert "NOTES_DB" in loops[0]["detail"]


# ---- GREEN edges ------------------------------------------------------------

def test_handler_requiring_contracted_field_passes(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            text = payload["text"]
            return 201, {"id": 1}


        def get_notes(payload, query):
            return 200, {"items": []}
    """, """\
        from core import post_notes


        def test_post():
            code, body = post_notes({"text": "hi"}, {})
            assert code == 201
    """)
    assert ok and not loops, (
        "a handler requiring exactly the contracted field must stay green")


def test_optional_get_access_is_lenient(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            text = payload.get("text", "")
            return 201, {"id": 1}
    """)
    assert ok and not loops, (
        "payload.get(...) is not a REQUIRED-field check — optional access "
        "never reds (leniency: one false positive sinks a run)")


def test_leaf_without_contracted_shape_untouched(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def post_notes(payload, query):
            return 201, {"id": payload["whatever"]}
    """, goal="", constitution=[])
    assert ok and not loops, (
        "no contracted request shape for the route = the gate is a no-op")


# ---- the datum is printed and persisted --------------------------------------

def test_binding_prints_request_shape_and_env_var(tmp_path):
    binding = _engine(tmp_path)._leaf_route_binding(dict(_NODE))
    assert "'text'" in binding or '"text"' in binding, (
        "the owner leaf's binding must PRINT the contracted request fields")
    assert "NOTES_DB" in binding
    assert re.search(r"environment variable", binding, re.IGNORECASE), (
        "the binding must state that NOTES_DB is an environment variable, "
        "never a request field")


def test_interface_contract_carries_request_fields(tmp_path):
    eng = _engine(tmp_path)
    pathlib.Path(eng.workspace.root).mkdir(parents=True, exist_ok=True)
    eng._write_interface_contract()
    data = json.loads((pathlib.Path(eng.workspace.root)
                       / "contracts/interface.json").read_text())
    rows = {(r["method"], r["path"]): r for r in data["routes"]}
    assert rows[("POST", "/notes")].get("request_fields") == ["text"], (
        "contracts/interface.json must carry the request shape datum "
        "(S10.27 pattern: machine-readable, one source)")
    assert rows[("GET", "/notes")].get("request_fields") == []


def test_gate_reachable_from_pipeline():
    # S7.2: the gate must be CALLED where its siblings run, not orphaned
    src = pathlib.Path(sfr.__file__).read_text(encoding="utf-8")
    called = re.findall(r"self\._leaf_request_shape_gate\(", src)
    assert called, "_leaf_request_shape_gate is never invoked by the pipeline"
