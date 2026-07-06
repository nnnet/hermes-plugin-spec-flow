"""Audit rule S39 (catalog B, root of the v167 red): the contract graph is
CLOSED EARLY — at decomposition close, not at integrate.

Plan 2026-07-06T20-15__systemic-design-fixes.md, catalog B / node Q3. The v167
run went GREEN through decomposition and blew up on the final tree because the
constitution-pinned ``src/db.py`` had no owner node — connectivity was checked
in three scattered places, none of which closed the graph at decomposition
close. `spec_ir.contract_closure_gaps` is the ONE early check: every contracted
obligation (pinned file, product entry, contracted route, consumed symbol,
human requirement) must have an OWNER NODE in the realized IR, or it is a NAMED
refusal HERE — a milestone sibling of the decomposer_* gates — instead of a
late integrate surprise.

RED direction: a plan that omits an owner for any of the five obligation
classes passes `validate_ir` growth-tolerant checks but MUST red on closure.
GREEN direction (v151 lesson): a fully closed IR yields ZERO gaps — one false
positive sinks a run.

Deterministic: hand-built known-answer IR dicts, no engine, no LLM.
"""
from __future__ import annotations

import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402


def _closed_ir() -> dict:
    """A minimal FULLY-CLOSED IR: db exposes connect and owns the pinned
    src/db.py; core owns the /notes routes (both with a handler symbol),
    consumes db.connect, and claims the 'core' requirement via dependencies.
    Every obligation has an owner — closure must return []."""
    return {
        "format": spec_ir.IR_FORMAT,
        "product": {
            "entry": "src/app.py",
            "callable": ["wsgi_app"],
            "pinned_files": ["src/db.py"],
            "requirements": ["core"],
        },
        "nodes": {
            "db": {
                "children": [],
                "files": ["src/db.py"],
                "symbols": {"exposes": [{"name": "connect", "args": ["path"]}]},
            },
            "core": {
                "children": [],
                "files": ["src/core.py"],
                "dependencies": ["core"],
                "openapi": {
                    "openapi": spec_ir.OPENAPI_VERSION,
                    "info": {"title": "node core interface", "version": "1"},
                    "paths": {
                        "/notes": {
                            "post": {
                                "x-spec-flow-handler": "post_notes",
                                "responses": {"201": {
                                    "description": "ok",
                                    "content": {"application/json": {
                                        "schema": {}}}}},
                            },
                            "get": {
                                "x-spec-flow-handler": "get_notes",
                                "responses": {"200": {
                                    "description": "ok",
                                    "content": {"application/json": {
                                        "schema": {}}}}},
                            },
                        },
                    },
                },
                "symbols": {"consumes": [
                    {"from": "db", "name": "connect", "args": ["path"]}]},
            },
        },
    }


def _gaps(ir, constitution=None) -> list:
    gaps = spec_ir.contract_closure_gaps(ir, constitution)
    assert isinstance(gaps, list), "closure returns a list of gaps"
    for g in gaps:
        assert isinstance(g, str), "gaps are plain strings (P4, JSON-safe)"
    return gaps


# ---- GREEN: a fully closed graph yields ZERO gaps ---------------------------

def test_closed_graph_has_zero_gaps():
    assert _gaps(_closed_ir()) == [], (
        "a fully closed contract graph must yield ZERO gaps — one false "
        "positive sinks a run (v151)")


# ---- RED B3: constitution-pinned file with no owner node --------------------

def test_pinned_file_without_owner_node_reds():
    # The exact v167 shape: constitution pins src/db.py but the realized plan
    # gave it no leaf. Growth-time validate_ir would not catch the MISSING
    # owner; closure at decomposition close must.
    ir = _closed_ir()
    del ir["nodes"]["db"]           # drop the owner of the pinned module
    ir["nodes"]["core"]["symbols"]["consumes"] = []  # keep the rest closed
    gaps = _gaps(ir)
    assert any("src/db.py" in g and "owner" in g for g in gaps), (
        "a constitution-pinned module with no owner node is the v167 class — "
        "it must red EARLY, naming the file: %r" % gaps)


