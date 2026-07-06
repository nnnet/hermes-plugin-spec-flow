"""STAGE 13 extension (S13.8): a THIRD-PARTY structural oracle validates the
IR alongside the hand-rolled validate_ir (node H7, plan 2026-07-04T00-45;
principles-audit finding — the external-oracle pattern (Specmatic/
Schemathesis) applied to the IR itself).

Why: `validate_ir` is hand-written. The project already runs external oracles
(Specmatic, Schemathesis, pytest) against its outputs — two oracles that
disagree is itself a signal. Applying `jsonschema` to the IR structure gives
a second, independent witness of the STRUCTURE (validate_ir keeps the
closed-world SEMANTICS — route ownership, phantom consumes — that a schema
cannot express, so it stays on top).

What is pinned here:
  * S13.8 `spec_ir.jsonschema_errors(ir)` runs a JSON Schema (draft 2020-12)
    over the IR and returns structural errors;
  * the two oracles AGREE on a corpus of valid IR (both silent) and on
    structural garbage (both red) — a divergence is a failing test, which is
    exactly the point of a second oracle;
  * the schema is CLOSED at the levels validate_ir closes (unknown top /
    product / node key is a jsonschema error too).

Deterministic; jsonschema is a dev/test oracle (4.x), not a runtime dep.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402


def _valid_ir():
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"],
                        "requirements": [{"name": "flask", "version": "3.0"}]},
            "nodes": {"api": {
                "files": ["src/api.py"],
                "symbols": {"exposes": [{"name": "h", "args": ["p", "q"]}],
                            "consumes": []},
                "env": [{"name": "DB", "rule": "path"}],
                "dependencies": ["flask"],
                "effects": ["fs-write"]}}}


# ── S13.8 the oracle exists and passes a valid IR ───────────────────────────

def test_jsonschema_oracle_passes_valid_ir():
    assert spec_ir.jsonschema_errors(_valid_ir()) == [], (
        "the jsonschema oracle must accept a structurally valid IR")


# ── S13.8 both oracles agree on valid IR (silence) ──────────────────────────

def test_both_oracles_agree_on_valid_ir():
    ir = _valid_ir()
    assert spec_ir.validate_ir(ir)["errors"] == []
    assert spec_ir.jsonschema_errors(ir) == [], (
        "a divergence between the two oracles on valid IR is the signal this "
        "node exists to catch")


# ── S13.8 both oracles catch structural garbage ─────────────────────────────

def test_unknown_node_key_is_caught_by_jsonschema():
    ir = _valid_ir()
    ir["nodes"]["api"]["bogus"] = 1
    assert spec_ir.jsonschema_errors(ir), (
        "an unknown node key must red the jsonschema oracle (closed level), "
        "the way validate_ir reds it")
    assert spec_ir.validate_ir(ir)["errors"], "validate_ir must red it too"


def test_wrong_type_format_is_caught():
    ir = _valid_ir()
    ir["format"] = 123
    assert spec_ir.jsonschema_errors(ir), (
        "format must be the exact IR_FORMAT string — a number reds the oracle")


def test_unknown_product_key_is_caught():
    ir = _valid_ir()
    ir["product"]["extra"] = "x"
    assert spec_ir.jsonschema_errors(ir), (
        "an unknown product key reds the jsonschema oracle (closed level)")
