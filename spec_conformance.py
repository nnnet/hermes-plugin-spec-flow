"""spec-flow conformance test compiler (node B3, Stage 18).

Why (plan 2026-07-04T00-45): interface-level tests written by an LLM tester
are re-guesses over facts the IR already carries — v149 ("the tester guessed
a status") and v150 ("success asserted on a foreign route", smeared
``assertIn(code, (200, 201))``) are one class: tests inventing interface
facts. Compiling the tests FROM the node's IR entry kills the class by
construction: the compiler has no input other than the IR, so an undeclared
status or a foreign route simply has no code path into the output.

What: ``compile_leaf_tests(ir, node_id) -> str`` returns the full content of
``tests/test_<module>.py`` for one leaf node:

  * one test per contracted (route, status, media): the request is built
    from the openapi required fields valued by the node's scenario bodies
    (``when.body`` / ``given.state[].body`` — the only recorded values);
    the status assert is EXACT (``== 201``); the media assert checks the
    body shape the engine's router serializes for the contracted media;
  * one test per IR scenario: given.env overlay, given.state replay
    (every state step must be served, status < 400), when performed, then
    judged (status, media, body_check equals|contains|json_subset) — the
    leaf-level mirror of spec_scenarios;
  * a route or scenario lacking an IR datum (unvalued required body, no
    recorded request shape, a non-success status no scenario yields, a step
    on a route owned by another node) compiles to a VISIBLE
    ``pytest.mark.skip`` whose reason names the gap — never an invented
    value, never a silent drop (S13.1 discipline carried downstream).

The compiler REFUSES foreign data (S18.2): an IR failing
``spec_ir.validate_ir`` raises ``ValueError`` naming the offending value
before any content exists. Mid-growth "consumed but not yet exposed"
findings are non-fatal (the S15.3 seam rule — the full-tree closed world
still owns phantoms). Stdlib only; deterministic: same IR -> same string.

Test: tests/audit/test_ir_compiled_tests.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

GAP = "gap: "
_BODIED = ("POST", "PUT", "PATCH")
# the S15.3 seam rule: symbol growth findings are not compile stoppers
_MIDGROWTH = "is exposed by no node"
# body-shape asserts the engine's router serialization implies per media
_MEDIA_SHAPES = {
    "application/json": (
        "assert isinstance(body, (dict, list)), (\n"
        '        "contracted media application/json requires a '
        'JSON-shaped body")'),
    "text/html": (
        "assert isinstance(body, str) and body.lstrip().startswith(\"<\"), "
        "(\n        \"contracted media text/html requires an HTML string "
        "body\")"),
}


def _handler_symbol(method: str, path: str) -> str:
    """The engine-declared handler name for a route — mirrors the runner's
    _canonical_handler_symbol (GET /ui -> get_ui, GET / -> get_root,
    template segments dropped); a fragment's x-spec-flow-handler wins."""
    segs = [s for s in str(path or "").split("/")
            if s and not (s.startswith("{") and s.endswith("}"))]
    base = re.sub(r"\W", "_", "_".join(segs)).lower()
    base = re.sub(r"_+", "_", base).strip("_") or "root"
    return "%s_%s" % ((method or "GET").strip().lower() or "get", base)


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return out or "root"


def _literal(value: Any) -> str:
    """ASCII Python literal for an IR datum (JSON-safe by construction)."""
    return ascii(value)


def _module_stem(node: dict, node_id: str) -> str:
    """The import target from the node's files datum — never guessed."""
    for f in (node.get("files") or []):
        p = str(f).replace("\\", "/")
        if p.startswith("src/") and p.endswith(".py"):
            stem = Path(p).stem
            if stem.isidentifier() and stem.isascii():
                return stem
    raise ValueError(
        "node %s: no src/<module>.py in the IR files datum — the compiled "
        "tests would have no import surface" % node_id)


def _required_fields(op: Any) -> Optional[list]:
    """Required request fields, [] for a recorded empty shape, None when the
    request shape was never recorded (an honest gap, not an empty shape)."""
    if not isinstance(op, dict):
        return None
    rb = op.get("requestBody")
    if not isinstance(rb, dict):
        return None
    schema = ((rb.get("content") or {}).get("application/json")
              or {}).get("schema") or {}
    return [str(f) for f in (schema.get("required") or [])]


def _single_media(op: dict, status: str) -> Optional[str]:
    """The one contracted response media for a status, None when absent."""
    resp = (op.get("responses") or {}).get(status)
    content = resp.get("content") if isinstance(resp, dict) else None
    if isinstance(content, dict) and len(content) == 1:
        return next(iter(content))
    return None


