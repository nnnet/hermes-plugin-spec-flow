"""LLM judge agent — scores a leaf's code against its spec (roadmap 4.5).

An independent reviewer: given the node spec and the produced code+test, it
returns a verdict (pass/fail + reasons). Injected via
``run_project(..., agents={"judge": judge})``; off by default. The model call is
injectable so the adapter is verified OFFLINE with a stub (no quota). A failing
verdict is recorded by the engine as a ``judge`` gate and a rework loop.
"""

from __future__ import annotations

import json
import os
import re

from . import config, llm_log

PROMPT = """You are an independent reviewer in a Spec-Driven Development run.

Decide whether the CODE faithfully implements the SPEC for one leaf. Be strict
but fair: judge behaviour against the spec's acceptance criteria, not style.

Leaf: {node} — {title}
Spec (path): {spec}

CODE (src):
{code}

TEST (tests):
{test}

Return ONLY a JSON object, no prose, no fence:
{{"verdict": "pass" | "fail", "reasons": "<one or two sentences>"}}
"""

# per-role model: SPEC_FLOW_JUDGE_MODEL overrides the shared SPEC_FLOW_LLM_MODEL
MODEL = config.env("JUDGE_MODEL", default="") or config.env("LLM_MODEL")


def _ask(prompt: str) -> str:
    """Delegate to the unified backend (provider/model = config)."""
    from . import llm_backend
    return llm_backend.ask(prompt, model=MODEL, role="judge", step="")


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"judge reply has no JSON: {text[-200:]}")
    out = json.loads(m.group(0))
    verdict = str(out.get("verdict", "")).lower()
    if verdict not in ("pass", "fail"):
        raise ValueError(f"judge verdict not pass/fail: {out!r}")
    return {"verdict": verdict, "reasons": out.get("reasons", "")}


def make_judge(ask=_ask):
    """Build a judge agent driven by ``ask`` (prompt -> reply). Default is the
    live claude CLI; inject a stub for offline tests."""

    def judge(ctx: dict) -> dict:
        node = ctx.get("node", "?")
        prompt = PROMPT.format(node=node, title=ctx.get("title", ""),
                               spec=ctx.get("spec", ""),
                               code=ctx.get("code", "")[:4000],
                               test=ctx.get("test", "")[:4000])
        # SINGLE door: the backend (default _ask -> llm_backend.ask) logs
        # call_start/ok/error itself, so call it directly (no timed_ask).
        reply = ask(prompt)
        out = _parse(reply)
        llm_log.log_outcome(role="judge", node=node, verdict=out["verdict"])
        return out

    return judge
