"""Audit stage S44 (node Q4 deferred, plan 2026-07-06T20-15): conformance of
the ASSEMBLED product against its OpenAPI contract at integrate — the twin of
the leaf-level oracle S42. S42 validates a leaf handler in isolation; a router
wiring the wrong handler, or the entry synthesis reshaping a body, only shows on
the LIVE assembled app. S44 drives each schema-bearing route against the booted
WSGI product and conforms the live response with the ready openapi-schema-
validator oracle (engine-side — the isolated python3 probe cannot import it, so
the probe DUMPS bodies and the engine judges).

  * S44.1 — the engine collects (METHOD, path) -> success response schema from
    the accepted IR fragments (real shapes only).
  * S44.2 — it builds deterministic probe rows (a payload for a bodied method,
    null for GET) — replayed contracted examples, never fuzzed.
  * S44.3 — a live body that conforms passes; one that violates the schema reds
    with a root message naming the route AND its owner leaf.
  * S44.4 — the probe emits CONFORM_DUMP and the boot-gate cfg carries
    response_probes (the wiring is present, not dead code).

Deterministic: pure engine methods + template assertions, no subprocess, no net.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402


def _op():
    return {"responses": {"200": {"description": "ok", "content":
            {"application/json": {"schema": {
                "type": "object",
                "properties": {"kind": {"enum": ["a", "b"]}},
                "required": ["kind"],
                "additionalProperties": False}}}}}}


def _engine():
    e = sfr.Engine.__new__(sfr.Engine)
    e.__dict__["_decomposer_ir_nodes"] = {
        "kinds": {"openapi": {"paths": {"/k": {"get": _op()}}}}}
    e.__dict__["_route_owners"] = {("GET", "/k"): {"kinds"}}
    return e


# ── S44.1 / S44.2: schemas + probe rows from the IR ─────────────────────────

def test_assembled_response_schemas_from_ir():
    schemas = _engine()._assembled_response_schemas()
    assert ("GET", "/k") in schemas, "the contracted route's schema is collected"
    assert schemas[("GET", "/k")].get("required") == ["kind"]


def test_probe_rows_are_deterministic_replays():
    e = _engine()
    rows = e._response_probe_rows(e._assembled_response_schemas())
    assert rows == [["GET", "/k", None]], \
        "GET carries no body payload; the row replays the contracted route"


# ── S44.3: conform the live dump ────────────────────────────────────────────

def test_conforming_body_passes():
    e = _engine()
    schemas = e._assembled_response_schemas()
    dump = [{"route": ["GET", "/k"], "status": 200, "body": {"kind": "a"}}]
    assert e._conform_assembled_responses(dump, schemas) == "", \
        "a body matching the schema must pass"


def test_violating_body_reds_with_route_and_owner():
    e = _engine()
    schemas = e._assembled_response_schemas()
    dump = [{"route": ["GET", "/k"], "status": 200, "body": {"kind": "zzz"}}]
    msg = e._conform_assembled_responses(dump, schemas)
    assert msg, "an out-of-enum live response must red"
    assert "/k" in msg and "kinds" in msg, \
        "the root message names the route and its owner leaf"


def test_non_2xx_and_shapeless_are_left_to_other_gates():
    e = _engine()
    schemas = e._assembled_response_schemas()
    # a 500 is owned by the boot sections, not the schema oracle
    dump = [{"route": ["GET", "/k"], "status": 500, "body": {"kind": "zzz"}}]
    assert e._conform_assembled_responses(dump, schemas) == ""


# ── S44.4: the wiring is live ───────────────────────────────────────────────

def test_probe_template_emits_conform_dump():
    src = sfr._ROOT_BOOT_PROBE
    assert "CONFORM_DUMP" in src, "the probe must dump live bodies"
    assert "response_probes" in src, "the probe must read the response_probes cfg"


def test_boot_gate_wires_response_probes():
    src = pathlib.Path(sfr.__file__).read_text(encoding="utf-8")
    assert '"response_probes": _resp_probes' in src, \
        "the boot-gate cfg must carry the response probes"
    assert "_conform_assembled_responses(dump" in src, \
        "the boot-gate must conform the dumped responses"
