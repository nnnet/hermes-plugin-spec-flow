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
         "incomplete": [{requirement, node, step, missing}]}

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


def _judge(then: dict, status: Any, media: str, text: str) -> Optional[tuple]:
    """Judge one then-clause; returns (expected, got) on violation."""
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
                 "incomplete": [], "passed": 0}
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
        bad = _judge(sc.get("then") or {}, status, media, text)
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
