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

from . import config, llm_backend, llm_log

PROMPT = """You are the spec-decomposer of a Spec-Driven Development run.

Project goal: {goal}
Measurable target: {target}
Constitution (non-negotiable): {constitution}

Current node: "{title}" (id: {id}, depth: {depth}, parent: {parent})
Ancestor chain (root → parent): {ancestors}
Nodes ALREADY created elsewhere in the tree: {existing}

Task: FIRST judge atomicity, THEN size the node.

Step 1 — atomicity (the PRIMARY decision). Ask: is this ONE indivisible unit of
work — a single concern that a competent implementer could solve in a SINGLE
focused session / one prompt, with no unresolved design decision? Set
"atomic": true if yes, false if it genuinely needs splitting. (Criteria:
atomicity-as-executability + single-prompt solvability. Do NOT over-split — a
node that is already one small concern is atomic even if you could imagine
sub-steps.)

Step 2 — metrics (the GUARDRAIL the engine checks your claim against):
- metrics keys (exact): modules, tasks, interfaces, estimated_loc,
  open_decisions, single_concern (bool), testable_criteria (bool)
- atomic nodes satisfy: modules <= 1, tasks <= 5, interfaces <= 2,
  estimated_loc <= 100, open_decisions == 0, single_concern, testable_criteria
- if atomic=false, give honest big metrics AND 2-4 children
  (id: snake_case slug, title: short English); children get NO metrics
- child ids name the WORK ITSELF (catalog_schema, seller_onboarding); do
  NOT encode phase/type into the id — no req-/spec-/task-/research-
  prefixes, no kebab-case (the engine tracks phases; prefixes pollute
  ids and dedup)

Rules:
- keep "atomic" and the metrics CONSISTENT: atomic=true ⇒ metrics within the
  leaf thresholds and NO children; atomic=false ⇒ at least one threshold
  exceeded and 2-4 children
- NEVER propose a child that repeats work already covered by a node in the
  "already created" list above (ANY branch — not just your ancestors). If
  this node needs that result, reference it instead:
  "depends_on": ["<existing-node-id>"]. Re-creating existing work is the
  worst failure mode of this run.
- be FRUGAL: a minimal viable tree, <= 20 nodes total; prefer fewer, LARGER
  leaves over many tiny ones; the tree must converge quickly
- if something is genuinely unknown, add a research spike:
  "spike": {{"question": "...", "recommendation": "..."}}

Return ONLY a JSON object, no prose, no markdown fence:
{{"atomic": true, "metrics": {{...}}, "children": [{{"id": "...", "title": "..."}}], "depends_on": ["..."], "spike": {{...}}}}
"""

# Root-only shaping: applies to the FIRST decomposition (depth 0). Stated
# per-call before, the model dutifully re-created research/NFR children at
# EVERY branch — a major source of duplicate specs across levels.
ROOT_RULE = """
ROOT-ONLY RULE (this is depth 0): the FIRST child must be upfront research
(analogs, build-vs-reuse, differentiation), the second an architecture/NFR
baseline. Deeper nodes must NOT re-introduce research/NFR children — that
work exists once, at the top."""

LEAF_RULE = """
HARD CONSTRAINT for this node: depth {depth} >= {leaf_depth}, so it MUST be
atomic. Scope it down to ONE concern doable in <= 100 LOC and <= 5 tasks.
Return metrics WITHIN the leaf thresholds and NO children."""


# cheap & fast model for tree decomposition test runs; override via env
# per-role model: SPEC_FLOW_DECOMPOSER_MODEL overrides the shared SPEC_FLOW_LLM_MODEL
MODEL = config.env("DECOMPOSER_MODEL", default="") or config.env("LLM_MODEL")
# depth at which the decomposer is forced to leaf — bound the tree (and thus the
# call count / wall time) for tractable live calibration. Default 3; the p1
# calibration showed fanout ~4 to depth 3 = ~85 calls > the 80 budget, so a
# tighter cap (e.g. 2) makes a run ~13-21 nodes. Override: SPEC_FLOW_LLM_LEAF_DEPTH.
def _leaf_depth() -> int:
    # a case workers block overrides the .test.env floor per run
    return int(llm_backend.WORKERS_CFG.get("leaf_depth")
               or config.env("LLM_LEAF_DEPTH", int))


# soft cap on children per node (the prompt asks the model to respect it)
def _max_children() -> int:
    return int(llm_backend.WORKERS_CFG.get("max_children")
               or config.env("LLM_MAX_CHILDREN", int))


