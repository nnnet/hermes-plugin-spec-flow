#!/usr/bin/env python3
"""A real (tiny) OpenAPI-vs-implementation drift validator.

Used as a stand-in for redocly/specmatic so contract_check tests exercise an
actual external validator over actual files instead of a true/false stub.

Interface source of truth (node K4 / S23): the endpoint/field interface this
validator diffs against the implementation is NOT re-derived here by a private
walk of ``paths -> method -> responses`` — that duplicated the exact extraction
`spec_openapi.compile_openapi` already performs when it merges node fragments
into one machine document, and was blind to everything the compiler names
(honest ``x-spec-flow-gaps``, router-truthful status injection, per-operation
``x-spec-flow-node`` ownership, duplicate-route refusal). Instead the raw
contract fragment is wrapped in a minimal one-node IR, compiled through
`compile_openapi`, and the interface is read off the resulting document. A
compiler-named gap (media-less or empty-schema success response) becomes a
``contract_gap`` drift record, so a hollow contract can never pass green.

Usage:
    openapi_diff.py <contract.yaml> <code.json>

Contract (any OpenAPI 3.x fragment): paths -> method -> responses.2xx.content.
application/json.schema.properties{name: {type}}. Field-level drift is compared
against successful (2xx) JSON response schemas the compiled document carries.

Code (implementation manifest, JSON):
    {"endpoints": {"POST /shorten": {"short_url": "string", "id": "integer"}}}

Exits 0 when the implementation matches the contract, 1 on any drift, printing
a JSON list of drift records to stdout.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Import the machine OpenAPI compiler from the repo root (REPO_ROOT pattern):
# this harness lives at tests/harness/, the compiler at the project root.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import yaml
except Exception:  # pragma: no cover - yaml is expected in the test env
    print(json.dumps([{"kind": "validator_error", "detail": "PyYAML missing"}]))
    sys.exit(2)

try:
    import spec_openapi
except Exception as exc:  # pragma: no cover - compiler is expected on path
    print(json.dumps([{"kind": "validator_error",
                       "detail": f"spec_openapi import failed: {exc}"}]))
    sys.exit(2)


_2XX = range(200, 300)


def _compile(fragment: dict) -> dict:
    """Wrap the raw contract fragment in a minimal one-node IR and compile it.

    Why: the interface must come from the same machine document the rest of the
    engine consumes, not a private dict walk — one source, one truth.
    What: builds ``{"nodes": {"contract": {"openapi": fragment}}}`` and returns
    the compiled OpenAPI 3.1 document (paths + ``x-spec-flow-gaps``).
    Test: S23.1/S23.2 assert gaps surface; S23.3 asserts fields still diff.
    """
    ir = {"nodes": {"contract": {"openapi": fragment}}}
    return spec_openapi.compile_openapi(ir)


def _success_fields(op: dict) -> dict[str, str | None]:
    """Extract {field: type} from an operation's 2xx JSON response schema.

    Reads the compiled operation the same shape compile_openapi emits verbatim;
    non-2xx and non-JSON responses carry no interface fields.
    """
    fields: dict[str, str | None] = {}
    for status, resp in (op.get("responses") or {}).items():
        if not (isinstance(resp, dict) and "$ref" not in resp):
            continue
        if not (str(status).isdigit() and int(status) in _2XX):
            continue
        schema = (
            (resp.get("content") or {})
            .get("application/json", {})
            .get("schema", {})
        )
        for name, spec in ((schema or {}).get("properties", {}) or {}).items():
            fields[name] = (spec or {}).get("type")
    return fields


def _interface_from_compiled(doc: dict) -> dict[str, dict[str, str | None]]:
    """Read the endpoint->fields interface off the compiled machine document."""
    out: dict[str, dict[str, str | None]] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict) or "responses" not in op:
                continue
            out[f"{method.upper()} {path}"] = _success_fields(op)
    return out


def _gap_endpoint(gap: str) -> str:
    """Best-effort ``VERB /path`` extraction from a compiler gap message.

    Gap strings read like ``contract: GET /a response 200 ...``; pull the
    ``VERB /path`` pair out for the drift record, falling back to the raw text.
    """
    parts = gap.split()
    for i, tok in enumerate(parts):
        if tok.isupper() and tok.isalpha() and i + 1 < len(parts) \
                and parts[i + 1].startswith("/"):
            return f"{tok} {parts[i + 1]}"
    return gap


def _gap_records(doc: dict) -> list[dict]:
    """Turn the compiler's honest ``x-spec-flow-gaps`` into drift records.

    Why: a media-less or empty-schema success response is a hollow contract the
    hand walk used to pass green; the compiler NAMES it, so the diff must too.
    What: one ``contract_gap`` record per named gap, tagged with its endpoint.
    Test: S23.1 (media gap) and S23.2 (body-shape gap).
    """
    records: list[dict] = []
    for gap in doc.get("x-spec-flow-gaps") or []:
        records.append({"kind": "contract_gap",
                        "endpoint": _gap_endpoint(gap), "detail": gap})
    return records


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(json.dumps([{"kind": "validator_error",
                           "detail": "usage: openapi_diff.py <contract> <code>"}]))
        return 2
    fragment = yaml.safe_load(open(argv[1], encoding="utf-8")) or {}
    code = json.load(open(argv[2], encoding="utf-8")) or {}

    try:
        doc = _compile(fragment)
    except spec_openapi.DuplicateRouteError as exc:
        print(json.dumps([{"kind": "duplicate_route", "detail": str(exc)}]))
        return 1

    want = _interface_from_compiled(doc)
    have = code.get("endpoints", {}) or {}

    drifts: list[dict] = _gap_records(doc)
    for ep, fields in want.items():
        if ep not in have:
            drifts.append({"kind": "missing_endpoint", "endpoint": ep})
            continue
        impl = have[ep]
        for fname, ftype in fields.items():
            if fname not in impl:
                drifts.append({"kind": "missing_field", "endpoint": ep, "field": fname})
            elif impl[fname] != ftype:
                drifts.append({
                    "kind": "type_mismatch", "endpoint": ep, "field": fname,
                    "contract": ftype, "code": impl[fname],
                })

    print(json.dumps(drifts))
    return 1 if drifts else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