def _const_body(op: dict, status: str) -> Any:
    """The contracted fixed response body (JSON Schema const), or None."""
    resp = (op.get("responses") or {}).get(status)
    content = resp.get("content") if isinstance(resp, dict) else {}
    for mtv in (content or {}).values():
        if isinstance(mtv, dict):
            schema = mtv.get("schema") or {}
            if isinstance(schema, dict) and "const" in schema:
                return schema["const"]
    return None


# JSON Schema type -> Python isinstance target, resolved ONCE (H3/S21). An
# unknown type is absent from the map and skipped — never guessed.
_JSON_PY = {"string": "str", "integer": "int", "number": "(int, float)",
            "boolean": "bool", "object": "dict", "array": "list"}


def _response_shape(op: dict, status: str) -> Optional[dict]:
    """The CLOSED object response schema for a success status, or None.

    Why: an empty ``{}`` schema pinned nothing (F1) — a weak model could
    invent response fields and stay green. A schema is a real shape only when
    the IR declares one of properties/required/additionalProperties:false;
    a bare ``{}`` (honest gap) and a ``const`` (handled separately) both
    return None so the historical isinstance/exact-const paths stay untouched.
    Test: tests/audit/test_closed_response_schemas.py."""
    resp = (op.get("responses") or {}).get(status)
    content = resp.get("content") if isinstance(resp, dict) else {}
    jm = (content or {}).get("application/json")
    schema = jm.get("schema") if isinstance(jm, dict) else None
    if not isinstance(schema, dict) or "const" in schema:
        return None
    has_shape = (schema.get("properties") or schema.get("required")
                 or schema.get("additionalProperties") is False)
    if not has_shape:
        return None
    return schema


def _shape_body_asserts(shape: dict, label: str) -> list:
    """Compile the pin for a closed object response schema (S21.1): required
    fields present, declared field types hold, and — under
    additionalProperties:false — no field outside the declared union."""
    props = shape.get("properties") or {}
    required = [str(f) for f in (shape.get("required") or [])]
    lines = ["    assert isinstance(body, dict), (",
             '        "%s: a shaped response must be a JSON object")' % label]
    for f in required:
        lines.append("    assert %s in body, (" % _literal(f))
        lines.append('        "%s: contracted response field %s missing")'
                     % (label, f))
    for f in sorted(props):
        sub = props[f] if isinstance(props[f], dict) else {}
        pytype = _JSON_PY.get(str(sub.get("type") or ""))
        if pytype:
            lines.append("    assert %s not in body or isinstance("
                         "body[%s], %s), (" % (_literal(f), _literal(f),
                                               pytype))
            lines.append('        "%s: response field %s has the wrong type")'
                         % (label, f))
    if shape.get("additionalProperties") is False:
        allowed = sorted(set(required) | set(props))
        lines.append("    assert set(body) <= set(%s), (" % _literal(allowed))
        lines.append('        "%s: response carries a field the contract '
                     'never declared")' % label)
    return lines


def _iter_steps(sc: dict):
    """Every when-shaped step of one scenario, given.state first."""
    given = sc.get("given") if isinstance(sc.get("given"), dict) else {}
    for step in (given.get("state") or []):
        if isinstance(step, dict):
            yield step
    if isinstance(sc.get("when"), dict):
        yield sc["when"]


def _scenario_values(scenarios: list, method: str, path: str) -> dict:
    """The FIRST recorded body datum for a route across the node's
    scenarios — the only place request values exist as data (S15.5)."""
    for sc in scenarios:
        for step in _iter_steps(sc):
            if (str(step.get("method") or "").upper() == method
                    and str(step.get("path") or "") == path
                    and isinstance(step.get("body"), dict)):
                return step["body"]
    return {}


def _skip_test(name: str, reason: str, doc: str) -> str:
    lines = ["@pytest.mark.skip(reason=%s)" % _literal(reason),
             "def %s():" % name,
             '    """%s"""' % doc]
    return "\n".join(lines) + "\n"


def _shape_assert(media: Optional[str]) -> list:
    shape = _MEDIA_SHAPES.get(media or "")
    return ["    " + shape] if shape else []