def _ask(prompt: str, meta: dict | None = None, *, model: str = "",
         step: str = "") -> str:
    """Delegate to the unified backend (provider/model = config). The backend
    is the SINGLE door: it logs call_start/call_ok/call_error itself, so no
    timed_ask wrapper here — `meta` carries node/depth into that logging.
    ``model``/``step`` let a D2 orchestra role use its own model + tag its call."""
    return llm_backend.ask(prompt, model=model or MODEL, role="decomposer",
                           step=step, meta=meta)


# ── D2: decomposer orchestra (drafter → critic → reconciler) ──────────────
# Symmetric to D1 (the implementer orchestra): with NO team configured the
# decomposer is the single agent above, byte-for-byte. A configured team turns
# one node's decomposition into a drafter that proposes the split, a critic that
# challenges it (over-split, duplicates, missing contract coverage, wrong
# atomicity), and a reconciler that returns the FINAL decomposition — every call
# still through the single door.

CRITIC_PROMPT = """You are the CRITIC of a Spec-Driven Development decomposer
team. A drafter proposed the decomposition below for ONE node; find what is
WRONG with it before it becomes the tree.

Project goal: {goal}
Measurable target: {target}
Constitution (non-negotiable): {constitution}
Node: "{title}" (id: {id}, depth: {depth})
Nodes ALREADY created elsewhere: {existing}

Drafter's proposal (JSON):
{draft}

Judge it on:
- ATOMICITY: is the atomic flag honest? Over-split (a one-concern node cut into
  tiny children) and under-split (a sprawling node called atomic) are both bugs.
- DUPLICATES: does any child repeat work already created elsewhere? It must
  depends_on it instead.
- COVERAGE: if the constitution freezes an API contract, is every endpoint owned
  by exactly one child? A missing endpoint fails the assembled product.
- FRUGALITY: is the fan-out minimal (prefer fewer, larger leaves)?
Reply with SHORT prose (no JSON): the concrete defects and the fix for each, or
"NO DEFECTS" if the draft is already correct."""

RECONCILE_PROMPT = """You are the RECONCILER of a Spec-Driven Development
decomposer team. Produce the FINAL decomposition for this node by applying the
critic's findings to the drafter's proposal — keep what is right, fix what the
critic flagged. Do NOT over-correct: an empty critique ("NO DEFECTS") means
return the draft unchanged.

Drafter's proposal (JSON):
{draft}

Critic's findings:
{critique}

Return ONLY the corrected JSON object in the SAME shape as the draft (atomic,
metrics, children[], depends_on, spike) — no prose, no markdown fence."""


def _decomposer_team() -> list[dict]:
    """The configured decomposer team (D2), or [] for the single-agent path.
    Env SPEC_FLOW_DECOMPOSER_TEAM (JSON) overrides the case YAML, mirroring the
    implementer team. Shapes: {specialists: [{role, model?}, ...]} or a bare
    list. Roles drive the flow; a role's `model` overrides the decomposer model
    for its step."""
    env = os.environ.get("SPEC_FLOW_DECOMPOSER_TEAM")
    raw = None
    if env is not None and env.strip():
        try:
            raw = json.loads(env)
        except (ValueError, TypeError):
            raw = None
    if raw is None:
        cfg = getattr(llm_backend, "WORKERS_CFG", None) or {}
        raw = (cfg.get("decomposer") or {}).get("team")
    if isinstance(raw, dict):
        raw = raw.get("specialists")
    if not isinstance(raw, list):
        return []
    team = []
    for item in raw:
        if isinstance(item, dict) and item.get("role"):
            team.append({"role": str(item["role"]),
                         "model": str(item.get("model") or "")})
        elif isinstance(item, str) and item:
            team.append({"role": item, "model": ""})
    return team


def _role_model(team: list[dict], role: str) -> str:
    """The model a team role declares for its step, or '' (the decomposer
    default model)."""
    for s in team:
        if s.get("role") == role:
            return s.get("model") or ""
    return ""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in LLM reply: {text[-300:]}")
    return json.loads(m.group(0))


def _solicit(prompt: str, nid: str, depth: int, *, model: str = "",
             step: str = "") -> dict:
    """One robust decomposition reply, parsed to a dict. A weak free model often
    returns malformed JSON on the first try; re-prompt with the exact failure
    instead of crashing the run on one bad node. Persistent failure is fatal."""
    attempts = config.env("DECOMPOSE_ATTEMPTS", int)
    last = None
    for i in range(attempts):
        p = prompt if i == 0 else (
            prompt + f"\n\nYour previous answer could not be parsed: {last}. "
            "Return ONLY a single valid JSON object, no prose, no markdown fence.")
        reply = _ask(p, meta={"node": nid, "depth": depth}, model=model,
                     step=step)
        try:
            return _extract_json(reply)
        except (ValueError, json.JSONDecodeError) as exc:
            last = repr(exc)[:160]
            llm_log.log_outcome(role="decomposer", node=nid, depth=depth,
                                parse="retry", attempt=i + 1, error=last)
    llm_log.log_outcome(role="decomposer", node=nid, depth=depth,
                        parse="fail", error=last)
    raise ValueError(f"decomposer failed after {attempts} attempts: {last}")


