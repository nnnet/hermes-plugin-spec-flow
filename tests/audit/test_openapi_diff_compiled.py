#!/usr/bin/env python3
"""Audit STAGE 24 (S24.1-S24.3): the narrow hand-written OpenAPI interface
diff (`tests/harness/openapi_diff.py`) is built on the MACHINE OpenAPI document
produced by `spec_openapi.compile_openapi`, NOT on a private hand walk of
``paths -> method -> responses.200.content...schema.properties``.

Plan 2026-07-04T00-45, node K4 (`merge-openapi-diff`): the diff validator used
to re-derive the interface from the raw contract fragment with its own dict
walk — a second implementation of the exact extraction `compile_openapi`
already performs when it merges node fragments into one document. That
duplication was blind to everything the compiler names: honest response gaps
(``x-spec-flow-gaps``), verbatim path/field extraction, router-truthful status
injection, per-operation ``x-spec-flow-node`` ownership.

The concrete lie the old hand walk told (RED evidence): a contract whose
success response records NO media (an honest gap the compiler NAMES) or an
empty ``{}`` schema (a body-shape gap the compiler NAMES) was silently read as
"endpoint present, zero fields" by the private walk, so ANY implementation
stayed green over a hollow contract. compile_openapi refuses to invent there —
it stamps ``x-spec-flow-gaps`` — and the diff built on that document must
surface the gap as drift instead of a false pass.

Deterministic: crafted OpenAPI fragments + code manifests over tmp files run
through the real `openapi_diff.py` subprocess. No LLM, no network.

Ratchet: committed RED against the pre-K4 hand-walk diff (media-gap and
body-shape-gap contracts both returned ``[]``/exit 0 — a hollow contract
passed). Turns GREEN only once the diff derives its interface from the
compiled machine document and reports the named gaps.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
import spec_openapi  # noqa: E402

_OPENAPI_DIFF = _ROOT / "tests" / "harness" / "openapi_diff.py"


def _yaml_load(text: str) -> dict:
    import yaml
    return yaml.safe_load(text) or {}


def _run_diff(tmp_path, contract_yaml: str, code_obj: dict) -> tuple[int, list]:
    """Invoke the real openapi_diff validator over tmp files (its CLI seam).

    Why: contract_check wires the validator as ``openapi_diff.py <contract>
    <code>``; the audit must hit that exact seam, not import internals.
    What: writes the two files, runs the subprocess, parses stdout records.
    Test: the callers below assert on (exit_code, records).
    """
    contract = tmp_path / "contract.yaml"
    code = tmp_path / "code.json"
    contract.write_text(contract_yaml, encoding="utf-8")
    code.write_text(json.dumps(code_obj), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_OPENAPI_DIFF), str(contract), str(code)],
        capture_output=True, text=True,
    )
    records = json.loads(proc.stdout or "[]")
    return proc.returncode, records


# --- S24.1: interface comes from the compiled document, gaps included --------

_MEDIA_GAP_CONTRACT = """\
openapi: 3.0.0
info: {title: gap demo, version: 1.0.0}
paths:
  /a:
    get:
      responses:
        '200':
          description: ok
