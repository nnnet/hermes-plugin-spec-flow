"""spec-flow IR — the machine spec, Phase A of the spec-IR rearchitecture.

Why (plan 2026-07-04T00-45): every drift class S10.9-S12.16 is one shape —
two artifacts disagreeing on a value that never existed as data. This module
merges the datums the engine ALREADY records (route ownership, success
status, media, request shape, fixed bodies, module symbol contracts, env
vars, pinned paths, product entry) into ONE machine-checkable structure per
product build, with a per-node COMPLETE closed interface, and validates the
CLOSED WORLD: anything the interface does not declare is an error.

What:
  * ``build_ir(engine)`` — assemble the IR purely from recorded engine
    datums. The builder NEVER invents: a datum the engine never recorded
    leaves the IR field ABSENT (an honest gap, reported by the validator as
    an incompleteness finding — never a guessed default).
  * ``validate_ir(ir)`` — returns
    ``{"errors": [...], "incomplete": [...], "not_checked": [...]}``
    of plain strings naming the node id and the offending value (P4,
    attributable failure). Errors are closed-world violations; incomplete
    are honest gaps (absent datums).

IR top-level shape (format "spec-flow ir v1")::

    {"format": ..., "product": {kind?, entry?, callable?, pinned_files?},
     "nodes": {<node id>: {
         children?:  [child node ids]           # from the realized tree
         files?:     ["src/x.py", ...]          # code_target / module / pins
         openapi?:   REAL OpenAPI 3.1 fragment for the routes the node OWNS
         symbols?:   {exposes: [{name, args}], consumes: [{from, name, args}]}
         env?:       [{name, rule}]             # config surface (S12.1)
         scenarios?: [{requirement, given?, when, then}]
     }}}

Scenarios are FIRST-CLASS with a tiny CLOSED schema the engine owns — this is
the engine's TARGET shape, not a free grammar::

    given {env: {NAME: value}, state: [prior when-steps]}
    when  {method, path, body}
    then  {status, media, body_check = exactly one of
           equals | contains | json_subset}

The GRAMMAR of these scenarios is validated against the Gherkin STANDARD by a
ready-made parser LIBRARY (`gherkin-official`), not by our own string logic
(S31/N1, `gherkin_errors`): each closed scenario is projected to a canonical
`.feature` document and parsed, so a malformed carrier is caught by the library
AST, exactly as `jsonschema` (S13.8) and `openapi-schema-validator` (S14.7) sit
beside the hand checks. The hand `_check_scenario` keeps only the CLOSED
cross-rules a grammar cannot express (declared routes/env, then/openapi
agreement).

Phase A only BUILDS and VALIDATES the IR (and the engine dumps ir.json at
plan time); compiling specs / skeletons / conformance tests from it is
Phases B/C.

Test: tests/audit/test_ir_closed_world.py,
tests/audit/test_ir_openapi_conformance.py,
tests/audit/test_ir_scenarios_schema.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

IR_FORMAT = "spec-flow ir v1"
OPENAPI_VERSION = "3.1.0"

# S38/Q2 (catalog C): fail-closed marker for an oracle that could NOT run (its
# optional library is absent, or a carrier is inactive/unknown). An oracle that
# did not run must be distinguishable from one that ran and found nothing: it
# emits a NOT-CHECKED error carrying this prefix, never a bare `[]` (== ok).
# `[]` stays reserved for "the oracle ran and the datum is clean".
NOT_CHECKED_PREFIX = "NOT-CHECKED: "


def not_checked(reason: str) -> str:
    """Why: absence of a check is not success — a skipped oracle must surface a
    NAMED refusal so no consumer collapses not-checked into ok (catalog C4/C1).
    What: wrap ``reason`` in the NOT-CHECKED marker prefix.
    Test: tests/audit/test_fail_closed_absence.py (is_not_checked round-trip)."""
    return NOT_CHECKED_PREFIX + str(reason)


def is_not_checked(err: Any) -> bool:
    """Why: consumers gate on this to tell a not-run oracle from a clean one.
    What: True iff ``err`` is a string carrying the NOT-CHECKED marker.
    Test: tests/audit/test_fail_closed_absence.py."""
    return isinstance(err, str) and err.startswith(NOT_CHECKED_PREFIX)

_MIME = {"json": "application/json", "html": "text/html"}
_BODIED = ("POST", "PUT", "PATCH")
_OA_METHODS = ("get", "post", "put", "delete", "patch")

# closed key sets — everything else is an error (S13.2)
_TOP_KEYS = {"format", "product", "nodes"}
_PRODUCT_KEYS = {"kind", "entry", "callable", "pinned_files", "requirements"}
# N3/S32: a non-HTTP code leaf carries its behaviour as a machine Gherkin
# feature (``behavior``) — the primary carrier for storage/lib nodes that own
# no route, validated by spec_gherkin at the decomposer seam.
_NODE_KEYS = {"children", "files", "openapi", "symbols", "env", "scenarios",
              "dependencies", "effects", "behavior", "code_target"}
# H5/S13.7: a requirement is a bare name or {name, version?}.
_REQUIREMENT_KEYS = {"name", "version"}
# H6/S17.5: the effect classes a node may declare it is contracted to perform.
_EFFECT_CLASSES = {"subprocess", "network", "env-write", "fs-write",
                   "dynamic-import"}
_SYMBOLS_KEYS = {"exposes", "consumes"}
# N3/S32: an exposed callable may carry its return type and error surface so a
# weak LLM builds it without guessing; spec_gherkin enforces their PRESENCE for
# non-HTTP code leaves, spec_ir only widens the closed world to admit them.
_EXPOSE_KEYS = {"name", "args", "returns", "raises", "signature"}
_CONSUME_KEYS = {"from", "name", "args"}
_ENV_ENTRY_KEYS = {"name", "rule"}
_SCENARIO_KEYS = {"requirement", "given", "when", "then"}
_GIVEN_KEYS = {"env", "state"}
_WHEN_KEYS = {"method", "path", "body"}
_THEN_KEYS = {"status", "media", "body_check"}
_BODY_CHECK_KINDS = {"equals", "contains", "json_subset"}
_OA_TOP_KEYS = {"openapi", "info", "paths"}
_OA_INFO_KEYS = {"title", "version"}
_OA_OP_KEYS = {"summary", "requestBody", "responses"}
_OA_REQBODY_KEYS = {"required", "content"}
_OA_MEDIA_KEYS = {"schema"}
_OA_RESPONSE_KEYS = {"description", "content"}


# --------------------------------------------------------------------------
# builder
# --------------------------------------------------------------------------

def _datum(engine: Any, name: str, default: Any) -> Any:
    """Read a recorded in-memory datum; absent = default, never invented."""
    got = engine.__dict__.get(name)
    return got if got is not None else default


def _call(engine: Any, name: str, default: Any) -> Any:
    """Call a datum-deriving engine method defensively.

    Why: build_ir runs mid-pipeline at plan time; a derivation error must
    surface as an ABSENT datum (incompleteness finding), never crash the run.
    What: returns the method result or `default` on any failure.
    Test: build over a bare engine yields an empty-node IR (never-invents
    case in test_ir_openapi_conformance.py)."""
    try:
        return getattr(engine, name)() or default
    except Exception:  # noqa: BLE001 — absent datum, honest gap
        return default


def _tree_nodes(engine: Any) -> dict:
    """{node id: tree node} from the realized plan, empty when no tree."""
    meta = getattr(engine, "_project_meta", None) or {}
    out: dict = {}

    def walk(n: Any) -> None:
        if not isinstance(n, dict) or not n.get("id"):
            return
        out[str(n["id"])] = n
        for k in (n.get("children") or []):
            walk(k)

    walk(meta.get("tree") or {})
    return out


def _when_step(engine_mod: Any, method: str, path: str,
               shape: Optional[list]) -> dict:
    """One when-step from the datums. Body only when the engine holds the
    COMPLETE datum: recorded-empty shape => body {}; a shaped body whose
    VALUES were never recorded stays absent (honest gap)."""
    step: dict = {"method": method, "path": path}
    if method in _BODIED and shape == []:
        step["body"] = {}
    return step


def _node_scenarios(engine: Any, nid: str, routes: list, reqf: dict,
                    media: dict, contract: dict, status_fn: Any,
                    fixed_fn: Any) -> list:
    """One scenario per owned route, grouped by requirement id (= the owning
    node), derived from the SAME datums the openapi fragment reads."""
    roundtrip = str(((contract.get("boot") or {}).get("json_roundtrip"))
                    or "")
    out: list = []
    for method, path in routes:
        shape = reqf.get((method, path))
        sc: dict = {"requirement": nid,
                    "when": _when_step(engine, method, path, shape),
                    "then": {"status": status_fn(method)}}
        md = media.get(path)
        if md in _MIME:
            sc["then"]["media"] = _MIME[md]
        fixed = fixed_fn(method, path)
        if fixed is not None:
            sc["then"]["body_check"] = {"equals": fixed}
        # boot json_roundtrip datum means POST-then-GET on the same path:
        # the GET scenario carries the prior POST as given.state
        if method == "GET" and path == roundtrip and ("POST", path) in [
                (m, p) for m, p in routes]:
            sc["given"] = {"state": [
                _when_step(engine, "POST", path, reqf.get(("POST", path)))]}
        out.append(sc)
    return out


def _node_openapi(nid: str, routes: list, reqf: dict, media: dict,
                  status_fn: Any, fixed_fn: Any, handler_fn: Any) -> dict:
    """A REAL OpenAPI 3.1 fragment for the routes this node OWNS, built from
    the datums; absent datum => absent field (S13.1/S13.3)."""
    paths: dict = {}
    for method, path in routes:
        op: dict = {"x-spec-flow-handler": handler_fn(method, path)}
        shape = reqf.get((method, path))
        if method in _BODIED and shape is not None:
            op["requestBody"] = {
                "required": bool(shape),
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {f: {} for f in shape},
                    "required": list(shape),
                    "additionalProperties": False}}}}
        status = str(status_fn(method))
        # H2/S13.6: this status is the engine's REST convention (POST->201,
        # else 200), not a spec datum — mark its origin so ir.json shows what
        # the spec asked for vs what the engine invented (F5). A decomposer
        # machine fragment replaces the whole operation and carries no marker.
        op["x-spec-flow-status-source"] = "convention"
        resp: dict = {"description": "contracted success response"}
        md = media.get(path)
        fixed = fixed_fn(method, path)
        if fixed is not None:
            resp["content"] = {"application/json": {"schema": {
                "const": fixed}}}
            # H2/S13.6: the engine inlined this body (e.g. /health ->
            # {"status": "ok"}) — an engine convention, not a spec datum.
            op["x-spec-flow-body-source"] = "convention"
        elif md in _MIME:
            resp["content"] = {_MIME[md]: {"schema": {}}}
        op["responses"] = {status: resp}
        paths.setdefault(path, {})[method.lower()] = op
    return {"openapi": OPENAPI_VERSION,
            "info": {"title": "spec-flow node %s interface" % nid,
                     "version": "1"},
            "paths": paths}


# H7/S13.8: a JSON Schema (draft 2020-12) — a SECOND, third-party structural
# oracle for the IR, run alongside the hand-rolled validate_ir. It closes the
# same top/product/node levels; validate_ir keeps the closed-world SEMANTICS
# (route ownership, phantom consumes) a schema cannot express.
_REQUIREMENT_SCHEMA = {"oneOf": [
    {"type": "string"},
    {"type": "object", "properties": {"name": {"type": "string"},
                                      "version": {"type": "string"}},
     "required": ["name"], "additionalProperties": False}]}
IR_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "format": {"const": IR_FORMAT},
        "product": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "entry": {"type": "string"},
                "callable": {"type": "array"},
                "pinned_files": {"type": "array"},
                "requirements": {"type": "array",
                                 "items": _REQUIREMENT_SCHEMA}},
            "additionalProperties": False},
        "nodes": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "children": {"type": "object"},
                    "files": {"type": "array"},
                    "openapi": {"type": "object"},
                    "symbols": {"type": "object",
                                "properties": {
                                    "exposes": {"type": "array"},
                                    "consumes": {"type": "array"}},
                                "additionalProperties": False},
                    "env": {"type": "array"},
                    "scenarios": {"type": "array"},
                    "dependencies": {"type": "array",
                                     "items": {"type": "string"}},
                    "effects": {"type": "array",
                                "items": {"enum": sorted(_EFFECT_CLASSES)}},
                    "behavior": {"type": "string"},
                    # Q9 (S49): the owner file an amend node edits in place; its
                    # presence marks a carrier-less-by-design edit node (exempt
                    # from the hollow check, like a branch/entry).
                    "code_target": {"type": "string"}},
                "additionalProperties": False}}},
    "required": ["format", "nodes"],
    "additionalProperties": False}


def jsonschema_errors(ir: Any) -> list:
    """H7/S13.8: validate the IR STRUCTURE with the third-party `jsonschema`
    oracle (draft 2020-12), returning a list of human-readable errors.

    Why: a second, independent witness of structure — two oracles disagreeing
    is itself a signal (the Specmatic/Schemathesis pattern applied to the IR).
    validate_ir keeps the closed-world semantics on top; this catches the
    structural class (unknown key, wrong type) from a maintained library
    rather than only hand-rolled checks. The schema is strict (draft 2020-12,
    `additionalProperties: False` throughout): empty/extra keys are errors at
    the model level (catalog C, Pydantic-v2 `extra='forbid'` equivalent).
    S38/Q2 (catalog C4): an ABSENT library no longer crashes the run — it
    returns a NOT-CHECKED marker (the oracle was skipped, not passed).
    Test: tests/audit/test_ir_jsonschema_oracle.py, test_fail_closed_absence.py."""
    try:
        import jsonschema  # dev/test oracle, not a runtime dependency
    except ImportError:
        return [not_checked(
            "jsonschema (draft 2020-12) structural oracle skipped — the "
            "library is not importable; IR structure went unvalidated")]
    validator = jsonschema.Draft202012Validator(
        IR_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())
    return ["%s: %s" % ("/".join(str(p) for p in e.path) or "<root>",
                        e.message)
            for e in sorted(validator.iter_errors(ir), key=str)]


def _scenario_to_gherkin(nid: str, idx: int, sc: dict) -> str:
    """Render ONE closed Given/When/Then scenario to a canonical Gherkin
    `.feature` document — the standard machine carrier the parser library reads.

    Why (S31): the engine owns a closed `{given, when, then}` structure; to hand
    its GRAMMAR to a ready-made oracle the structure is projected into the
    standard Gherkin surface. `given.state` when-steps become `Given` steps,
    the `when` becomes the `When`, and `then` becomes `Then` (+ optional `And`
    for media / body_check). The scenario title is the requirement id; a
    requirement carrying newlines is deliberately NOT sanitised here, so a
    malformed carrier reaches the parser and is caught as a grammar break
    rather than silently smoothed over.
    What: returns the feature text (always exactly one Feature, one Scenario).
    Test: tests/audit/test_gherkin_lib_oracle.py."""
    title = str(sc.get("requirement") or nid)
    lines = ["Feature: node %s" % nid, "  Scenario: %s" % title]
    given = sc.get("given") if isinstance(sc.get("given"), dict) else {}
    for st in (given.get("state") or []):
        if isinstance(st, dict):
            lines.append("    Given a %s request to %s"
                         % (st.get("method"), st.get("path")))
    when = sc.get("when") if isinstance(sc.get("when"), dict) else {}
    lines.append("    When a %s request to %s"
                 % (when.get("method"), when.get("path")))
    then = sc.get("then") if isinstance(sc.get("then"), dict) else {}
    lines.append("    Then the response status is %s" % then.get("status"))
    if then.get("media") is not None:
        lines.append("    And the response media type is %s" % then["media"])
    bc = then.get("body_check")
    if isinstance(bc, dict) and len(bc) == 1:
        kind, expected = next(iter(bc.items()))
        lines.append("    And the response body %s %s" % (kind, expected))
    return "\n".join(lines) + "\n"


def _expected_step_keywords(sc: dict) -> list:
    """The keyword sequence the closed structure implies for its Gherkin
    projection: one Given per given.state step, one When, one Then, one And per
    optional then clause (media / body_check). The parser AST must reproduce
    exactly this — any drift means the rendered carrier was ungrammatical
    (an injected keyword line stole or spawned a step)."""
    given = sc.get("given") if isinstance(sc.get("given"), dict) else {}
    kws = ["Given" for st in (given.get("state") or [])
           if isinstance(st, dict)]
    kws.append("When")
    kws.append("Then")
    then = sc.get("then") if isinstance(sc.get("then"), dict) else {}
    if then.get("media") is not None:
        kws.append("And")
    bc = then.get("body_check")
    if isinstance(bc, dict) and len(bc) == 1:
        kws.append("And")
    return kws


# Lazily-loaded gherkin-official parser pieces. None until first lookup, a tuple
# (Parser, TokenScanner, error-classes) when present, False when the library is
# genuinely absent — so a missing dev dependency degrades to the hand check
# instead of hard-crashing validation (the S14.7 optional-oracle contract).
_GHERKIN_PARSER: Any = None


def _gherkin_parser() -> Any:
    """The gherkin-official (Parser, TokenScanner, (error types)) if importable,
    else None.

    Why: the parser LIBRARY is the oracle of Gherkin grammar; it is a dev/test
    dependency (tests/requirements-dev.txt), not a hard runtime dependency of
    the shipped engine. Imported lazily and tolerating absence keeps validate_ir
    usable in a bare environment (grammar oracle skipped, hand cross-rules run).
    What: imports once, memoises the tuple (or False on ImportError)."""
    global _GHERKIN_PARSER
    if _GHERKIN_PARSER is None:
        try:
            from gherkin.parser import Parser
            from gherkin.token_scanner import TokenScanner
            from gherkin.errors import ParserError, CompositeParserException
            _GHERKIN_PARSER = (Parser, TokenScanner,
                               (ParserError, CompositeParserException))
        except Exception:  # noqa: BLE001 — optional dev/test oracle
            _GHERKIN_PARSER = False
    return _GHERKIN_PARSER or None


def gherkin_errors(ir: Any) -> list:
    """S31/N1: validate the Gherkin GRAMMAR of every scenario with the ready-made
    `gherkin-official` Cucumber parser, returning human-readable errors.

    Why (user 2026-07-06, hard rule): a spec must be in a machine STANDARD whose
    grammar is enforced by a maintained oracle LIBRARY, never by a home-grown
    splitter. The closed key-check (`_check_scenario`) is grammar-blind: a
    closed-valid scenario can still render to a broken Gherkin document (a
    requirement id that opens a second `Scenario:`, a value injecting a keyword
    line). This projects each scenario to canonical Gherkin and PARSES it — a
    parse error, or an AST that disagrees with the closed structure (not exactly
    one Feature+Scenario, or a step-keyword sequence differing from the closed
    projection), is a named error.
    What: returns a list of "node <nid>: scenario[<i>]: ..." strings; empty when
    every scenario is grammatical. S38/Q2 (catalog C4): when the library is
    ABSENT but there ARE scenarios to check, the oracle no longer degrades to
    `[]`==ok — it returns a single NOT-CHECKED marker (the check was SKIPPED,
    not passed). A NOT-CHECKED result is not silence; validate_ir carries it as
    a named refusal. (No scenarios to check => empty is legitimate: nothing to
    validate is not the same as an un-run oracle.)
    Test: tests/audit/test_gherkin_lib_oracle.py, test_fail_closed_absence.py."""
    nodes = (ir or {}).get("nodes") if isinstance(ir, dict) else None
    parts = _gherkin_parser()
    if parts is None:
        n_scen = sum(len(node.get("scenarios") or [])
                     for node in (nodes or {}).values()
                     if isinstance(node, dict))
        if n_scen:
            return [not_checked(
                "gherkin grammar oracle skipped — the `gherkin-official` "
                "library is not importable; %d scenario(s) went unvalidated "
                "(install the oracle or treat this run as unchecked)" % n_scen)]
        return []
    Parser, TokenScanner, err_types = parts
    out: list = []
    for nid, node in sorted((nodes or {}).items()):
        if not isinstance(node, dict):
            continue
        for idx, sc in enumerate(node.get("scenarios") or []):
            if not isinstance(sc, dict):
                continue
            where = "node %s: scenario[%d]" % (nid, idx)
            text = _scenario_to_gherkin(str(nid), idx, sc)
            try:
                doc = Parser().parse(TokenScanner(text))
            except err_types as exc:  # a broken Gherkin document
                out.append("%s: gherkin grammar error: %s" % (where, exc))
                continue
            feat = doc.get("feature") if isinstance(doc, dict) else None
            children = (feat or {}).get("children") or []
            scen_children = [c for c in children if c.get("scenario")]
            if len(scen_children) != 1:
                out.append(
                    "%s: gherkin AST has %d scenarios, expected exactly 1 "
                    "(an injected keyword line spawned or stole a scenario)"
                    % (where, len(scen_children)))
                continue
            steps = (scen_children[0].get("scenario") or {}).get("steps") or []
            got = [str(s.get("keyword") or "").strip() for s in steps]
            want = _expected_step_keywords(sc)
            if got != want:
                out.append(
                    "%s: gherkin step keywords %s disagree with the closed "
                    "structure %s (the carrier was ungrammatical)"
                    % (where, got, want))
    return out


# --- N6/S34: machine model of node-spec completeness for the weak-LLM -------
# criterion. This is the DEFINITION of "complete enough for a weak model to
# build the node" expressed as DATA (a detector), NOT a gate: the milestone
# that consumes these gaps is emitted elsewhere (N2). It answers "is a MANDATORY
# aspect ABSENT?", which is orthogonal to the FORMAT oracles (validate_ir /
# gherkin_errors / validate_openapi_library) that answer "is the carrier VALID?".
# A node can pass every format oracle and still be hollow: no requirements, no
# error/edge cases, untyped exposed signatures.

# The seven content aspects a node may have to carry (plan "Модель содержания").
COMPLETENESS_ASPECTS = (
    "behavior",         # WHAT IT DOES — executable behaviour (scenarios/Gherkin)
    "http_interface",   # WHAT IT CONFORMS TO — HTTP interface (openapi.paths)
    "data_schema",      # WHAT IT CONFORMS TO — data/body shapes (JSON Schema)
    "public_api",       # PUBLIC API — typed exposes (signatures/returns/raises)
    "architecture",     # ARCHITECTURE — dependencies/effects/files placement
    "requirements",     # REQUIREMENTS — declared and traceable
    "errors_edges",     # ERRORS/EDGE CASES — failure surface (Examples / non-2xx)
)


def _node_class(node: Any) -> str:
    """Classify a node so applicability of each aspect is decided by CLASS,
    never guessed per-field.

    Why: a non-HTTP storage leaf legitimately owns no route, and a branch
    executes nothing itself — treating a missing HTTP interface as a hole on
    either would false-red a correct spec. Applicability is class-driven.
    What: returns "http" (owns an ``openapi`` document), "code" (a non-HTTP
    ``.py`` leaf per spec_gherkin.is_code_leaf), "branch" (delegates via
    ``children``) or "other" (a leaf that is none of these — e.g. a config-only
    node), reusing the SAME leaf test the behaviour carrier gate uses.
    Test: tests/audit/test_spec_completeness_gaps.py."""
    if not isinstance(node, dict):
        return "other"
    if node.get("openapi"):
        return "http"
    if node.get("children"):
        return "branch"
    import spec_gherkin  # local import: no module-level cycle, mirrors _gherkin_parser
    if spec_gherkin.is_code_leaf("?", node):
        return "code"
    return "other"


def _entry_exposes(nid: str, files: Any, contract: Any) -> list:
    """Q8 (S47): the product callable(s) the ASSEMBLY entry exposes, as typed
    symbols. The entry node (id ``product_entry`` or the owner of the product
    entry file, e.g. ``src/app.py``) wires every module's routes into ONE
    callable (``wsgi_app`` / ``application`` / ``app``); that callable IS its
    public surface. Recording it makes the entry a code node with a real typed
    contract instead of a carrier-less hollow shell (it delegates like a branch,
    but the hollow check sees a leaf). Empty for any other node or when the
    product declared no callable — single-source: the names come from the
    engine-declared product contract, never invented.
    Test: tests/audit/test_entry_exposes_carrier.py."""
    contract = contract if isinstance(contract, dict) else {}
    entry_file = str(contract.get("entry") or "")
    callables = [str(c) for c in (contract.get("callable") or []) if c]
    files = files if isinstance(files, list) else []
    is_entry = (nid == "product_entry"
                or (entry_file != "" and entry_file in files))
    if not (is_entry and callables):
        return []
    return [{"name": c, "args": None} for c in callables]


def _http_owns_routes(node: Any) -> bool:
    """Whether an HTTP node actually declares at least one operation path."""
    doc = node.get("openapi")
    return bool(isinstance(doc, dict) and doc.get("paths"))


def _http_has_body_schema(node: Any) -> bool:
    """Whether any HTTP operation records a request/response body schema — the
    node's data/form contract. Presence only; validity is jsonschema's job."""
    paths = ((node.get("openapi") or {}).get("paths") or {})
    for ops in paths.values():
        if not isinstance(ops, dict):
            continue
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            rb = op.get("requestBody")
            if isinstance(rb, dict) and rb.get("content"):
                return True
            for resp in (op.get("responses") or {}).values():
                if isinstance(resp, dict) and resp.get("content"):
                    return True
    return False


def _http_has_error_response(node: Any) -> bool:
    """Whether any HTTP operation declares a non-2xx (error/edge) response.

    Why: a route that lists only its success status leaves the failure surface
    unspecified — a weak LLM will not invent the error modes."""
    paths = ((node.get("openapi") or {}).get("paths") or {})
    for ops in paths.values():
        if not isinstance(ops, dict):
            continue
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            for status in (op.get("responses") or {}):
                s = str(status)
                if s and s[0] not in ("2", "x", "X") and s != "default":
                    return True
                if s == "default":
                    return True
    return False


def _behavior_has_edge_cases(text: Any) -> bool:
    """Whether a Gherkin behaviour carrier declares an error/edge case as well
    as the happy path.

    Why: presence of an ``Examples:`` table or a second ``Scenario`` is the
    machine signal that failure/boundary behaviour was specified, not just the
    nominal flow. This is a PRESENCE probe over the raw text — the Gherkin
    GRAMMAR itself is validated by ``feature_library_errors``/``gherkin_errors``,
    not here.
    What: True iff the text carries an ``Examples:`` block or two-plus
    ``Scenario``/``Scenario Outline`` blocks."""
    body = str(text or "")
    if "Examples:" in body:
        return True
    n_scenarios = body.count("Scenario:") + body.count("Scenario Outline:")
    return n_scenarios >= 2


def _requirements_of(node: Any) -> list:
    """Requirements declared on the node itself (a decomposer node carries them
    inline; the assembled IR later lifts them to product.requirements)."""
    reqs = node.get("requirements")
    return reqs if isinstance(reqs, list) else []


def _has_architecture(node: Any) -> bool:
    """Whether the node states its placement/side-effect envelope: source
    ``files`` plus a declared ``effects`` class list (empty list = an explicit
    "no side effects" decision, which is still a decision)."""
    files = node.get("files")
    has_files = isinstance(files, list) and bool(files)
    has_effects = isinstance(node.get("effects"), list)
    return has_files and has_effects


def spec_completeness_gaps(node: Any) -> list:
    """The MACHINE model of "complete enough for a weak LLM" for ONE node.

    Why: the single build criterion turns on a node carrying every MANDATORY
    content aspect, not merely on the aspects it does carry being format-valid.
    The format oracles (validate_ir / gherkin_errors / validate_openapi_library
    / jsonschema_errors) never fire on a valid-but-hollow spec — no requirements,
    no error/edge cases, untyped exposed signatures are not format defects. This
    detector closes that orthogonal hole; it only DETECTS — the milestone that
    acts on the gaps is emitted by a separate node (N2).
    What: classifies the node, decides which of COMPLETENESS_ASPECTS APPLY to
    that class (an inapplicable aspect is n/a and NEVER a gap — no silent skip),
    and returns the applicable-yet-empty aspects as ``{aspect, why}`` records,
    where ``why`` states why the aspect is mandatory and empty. Reuses
    spec_gherkin's leaf test and exposes-completeness check rather than
    re-deriving them.
    Test: tests/audit/test_spec_completeness_gaps.py (S34)."""
    if not isinstance(node, dict):
        return [{"aspect": "architecture",
                 "why": "node is not a mapping — it carries no spec at all"}]

    cls = _node_class(node)
    gaps: list = []

    def gap(aspect: str, why: str) -> None:
        gaps.append({"aspect": aspect, "why": why})

    # A branch delegates to children; behaviour, interface, data and public API
    # are n/a for it (its children carry them). Only architecture is required.
    if cls == "branch":
        if not (isinstance(node.get("children"), list) and node["children"]):
            gap("architecture",
                "a branch must name the children it delegates to")
        return gaps

    # --- behavior — mandatory for every executable node ---------------------
    if cls in ("http", "code", "other"):
        if cls == "http":
            has_behavior = bool(node.get("scenarios") or node.get("behavior"))
        else:
            has_behavior = bool(str(node.get("behavior") or "").strip())
        if not has_behavior:
            gap("behavior",
                "an executable node with no behaviour carrier "
                "(scenarios/Gherkin) has nothing to build against")

    # --- http_interface — only if the node owns routes ----------------------
    if cls == "http":
        if not _http_owns_routes(node):
            gap("http_interface",
                "an HTTP node must declare at least one operation path")
    # (n/a for code/branch/other — they own no routes)

    # --- data_schema — if the node works with data/forms --------------------
    if cls == "http":
        if _http_owns_routes(node) and not _http_has_body_schema(node):
            gap("data_schema",
                "an HTTP route records no request/response body shape — a weak "
                "LLM cannot infer the data contract")
    # code/other: their data contract is the typed public API, checked below.

    # --- public_api — typed exposes for a code leaf -------------------------
    if cls == "code":
        import spec_gherkin  # local import: no module-level cycle
        exposes = ((node.get("symbols") or {}).get("exposes") or [])
        if not exposes:
            gap("public_api",
                "a non-HTTP code leaf exposes no typed callable — a weak LLM "
                "must guess the contract")
        else:
            frags: list = []
            for ent in exposes:
                frags.extend(spec_gherkin._expose_incompleteness(ent))
            if frags:
                gap("public_api",
                    "exposed callables are not fully typed "
                    "(untyped args / missing return type / no error surface): "
                    "%s" % "; ".join(frags))
    # (n/a for http — its contract lives in the OpenAPI document)

    # --- architecture — placement + side-effect envelope --------------------
    if cls in ("http", "code", "other"):
        if not _has_architecture(node):
            gap("architecture",
                "no source files and/or effects declared — a weak LLM has no "
                "placement or side-effect envelope")

    # --- requirements — traceable; mandatory once the node imports a dep ----
    if cls in ("http", "code", "other"):
        deps = node.get("dependencies")
        deps = deps if isinstance(deps, list) else []
        reqs = _requirements_of(node)
        if deps and not reqs:
            gap("requirements",
                "the node imports dependencies %r but declares no traceable "
                "requirement to resolve them" % deps)

    # --- errors_edges — failure/boundary surface ----------------------------
    if cls == "http":
        if _http_owns_routes(node) and not _http_has_error_response(node):
            gap("errors_edges",
                "the route declares only success responses — its error surface "
                "is unspecified")
    elif cls in ("code", "other"):
        # Only a node that actually carries behaviour can carry its edge cases;
        # an absent behaviour is already reported by the behavior aspect.
        if str(node.get("behavior") or "").strip() \
                and not _behavior_has_edge_cases(node.get("behavior")):
            gap("errors_edges",
                "the behaviour spec has only a happy path (no Examples / second "
                "Scenario) — a weak LLM will not invent the edge cases")

    return gaps


def _hollow_node_reason(nid: str, node: Any) -> "Optional[str]":
    """S38/Q2 (catalog C3): name the reason an executable node is HOLLOW, or
    None.

    Why: a syntactically valid but EMPTY spec (no behaviour carrier AND no
    interface/typed contract) passed the closed-world oracle with zero errors —
    the format was fine, the content absent, and absence read as success. This
    is the strictest completeness failure (a superset-empty node, not merely one
    missing aspect): validate_ir must BLOCK on it, not just record a gap.
    What: returns a reason string when an executable (non-branch) node carries
    NEITHER behaviour (scenarios / Gherkin / an error response) NOR any
    interface/typed contract (HTTP paths for an http node, typed exposes for a
    code node), else None. A branch delegates to children and is never hollow.
    Test: tests/audit/test_fail_closed_absence.py."""
    if not isinstance(node, dict):
        return None
    # Q9 (S49): an AMEND node edits an existing owner IN PLACE (code_target set)
    # — a late requirement folded into a surface another node owns. It carries
    # no carrier of its own BY DESIGN (its carrier is the owner's), exactly like
    # a branch or the assembly entry delegates. The engine already exempts amend
    # nodes from the card and ownership gates; the hollow check exempts them too.
    if str(node.get("code_target") or "").strip():
        return None
    cls = _node_class(node)
    if cls == "branch":
        return None
    has_behavior = bool(node.get("scenarios")
                        or str(node.get("behavior") or "").strip())
    if cls == "http":
        has_contract = _http_owns_routes(node)
    elif cls == "code":
        has_contract = bool((node.get("symbols") or {}).get("exposes"))
    else:  # "other": carries neither routes nor a typed public API by class
        has_contract = False
    if has_behavior or has_contract:
        return None
    return ("node %s: hollow spec — an executable node carrying neither a "
            "behaviour carrier (scenarios/Gherkin) nor an interface/typed "
            "contract (routes/typed exposes) has no content to build; a "
            "valid-but-empty shell is a FAIL, not a silent PASS" % nid)


def requirements_txt(ir: Any) -> str:
    """H5/S13.7: compile a pip requirements.txt from `product.requirements`.

    Why: once the spec can REQUEST a third-party library the product build
    needs a materialised manifest — the requested deps, nothing invented.
    What: one line per requirement; `{name, version}` -> `name==version`,
    a bare name -> `name`. Empty when the spec requested nothing (stdlib-only
    stays the default). Deterministic order = declaration order.
    Test: tests/audit/test_ir_dependencies.py."""
    product = (ir or {}).get("product") if isinstance(ir, dict) else None
    lines = []
    for req in ((product or {}).get("requirements") or []):
        if isinstance(req, str) and req:
            lines.append(req)
        elif isinstance(req, dict) and req.get("name"):
            ver = req.get("version")
            lines.append("%s==%s" % (req["name"], ver) if ver
                         else str(req["name"]))
    return "\n".join(lines) + ("\n" if lines else "")


def collect_ir_sources(engine: Any) -> dict:
    """Read every raw datum the IR is built from — ONCE — into one snapshot.

    Why (node I2): the IR and its downstream projections (contracts/
    interface.json's route rows: media, request_fields, success_status) used
    to each re-call `_route_media_map()` / `_route_request_fields()` on their
    own. Two independent reads of the same datum = the pairwise-drift class.
    Collecting the raw datums into ONE snapshot here makes that snapshot the
    single upstream: `build_ir` and the interface projection both read it, so
    there is one direction (datums -> snapshot -> {IR, interface}) and the
    datum methods are never queried a second time behind the accumulator.
    What: returns a dict of the raw datums (owners / module_names /
    module_contracts / module_importers / media / request_fields / env vars /
    pinned paths / product contract). Missing datum => empty, never guessed.
    Test: tests/audit/test_ir_single_accumulator.py."""
    return {
        "owners": _datum(engine, "_route_owners", {}),
        "module_names": _datum(engine, "_module_names", {}),
        "contracts_sym": _datum(engine, "_module_contracts", {}),
        "importers": _datum(engine, "_module_importers", {}),
        "media": _call(engine, "_route_media_map", {}),
        "reqf": _call(engine, "_route_request_fields", {}),
        "envs": _call(engine, "_constitution_env_vars", []),
        "pins": _call(engine, "_constitution_pinned_paths", []),
        "contract": _call(engine, "_product_contract", {}),
    }


def build_ir(engine: Any, sources: "dict | None" = None) -> dict:
    """Assemble the IR from ONE datum snapshot (the single-source accumulator).

    Why: ONE closed structure per node kills the pairwise-drift class by
    construction — every consumer reads the same merged data.
    What: merges the raw datums captured by `collect_ir_sources` — owners /
    module_names / module_contracts / module_importers / media /
    request_fields / env vars / pinned paths / product contract plus the pure
    route functions — into the IR documented in the module docstring. When
    `sources` is None it captures them itself; the runner passes the held
    snapshot so IR and interface.json share one upstream. Missing datum =>
    absent field, never a guessed default.
    Test: tests/audit/test_ir_openapi_conformance.py."""
    # late import: the runner imports this module, avoid a hard cycle
    try:
        from . import spec_flow_runner as sfr  # type: ignore
    except Exception:  # noqa: BLE001 — flat layout (repo root on sys.path)
        import spec_flow_runner as sfr  # type: ignore

    if sources is None:
        sources = collect_ir_sources(engine)
    owners: dict = sources["owners"]
    module_names: dict = sources["module_names"]
    contracts_sym: dict = sources["contracts_sym"]
    importers: dict = sources["importers"]
    media: dict = sources["media"]
    reqf: dict = sources["reqf"]
    envs: list = sources["envs"]
    pins: list = sources["pins"]
    contract: dict = sources["contract"]
    tree = _tree_nodes(engine)

    routes_by_node: dict = {}
    for (method, path), nids in owners.items():
        for nid in nids:
            routes_by_node.setdefault(str(nid), []).append(
                ((method or "GET").upper(), str(path)))
    for rs in routes_by_node.values():
        rs.sort()

    stem_by_node = {str(k): str(v) for k, v in module_names.items()}
    node_by_stem = {v: k for k, v in stem_by_node.items()}
    env_entries = [{"name": n, "rule": r} for n, r in envs]

    nodes: dict = {}
    all_ids = sorted(set(routes_by_node) | set(stem_by_node) | set(tree))
    for nid in all_ids:
        entry: dict = {}
        tn = tree.get(nid)
        if tn is not None:
            entry["children"] = [str(k.get("id"))
                                 for k in (tn.get("children") or [])
                                 if isinstance(k, dict) and k.get("id")]
        files: list = []
        if tn is not None and tn.get("code_target"):
            files.append(str(tn["code_target"]))
        stem = stem_by_node.get(nid)
        if stem:
            mod_file = "src/%s.py" % stem
            if mod_file not in files:
                files.append(mod_file)
            for pin in pins:
                if Path(str(pin)).stem == stem and str(pin) not in files:
                    files.append(str(pin))
        if files:
            entry["files"] = files

        routes = routes_by_node.get(nid) or []
        if routes:
            entry["openapi"] = _node_openapi(
                nid, routes, reqf, media, sfr._route_success_status,
                sfr._route_fixed_body, sfr._canonical_handler_symbol)
            entry["scenarios"] = _node_scenarios(
                engine, nid, routes, reqf, media, contract,
                sfr._route_success_status, sfr._route_fixed_body)

        symbols: dict = {}
        if stem and contracts_sym.get(stem):
            symbols["exposes"] = [dict(e) for e in contracts_sym[stem]]
        # Q8 (S47): the assembly entry exposes the declared product callable
        # (wsgi_app/…) — record it when the module contract carried none, so the
        # entry node is a code node with a real surface, not a hollow shell.
        if not symbols.get("exposes"):
            _ee = _entry_exposes(nid, entry.get("files"), contract)
            if _ee:
                symbols["exposes"] = _ee
        # Q9 (S49): carry the amend marker into the IR — a node that EDITS an
        # existing owner in place (code_target) is carrier-less by design and is
        # exempt from the hollow check (its carrier is the owner's).
        _ctgt = tn.get("code_target") if isinstance(tn, dict) else None
        if _ctgt:
            entry["code_target"] = str(_ctgt)
        consumes: list = []
        for exp_stem in sorted(importers):
            if stem and stem in (importers.get(exp_stem) or set()):
                for ent in (contracts_sym.get(exp_stem) or []):
                    consumes.append({"from": exp_stem,
                                     "name": ent.get("name"),
                                     "args": ent.get("args")})
        if consumes:
            symbols["consumes"] = consumes
        if symbols:
            entry["symbols"] = symbols

        # config surface: nodes that build code may read constitution env
        # vars (the engine's own leaf gates apply them to every handler)
        if env_entries and (routes or files):
            entry["env"] = [dict(e) for e in env_entries]

        if entry:
            nodes[nid] = entry
    # a node id recorded with routes but an otherwise empty entry must
    # still appear — openapi/scenarios already guarantee non-emptiness

    product: dict = {}
    for key in ("kind", "entry", "callable"):
        if contract.get(key):
            product[key] = contract[key]
    if pins:
        product["pinned_files"] = [str(p) for p in pins]

    return {"format": IR_FORMAT, "product": product, "nodes": nodes}


# --------------------------------------------------------------------------
# validator — the closed world
# --------------------------------------------------------------------------

def _keys(obj: dict, allowed: set, where: str, errors: list,
          allow_x: bool = False) -> None:
    """Unknown keys anywhere = error (S13.2). `allow_x` admits OpenAPI x-*
    specification extensions — part of the real standard."""
    for k in obj:
        if k in allowed or (allow_x and str(k).startswith("x-")):
            continue
        errors.append("%s: unknown key '%s'" % (where, k))


def _check_openapi(nid: str, doc: Any, errors: list, incomplete: list,
                   registry: dict) -> None:
    """Structural check of one node's OpenAPI 3.1 fragment; records every
    (METHOD, path) -> (nid, operation) into `registry` for cross checks."""
    where = "node %s" % nid
    if not isinstance(doc, dict):
        errors.append("%s: openapi is not a mapping" % where)
        return
    _keys(doc, _OA_TOP_KEYS, where + ": openapi", errors, allow_x=True)
    if doc.get("openapi") != OPENAPI_VERSION:
        errors.append("%s: openapi version %r is not %r"
                      % (where, doc.get("openapi"), OPENAPI_VERSION))
    info = doc.get("info")
    if not isinstance(info, dict) or not info.get("title") \
            or not info.get("version"):
        errors.append("%s: openapi info must carry title and version"
                      % where)
    elif isinstance(info, dict):
        _keys(info, _OA_INFO_KEYS, where + ": openapi info", errors,
              allow_x=True)
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            errors.append("%s: path %s is not a mapping" % (where, path))
            continue
        _keys(item, set(_OA_METHODS), "%s: path %s" % (where, path),
              errors, allow_x=True)
        for method in _OA_METHODS:
            op = item.get(method)
            if op is None:
                continue
            mp = (method.upper(), str(path))
            registry.setdefault(mp, []).append((nid, op))
            oploc = "%s: %s %s" % (where, method.upper(), path)
            if not isinstance(op, dict):
                errors.append(oploc + ": operation is not a mapping")
                continue
            _keys(op, _OA_OP_KEYS, oploc, errors, allow_x=True)
            rb = op.get("requestBody")
            if rb is not None:
                if not isinstance(rb, dict):
                    errors.append(oploc + ": requestBody is not a mapping")
                else:
                    _keys(rb, _OA_REQBODY_KEYS, oploc + ": requestBody",
                          errors, allow_x=True)
                    for mt, mtv in (rb.get("content") or {}).items():
                        if isinstance(mtv, dict):
                            _keys(mtv, _OA_MEDIA_KEYS,
                                  "%s: requestBody content %s" % (oploc, mt),
                                  errors, allow_x=True)
            elif method.upper() in _BODIED:
                incomplete.append(
                    oploc + ": request shape not recorded (no requestBody"
                    " — the human never shaped the body)")
            responses = op.get("responses")
            if not isinstance(responses, dict) or not responses:
                errors.append(oploc + ": responses missing")
                continue
            for status, resp in responses.items():
                if not isinstance(resp, dict):
                    errors.append("%s: response %s is not a mapping"
                                  % (oploc, status))
                    continue
                _keys(resp, _OA_RESPONSE_KEYS,
                      "%s: response %s" % (oploc, status), errors,
                      allow_x=True)
                if "content" not in resp:
                    incomplete.append(
                        "%s: response %s media not recorded"
                        % (oploc, status))
                else:
                    for mt, mtv in (resp.get("content") or {}).items():
                        if isinstance(mtv, dict):
                            _keys(mtv, _OA_MEDIA_KEYS,
                                  "%s: response %s content %s"
                                  % (oploc, status, mt), errors,
                                  allow_x=True)


def _check_when_step(where: str, step: Any, registry: dict,
                     errors: list) -> Optional[tuple]:
    """One when-step against the closed schema and the declared routes."""
    if not isinstance(step, dict):
        errors.append(where + ": when-step is not a mapping")
        return None
    _keys(step, _WHEN_KEYS, where, errors)
    method = str(step.get("method") or "").upper()
    path = str(step.get("path") or "")
    if not method or not path:
        errors.append(where + ": when-step requires method and path")
        return None
    mp = (method, path)
    if mp not in registry:
        errors.append(
            "%s: route %s %s is declared by no node's openapi"
            % (where, method, path))
        return None
    return mp


def _check_scenario(nid: str, idx: int, sc: Any, registry: dict,
                    declared_env: set, errors: list,
                    incomplete: list) -> None:
    """One scenario against the tiny closed G/W/T schema (S13.4) and the
    cross rules: declared routes, declared env vars, then/openapi agreement
    (the v164 media-drift and v149 status-guess classes)."""
    where = "node %s: scenario[%d]" % (nid, idx)
    if not isinstance(sc, dict):
        errors.append(where + ": not a mapping")
        return
    _keys(sc, _SCENARIO_KEYS, where, errors)
    if not isinstance(sc.get("when"), dict) \
            or not isinstance(sc.get("then"), dict):
        errors.append(where + ": a scenario requires when and then")
        return
    given = sc.get("given")
    if given is not None:
        if not isinstance(given, dict):
            errors.append(where + ": given is not a mapping")
        else:
            _keys(given, _GIVEN_KEYS, where + ": given", errors)
            for name in (given.get("env") or {}):
                if name not in declared_env:
                    errors.append(
                        "%s: env var '%s' is declared by no node"
                        % (where, name))
            for j, step in enumerate(given.get("state") or []):
                _check_when_step("%s: given.state[%d]" % (where, j),
                                 step, registry, errors)
    mp = _check_when_step(where + ": when", sc["when"], registry, errors)
    then = sc["then"]
    _keys(then, _THEN_KEYS, where + ": then", errors)
    if "status" not in then:
        errors.append(where + ": then requires status")
    bc = then.get("body_check")
    if bc is not None:
        if not isinstance(bc, dict) or len(bc) != 1 \
                or next(iter(bc)) not in _BODY_CHECK_KINDS:
            errors.append(
                "%s: body_check must be exactly one of %s, got %r"
                % (where, "|".join(sorted(_BODY_CHECK_KINDS)),
                   sorted(bc) if isinstance(bc, dict) else bc))
    if mp is None:
        return
    # cross rules against the OWNING operation — one datum, both sides
    _owner_nid, op = registry[mp][0]
    responses = op.get("responses") if isinstance(op, dict) else None
    if not isinstance(responses, dict):
        return
    status = str(then.get("status"))
    if "status" in then and status not in responses:
        errors.append(
            "%s: then.status %s is not a declared response of %s %s "
            "(declared: %s)" % (where, status, mp[0], mp[1],
                                ", ".join(sorted(responses))))
    elif then.get("media") is not None:
        resp = responses.get(status) or {}
        content = resp.get("content") if isinstance(resp, dict) else None
        if content:
            if then["media"] not in content:
                errors.append(
                    "%s: then.media %s contradicts the contracted media %s "
                    "for %s %s (the v164 drift class)"
                    % (where, then["media"], ", ".join(sorted(content)),
                       mp[0], mp[1]))
        else:
            errors.append(
                "%s: then.media %s asserted but %s %s declares no media"
                % (where, then["media"], mp[0], mp[1]))
    if mp[0] in _BODIED and "body" not in sc["when"]:
        rb = op.get("requestBody") if isinstance(op, dict) else None
        schema = (((rb or {}).get("content") or {})
                  .get("application/json") or {}).get("schema") or {}
        if schema.get("required"):
            incomplete.append(
                "%s: %s %s request body values not recorded (shape is %s)"
                % (where, mp[0], mp[1], schema["required"]))


def validate_ir(ir: Any) -> dict:
    """Enforce the CLOSED WORLD over an IR (S13.2/S13.5).

    Why: postfactum pairwise-drift audits scale linearly with failure
    classes; a closed world refuses undeclared values once, for all of them.
    What: returns {"errors": [...], "incomplete": [...], "not_checked": [...]}
    — plain strings naming the node id and the offending value. Errors are
    violations (unknown keys, undeclared routes/symbols/env, duplicate or
    non-leaf ownership, scenario/openapi disagreement, a hollow node — S38/Q2);
    incomplete are honest gaps (datums the engine never recorded); not_checked
    (S38/Q2, catalog C4) names oracles that were SKIPPED because their optional
    library was absent — not silence (a gate refuses on it), but not a
    structural error a hermetic runner should red on.
    Test: tests/audit/test_ir_closed_world.py, test_fail_closed_absence.py."""
    errors: list = []
    incomplete: list = []
    if not isinstance(ir, dict):
        return {"errors": ["ir: not a mapping"], "incomplete": [],
                "not_checked": []}
    _keys(ir, _TOP_KEYS, "ir", errors)
    if ir.get("format") != IR_FORMAT:
        errors.append("ir: format %r is not %r" % (ir.get("format"),
                                                   IR_FORMAT))
    product = ir.get("product")
    requirements: set = set()      # H5/S13.7: declared third-party deps
    if product is not None:
        if isinstance(product, dict):
            _keys(product, _PRODUCT_KEYS, "product", errors)
            for req in (product.get("requirements") or []):
                if isinstance(req, str):
                    requirements.add(req)
                elif isinstance(req, dict):
                    _keys(req, _REQUIREMENT_KEYS, "product: requirement",
                          errors)
                    if req.get("name"):
                        requirements.add(str(req["name"]))
                    else:
                        errors.append(
                            "product: requirement has no name: %r" % req)
                else:
                    errors.append(
                        "product: requirement is neither a name nor a "
                        "{name, version}: %r" % req)
        else:
            errors.append("product: not a mapping")
    nodes = ir.get("nodes")
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict):
        errors.append("nodes: not a mapping")
        nodes = {}

    registry: dict = {}      # (METHOD, path) -> [(nid, operation)]
    declared_env: set = set()
    exposed: list = []       # (nid, file stems, exposed symbol names)
    all_consumes: list = []  # (nid, consume entry)

    for nid, node in nodes.items():
        where = "node %s" % nid
        if not isinstance(node, dict):
            errors.append(where + ": not a mapping")
            continue
        _keys(node, _NODE_KEYS, where, errors)
        # H5/S13.7: a node may only import a dependency the PRODUCT requested
        # (closed world — you cannot pull in what the spec never declared).
        for dep in (node.get("dependencies") or []):
            if str(dep) not in requirements:
                errors.append(
                    "%s: dependency %r is not in product.requirements — "
                    "declare it at the product level first" % (where, dep))
        # H6/S17.5: a declared effect class must be a known one.
        for eff in (node.get("effects") or []):
            if str(eff) not in _EFFECT_CLASSES:
                errors.append(
                    "%s: effect %r is not a known class %s"
                    % (where, eff, sorted(_EFFECT_CLASSES)))
        symbols = node.get("symbols")
        names: set = set()
        if symbols is not None:
            if not isinstance(symbols, dict):
                errors.append(where + ": symbols is not a mapping")
                symbols = {}
            _keys(symbols, _SYMBOLS_KEYS, where + ": symbols", errors)
            for ent in (symbols.get("exposes") or []):
                if isinstance(ent, dict):
                    _keys(ent, _EXPOSE_KEYS, where + ": exposes entry",
                          errors)
                    if ent.get("name"):
                        names.add(str(ent["name"]))
            for ent in (symbols.get("consumes") or []):
                if isinstance(ent, dict):
                    _keys(ent, _CONSUME_KEYS, where + ": consumes entry",
                          errors)
                    all_consumes.append((nid, ent))
        for ent in (node.get("env") or []):
            if isinstance(ent, dict):
                _keys(ent, _ENV_ENTRY_KEYS, where + ": env entry", errors)
                if ent.get("name"):
                    declared_env.add(str(ent["name"]))
        stems = {Path(str(f)).stem for f in (node.get("files") or [])}
        exposed.append((nid, stems, names))
        doc = node.get("openapi")
        if doc is not None:
            before = len(registry)
            _check_openapi(nid, doc, errors, incomplete, registry)
            if node.get("children") and len(registry) > before:
                errors.append(
                    "%s: owns routes but has children %s — only a LEAF "
                    "builds a route (S10.16)"
                    % (where, sorted(node["children"])))

    # duplicate ownership across nodes — a drifted datum, never deduplicated
    for (method, path), owners in sorted(registry.items()):
        if len(owners) > 1:
            errors.append(
                "route %s %s owned by more than one node: %s"
                % (method, path, ", ".join(sorted(n for n, _ in owners))))

    # a symbol consumed but exposed by no node (the v157 phantom import)
    for nid, ent in all_consumes:
        name = str(ent.get("name") or "")
        src = ent.get("from")
        ok = any(name in nms and (src is None or str(src) in stems)
                 for _n, stems, nms in exposed)
        if not ok:
            shown = ("%s.%s" % (src, name)) if src else name
            errors.append(
                "node %s: consumed symbol '%s' is exposed by no node "
                "(the v157 ImportError class)" % (nid, shown))

    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        # S38/Q2 (catalog C3): a valid-but-hollow executable node BLOCKS here —
        # a spec with no behaviour and no interface is un-buildable, and the
        # closed-world oracle must refuse it, not pass it as format-clean.
        hollow = _hollow_node_reason(nid, node)
        if hollow:
            errors.append(hollow)
        for idx, sc in enumerate(node.get("scenarios") or []):
            _check_scenario(nid, idx, sc, registry, declared_env,
                            errors, incomplete)

    # S31/N1: the GRAMMAR of the Given/When/Then scenarios is validated by the
    # ready-made gherkin-official parser library, not the hand key-check above.
    # The library AST catches the grammatical class (an injected keyword line,
    # a step that is not a step) the closed-schema check is structurally blind
    # to. S38/Q2 (catalog C4): a NOT-CHECKED result (the optional library was
    # absent, so the oracle was SKIPPED) is kept OUT of `errors` and surfaced in
    # a separate `not_checked` channel — it is not silence (a completeness gate
    # reads it and refuses), but it is not a structural error either, so a
    # hermetic scenario runner that legitimately lacks the dev oracle is not
    # falsely refused. Real grammatical errors still ride into `errors`.
    not_checked_out: list = []
    for _e in gherkin_errors(ir):
        (not_checked_out if is_not_checked(_e) else errors).append(_e)

    return {"errors": errors, "incomplete": incomplete,
            "not_checked": not_checked_out}


def _req_name(req: Any) -> str:
    """Canonical string label of a product requirement (a plain string, or an
    object carrying its ``name``)."""
    if isinstance(req, dict):
        return str(req.get("name", ""))
    return str(req)


def contract_closure_gaps(ir: Any, constitution: Any = None) -> list:
    """S39 (catalog B, root of the v167 red): ONE early graph-closure check —
    every contracted obligation must have an OWNER NODE in the realized IR.

    Why: connectivity was checked in three scattered places (decomposer
    policy, _accept_decomposer_ir, _plan_ownership_report) and NONE closed the
    graph at decomposition-close. So a plan with a pinned file, product entry,
    contracted route, consumed symbol, or human requirement that NO node owns
    passed decomposition SILENTLY and only failed late at integrate_verify
    (v167: constitution pinned ``src/db.py`` had no owner node, the run went
    green through decomposition and blew up on the final tree). The v157
    "consumed symbol exposed by no node" class is INTENTIONALLY filtered
    mid-growth in _accept_decomposer_ir (a symbol may be consumed from a node
    not proposed yet — legitimately pending). This function runs AFTER growth,
    over the whole merged IR, where "pending" is no longer an excuse: an
    unowned obligation is a NAMED refusal here (a milestone sibling of the
    decomposer_* gates), not a late integrate surprise.

    What: pure function over the machine IR (never prose — Charter P5).
    Returns a list of plain-string owner-gaps (JSON-safe). Empty list = the
    contract graph is closed. Five owner invariants:

      1. pinned    — every ``product.pinned_files`` entry appears in some
                     ``nodes[*].files`` (entry is excluded upstream: entry
                     synthesis owns the entry module, not a leaf).
      2. entry     — ``product.entry``, when the key is present, is a non-empty
                     module (an empty entry declares an owner that does not
                     exist).
      3. route     — every route declared in a node's OpenAPI document binds a
                     non-empty handler symbol (``x-spec-flow-handler``); an
                     empty binding is an orphan route (contract without a
                     synthesised handler — catalog B2).
      4. symbol    — every consumed symbol (``nodes[*].symbols.consumes``) is
                     exposed by some node (``nodes[*].symbols.exposes``); a
                     phantom consume is the v157 ImportError class, refused
                     early instead of at import time.
      5. requirement — every ``product.requirements`` entry is claimed by at
                     least one node (listed in some node's ``dependencies``);
                     an unclaimed requirement is untraceable (catalog B4).

    ``constitution`` is accepted for symmetry with the pinned-path extractor
    and to let callers attribute a pinned gap to a rule; the closure itself
    reads only the IR (pinned files are already lifted into the IR).
    Test: tests/audit/test_contract_closure_gaps.py."""
    gaps: list = []
    if not isinstance(ir, dict):
        return ["ir is not a mapping — nothing to close over"]

    product = ir.get("product") if isinstance(ir.get("product"), dict) else {}
    nodes = ir.get("nodes") if isinstance(ir.get("nodes"), dict) else {}

    # Files owned across the realized plan, and the exposed-symbol universe.
    owned_files: set = set()
    exposed_names: set = set()
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for f in (node.get("files") or []):
            owned_files.add(str(f))
        syms = node.get("symbols")
        if isinstance(syms, dict):
            for ent in (syms.get("exposes") or []):
                if isinstance(ent, dict) and ent.get("name") is not None:
                    exposed_names.add(str(ent["name"]))

    # 1. pinned files — every constitution-pinned module has an owner node.
    for pf in (product.get("pinned_files") or []):
        if str(pf) not in owned_files:
            gaps.append(
                "pinned file %s has no owner node — no node builds it "
                "(the v167 class: a constitution-pinned module the realized "
                "plan never gave a leaf)" % pf)

    # 2. product entry — a declared entry names an owner that must exist.
    if "entry" in product and not str(product.get("entry") or "").strip():
        gaps.append(
            "product entry is declared but empty — the entry module has no "
            "owner")

    # 3 & 4. per-node routes and consumed symbols.
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        openapi = node.get("openapi")
        if isinstance(openapi, dict):
            paths = openapi.get("paths")
            if isinstance(paths, dict):
                for path, methods in paths.items():
                    if not isinstance(methods, dict):
                        continue
                    for method, op in methods.items():
                        handler = ""
                        if isinstance(op, dict):
                            handler = str(
                                op.get("x-spec-flow-handler") or "").strip()
                        if not handler:
                            gaps.append(
                                "route %s %s (node %s) has no handler symbol — "
                                "the contract declares it but no handler was "
                                "synthesised (orphan route)"
                                % (str(method).upper(), path, nid))
        syms = node.get("symbols")
        if isinstance(syms, dict):
            for ent in (syms.get("consumes") or []):
                if not isinstance(ent, dict):
                    continue
                name = ent.get("name")
                if name is None:
                    continue
                if str(name) not in exposed_names:
                    origin = ent.get("from")
                    shown = ("%s.%s" % (origin, name)) if origin else str(name)
                    gaps.append(
                        "consumed symbol '%s' (node %s) is exposed by no node "
                        "— the v157 ImportError class, refused at "
                        "decomposition close instead of at import time"
                        % (shown, nid))

    # 5. human requirements — every requirement is claimed by some node.
    claimed: set = set()
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for dep in (node.get("dependencies") or []):
            claimed.add(str(dep))
    for req in (product.get("requirements") or []):
        rn = _req_name(req)
        if rn and rn not in claimed:
            gaps.append(
                "requirement %r is claimed by no node — no leaf traces to it "
                "(untraceable human requirement)" % rn)

    return gaps