# ---- RED: product entry declared but empty ----------------------------------

def test_empty_product_entry_reds():
    ir = _closed_ir()
    ir["product"]["entry"] = ""
    gaps = _gaps(ir)
    assert any("entry" in g for g in gaps), (
        "a declared-but-empty product entry names an owner that does not "
        "exist — it must red: %r" % gaps)


# ---- RED B2: orphan route — contract without a synthesised handler ----------

def test_route_without_handler_symbol_reds():
    ir = _closed_ir()
    ir["nodes"]["core"]["openapi"]["paths"]["/notes"]["get"][
        "x-spec-flow-handler"] = ""
    gaps = _gaps(ir)
    assert any("/notes" in g and "GET" in g for g in gaps), (
        "a route the contract declares but no handler was synthesised is an "
        "orphan route (catalog B2) — it must red naming the route: %r" % gaps)


# ---- RED B1/v157: consumed symbol exposed by no node ------------------------

def test_phantom_consumed_symbol_reds_at_closure():
    # v157: `from db import store_note` while db exported different names —
    # ImportError at boot. At decomposition close it is an owner-gap.
    ir = _closed_ir()
    ir["nodes"]["core"]["symbols"]["consumes"].append(
        {"from": "db", "name": "store_note", "args": None})
    gaps = _gaps(ir)
    assert any("store_note" in g and "core" in g for g in gaps), (
        "a consumed symbol no node exposes is the v157 ImportError class — "
        "refused at closure, naming node and symbol: %r" % gaps)


def test_mid_growth_filter_is_lifted_at_closure():
    # The 'is exposed by no node' error is INTENTIONALLY filtered mid-growth in
    # _accept_decomposer_ir (pending sibling). Closure is the point where
    # 'pending' is no longer an excuse — the same phantom must red here.
    ir = _closed_ir()
    ir["nodes"]["core"]["symbols"]["consumes"].append(
        {"from": "cache", "name": "get", "args": None})
    gaps = _gaps(ir)
    assert any("cache" in g or "get" in g for g in gaps), (
        "consuming from a module no node owns must red at closure (the "
        "mid-growth filter no longer applies): %r" % gaps)


# ---- RED B4: human requirement claimed by no node ---------------------------

def test_unclaimed_requirement_reds():
    ir = _closed_ir()
    ir["product"]["requirements"] = ["core", "audit-log"]
    gaps = _gaps(ir)
    assert any("audit-log" in g for g in gaps), (
        "a human requirement no node traces to is untraceable (catalog B4) — "
        "it must red naming the requirement: %r" % gaps)


def test_requirement_object_form_is_claimed_by_name():
    ir = _closed_ir()
    ir["product"]["requirements"] = [{"name": "core", "version": 1}]
    assert _gaps(ir) == [], (
        "an object-form requirement whose name is claimed must close clean")


# ---- GREEN edges ------------------------------------------------------------

def test_branch_node_without_obligations_is_legal():
    ir = _closed_ir()
    ir["nodes"]["L0"] = {"children": ["db", "core"]}
    assert _gaps(ir) == [], (
        "a branch node owning nothing is the normal tree shape — it must not "
        "produce a gap")


def test_ir_without_pinned_or_requirements_is_legal():
    ir = _closed_ir()
    ir["product"].pop("pinned_files")
    ir["product"].pop("requirements")
    assert _gaps(ir) == [], (
        "absence of pinned files / requirements is not a gap — closure only "
        "checks obligations that exist")


def test_non_mapping_ir_reds_gracefully():
    assert _gaps(None) or _gaps([]) or True
    assert spec_ir.contract_closure_gaps(None) == [
        "ir is not a mapping — nothing to close over"]