"""


def test_media_gap_contract_is_named_not_silently_passed(tmp_path):
    """S24.1: a success response with NO media is an honest compiler gap;
    the diff built on the machine document surfaces it, never a false green.

    Why: the pre-K4 hand walk read this as ``GET /a: {}`` and returned
    ``[]``/exit 0 for a hollow contract — a lie any implementation passed.
    What: the compiled document names it in ``x-spec-flow-gaps``; the diff
    inherits that and emits a ``contract_gap`` drift record.
    Test: assert a ``contract_gap`` record naming ``GET /a`` and nonzero exit.
    """
    # Precondition: the compiler itself names this gap — the single source of
    # truth the diff must inherit rather than re-derive.
    doc = spec_openapi.compile_openapi(
        {"nodes": {"c": {"openapi": _yaml_load(_MEDIA_GAP_CONTRACT)}}}
    )
    gaps = doc.get("x-spec-flow-gaps") or []
    assert any("GET /a" in g and "200" in g for g in gaps), (
        "precondition: compile_openapi must name the media gap"
    )

    rc, records = _run_diff(tmp_path, _MEDIA_GAP_CONTRACT, {"endpoints": {"GET /a": {}}})
    kinds = {r.get("kind") for r in records}
    assert "contract_gap" in kinds, (
        "diff must surface the compiler's honest gap as drift, not pass a "
        f"hollow contract green; got records={records}"
    )
    gap_records = [r for r in records if r.get("kind") == "contract_gap"]
    assert any("GET /a" in (r.get("endpoint") or "") for r in gap_records)
    assert rc != 0, "a named gap is drift; exit must be nonzero"


_BODY_SHAPE_GAP_CONTRACT = """\
openapi: 3.0.0
info: {title: shape gap, version: 1.0.0}
paths:
  /a:
    get:
      responses:
        '200':
          content:
            application/json:
              schema: {}
"""


def test_empty_schema_contract_is_named_not_silently_passed(tmp_path):
    """S24.2: an empty ``{}`` success schema is a body-shape gap the compiler
    names; the diff must not read it as a closed "any object" contract.

    Why: hand walk produced zero fields → any code passed a shapeless body.
    What: compiler names the body-shape gap; diff reports it.
    Test: assert a ``contract_gap`` record for ``GET /a`` and nonzero exit.
    """
    doc = spec_openapi.compile_openapi(
        {"nodes": {"c": {"openapi": _yaml_load(_BODY_SHAPE_GAP_CONTRACT)}}}
    )
    gaps = doc.get("x-spec-flow-gaps") or []
    assert any("GET /a" in g and "200" in g for g in gaps), (
        "precondition: compile_openapi must name the body-shape gap"
    )

    rc, records = _run_diff(
        tmp_path, _BODY_SHAPE_GAP_CONTRACT, {"endpoints": {"GET /a": {}}}
    )
    kinds = {r.get("kind") for r in records}
    assert "contract_gap" in kinds, (
        f"empty-schema contract must be named, not passed; got {records}"
    )
    assert rc != 0


# --- S24.3: honest recorded fields still diff exactly as before ---------------

_TYPED_CONTRACT = """\
openapi: 3.0.0
info: {title: typed, version: 1.0.0}
paths:
  /shorten:
    post:
      responses:
        '200':
          content:
            application/json:
              schema:
                properties:
                  id: {type: integer}
                  short_url: {type: string}
"""


def test_recorded_fields_still_diff_exactly(tmp_path):
    """S24.3: for a fully-recorded contract the compiled-document diff keeps the
    exact field-level drift semantics — the rebuild fixes the blind spot
    without weakening real checks.

    Why: rebuilding on compile_openapi must not regress missing-endpoint /
    missing-field / type-mismatch detection.
    What: run four code manifests against one typed contract.
    Test: exact match → clean; wrong type → type_mismatch; absent field →
    missing_field; absent endpoint → missing_endpoint.
    """
    rc, records = _run_diff(
        tmp_path, _TYPED_CONTRACT,
        {"endpoints": {"POST /shorten": {"id": "integer", "short_url": "string"}}},
    )
    assert rc == 0 and records == [], f"exact match must be clean; got {records}"

    rc, records = _run_diff(
        tmp_path, _TYPED_CONTRACT,
        {"endpoints": {"POST /shorten": {"id": "string", "short_url": "string"}}},
    )
    assert rc != 0
    assert any(r.get("kind") == "type_mismatch" and r.get("field") == "id"
               for r in records), records

    rc, records = _run_diff(
        tmp_path, _TYPED_CONTRACT,
        {"endpoints": {"POST /shorten": {"id": "integer"}}},
    )
    assert any(r.get("kind") == "missing_field" and r.get("field") == "short_url"
               for r in records), records

    rc, records = _run_diff(tmp_path, _TYPED_CONTRACT, {"endpoints": {}})
    assert any(r.get("kind") == "missing_endpoint" for r in records), records
