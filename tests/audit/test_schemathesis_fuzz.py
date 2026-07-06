"""Audit stage S45 (node Q4 fuzz, plan 2026-07-06T20-15): OPT-IN schemathesis
property-fuzz of the ASSEMBLED product. S44 replays the contracted EXAMPLES; S45
lets schemathesis GENERATE inputs (derandomized — reproducible) to find the edge
cases the examples miss: a fuzzed request must never 5xx, and a 2xx body must
still conform to its response schema. Schemathesis is used only for input
generation (its CheckContext API is version-fragile); the verdict uses the same
openapi-schema-validator oracle as S42/S44. Off by default (a fuzz pass costs
time); the deterministic S44 gate stays the default.

  * S45.1 — the engine unions the IR fragments into one OpenAPI 3.1 document
    (schemathesis generates from it); no routes -> no doc.
  * S45.2 — `_assembled_fuzz` is OFF unless SPEC_FLOW_SCHEMATHESIS_FUZZ is set.
  * S45.3 — the fuzz runner is derandomized, uses the ready oracle, and emits
    FUZZ_OK / FUZZ_FAIL / FUZZ_ERROR (the last fail-closed).
  * S45.4 — live: a violating assembled app reds (FUZZ_FAIL), a conforming one
    passes (FUZZ_OK).

Deterministic: derandomized fuzz + known-answer apps; the one subprocess uses a
tiny example budget.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}},
           "required": ["n"], "additionalProperties": False}


def _engine():
    e = sfr.Engine.__new__(sfr.Engine)
    op = {"responses": {"200": {"content": {"application/json":
          {"schema": _SCHEMA}}}}}
    e.__dict__["_decomposer_ir_nodes"] = {
        "notes": {"openapi": {"paths": {"/notes": {"get": op}}}}}
    return e


# ── S45.1: union OpenAPI doc from the IR ─────────────────────────────────────

def test_assembled_openapi_doc_unions_fragments():
    doc = _engine()._assembled_openapi_doc()
    assert doc.get("openapi") == spec_ir.OPENAPI_VERSION
    assert "/notes" in doc["paths"] and "get" in doc["paths"]["/notes"]


def test_no_routes_no_doc():
    e = sfr.Engine.__new__(sfr.Engine)
    e.__dict__["_decomposer_ir_nodes"] = {"lib": {"openapi": {"paths": {}}}}
    assert e._assembled_openapi_doc() == {}


# ── S45.2: off by default ────────────────────────────────────────────────────

def test_fuzz_off_by_default():
    os.environ.pop("SPEC_FLOW_SCHEMATHESIS_FUZZ", None)

    class _WS:
        root = "/tmp"
    ok, detail = _engine()._assembled_fuzz(_WS(), {"callable": ["wsgi_app"],
                                                   "entry": "src/app.py"})
    assert ok and detail == "", "the fuzz must be opt-in (off unless enabled)"


# ── S45.3: the runner is derandomized + oracle-based + marked ────────────────

def test_fuzz_runner_shape():
    src = sfr._SCHEMATHESIS_FUZZ
    assert "derandomize=True" in src, "fuzz must be reproducible (fixed seed)"
    assert "OAS31Validator" in src, "the verdict uses the ready oracle"
    assert "FUZZ_OK" in src and "FUZZ_FAIL" in src and "FUZZ_ERROR" in src, \
        "the runner must emit all three verdict markers"
    assert "status_code >= 500" in src, "a fuzzed input must never 5xx"


# ── S45.4: live — violating reds, conforming passes ─────────────────────────

def _run_fuzz(app_body: str) -> str:
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "src"), exist_ok=True)
    with open(os.path.join(ws, "src", "app.py"), "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(app_body))
    cfg = json.dumps({
        "callable": ["wsgi_app"], "entry_stem": "app",
        "openapi": {"openapi": spec_ir.OPENAPI_VERSION,
                    "info": {"title": "t", "version": "1"},
                    "paths": {"/notes": {"get": {"responses": {"200":
                        {"content": {"application/json":
                         {"schema": _SCHEMA}}}}}}}},
        "response_schemas": [{"route": ["GET", "/notes"], "schema": _SCHEMA}],
        "max_examples": 4})
    proc = subprocess.run([sys.executable, "-c", sfr._SCHEMATHESIS_FUZZ, ws, cfg],
                          capture_output=True, text=True, timeout=120)
    return (proc.stdout or "") + (proc.stderr or "")


def test_live_violating_app_reds():
    out = _run_fuzz('''
        def wsgi_app(environ, start_response):
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b'{"n": "not-int"}']
    ''')
    assert "FUZZ_FAIL" in out, "a schema-violating live response must red: " + out[:200]


def test_live_conforming_app_passes():
    out = _run_fuzz('''
        def wsgi_app(environ, start_response):
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b'{"n": 1}']
    ''')
    assert "FUZZ_OK" in out, "a conforming live response must pass: " + out[:200]
