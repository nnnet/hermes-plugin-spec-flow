"""aider-style repository map — the cure for worker blindness.

A chat worker cannot list or read the workspace; everything it 'knows' is
what the harness puts into its prompt. Point fixes (inline the platform,
inline sibling schemas) each closed one failure class — this module replaces
them with the general mechanism: ONE auto-generated digest of every module's
public surface, rebuilt from the live AST on each call.

Per module the map shows:
  * the docstring's first line (what the module is for)
  * registered HTTP routes (the @registry.route decorators, verbatim)
  * sqlite schema blocks passed to db.register_schema (verbatim SQL)
  * class signatures with their method signatures
  * top-level function signatures with the docstring's first line

Standard library only (ast) — the workspace is pure Python by constitution.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable, Optional

MAP_BUDGET = 12000          # total characters handed to a prompt
_SCHEMA_RE = re.compile(
    r"register_schema\(\s*(?:\"\"\"|''')(.*?)(?:\"\"\"|''')", re.S)


def _arg_list(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    names = [a.arg for a in fn.args.args]
    if fn.args.vararg:
        names.append("*" + fn.args.vararg.arg)
    names += [a.arg for a in fn.args.kwonlyargs]
    if fn.args.kwarg:
        names.append("**" + fn.args.kwarg.arg)
    return ", ".join(names)


def _doc_first_line(node) -> str:
    doc = ast.get_docstring(node) or ""
    return doc.splitlines()[0].strip() if doc else ""


def _decorators(fn) -> list[str]:
    out = []
    for dec in fn.decorator_list:
        try:
            out.append("@" + ast.unparse(dec))
        except Exception:  # noqa: BLE001 — a map line is never worth a crash
            pass
    return out


def map_file(path: Path) -> str:
    """One module's public surface, compact and prompt-ready."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return f"## {path.name}: (unparseable: {str(exc)[:60]})"
    lines = [f"## {path.name}" + (
        f" — {_doc_first_line(tree)}" if _doc_first_line(tree) else "")]
    for sql in _SCHEMA_RE.findall(path.read_text(encoding="utf-8")):
        lines.append("  schema:")
        lines += [f"    {l}" for l in sql.strip().splitlines()]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in _decorators(node):
                lines.append(f"  {dec}")
            doc = _doc_first_line(node)
            lines.append(f"  def {node.name}({_arg_list(node)})"
                         + (f"  # {doc}" if doc else ""))
        elif isinstance(node, ast.ClassDef):
            lines.append(f"  class {node.name}:")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(
                        f"    def {sub.name}({_arg_list(sub)})")
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    lines.append(f"  {tgt.id} = …")
    return "\n".join(lines)


def build_map(ws_root: str, subdirs: Iterable[str] = ("src", "tests"),
              exclude: Optional[set] = None,
              budget: int = MAP_BUDGET) -> str:
    """The whole workspace's public surface, budget-capped.

    ``exclude`` holds workspace-relative paths to skip (e.g. platform files
    already inlined in FULL elsewhere in the prompt)."""
    exclude = exclude or set()
    blocks = []
    root = Path(ws_root or ".")
    for sub in subdirs:
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            rel = str(f.relative_to(root))
            if rel in exclude or "__pycache__" in rel:
                continue
            blocks.append(map_file(f))
    text = "\n".join(blocks)
    if len(text) > budget:
        text = text[:budget] + "\n…(map truncated)"
    return text
