"""Layer-1 audit: cross-artifact consistency checks for a spec-flow run dir.

Meta-principle: any two artifacts that are OBLIGED to agree are checked by
machine. A run directory looks like:

    <run_dir>/
        trace.jsonl
        meta.json
        tree.json
        workspace/
            specs/*.md
            src/*.py
            tests/*.py
            PRODUCT-RESULTS.md
            contracts/interface.json     (optional, absent in older runs)

Checks implemented (each mismatch becomes a Finding):

1. spec prose mentions of ``src/<X>.py``  <->  node ownership
   (node module = snake(node id); plus every declared code_target/entry).
   A mentioned file nobody owns is a finding.
2. spec metrics table (``| modules | N |``)  <->  number of planned src
   files in the SAME spec's Scope "In:" section. Contradiction is a finding.
3. status asserts in workspace/tests/*.py (2xx literals after a route call)
   <->  the status contract: POST=201 else 200, or contracts/interface.json
   when present. A 2xx assert on an undeclared route is also a finding.
4. meta.json ``status``  <->  ``Status:`` line in PRODUCT-RESULTS.md.
5. every workspace/src/*.py must map to an owner node or the declared
   entry — an orphan file is a finding.

Library + CLI:  python3 tests/audit/consistency.py <run_dir>
Exit code 1 when findings exist.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# 2xx success status the contract assigns per method (unless interface.json overrides)
DEFAULT_SUCCESS = {"POST": 201}
DEFAULT_SUCCESS_FALLBACK = 200

# meta.json status values normalised onto the PRODUCT-RESULTS vocabulary
_META_NOT_READY = {"FAILED", "FAIL", "NOT READY", "NOT_READY", "ERROR", "RED"}
_META_READY = {"PASSED", "PASS", "OK", "READY", "SUCCESS", "DONE", "GREEN"}


@dataclass(frozen=True)
class Finding:
    """One machine-detected disagreement between two artifacts."""

    file: str  # artifact path relative to the run dir
    kind: str  # check class identifier
    message: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.kind}] {self.file}: {self.message}"


# --------------------------------------------------------------------------
# tree.json helpers
# --------------------------------------------------------------------------

def walk_nodes(tree: dict) -> Iterator[dict]:
    """Yield every node of the decomposition tree, root included."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("children") or [])


def snake(node_id: str) -> str:
    """Node id -> module stem (engine convention: snake of the id)."""
    return re.sub(r"\W+", "_", node_id, flags=re.UNICODE).strip("_").lower()


def owned_modules(tree: dict) -> set[str]:
    """Module file names (basename) owned by some node or declared entry."""
    owned: set[str] = set()
    for node in walk_nodes(tree):
        node_id = node.get("id")
        if node_id:
            owned.add(snake(node_id) + ".py")
        target = node.get("code_target")
        if target:
            owned.add(Path(target).name)
    return owned


def _handler_to_route(handler: str) -> tuple[str, str] | None:
    """'post_notes(payload, query)' -> ('POST', '/notes'); None if not route-shaped."""
    name = handler.split("(", 1)[0].strip()
    head, _, rest = name.partition("_")
    if head.upper() in HTTP_METHODS and rest:
        return head.upper(), "/" + rest
    return None