class _Emitter:
    """Accumulates test functions and the helper/import surface they need."""

    def __init__(self) -> None:
        self.tests: list = []
        self.handlers: set = set()
        self.needs: set = set()

    def skip(self, name: str, reason: str, doc: str) -> None:
        self.needs.add("pytest")
        self.tests.append(_skip_test(name, GAP + reason, doc))

    def live(self, name: str, doc: str, body_lines: list,
             handlers: set) -> None:
        self.needs.add("invoke")
        self.handlers.update(handlers)
        out = ["def %s():" % name, '    """%s"""' % doc] + body_lines
        self.tests.append("\n".join(out) + "\n")


def _emit_route_tests(em: _Emitter, method: str, path: str, op: dict,
                      scenarios: list) -> None:
    """One test per contracted (route, status, media) — S18.1/S18.3/S18.4."""
    responses = op.get("responses") if isinstance(op, dict) else {}
    statuses = sorted(responses or {})
    success = [s for s in statuses
               if s.isdigit() and 200 <= int(s) < 300]
    handler = str(op.get("x-spec-flow-handler")
                  or _handler_symbol(method, path))
    for status in statuses:
        name = "test_%s_%s_status_%s" % (method.lower(), _slug(path),
                                         _slug(status))
        media = _single_media(op, status)
        label = "%s %s" % (method, path)
        if status not in success:
            covered = any(
                str(sc.get("when", {}).get("method") or "").upper() == method
                and str(sc.get("when", {}).get("path") or "") == path
                and str((sc.get("then") or {}).get("status")) == status
                for sc in scenarios)
            if covered:
                continue    # the scenario-derived test asserts it exactly
            em.skip(name,
                    "no IR scenario yields status %s for %s — the datum "
                    "that would trigger it was never recorded" % (status,
                                                                  label),
                    "%s status %s — skipped: honest gap, nothing invented."
                    % (label, status))
            continue
        if len(success) > 1:
            em.skip(name,
                    "ambiguous success statuses %s declared for %s — one "
                    "contracted request cannot assert both"
                    % (", ".join(success), label),
                    "%s — skipped: the IR datum is ambiguous." % label)
            continue
        required = _required_fields(op)
        if method in _BODIED:
            if required is None:
                em.skip(name,
                        "request shape not recorded for %s — the human "
                        "never shaped the body" % label,
                        "%s — skipped: honest gap, nothing invented."
                        % label)
                continue
            values = _scenario_values(scenarios, method, path)
            missing = [f for f in required if f not in values]
            if missing:
                em.skip(name,
                        "request body values not recorded for %s "
                        "(required: %s) — no IR scenario carries them"
                        % (label, ", ".join(missing)),
                        "%s — skipped: honest gap, nothing invented."
                        % label)
                continue
            payload = _literal({f: values[f] for f in required})
        else:
            payload = "None"
        lines = ["    status, body = _invoke(%s, %s, %s, %s, {})"
                 % (handler, _literal(method), _literal(path), payload),
                 "    assert status == %d" % int(status)]
        lines += _shape_assert(media)
        const = _const_body(op, status)
        if const is not None:
            lines.append("    assert body == %s, (" % _literal(const))
            lines.append('        "the ONE contracted fixed body for %s")'
                         % label)
        else:
            # H3/S21: a CLOSED object response schema is pinned (required
            # present, types hold, no invented field); a bare {} stays the
            # isinstance-only honest gap above — nothing is invented.
            shape = _response_shape(op, status)
            if shape is not None:
                lines += _shape_body_asserts(shape, label)
        em.live(name, "%s answers the contracted status %s (%s)."
                % (label, status, media or "media not recorded in the IR"),
                lines, {handler})


