"""Compile ONE product-level OpenAPI 3.1 document from ir.json (node B2).

Why: external contract oracles (Schemathesis fuzzing, Specmatic contract
tests) need a single document, but the IR carries one OpenAPI fragment per
node.  This module is the honest merge point: paths come VERBATIM from the
fragments, and the only thing added is what the engine's synthesized router
(`spec_flow_runner._synthesize_entry_code`) provably answers on every route
— 404 unknown path, 405 undeclared method, 500 escaped handler exception,
and 400 ONLY where the contracted requestBody has required fields (the
router's own S12.1/S12.14 validation).  Nothing is guessed: a response with
no recorded media compiles to a description-only response plus a named gap.

What: `compile_openapi(ir)` merges node fragments (refusing duplicate
(method, path) claims with `DuplicateRouteError`), stamps every operation
with its owning node (`x-spec-flow-node`), and injects router error
responses as `$ref`s into ONE shared components section.  `lint_openapi(doc)`
is the stdlib structural self-check returning named findings.

Test: tests/audit/test_openapi_compiler.py (S16.1-S16.4) — crafted IRs plus
the engine's own ir.json; deterministic, no network, no LLM.

Stdlib only.  Consumes ir.json content; never imports the engine.
"""
from __future__ import annotations

import copy
from typing import Any

OPENAPI_VERSION = "3.1.0"

# HTTP methods that are operations inside an OpenAPI path item; every other
# key ("parameters", "x-*", "summary", ...) is metadata, not an operation.
_OA_METHODS = ("get", "put", "post", "delete", "options", "head", "patch",
               "trace")

# The router error catalogue — status -> (component name, description).
# Descriptions QUOTE the exact bodies `_synthesize_entry_code` emits so the
# document never advertises behavior the router does not have.
_ROUTER_ERROR_COMPONENTS = {
    "400": ("RouterBadRequest",
            "Router request validation against the CONTRACTED shape "
            "(S12.1/S12.14): {\"error\": \"missing required field: "
            "'<field>'\"} when a contracted field is absent, "
            "{\"error\": \"invalid json\"} for a malformed body, "
            "{\"error\": \"empty request body\"} for a bodied write "
            "without one."),
    "404": ("RouterNotFound",
            "Router dispatch: {\"error\": \"not found\"} for a path no "
            "node declares."),
    "405": ("RouterMethodNotAllowed",
            "Router dispatch: {\"error\": \"method not allowed\"} for a "
            "declared path asked with an undeclared method; carries an "
            "Allow header listing the path's contracted methods, sorted "
            "and comma-separated (RFC 9110 §15.5.6, S16.6)."),
    "500": ("RouterInternalError",
            "Router guard: {\"error\": \"internal: <type>: <msg>\"} when "
            "an exception escapes a leaf handler."),
}

# Every router error body is exactly {"error": <string>} — one shared schema.
_ROUTER_ERROR_SCHEMA = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
    "additionalProperties": False,
}


class DuplicateRouteError(ValueError):
    """Why: two nodes claiming the same (method, path) is an IR contract
    violation that MUST be attributable (P4), never a silent
    last-writer-wins merge.
    What: raised by compile_openapi naming the method, the path and both
    owning node ids.
    Test: test_duplicate_route_across_nodes_is_named_refusal.
    """


def _operation_has_required_body_fields(op: dict) -> bool:
    """Why: the router's _REQUIRED table only has a row when the contracted
    requestBody schema lists required fields — a 400 anywhere else would be
    invented behavior.
    What: True iff op.requestBody.content.application/json.schema.required
    is a non-empty list.
    Test: test_bodied_operation_without_required_fields_gets_no_400.
    """
    body = op.get("requestBody")
    if not isinstance(body, dict):
        return False
    media = (body.get("content") or {}).get("application/json") or {}
    schema = media.get("schema") if isinstance(media, dict) else None
    return bool(isinstance(schema, dict) and schema.get("required"))


