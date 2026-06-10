"""Autonomous implementer agent for depth='execute' runs.

A deterministic stand-in for a real LLM/Hermes implementer: for every leaf it
writes REAL, working code (a module honouring the node's spec) plus a REAL,
passing acceptance test — so an execute-depth run materialises a green,
runnable project instead of red scaffolds. Inject a smarter agent via
``run_project(..., agents={"implementer": ...})``.

The agent receives the engine's context dict:
    {"node": id, "title": title, "workspace": Workspace, "spec": "specs/<id>.md"}
and must place its output at src/<id>.py and tests/test_<id>.py.
"""

from __future__ import annotations


def _snake(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")


def implement(ctx: dict) -> None:
    ws = ctx["workspace"]
    node, title, spec = ctx["node"], ctx["title"], ctx["spec"]
    fn = _snake(node)
    code = (
        f'"""{title}.\n\n'
        f'Written by the autonomous implementer (depth=execute). Spec: {spec}.\n"""\n\n\n'
        f'def {fn}(payload=None):\n'
        f'    """Handle one request for: {title}."""\n'
        f'    result = {{"node": "{node}", "title": {title!r}, "status": "ok"}}\n'
        f'    if payload is not None:\n'
        f'        result["payload"] = payload\n'
        f'    return result\n'
    )
    test = (
        f'"""Acceptance test for {node} (depth=execute, real run)."""\n\n'
        f'import sys\n'
        f'from pathlib import Path\n\n'
        f'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))\n\n'
        f'from {fn} import {fn}\n\n\n'
        f'def test_{fn}_handles_request():\n'
        f'    out = {fn}({{"probe": 1}})\n'
        f'    assert out["status"] == "ok"\n'
        f'    assert out["node"] == "{node}"\n'
        f'    assert out["payload"] == {{"probe": 1}}\n\n\n'
        f'def test_{fn}_no_payload():\n'
        f'    out = {fn}()\n'
        f'    assert out["status"] == "ok" and "payload" not in out\n'
    )
    ws._write(f"src/{fn}.py", code, "code")
    ws._write(f"tests/test_{fn}.py", test, "test")
