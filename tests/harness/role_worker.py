"""Real role workers — the production contract exercised WITHOUT Hermes.

A worker here is a headless ``claude -p`` session assembled from the
plugin's OWN shipped artifacts — exactly the parts a Hermes worker would
load, with Hermes itself out of the picture:

  * ``skills/<skill>/SKILL.md``        → the session system prompt, VERBATIM
    (the sim harness paraphrased it; here the real skill text is under test)
  * ``profiles/<role>/config.yaml``    → the role's tool policy, mapped to
    ``--allowedTools`` / ``--disallowedTools`` (a decomposer really cannot
    run shell commands; only the implementer can)
  * the run workspace                  → session cwd, so file tools operate
    on the real artifacts of this run

What this deliberately does NOT cover: the Hermes adapter layer (kanban
transport, dispatcher, toolset registration). That is "Hermes USES the
plugin" — out of scope for the standalone industrial test by design.

Env knobs:
  SPEC_FLOW_<ROLE>_MODEL / SPEC_FLOW_LLM_MODEL   per-role / shared model
  SPEC_FLOW_WORKER_TIMEOUT                        seconds per call (def 600)
  SPEC_FLOW_WORKER_RETRIES                        attempts per call (def 2)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import claims, claude_cli, config, llm_backend, llm_log, memory

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PLUGIN_ROOT / "skills"
PROFILES_DIR = PLUGIN_ROOT / "profiles"

# Phase-1 provider seam: a specialist names WHERE it runs. Only `local` (the
# built-in role_worker LLM call) runs in-process; the remote ones (hermes / a2a /
# mission-control) live in the ``providers`` package and run a RoleTask over the
# wire. Phase 3 dispatches non-local specialists through those adapters.
LOCAL_PROVIDER = "local"

# Injectable transport for the remote provider adapters (doc §4). Tests set this
# to a fake (url/method/body -> (status, text)); a live run leaves it None and
# the adapter uses its stdlib urllib transport. Module-global so a test can swap
# it WITHOUT threading a param through the unchanged ``_orchestra_run`` loop.
PROVIDER_TRANSPORT = None

TIMEOUT = config.env("WORKER_TIMEOUT", int)
RETRIES = config.env("WORKER_RETRIES", int)
# how much of a referenced file is inlined into a chat-only prompt
INLINE_FILE_LIMIT = config.env("INLINE_FILE_LIMIT", int)
PYTEST_TIMEOUT = config.env("PYTEST_TIMEOUT", int)

# Hermes toolset name → Claude Code tool names. ``kanban`` and ``memory``
# have no standalone counterpart (they ARE the Hermes adapter) — empty.
TOOLSET_MAP: dict[str, list[str]] = {
    "file": ["Read", "Write", "Edit", "Glob", "Grep"],
    "terminal": ["Bash"],
    "code_execution": ["Bash"],
    "web": ["WebSearch", "WebFetch"],
    "browser": ["WebFetch"],
    "delegation": ["Task"],
    "kanban": [],
    "memory": [],
}


def load_profile_policy(role: str) -> tuple[list[str], list[str]]:
    """(allowed, disallowed) Claude tool names from the role's REAL profile."""
    cfg = yaml.safe_load(
        (PROFILES_DIR / role / "config.yaml").read_text(encoding="utf-8")) or {}
    cli = (cfg.get("tools") or {}).get("cli") or {}
    allowed, disallowed = [], []
    for ts in cli.get("enabled") or []:
        allowed += TOOLSET_MAP.get(ts, [])
    for ts in cli.get("disabled") or []:
        disallowed += TOOLSET_MAP.get(ts, [])
    # an explicitly disabled toolset wins over an enabled overlap
    allowed = [t for t in dict.fromkeys(allowed) if t not in set(disallowed)]
    return allowed, list(dict.fromkeys(disallowed))