def _emit_scenario_test(em: _Emitter, idx: int, sc: dict, own: dict,
                        registry: dict) -> None:
    """One test per IR scenario — the leaf-level mirror of spec_scenarios."""
    req = str(sc.get("requirement") or "scenario")
    name = "test_scenario_%d_%s" % (idx, _slug(req))
    doc = "IR scenario '%s': given replayed, when performed, then judged." \
          % req
    steps = list(_iter_steps(sc))
    for step in steps:
        method = str(step.get("method") or "").upper()
        path = str(step.get("path") or "")
        label = "%s %s" % (method, path)
        if (method, path) not in own:
            em.skip(name,
                    "scenario '%s' steps onto %s, a route owned by another "
                    "node — replayed only by the product-level scenario "
                    "runner" % (req, label), doc)
            return
        if method in _BODIED and "body" not in step:
            required = _required_fields(registry.get((method, path))) or []
            if required:
                em.skip(name,
                        "request body values not recorded for %s "
                        "(required: %s)" % (label, ", ".join(required)),
                        doc)
                return
    overlay = {}
    given = sc.get("given") if isinstance(sc.get("given"), dict) else {}
    for k, v in (given.get("env") or {}).items():
        overlay[str(k)] = str(v)
    handlers: set = set()
    body_lines: list = []
    indent = "    "
    if overlay:
        em.needs.add("os")
        body_lines += [
            "    _overlay = %s   # given.env — literal IR values"
            % _literal(overlay),
            "    _saved = {_k: os.environ.get(_k) for _k in _overlay}",
            "    os.environ.update(_overlay)",
            "    try:"]
        indent = "        "
    state = list(given.get("state") or [])
    for step in steps:
        method = str(step.get("method") or "").upper()
        path = str(step.get("path") or "")
        op = own[(method, path)]
        handler = str(op.get("x-spec-flow-handler")
                      or _handler_symbol(method, path))
        handlers.add(handler)
        payload = (_literal(step["body"]) if isinstance(step.get("body"),
                                                        dict) else "None")
        body_lines.append(
            "%sstatus, body = _invoke(%s, %s, %s, %s, {})"
            % (indent, handler, _literal(method), _literal(path), payload))
        if step in state:
            body_lines.append(
                '%sassert status is not None and int(status) < 400, (\n'
                '%s    "given.state step %s must be served")'
                % (indent, indent, "%s %s" % (method, path)))
            continue
        then = sc.get("then") or {}
        body_lines.append("%sassert status == %d"
                          % (indent, int(str(then.get("status")))))
        media = then.get("media")
        shape = _MEDIA_SHAPES.get(str(media or ""))
        if shape:
            body_lines.append(indent + shape.replace("\n        ",
                                                     "\n" + indent + "    "))
        bc = then.get("body_check")
        if isinstance(bc, dict) and len(bc) == 1:
            kind, expected = next(iter(bc.items()))
            if kind == "contains":
                em.needs.add("text")
                body_lines.append("%sassert %s in _text(body)"
                                  % (indent, _literal(str(expected))))
            elif kind == "equals":
                if isinstance(expected, str):
                    em.needs.add("text")
                    body_lines.append(
                        "%sassert _text(body).strip() == %s.strip()"
                        % (indent, _literal(expected)))
                else:
                    body_lines.append("%sassert body == %s"
                                      % (indent, _literal(expected)))
            elif kind == "json_subset":
                em.needs.add("subset")
                body_lines.append("%sassert _json_subset(%s, body)"
                                  % (indent, _literal(expected)))
    if overlay:
        body_lines += [
            "    finally:",
            "        for _k, _v in _saved.items():",
            "            if _v is None:",
            "                os.environ.pop(_k, None)",
            "            else:",
            "                os.environ[_k] = _v"]
    em.live(name, doc, body_lines, handlers)


_HELPER_INVOKE = '''\
def _invoke(handler, method, path, payload, query):
    """Call a leaf handler by its positional arity, the way the engine's
    synthesized router does; method and path name the contracted route."""
    n = 0
    for p in inspect.signature(handler).parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
        elif p.kind is p.VAR_POSITIONAL:
            n = 2
    return handler(*(payload, query)[:min(n, 2)])
'''

_HELPER_TEXT = '''\
def _text(body):
    """Serialized view of a handler body for the body_check judgements."""
    return body if isinstance(body, str) else json.dumps(body)
'''

_HELPER_SUBSET = '''\
def _json_subset(expected, got):
    """Expected is a structural subset of got (dict keys recursive, list
    prefix, equality otherwise) — mirrors the product scenario runner."""
    if isinstance(expected, dict):
        return isinstance(got, dict) and all(
            k in got and _json_subset(v, got[k])
            for k, v in expected.items())
    if isinstance(expected, list):
        return (isinstance(got, list) and len(expected) <= len(got)
                and all(_json_subset(e, g)
                        for e, g in zip(expected, got)))
    return expected == got
'''

# the S14.4 rule-token pattern — generic filesystem flavour, never a
# whitelist of product names
_ENV_PREAMBLE = '''\
# Engine-owned deterministic env values (S14.4): each value derives from
# the IR env entry's RULE — a filesystem-flavoured rule gets a fresh temp
# path; values already present in the environment are honoured.
_ENV_RULES = %(rules)s
_FS_RULE = re.compile(
    r"\\b(path|file|files|db|database|dir|directory|folder|storage|store|"
    r"stored|persist|persistence|sqlite|disk|location)\\w*", re.I)
for _name in sorted(_ENV_RULES):
    if _name not in os.environ:
        _probe = "%%s %%s" %% (_ENV_RULES[_name],
                               _name.replace("_", " "))
        if _FS_RULE.search(_probe):
            os.environ[_name] = os.path.join(
                tempfile.mkdtemp(prefix="spec-flow-ir-"), _name.lower())
        else:
            os.environ[_name] = "spec-flow-%%s" %% _name.lower()
'''