def compile_openapi(ir: dict) -> dict:
    """Why: oracles consume ONE document; the IR carries per-node fragments.
    What: merges every node's `openapi` fragment into a single OpenAPI 3.1
    document — verbatim paths, per-operation `x-spec-flow-node` ownership,
    router-truthful error `$ref`s, honest `x-spec-flow-gaps` for responses
    with no recorded media.  Refuses duplicate (method, path) claims.
    Test: S16.1/S16.2/S16.4 in tests/audit/test_openapi_compiler.py plus
    the end-to-end engine-built-IR test there.
    """
    nodes = ir.get("nodes") or {}
    paths: dict = {}
    owners: dict = {}  # (METHOD, path) -> owning node id
    gaps: list = []
    used_statuses: set = set()  # router statuses actually injected

    for nid in sorted(nodes):
        node = nodes[nid] or {}
        frag = node.get("openapi")
        if not isinstance(frag, dict):
            continue
        for path, item in (frag.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method in _OA_METHODS:
                if method not in item:
                    continue
                op = copy.deepcopy(item[method])
                verb = method.upper()
                prior = owners.get((verb, path))
                if prior is not None:
                    raise DuplicateRouteError(
                        "duplicate route %s %s: node '%s' and node '%s' "
                        "both declare it — route ownership must be unique"
                        % (verb, path, prior, nid))
                owners[(verb, path)] = nid
                op["x-spec-flow-node"] = nid
                responses = op.setdefault("responses", {})
                # Honest media gap: a fragment response with no recorded
                # content stays content-less and is NAMED, never guessed.
                # Honest body-shape gap (H3/F1): a success response WITH media
                # but an empty json schema ({}) pinned nothing — a weak model
                # could invent fields and stay green. The empty schema is left
                # untouched (nothing invented) but the silence is NAMED, the
                # way the media gap is, so it can never read as a closed "any
                # object" contract.
                for status, resp in responses.items():
                    if not (isinstance(resp, dict) and "$ref" not in resp):
                        continue
                    if "content" not in resp:
                        gaps.append(
                            "%s: %s %s response %s has no media recorded "
                            "— compiled without content (honest gap)"
                            % (nid, verb, path, status))
                        continue
                    if str(status).isdigit() and 200 <= int(status) < 300:
                        jm = ((resp.get("content") or {})
                              .get("application/json") or {})
                        sch = jm.get("schema") if isinstance(jm, dict) else None
                        if isinstance(sch, dict) and not sch:
                            gaps.append(
                                "%s: %s %s response %s body shape not recorded "
                                "— compiled with an open schema (honest gap)"
                                % (nid, verb, path, status))
                # Router-truthful error injection: never overwrite a status
                # the fragment already contracts; 400 only where the router
                # actually validates required fields.
                for status in ("400", "404", "405", "500"):
                    if status == "400" and \
                            not _operation_has_required_body_fields(op):
                        continue
                    if status in responses:
                        continue
                    name = _ROUTER_ERROR_COMPONENTS[status][0]
                    responses[status] = {
                        "$ref": "#/components/responses/%s" % name}
                    used_statuses.add(status)
                paths.setdefault(path, {})[method] = op

    components_responses = {}
    for status in sorted(used_statuses):
        name, description = _ROUTER_ERROR_COMPONENTS[status]
        components_responses[name] = {
            "description": description,
            "content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/RouterError"}}},
        }
        if status == "405":
            # Mirror of the router's RFC 9110 Allow header (S16.6): declared
            # per OpenAPI 3.1 response `headers`; the value is per-path data
            # (the path's contracted methods), so only its shape lives here.
            components_responses[name]["headers"] = {"Allow": {
                "description": "The path's contracted methods, sorted and "
                               "comma-separated (RFC 9110 §15.5.6).",
                "schema": {"type": "string"},
            }}

    doc: dict = {
        "openapi": OPENAPI_VERSION,
        "info": {"title": "spec-flow product interface", "version": "1"},
        "paths": paths,
    }
    if components_responses:
        doc["components"] = {
            "schemas": {"RouterError": copy.deepcopy(_ROUTER_ERROR_SCHEMA)},
            "responses": components_responses,
        }
    if gaps:
        doc["x-spec-flow-gaps"] = gaps
    return doc


