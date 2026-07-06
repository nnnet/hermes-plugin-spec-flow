"""spec-flow scenario runner — THE interface oracle (node B1, Stage 14).

Why (plan 2026-07-04T00-45): Phase A made the G-W-T scenarios first-class
DATA in the IR; this module makes them the JUDGEMENT. Static IR consistency
plus a green unit suite is still not evidence that the RUNNING product
honours the interface (v164: contracts said text/html the whole time while
the live /about answered JSON). The runner executes every IR scenario
end-to-end through the WSGI surface as a black box and reds the build on
any violation.

What:
  * ``run_scenarios(ir, wsgi_app=None, entry_path=None, workdir=None)`` —
    validate the IR first (an invalid IR is REFUSED, never executed — the
    v149 status guess is a validation error, S14.2); then for every
    scenario: apply given.env (see EnvValueFactory), replay given.state
    when-steps, perform when, judge then (status, media, body_check
    equals|contains|json_subset). Returns::

        {"ok": bool, "refused": [str], "passed": int,
         "failures":   [{requirement, node, step, expected, got}],
         "incomplete": [{requirement, node, step, missing}],
         "notes":      [str]}

    A CLOSED response schema (S21 shape) is validated against the LIVE body
    with the third-party openapi-schema-validator (S14.7), in addition to the
    hand body_check; ``notes`` records the one case where that library is
    absent and the runner fell back to the hand check alone.

    Failures are attributable plain JSON-safe strings (P4). A when-step
    whose body is ABSENT while the route's requestBody has required fields
    is an incompleteness finding and the scenario is skipped — the runner
    NEVER invents a value (S14.3).
  * ``EnvValueFactory(workdir)`` — engine-owned deterministic env values
    (closes Phase A open question #3): a var whose RULE mentions a
    path/file/db gets a fresh temp path under the run workspace; same name
    -> same value within one run; behaviour derives from the IR env entry's
    rule text, never from product-specific name literals (S14.4).
  * CLI: ``python3 -I spec_scenarios.py <ir.json> <workspace_root>`` —
    loads the product entry declared by ir.product and prints one
    ``SCENARIO_RESULT {json}`` line; the engine drives this in a hermetic
    subprocess.

Test: tests/audit/test_scenario_runner_oracle.py,
tests/audit/test_scenario_engine_wiring.py.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

RESULT_MARK = "SCENARIO_RESULT "
_BODIED = ("POST", "PUT", "PATCH")
_DEFAULT_CALLABLES = ("wsgi_app", "application", "app")
# generic filesystem tokens — matched against the env entry's RULE (and the
# name split on separators), never a whitelist of product names (S14.4)
_FS_TOKEN = re.compile(
    r"\b(path|file|files|db|database|dir|directory|folder|storage|store|"
    r"stored|persist|persistence|sqlite|disk|location)\w*", re.I)


class EnvValueFactory:
    """Deterministic engine-owned env values for one run (S14.4).

    Why: scenarios must run without a human and without guessing — the ONE
    open question of Phase A was where env VALUES come from.
    What: value(name, rule) memoizes per name; a filesystem-flavoured rule
    yields a fresh temp path under the run workspace, anything else a plain
    deterministic token derived from the name.
    Test: env-factory cases in test_scenario_runner_oracle.py.
    """

    def __init__(self, workdir: str) -> None:
        self._workdir = Path(workdir)
        self._values: dict = {}
        self._run_root: Optional[Path] = None

    def _fresh_root(self) -> Path:
        if self._run_root is None:
            base = self._workdir / "_scenario_env"
            base.mkdir(parents=True, exist_ok=True)
            self._run_root = Path(tempfile.mkdtemp(prefix="run-",
                                                   dir=str(base)))
        return self._run_root

    def value(self, name: str, rule: str = "") -> str:
        if name in self._values:
            return self._values[name]
        probe = "%s %s" % (rule or "", str(name).replace("_", " "))
        if _FS_TOKEN.search(probe):
            val = str(self._fresh_root() / str(name).lower())
        else:
            val = "spec-flow-%s" % str(name).lower()
        self._values[name] = val
        return val


# --------------------------------------------------------------------------
# WSGI plumbing
# --------------------------------------------------------------------------

def _load_wsgi_app(entry_path: str, callables: Any) -> Any:
    """Import the product entry from its file and return the WSGI callable."""
    import importlib.util
    p = Path(entry_path).resolve()
    if not p.is_file():
        raise RuntimeError("entry %s is not built" % entry_path)
    if str(p.parent) not in sys.path:
        sys.path.insert(0, str(p.parent))   # sibling module imports
    spec = importlib.util.spec_from_file_location(p.stem, str(p))
    if spec is None or spec.loader is None:
        raise RuntimeError("entry %s is not importable" % entry_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[p.stem] = mod
    spec.loader.exec_module(mod)
    names = ([callables] if isinstance(callables, str)
             else list(callables or [])) or list(_DEFAULT_CALLABLES)
    for name in names:
        cand = getattr(mod, name, None)
        if callable(cand):
            return cand
    raise RuntimeError("entry %s exposes none of %s"
                       % (entry_path, ", ".join(names)))


def _call(app: Any, method: str, path: str, body: Any) -> tuple:
    """One black-box WSGI request; returns (status int, media, text)."""
    raw = b"" if body is None else json.dumps(body).encode()
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(raw)),
        "SERVER_NAME": "scenario", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(raw), "wsgi.errors": sys.stderr,
        "wsgi.multithread": False, "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if body is not None:
        environ["CONTENT_TYPE"] = "application/json"
    cap: dict = {}

    def start_response(status, headers, exc_info=None):
        cap["status"] = int(str(status).split()[0])
        cap["headers"] = {str(k).lower(): str(v) for k, v in headers}
        return lambda _data: None

    chunks = app(environ, start_response)
    try:
        data = b"".join(bytes(c) for c in chunks)
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
    media = (cap.get("headers") or {}).get("content-type", "")
    media = media.split(";")[0].strip()
    return cap.get("status"), media, data.decode("utf-8", "replace")


def _json_subset(expected: Any, got: Any) -> bool:
    """Expected is a structural subset of got: dicts by key (recursive),
    lists as a matching prefix, everything else by equality."""
    if isinstance(expected, dict):
        return (isinstance(got, dict)
                and all(k in got and _json_subset(v, got[k])
                        for k, v in expected.items()))
    if isinstance(expected, list):
        return (isinstance(got, list) and len(expected) <= len(got)
                and all(_json_subset(e, g)
                        for e, g in zip(expected, got)))
    return expected == got


def _show(value: Any, cap: int = 300) -> str:
    """Plain JSON-safe string for expected/got fields (P4)."""
    try:
        text = value if isinstance(value, str) else json.dumps(value)
    except Exception:  # noqa: BLE001 — display only
        text = repr(value)
    return text if len(text) <= cap else text[:cap] + "..."


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------

def _registry(ir: dict) -> dict:
    """(METHOD, path) -> operation mapping from every node's openapi."""
    reg: dict = {}
    for node in (ir.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        doc = node.get("openapi") or {}
        for path, item in (doc.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if isinstance(op, dict):
                    reg[(str(method).upper(), str(path))] = op
    return reg


def _required_fields(op: Any) -> list:
    """Required request-body fields declared by an operation, [] if none."""
    if not isinstance(op, dict):
        return []
    schema = (((op.get("requestBody") or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    return [str(f) for f in (schema.get("required") or [])]


def _unvalued_step(step: dict, registry: dict) -> Optional[list]:
    """Required fields a bodyless when-step leaves unvalued, None if fine."""
    method = str(step.get("method") or "").upper()
    if method not in _BODIED or "body" in step:
        return None
    required = _required_fields(
        registry.get((method, str(step.get("path") or ""))))
    return required or None


def _response_schema(op: Any, status: Any) -> Optional[dict]:
    """The CLOSED JSON response schema an operation contracts for a status,
    or None when the contract is an honest gap.

    Why (nodes K1b+L1, S14.7): the runner validates the LIVE body against a
    schema only when that schema is a REAL closed shape — the S21 gate. A bare
    ``{}`` (honest gap) and a ``const`` (an exact value handled by the
    equals/json_subset hand path) are deliberately NOT closed, so they keep
    the historical behaviour and never produce a library assertion. This
    mirrors the single source of the closed-schema decision,
    ``spec_conformance._response_shape``, so the runtime oracle and the
    compile-time S21 pin agree on what "closed" means.
    What: reads ``responses[str(status)].content['application/json'].schema``
    and returns it iff it declares properties / required /
    additionalProperties:false and carries no ``const``; else None.
    Test: tests/audit/test_scenario_body_oracle.py — a closed schema drives
    library validation; a bare-{} schema leaves the hand check untouched."""
    if not isinstance(op, dict):
        return None
    resp = (op.get("responses") or {}).get(str(status))
    content = resp.get("content") if isinstance(resp, dict) else {}
    jm = (content or {}).get("application/json")
    schema = jm.get("schema") if isinstance(jm, dict) else None
    if not isinstance(schema, dict) or "const" in schema:
        return None
    has_shape = (schema.get("properties") or schema.get("required")
                 or schema.get("additionalProperties") is False)
    return schema if has_shape else None


def _any_closed_schema(ir: dict) -> bool:
    """True when any node's OpenAPI contracts a CLOSED response schema.

    Why (S14.7 honesty): only worth noting the library's absence if a closed
    schema actually exists to validate — otherwise the hand check was always
    the whole story and there is nothing degraded to report.
    What: scans every operation's success responses via ``_response_schema``.
    Test: tests/audit/test_scenario_body_oracle.py drives the present-lib
    path; the note path is a pure projection of this predicate."""
    for node in (ir.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        for item in ((node.get("openapi") or {}).get("paths") or {}).values():
            if not isinstance(item, dict):
                continue
            for op in item.values():
                responses = op.get("responses") if isinstance(op, dict) else {}
                for status in (responses or {}):
                    if _response_schema(op, status) is not None:
                        return True
    return False


# Lazily-loaded third-party JSON-Schema validator class (openapi-schema-
# validator, an OpenAPI-flavoured wrapper over jsonschema). Cached as the
# class, as False when the library is genuinely absent, and as None until the
# first lookup — so a missing dev dependency degrades to the hand check
# instead of hard-crashing the runner (S14.7 honesty clause).
_SCHEMA_VALIDATOR: Any = None


def _schema_validator() -> Any:
    """The OAS31Validator class if the schema library is importable, else None.

    Why: the library is a DEV/TEST oracle (tests/requirements-dev.txt), not a
    hard runtime dependency of the shipped engine — importing it lazily and
    tolerating its absence keeps ``run_scenarios`` usable in a bare
    environment (falls back to the hand body_check + a note).
    What: imports once, memoises the class (or False on ImportError).
    Test: tests/audit/test_scenario_body_oracle.py exercises the present-lib
    path; the absent path degrades to the historical hand check."""
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        try:
            from openapi_schema_validator import OAS31Validator
            _SCHEMA_VALIDATOR = OAS31Validator
        except Exception:  # noqa: BLE001 — optional dev/test oracle
            _SCHEMA_VALIDATOR = False
    return _SCHEMA_VALIDATOR or None


def _schema_violation(schema: dict, text: str) -> Optional[tuple]:
    """First closed-schema violation of a live body, as (expected, got), or
    None when the body conforms / cannot be judged by the library.

    Why (S14.7): catches what the hand equals/contains/json_subset cannot —
    a WRONG type on a declared field and an EXTRA field under
    additionalProperties:false — using the maintained library, not more
    hand-rolled subset logic (F1 exploit at RUNTIME). Non-JSON bodies and an
    absent library return None so this stays strictly ADDITIVE: it never
    overrides or weakens an existing hand judgement, only adds reds the hand
    check structurally misses. When the library is absent the caller keeps a
    note; when the body is non-JSON there is nothing for a JSON schema to say.
    What: runs OAS31Validator(schema).iter_errors(doc); the first error's
    message becomes the ``got`` and the schema the ``expected``.
    Test: tests/audit/test_scenario_body_oracle.py — extra field and wrong
    type both red; an honest closed body returns None."""
    validator_cls = _schema_validator()
    if validator_cls is None:
        return None
    try:
        doc = json.loads(text)
    except Exception:  # noqa: BLE001 — a non-JSON body: nothing to validate
        return None
    try:
        errors = list(validator_cls(schema).iter_errors(doc))
    except Exception:  # noqa: BLE001 — a malformed schema is not a body red
        return None
    if errors:
        return ("body matching response schema %s" % _show(schema),
                "body %s violates schema (%s)"
                % (_show(text), errors[0].message))
    return None


def _judge(then: dict, status: Any, media: str, text: str,
           op: Any = None) -> Optional[tuple]:
    """Judge one then-clause; returns (expected, got) on violation.

    Why: status, media and the hand body_check (equals/contains/json_subset)
    are the historical judgement; S14.7 ADDS a library check of the live body
    against the route's CLOSED response schema (``op`` for the achieved
    status) so a wrong-type or extra-field violation the hand subset misses
    still reds the scenario.
    What: runs the four hand checks unchanged, then — only when ``op``
    declares a closed schema for ``status`` — validates the body with the
    schema library; the library red is returned after the hand red so an
    explicit body_check still wins the attribution.
    Test: tests/audit/test_scenario_runner_oracle.py (hand paths) and
    tests/audit/test_scenario_body_oracle.py (library path)."""
    want = then.get("status")
    if want is not None and str(status) != str(want):
        return ("status %s" % want, "status %s" % status)
    want_media = then.get("media")
    if want_media is not None and media != want_media:
        return ("media %s" % want_media, "media %s" % (media or "(none)"))
    bc = then.get("body_check")
    if isinstance(bc, dict) and len(bc) == 1:
        kind, expected = next(iter(bc.items()))
        if kind == "contains":
            if str(expected) not in text:
                return ("body contains %s" % _show(expected),
                        "body %s" % _show(text))
        elif kind == "equals":
            if isinstance(expected, str):
                if text.strip() != expected.strip():
                    return ("body equals %s" % _show(expected),
                            "body %s" % _show(text))
            else:
                try:
                    doc = json.loads(text)
                except Exception:  # noqa: BLE001 — non-JSON live body
                    doc = None
                if doc != expected:
                    return ("body equals %s" % _show(expected),
                            "body %s" % _show(text))
        elif kind == "json_subset":
            try:
                doc = json.loads(text)
            except Exception:  # noqa: BLE001 — non-JSON live body
                doc = None
            if not _json_subset(expected, doc):
                return ("body json_subset %s" % _show(expected),
                        "body %s" % _show(text))
    # S14.7 — additive library check against the CLOSED response schema, run
    # after the hand body_check so an explicit hand red keeps attribution.
    schema = _response_schema(op, status)
    if isinstance(schema, dict):
        return _schema_violation(schema, text)
    return None


def _iter_scenarios(ir: dict):
    for nid, node in (ir.get("nodes") or {}).items():
        if isinstance(node, dict):
            for sc in (node.get("scenarios") or []):
                if isinstance(sc, dict):
                    yield str(nid), sc


def run_scenarios(ir: dict, wsgi_app: Any = None,
                  entry_path: Optional[str] = None,
                  workdir: Optional[str] = None) -> dict:
    """Execute every IR scenario through the WSGI surface as a black box.

    Why: the ONE oracle of the interface — a scenario violation is a red
    build; nothing else may claim the interface is honoured.
    What: refuses an invalid IR (S14.2), applies factory + given.env,
    replays given.state, performs when, judges then; see the result schema
    in the module docstring.
    Test: tests/audit/test_scenario_runner_oracle.py.
    """
    res: dict = {"ok": True, "refused": [], "failures": [],
                 "incomplete": [], "passed": 0, "notes": []}
    # S14.7 honesty: if a CLOSED response schema is contracted anywhere but
    # the schema library is absent, the runner degrades to the hand body_check
    # and records ONE note — never a hard crash, never a silent downgrade.
    if _schema_validator() is None and _any_closed_schema(ir):
        res["notes"].append(
            "openapi-schema-validator unavailable — response bodies judged by "
            "the hand body_check only (closed-schema validation skipped)")
    try:
        import spec_ir
    except ImportError:
        # `python3 -I` implies -P since 3.11: the script dir is NOT on
        # sys.path — add it so the sibling spec_ir resolves hermetically
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import spec_ir
    verdict = spec_ir.validate_ir(ir)
    if verdict.get("errors"):
        res["ok"] = False
        res["refused"] = [str(e) for e in verdict["errors"]]
        return res

    scenarios = list(_iter_scenarios(ir))
    if not scenarios:
        return res

    registry = _registry(ir)
    factory = EnvValueFactory(workdir or tempfile.gettempdir())
    # factory values for every DECLARED env var are applied before the
    # entry loads, so import-time reads see the same values as request-time
    base_env: dict = {}
    for _nid, node in sorted((ir.get("nodes") or {}).items()):
        if isinstance(node, dict):
            for ent in (node.get("env") or []):
                if isinstance(ent, dict) and ent.get("name"):
                    name = str(ent["name"])
                    base_env.setdefault(
                        name, factory.value(name, str(ent.get("rule") or "")))

    saved = {k: os.environ.get(k) for k in base_env}
    os.environ.update(base_env)
    try:
        if wsgi_app is None:
            try:
                wsgi_app = _load_wsgi_app(
                    entry_path or "",
                    (ir.get("product") or {}).get("callable"))
            except Exception as exc:  # noqa: BLE001 — boot IS the finding
                for nid, sc in scenarios:
                    res["failures"].append({
                        "requirement": str(sc.get("requirement") or nid),
                        "node": nid, "step": "boot",
                        "expected": "an importable WSGI entry",
                        "got": _show(str(exc))})
                res["ok"] = False
                return res
        for nid, sc in scenarios:
            _run_one(nid, sc, wsgi_app, registry, res)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    res["ok"] = not res["failures"] and not res["refused"]
    return res


def _run_one(nid: str, sc: dict, app: Any, registry: dict,
             res: dict) -> None:
    """One scenario: env overlay, state replay, the when, the judgement."""
    req = str(sc.get("requirement") or nid)
    given = sc.get("given") if isinstance(sc.get("given"), dict) else {}
    when = sc.get("when") or {}
    steps = list(given.get("state") or []) + [when]

    # S14.3 — a bodyless step over required fields is a gap, never a guess
    for step in steps:
        missing = _unvalued_step(step, registry) if isinstance(step, dict) \
            else None
        if missing:
            res["incomplete"].append({
                "requirement": req, "node": nid,
                "step": "%s %s" % (str(step.get("method") or "").upper(),
                                   step.get("path")),
                "missing": "request body values not recorded (required: %s)"
                           % ", ".join(missing)})
            return

    overlay = {str(k): str(v) for k, v in (given.get("env") or {}).items()}
    saved = {k: os.environ.get(k) for k in overlay}
    os.environ.update(overlay)
    try:
        for step in given.get("state") or []:
            method = str(step.get("method") or "").upper()
            path = str(step.get("path") or "")
            label = "%s %s (given.state)" % (method, path)
            try:
                status, _media, text = _call(app, method, path,
                                             step.get("body"))
            except Exception as exc:  # noqa: BLE001 — a crash is a red
                res["failures"].append({
                    "requirement": req, "node": nid, "step": label,
                    "expected": "a served state step",
                    "got": _show("crash: %s" % exc)})
                return
            if status is None or int(status) >= 400:
                res["failures"].append({
                    "requirement": req, "node": nid, "step": label,
                    "expected": "a served state step (status < 400)",
                    "got": "status %s, body %s" % (status, _show(text))})
                return
        method = str(when.get("method") or "").upper()
        path = str(when.get("path") or "")
        label = "%s %s" % (method, path)
        try:
            status, media, text = _call(app, method, path, when.get("body"))
        except Exception as exc:  # noqa: BLE001 — a crash is a red
            res["failures"].append({
                "requirement": req, "node": nid, "step": label,
                "expected": "a served request",
                "got": _show("crash: %s" % exc)})
            return
        # the op for the achieved route+status carries the CLOSED response
        # schema the live body is validated against (S14.7)
        op = registry.get((method, path))
        bad = _judge(sc.get("then") or {}, status, media, text, op)
        if bad:
            res["failures"].append({
                "requirement": req, "node": nid, "step": label,
                "expected": bad[0], "got": bad[1]})
        else:
            res["passed"] += 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------
# CLI — the engine drives this in a hermetic subprocess (python3 -I)
# --------------------------------------------------------------------------

def main(argv: list) -> int:
    """Why: the engine must judge the product in a sterile process (P6).
    What: reads <ir.json> <workspace_root>, resolves the product entry from
    ir.product, prints one 'SCENARIO_RESULT {json}' line; rc 0 always — the
    verdict travels in the JSON, never in the exit code.
    Test: engine-wiring cases spawn this via _ir_scenario_gate."""
    if len(argv) < 3:
        print(RESULT_MARK + json.dumps(
            {"ok": False, "refused": ["usage: spec_scenarios.py "
                                      "<ir.json> <workspace_root>"],
             "failures": [], "incomplete": [], "passed": 0}))
        return 0
    ir_path, ws_root = argv[1], argv[2]
    try:
        ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — unreadable IR is a refusal
        print(RESULT_MARK + json.dumps(
            {"ok": False, "refused": ["ir.json unreadable: %s" % exc],
             "failures": [], "incomplete": [], "passed": 0}))
        return 0
    entry = ((ir.get("product") or {}).get("entry")
             if isinstance(ir, dict) else None)
    has_scenarios = isinstance(ir, dict) and any(
        (n or {}).get("scenarios")
        for n in (ir.get("nodes") or {}).values() if isinstance(n, dict))
    if has_scenarios and not entry:
        print(RESULT_MARK + json.dumps(
            {"ok": False,
             "refused": ["scenarios present but ir.product declares no "
                         "entry — no WSGI surface to judge against"],
             "failures": [], "incomplete": [], "passed": 0}))
        return 0
    res = run_scenarios(
        ir, entry_path=str(Path(ws_root) / entry) if entry else None,
        workdir=ws_root)
    print(RESULT_MARK + json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