def _env_rules(ir: dict) -> dict:
    """{env var: rule} across ALL nodes — declared datums the module (or a
    sibling it imports) may read; the values are derived, never invented."""
    rules: dict = {}
    for _nid, node in sorted((ir.get("nodes") or {}).items()):
        if isinstance(node, dict):
            for ent in (node.get("env") or []):
                if isinstance(ent, dict) and ent.get("name"):
                    rules.setdefault(str(ent["name"]),
                                     str(ent.get("rule") or ""))
    return rules


def compile_leaf_tests(ir: Any, node_id: str) -> str:
    """Deterministic pytest file for one leaf, derived ONLY from its IR entry.

    Why: the LLM tester re-guessed interface facts (v149/v150); the compiler
    can only emit what the IR declares, so those classes die by construction.
    What: refuses an invalid IR or unknown node (ValueError naming the
    offence), then emits one test per contracted (route, status, media) plus
    one per scenario; missing datums become named pytest skips.
    Test: tests/audit/test_ir_compiled_tests.py.
    """
    try:
        import spec_ir
    except ImportError:     # flat layout: repo root on sys.path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import spec_ir
    if not isinstance(ir, dict):
        raise ValueError("ir: not a mapping — nothing to compile")
    verdict = spec_ir.validate_ir(ir)
    fatal = [e for e in verdict.get("errors") or [] if _MIDGROWTH not in e]
    if fatal:
        raise ValueError("ir refused: " + "; ".join(fatal))
    node = (ir.get("nodes") or {}).get(node_id)
    if not isinstance(node, dict):
        raise ValueError("node %s: absent from the IR — a compiled file "
                         "would be pure invention" % node_id)
    if node.get("children"):
        raise ValueError("node %s: has children — only a LEAF gets compiled "
                         "interface tests (S10.16)" % node_id)
    paths = ((node.get("openapi") or {}).get("paths")) or {}
    if not paths:
        raise ValueError("node %s: declares no openapi routes — no "
                         "interface to conform to" % node_id)
    stem = _module_stem(node, node_id)

    own: dict = {}          # (METHOD, path) -> operation of THIS node
    for path, item in sorted(paths.items()):
        if isinstance(item, dict):
            for method, op in sorted(item.items()):
                if isinstance(op, dict):
                    own[(str(method).upper(), str(path))] = op

    scenarios = [sc for sc in (node.get("scenarios") or [])
                 if isinstance(sc, dict)]
    em = _Emitter()
    for (method, path), op in sorted(own.items()):
        _emit_route_tests(em, method, path, op, scenarios)
    for idx, sc in enumerate(scenarios, 1):
        _emit_scenario_test(em, idx, sc, own, own)

    env_rules = _env_rules(ir)
    out = [
        "# code: src/%s.py" % stem,
        "# test: tests/test_%s.py" % stem,
        '"""Interface conformance tests for node \'%s\' — compiled from '
        "the IR.\n" % node_id,
        "Generated by the spec-flow engine "
        "(spec_conformance.compile_leaf_tests).",
        "Every route, status, media and request value below traces to an IR",
        "datum; nothing here is invented. Do not hand-edit: the engine",
        "re-asserts this file over any worker rewrite — the interface truth",
        'lives in the IR.\n"""',
    ]
    std = []
    if "invoke" in em.needs:
        std.append("import inspect")
    if "text" in em.needs:
        std.append("import json")
    if em.needs & {"os"} or env_rules:
        std.append("import os")
    if env_rules:
        std += ["import re", "import tempfile"]
    if std:
        out += [""] + std
    if "pytest" in em.needs:
        out += ["", "import pytest"]
    if env_rules:
        out += ["", _ENV_PREAMBLE % {"rules": _literal(env_rules)}]
    if em.handlers:
        out += ["", "from %s import %s"
                % (stem, ", ".join(sorted(em.handlers)))]
    blocks = []
    if "invoke" in em.needs:
        blocks.append(_HELPER_INVOKE)
    if "text" in em.needs:
        blocks.append(_HELPER_TEXT)
    if "subset" in em.needs:
        blocks.append(_HELPER_SUBSET)
    blocks += em.tests
    text = "\n".join(out) + "\n"
    for block in blocks:
        text += "\n\n" + block
    return text
