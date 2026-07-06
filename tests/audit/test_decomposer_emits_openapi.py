"""Audit STAGE 22 — the decomposer's per-node OpenAPI 3.1 document is the
PRIMARY node interface and is LIBRARY-validated at the decomposer seam
(node K2, plan 2026-07-04T00-45; user superpriority 2026-07-06 — "use the
OpenAPI library; make the machine document the primary carrier, prose a
derived fallback").

Why this stage exists (the hole K2 closes):
    E1 (Stage 15) made the decomposer emit a MACHINE part — per-node
    ``openapi`` fragments (a real OpenAPI 3.1 document each) — and validated
    it at ``_accept_decomposer_ir`` with ``spec_ir.validate_ir``. But
    validate_ir is the engine's HAND-ROLLED closed-world check; it does NOT
    run the node's OpenAPI document through the maintained third-party
    ``openapi-spec-validator`` (the S16.7 / K1 oracle). So a node whose
    OpenAPI document is structurally INVALID by the standard (a wrong-typed
    ``schema``, a dangling local ``$ref``, a list where the standard wants an
    object) sails through the seam SILENTLY — no named refusal, prose is never
    even consulted, and the drift only surfaces (if at all) at assembly. That
    is the exact v165 class one layer up: an interface fact never
    library-checked from the first step.

    K2 makes the machine OpenAPI document the PRIMARY carrier: EVERY node's
    OpenAPI document is validated by the LIBRARY at the decomposer seam, and a
    library-invalid document is an HONEST NAMED refusal (milestone gate
    ``decomposer_openapi`` FAIL — the ``decomposer_ir`` precedent), never a
    silent fallback to prose. Under ``interface_policy: ir-required`` a node
    that arrives as prose without a valid machine OpenAPI document is a
    failing product check (the H8/S15.8 seam) — prose-as-carrier = refusal.

Ratchet (RED before code): the RED case below MUST fail on the pre-K2 engine —
a node with a library-invalid OpenAPI document is accepted by
``_accept_decomposer_ir`` with ZERO errors (the bug) — and go GREEN once the
seam library-validates each node's document.

Test: run ``python -m pytest tests/audit/test_decomposer_emits_openapi.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402


def _engine(tmp_path, interface_policy="ir-required", routes=None):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC,
                   interface_policy=interface_policy)
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": routes if routes is not None else [("GET", "/ui")],
    }
    return e


def _valid_openapi(nid, method, path, status="200",
                   media="application/json"):
    """A per-node OpenAPI 3.1 document the LIBRARY accepts unmodified."""
    op = {"responses": {str(status): {"description": "success",
                                      "content": {media: {"schema": {}}}}}}
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": {path: {method.lower(): op}}}


def _library_invalid_openapi(nid, method="GET", path="/x"):
    """A document our HAND-ROLLED validate_ir passes but the standard library
    rejects: the response schema is a string, not a JSON Schema object.

    This is the K2 hole made concrete — the shape reaches _accept_decomposer_ir
    and the engine's own closed-world check has nothing to say about it, so
    only the third-party oracle can name it.
    """
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": {path: {method.lower(): {"responses": {"200": {
                "description": "success",
                "content": {"application/json": {
                    "schema": "not-an-object"}}}}}}}}


def _machine_part(nodes):
    return {"format": spec_ir.IR_FORMAT, "nodes": nodes}


def _gate_events(e, gate, verdict=None):
    return [ev for ev in e.events
            if ev.gate == gate and (verdict is None or ev.verdict == verdict)]


# -- premise: the hand-rolled and library oracles genuinely DISAGREE ----------

def test_library_invalid_doc_slips_past_hand_rolled_validate_ir():
    """The premise of this stage: validate_ir (hand-rolled) is SILENT on the
    library-invalid document, so only a library check at the seam can catch
    it. If this ever stops holding the RED case is no longer a real hole and
    the fixture must be strengthened.

    Test: this test itself — hand errors empty, library errors non-empty."""
    doc = _library_invalid_openapi("core")
    ir = {"format": spec_ir.IR_FORMAT, "product": {},
          "nodes": {"core": {"files": ["src/core.py"], "openapi": doc}}}
    rep = spec_ir.validate_ir(ir)
    hand = [x for x in rep["errors"] if "exposed by no node" not in x]
    assert hand == [], (
        "premise broken — validate_ir now catches the library-invalid doc; "
        "pick a stronger fixture: %r" % hand)
    assert spec_openapi.validate_openapi_library(doc), (
        "the library MUST reject the wrong-typed schema for this stage to "
        "have a hole to close")


# -- S22.1 (RED before K2) library-invalid node document = NAMED refusal ------

def test_library_invalid_node_openapi_is_named_refusal_not_silent(tmp_path):
    """Why: a node OpenAPI document invalid by the STANDARD must not enter the
    engine as the interface truth. What: _accept_decomposer_ir returns a named
    error, emits a decomposer_openapi FAIL, and keeps the fragment out of the
    registry. Test: this function — RED on pre-K2 (errs == [])."""
    e = _engine(tmp_path)
    out = {"atomic": True, "ir": _machine_part({"core": {
        "files": ["src/core.py"],
        "openapi": _library_invalid_openapi("core")}})}
    errs = e._accept_decomposer_ir({"id": "core"}, out)
    assert errs, (
        "a node whose OpenAPI document is INVALID by the third-party library "
        "must be REFUSED at the decomposer seam — never accepted silently "
        "(the K2 hole: only validate_ir ran, the library never did)")
    assert any("core" in x for x in errs), (
        "the refusal must NAME the offending node (P4 attributability): %r"
        % errs)
    fails = _gate_events(e, "decomposer_openapi", verdict="FAIL")
    assert fails, (
        "the refusal must be a MILESTONE on gate decomposer_openapi, the "
        "decomposer_ir precedent — a visible honest FAIL, not a whisper")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "core" not in reg, (
        "a library-refused fragment must NOT enter the IR registry — a later "
        "consumer can never read a document that failed the standard oracle")


# -- S22.2 a valid node OpenAPI document passes the library seam (GREEN) ------

def test_valid_node_openapi_passes_the_library_seam(tmp_path):
    """Why: the library check must not false-red a conforming fragment. What:
    a standard-valid node document is accepted, no decomposer_openapi FAIL, the
    decomposer_ir PASS still journals. Test: this function."""
    e = _engine(tmp_path)
    out = {"atomic": True, "ir": _machine_part({"core": {
        "files": ["src/core.py"],
        "openapi": _valid_openapi("core", "POST", "/notes", "201")}})}
    assert e._accept_decomposer_ir({"id": "core"}, out) == [], (
        "a standard-valid node OpenAPI document must be accepted — the "
        "library check must not false-red a conforming fragment")
    assert not _gate_events(e, "decomposer_openapi", verdict="FAIL")
    assert _gate_events(e, "decomposer_ir", verdict="PASS"), (
        "an accepted fragment still journals the decomposer_ir PASS")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "core" in reg


# -- S22.3 the machine OpenAPI carries the route (v165 class impossible) ------

def test_route_lives_in_machine_openapi_from_the_first_step(tmp_path):
    """Why: v165 lost get_ping because the route travelled as PROSE. What: the
    node's route is present in its library-valid machine OpenAPI document from
    acceptance and ownership reads it — never a prose guess deferred to
    assembly. Test: this function (route in doc.paths, library-clean, owned)."""
    e = _engine(tmp_path, routes=[("GET", "/ping")])
    out = {"atomic": True, "ir": _machine_part({"core": {
        "files": ["src/core.py"],
        "openapi": _valid_openapi("core", "GET", "/ping", "200")}})}
    assert e._accept_decomposer_ir({"id": "core"}, out) == []
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    doc = (reg.get("core") or {}).get("openapi") or {}
    assert "/ping" in (doc.get("paths") or {}), (
        "the route must be present in the node's MACHINE OpenAPI document, "
        "not carried by prose")
    assert spec_openapi.validate_openapi_library(doc) == [], (
        "and that machine document is library-valid from the first step")
    owned = e._leaf_owned_routes({"id": "core"})
    assert ("GET", "/ping") in owned, (
        "ownership reads the machine document — the route is bound, never "
        "a prose guess deferred to assembly (the v165 class)")


# -- S22.4 under ir-required, a prose node (no valid machine doc) reds --------

def test_ir_required_reds_prose_node_without_machine_openapi(tmp_path):
    """Why: prose as the interface carrier is a refusal under ir-required
    (H8/S15.8) — the policy connective K2 must keep honest. What: a
    prose-derived node surfaces as a failing product check naming the node.
    Test: this function (fails non-empty, names the node)."""
    e = _engine(tmp_path, interface_policy="ir-required",
                routes=[("POST", "/notes"), ("GET", "/notes")])
    node = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    # no IR fragment supplied -> prose derivation records the node as prose
    e._leaf_owned_routes(node)
    fails = e._interface_policy_failures()
    assert fails and any("core" in m for m in fails), (
        "under ir-required a prose-carried interface must surface as a "
        "failing product check naming the node: %r" % fails)


def test_allow_prose_policy_does_not_red_a_prose_node(tmp_path):
    """Why: with explicit consent the historical prose fallback runs clean —
    the policy connective is symmetric, not a blanket ban. What: allow-prose
    yields no interface-policy failure. Test: this function (fails empty)."""
    e = _engine(tmp_path, interface_policy="allow-prose",
                routes=[("POST", "/notes"), ("GET", "/notes")])
    node = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    e._leaf_owned_routes(node)
    assert e._interface_policy_failures() == [], (
        "allow-prose must NOT red a prose-derived node")
