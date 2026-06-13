"""Auto-derive an API contract from a smoke test (#9 / П2).

A smoke acceptance test IS the executable truth of what the product must do.
Rather than hand-maintain a separate contract block that drifts from it, parse
the smoke's AST and read the surface it exercises:

  * ENDPOINTS — calls to an HTTP-ish helper (get/post/put/delete/patch, or any
    call whose first argument is a "/path" string literal);
  * ASSERTED FACTS per test — status codes (``status == 200``), content types
    (``startswith("text/html")`` / ``"application/json" in ...``), and JSON
    keys (``data["items"]`` / ``"items" in body``).

The derived clauses render to a markdown contract block the constitution can
reference, so the implementer and reviewer enforce exactly what the smoke
checks — one source of truth, no drift. Test: tests/test_contract_from_smoke.py.
"""
from __future__ import annotations

import ast
from typing import Optional

_HTTP_HELPERS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _str(node: ast.AST) -> Optional[str]:
    return node.value if isinstance(node, ast.Constant) and \
        isinstance(node.value, str) else None


def _endpoints_in(fn: ast.AST) -> list:
    """Endpoints a test function hits: (METHOD, path). METHOD is the helper
    name upper-cased when it is an HTTP verb, else 'CALL'."""
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        name = (n.func.id if isinstance(n.func, ast.Name)
                else n.func.attr if isinstance(n.func, ast.Attribute) else "")
        path = _str(n.args[0]) if n.args else None
        if path and path.startswith("/"):
            verb = name.upper() if name.lower() in _HTTP_HELPERS else "CALL"
            out.append((verb, path))
    # de-dup preserving order
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def _facts_in(fn: ast.AST) -> list:
    """Asserted facts: status codes, content types, JSON/body keys."""
    facts = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assert):
            continue
        for sub in ast.walk(n.test):
            # status == 200
            if isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name) \
                    and "status" in sub.left.id.lower():
                for c in sub.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, int):
                        facts.append(f"status {c.value}")
            # x.startswith("text/html")  /  "application/json" in x
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "startswith":
                s = _str(sub.args[0]) if sub.args else None
                if s:
                    facts.append(f"content-type {s}")
            if isinstance(sub, ast.Compare) and any(
                    isinstance(o, (ast.In,)) for o in sub.ops):
                s = _str(sub.left)
                if s and ("/" in s or s.islower()):
                    facts.append(f"contains '{s}'")
            # data["items"] subscript with a string key
            if isinstance(sub, ast.Subscript):
                s = _str(sub.slice)
                if s:
                    facts.append(f"key '{s}'")
    # de-dup preserving order
    seen, uniq = set(), []
    for f in facts:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def derive_contract(source: str) -> list:
    """Parse smoke source → [{test, endpoints:[(verb,path)], facts:[str]}].
    A syntactically broken smoke yields [] (never raises)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            eps = _endpoints_in(node)
            facts = _facts_in(node)
            if eps or facts:
                out.append({"test": node.name, "endpoints": eps,
                            "facts": facts})
    return out


def render_contract(clauses: list) -> str:
    """Render derived clauses as a markdown contract block."""
    if not clauses:
        return ""
    lines = ["## API contract (auto-derived from the smoke acceptance)"]
    for c in clauses:
        lines.append(f"- **{c['test']}**")
        for verb, path in c["endpoints"]:
            lines.append(f"    - `{verb} {path}`")
        if c["facts"]:
            lines.append(f"    - asserts: {', '.join(c['facts'])}")
    return "\n".join(lines) + "\n"


def contract_from_file(path: str) -> str:
    """Convenience: derive + render a contract block from a smoke file path."""
    try:
        with open(path, encoding="utf-8") as fh:
            return render_contract(derive_contract(fh.read()))
    except OSError:
        return ""