def contract_routes(tree: dict, run_dir: Path) -> dict[tuple[str, str], int]:
    """Declared routes -> contracted success status.

    Prefers workspace/contracts/interface.json when present (tolerant parse);
    otherwise derives routes from the ``exposes`` handler names in tree.json
    with the default status rule POST=201 else 200.
    """
    routes: dict[tuple[str, str], int] = {}

    for node in walk_nodes(tree):
        for handler in node.get("exposes") or []:
            route = _handler_to_route(handler)
            if route:
                method, _ = route
                routes[route] = DEFAULT_SUCCESS.get(method, DEFAULT_SUCCESS_FALLBACK)

    iface = run_dir / "workspace" / "contracts" / "interface.json"
    if iface.is_file():
        try:
            data = json.loads(iface.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        for method, path, status in _iter_interface_routes(data):
            routes[(method, path)] = status if status else routes.get(
                (method, path), DEFAULT_SUCCESS.get(method, DEFAULT_SUCCESS_FALLBACK)
            )
    return routes


def _iter_interface_routes(data) -> Iterator[tuple[str, str, int | None]]:
    """Best-effort extraction of (method, path, success_status) from interface.json."""
    if isinstance(data, dict):
        candidates = data.get("routes", data)
    else:
        candidates = data
    if isinstance(candidates, dict):
        # {"POST /notes": 201, ...} style
        for key, value in candidates.items():
            parts = str(key).split()
            if len(parts) == 2 and parts[0].upper() in HTTP_METHODS:
                status = value if isinstance(value, int) else None
                yield parts[0].upper(), parts[1], status
        return
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            method = str(item.get("method", "")).upper()
            path = item.get("path") or item.get("url_path") or ""
            if method in HTTP_METHODS and str(path).startswith("/"):
                status = item.get("success_status") or item.get("status")
                yield method, str(path), status if isinstance(status, int) else None


# --------------------------------------------------------------------------
# check 1 + 2: spec prose vs ownership, spec metrics vs scope
# --------------------------------------------------------------------------

_SRC_MENTION = re.compile(r"src/([\w\-]+\.py)", re.UNICODE)
_METRIC_MODULES = re.compile(r"^\|\s*modules\s*\|\s*(\d+)\s*\|", re.MULTILINE)
_SCOPE_SRC_LINE = re.compile(r"^\s*-\s*(src/[\w\-]+\.py)\b", re.UNICODE | re.MULTILINE)


def _spec_files(run_dir: Path) -> list[Path]:
    specs = run_dir / "workspace" / "specs"
    return sorted(specs.glob("*.md")) if specs.is_dir() else []


def check_spec_mentions_owned(run_dir: Path, tree: dict) -> list[Finding]:
    """Every src/<X>.py a spec talks about must be owned by some node/entry."""
    owned = owned_modules(tree)
    findings: list[Finding] = []
    for spec in _spec_files(run_dir):
        text = spec.read_text(encoding="utf-8")
        rel = str(spec.relative_to(run_dir))
        for module in sorted(set(_SRC_MENTION.findall(text))):
            if module not in owned:
                findings.append(
                    Finding(
                        file=rel,
                        kind="spec_mentions_unowned_file",
                        message=(
                            f"spec mentions src/{module} but no tree node owns it "
                            f"(owned modules: {', '.join(sorted(owned))})"
                        ),
                    )
                )
    return findings


def _scope_in_files(text: str) -> list[str]:
    """src/*.py bullets from the '## Scope' 'In:' block of a spec."""
    scope_match = re.search(r"^## Scope\s*$", text, flags=re.MULTILINE)
    if not scope_match:
        return []
    tail = text[scope_match.end():]
    next_section = re.search(r"^## ", tail, flags=re.MULTILINE)
    scope = tail[: next_section.start()] if next_section else tail
    in_match = re.search(r"^In:\s*$", scope, flags=re.MULTILINE)
    if not in_match:
        return []
    in_block = scope[in_match.end():]
    out_match = re.search(r"^Out:\s*$", in_block, flags=re.MULTILINE)
    if out_match:
        in_block = in_block[: out_match.start()]
    return [m.group(1) for m in _SCOPE_SRC_LINE.finditer(in_block)]


def check_spec_metrics_vs_scope(run_dir: Path) -> list[Finding]:
    """metrics table 'modules=N' must not contradict the spec's own Scope plan."""
    findings: list[Finding] = []
    for spec in _spec_files(run_dir):
        text = spec.read_text(encoding="utf-8")
        metric = _METRIC_MODULES.search(text)
        planned = _scope_in_files(text)
        if not metric or not planned:
            continue  # nothing to cross-check in this spec
        modules = int(metric.group(1))
        if modules != len(planned):
            findings.append(
                Finding(
                    file=str(spec.relative_to(run_dir)),
                    kind="spec_metrics_scope_contradiction",
                    message=(
                        f"metrics table declares modules={modules} but Scope 'In:' "
                        f"plans {len(planned)} src file(s): {', '.join(planned)}"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------
# check 3: status asserts in workspace tests vs the status contract
# --------------------------------------------------------------------------

def _route_of_call(call: ast.Call, handlers: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """Route addressed by a call: ('METHOD', '/path') args or a known handler name."""
    if len(call.args) >= 2:
        first, second = call.args[0], call.args[1]
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.upper() in HTTP_METHODS
            and isinstance(second, ast.Constant)
            and isinstance(second.value, str)
            and second.value.startswith("/")
        ):
            return first.value.upper(), second.value
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name in handlers:
        return handlers[name]
    return None


def _success_statuses(node: ast.AST) -> list[int]:
    """2xx literals inside an assert: ints 200-299 or strings like '201 Created'."""
    statuses: list[int] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Constant):
            continue
        value = sub.value
        if isinstance(value, int) and not isinstance(value, bool) and 200 <= value <= 299:
            statuses.append(value)
        elif isinstance(value, str) and re.match(r"^2\d\d(\s|$)", value):
            statuses.append(int(value[:3]))
    return statuses


def _assert_nodes(func: ast.AST) -> Iterator[ast.AST]:
    """assert statements + unittest self.assert* calls, any nesting depth."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            yield node
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("assert")
        ):
            yield node


def _interface_handlers(run_dir: Path) -> dict[str, tuple[str, str]]:
    """handler name -> (METHOD, path) from contracts/interface.json rows.

    The machine contract names each route's handler (S10.13 grows it with
    adopted routes), so the checker binds calls to the SAME datum the engine
    declared instead of re-deriving it from tree ``exposes`` alone."""
    iface = run_dir / "workspace" / "contracts" / "interface.json"
    out: dict[str, tuple[str, str]] = {}
    if not iface.is_file():
        return out
    try:
        data = json.loads(iface.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return out
    rows = data.get("routes") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method", "")).upper()
        path = item.get("path") or ""
        name = str(item.get("handler") or "").strip()
        if name and method in HTTP_METHODS and str(path).startswith("/"):
            out[name] = (method, str(path))
    return out


def check_test_status_asserts(run_dir: Path, tree: dict) -> list[Finding]:
    """2xx status asserted after a route call must match the contracted status.

    Binding rule (v152): each 2xx assert binds to the nearest PRECEDING
    recognized route call in source order; a handler-SHAPED call the contract
    does not know RESETS the binding, so the asserts answering it are skipped
    — never blamed on an earlier recognized route (v152: test_core.py:87's
    200 answered delete_notes at line 86 but was bound to post_notes at 85
    and reported as a POST /notes mismatch).
    """
    routes = contract_routes(tree, run_dir)
    handlers: dict[str, tuple[str, str]] = {}
    for node in walk_nodes(tree):
        for handler in node.get("exposes") or []:
            route = _handler_to_route(handler)
            if route:
                handlers[handler.split("(", 1)[0].strip()] = route
    # the machine contract wins: it names the engine-declared handler per route
    handlers.update(_interface_handlers(run_dir))

    findings: list[Finding] = []
    tests_dir = run_dir / "workspace" / "tests"
    for test_file in sorted(tests_dir.glob("*.py")) if tests_dir.is_dir() else []:
        rel = str(test_file.relative_to(run_dir))
        try:
            module = ast.parse(test_file.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            findings.append(
                Finding(file=rel, kind="test_unparseable", message=f"cannot parse: {exc}")
            )
            continue
        for func in ast.walk(module):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Time-ordered pairing: each 2xx assert binds to the closest
            # preceding route call in the same function.
            events: list[tuple[int, str, object]] = []
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    route = _route_of_call(node, handlers)
                    if route:
                        events.append((node.lineno, "route", route))
                        continue
                    fn = node.func
                    name = (fn.attr if isinstance(fn, ast.Attribute)
                            else getattr(fn, "id", None))
                    if name and name not in handlers \
                            and _handler_to_route(name):
                        # handler-shaped call the contract does not know:
                        # it resets the binding (asserts answering it are
                        # skipped, never blamed on an earlier route)
                        events.append((node.lineno, "unknown", None))
            for node in _assert_nodes(func):
                for status in _success_statuses(node):
                    events.append((node.lineno, "assert", status))
            events.sort(key=lambda item: (item[0], item[1] != "route"))

            current_route: tuple[str, str] | None = None
            for lineno, kind, payload in events:
                if kind == "route":
                    current_route = payload  # type: ignore[assignment]
                    continue
                if kind == "unknown":
                    current_route = None
                    continue
                status = payload  # type: ignore[assignment]
                if current_route is None:
                    continue  # a bare 2xx literal with no route context — not our check
                expected = routes.get(current_route)
                method, path = current_route
                if expected is None:
                    findings.append(
                        Finding(
                            file=rel,
                            kind="success_on_undeclared_route",
                            message=(
                                f"line {lineno}: asserts success {status} on "
                                f"{method} {path} which the contract does not declare"
                            ),
                        )
                    )
                elif status != expected:
                    findings.append(
                        Finding(
                            file=rel,
                            kind="status_contract_mismatch",
                            message=(
                                f"line {lineno}: asserts {status} for {method} {path} "
                                f"but the contract says {expected}"
                            ),
                        )
                    )
    return findings


# --------------------------------------------------------------------------
# check 4: meta.json status vs PRODUCT-RESULTS.md Status line
# --------------------------------------------------------------------------

def _normalize_meta_status(status: str) -> str | None:
    upper = status.strip().upper()
    if upper in _META_NOT_READY:
        return "NOT READY"
    if upper in _META_READY:
        return "READY"
    return None


def _results_status(text: str) -> str | None:
    match = re.search(r"^Status:\s*(.+)$", text, flags=re.MULTILINE)
    if not match:
        return None
    line = match.group(1).upper()
    if "NOT READY" in line:
        return "NOT READY"
    if "READY" in line:
        return "READY"
    return None


def check_meta_vs_results(run_dir: Path) -> list[Finding]:
    """meta.json 'status' must agree with the PRODUCT-RESULTS.md Status line."""
    meta_path = run_dir / "meta.json"
    results_path = run_dir / "workspace" / "PRODUCT-RESULTS.md"
    if not meta_path.is_file() or not results_path.is_file():
        return []  # older runs may lack either artifact — nothing to cross-check
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_norm = _normalize_meta_status(str(meta.get("status", "")))
    results_norm = _results_status(results_path.read_text(encoding="utf-8"))
    if meta_norm is None or results_norm is None:
        return []
    if meta_norm != results_norm:
        return [
            Finding(
                file="meta.json",
                kind="meta_results_status_mismatch",
                message=(
                    f"meta.json status={meta.get('status')!r} (normalised {meta_norm}) "
                    f"but PRODUCT-RESULTS.md says {results_norm}"
                ),
            )
        ]
    return []


# --------------------------------------------------------------------------
# check 5: every src file has an owner
# --------------------------------------------------------------------------

# engine-declared route -> handler binding line inside a spec, e.g.
# - `GET /ping` -> `def get_ping(payload, query)` — <behaviour>; SUCCESS STATUS 200
_BINDING_LINE = re.compile(
    r"^-\s+`(?P<method>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/[^`]*)`\s+->\s+"
    r"`def\s+\w+\([^)]*\)`\s+—\s+(?P<behaviour>.+?);\s+SUCCESS STATUS"
)


def check_route_contract_distinct(run_dir: Path) -> list[Finding]:
    """Template contamination: identical behaviour text stamped on DIFFERENT
    routes across DIFFERENT specs (v150: GET /ping, /health and /ui all
    carried the notes-collection wording — "a list, newest-first, honour a
    `q` filter" — verbatim from a neighbour's contract). A route's contract
    must derive from ITS OWN requirement, so two routes born of different
    requirements can never share the exact behaviour sentence."""
    seen: dict[str, set[tuple[str, str, str]]] = {}
    for spec in _spec_files(run_dir):
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _BINDING_LINE.match(line.strip())
            if not match:
                continue
            behaviour = match.group("behaviour").strip()
            seen.setdefault(behaviour, set()).add(
                (spec.name, match.group("method"),
                 match.group("path").strip().rstrip("/") or "/"))
    findings: list[Finding] = []
    for behaviour, sites in sorted(seen.items()):
        routes = {(method, path) for _f, method, path in sites}
        files = {name for name, _m, _p in sites}
        # the same spec repeating one behaviour for its own routes shares ONE
        # requirement — only a cross-spec duplicate proves a foreign template
        if len(routes) > 1 and len(files) > 1:
            findings.append(
                Finding(
                    file=", ".join(sorted(files)),
                    kind="route_contract_templated",
                    message=(
                        "identical behaviour contract stamped on different "
                        "routes (%s): %r"
                        % (", ".join(sorted(f"{m} {p}" for m, p in routes)),
                           behaviour[:120])
                    ),
                )
            )
    return findings


# the deterministic entry synthesized by the engine carries this docstring
_ENTRY_MARKER = "generated deterministically by the spec-flow engine"


def _route_table_of(path: Path) -> set[tuple[str, str]]:
    """(METHOD, path) keys of every module-level dict keyed by 2-string
    tuples — the dispatch-table shape both leaf modules and the synthesized
    entry use. AST-only, no import."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return set()
    routes: set[tuple[str, str]] = set()
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)):
            continue
        for key in node.value.keys:
            if (
                isinstance(key, ast.Tuple)
                and len(key.elts) == 2
                and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in key.elts
                )
            ):
                method = key.elts[0].value.strip().upper()
                p = key.elts[1].value.strip()
                if method in HTTP_METHODS and p.startswith("/"):
                    routes.add((method, p.rstrip("/") or "/"))
    return routes