# ---- structural self-lint (stdlib; the rules we CAN check ourselves) --------

def _resolve_local_ref(doc: dict, ref: str) -> bool:
    """Why: a $ref pointing nowhere inside the document makes oracles choke
    at parse time — cheapest possible failure to catch here.
    What: True iff the '#/'-style JSON pointer resolves within doc.
    Test: test_lint_flags_unresolved_local_ref.
    """
    node: Any = doc
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() \
                and int(key) < len(node):
            node = node[int(key)]
        else:
            return False
    return True


def _walk_refs(doc: dict, node: Any, where: str, findings: list) -> None:
    """Why: refs hide at any depth; one recursive walk beats per-shape rules.
    What: appends an `unresolved-ref` finding for every local $ref that does
    not resolve inside doc.
    Test: test_lint_flags_unresolved_local_ref (planted dangling ref).
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") \
                and not _resolve_local_ref(doc, ref):
            findings.append({
                "rule": "unresolved-ref", "where": where,
                "message": "local $ref '%s' does not resolve inside the "
                           "document" % ref})
        for key, value in node.items():
            _walk_refs(doc, value, "%s.%s" % (where, key), findings)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk_refs(doc, value, "%s[%d]" % (where, i), findings)


def validate_openapi_library(doc: dict) -> list:
    """K1/S16.7: validate the document with the THIRD-PARTY
    openapi-spec-validator (a maintained OpenAPI 3.1 standard oracle),
    returning a list of human-readable errors ([] == standard-valid).

    Why: the engine hand-rolled OpenAPI lint on stdlib; the point of the
    IR-as-OpenAPI form is that a maintained library can read it. This is the
    external-oracle pattern (Specmatic/Schemathesis/jsonschema) applied to the
    compiled document itself — a real 3.1 validator catches spec violations
    our lint never enumerated, and a disagreement between the two is a signal.
    What: runs `openapi_spec_validator.validate`; a raised OpenAPIValidation
    error (or any validation error) becomes one error string. The library is
    a dev/test oracle (tests/requirements-dev.txt), imported lazily so the
    engine does not hard-depend on it at import time.
    Test: tests/audit/test_openapi_library_validator.py."""
    try:
        from openapi_spec_validator import validate as _validate
        from openapi_spec_validator.validation.exceptions import (
            OpenAPIValidationError)
    except ImportError as exc:  # oracle absent — report, never crash the engine
        return ["openapi-spec-validator not installed: %s" % exc]
    try:
        _validate(doc)
        return []
    except OpenAPIValidationError as exc:
        return [str(exc).splitlines()[0] if str(exc) else "invalid OpenAPI"]
    except Exception as exc:  # noqa: BLE001 — any validator failure is an error
        return ["%s: %s" % (type(exc).__name__, str(exc).splitlines()[0])]


def openapi_library_available() -> bool:
    """K2: is the third-party openapi-spec-validator importable here?

    Why: the decomposer seam library-validates each node's OpenAPI document,
    but the library is a dev/test oracle (tests/requirements-dev.txt) — the
    engine must NOT hard-depend on it. A caller distinguishes "library says the
    document is invalid" (a real refusal) from "library is absent" (skip the
    extra check, never a false refusal) with this probe.
    What: True when both symbols import, False otherwise.
    Test: tests/audit/test_decomposer_emits_openapi.py (the seam falls back
    cleanly when the oracle is absent)."""
    try:
        from openapi_spec_validator import validate as _validate  # noqa: F401
        from openapi_spec_validator.validation.exceptions import (  # noqa: F401
            OpenAPIValidationError)
        return True
    except ImportError:
        return False


def node_openapi_library_errors(nid: str, doc: dict) -> list:
    """K2: library-validate ONE node's OpenAPI document at the decomposer seam.

    Why: E1 made every node carry a machine OpenAPI document but validated it
    only with the engine's hand-rolled closed-world check (spec_ir.validate_ir);
    a document invalid by the OpenAPI 3.1 STANDARD (wrong-typed schema, dangling
    $ref, list-where-object) slipped through silently — the v165 class one layer
    up. This runs the maintained third-party oracle over the node's document so
    a standard violation is an attributable, NAMED value, never a silent pass.
    What: returns a list of plain error strings, each naming ``nid`` and the
    library complaint ([] == standard-valid). When the library is ABSENT the
    check is skipped ([]), never a false refusal (the engine stays dev-optional).
    Test: tests/audit/test_decomposer_emits_openapi.py (S22.1 named refusal,
    S22.2 valid document passes)."""
    if not openapi_library_available():
        return []
    if not isinstance(doc, dict) or not doc:
        # a node with no machine OpenAPI document is a prose carrier — that is
        # the interface_policy seam's concern (S15.8), not a library error.
        return []
    return ["node %s: openapi document rejected by openapi-spec-validator: %s"
            % (nid, err) for err in validate_openapi_library(doc)]


def lint_openapi(doc: dict) -> list:
    """Why: shipping a structurally broken document means the external
    oracles reject it before testing anything — self-check first, stdlib
    only, named findings so failures are attributable.
    What: returns a list of {"rule", "where", "message"} findings — required
    top-level keys, the 3.1 version string, info fields, path shape,
    per-operation responses, and resolvable local $refs.  Empty list means
    structurally clean.
    Test: S16.3 in tests/audit/test_openapi_compiler.py (broken docs red,
    compiled document from a valid IR lints clean).
    """
    findings: list = []
    for key in ("openapi", "info", "paths"):
        if key not in doc:
            findings.append({
                "rule": "missing-top-key", "where": "$",
                "message": "required top-level key '%s' is missing" % key})
    version = doc.get("openapi")
    if version is not None and not str(version).startswith("3.1"):
        findings.append({
            "rule": "bad-version", "where": "$.openapi",
            "message": "expected an OpenAPI 3.1.x version string, got %r"
                       % version})
    info = doc.get("info")
    if isinstance(info, dict):
        for field in ("title", "version"):
            if not info.get(field):
                findings.append({
                    "rule": "bad-info", "where": "$.info",
                    "message": "info.%s is required by the OpenAPI "
                               "standard and is missing or empty" % field})
    pth = doc.get("paths")
    if isinstance(pth, dict):
        for path, item in pth.items():
            if not str(path).startswith("/"):
                findings.append({
                    "rule": "bad-path", "where": "$.paths",
                    "message": "path %r does not start with '/'" % path})
            if not isinstance(item, dict):
                findings.append({
                    "rule": "bad-path-item", "where": "$.paths.%s" % path,
                    "message": "path item for %r is not a mapping" % path})
                continue
            for method in _OA_METHODS:
                if method not in item:
                    continue
                op = item[method]
                where = "%s %s" % (method.upper(), path)
                if not isinstance(op, dict):
                    findings.append({
                        "rule": "bad-operation", "where": where,
                        "message": "operation is not a mapping"})
                    continue
                responses = op.get("responses")
                if not isinstance(responses, dict) or not responses:
                    findings.append({
                        "rule": "missing-responses", "where": where,
                        "message": "operation has no responses object — "
                                   "an oracle cannot test it"})
                    continue
                for status, resp in responses.items():
                    if not isinstance(resp, dict) or (
                            "$ref" not in resp
                            and "description" not in resp):
                        findings.append({
                            "rule": "bad-response",
                            "where": "%s -> %s" % (where, status),
                            "message": "a response must be a mapping with "
                                       "a description or a $ref"})
    _walk_refs(doc, doc, "$", findings)
    return findings
