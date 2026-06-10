#!/usr/bin/env python3
"""A real (tiny) OpenAPI-vs-implementation drift validator.

Used as a stand-in for redocly/specmatic so contract_check tests exercise an
actual external validator over actual files instead of a true/false stub.

Usage:
    openapi_diff.py <contract.yaml> <code.json>

Contract (subset of OpenAPI): paths -> method -> responses.200.content.
application/json.schema.properties{name: {type}}.

Code (implementation manifest, JSON):
    {"endpoints": {"POST /shorten": {"short_url": "string", "id": "integer"}}}

Exits 0 when the implementation matches the contract, 1 on any drift, printing
a JSON list of drift records to stdout.
"""

from __future__ import annotations

import json
import sys

try:
    import yaml
except Exception:  # pragma: no cover - yaml is expected in the test env
    print(json.dumps([{"kind": "validator_error", "detail": "PyYAML missing"}]))
    sys.exit(2)


def _contract_endpoints(contract: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path, methods in (contract.get("paths") or {}).items():
        for method, op in (methods or {}).items():
            schema = (
                (((op or {}).get("responses") or {}).get("200") or {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            props = (schema or {}).get("properties", {}) or {}
            fields = {name: (spec or {}).get("type") for name, spec in props.items()}
            out[f"{method.upper()} {path}"] = fields
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(json.dumps([{"kind": "validator_error", "detail": "usage: openapi_diff.py <contract> <code>"}]))
        return 2
    contract = yaml.safe_load(open(argv[1], encoding="utf-8")) or {}
    code = json.load(open(argv[2], encoding="utf-8")) or {}

    want = _contract_endpoints(contract)
    have = code.get("endpoints", {}) or {}

    drifts: list[dict] = []
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
