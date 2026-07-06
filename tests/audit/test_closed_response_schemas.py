"""STAGE 21 (S21.1-S21.4): the response body shape is CLOSED — a declared
response schema is pinned by the compiled test and an undeclared one is a
VISIBLE gap, never a silent permissive ``{}`` (node H3, plan 2026-07-04T00-45;
principles-audit finding F1).

Why: ``compile_openapi`` emitted ``{"schema": {}}`` for a media-only success
response and ``compile_leaf_tests`` asserted only ``isinstance(body, dict)`` —
so a weak model could return ``{"invented": "fields"}`` and the compiled test
stayed GREEN (wrong-but-green software). The empty schema READ like a closed
"any object" contract while it was really a GAP the engine never recorded.

What is pinned here:
  * S21.1 a success response whose IR carries a CLOSED object schema
    (properties/required/additionalProperties:false) compiles to asserts that
    the required fields are PRESENT, the declared field types hold, and — when
    additionalProperties is false — no extra field is returned;
  * S21.2 the exploit is dead: a handler returning the contracted fields PLUS
    an invented one fails the compiled test (behavioural, run in-process);
  * S21.3 a media-only response with NO recorded body shape stays an honest
    ``isinstance`` check AND is NAMED as a gap in the compiled document
    (``x-spec-flow-gaps``) — nothing is invented for it, but the silence is
    made visible (S13.1 discipline carried to the response body);
  * S21.4 a ``const`` fixed body keeps its exact ``== body`` assert (the S18
    contract is not weakened by the new shape path).

Deterministic: hand-built known-answer IR dicts; no LLM.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))

import spec_conformance  # noqa: E402
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402


# ── known-answer IR fixtures ────────────────────────────────────────────────

def _doc(nid, paths):
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": paths}


def _resp(status="200", media="application/json", schema=None):
    resp: dict = {"description": "contracted"}
    if media:
        resp["content"] = {media: {"schema": schema if schema is not None
                                   else {}}}
    return {"responses": {str(status): resp}}


_SHAPE = {"type": "object",
          "properties": {"id": {"type": "integer"},
                         "text": {"type": "string"}},
          "required": ["id", "text"],
          "additionalProperties": False}


def _shaped_node():
    """A leaf whose GET /item 200 carries a CLOSED object response shape."""
    op = _resp("200", schema=_SHAPE)
    op["responses"]["200"]["content"]["application/json"][
        "x-spec-flow-handler"] = "get_item"
    op["x-spec-flow-handler"] = "get_item"
    return {"files": ["src/item.py"],
            "openapi": _doc("item", {"/item": {"get": op}})}


def _shapeless_node():
    """A leaf whose GET /list 200 records media but NO body shape (gap)."""
    op = _resp("200")
    op["x-spec-flow-handler"] = "get_list"
    return {"files": ["src/listing.py"],
            "openapi": _doc("listing", {"/list": {"get": op}})}


def _ir(nodes):
    return {"format": spec_ir.IR_FORMAT, "product": {}, "nodes": nodes}


def _run_compiled(out: str, stem: str, handlers: dict, test_name: str):
    """Exec a compiled file with a fake source module supplying `handlers`,
    then call `test_name`. Returns None on pass, raises the test's error."""
    import types
    mod = types.ModuleType(stem)
    for k, v in handlers.items():
        setattr(mod, k, v)
    sys.modules[stem] = mod
    try:
        ns: dict = {}
        exec(compile(out, "<compiled:%s>" % stem, "exec"), ns)
        ns[test_name]()
    finally:
        sys.modules.pop(stem, None)


# ── S21.1 a closed response schema is pinned ────────────────────────────────

def test_declared_response_shape_is_pinned():
    out = spec_conformance.compile_leaf_tests(
        _ir({"item": _shaped_node()}), "item")
    ast.parse(out)
    assert '"id" in body' in out and '"text" in body' in out, (
        "the required response fields must be asserted PRESENT — an empty "
        "schema that pinned nothing was the F1 hole")
    assert "isinstance(body[\"id\"], int)" in out or \
           "isinstance(body['id'], int)" in out, (
        "a declared field type must be pinned, not left to isinstance(dict)")
    assert "set(body)" in out, (
        "additionalProperties:false must forbid invented response fields")


# ── S21.2 the exploit is dead (behavioural) ─────────────────────────────────

def test_invented_response_field_fails_the_compiled_test():
    out = spec_conformance.compile_leaf_tests(
        _ir({"item": _shaped_node()}), "item")
    # honest handler passes
    _run_compiled(out, "item",
                  {"get_item": lambda payload, query: {"id": 1, "text": "a"}},
                  "test_get_item_status_200")
    # a handler that invents an extra field must FAIL the pinned test
    with pytest.raises(AssertionError):
        _run_compiled(
            out, "item",
            {"get_item": lambda payload, query: {"id": 1, "text": "a",
                                                 "evil": "x"}},
            "test_get_item_status_200")


# ── S21.3 a shapeless response is an honest named gap ───────────────────────

def test_shapeless_response_is_a_named_gap_not_a_silent_any():
    doc = spec_openapi.compile_openapi(_ir({"listing": _shapeless_node()}))
    gaps = doc.get("x-spec-flow-gaps") or []
    assert any("/list" in g and "body shape" in g for g in gaps), (
        "a media-only response with no recorded body shape must be NAMED as "
        "a gap, not compiled to a silent permissive empty schema")
    out = spec_conformance.compile_leaf_tests(
        _ir({"listing": _shapeless_node()}), "listing")
    assert "isinstance(body" in out, (
        "with no recorded shape the test stays an honest isinstance — the "
        "engine invents no fields")


# ── S21.4 a const fixed body keeps its exact assert ─────────────────────────

def test_const_body_still_pinned_exactly():
    op = _resp("200", schema={"const": {"status": "ok"}})
    op["x-spec-flow-handler"] = "get_health"
    node = {"files": ["src/health.py"],
            "openapi": _doc("health", {"/health": {"get": op}})}
    out = spec_conformance.compile_leaf_tests(_ir({"health": node}), "health")
    assert "== {\"status\": \"ok\"}" in out or \
           "== {'status': 'ok'}" in out, (
        "the S18 const contract must survive the new shape path unweakened")