def _orchestra_decompose(ctx: dict, base_prompt: str, nid: str,
                         team: list[dict]) -> dict:
    """D2: drafter proposes, critic challenges, reconciler finalises — all via
    the single door. The canonical flow is drafter→critic→reconciler (driven by
    the declared roles). A critic that finds nothing, or a reconciler that fails
    to produce usable JSON, degrades to the draft, so the run never loses a
    valid decomposition to the refinement step."""
    p = ctx["project"]
    depth = ctx["depth"]
    roles = [s["role"] for s in team]
    llm_log.log({"event": "decompose_orchestra_start", "node": nid,
                 "steps": roles})
    # drafter: the same single-agent decomposition, robustly parsed
    draft = _solicit(base_prompt, nid, depth,
                     model=_role_model(team, "drafter"), step="drafter")
    if "critic" not in roles:
        return draft
    draft_json = json.dumps(draft, ensure_ascii=False)
    existing = "; ".join(f"{n['id']} ({n['title']})"
                         for n in (ctx.get("existing_nodes") or [])) or "—"
    critique = _ask(
        CRITIC_PROMPT.format(
            goal=p.get("goal", ""), target=p.get("target", ""),
            constitution="; ".join(p.get("constitution", [])),
            title=ctx["node"]["title"], id=nid, depth=depth,
            existing=existing, draft=draft_json),
        meta={"node": nid, "depth": depth},
        model=_role_model(team, "critic"), step="critic").strip()
    if ("reconciler" not in roles or not critique
            or "NO DEFECTS" in critique.upper()):
        return draft
    try:
        final = _solicit(
            RECONCILE_PROMPT.format(draft=draft_json, critique=critique),
            nid, depth, model=_role_model(team, "reconciler"),
            step="reconciler")
    except ValueError:
        return draft        # reconciler botched JSON -> keep the valid draft
    llm_log.log({"event": "decompose_orchestra_done", "node": nid,
                 "revised": final != draft})
    return final


def decompose(ctx: dict) -> dict:
    p = ctx["project"]
    ancestors = ctx.get("ancestors") or []
    existing = ctx.get("existing_nodes") or []
    existing_lines = "; ".join(
        f"{n['id']} ({n['title']})" for n in existing) or "—"
    prompt = PROMPT.format(
        goal=p.get("goal", ""), target=p.get("target", ""),
        constitution="; ".join(p.get("constitution", [])),
        title=ctx["node"]["title"], id=ctx["node"]["id"],
        depth=ctx["depth"], parent=ctx.get("parent") or "—",
        ancestors=" → ".join(ancestors) or "—",
        existing=existing_lines)
    if ctx["depth"] == 0:
        prompt += ROOT_RULE
    if ctx["depth"] >= _leaf_depth():
        prompt += LEAF_RULE.format(depth=ctx["depth"], leaf_depth=_leaf_depth())
    nid = ctx["node"]["id"]
    # D2: a configured decomposer team runs the drafter→critic→reconciler
    # orchestra; with NO team (the default) it is the single agent, unchanged.
    # A weak free model often returns malformed JSON on the first try; _solicit
    # re-prompts with the exact failure instead of crashing the run on one bad
    # node. Only a persistent failure is fatal. Mirrors llm_implementer.
    team = _decomposer_team()
    if team:
        out = _orchestra_decompose(ctx, prompt, nid, team)
    else:
        out = _solicit(prompt, nid, ctx["depth"])
    # keep only the keys the engine understands (incl. the atomicity judgment)
    keep = {k: out[k] for k in ("atomic", "metrics", "children", "spike", "clarify") if k in out}
    if ctx["depth"] >= _leaf_depth():
        keep.pop("children", None)          # convergence is enforced, not hoped for
        keep["atomic"] = True               # forced-leaf depth ⇒ declare atomic
    # bound fan-out so a wide tree cannot blow the call budget
    if keep.get("children") and len(keep["children"]) > _max_children():
        keep["children"] = keep["children"][:_max_children()]
    for child in keep.get("children", []) or []:
        child.pop("metrics", None)          # children are sized on their own visit
    children = [c.get("id", "?") for c in keep.get("children", []) or []]
    llm_log.log_outcome(role="decomposer", node=nid, depth=ctx["depth"],
                        atomic=keep.get("atomic"),
                        verdict="leaf" if not children else "branch",
                        children=children)
    return keep
