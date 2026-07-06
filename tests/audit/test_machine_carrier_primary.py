"""Audit stage S41 (node Q1, plan 2026-07-06T20-15 systemic-design-fixes):
single source → consumer. The LLM implementer must receive the node's
MACHINE carrier (OpenAPI / Gherkin behaviour / typed symbols / env / data
schema) as the PRIMARY, authoritative contract — the prose spec is demoted
to secondary human context.

Catalog A of the 2026-07-06 review (A1/A2): the router, the tests and the
conformance oracle already ride the machine IR, but the coder still read the
prose specs/*.md as its leading intent. A machine section buried mid-file with
no "this is authoritative" signal is drift waiting to happen. This stage flips
it: the engine hands the carrier as data (`ictx["carrier"]`), the worker renders
it FIRST with an authoritative marker, and the prose sits BELOW as context.

  * S41.1 — the engine derives the carrier from the node's machine fields
    (openapi/behavior/symbols/env/scenarios) unioned with its decomposer IR
    fragment; a bare node yields an EMPTY carrier (back-compat, prompt
    unchanged).
  * S41.2 — `_machine_carrier_block` renders a non-empty, AUTHORITATIVE block
    carrying the openapi path, the Gherkin behaviour, the exposed symbol and
    the env var; no carrier → empty block.
  * S41.3 — in the assembled coder prompt the machine carrier PRECEDES the
    prose spec body (the machine contract leads; prose is secondary).

Deterministic: pure function + prompt-assembly calls, no LLM, no network.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402
from harness import role_worker as rw  # noqa: E402


# a node carrying every machine field the carrier unions
_NODE = {
    "id": "db_layer",
    "title": "SQLite note store",
    "openapi": {
        "openapi": "3.1.0",
        "paths": {"/notes": {"post": {"responses": {"200": {}}}}},
    },
    "behavior": "Feature: note store\n  Scenario: save a note\n"
                "    When a note is saved\n    Then it can be read back",
    "symbols": {"exposes": ["create_note(text)", "get_notes()"]},
    "env": ["NOTES_DB"],
}
_FRAG = {
    "openapi": {"paths": {"/notes": {"post": {}}}},
    "symbols": {"exposes": ["create_note(text)"]},
    "env": ["NOTES_DB"],
    "schema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


# ---- S41.1: engine derives the carrier as data -----------------------------

def test_engine_derives_carrier_from_machine_fields():
    car = sfr.machine_carrier_of(_NODE, _FRAG)
    assert isinstance(car, dict) and car, "carrier must be a non-empty dict"
    assert car.get("openapi"), "openapi must ride the carrier"
    assert car.get("behavior"), "gherkin behaviour must ride the carrier"
    assert "exposes" in (car.get("symbols") or {}), "typed symbols must ride"
    assert car.get("env"), "env must ride the carrier"


def test_bare_node_yields_empty_carrier():
    bare = {"id": "x", "title": "prose only"}
    assert sfr.machine_carrier_of(bare, None) == {}, \
        "a node with no machine field must yield an EMPTY carrier (back-compat)"


def test_leaf_machine_carrier_method_reads_decomposer_fragment():
    r = sfr.Engine.__new__(sfr.Engine)
    r.__dict__["_decomposer_ir_nodes"] = {"db_layer": _FRAG}
    car = r._leaf_machine_carrier(_NODE, "db_layer")
    assert car.get("schema"), "the IR fragment's data schema must reach the carrier"
    assert car.get("openapi"), "openapi must be present via node or fragment"


# ---- S41.2: worker renders an authoritative block --------------------------

_AUTH_MARK = "MACHINE CONTRACT"


def test_carrier_block_authoritative_and_complete():
    ctx = {"carrier": sfr.machine_carrier_of(_NODE, _FRAG)}
    block = rw._machine_carrier_block(ctx)
    assert block.strip(), "carrier block must be non-empty when a carrier is set"
    assert _AUTH_MARK in block, "block must be flagged as the authoritative contract"
    assert "/notes" in block, "the openapi path must appear in the block"
    assert "create_note" in block, "the exposed symbol must appear in the block"
    assert "NOTES_DB" in block, "the env var must appear in the block"
    assert "note can be read back" in block.lower().replace("  ", " ") \
        or "scenario" in block.lower(), "the gherkin behaviour must appear"


def test_carrier_block_empty_without_carrier():
    assert rw._machine_carrier_block({}) == "", \
        "no carrier → empty block (prose-only leaves keep today's prompt)"


# ---- S41.3: carrier precedes prose in the coder prompt ---------------------

def test_carrier_precedes_prose_in_coder_prompt(tmp_path):
    ws = tmp_path
    (ws / "specs").mkdir()
    prose = "# SQLite note store\n\nStore notes in sqlite. Human prose intent."
    (ws / "specs" / "db_layer.md").write_text(prose, encoding="utf-8")
    ctx = {
        "title": _NODE["title"],
        "spec": "specs/db_layer.md",
        "carrier": sfr.machine_carrier_of(_NODE, _FRAG),
    }
    prompt = rw._coder_chat_prompt(ctx, str(ws), "db_layer", "db_layer")
    assert _AUTH_MARK in prompt, "assembled prompt must carry the machine block"
    assert "Human prose intent" in prompt, "prose body must still be present"
    assert prompt.index(_AUTH_MARK) < prompt.index("Human prose intent"), \
        "the machine carrier must PRECEDE the prose spec body"