def check_module_routes_reach_entry(run_dir: Path, tree: dict) -> list[Finding]:
    """Late-requirement assembly loss (v150): a route present in a feature
    module's OWN dispatch table must be served by the assembled entry too.

    v150: красивый_вид's in-place edit put ('GET', '/') into the notes
    owner's table; the neutralizer replaced the owner's WSGI callable with a
    delegator to the declared entry, which never wired GET / — the feature
    was silently dropped while every leaf stayed green (4 suite tests red
    forever). A dropped table route means the late requirement never reached
    its contribution to the assembly AND no node went honestly red for it.

    Silent when the entry is undetectable or carries no dispatch table (a
    promote-path entry delegates to the rival wholesale, serving its routes).
    """
    src = run_dir / "workspace" / "src"
    if not src.is_dir():
        return []
    entry: Path | None = None
    for py in sorted(src.glob("*.py")):
        try:
            head = py.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        if _ENTRY_MARKER in head:
            entry = py
            break
    if entry is None:
        for node in walk_nodes(tree):
            target = node.get("code_target")
            if node.get("id") == "product_entry" and target:
                cand = run_dir / "workspace" / str(target)
                if cand.is_file():
                    entry = cand
                break
    if entry is None:
        return []
    entry_routes = _route_table_of(entry)
    if not entry_routes:
        return []  # delegating entry: the rival's own router serves its table
    findings: list[Finding] = []
    for py in sorted(src.glob("*.py")):
        if py == entry or py.name == "__init__.py":
            continue
        for method, p in sorted(_route_table_of(py) - entry_routes):
            findings.append(
                Finding(
                    file=f"src/{py.name}",
                    kind="module_route_not_wired_in_entry",
                    message=(
                        f"{method} {p} is served by {py.name}'s own dispatch "
                        "table but the assembled entry never wires it — the "
                        "late requirement's contribution was dropped at assembly"
                    ),
                )
            )
    return findings


