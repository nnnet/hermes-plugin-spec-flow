"""Audit STAGE 16 (S16.1-S16.5): one product-level OpenAPI 3.1 document
compiled from ir.json for external contract oracles.

Plan 2026-07-04T00-45, node B2 (`contract-oracle`): the per-node OpenAPI
fragments inside the IR are merged by `spec_openapi.compile_openapi` into ONE
document that Schemathesis / Specmatic consume unmodified. The compiler NEVER
invents: paths come verbatim from the fragments, and the injected error
responses mirror EXACTLY what the engine's synthesized router
(`_synthesize_entry_code`) actually does — 404 unknown path, 405 wrong
method, 500 escaped handler exception, and 400 ONLY where the contracted
requestBody has required fields (the router's own S12.1 validation). This
closes Phase A open question #2 (router error responses as shared data).

Named directions per gate (both-directions convention):
- S16.1 duplicate (method, path) across nodes = NAMED refusal; distinct
  routes merge green.
- S16.2 router error responses present on every operation and TRUTHFUL
  (400 only where required fields exist; never overwrite a fragment status).
- S16.3 `lint_openapi` reds an operation missing `responses`; the compiled
  document from a valid IR lints clean.
- S16.4 no media recorded compiles to an honest gap finding, never a guessed
  content type.
- S16.5 external-oracle harness argument assembly is unit-tested (inspection
  level) so it is correct even where pip/java are unavailable.

Deterministic: crafted IR dicts + engine unit calls over a tmp workspace,
no LLM, no network.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402
from tests.tools import run_schemathesis, run_specmatic  # noqa: E402


# ---- crafted-IR helpers (shape matches spec_ir.build_ir output) ---------------

def _frag(nid: str, paths: dict) -> dict:
    return {"openapi": "3.1.0",
            "info": {"title": "spec-flow node %s interface" % nid,
                     "version": "1"},
            "paths": paths}


def _op(handler: str, status: str = "200", media: str = "application/json",
        required: list | None = None) -> dict:
    op: dict = {"x-spec-flow-handler": handler,
                "responses": {status: {"description":
                                       "contracted success response",
                                       "content": {media: {"schema": {}}}}}}
    if required is not None:
        op["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {f: {} for f in required},
                "required": list(required),
                "additionalProperties": False}}}}
    return op


def _ir(nodes: dict) -> dict:
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"]},
            "nodes": nodes}


_TWO_NODE_IR = _ir({
    "notes": {"files": ["src/notes.py"], "openapi": _frag("notes", {
        "/notes": {
            "post": _op("post_notes", status="201", required=["text"]),
            "get": _op("get_notes")}})},
    "about": {"files": ["src/about.py"], "openapi": _frag("about", {
        "/about": {"get": _op("get_about", media="text/html")}})},
})


# ---- S16.1 one document, ownership honored ------------------------------------

def test_compile_merges_node_fragments_into_one_document():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    assert doc["openapi"] == "3.1.0", (
        "the compiled document must declare the REAL OpenAPI 3.1 version "
        "string so Schemathesis/Specmatic consume it unmodified")
    assert doc["info"].get("title") and doc["info"].get("version"), (
        "info.title and info.version are REQUIRED by the OpenAPI standard")
    assert set(doc["paths"]) == {"/notes", "/about"}, (
        "ONE paths object carrying every node's routes verbatim")
    assert doc["paths"]["/notes"]["post"]["x-spec-flow-node"] == "notes", (
        "every operation must carry its owning node for traceability")
    assert doc["paths"]["/about"]["get"]["x-spec-flow-node"] == "about"


def test_duplicate_route_across_nodes_is_named_refusal():
    ir = _ir({
        "a": {"openapi": _frag("a", {"/dup": {"get": _op("get_dup")}})},
        "b": {"openapi": _frag("b", {"/dup": {"get": _op("get_dup")}})},
    })
    with pytest.raises(spec_openapi.DuplicateRouteError) as exc:
        spec_openapi.compile_openapi(ir)
    msg = str(exc.value)
    assert "GET" in msg and "/dup" in msg and "a" in msg and "b" in msg, (
        "the refusal must NAME the method, the path and both owning nodes "
        "(P4 attributable failure), never silently last-writer-wins: %s"
        % msg)


# ---- S16.2 router-truthful error responses ------------------------------------

def test_router_error_responses_injected_truthfully():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    post = doc["paths"]["/notes"]["post"]["responses"]
    for status in ("400", "404", "405", "500"):
        assert status in post, (
            "POST /notes has a required requestBody field, so the router "
            "can answer every one of 400/404/405/500 — %s missing" % status)
        assert "$ref" in post[status], (
            "error responses are $refs into ONE shared components section, "
            "never per-operation copies")
    get_notes = doc["paths"]["/notes"]["get"]["responses"]
    assert "400" not in get_notes, (
        "TRUTHFUL to the router template: a GET has no request-body "
        "validation, the router never answers it 400 — injecting one would "
        "invent behavior the router does not have")
    for status in ("404", "405", "500"):
        assert status in get_notes
    about = doc["paths"]["/about"]["get"]["responses"]
    assert "400" not in about and {"404", "405", "500"} <= set(about)


def test_bodied_operation_without_required_fields_gets_no_400():
    ir = _ir({"jobs": {"openapi": _frag("jobs", {
        "/jobs": {"post": _op("post_jobs", status="201", required=[])}})}})
    doc = spec_openapi.compile_openapi(ir)
    responses = doc["paths"]["/jobs"]["post"]["responses"]
    assert "400" not in responses, (
        "an empty contracted shape means the router's _REQUIRED table has "
        "no row for this route — no missing-field 400 exists to advertise")


def test_error_components_mirror_router_bodies():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    comps = doc["components"]["responses"]
    texts = {name: str(comps[name].get("description", ""))
             for name in comps}
    joined = " ".join(texts.values())
    for phrase in ("missing required field", "invalid json",
                   "empty request body", "not found", "method not allowed",
                   "internal:"):
        assert phrase in joined, (
            "the shared error responses must quote the router's ACTUAL "
            "bodies (from _synthesize_entry_code), '%s' absent" % phrase)
    schema = doc["components"]["schemas"]["RouterError"]
    assert schema["required"] == ["error"], (
        "every router error body is exactly {'error': <string>}")
    for name, resp in comps.items():
        content = resp["content"]["application/json"]
        assert content["schema"] == {
            "$ref": "#/components/schemas/RouterError"}, (
            "component %s must reference the ONE RouterError schema" % name)


def test_injection_never_overwrites_fragment_declared_status():
    frag = _frag("a", {"/thing": {"get": _op("get_thing")}})
    frag["paths"]["/thing"]["get"]["responses"]["404"] = {
        "description": "domain not-found contracted by the node"}
    doc = spec_openapi.compile_openapi(_ir({"a": {"openapi": frag}}))
    resp = doc["paths"]["/thing"]["get"]["responses"]["404"]
    assert resp.get("description") == (
        "domain not-found contracted by the node"), (
        "the fragment is the contract source: a status it already declares "
        "is NEVER overwritten by the injected router $ref: %r" % resp)


# ---- S16.3 structural self-lint ------------------------------------------------

def test_lint_rejects_operation_missing_responses():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    del doc["paths"]["/notes"]["get"]["responses"]
    findings = spec_openapi.lint_openapi(doc)
    assert any(f["rule"] == "missing-responses" and "/notes" in f["where"]
               for f in findings), (
        "an operation without responses is a NAMED finding carrying its "
        "location: %r" % findings)


def test_lint_flags_unresolved_local_ref():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    doc["paths"]["/about"]["get"]["responses"]["500"] = {
        "$ref": "#/components/responses/Nope"}
    findings = spec_openapi.lint_openapi(doc)
    assert any(f["rule"] == "unresolved-ref" and "Nope" in f["message"]
               for f in findings), (
        "a $ref pointing nowhere inside the document is a finding: %r"
        % findings)


def test_lint_flags_missing_top_keys_and_bad_version():
    findings = spec_openapi.lint_openapi({"openapi": "2.0"})
    rules = {f["rule"] for f in findings}
    assert "bad-version" in rules and "missing-top-key" in rules, (
        "required top-level keys and the 3.1 version string are the "
        "OpenAPI rules we CAN check ourselves (stdlib): %r" % findings)


def test_compiled_document_from_valid_ir_lints_clean():
    doc = spec_openapi.compile_openapi(_TWO_NODE_IR)
    assert spec_openapi.lint_openapi(doc) == [], (
        "the compiler's own output must pass its own structural lint — "
        "anything else means we ship a document the oracles reject")


# ---- S16.4 honest media gap ----------------------------------------------------

def test_missing_media_compiles_to_gap_never_guess():
    frag = _frag("about_page", {"/about": {"get": {
        "x-spec-flow-handler": "get_about",
        "responses": {"200": {"description":
                              "contracted success response"}}}}})
    doc = spec_openapi.compile_openapi(_ir({"about_page":
                                            {"openapi": frag}}))
    resp = doc["paths"]["/about"]["get"]["responses"]["200"]
    assert "content" not in resp, (
        "no media datum recorded => the compiled response stays WITHOUT "
        "content — never a guessed content type (S13.1 downstream)")
    gaps = doc.get("x-spec-flow-gaps", [])
    assert any("about_page" in g and "GET" in g and "/about" in g
               and "media" in g.lower() for g in gaps), (
        "the gap must be an honest finding naming node, method and path: %r"
        % gaps)
    assert spec_openapi.lint_openapi(doc) == [], (
        "a description-only response is legal OpenAPI — the gap is a "
        "finding, not a lint failure")


# ---- S16.5 external oracle harnesses (argument assembly, inspection level) ------

def test_schemathesis_harness_command_assembly():
    cmd = run_schemathesis.build_command("out/openapi.json",
                                         "http://127.0.0.1:8000")
    assert cmd[1] == "run" and "out/openapi.json" in cmd, (
        "schemathesis CLI shape is `<cli> run <schema> --url <base>`: %r"
        % cmd)
    i = cmd.index("--url")
    assert cmd[i + 1] == "http://127.0.0.1:8000"
    extra = run_schemathesis.build_command("s.json", "http://x", ["--max-examples", "5"])
    assert extra[-2:] == ["--max-examples", "5"], (
        "extra args pass through verbatim after the base invocation")


def test_specmatic_harness_command_assembly():
    cmd = run_specmatic.build_command("tools/specmatic.jar",
                                      "out/openapi.json",
                                      "http://127.0.0.1:8000")
    assert cmd[:3] == ["java", "-jar", "tools/specmatic.jar"], (
        "specmatic is a java jar: `java -jar <jar> test <spec> "
        "--testBaseURL=<base>`: %r" % cmd)
    assert "test" in cmd and "out/openapi.json" in cmd
    assert any(a.startswith("--testBaseURL") for a in cmd)


def test_probes_report_availability_honestly():
    st = run_schemathesis.probe()
    assert set(st) >= {"available", "cli", "package"}, (
        "the probe names WHAT is missing so the [!] human list is precise")
    sp = run_specmatic.probe()
    assert set(sp) >= {"available", "java", "jar"}
    assert isinstance(st["available"], bool)
    assert isinstance(sp["available"], bool)


# ---- end-to-end: the engine's own ir.json compiles and lints clean --------------

_GOAL = (
    'A tiny notes service. Two HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first.')
_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`), storage through sqlite3 in src/db.py.",
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


def test_engine_built_ir_compiles_and_lints_clean(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    eng._constitution = list(_CONSTITUTION)
    assert eng._leaf_owned_routes(dict(_NODE))
    ir = spec_ir.build_ir(eng)
    doc = spec_openapi.compile_openapi(ir)
    assert set(doc["paths"]) == {"/notes", "/health"}
    post = doc["paths"]["/notes"]["post"]["responses"]
    assert {"201", "400", "404", "405", "500"} <= set(post), (
        "the REAL engine datums produce a document with the contracted "
        "success status plus every router error the route can answer")
    health = doc["paths"]["/health"]["get"]["responses"]
    assert "400" not in health, (
        "GET /health carries no request shape — a 400 there would be "
        "invented behavior")
    assert spec_openapi.lint_openapi(doc) == [], (
        "the document compiled from the engine's own ir.json is what the "
        "external oracles eat — it must be structurally flawless")