def load_skill_md(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def worker_language() -> str:
    """The working language for specs and in-plugin communication. Taken
    from SPEC_FLOW_LANG env or the case `workers.language`; default
    English. Centralised so every role speaks the configured language —
    a worker once asked its HITL question in Russian and the (English)
    auto-responder missed it."""
    env = os.environ.get("SPEC_FLOW_LANG")
    if env:
        return env.strip()
    cfg = getattr(llm_backend, "WORKERS_CFG", None) or {}
    return str(cfg.get("language") or "English").strip()


_LANG_DIRECTIVE = (
    "\n\n## Working language\n"
    "Write EVERYTHING you produce — specs, plans, code comments,"
    " identifiers, and any HITL question or reply — in {lang}. This is the"
    " plugin's configured working language (env SPEC_FLOW_LANG /"
    " workers.language). Do NOT switch to another language even if an"
    " input fragment uses one.")


def _with_language(system: str) -> str:
    """Append the configured-language directive to a role's system prompt."""
    return system + _LANG_DIRECTIVE.format(lang=worker_language())


def _model_for(role: str, specialty: str = "") -> str:
    # delegates to the shared per-role resolver: the case YAML `workers:`
    # block is the single source of truth when present (env vars apply only
    # without it); paid models stay forbidden — llm_backend gates ':free'.
    # #10: a node's specialty routes the role to a specialty-specific chain.
    return llm_backend.model_for(role, specialty)


def _chat_only() -> bool:
    """True when the backend is a plain chat API: no file/shell tools, so
    referenced files are inlined into prompts and the harness itself does
    the file writes and test runs."""
    return llm_backend.BACKEND == "openai"


def _granular_commits() -> bool:
    """Opt-in (default OFF): commit the workspace after the leaf's CREATE and
    after each REPAIR, not only once at leaf close. Gives a fine-grained git
    history of how a leaf converged. Enabled by env SPEC_FLOW_GRANULAR_COMMITS
    (1/true) or workers.granular_commits in the case YAML."""
    env = os.environ.get("SPEC_FLOW_GRANULAR_COMMITS")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    cfg = getattr(llm_backend, "WORKERS_CFG", None) or {}
    return bool(cfg.get("granular_commits"))


def _granular_commit(ws_root: str, fn: str, stage: str) -> None:
    """Stage + commit the leaf's files when granular commits are on. Best
    effort: a commit failure (nothing staged, no git) never breaks the leaf.
    Commits whatever ws_root is — under axis F that is the leaf's worktree,
    so the granular history rides the leaf branch and merges with it."""
    if not _granular_commits():
        return
    try:
        from . import ws_tx
        if not ws_tx.ensure_repo(ws_root):
            return
        ws_tx._git(ws_root, "add", "-A")
        rc, _ = ws_tx._git(ws_root, "diff", "--cached", "--quiet")
        if rc == 0:
            return                       # nothing staged — skip an empty commit
        ws_tx._git(ws_root, "-c", "user.name=spec-flow",
                   "-c", "user.email=tx@spec.flow",
                   "commit", "-qm", f"{stage}: {fn}")
        llm_log.log({"event": "granular_commit", "node": fn, "stage": stage})
    except Exception:                    # noqa: BLE001 — never breaks the leaf
        pass


def _provider_config(provider: str, role: str) -> dict:
    """The adapter config (endpoint/agent/auth) for the specialist that runs as
    ``role`` on ``provider`` — re-read from the SAME team config the orchestra
    resolved from, so no extra param threads through the unchanged loop.

    Why: the call path needs WHERE/HOW to reach the remote agent, which lives on
    the specialist record, not on the bare model/meta the orchestra passes.
    What: scans the configured implementer team for the matching specialist and
    returns its ``_PROVIDER_CFG_KEYS`` (endpoint/agent/api/token/…); empty when
    none matches (the adapter then raises a clear 'needs endpoint' error).
    Test: a team with {role:'coder', provider:'hermes', gateway, agent} yields
    that gateway+agent for ('hermes','coder')."""
    for spec in _implementer_team():
        if spec.get("provider") == provider and spec.get("role") == role:
            return {k: spec[k] for k in _PROVIDER_CFG_KEYS if k in spec}
    return {}


def _remote_call(provider: str, *, prompt: str, model: str,
                 role: Optional[str], specialty: str, cwd: Optional[str],
                 meta: Optional[dict]) -> str:
    """Route ONE specialist call to a remote provider adapter and render its
    RoleResult back into the local reply shape (a ``{"files": {...}}`` JSON
    string), so the orchestra parses it provider-blind.

    Why: this is the dispatch seam — ``provider != local`` runs on Hermes / MC /
    A2A instead of the LLM, yet returns what the local path returns.
    What: builds a RoleTask from the call args + the specialist's adapter config,
    calls ``providers.get_provider(provider).execute(task)``, and serialises the
    returned artifacts (or verdict) as the reply text.
    Test: with a fake transport, a 'hermes' specialist's step returns the canned
    files as a JSON ``{"files": {...}}`` reply and the orchestra writes them."""
    from .providers import get_provider
    m = meta or {}
    node = str(m.get("node") or "")
    # the SPECIALIST role (architect/coder/tester/fixer) is the orchestra step;
    # the orchestra threads the call's generic role as 'implementer', so the
    # adapter config is keyed on the step, falling back to the call role.
    step_role = str(m.get("step") or role or "")
    cfg = _provider_config(provider, step_role)
    task = RoleTask(
        role=step_role, node=node,
        title=str(m.get("title") or ""), spec=str(m.get("spec") or ""),
        workspace=cwd, specialty=specialty, provider=provider, model=model,
        params=None, context={"prompt": prompt, **cfg})
    adapter = get_provider(provider, transport=PROVIDER_TRANSPORT)
    result = adapter.execute(task)
    return _render_remote_reply(result)


def _render_remote_reply(result: Any) -> str:
    """A remote RoleResult → the reply string the local coder would emit.

    files → ``{"files": {path: content}}``; a verdict-only result →
    ``{"verdict": {...}}``; this keeps the orchestra's ``_extract_json`` step
    identical for local and remote specialists."""
    artifacts = getattr(result, "artifacts", None) or {}
    verdict = getattr(result, "verdict", None)
    if artifacts:
        return json.dumps({"files": dict(artifacts)})
    if verdict is not None:
        return json.dumps({"verdict": verdict})
    return json.dumps({"files": {}})


def _call_model(prompt: str, *, system: str, allowed: list[str],
                disallowed: list[str], cwd: Optional[str], model: str,
                role: Optional[str] = None, specialty: str = "",
                meta: Optional[dict] = None,
                params: Optional[dict] = None) -> str:
    """ONE door to the model for every role worker (free-pool rule lives in
    llm_backend). ``role`` resolves the case-configured model CHAIN — the
    tail entries answer when the primary's quota is exhausted. #10: a node's
    ``specialty`` routes the role to a specialty-specific chain. The
    claude-CLI path keeps the real tool-policy flags.

    ``params`` is a specialist's open-schema sampling config (temperature,
    max_tokens, …); it is forwarded to llm_backend.ask so the chat-backend
    request carries it. The claude-CLI path has no flag for these and ignores
    them — same as a solo worker with no params (today's behaviour).

    Every call is logged universally through llm_log.timed_ask with an
    open-schema ``meta`` (role, model, specialty + whatever the caller adds:
    step/mode/attempt/...), so request + outcome are recorded the same way for
    EVERY worker — solo or orchestra — and the usage analysis is multi-axis by
    construction. No caller needs its own call_start.

    Phase 3: when ``meta['provider']`` names a non-local provider, the call is
    routed to that provider's adapter (Hermes / Mission-Control / A2A) instead of
    the local LLM. The adapter's RoleResult is rendered back into the SAME reply
    string the local coder would produce (a ``{"files": {...}}`` JSON object), so
    the orchestra's downstream parsing (``_extract_json``) is provider-blind and
    ``_orchestra_run`` is unchanged. ``local`` is byte-for-byte today's path."""
    provider = str((meta or {}).get("provider") or LOCAL_PROVIDER)
    remote = provider != LOCAL_PROVIDER

    def _provider_call(p: str) -> str:
        # the remote adapter as a plain prompt->reply callable; ask() owns the
        # degrade-to-local-chain decision and all logging around it (Phase 2).
        return _remote_call(provider, prompt=p, model=model, role=role,
                            specialty=specialty, cwd=cwd, meta=meta)

    def _do(p: str) -> str:
        if _chat_only():
            step = (meta or {}).get("step", "")    # orchestra specialist tag
            fallbacks = llm_backend.chain_for(role, specialty)[1:] if role else ()
            # node/specialty ride into the SINGLE door's call_start logging;
            # provider (when remote) is folded into the door as the chain's
            # first link, so the old provider_fallback bypass here is gone.
            ask_meta = {"node": (meta or {}).get("node", ""),
                        "specialty": specialty}
            return llm_backend.ask(p, model=model, system=system,
                                   fallbacks=fallbacks or (), role=role or "",
                                   step=step, params=params, meta=ask_meta,
                                   provider=provider if remote else "",
                                   provider_call=_provider_call if remote else None)
        # non-chat (claude agentic CLI) path — now ALSO through the SINGLE door
        # (Phase 4): the tool policy + workspace ride into ask() as tools/cwd, so
        # the agentic session is one claude backend under the door with the same
        # call_start/ok logging, and the provider is the chain's first link just
        # like the chat path (no private provider_fallback bypass left).
        step = (meta or {}).get("step", "")
        ask_meta = {"node": (meta or {}).get("node", ""), "specialty": specialty}
        return llm_backend.ask(p, model=model, system=system,
                               role=role or "decomposer", step=step,
                               params=params, meta=ask_meta,
                               tools={"allowed": allowed,
                                      "disallowed": disallowed}, cwd=cwd,
                               provider=provider if remote else "",
                               provider_call=_provider_call if remote else None)

    # The SINGLE door (llm_backend.ask) logs call_start/ok/error itself, so the
    # chat path needs no wrapper. The claude-CLI branch is consolidated into the
    # door in Phase 4; for now it runs un-wrapped.
    return _do(prompt)


def _inline_file(root: Optional[str], rel: str) -> str:
    """The file's text for prompt embedding (chat-only mode), truncated."""
    try:
        text = (Path(root or ".") / rel).read_text(encoding="utf-8")
    except OSError:
        return "(file not found)"
    if len(text) > INLINE_FILE_LIMIT:
        text = text[:INLINE_FILE_LIMIT] + "\n…(truncated)"
    return text




# NOTE: the agentic worker session (formerly _run_claude) now lives in
# llm_backend._ask_claude as the claude backend — the non-chat path routes
# through the SINGLE door llm_backend.ask(..., tools=, cwd=) (Phase 4).


def _extract_json(text: str) -> dict:
    """The first complete JSON OBJECT anywhere in the reply.

    Models wrap JSON in prose, code fences, or lead with a '{' that is not
    the object start — try every candidate position instead of trusting the
    first brace (a single bad reply once crashed a whole run)."""
    text = re.sub(r"```(?:json)?", "", text)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in worker reply: {text[-300:]}")


_ASK_RULE = """
If (and ONLY if) a genuine blocker needs a HUMAN decision you cannot make
yourself, reply with ONLY {"question": "<one specific question>"} instead of
the normal output — you will be re-invoked with the human's answer."""

_NOTE_RULE = """

OPERATOR NOTE (the human watching this run addressed you — you MUST react):
{note}

Address it in your reply by ADDING the key
"operator_reply": {{"position": "comply"|"defend", "response": "<your answer>"}}
— "comply" = you accept it as a directive and your output reflects it;
"defend" = you keep your course and argue why. Then produce the normal
output as specified above."""


def _dialog_round(prompt: str, *, role: str, node: str, system: str,
                  allowed: list[str], disallowed: list[str],
                  cwd: Optional[str], model: str, channel: Any,
                  specialty: str = "", meta: Optional[dict] = None,
                  params: Optional[dict] = None) -> str:
    """One worker session + at most one human Q&A round.

    A reply consisting of {"question": ...} pauses the work, asks the human
    through the channel and re-runs the session with the answer appended.
    No channel / no answer → the worker is told to proceed on its own
    judgement and state its assumption. #10: ``specialty`` routes the call's
    fallback chain to a specialty-specific one. ``meta`` carries extra call
    descriptors (step, mode, attempt) for the universal call log. ``params``
    threads a specialist's sampling config through to the backend on both the
    first round and the post-question follow-up."""
    _m = dict(meta or {}); _m.setdefault("node", node)
    raw = _call_model(prompt, system=system, allowed=allowed,
                      disallowed=disallowed, cwd=cwd, model=model, role=role,
                      specialty=specialty, meta=_m, params=params)
    try:
        probe = _extract_json(raw)
    except ValueError:
        return raw
    question = str(probe.get("question", "")).strip()
    if not question or len(probe) > 1:
        return raw
    answer = channel.ask(role, node, question) if channel is not None else None
    if answer:
        memory.retain_project(
            f"Operator decision at node '{node}': Q: {question} A: {answer}",
            context=f"hitl answer to {role}", tags=["hitl"])
        followup = (f"{prompt}\n\nYOU ASKED: {question}\n"
                    f"HUMAN ANSWER: {answer}\n"
                    "Fold the answer into your work and produce the normal "
                    "output now (no more questions).")
    else:
        followup = (f"{prompt}\n\nYOU ASKED: {question}\n"
                    "No human answer arrived. Proceed on your own best "
                    "judgement, STATE the assumption you made, and produce "
                    "the normal output now (no more questions).")
    return _call_model(followup, role=role, system=system, allowed=allowed,
                       disallowed=disallowed, cwd=cwd, model=model,
                       specialty=specialty, meta={**_m, "followup": True},
                       params=params)


def _handle_operator_reply(out: dict, *, role: str, node: str,
                           note: Optional[str], channel: Any) -> dict:
    """Record the worker's comply/defend answer to an operator note."""
    reply = out.pop("operator_reply", None)
    if note and channel is not None:
        if isinstance(reply, dict) and str(reply.get("response", "")).strip():
            channel.record_reply(role, node,
                                 str(reply.get("position", "comply")).lower(),
                                 str(reply["response"]), note)
        else:
            channel.record_reply(role, node, "unaddressed",
                                 "(worker did not address the note)", note)
    return out


def _ws_root(ws: Any) -> Optional[str]:
    root = getattr(ws, "root", None) if not isinstance(ws, str) else ws
    return str(root) if root else None


# ── decomposer (skill: spec-flow-decompose, profile: spec-decomposer) ───

_DECOMPOSE_TASK = """You are running the skill above as the spec-decomposer
worker for ONE node of a Spec-Driven Development tree.

Project goal: {goal}
Measurable target: {target}
Constitution (non-negotiable): {constitution}

Current node: "{title}" (id: {id}, depth: {depth}, parent: {parent})
Ancestor chain (root → parent): {ancestors}
Nodes ALREADY created elsewhere in the tree: {existing}

Apply the skill: judge atomicity first, then size the node. NEVER propose a
child that repeats work already covered by an existing node (any branch) —
reference it via "depends_on" instead.

Child id rule: a short snake_case slug naming the WORK ITSELF
(e.g. catalog_schema, seller_onboarding). Do NOT encode phase/type into the
id — no req-/spec-/task-/research- prefixes and no kebab-case: the engine
already tracks phases, prefixes only pollute ids and dedup.

Reply with ONLY a JSON object (no prose, no fence):
{{"atomic": true|false,
  "metrics": {{"modules": n, "tasks": n, "interfaces": n, "estimated_loc": n,
              "open_decisions": n, "single_concern": bool, "testable_criteria": bool}},
  "children": [{{"id": "snake_case", "title": "short", "depends_on": ["sibling_id"]}}],
  "depends_on": ["existing-node-id"],
  "spike": {{"question": "...", "recommendation": "..."}},
  "spec_markdown": "<markdown, see below>"}}
Omit "children"/"depends_on"/"spike" when not applicable; atomic=true means
NO children and metrics within: modules<=1, tasks<=5, interfaces<=2,
estimated_loc<=100, open_decisions==0.
Decompose by product FEATURE, never by test phase: do NOT create nodes for
end-to-end scenarios, smoke harnesses, test fixtures or 'integration
testing' — the platform already owns the assembled-product check, and a
duplicate of it among regular leaves fails every partial build.
COVERAGE IS BINDING: when the constitution freezes an API contract, EVERY
endpoint of it must be owned by exactly one leaf in your decomposition —
a missing endpoint means the assembled product fails its acceptance and
nobody else will add it later.

"spec_markdown" is REQUIRED — you AUTHOR this level's specification (the
engine only adds its deterministic header). It must contain exactly these
sections:
  ## Requirements — 2-6 EARS-style requirements, each with an id
     REQ-{id}-1..n, each independently testable
  ## Scope — 'In:' and 'Out:' bullet lists (explicit boundary)
  ## Open decisions — numbered list, or 'none'
  ## Acceptance criteria — measurable checks tied to the REQ ids
Keep it under 60 lines. Escape newlines as \n inside the JSON string."""

def _leaf_depth() -> int:
    # a case workers block overrides the .test.env floor per run
    return int(llm_backend.WORKERS_CFG.get("leaf_depth")
               or config.env("LLM_LEAF_DEPTH", int))


def _max_children() -> int:
    return int(llm_backend.WORKERS_CFG.get("max_children")
               or config.env("LLM_MAX_CHILDREN", int))


def make_decomposer(workspace_dir: Optional[str] = None,
                    channel: Any = None) -> Callable[[dict], dict]:
    system = _with_language(load_skill_md("spec-flow-decompose"))
    allowed, disallowed = load_profile_policy("spec-decomposer")
    model = _model_for("decomposer")

    def decompose(ctx: dict) -> dict:
        p = ctx["project"]
        nid = ctx["node"]["id"]
        existing = "; ".join(
            f"{n['id']} ({n['title']})" for n in ctx.get("existing_nodes") or []) or "—"
        prompt = _DECOMPOSE_TASK.format(
            goal=p.get("goal", ""), target=p.get("target", ""),
            constitution="; ".join(p.get("constitution", [])),
            title=ctx["node"]["title"], id=nid,
            depth=ctx["depth"], parent=ctx.get("parent") or "—",
            ancestors=" → ".join(ctx.get("ancestors") or []) or "—",
            existing=existing) + _ASK_RULE
        feedback = str(ctx.get("review_feedback") or "").strip()
        prev_spec = str(ctx.get("previous_spec") or "").strip()
        if feedback and prev_spec:
            # minimal-edit rework: re-authoring from scratch re-rolls the
            # dice on every id and section — the reviewer's point about ONE
            # missing line then survives whole rework budgets (the
            # product_discovery class)
            feedback_block = (
                "\n\nYOUR PREVIOUS SPEC (verbatim):\n" + prev_spec +
                "\n\nMake the MINIMAL edit to the text above that fixes"
                " EVERY reason below — keep all ids, numbering and"
                " untouched sections exactly as they are, do NOT rewrite"
                " from scratch.")
        else:
            feedback_block = ""
        if feedback:
            prompt += (feedback_block +
                       "\n\nREWORK ROUND — the reviewer REJECTED the previous"
                       " version of this node's spec. The exact reasons:\n"
                       + feedback +
                       "\nThe node's atomicity and children are already"
                       " decided and MUST NOT change. Re-author ONLY the"
                       " specification text fixing EVERY point above —"
                       " split coupled requirements, cover each REQ with an"
                       " acceptance criterion, keep REQ ids stable."
                       "\nIf the reviewer demands open decisions be CLOSED,"
                       " actually decide: pick the option that best serves"
                       " the project goal and constitution, state the choice"
                       " and its rationale in the spec, and keep '## Open"
                       " decisions' honest — list ONLY what truly remains"
                       " open ('None — <why>' when nothing does)."
                       "\nReply with ONLY: {\"spec_markdown\": \"...\"}")
        parent_id = ctx.get("parent_id")
        if parent_id:
            trace_rule = (" Every requirement you author MUST carry"
                          f" 'Traces-to: REQ-{parent_id}-n' pointing at the"
                          " parent requirement it refines.")
            # Workspace.spec snake_cases filenames — 'L0' lives at
            # specs/l0.md; the case-sensitive miss once stalled a worker
            spec_rel = ("specs/"
                        + re.sub(r"\W+", "_", parent_id).strip("_").lower()
                        + ".md")
            if _chat_only():
                prompt += (f"\n\nParent approved spec ({spec_rel}):"
                           "\n---\n"
                           + _inline_file(workspace_dir, spec_rel)
                           + "\n---\n" + trace_rule)
            else:
                prompt += (f"\n\nParent approved spec: {spec_rel} —"
                           " READ it first (Read tool, relative to the"
                           " current directory)." + trace_rule)
        if ctx["depth"] >= _leaf_depth():
            prompt += (f"\n\nHARD CONSTRAINT: depth {ctx['depth']} >= "
                       f"{_leaf_depth()} — this node MUST be atomic (no children).")
        from . import repo_map
        rmap = repo_map.build_map(workspace_dir, subdirs=("src",))
        if rmap:
            prompt += ("\n\nREPOSITORY MAP — modules that ALREADY exist"
                       " (routes, schemas, signatures). Plan children that"
                       " complement this surface; never plan a node that"
                       " duplicates an existing route or table:\n" + rmap)
        prompt += memory.recall_block_for(
            "decomposer",
            f"{ctx['node'].get('title', nid)}:"
            f" {ctx['project'].get('goal', '')}")
        branch_capable = ctx["depth"] < _leaf_depth()
        # standing human requirements (possibly added MID-RUN) bind every
        # branch decomposition — a late requirement enters the tree here
        # (getattr: transport channels may predate this capability)
        reqs_fn = getattr(channel, "standing_requirements", None) \
            if channel is not None else None
        # normalize (name, statement[, scope]) — scope is ENGINE business,
        # the worker only needs awareness text
        reqs = [(item[0], item[1]) for item in (reqs_fn() if reqs_fn else [])]
        own_req = next((t for n, t in reqs if n == nid), None)
        if own_req:
            prompt += ("\n\nTHIS NODE EXISTS to satisfy a standing HUMAN"
                       " REQUIREMENT (added mid-run; binding). Author a spec"
                       " that covers it COMPLETELY — every page, element and"
                       " behaviour below is an acceptance criterion:\n"
                       + own_req)
        elif reqs and branch_capable:
            # awareness ONLY — placement belongs to the engine: a late
            # requirement becomes a ROOT-level node (its acceptance runs on
            # the assembled product), never a child of whatever branch
            # happened to decompose next (v11 proved branches obey and
            # swallow cross-cutting scope into the wrong subtree)
            prompt += ("\n\nSTANDING HUMAN REQUIREMENTS (context). Each is"
                       " materialized by the ENGINE as a separate root-level"
                       " node — do NOT create a child for it and do NOT"
                       " duplicate its scope; just avoid conflicting with"
                       " it:\n" + "\n".join(
                           f"- [{n}] {t}" for n, t in reqs))
        note = channel.poll_note(branch_capable=branch_capable) \
            if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        raw = _dialog_round(prompt, role="decomposer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=workspace_dir, model=model, channel=channel)
        try:
            parsed = _extract_json(raw)
        except ValueError:
            # prose instead of JSON: one strict re-ask (the implementer's
            # discipline) — a second failure raises and the ENGINE
            # surrenders the node, not the run
            raw = _call_model(
                prompt + "\n\nYOUR PREVIOUS REPLY WAS NOT VALID JSON."
                " Reply with ONLY the JSON object, no prose, no fence.",
                system=system, allowed=allowed, disallowed=disallowed,
                cwd=workspace_dir, model=model, role="decomposer")
            parsed = _extract_json(raw)
        out = _handle_operator_reply(parsed, role="decomposer",
                                     node=nid, note=note, channel=channel)
        if ctx["depth"] >= _leaf_depth():
            out.pop("children", None)
        if out.get("children"):
            out["children"] = out["children"][:_max_children()]
        # canonical role name + children ids — the live dashboard rebuilds
        # the growing tree from exactly these fields; ``worker`` marks the
        # real-worker (skill+profile) origin
        llm_log.log_outcome(role="decomposer", worker=True, node=nid,
                            depth=ctx["depth"], model=model,
                            atomic=bool(out.get("atomic")),
                            children=[c.get("id") for c in out.get("children") or []],
                            prompt=prompt, reply=raw, ok=True)
        return out

    return decompose


# ── creator ensemble (2-3 agents in the implementer role) ────────────────
# A weak free model emits code that won't even compile (a non-ASCII '…' or a
# truncation) and the error only surfaces at integrate. An ensemble generates
# several candidates from DIFFERENT free models and keeps the first that
# passes a deterministic compile gate (and, when several pass, a cross-review
# picks the best), so the broken candidate never reaches the workspace. Off by
# default (size 1 == today's single call); set SPEC_FLOW_CREATOR_ENSEMBLE=2..4.


def _ensemble_size() -> int:
    try:
        n = int(llm_backend.WORKERS_CFG.get("creator_ensemble")
                or config.env("CREATOR_ENSEMBLE", int))
    except (ValueError, TypeError):
        n = 1
    return max(1, min(n, 4))


def _candidate_compiles(raw: str) -> "tuple[bool, int, str]":
    """Deterministic gate on a candidate reply: every .py file it returns must
    compile and be pure ASCII. Returns (clean, n_files, reason)."""
    try:
        files = _extract_json(raw).get("files") or {}
    except ValueError:
        return (False, 0, "unparseable reply")
    if not files:
        return (False, 0, "no files")
    for path, content in files.items():
        if not str(path).endswith(".py") or not isinstance(content, str):
            continue
        bad = next((c for c in content if ord(c) > 127), "")
        if bad:
            return (False, len(files),
                    f"{path}: non-ASCII {bad!r} (U+{ord(bad):04X})")
        try:
            compile(content, str(path), "exec")
        except SyntaxError as e:
            return (False, len(files), f"{path}: {str(e)[:60]}")
    return (True, len(files), "")


def _ensemble_generate(prompt: str, *, node: str, system: str, allowed: list,
                       disallowed: list, cwd: str, model: str, channel: Any,
                       specialty: str, meta: Optional[dict] = None,
                       params: Optional[dict] = None) -> str:
    """Generate up to N candidate implementations from different free models
    and return the reply of the first that compiles clean (else the best by
    (compiles, n_files)). Stops early on the first clean candidate to spare
    the free-pool quota. N==1 is exactly the legacy single call. ``meta`` is
    forwarded to the universal call log (step/mode/...). ``params`` carries a
    specialist's sampling config to every candidate call."""
    n = _ensemble_size()
    if n <= 1:
        return _dialog_round(prompt, role="implementer", node=node,
                             system=system, allowed=allowed,
                             disallowed=disallowed, cwd=cwd, model=model,
                             channel=channel, specialty=specialty, meta=meta,
                             params=params)
    chain = llm_backend.chain_for("implementer", specialty) or [model]
    best = None     # (score_tuple, raw)
    for i in range(n):
        m = chain[i % len(chain)]
        raw = _dialog_round(prompt, role="implementer", node=node,
                            system=system, allowed=allowed,
                            disallowed=disallowed, cwd=cwd, model=m,
                            channel=channel, specialty=specialty,
                            meta={**(meta or {}), "candidate": i},
                            params=params)
        clean, nfiles, why = _candidate_compiles(raw)
        # candidate EVALUATION marker (engine decision), not a call log — the
        # call itself was logged once by timed_ask. No `model` here so it is
        # never miscounted as a request.
        llm_log.log({"event": "creator_candidate", "node": node,
                     "idx": i, "clean": clean, "files": nfiles,
                     "reason": why[:80]})
        score = (1 if clean else 0, nfiles)
        if best is None or score > best[0]:
            best = (score, raw)
        if clean:
            llm_log.log({"event": "creator_ensemble", "node": node,
                         "chosen_model": m, "candidates": i + 1, "clean": True})
            return raw
    llm_log.log({"event": "creator_ensemble", "node": node,
                 "candidates": n, "clean": False})
    return best[1]


# ── RoleTask / RoleResult contract (doc §3) ─────────────────────────────
# The uniform seam every executor speaks: the engine hands a specialist a
# RoleTask and gets back a RoleResult — provider-independent, so the same
# observability and workspace write-back work whether the work ran locally
# (this phase) or, later, on a remote Hermes/A2A/MC agent. stdlib only (NO
# `from tests.harness import` at plugin import — these dataclasses are the
# layering-safe runtime contract). Phase 1 routes the IMPLEMENTER orchestra
# through it internally, with NO observable change.
# TODO(phase 2+): route the decomposer / reviewer / verifier through the same
# contract; they still take their legacy ctx dict for now.


@dataclass
class RoleTask:
    """Engine → executor: one unit of role work, transport-agnostic.

    Why: a single shape lets a Team compose child tasks and (later) a remote
    adapter serialise/deserialise without the engine branching on provider.
    What: carries the role, the node identity, free-form context/handoff, the
    workspace path (local) and constraints.
    Test: build one for a specialist and assert role/node/specialty/params
    round-trip (tests/workers/test_specialist_config.py)."""
    role: str
    node: str
    title: str = ""
    spec: str = ""
    workspace: Optional[str] = None
    specialty: str = ""
    provider: str = LOCAL_PROVIDER
    model: str = ""
    params: Optional[dict] = None
    skill: Optional[str] = None
    context: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)


@dataclass
class RoleResult:
    """Executor → engine: the uniform outcome of a RoleTask.

    Why: every adapter returns this same record, so write-back and the call
    log are provider-independent.
    What: `kind` (files|spec|verdict), the produced `artifacts`, an optional
    `verdict`, and open-schema `meta` (provider, model, step, …).
    Test: assert an orchestra step builds a RoleResult whose meta names the
    provider+step it ran."""
    kind: str
    artifacts: dict = field(default_factory=dict)
    verdict: Optional[dict] = None
    meta: dict = field(default_factory=dict)


def _specialist_task(step: dict, ctx: dict, nid: str, fn: str,
                     ws_root: str) -> RoleTask:
    """Build the RoleTask for one orchestra specialist from its config + the
    leaf ctx. Resolves the provider (phase-1 gate) and the step's model/params
    so the call path reads them off ONE record.
    Test: a {role:'coder', params:{...}} step yields a RoleTask with provider
    'local', the resolved model and those params."""
    specialty = str(ctx.get("specialty", "") or "")
    provider = _resolve_provider(step)          # raises for non-local (phase 3)
    return RoleTask(
        role=str(step["role"]), node=nid, title=ctx.get("title", ""),
        spec=ctx.get("spec", ""), workspace=ws_root, specialty=specialty,
        provider=provider, model=_step_model(step, specialty),
        params=_step_params(step), skill=step.get("skill"),
        context={"module": fn})


# ── implementer orchestra (D1) ──────────────────────────────────────────
# Off by default: with no `team` config the implementer is the SINGLE-agent
# path, byte-for-byte unchanged. When a `team` list IS configured the leaf is
# produced by a sequence of sub-roles run in order with handoffs — architect →
# coder → tester → fixer — each step's structured output fed to the next. The
# orchestra ORCHESTRATES the same building blocks (_ensemble_generate,
# _write_reply_files, _leaf_bar, _apply_diff_repair, ws_tx); it never
# duplicates them. Config: workers.implementer.team.specialists (canonical) or
# the legacy bare list workers.implementer.team, or env
# SPEC_FLOW_IMPLEMENTER_TEAM (JSON). Each specialist routes through a RoleTask
# (the §3 contract) so a future provider adapter slots in without touching the
# orchestra loop.


def _normalise_specialist(item: Any) -> Optional[dict]:
    """One team entry → the canonical specialist record, or None if it has no
    role. The schema is `{role, provider='local', model?, params?, skill?}`.

    Why: phase 1 forward-declares the provider seam — every specialist carries
    a provider (default 'local'); a non-local provider is PARSED here but the
    orchestra raises NotImplementedError when it tries to run it, so a case YAML
    can already name `provider: hermes` and fail with a clear phase-3 message
    rather than silently ignoring it.
    Test: a bare {role} yields provider 'local'; {role, params:{...}} round-trips
    the params; an item with no role yields None."""
    if not isinstance(item, dict) or not item.get("role"):
        return None
    params = item.get("params")
    rec = {"role": str(item["role"]),
           "provider": str(item.get("provider") or LOCAL_PROVIDER),
           "skill": item.get("skill"),
           "model": item.get("model"),
           "params": dict(params) if isinstance(params, dict) else None}
    # Phase 3: a remote specialist also carries its adapter config (where/how to
    # reach the agent). These keys are transport/auth details — kept verbatim so
    # the provider adapter reads them at call time, never leaked into the prompt.
    for key in _PROVIDER_CFG_KEYS:
        if key in item:
            rec[key] = item[key]
    return rec


# Adapter-config keys a non-local specialist may carry (doc §4). They live on
# the specialist record and are forwarded to the provider adapter — endpoint/
# agent/auth, never sampling. Capability-named: the role schema names WHERE, the
# adapter owns the transport.
_PROVIDER_CFG_KEYS = ("agent", "agent_template", "endpoint", "gateway", "api",
                      "api_key", "token", "agent_card", "poll_attempts")


def _implementer_team() -> list[dict]:
    """The configured implementer team (orchestra), or [] for the default
    single-agent path. Env SPEC_FLOW_IMPLEMENTER_TEAM (JSON) overrides the case
    YAML for testability, mirroring how the other knobs read env first.

    Two shapes are accepted (the new one is canonical, the old one an alias):
      * NEW:  implementer.team.specialists: [ {role, provider?, model?,
              params?, skill?}, ... ]   — each specialist carries its own model
              and open-schema params (temperature/max_tokens/…).
      * OLD:  implementer.team: [ {role, model?, skill?}, ... ]   — the bare
              list form keeps working unchanged (provider defaults to local).
    Test: a `specialists:` block and the old list form both yield the same role
    sequence; per-specialist model/params survive the round-trip."""
    env = os.environ.get("SPEC_FLOW_IMPLEMENTER_TEAM")
    raw: Any = None
    if env is not None and env.strip():
        try:
            raw = json.loads(env)
        except (ValueError, TypeError):
            raw = None
    if raw is None:
        cfg = getattr(llm_backend, "WORKERS_CFG", None) or {}
        raw = (cfg.get("implementer") or {}).get("team")
    # canonical {team: {specialists: [...]}} (a dict) OR the alias bare list.
    # A JSON env value may carry either shape, so unwrap dict here too.
    return _normalise_team(raw)


def _normalise_team(raw: Any) -> list[dict]:
    """Normalise a raw team value into a specialist LIST. Accepts the canonical
    `{specialists: [...]}` dict and the legacy bare list — a JSON env value, a
    WORKERS_CFG block, or a per-leaf executor team (C1) all reach here. Anything
    else -> []."""
    if isinstance(raw, dict):
        raw = raw.get("specialists")
    if not isinstance(raw, list):
        return []
    team = []
    for item in raw:
        spec = _normalise_specialist(item)
        if spec is not None:
            team.append(spec)
    return team


def _resolve_provider(step: dict) -> str:
    """A step's provider NAME, validated against the adapter registry.

    Why: `local` is the built-in LLM call; every other name must resolve to a
    registered remote adapter (phase 3) — an unknown provider must fail LOUDLY
    here, not be silently skipped.
    What: returns the provider name unchanged for `local`; for a non-local name
    it confirms an adapter exists (``providers.get_provider``) and returns the
    name (the orchestra reads it off the RoleTask; the actual adapter is looked
    up at call time in ``_call_model``).
    Test: 'hermes' returns 'hermes'; 'bogus' raises a clear error naming the
    available providers."""
    provider = str(step.get("provider") or LOCAL_PROVIDER)
    if provider == LOCAL_PROVIDER:
        return provider
    from .providers import get_provider          # local import: layering
    get_provider(provider)                       # raises ValueError if unknown
    return provider


def _step_model(step: dict, specialty: str) -> str:
    """A team step's model: its declared `model`, else the implementer chain
    head for this specialty (the same resolver the single path uses)."""
    return str(step.get("model") or llm_backend.model_for("implementer", specialty))


def _step_params(step: dict) -> Optional[dict]:
    """A team step's open-schema sampling params (temperature/max_tokens/…),
    or None when the specialist declares none (then the backend uses its
    defaults, byte-for-byte today's behaviour)."""
    params = step.get("params")
    return dict(params) if isinstance(params, dict) and params else None


def _step_system(step: dict, default_system: str) -> str:
    """A team step's system prompt: its declared skill SKILL.md when given,
    else a short inline role prompt layered on the implementer's own system."""
    skill = step.get("skill")
    if skill:
        try:
            return _with_language(load_skill_md(str(skill)))
        except OSError:
            pass            # missing skill md → fall back to the role prompt
    role = step.get("role", "")
    inline = _ORCHESTRA_ROLE_PROMPTS.get(
        role, f"You act as the '{role}' sub-role of the implementer team.")
    return _with_language(default_system + "\n\n## Orchestra sub-role\n" + inline)


_ORCHESTRA_ROLE_PROMPTS = {
    "architect": (
        "Produce a SHORT interface plan for this leaf's module: the public"
        " functions/classes with signatures, the files to create, and how it"
        " wires into the assembled app. Plain text, no code, no file writes."),
    "coder": (
        "Implement the module and its tests following the architect's plan"
        " above and the spec. Reply with ONLY the files JSON."),
    "tester": (
        "Strengthen the leaf's tests so every acceptance criterion of the spec"
        " is covered, consistent with the architect interface. Reply with ONLY"
        " the files JSON (you may return only tests/test_<fn>.py)."),
    "fixer": (
        "The tests fail. Repair with the SMALLEST possible edit (SEARCH/REPLACE"
        " diff blocks against the current files)."),
}

_ARCHITECT_TASK = """You are the ARCHITECT sub-role of the implementer team for
ONE leaf of a Spec-Driven Development run.

Leaf: "{title}" (id: {id})
Its approved spec ({spec}):
---
{spec_body}
---

Produce a short interface/plan for src/{fn}.py: the public functions/classes
(with signatures), the files to create, and how it wires into the assembled
app. Text only — do NOT write any files, do NOT emit code blocks. Keep it
under 25 lines; the coder will implement against it."""


def _orchestra_run(ctx: dict, ws_root: str, nid: str, fn: str, *,
                   system: str, allowed: list, disallowed: list,
                   channel: Any, team: list[dict]) -> Any:
    """Run the declared implementer team in order, threading a shared handoff
    context between steps. Reuses the SAME machinery as the single path — git
    transaction, _write_reply_files, _leaf_bar, _apply_diff_repair, memory
    retain, llm_log. A failing sub-step never crashes the run differently than
    today: it is logged and skipped, and the leaf is still verified by the same
    two-tier _leaf_bar gate at the end."""
    from . import pytest_verifier as pv
    from . import ws_tx
    specialty = str(ctx.get("specialty", "") or "")
    ws = ctx["workspace"]
    spec_body = _inline_file(ws_root, ctx.get("spec", ""))
    base_prompt = _IMPLEMENT_CHAT_TASK.format(
        title=ctx["title"], id=nid, spec=ctx.get("spec", ""),
        spec_body=spec_body, fn=fn)
    handoff: dict[str, Any] = {"architect_plan": "", "test_output": ""}
    passed, test_out, wrote = False, "(no files written)", False
    baseline = 0
    # Resolve every specialist into a RoleTask BEFORE any LLM call (doc §3).
    # This validates the team up front — a non-local provider raises
    # NotImplementedError here, OUT of the per-step try/except, so an
    # unsupported provider fails loudly (phase-3 message) instead of being
    # logged-and-skipped like a transient step error.
    tasks = [_specialist_task(step, ctx, nid, fn, ws_root) for step in team]
    # Phase 2: the STEP ORDER is now declarative. A team with no `workflow:`/
    # `process:` (today's bare list) drives the specialists sequentially — the
    # exact architect->coder->tester->fixer order, byte-for-byte. A `workflow:`
    # graph drives loops/branches (e.g. tester<->fixer until green). The driver
    # decides the next ROLE; the per-role branch bodies below stay as-is.
    from . import orchestra_workflow as owf
    wf_spec, proc_spec = owf.workflow_of(
        owf.team_config_raw(llm_backend, os.environ))
    plan = owf.WorkflowPlan.from_team(team, workflow=wf_spec, process=proc_spec)
    state = owf.WorkflowState()
    # Map each role to its resolved RoleTask. Sequential keeps positional order
    # (duplicate roles consume in declaration order); a graph looks a role up.
    by_role: dict[str, list] = {}
    for t in tasks:
        by_role.setdefault(t.role, []).append(t)
    llm_log.log({"event": "orchestra_start", "node": nid,
                 "steps": [t.role for t in tasks],
                 "providers": [t.provider for t in tasks],
                 "flow": "graph" if not plan.is_sequential else "sequential"})

    for step_role in plan.run(state):
        queue = by_role.get(step_role)
        if not queue:
            continue                    # graph names a role with no specialist
        # Sequential pops in declaration order; a graph re-runs the same task
        # for a looped role (tester revisited), so peek without exhausting.
        task = queue.pop(0) if plan.is_sequential else queue[0]
        role = task.role
        s_model = task.model
        s_params = task.params
        s_system = _step_system({"skill": task.skill, "role": role}, system)
        # the call is logged once by timed_ask; the step/mode travel as call
        # meta so usage is sliceable by orchestra step without a second log.
        # node+title travel on EVERY step's call meta so each orchestra call
        # (incl. fixer, which goes through _call_model) is grouped under its real
        # node in the flow/sequence views — never under «None».
        step_meta = {"step": role, "mode": "orchestra",
                     "provider": task.provider,
                     "node": nid, "title": ctx.get("title", "") or nid}
        try:
            if role == "architect":
                prompt = _ARCHITECT_TASK.format(
                    title=ctx["title"], id=nid, spec=ctx.get("spec", ""),
                    spec_body=spec_body, fn=fn)
                handoff["architect_plan"] = _dialog_round(
                    prompt, role="implementer", node=nid, system=s_system,
                    allowed=allowed, disallowed=disallowed, cwd=ws_root,
                    model=s_model, channel=channel, specialty=specialty,
                    meta=step_meta, params=s_params)

            elif role in ("coder", "tester"):
                prompt = base_prompt
                if handoff["architect_plan"]:
                    prompt += ("\n\nARCHITECT PLAN (build to this interface):\n"
                               + handoff["architect_plan"])
                if role == "tester":
                    prompt += ("\n\nTESTER PASS — strengthen the tests to cover"
                               " every acceptance criterion of the spec.")
                raw = _ensemble_generate(
                    prompt, node=nid, system=s_system, allowed=allowed,
                    disallowed=disallowed, cwd=ws_root, model=s_model,
                    channel=channel, specialty=specialty, meta=step_meta,
                    params=s_params)
                try:
                    out = _extract_json(raw)
                except ValueError:
                    out = {}
                with ws_tx.transaction(ws_root, f"leaf:{nid}",
                                       f"orchestra {role}"):
                    base_passed, base_out = pv.run_suite(
                        ws_root, include_smoke=False)
                    baseline = pv._badness(base_passed, base_out)
                    if _write_reply_files(ws, out.get("files") or {}, fn):
                        wrote = True
                        passed, test_out = _leaf_bar(ws_root, fn, baseline, pv)
                        handoff["test_output"] = test_out

            elif role == "fixer":
                if passed or not wrote:
                    continue                # nothing to fix
                cur_src = _inline_file(ws_root, f"src/{fn}.py") or ""
                cur_test = _inline_file(ws_root, f"tests/test_{fn}.py") or ""
                repair = (base_prompt + "\n\n" + _REPAIR_DIFF_TASK.format(
                    output=handoff["test_output"], fn=fn,
                    src=cur_src, test=cur_test))
                raw2 = _call_model(
                    repair, system=s_system, allowed=allowed,
                    disallowed=disallowed, cwd=ws_root, model=s_model,
                    role="implementer", specialty=specialty, meta=step_meta,
                    params=s_params)
                with ws_tx.transaction(ws_root, f"leaf:{nid}",
                                       "orchestra repair"):
                    if _apply_diff_repair(ws, ws_root, fn, raw2):
                        passed, test_out = _leaf_bar(ws_root, fn, baseline, pv)
                    else:
                        try:
                            out2 = _extract_json(raw2)
                        except ValueError:
                            out2 = {}
                        if _write_reply_files(ws, out2.get("files") or {}, fn):
                            passed, test_out = _leaf_bar(
                                ws_root, fn, baseline, pv)
                handoff["test_output"] = test_out
        except Exception as exc:           # noqa: BLE001 — a sub-step never
            # crashes the run differently than the single path: log and skip
            llm_log.log({"event": "orchestra_step_error", "node": nid,
                         "role": role, "error": str(exc)[:150]})
            continue
        finally:
            # Feed the just-run step's outcome back so the driver's conditional
            # edges (tests_passed / tests_failed / retry) resolve the next role.
            state.passed, state.wrote = passed, wrote

    # the SAME completion contract as the single path: a green leaf is
    # remembered, a red one teaches; the leaf is judged by its artifacts.
    if passed:
        memory.retain_role(
            "implementer",
            f"Leaf '{nid}' ({ctx.get('title', '')}) landed green via orchestra"
            f" as src/{fn}.py with its own tests and no suite regression.",
            context="green leaf", tags=["leaf"])
    elif wrote:
        memory.retain_role(
            "implementer",
            f"Leaf '{nid}' ({ctx.get('title', '')}) orchestra surrendered RED;"
            f" failing output tail: {test_out[-300:]}",
            context="red leaf", tags=["fail"])
    llm_log.log_outcome(role="implementer", worker=True, node=nid, depth=-1,
                        model=_model_for("implementer", specialty),
                        ok=True, tests_passed=passed,
                        test_output=test_out[-300:], orchestra=True)
    return None     # the engine judges by the artifacts, not the reply


_IMPLEMENT_TASK = """You are running the skill above as the implementer
worker for ONE leaf of a Spec-Driven Development run.

Leaf: "{title}" (id: {id})
Its approved spec: {spec} (read it first).
Write the implementation to src/{fn}.py and the tests to tests/test_{fn}.py
(both paths relative to the current directory). Follow the skill: TDD —
write the failing test first, then the minimal implementation, then make it
pass by RUNNING the tests. Keep to the spec's scope; no extra features.
When done reply with ONLY: {{"done": true, "files": ["src/{fn}.py",
"tests/test_{fn}.py"], "tests_passed": true|false}}"""


_IMPLEMENT_CHAT_TASK = """You are running the skill above as the implementer
worker for ONE leaf of a Spec-Driven Development run.

Leaf: "{title}" (id: {id})
Its approved spec ({spec}):
---
{spec_body}
---

Write the implementation and its tests. Conventions (already used by the
project): code at src/{fn}.py, tests at tests/test_{fn}.py; the test file
inserts ../src into sys.path and imports the module by name:
    import sys; from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from {fn} import ...
TDD discipline: the tests must cover every acceptance criterion of the spec;
the implementation must be the MINIMUM that makes them pass. Standard library
only — no third-party imports. Keep to the spec's scope; no extra features.

REAL CODE ONLY — STUBS, MOCKS AND SMOKE ARE FORBIDDEN (the build is mechanically
checked and a violation REJECTS the leaf):
  * NO stub bodies — a public function/handler whose body is just `pass`, `...`,
    `raise NotImplementedError`, a lone docstring or a `# TODO` is not a product.
    Every function does its real work now.
  * NO mocking a LOCAL module — never `unittest.mock` / Mock / MagicMock /
    monkeypatch a sibling src module to fake the product. (Patching the real
    network/clock is fine; faking your own code is not.)
  * NO smoke-only tests — a test that only does `assert True`, imports the
    module, or never asserts behaviour proves nothing. Each test asserts the
    REAL behaviour the spec promises (inputs → outputs), end to end through the
    public surface.
If the leaf registers HTTP routes: every handler MUST accept EXACTLY two
positional arguments (payload, query) and return (status_code, dict) — the
platform dispatcher calls handler(payload, query); your tests MUST invoke
handlers through that exact signature, or the assembled app dies at the
smoke gate while your leaf stays green.
Test ONLY your own module in isolation. NEVER author whole-product or
cross-feature end-to-end tests — the platform smoke suite (tests/smoke/)
owns the assembled-product check and runs at the root integrate; a copy of
it among regular tests fails every partial build it reaches first.
NEVER import app from a feature module: the platform loader imports every
src/*.py FROM app, so importing it back is a circular import that kills
the whole assembly. A feature imports only registry, db and the stdlib.
NEVER touch platform internals (db._SCHEMAS, registry.ROUTES) from code or
tests — clearing the schema registry once destroyed every sibling's tables
for the whole session. Test isolation = a fresh MARKETPLACE_DB path per
test, nothing else. Such writes are REFUSED by the platform.

SURFACE ALREADY BUILT — EDIT IN PLACE, DO NOT FORK A SECOND ONE: if your spec
refines or extends a surface (an HTTP route, an HTML page like GET /ui) that the
REPOSITORY MAP shows an EXISTING sibling module already serves, do NOT create a
new src/{fn}.py for it. Return THAT existing module's path with its FULL
extended content (and update its test file), preserving the behaviour already
there. One surface = one module — a second module for the same page/route is a
duplicate, even if the spec arrived later. Create src/{fn}.py ONLY when the
surface is genuinely new.

Reply with ONLY a JSON object (no prose, no fence). Default to the new-file
keys below; when EDITING an existing surface in place, use that module's
existing path instead (e.g. "src/web_ui.py") and its test path:
{{"files": {{"src/{fn}.py": "<full file text>",
            "tests/test_{fn}.py": "<full file text>"}}}}
Escape newlines as \\n inside the JSON strings."""

_REPAIR_TASK = """The test run FAILED. Output (tail):
---
{output}
---
Fix the code and/or the tests (same files, same conventions, standard library
only) and reply again with ONLY the same JSON shape:
{{"files": {{"src/{fn}.py": "...", "tests/test_{fn}.py": "..."}}}}"""


# Diff-based repair (#1 / П3): a SURGICAL fix instead of a whole-file rewrite.
# The model edits the CURRENT files via SEARCH/REPLACE blocks; a search that
# doesn't match exactly once is refused without a write, so a stale diff can
# never clobber working code.
_REPAIR_DIFF_TASK = """The test run FAILED. Output (tail):
---
{output}
---
Repair with the SMALLEST possible edit. Do NOT rewrite whole files — emit one
or more SEARCH/REPLACE blocks against the CURRENT files shown below. The SEARCH
text must be copied EXACTLY from the current file and be unique. Standard
library only, same conventions.

CURRENT src/{fn}.py:
---
{src}
---
CURRENT tests/test_{fn}.py:
---
{test}
---
Reply with ONLY diff blocks in this exact format (repeat as needed):
FILE: src/{fn}.py
<<<<<<< SEARCH
<exact lines to find>
=======
<replacement lines>
>>>>>>> REPLACE"""


def _leaf_bar(ws_root: str, fn: str, baseline: int, pv) -> tuple[bool, str]:
    """The leaf's completion bar: own tests green AND the whole (non-smoke)
    suite no worse than before this leaf touched the tree."""
    passed, out = _run_pytest(ws_root, f"tests/test_{fn}.py")
    if not passed:
        return False, out
    # REALNESS gate at the LEAF: a green test is not enough — the leaf's own
    # code must not be a stub/mock-of-a-local-module, and its test must assert
    # real behaviour (not smoke). A hollow leaf is RED here, before integrate,
    # so the rework loop fixes it at the source.
    from . import contract_checks as _cc
    real_viol = _cc.realness_violations(ws_root, modules={fn})
    if real_viol:
        return False, ("OWN tests pass but the leaf is NOT a real product "
                       "(stubs / mocks of a local module / smoke-only tests are "
                       "FORBIDDEN — ship working code and tests that assert real "
                       "behaviour):\n- " + "\n- ".join(real_viol))
    s_passed, s_out = pv.run_suite(ws_root, include_smoke=False)
    if pv._badness(s_passed, s_out) > baseline:
        return False, ("OWN tests green, but the WHOLE suite degraded after"
                       " this leaf (cross-feature breakage):\n" + s_out)
    return True, out


def _run_pytest(ws_root: str, test_rel: str) -> tuple[bool, str]:
    """Run the leaf's tests for REAL. Returns (passed, output tail)."""
    import sys as _sys
    with llm_backend.PYTEST_LOCK:
        proc = subprocess.run(
            [_sys.executable, "-m", "pytest", test_rel, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT,
            cwd=ws_root)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out[-1500:]


def _path_header(fn: str) -> str:
    """A two-line provenance header naming the product-relative paths of THIS
    leaf's code and test, prepended to both files so the dashboard Код/Тест tabs
    (and anyone reading the file) immediately see WHERE the artifact lives. Paths
    are repo/product-relative on purpose — absolute paths are forbidden in
    artifacts; this is the full path within the runnable product."""
    return f"# code: src/{fn}.py\n# test: tests/test_{fn}.py\n"


def _ensure_path_header(body: str, fn: str) -> str:
    """Prepend the path header if the body does not already start with one
    (idempotent across re-writes / diff repairs). A leading ``#`` comment keeps
    a following module docstring valid as the first statement."""
    head = body.lstrip("\n")
    if head.startswith("# code: src/"):
        return body
    return _path_header(fn) + body


def _write_reexport(ws: Any, fn: str, owner_module: str, owner: str) -> None:
    """Variant B cache-hit: instead of re-implementing a duplicate intent,
    write a thin module that re-exports the owner's public surface plus a
    trivial green test — the feature exists once, this leaf reuses it."""
    src = (f'"""De-duplicated: same intent as node \'{owner}\'.\n'
           f'Re-exports {owner_module} instead of a second implementation."""\n'
           f'from {owner_module} import *  # noqa: F401,F403\n')
    test = (f'def test_{fn}_reexports_{owner_module}():\n'
            f'    import {fn}  # the de-duplicated module imports cleanly\n'
            f'    assert {fn} is not None\n')
    ws._write(f"src/{fn}.py", _ensure_path_header(src, fn), "code")
    ws._write(f"tests/test_{fn}.py", _ensure_path_header(test, fn), "test")
    llm_log.log({"event": "ws_write", "writer": "dedup", "node": fn,
                 "paths": [f"src/{fn}.py", f"tests/test_{fn}.py"]})


def _write_reply_files(ws: Any, files: dict, fn: str) -> bool:
    """Write the worker's files into the workspace (harness does the I/O in
    chat-only mode). Only the leaf's own src/tests paths are accepted, and
    the platform-seeded skeleton is immutable."""
    from . import pytest_verifier
    protected = pytest_verifier.protected_files()
    wrote = False
    safe = {f"src/{fn}.py": "code", f"tests/test_{fn}.py": "test"}
    for rel, kind in safe.items():
        if rel in protected:
            llm_log.log({"event": "write_refused", "role": "implementer",
                         "node": fn, "path": rel,
                         "reason": "platform-seeded skeleton is immutable"})
            continue
        body = files.get(rel)
        if isinstance(body, str) and body.strip():
            if not pytest_verifier.content_allowed(body):
                llm_log.log({"event": "write_refused", "role": "implementer",
                             "node": fn, "path": rel,
                             "reason": "touches platform internals"})
                continue
            body = body if body.endswith("\n") else body + "\n"
            ws._write(rel, _ensure_path_header(body, fn), kind)
            # every workspace write is journaled with its writer — the
            # one tool that ATTRIBUTES any future clobbering instantly
            llm_log.log({"event": "ws_write", "writer": "implementer",
                         "node": fn, "paths": [rel]})
            wrote = True
    return wrote


def _apply_diff_repair(ws: Any, ws_root: str, fn: str, reply: str) -> bool:
    """#1 / П3: apply SEARCH/REPLACE diff blocks from a repair reply to the
    leaf's own files. Only src/<fn>.py and tests/test_<fn>.py are touchable;
    the platform skeleton stays immutable and platform-internal content is
    refused. Returns True if at least one block applied (a write happened)."""
    from . import diff_repair, pytest_verifier
    blocks = diff_repair.parse_blocks(reply)
    if not blocks:
        return False
    safe = {f"src/{fn}.py": "code", f"tests/test_{fn}.py": "test"}
    protected = pytest_verifier.protected_files()
    allowed = {p for p in safe if p not in protected}

    def _read(rel: str) -> str:
        return _inline_file(ws_root, rel) or ""

    res = diff_repair.apply_repair(_read, blocks, allowed=allowed)
    for rel, reason in res["refused"]:
        llm_log.log({"event": "diff_refused", "role": "implementer",
                     "node": fn, "path": rel, "reason": reason})
    wrote = False
    for rel, body in res["files"].items():
        kind = safe.get(rel)
        if kind is None:
            continue
        if not pytest_verifier.content_allowed(body):
            llm_log.log({"event": "write_refused", "role": "implementer",
                         "node": fn, "path": rel,
                         "reason": "touches platform internals"})
            continue
        ws._write(rel, body if body.endswith("\n") else body + "\n", kind)
        llm_log.log({"event": "ws_write", "writer": "implementer-diff",
                     "node": fn, "paths": [rel],
                     "applied": len(res["applied"])})
        wrote = True
    return wrote


def make_implementer(channel: Any = None) -> Callable[[dict], Any]:
    system = _with_language(load_skill_md("spec-implement"))
    allowed, disallowed = load_profile_policy("implementer")
    model = _model_for("implementer")

    def implement(ctx: dict) -> Any:
        ws_root = _ws_root(ctx.get("workspace"))
        nid = ctx["node"]
        # variant A: prefer the engine-resolved collision-free module name;
        # fall back to the legacy local derivation only if absent
        fn = ctx.get("module") or re.sub(r"\W+", "_", nid).strip("_").lower()
        # variants B+C: claim this leaf's INTENT before doing the work. If
        # an earlier leaf already finished the SAME intent, re-export it
        # instead of paying a second implementation (cache-hit).
        claim = None
        if claims.BOARD is not None:
            intent = _inline_file(ws_root, ctx.get("spec", "")) or ctx["title"]
            claim = claims.BOARD.claim(nid, fn, ctx["title"], intent)
            if claim["verdict"] == "duplicate":
                _write_reexport(ctx["workspace"], fn, claim["owner_module"],
                                claim["owner"])
                llm_log.log({"event": "dedup", "node": nid,
                             "reused": claim["owner_module"]})
                return None     # no implementer call — the work already exists
        # D1: a configured `team` runs the orchestra; with NO team (the
        # default, and p4/p5) we fall through to the existing single-agent
        # path, byte-for-byte unchanged. C1: a per-leaf executor team on the
        # ctx (the engine routed this leaf to a domain executor) takes
        # precedence over the globally-configured implementer team.
        team = _normalise_team(ctx.get("team")) or _implementer_team()
        if team:
            out = _orchestra_run(ctx, ws_root, nid, fn, system=system,
                                 allowed=allowed, disallowed=disallowed,
                                 channel=channel, team=team)
        else:
            out = (_implement_chat(ctx, ws_root, nid, fn) if _chat_only()
                   else _implement_claude(ctx, ws_root, nid, fn))
        if claim is not None:
            claims.BOARD.complete(nid, claim["hash"])
        return out

    def _implement_claude(ctx: dict, ws_root: str, nid: str, fn: str) -> Any:
        # #10: a node's specialty routes the implementer to a specialty model
        specialty = str(ctx.get("specialty", "") or "")
        model = _model_for("implementer", specialty)
        prompt = _IMPLEMENT_TASK.format(title=ctx["title"], id=nid,
                                        spec=ctx["spec"], fn=fn) \
            + memory.recall_block_for("implementer", ctx["title"]) \
            + _ASK_RULE
        note = channel.poll_note() if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        raw = _dialog_round(prompt, role="implementer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=ws_root, model=model, channel=channel,
                            specialty=specialty)
        try:
            _handle_operator_reply(_extract_json(raw), role="implementer",
                                   node=nid, note=note, channel=channel)
        except ValueError:  # noqa: TRY302 — prose reply, judged by artifacts
            # non-JSON final reply: artifacts still judge the work; an
            # unaddressed operator note is recorded honestly
            if note and channel is not None:
                channel.record_reply("implementer", nid, "unaddressed",
                                     "(worker did not address the note)", note)
        llm_log.log_outcome(role="implementer", worker=True, node=nid, depth=-1,
                            model=model, prompt=prompt, reply=raw, ok=True)
        return None     # the engine judges by the artifacts, not the reply

    def _implement_chat(ctx: dict, ws_root: str, nid: str, fn: str) -> Any:
        """Chat-only implementer: the model returns the files, the harness
        writes them and runs pytest for REAL; one repair round on failure."""
        ws = ctx["workspace"]
        # #10: a node's specialty routes the implementer to a specialty model
        specialty = str(ctx.get("specialty", "") or "")
        model = _model_for("implementer", specialty)
        spec_body = _inline_file(ws_root, ctx["spec"])
        prompt = _IMPLEMENT_CHAT_TASK.format(
            title=ctx["title"], id=nid, spec=ctx["spec"],
            spec_body=spec_body, fn=fn) + _ASK_RULE
        from . import pytest_verifier, repo_map
        protected = sorted(pytest_verifier.protected_files())
        rmap = repo_map.build_map(ws_root, exclude=set(protected))
        if rmap:
            prompt += ("\n\nREPOSITORY MAP — every existing module's public"
                       " surface (routes, schemas, signatures). Build"
                       " CONSISTENTLY with it: reference declared tables AS"
                       " DECLARED (db.connect applies every registered"
                       " schema; CREATE IF NOT EXISTS keeps the FIRST"
                       " definition), reuse existing signatures, never"
                       " duplicate a sibling's route or table:\n" + rmap)
        if protected:
            # a chat worker cannot list the workspace — show it the seeded
            # platform API instead of bare file names, or it doubts they exist
            shown = [rel for rel in protected if rel.startswith("src/")]
            blocks = "\n".join(
                f"--- {rel} (read-only) ---\n{_inline_file(ws_root, rel)}"
                for rel in shown)
            prompt += ("\n\nPLATFORM FILES (already present in the workspace,"
                       " IMMUTABLE — import and build INTO them, NEVER"
                       " rewrite): " + ", ".join(protected)
                       + ("\n" + blocks if blocks else ""))
        note = channel.poll_note() if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        # every entry into the leaf is journaled with its elapsed time —
        # a wedged leaf becomes visible BEFORE the ceiling fires
        leaf_t0 = _time.time()
        deadline = float(ctx.get("deadline") or 0)

        def _round_gate(round_no: int, what: str) -> None:
            llm_log.log({"event": "leaf_round", "node": nid,
                         "round": round_no, "stage": what,
                         "elapsed_s": round(_time.time() - leaf_t0, 1)})
            if deadline and _time.time() > deadline:
                raise TimeoutError(
                    f"leaf time ceiling reached before {what}"
                    f" (round {round_no},"
                    f" {round(_time.time() - leaf_t0)}s elapsed)")

        _round_gate(1, "first implement round")
        # creator ensemble: with SPEC_FLOW_CREATOR_ENSEMBLE>=2 this generates
        # several candidates from different free models and returns the first
        # that compiles clean — the broken-syntax candidate never lands. N==1
        # is identical to the legacy single _dialog_round call.
        raw = _ensemble_generate(prompt, node=nid, system=system,
                                 allowed=allowed, disallowed=disallowed,
                                 cwd=ws_root, model=model, channel=channel,
                                 specialty=specialty)
        try:
            parsed = _extract_json(raw)
        except ValueError:
            # garbage reply: ONE strict re-ask; a second failure surrenders
            # the leaf honestly (red artifacts) instead of crashing the run
            raw = _call_model(
                prompt + "\n\nYour previous reply was not parseable."
                " Reply with ONLY the JSON object, no prose, no fence.",
                system=system, allowed=allowed, disallowed=disallowed,
                cwd=ws_root, model=model, role="implementer")
            try:
                parsed = _extract_json(raw)
            except ValueError as exc:
                llm_log.log_outcome(role="implementer", worker=True, node=nid,
                                    depth=-1, model=model, ok=False,
                                    error=f"unparseable reply: {str(exc)[:150]}",
                                    tests_passed=False)
                return None
        out = _handle_operator_reply(parsed, role="implementer",
                                     node=nid, note=note, channel=channel)
        # the leaf bar is TWO-tier: its own tests green AND the whole suite
        # not degraded — 'green alone, poisons the suite' must surface at
        # leaf completion, not branches later at the integrate gate.
        # GIT TRANSACTION: baseline + write + bar is ONE serialized,
        # attributable commit — a baseline captured while a sibling was
        # mid-write poisoned v17; git history answers 'who wrote what'
        from . import pytest_verifier as pv
        from . import ws_tx
        passed, test_out = False, "(no files written)"
        with ws_tx.transaction(ws_root, f"leaf:{nid}", "write+bar"):
            base_passed, base_out = pv.run_suite(ws_root, include_smoke=False)
            baseline = pv._badness(base_passed, base_out)
            wrote = _write_reply_files(ws, out.get("files") or {}, fn)
            if wrote:
                passed, test_out = _leaf_bar(ws_root, fn, baseline, pv)
        if wrote:
            _granular_commit(ws_root, fn, "create")   # opt-in fine-grained history
        if wrote and not passed:
            _round_gate(2, "repair round")
            # #1 / П3: prefer a SURGICAL diff repair (SEARCH/REPLACE against
            # the current files) over a whole-file rewrite — fewer regressions,
            # cheaper. Whole-file JSON stays as a back-compat fallback when the
            # model returns no applicable diff.
            cur_src = _inline_file(ws_root, f"src/{fn}.py") or ""
            cur_test = _inline_file(ws_root, f"tests/test_{fn}.py") or ""
            repair = (prompt + "\n\n"
                      + _REPAIR_DIFF_TASK.format(output=test_out, fn=fn,
                                                 src=cur_src, test=cur_test))
            raw2 = _call_model(repair, system=system, allowed=allowed,
                               disallowed=disallowed, cwd=ws_root,
                               model=model, role="implementer",
                               specialty=specialty)
            with ws_tx.transaction(ws_root, f"leaf:{nid}",
                                   "repair write+bar"):
                if _apply_diff_repair(ws, ws_root, fn, raw2):
                    passed, test_out = _leaf_bar(ws_root, fn, baseline, pv)
                else:
                    # no applicable diff — fall back to whole-file JSON if the
                    # model returned that shape instead
                    try:
                        out2 = _extract_json(raw2)
                    except ValueError:
                        out2 = {}
                    if _write_reply_files(ws, out2.get("files") or {}, fn):
                        passed, test_out = _leaf_bar(ws_root, fn, baseline, pv)
            _granular_commit(ws_root, fn, "repair")    # opt-in fine-grained history
            raw = raw2
        if passed:
            # a GREEN leaf is worth remembering: craft for the role,
            # the decision for the project (no-ops when memory is off)
            memory.retain_role(
                "implementer",
                f"Leaf '{nid}' ({ctx.get('title', '')}) landed green as"
                f" src/{fn}.py with its own tests and no suite regression.",
                context="green leaf", tags=["leaf"])
            memory.retain_project(
                f"Feature '{ctx.get('title', '')}' is implemented by"
                f" src/{fn}.py (node {nid}).",
                context="leaf landed", tags=["leaf"])
            # a DECLARATIVE interface fact: the module's public surface,
            # so later nodes recall real signatures, not just feature names
            try:
                from . import repo_map
                surface = repo_map.map_file(
                    Path(ws_root) / "src" / f"{fn}.py")
                if surface:
                    memory.retain_project(
                        f"Public surface of src/{fn}.py:"
                        f" {surface[:600]}",
                        context="interface fact", tags=["interface"])
            except Exception:        # noqa: BLE001 — facts never kill a leaf
                pass
        else:
            # a RED leaf with its diagnosis teaches more than a green one:
            # the failure tail is what recurring mistakes look like
            memory.retain_role(
                "implementer",
                f"Leaf '{nid}' ({ctx.get('title', '')}) surrendered RED;"
                f" failing output tail: {test_out[-300:]}",
                context="red leaf", tags=["fail"])
        llm_log.log_outcome(role="implementer", worker=True, node=nid, depth=-1,
                            model=model, prompt=prompt, reply=raw, ok=True,
                            tests_passed=passed,
                            test_output=test_out[-300:])
        return None     # the engine judges by the artifacts, not the reply

    return implement


# ── reviewer (skill: spec-reviewer, profile: spec-reviewer) ──────────────

_REVIEW_TASK = """You are running the skill above as the spec-reviewer
worker. Review ONE spec file: {spec}{spec_body}

Project goal: {goal}
Constitution (non-negotiable): {constitution}

What you are judging: the worker-authored sections (Requirements / Scope /
Open decisions / Acceptance criteria). The header block (Node, Traces-to,
leaf_check, Size estimate) is ENGINE-GENERATED metadata — prose in the
HEADER Traces-to line is fine, never a reject reason. Traceability is
judged INSIDE ## Requirements: each requirement carries its own
'Traces-to: REQ-<parent>-n'. For the ROOT node there is no parent —
requirements trace to the project goal and that is valid by definition.

Apply the skill's gate to the authored sections: REQ-id traceability,
EARS form, testable acceptance, explicit scope boundary, constitution
compliance. Binary verdict.{repo_map}{refusals}

REJECT a spec whose acceptance criteria would be satisfiable by a STUB, a MOCK
of the product's own modules, or a SMOKE-only test. Acceptance must demand REAL
observable behaviour (concrete inputs → concrete outputs through the public
surface), so a hollow implementation cannot pass it. "It imports", "it returns
something", "no error" are NOT acceptable criteria.
Reply with ONLY: {{"verdict": "PASS"|"REJECT", "reasons": ["..."]}}"""


def _review_repo_map(ws_root: Optional[str]) -> str:
    """П7: the public surface (AST digest) of the modules already in the
    workspace, so the reviewer catches a spec that COLLIDES with what exists
    — a duplicate concern, a clashing route, a signature mismatch — BEFORE
    it is implemented, not after the integration goes red."""
    if not ws_root:
        return ""
    try:
        from . import repo_map
        rmap = repo_map.build_map(ws_root, subdirs=("src",))
    except Exception:  # noqa: BLE001
        return ""
    if not rmap:
        return ""
    return ("\n\nREPOSITORY MAP — modules ALREADY built (public surface). "
            "REJECT if this spec duplicates an existing concern or clashes "
            "with an existing route/signature:\n" + rmap)


def _review_refusals(node: Any) -> str:
    """П7: this node's write_refused / rolled_back history, so the reviewer
    sees that an earlier attempt was blocked touching protected/platform
    files — a strong signal the spec is reaching outside its boundary."""
    if node in (None, "", "?"):
        return ""
    try:
        events = llm_log.read_events({"write_refused", "rolled_back"}, node=node)
    except Exception:  # noqa: BLE001
        return ""
    if not events:
        return ""
    lines = []
    for e in events[:8]:
        path = e.get("path", e.get("file", "?"))
        reason = e.get("reason", e.get("event", ""))
        lines.append(f"  - {e.get('event')}: {path} ({reason})")
    return ("\n\nREFUSAL HISTORY for this node (writes the platform blocked). "
            "If the spec still requires reaching into these, REJECT and tell "
            "it to stay within its boundary:\n" + "\n".join(lines))


def make_reviewer() -> Callable[[dict], dict]:
    system = _with_language(load_skill_md("spec-reviewer"))
    allowed, disallowed = load_profile_policy("spec-reviewer")
    model = _model_for("reviewer")

    def review(ctx: dict) -> dict:
        if _chat_only():
            spec_body = ("\nIts full text:\n---\n"
                         + _inline_file(ctx.get("workspace_root"), ctx["spec"])
                         + "\n---")
        else:
            spec_body = " (read it)."
        prompt = _REVIEW_TASK.format(
            spec=ctx["spec"], spec_body=spec_body, goal=ctx.get("goal", ""),
            constitution="; ".join(ctx.get("constitution") or []),
            repo_map=_review_repo_map(ctx.get("workspace_root")),
            refusals=_review_refusals(ctx.get("node")))
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model, role="reviewer",
                          # carry node+title so the live status shows WHICH node
                          # is being reviewed instead of «None»
                          meta={"node": ctx.get("node"),
                                "title": ctx.get("title") or ctx.get("node")})
        out = _extract_json(raw)
        llm_log.log_outcome(role="reviewer", worker=True, node=ctx.get("node", "?"),
                            depth=-1, model=model, prompt=prompt, reply=raw, ok=True)
        verdict = str(out.get("verdict", "PASS")).upper()
        reasons = [str(r) for r in out.get("reasons") or []]
        if verdict == "REJECT" and reasons:
            # rejection reasons are the reviewer's craft: recurring ones
            # surface in recall and the distilled mental model
            memory.retain_role(
                "reviewer",
                f"Spec review REJECT at node"
                f" '{ctx.get('node', '?')}': " + "; ".join(reasons[:3]),
                context="review reject", tags=["reject"])
        return {"verdict": "REJECT" if verdict == "REJECT" else "PASS",
                "reasons": reasons}

    return review


# ── researcher (skill: spec-research, profile: researcher) ───────────────

_RESEARCH_TASK = """You are running the skill above as the researcher worker
on ONE spike question raised during decomposition.

Project goal: {goal}
Node: {node}
Spike question: {question}

Research it (use your tools if helpful) and give ONE actionable
recommendation that the decomposer folds into the spec — short, decisive,
no hedging. Reply with ONLY:
{{"recommendation": "...", "basis": "one line on what it rests on"}}"""


def make_researcher() -> Callable[[dict], dict]:
    system = load_skill_md("spec-research")
    allowed, disallowed = load_profile_policy("researcher")
    model = _model_for("researcher")

    def research(ctx: dict) -> dict:
        prompt = _RESEARCH_TASK.format(goal=ctx.get("goal", ""),
                                       node=ctx.get("node", "?"),
                                       question=ctx["question"])
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model, role="researcher")
        out = _extract_json(raw)
        llm_log.log_outcome(role="researcher", worker=True, node=ctx.get("node", "?"),
                            depth=-1, model=model, prompt=prompt, reply=raw, ok=True)
        return {"recommendation": str(out.get("recommendation", "")).strip(),
                "basis": str(out.get("basis", "")).strip()}

    return research
