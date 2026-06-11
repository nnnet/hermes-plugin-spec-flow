"""LLM decomposer agent — the plugin builds the task tree ITSELF from the goal.

This is the live counterpart of the spec-flow-decompose skill: for one node it
estimates the size metrics and, if the node is too big, proposes children one
level down. The engine still makes every leaf/branch decision through its own
leaf_check gate — the agent only supplies the level content, exactly like a
Hermes worker would.

Backend: the local ``claude`` CLI (``claude -p``), so no API key handling here;
swap ``_ask`` for a Hermes worker / SDK call in production.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from . import llm_log

PROMPT = """You are the spec-decomposer of a Spec-Driven Development run.

Project goal: {goal}
Measurable target: {target}
Constitution (non-negotiable): {constitution}

Current node: "{title}" (id: {id}, depth: {depth}, parent: {parent})

Task: estimate THIS node's size and, if it is too big to be one atomic work
package, propose its children — ONE level down only.

Rules:
- metrics keys (exact): modules, tasks, interfaces, estimated_loc,
  open_decisions, single_concern (bool), testable_criteria (bool)
- a node is atomic (leaf) only if: modules <= 1, tasks <= 5, interfaces <= 2,
  estimated_loc <= 100, open_decisions == 0, single_concern, testable_criteria
- if the node is bigger -> give it honest big metrics AND 2-4 children
  (id: snake_case slug, title: short English); children get NO metrics
- the FIRST child of the root must be upfront research (analogs,
  build-vs-reuse, differentiation), the second an architecture/NFR baseline
- be FRUGAL: a minimal viable tree, <= 20 nodes total; at depth >= 2 prefer
  leaf-sized nodes; the tree must converge by depth 3
- if something is genuinely unknown, add a research spike:
  "spike": {{"question": "...", "recommendation": "..."}}

Return ONLY a JSON object, no prose, no markdown fence:
{{"metrics": {{...}}, "children": [{{"id": "...", "title": "..."}}], "spike": {{...}}}}
"""

LEAF_RULE = """
HARD CONSTRAINT for this node: depth {depth} >= {leaf_depth}, so it MUST be
atomic. Scope it down to ONE concern doable in <= 100 LOC and <= 5 tasks.
Return metrics WITHIN the leaf thresholds and NO children."""


# cheap & fast model for tree decomposition test runs; override via env
MODEL = os.environ.get("SPEC_FLOW_LLM_MODEL", "haiku")
# depth at which the decomposer is forced to leaf — bound the tree (and thus the
# call count / wall time) for tractable live calibration. Default 3; the p1
# calibration showed fanout ~4 to depth 3 = ~85 calls > the 80 budget, so a
# tighter cap (e.g. 2) makes a run ~13-21 nodes. Override: SPEC_FLOW_LLM_LEAF_DEPTH.
LEAF_DEPTH = int(os.environ.get("SPEC_FLOW_LLM_LEAF_DEPTH", "3"))
# soft cap on children per node (the prompt asks the model to respect it)
MAX_CHILDREN = int(os.environ.get("SPEC_FLOW_LLM_MAX_CHILDREN", "4"))


def _ask(prompt: str) -> str:
    proc = subprocess.run(["claude", "-p", "--model", MODEL, prompt],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {proc.stderr[-500:]}")
    return proc.stdout


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in LLM reply: {text[-300:]}")
    return json.loads(m.group(0))


def decompose(ctx: dict) -> dict:
    p = ctx["project"]
    prompt = PROMPT.format(
        goal=p.get("goal", ""), target=p.get("target", ""),
        constitution="; ".join(p.get("constitution", [])),
        title=ctx["node"]["title"], id=ctx["node"]["id"],
        depth=ctx["depth"], parent=ctx.get("parent") or "—")
    if ctx["depth"] >= LEAF_DEPTH:
        prompt += LEAF_RULE.format(depth=ctx["depth"], leaf_depth=LEAF_DEPTH)
    nid = ctx["node"]["id"]
    reply = llm_log.timed_ask(_ask, role="decomposer", node=nid,
                              depth=ctx["depth"], model=MODEL, prompt=prompt)
    out = _extract_json(reply)
    # keep only the keys the engine understands
    keep = {k: out[k] for k in ("metrics", "children", "spike", "clarify") if k in out}
    if ctx["depth"] >= LEAF_DEPTH:
        keep.pop("children", None)          # convergence is enforced, not hoped for
    # bound fan-out so a wide tree cannot blow the call budget
    if keep.get("children") and len(keep["children"]) > MAX_CHILDREN:
        keep["children"] = keep["children"][:MAX_CHILDREN]
    for child in keep.get("children", []) or []:
        child.pop("metrics", None)          # children are sized on their own visit
    children = [c.get("id", "?") for c in keep.get("children", []) or []]
    llm_log.log_outcome(role="decomposer", node=nid, depth=ctx["depth"],
                        verdict="leaf" if not children else "branch",
                        children=children)
    return keep
