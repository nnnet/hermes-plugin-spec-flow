"""Audit rules S17.1/S17.2: the ENGINE compiles the module skeleton from
the spec IR; the model fills ONLY the function bodies.

Node C1 (plan 2026-07-04T00-45). The v157/v164 lesson generalized: every
surface the model is free to restate is a surface it can drift. Stage 13
made the interface DATA (one IR entry per node); Stage 17 makes the module
SKELETON a compiler output of that data — signatures, allowed imports, env
access points and per-function contract anchors are ENGINE-written, and a
delivery is judged against them with attributable findings.

  * `spec_skeletons.compile_skeleton(ir, node_id)` — deterministic skeleton
    text from the node's IR entry: handler defs from openapi
    (x-spec-flow-handler, the platform (payload, query) ABI), exposed-symbol
    defs from symbols.exposes, the allowed import line-up from
    symbols.consumes, env access points from env, and a single
    `raise NotImplementedError` placeholder under each def with an
    AICODE-NOTE anchor naming the contract.
  * `spec_skeletons.skeleton_conformance(ir, node_id, code)` — AST-based
    findings (node id + offending symbol): a rewritten signature, an import
    outside the allowed list (the v157 phantom-import class dies at the
    door), a public function/route beyond the IR (closed world), a stripped
    skeleton anchor. GREEN direction: a bodies-only delivery with private
    helpers and stdlib imports passes with ZERO findings.

Deterministic: pure functions over a hand IR, no engine, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_skeletons  # noqa: E402

# a hand IR in the exact spec_ir.build_ir shape: one route-owning node
# (core), one pure-library exporter it consumes (db), one branch
_IR = {
    "format": "spec-flow ir v1",
    "product": {"kind": "web-service", "entry": "src/app.py",
                "callable": "wsgi_app"},
    "nodes": {
        "core": {
            "files": ["src/core.py"],
            "openapi": {
                "openapi": "3.1.0",
                "info": {"title": "spec-flow node core interface",
                         "version": "1"},
                "paths": {"/notes": {
                    "post": {
                        "x-spec-flow-handler": "post_notes",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {"text": {}},
                                "required": ["text"],
                                "additionalProperties": False}}}},
                        "responses": {"201": {
                            "description": "contracted success response",
                            "content": {"application/json": {
                                "schema": {}}}}}},
                    "get": {
                        "x-spec-flow-handler": "get_notes",
                        "responses": {"200": {
                            "description": "contracted success response",
                            "content": {"application/json": {
                                "schema": {}}}}}}}}},
            "symbols": {
                "exposes": [
                    {"name": "post_notes", "args": ["payload", "query"]},
                    {"name": "get_notes", "args": ["payload", "query"]}],
                "consumes": [
                    {"from": "db", "name": "init_db", "args": ["path"]},
                    {"from": "db", "name": "store_note",
                     "args": ["path", "text"]}]},
            "env": [{"name": "NOTES_DB",
                     "rule": "path to the sqlite database file"}]},
        "db": {
            "files": ["src/db.py"],
            "symbols": {"exposes": [
                {"name": "init_db", "args": ["path"]},
                {"name": "store_note", "args": ["path", "text"]}]}},
        "branch": {"children": ["core", "db"]},
    }}


def _skel() -> str:
    return spec_skeletons.compile_skeleton(_IR, "core")


def _findings(code: str) -> list:
    return spec_skeletons.skeleton_conformance(_IR, "core", code)


def _bodies_only(skel: str) -> str:
    """The honest delivery: the skeleton VERBATIM, placeholders replaced."""
    return skel.replace("raise NotImplementedError",
                        'return 200, {"ok": True}')


# ── S17.1 the compiler: skeleton is a deterministic function of the IR ──────

def test_skeleton_carries_handler_signatures_from_openapi():
    skel = _skel()
    assert "def post_notes(payload, query):" in skel, (
        "the handler signature comes from x-spec-flow-handler + the "
        "platform (payload, query) ABI — engine-written, never model-guessed")
    assert "def get_notes(payload, query):" in skel
    assert "raise NotImplementedError" in skel, (
        "each def carries a single placeholder body the model REPLACES")


def test_skeleton_anchors_name_the_contract():
    skel = _skel()
    assert "AICODE-NOTE: skeleton-contract post_notes" in skel, (
        "every contracted def carries a greppable anchor naming its contract")
    assert "POST /notes" in skel and "201" in skel, (
        "the handler anchor names method, path and contracted status")
    assert "text" in skel, (
        "the contracted request-body fields are visible at the def")


def test_skeleton_lists_only_consumed_imports():
    skel = _skel()
    assert "from db import init_db, store_note" in skel, (
        "the import line-up is compiled from symbols.consumes — the ONE "
        "allowed non-stdlib surface")
    assert "import requests" not in skel


def test_skeleton_names_env_access_points():
    skel = _skel()
    assert "NOTES_DB" in skel, (
        "declared env vars are visible access points in the skeleton")
    assert "sqlite database" in skel, "the env RULE text travels along"


def test_skeleton_is_deterministic_and_absent_without_interface():
    assert _skel() == _skel(), "same IR -> byte-identical skeleton"
    assert spec_skeletons.compile_skeleton(_IR, "branch") == "", (
        "a branch (children, no interface) compiles to NO skeleton — the "
        "leaf falls back to today's path")
    assert spec_skeletons.compile_skeleton(_IR, "ghost") == "", (
        "a node without an IR entry compiles to NO skeleton")


def test_pure_exporter_gets_symbol_skeleton():
    skel = spec_skeletons.compile_skeleton(_IR, "db")
    assert "def init_db(path):" in skel, (
        "a non-HTTP exporter's skeleton comes from symbols.exposes")
    assert "def store_note(path, text):" in skel


# ── S17.2 conformance: closed world, attributable findings ─────────────────

def test_signature_arg_rewrite_is_a_finding():
    skel = _skel()
    bad = skel.replace("def post_notes(payload, query):",
                       "def post_notes(payload):")
    fnd = _findings(_bodies_only(bad))
    assert fnd, "a delivery that changes a contracted argument list must red"
    assert any("core" in f and "post_notes" in f for f in fnd), (
        "the finding must name the node and the offending symbol: %r" % fnd)


def test_signature_rename_is_a_finding():
    skel = _skel()
    bad = skel.replace("post_notes", "post_note")
    fnd = _findings(_bodies_only(bad))
    assert any("post_notes" in f for f in fnd), (
        "the v143 rename class: the contracted name is gone — must red: %r"
        % fnd)


def test_import_outside_the_allowed_list_is_a_finding():
    code = _bodies_only(_skel()).replace(
        "from db import init_db, store_note",
        "from db import init_db, store_note\nimport requests")
    fnd = _findings(code)
    assert any("requests" in f for f in fnd), (
        "a third-party import must red at the door: %r" % fnd)
    # v157 phantom-import class: a SIBLING module the node never consumed
    code2 = _bodies_only(_skel()).replace(
        "from db import init_db, store_note",
        "from db import init_db, store_note\nfrom web_ui import render")
    fnd2 = _findings(code2)
    assert any("web_ui" in f for f in fnd2), (
        "importing a module outside symbols.consumes is the v157 class — "
        "it dies AT THE DOOR now: %r" % fnd2)


def test_uncontracted_public_function_is_a_finding():
    code = _bodies_only(_skel()) + (
        '\n\ndef delete_notes(payload, query):\n'
        '    return 200, {"deleted": True}\n')
    fnd = _findings(code)
    assert any("delete_notes" in f for f in fnd), (
        "a public function/route the IR never contracted must red "
        "(closed world): %r" % fnd)


def test_stripped_anchor_is_a_finding():
    code = "\n".join(ln for ln in _bodies_only(_skel()).splitlines()
                     if "AICODE-NOTE" not in ln)
    fnd = _findings(code)
    assert any("anchor" in f.lower() for f in fnd), (
        "the skeleton anchors are part of the contract — stripping them "
        "must red: %r" % fnd)


def test_fresh_conforming_module_without_engine_text_is_green():
    # BOTH-DIRECTIONS convention (v151 lesson): the anchor rule is
    # engine-text INTEGRITY, not a comment tax — a module written fresh
    # with the exact contracted surface and no engine text must land on
    # the data checks alone (the deterministic conforming implementer in
    # test_module_symbol_contract writes exactly such modules)
    code = ("from db import init_db, store_note\n\n\n"
            "def post_notes(payload, query):\n"
            '    return 201, {"id": 1}\n\n\n'
            "def get_notes(payload, query):\n"
            '    return 200, {"items": []}\n')
    assert _findings(code) == []


def test_bodies_only_delivery_is_green():
    code = _bodies_only(_skel())
    assert _findings(code) == [], (
        "the honest delivery — skeleton verbatim, bodies filled — passes "
        "untouched")


def test_private_helpers_and_stdlib_imports_are_green():
    code = _bodies_only(_skel()) + (
        "\n\nimport json\nimport os\n\n\ndef _db():\n"
        "    return os.environ.get('NOTES_DB', '')\n")
    assert _findings(code) == [], (
        "bodies NEED private helpers and the stdlib — the closed world "
        "covers the PUBLIC surface, not the implementation")


def test_syntax_error_is_not_the_doors_business():
    assert _findings("def (broken\n") == [], (
        "a syntactically broken file is judged by the suite, not the door "
        "(the S10.12 convention)")


def test_node_without_ir_entry_is_inert():
    assert spec_skeletons.skeleton_conformance(_IR, "ghost", "import requests\n") == [], (
        "no IR entry -> no skeleton -> the gate stays inert (fallback path)")
