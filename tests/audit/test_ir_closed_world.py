"""Audit rules S13.1/S13.2/S13.5 (Phase A, spec-IR): the IR is a CLOSED WORLD.

Plan 2026-07-04T00-45 (spec-IR compiler rearchitecture): every drift class in
S10.9-S12.16 is one shape — two artifacts disagreeing on a value that never
existed as data. Phase A merges the already-recorded engine datums into ONE
machine-checkable structure per product build (`spec_ir.build_ir`) and a
validator (`spec_ir.validate_ir`) that refuses anything the interface does
not declare:

  * unknown keys anywhere = error;
  * a scenario touching a route absent from every node's openapi = error;
  * a symbol consumed but exposed by no node = error (the v157 phantom
    `from db import store_note` class);
  * an env var used in a scenario but declared by no node = error;
  * two nodes owning the same (method, path) = error;
  * a route owned by a node with children (non-leaf) = error.

Errors are PLAIN STRINGS naming the node id and the offending value (P4,
attributable failure). Both directions (v151 lesson): a fully consistent IR
validates with ZERO errors — one false positive sinks a run.

Deterministic: hand-built known-answer IR dicts, no engine, no LLM.
"""
from __future__ import annotations

import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402


def _valid_ir() -> dict:
    """A minimal fully-consistent IR: two leaves, one exporter (db), one
    route owner (core) that consumes db.connect — the p6-notes shape."""
    return {
        "format": spec_ir.IR_FORMAT,
        "product": {"entry": "src/app.py", "callable": ["wsgi_app"]},
        "nodes": {
            "db": {
                "children": [],
                "files": ["src/db.py"],
                "symbols": {"exposes": [{"name": "connect", "args": ["path"]}]},
                "env": [{"name": "NOTES_DB",
                         "rule": "db.connect reads the NOTES_DB env var"}],
            },
            "core": {
                "children": [],
                "files": ["src/core.py"],
                "openapi": {
                    "openapi": spec_ir.OPENAPI_VERSION,
                    "info": {"title": "node core interface", "version": "1"},
                    "paths": {
                        "/notes": {
                            "post": {
                                "x-spec-flow-handler": "post_notes",
                                "requestBody": {
                                    "required": True,
                                    "content": {"application/json": {"schema": {
                                        "type": "object",
                                        "properties": {"text": {}},
                                        "required": ["text"],
                                        "additionalProperties": False}}},
                                },
                                "responses": {"201": {
                                    "description": "contracted success",
                                    "content": {"application/json": {"schema": {}}}}},
                            },
                            "get": {
                                "x-spec-flow-handler": "get_notes",
                                "responses": {"200": {
                                    "description": "contracted success",
                                    "content": {"application/json": {"schema": {}}}}},
                            },
                        },
                    },
                },
                "symbols": {"consumes": [
                    {"from": "db", "name": "connect", "args": ["path"]}]},
                "env": [{"name": "NOTES_DB",
                         "rule": "db.connect reads the NOTES_DB env var"}],
                "scenarios": [
                    {"requirement": "core",
                     "when": {"method": "POST", "path": "/notes",
                              "body": {"text": "hi"}},
                     "then": {"status": 201, "media": "application/json"}},
                    {"requirement": "core",
                     "given": {"env": {"NOTES_DB": "notes.db"},
                               "state": [{"method": "POST", "path": "/notes",
                                          "body": {"text": "hi"}}]},
                     "when": {"method": "GET", "path": "/notes"},
                     "then": {"status": 200, "media": "application/json"}},
                ],
            },
        },
    }


def _errors(ir) -> list:
    rep = spec_ir.validate_ir(ir)
    assert set(rep) == {"errors", "incomplete"}, (
        "validate_ir returns exactly {errors, incomplete} — closed-world "
        "violations vs honest incompleteness findings, never mixed")
    for s in rep["errors"] + rep["incomplete"]:
        assert isinstance(s, str), "findings are plain strings (P4)"
    return rep["errors"]


# ---- GREEN: a consistent IR validates clean ---------------------------------

def test_consistent_ir_has_zero_errors():
    assert _errors(_valid_ir()) == [], (
        "a fully consistent IR must validate with ZERO errors — one false "
        "positive sinks a run (v151)")


# ---- RED: unknown keys anywhere ----------------------------------------------

def test_unknown_top_level_key_is_error():
    ir = _valid_ir()
    ir["surprise"] = 1
    assert any("surprise" in e for e in _errors(ir)), (
        "closed world: an unknown top-level key must be a named error")


