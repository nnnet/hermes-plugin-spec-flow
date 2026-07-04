"""Audit rules S13.1/S13.3/S13.5 (Phase A, spec-IR): the per-node interface
is REAL OpenAPI 3.1, built purely from recorded engine datums.

Plan 2026-07-04T00-45, Addendum 2: the interface part of the IR must be a
GENUINE OpenAPI 3.1 document fragment (openapi: "3.1.0", info, paths) so
off-the-shelf contract tools (Specmatic / Schemathesis, Phase B) can consume
it unmodified. `spec_ir.build_ir(engine)` assembles it FROM the datums the
engine already records — `_route_owners`, `_route_request_fields`,
`_route_media_map`, `_route_success_status`, `_route_fixed_body`,
`_canonical_handler_symbol` — and NEVER invents a value: a missing datum
leaves the field ABSENT and `validate_ir` reports it as an incompleteness
finding (honest gap, never a guessed default).

Named past-failure case: v164 media drift (/about served JSON against the
contracted text/html) must be expressible as an IR validation error.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

_GOAL = (
    'A tiny notes service. Two HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first.')
_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`), storage through sqlite3 in src/db.py.",
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


def _engine(tmp_path, contract=_CONTRACT):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(contract)
    eng._goal = _GOAL
    eng._constitution = list(_CONSTITUTION)
    return eng


def _built(tmp_path, contract=_CONTRACT, node=_NODE):
    eng = _engine(tmp_path, contract)
    owned = eng._leaf_owned_routes(dict(node))
    assert owned, "precondition: the leaf must own the declared routes"
    return eng, spec_ir.build_ir(eng)


# ---- the document is real OpenAPI 3.1 ----------------------------------------

def test_node_openapi_is_a_real_31_document(tmp_path):
    _, ir = _built(tmp_path)
    assert ir["format"] == spec_ir.IR_FORMAT
    doc = ir["nodes"]["core"]["openapi"]
    assert doc["openapi"] == "3.1.0", (
        "the interface fragment must declare the REAL OpenAPI 3.1 version "
        "string so Specmatic/Schemathesis can consume it (Phase B)")
    assert doc["info"].get("title") and doc["info"].get("version"), (
        "info.title and info.version are REQUIRED by the OpenAPI standard")
    assert set(doc["paths"]) == {"/notes", "/health"}, (
        "paths must carry exactly the routes this node OWNS")


def test_request_body_schema_from_request_fields_datum(tmp_path):
    _, ir = _built(tmp_path)
    post = ir["nodes"]["core"]["openapi"]["paths"]["/notes"]["post"]
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["text"], (
        "requestBody required fields come from the `_route_request_fields` "
        "datum ('POST /notes accepts {\"text\": ...}') — never invented")
    assert schema.get("additionalProperties") is False, (
        "S12.1 closed request shape: fields outside the contract are red — "
        "the schema must say additionalProperties: false")
    assert set(schema["properties"]) == {"text"}


def test_success_status_and_media_from_datums(tmp_path):
    _, ir = _built(tmp_path)
    paths = ir["nodes"]["core"]["openapi"]["paths"]
    post = paths["/notes"]["post"]
    assert list(post["responses"]) == ["201"], (
        "the response status is `_route_success_status` — the v149 "
        "200-vs-201 collision datum, one source")
    assert "application/json" in post["responses"]["201"]["content"], (
        "response media comes from the `_route_media_map` datum")
    get = paths["/notes"]["get"]
    assert list(get["responses"]) == ["200"]


def test_fixed_body_route_carries_const_schema(tmp_path):
    _, ir = _built(tmp_path)
    health = ir["nodes"]["core"]["openapi"]["paths"]["/health"]["get"]
    content = health["responses"]["200"]["content"]["application/json"]
    assert content["schema"] == {"const": {"status": "ok"}}, (
        "GET /health answers exactly {'status': 'ok'} — the "
        "`_route_fixed_body` datum lands as a JSON Schema const, v150's "
        "three-way body split can no longer happen")


def test_canonical_handler_recorded_as_extension(tmp_path):
    _, ir = _built(tmp_path)
    post = ir["nodes"]["core"]["openapi"]["paths"]["/notes"]["post"]
    assert post["x-spec-flow-handler"] == "post_notes", (
        "the engine-declared canonical handler travels with the interface "
        "as a standard x- extension")


def test_engine_built_ir_validates_clean(tmp_path):
    _, ir = _built(tmp_path)
    rep = spec_ir.validate_ir(ir)
    assert rep["errors"] == [], (
        "the IR built from the engine's own datums must be internally "
        "consistent — an error here is an engine datum drift: %r"
        % rep["errors"])


# ---- honest gaps: absent datum => absent field + incompleteness finding -------

def test_missing_media_datum_is_absent_and_reported(tmp_path):
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"],
                "boot": {}, "routes": [["GET", "/about"]], "media": {}}
    node = {"id": "about_page", "title": "About page",
            "requirement": "own GET /about"}
    _, ir = _built(tmp_path, contract, node)
    get = ir["nodes"]["about_page"]["openapi"]["paths"]["/about"]["get"]
    assert "content" not in get["responses"]["200"], (
        "no media datum recorded for /about => the field is ABSENT, "
        "never a guessed default")
    rep = spec_ir.validate_ir(ir)
    assert any("/about" in f and "media" in f.lower()
               for f in rep["incomplete"]), (
        "the validator must report the gap as an incompleteness finding "
        "naming the route: %r" % rep["incomplete"])


def test_unshaped_body_route_has_no_request_body_and_is_reported(tmp_path):
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"],
                "boot": {}, "routes": [["POST", "/jobs"]],
                "media": {"/jobs": "json"}}
    node = {"id": "jobs", "title": "Jobs",
            "requirement": "own POST /jobs"}
    eng = _engine(tmp_path, contract)
    eng._goal = "a service with POST /jobs"      # human never shaped the body
    eng._constitution = []
    assert eng._leaf_owned_routes(dict(node))
    ir = spec_ir.build_ir(eng)
    post = ir["nodes"]["jobs"]["openapi"]["paths"]["/jobs"]["post"]
    assert "requestBody" not in post, (
        "a body route the human never shaped has NO request-shape datum "
        "(lenient) => requestBody is ABSENT, never guessed")
    rep = spec_ir.validate_ir(ir)
    assert any("/jobs" in f for f in rep["incomplete"]), (
        "the absent request shape is an honest incompleteness finding")


# ---- v164: media drift is an IR validation error ------------------------------

def test_v164_media_drift_is_ir_error(tmp_path):
    # v164: the assembled /about answered a JSON dict while
    # contracts/interface.json contracted text/html — the drift lived
    # between two artifacts. In the IR a scenario asserting a media that
    # contradicts the owning openapi response is a VALIDATION error.
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"],
                "boot": {}, "routes": [["GET", "/about"]],
                "media": {"/about": "html"}}
    node = {"id": "about_page", "title": "About page",
            "requirement": "own GET /about"}
    _, ir = _built(tmp_path, contract, node)
    ir["nodes"]["about_page"]["scenarios"] = [
        {"requirement": "about_page",
         "when": {"method": "GET", "path": "/about"},
         "then": {"status": 200, "media": "application/json"}}]
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("/about" in e and "application/json" in e
               and "text/html" in e for e in errs), (
        "the v164 class — scenario media vs contracted media — must be a "
        "named IR error carrying BOTH medias: %r" % errs)


def test_scenario_status_outside_responses_is_error(tmp_path):
    _, ir = _built(tmp_path)
    ir["nodes"]["core"]["scenarios"] = [
        {"requirement": "core",
         "when": {"method": "GET", "path": "/notes"},
         "then": {"status": 418}}]
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("418" in e for e in errs), (
        "a scenario asserting a status the interface never declares is the "
        "v149 status-guess class inside one structure: %r" % errs)


# ---- ownership integrity from the datum ---------------------------------------

def test_datum_level_duplicate_ownership_reds_at_validation(tmp_path):
    eng = _engine(tmp_path)
    assert eng._leaf_owned_routes(dict(_NODE))
    # simulate a datum drift: a second owner recorded for POST /notes
    eng.__dict__["_route_owners"][("POST", "/notes")].add("rogue")
    ir = spec_ir.build_ir(eng)
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("/notes" in e and "rogue" in e for e in errs), (
        "an inconsistent ownership datum must surface as an IR error, "
        "never be silently deduplicated: %r" % errs)


# ---- symbols and env merged from their datums ----------------------------------

def test_symbols_merged_from_module_contract_datum(tmp_path):
    eng = _engine(tmp_path)
    assert eng._leaf_owned_routes(dict(_NODE))
    eng._register_module_import(
        "core", {"id": "db", "exposes": ["connect(path)"]})
    ir = spec_ir.build_ir(eng)
    exposes = ir["nodes"]["db"]["symbols"]["exposes"]
    assert {"name": "connect", "args": ["path"]} in exposes, (
        "exposes comes from the S11 `_module_contracts` datum verbatim")
    consumes = ir["nodes"]["core"]["symbols"]["consumes"]
    assert any(c["name"] == "connect" and c["from"] == "db"
               for c in consumes), (
        "the importer's consumable surface is the exporter's contract — "
        "the same datum, so consumed and exposed can never drift")
    assert spec_ir.validate_ir(ir)["errors"] == []


def test_env_vars_merged_from_constitution_datum(tmp_path):
    _, ir = _built(tmp_path)
    names = [e["name"] for e in ir["nodes"]["core"].get("env", [])]
    assert "NOTES_DB" in names, (
        "constitution env vars (`_constitution_env_vars`) attach to the "
        "nodes that may read config — the S12.1 config surface as IR data")


def test_ir_never_invents_nodes_or_routes(tmp_path):
    eng = _engine(tmp_path, {"entry": "src/app.py", "callable": ["wsgi_app"],
                             "boot": {}, "routes": [], "media": {}})
    ir = spec_ir.build_ir(eng)
    assert ir["nodes"] == {}, (
        "no datum recorded => no node entry; the builder NEVER invents")
