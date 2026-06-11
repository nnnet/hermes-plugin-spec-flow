"""LLM implementer agent — writes REAL leaf code from the node spec (roadmap D2).

This is the live counterpart of the auto_implementer stand-in: instead of a
fixed template it asks a model to write a working module plus a passing pytest
for ONE leaf, honouring the node's spec. The engine still runs the produced
tests (depth >= verify) — the agent only supplies the code, exactly like a
Hermes worker / Claude Agent SDK call would.

Backend: the local ``claude`` CLI (``claude -p``), same as ``llm_decomposer``.
The model call is injectable (``make_implementer(ask=...)``) so the adapter — its
prompt contract, JSON parsing and file layout — is verified OFFLINE with a stub,
spending NO quota. A live run is opt-in via ``make_implementer()`` (real CLI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

PROMPT = """You are the implementer of a Spec-Driven Development run.

Implement EXACTLY ONE leaf task as real, working Python.

Leaf id: {id}
Leaf title: {title}
Spec file (already written): {spec}
Goal context: {goal}

Hard contract (the run will import and test your output):
- module file: src/{fn}.py exposing a function ``{fn}(payload=None)``
- the function returns a dict with at least {{"status": "ok"}}
- test file: tests/test_{fn}.py — real pytest assertions that import the module
  with: sys.path.insert(0, str(Path(__file__).resolve().parent.parent/"src"))
- tests MUST pass against your own code (no TODOs, no NotImplementedError)

Return ONLY a JSON object, no prose, no markdown fence:
{{"code": "<contents of src/{fn}.py>", "test": "<contents of tests/test_{fn}.py>"}}
"""

# cheap & fast model for the test runs; override via env (shared with decomposer)
MODEL = os.environ.get("SPEC_FLOW_LLM_MODEL", "haiku")


def _snake(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")


def _ask(prompt: str) -> str:
    """Default backend — the local claude CLI. Swap for a Hermes worker / SDK."""
    proc = subprocess.run(["claude", "-p", "--model", MODEL, prompt],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {proc.stderr[-500:]}")
    return proc.stdout


def _parse(text: str) -> dict:
    """Accept a raw JSON object or two fenced code blocks (code then test)."""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            out = json.loads(m.group(0))
            if "code" in out and "test" in out:
                return {"code": out["code"], "test": out["test"]}
        except json.JSONDecodeError:
            pass
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", text, re.S)
    if len(blocks) >= 2:
        return {"code": blocks[0].strip(), "test": blocks[1].strip()}
    raise ValueError(f"implementer reply has no code+test: {text[-300:]}")


def make_implementer(ask=_ask):
    """Build an implementer agent driven by ``ask`` (a prompt -> reply callable).

    Default ``ask`` is the live claude CLI; inject a stub for offline tests.
    """

    def implement(ctx: dict) -> None:
        ws = ctx["workspace"]
        node, title, spec = ctx["node"], ctx["title"], ctx["spec"]
        fn = _snake(node)
        prompt = PROMPT.format(id=node, title=title, spec=spec, fn=fn,
                               goal=ctx.get("goal", ""))
        out = _parse(ask(prompt))
        ws._write(f"src/{fn}.py", out["code"], "code")
        ws._write(f"tests/test_{fn}.py", out["test"], "test")

    return implement


# live default — used when an implementer is injected without a stub
implement = make_implementer()