def test_unknown_node_key_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["colour"] = "red"
    errs = _errors(ir)
    assert any("colour" in e and "core" in e for e in errs), (
        "an unknown node-entry key must be an error naming the node id "
        "and the offending key: %r" % errs)


def test_unknown_scenario_key_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["scenarios"][0]["comment"] = "free prose"
    assert any("comment" in e for e in _errors(ir)), (
        "the scenario schema is CLOSED — no free grammar, no extra keys")


def test_unknown_when_key_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["scenarios"][0]["when"]["headers"] = {}
    assert any("headers" in e for e in _errors(ir)), (
        "when carries exactly {method, path, body} — nothing else")


def test_wrong_format_marker_is_error():
    ir = _valid_ir()
    ir["format"] = "somebody else's ir"
    assert _errors(ir), "an unrecognized format marker must red"


# ---- RED: v157 — symbol consumed, exposed by no node -------------------------

def test_v157_phantom_consumed_symbol_is_ir_error():
    # v157: src/core.py line 3 `from db import init_db, store_note,
    # list_notes` while src/db.py exported different names — ImportError at
    # boot, doctor churned. In the IR the phantom is a VALIDATION error.
    ir = _valid_ir()
    ir["nodes"]["core"]["symbols"]["consumes"].append(
        {"from": "db", "name": "store_note", "args": None})
    errs = _errors(ir)
    assert any("store_note" in e and "core" in e for e in errs), (
        "a consumed symbol no node exposes is the v157 ImportError class — "
        "it must be an IR error naming node and symbol: %r" % errs)


def test_consumed_symbol_without_exporter_module_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["symbols"]["consumes"].append(
        {"from": "cache", "name": "get", "args": None})
    assert any("cache" in e for e in _errors(ir)), (
        "consuming from a module no node owns must red")


# ---- RED: scenario touching an undeclared route -------------------------------

def test_scenario_on_undeclared_route_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["scenarios"].append(
        {"requirement": "core",
         "when": {"method": "GET", "path": "/about"},
         "then": {"status": 200}})
    errs = _errors(ir)
    assert any("/about" in e for e in errs), (
        "a scenario touching a route absent from every node's openapi "
        "violates the closed world: %r" % errs)


def test_state_step_on_undeclared_route_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["scenarios"][1]["given"]["state"] = [
        {"method": "POST", "path": "/ghost"}]
    assert any("/ghost" in e for e in _errors(ir)), (
        "given.state steps are when-steps — the same closed-route rule")


# ---- RED: env var used but declared by no node --------------------------------

def test_scenario_env_var_declared_by_no_node_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["scenarios"][1]["given"]["env"]["GHOST_VAR"] = "x"
    errs = _errors(ir)
    assert any("GHOST_VAR" in e for e in errs), (
        "an env var used in a scenario but declared by no node is the "
        "config-surface twin of the phantom symbol: %r" % errs)


# ---- RED: ownership integrity --------------------------------------------------

def test_duplicate_route_ownership_is_error():
    ir = _valid_ir()
    ir["nodes"]["db"]["openapi"] = copy.deepcopy(
        ir["nodes"]["core"]["openapi"])
    errs = _errors(ir)
    assert any("/notes" in e and "core" in e and "db" in e for e in errs), (
        "two nodes owning the same (method, path) must red naming BOTH "
        "node ids: %r" % errs)


def test_route_owned_by_non_leaf_is_error():
    ir = _valid_ir()
    ir["nodes"]["core"]["children"] = ["core.a"]
    ir["nodes"]["core.a"] = {"children": []}
    errs = _errors(ir)
    assert any("core" in e for e in errs), (
        "ownership means 'this LEAF builds the route' (S10.16) — a node "
        "with children may not own routes: %r" % errs)


# ---- GREEN edges ----------------------------------------------------------------

def test_branch_without_routes_is_legal():
    ir = _valid_ir()
    ir["nodes"]["L0"] = {"children": ["db", "core"]}
    assert _errors(ir) == [], (
        "a branch node that owns nothing is the normal tree shape — "
        "it must not red")


def test_openapi_x_extensions_are_legal():
    # OpenAPI 3.1 specification extensions (x-*) are part of the REAL
    # standard — the closed world admits them by the standard's own rule.
    ir = _valid_ir()
    op = ir["nodes"]["core"]["openapi"]["paths"]["/notes"]["get"]
    op["x-spec-flow-owner"] = "core"
    assert _errors(ir) == []