def check_src_orphans(run_dir: Path, tree: dict) -> list[Finding]:
    """Every workspace/src/*.py must be owned by a node or the declared entry."""
    owned = owned_modules(tree)
    findings: list[Finding] = []
    src_dir = run_dir / "workspace" / "src"
    for src_file in sorted(src_dir.glob("*.py")) if src_dir.is_dir() else []:
        if src_file.name == "__init__.py":
            continue  # packaging glue, never a feature module
        if src_file.name not in owned:
            findings.append(
                Finding(
                    file=str(src_file.relative_to(run_dir)),
                    kind="orphan_src_file",
                    message=(
                        f"{src_file.name} exists on disk but no tree node owns it "
                        f"(owned modules: {', '.join(sorted(owned))})"
                    ),
                )
            )
    return findings


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def audit(run_dir: Path) -> list[Finding]:
    """Run all consistency checks over one run directory."""
    run_dir = Path(run_dir)
    tree_path = run_dir / "tree.json"
    if not tree_path.is_file():
        return [Finding(file="tree.json", kind="missing_artifact", message="tree.json not found")]
    tree = json.loads(tree_path.read_text(encoding="utf-8"))

    findings: list[Finding] = []
    findings.extend(check_spec_mentions_owned(run_dir, tree))
    findings.extend(check_spec_metrics_vs_scope(run_dir))
    findings.extend(check_test_status_asserts(run_dir, tree))
    findings.extend(check_meta_vs_results(run_dir))
    findings.extend(check_src_orphans(run_dir, tree))
    findings.extend(check_route_contract_distinct(run_dir))
    findings.extend(check_module_routes_reach_entry(run_dir, tree))
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 tests/audit/consistency.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[1])
    if not run_dir.is_dir():
        print(f"not a directory: {run_dir}", file=sys.stderr)
        return 2
    findings = audit(run_dir)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
