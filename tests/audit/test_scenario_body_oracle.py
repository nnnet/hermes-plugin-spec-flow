"""Audit rule S14.7: the scenario runner validates the LIVE response body
against the contracted CLOSED response schema with the THIRD-PARTY
openapi-schema-validator — not only the hand-rolled equals/contains/json_subset.

Nodes K1b + L1 (library-inventory audit, plan 2026-07-04T00-45; user
superpriority 2026-07-06 "use the OpenAPI library").

Why: `spec_scenarios._judge` judged a response body ONLY by hand —
`_json_subset` accepts any extra key and never type-checks against a schema.
So a live body that carries the contracted required field but ALSO an invented
field (under `additionalProperties: false`) or the WRONG type for a declared
field passed the runner silently. That is exactly the F1 exploit at RUNTIME:
the S21 closed response schema (`type: object` + properties/required/
additionalProperties:false) is a real contract, and a MAINTAINED JSON-Schema
library (openapi-schema-validator, wrapping jsonschema) is what closes it.

What is pinned here:
  * S14.7 RED — a live body violating the CLOSED response schema (wrong type,
    or an extra field under additionalProperties:false) that the hand
    json_subset PASSES must become a scenario FAILURE, caught BY THE LIBRARY.
  * S14.7 GREEN direction — an honest body matching the closed schema stays a
    pass; a route with NO closed response schema is judged exactly as before
    (the hand equals/contains/json_subset fallback is untouched); when the
    library is absent the runner falls back to the hand check and never
    hard-crashes.

Deterministic: in-process WSGI callables, no LLM, no network. The library is a
dev/test oracle pinned in tests/requirements-dev.txt.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_scenarios  # noqa: E402

IR_FORMAT = "spec-flow ir v1"


# --------------------------------------------------------------------------
# fixtures: an IR whose GET /widget declares a CLOSED response schema
# --------------------------------------------------------------------------

def _closed_schema():
    """S21 closed object: id required (integer), name string, nothing else."""
    return {"type": "object",
            "properties": {"id": {"type": "integer"},
                           "name": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False}


def _op_closed(status=200, media="application/json"):
    """One operation with a single CLOSED success response schema."""
    return {"responses": {str(status): {
        "description": "contracted success response",
        "content": {media: {"schema": _closed_schema()}}}}}


def _openapi(paths):
    return {"openapi": "3.1.0",
            "info": {"title": "t", "version": "1"}, "paths": paths}


def _ir(nodes):
    return {"format": IR_FORMAT, "product": {}, "nodes": nodes}


def _serve(status, ctype, body):
    """A WSGI app answering every request with one fixed response."""
    def app(environ, start_response):
        start_response(status, [("Content-Type", ctype)])
        return [body]
    return app


def _widget_ir(body_check):
    """GET /widget contracted with a CLOSED schema; the scenario's hand
    body_check only ever names the required id (so the hand path passes)."""
    return _ir({"widget": {
        "openapi": _openapi({"/widget": {"get": _op_closed()}}),
        "scenarios": [{
            "requirement": "widget",
            "when": {"method": "GET", "path": "/widget"},
            "then": {"status": 200, "media": "application/json",
                     "body_check": body_check}}]}})


# --------------------------------------------------------------------------
# S14.7 RED — the hand json_subset PASSES a body the closed schema forbids
# --------------------------------------------------------------------------

def test_extra_field_under_closed_schema_is_a_library_failure():
    # the required id is present with the right type — the hand json_subset
    # {"id": 1} is satisfied — but the body ALSO carries an invented field
    # while the contract says additionalProperties:false. Only a real schema
    # validator catches this; today nothing does.
    app = _serve("200 OK", "application/json",
                 b'{"id": 1, "surprise": 99}')
    res = spec_scenarios.run_scenarios(
        _widget_ir({"json_subset": {"id": 1}}), wsgi_app=app)
    assert res["ok"] is False, (
        "a live body with a field the CLOSED response schema never declared "
        "must red the scenario — the hand json_subset misses it, the library "
        "must catch it")
    assert res["failures"], "the closed-schema violation must be a failure"
    f = res["failures"][0]
    assert f["node"] == "widget"
    assert "schema" in (f["expected"] + f["got"]).lower(), (
        "the failure must name the response-schema contract, not the hand "
        "check: %r" % f)


def test_wrong_type_under_closed_schema_is_a_library_failure():
    # id present but as a string — the hand json_subset {"id": ...} is not
    # even asserted here (contains on name), so the type violation slips by
    # every hand path; the closed schema says id must be an integer.
    app = _serve("200 OK", "application/json",
                 b'{"id": "not-an-int", "name": "w"}')
    res = spec_scenarios.run_scenarios(
        _widget_ir({"contains": "w"}), wsgi_app=app)
    assert res["ok"] is False, (
        "a live body whose declared field has the WRONG type must red the "
        "scenario — caught by the schema library, missed by the hand check")
    assert res["failures"], "the type violation must be a failure"


# --------------------------------------------------------------------------
# S14.7 GREEN direction — honesty: additive, never a false positive
# --------------------------------------------------------------------------

def test_honest_closed_body_still_passes():
    app = _serve("200 OK", "application/json", b'{"id": 7, "name": "w"}')
    res = spec_scenarios.run_scenarios(
        _widget_ir({"json_subset": {"id": 7}}), wsgi_app=app)
    assert res["ok"] is True, (
        "a body that honours the closed schema must stay green: %r"
        % res.get("failures"))
    assert res["passed"] == 1


def test_no_closed_schema_leaves_hand_check_untouched():
    # media-only response (schema {}), an honest gap — the library adds no
    # assertion, so an extra field is judged ONLY by the hand json_subset and
    # stays green exactly as before.
    ir = _ir({"open": {
        "openapi": _openapi({"/open": {"get": {"responses": {"200": {
            "description": "d",
            "content": {"application/json": {"schema": {}}}}}}}}),
        "scenarios": [{
            "requirement": "open",
            "when": {"method": "GET", "path": "/open"},
            "then": {"status": 200, "media": "application/json",
                     "body_check": {"json_subset": {"id": 1}}}}]}})
    app = _serve("200 OK", "application/json", b'{"id": 1, "extra": 2}')
    res = spec_scenarios.run_scenarios(ir, wsgi_app=app)
    assert res["ok"] is True, (
        "with no CLOSED schema the runner must behave exactly as before — the "
        "hand json_subset tolerates extra keys: %r" % res.get("failures"))
    assert res["passed"] == 1
