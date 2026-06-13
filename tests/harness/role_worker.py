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
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import claims, claude_cli, llm_backend, llm_log, memory

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PLUGIN_ROOT / "skills"
PROFILES_DIR = PLUGIN_ROOT / "profiles"

TIMEOUT = int(os.environ.get("SPEC_FLOW_WORKER_TIMEOUT", "600"))
RETRIES = int(os.environ.get("SPEC_FLOW_WORKER_RETRIES", "2"))
# how much of a referenced file is inlined into a chat-only prompt
INLINE_FILE_LIMIT = int(os.environ.get("SPEC_FLOW_INLINE_FILE_LIMIT", "8000"))
PYTEST_TIMEOUT = int(os.environ.get("SPEC_FLOW_PYTEST_TIMEOUT", "120"))

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


def _model_for(role: str) -> str:
    # delegates to the shared per-role resolver: the case YAML `workers:`
    # block is the single source of truth when present (env vars apply only
    # without it); paid models stay forbidden — llm_backend gates ':free'
    return llm_backend.model_for(role)


def _chat_only() -> bool:
    """True when the backend is a plain chat API: no file/shell tools, so
    referenced files are inlined into prompts and the harness itself does
    the file writes and test runs."""
    return llm_backend.BACKEND == "openai"


def _call_model(prompt: str, *, system: str, allowed: list[str],
                disallowed: list[str], cwd: Optional[str], model: str,
                role: Optional[str] = None) -> str:
    """ONE door to the model for every role worker (free-pool rule lives in
    llm_backend). ``role`` resolves the case-configured model CHAIN — the
    tail entries answer when the primary's quota is exhausted. The
    claude-CLI path keeps the real tool-policy flags."""
    if _chat_only():
        fallbacks = llm_backend.chain_for(role)[1:] if role else ()
        if fallbacks:
            return llm_backend.ask(prompt, model=model, system=system,
                                   fallbacks=fallbacks)
        return llm_backend.ask(prompt, model=model, system=system)
    return _run_claude(prompt, system=system, allowed=allowed,
                       disallowed=disallowed, cwd=cwd, model=model)


def _inline_file(root: Optional[str], rel: str) -> str:
    """The file's text for prompt embedding (chat-only mode), truncated."""
    try:
        text = (Path(root or ".") / rel).read_text(encoding="utf-8")
    except OSError:
        return "(file not found)"
    if len(text) > INLINE_FILE_LIMIT:
        text = text[:INLINE_FILE_LIMIT] + "\n…(truncated)"
    return text


def _log_call_start(role: str, node: str, depth: int, model: str) -> None:
    """The live dashboard derives 'what is being worked on RIGHT NOW' from
    an open call_start (one without a following outcome) — same contract
    as the sim harness's timed_ask."""
    llm_log.log({"event": "call_start", "role": role, "worker": True,
                 "node": node, "depth": depth, "model": model})


def _run_claude(prompt: str, *, system: str, allowed: list[str],
                disallowed: list[str], cwd: Optional[str], model: str) -> str:
    """One real worker session. Separated for offline test stubbing."""
    cmd = [*claude_cli.claude_cmd(), "-p", "--model", model,
           "--append-system-prompt", system,
           *claude_cli.mcp_args_no_serena()]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        cmd += ["--disallowedTools", ",".join(disallowed)]
    last = ""
    for _ in range(RETRIES):
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=TIMEOUT, cwd=cwd or claude_cli.agent_cwd())
        if proc.returncode == 0 and proc.stdout.strip():
            return claude_cli.strip_headroom_banner(proc.stdout)
        last = (proc.stderr or proc.stdout)[-300:]
    raise RuntimeError(f"worker session failed after {RETRIES} tries: {last}")


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
                  cwd: Optional[str], model: str, channel: Any) -> str:
    """One worker session + at most one human Q&A round.

    A reply consisting of {"question": ...} pauses the work, asks the human
    through the channel and re-runs the session with the answer appended.
    No channel / no answer → the worker is told to proceed on its own
    judgement and state its assumption."""
    raw = _call_model(prompt, system=system, allowed=allowed,
                      disallowed=disallowed, cwd=cwd, model=model, role=role)
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
                       disallowed=disallowed, cwd=cwd, model=model)


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
  "children": [{{"id": "snake_case", "title": "short"}}],
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

LEAF_DEPTH = int(os.environ.get("SPEC_FLOW_LLM_LEAF_DEPTH", "3"))
MAX_CHILDREN = int(os.environ.get("SPEC_FLOW_LLM_MAX_CHILDREN", "4"))


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
        if ctx["depth"] >= LEAF_DEPTH:
            prompt += (f"\n\nHARD CONSTRAINT: depth {ctx['depth']} >= "
                       f"{LEAF_DEPTH} — this node MUST be atomic (no children).")
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
        branch_capable = ctx["depth"] < LEAF_DEPTH
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
        _log_call_start("decomposer", nid, ctx["depth"], model)
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
        if ctx["depth"] >= LEAF_DEPTH:
            out.pop("children", None)
        if out.get("children"):
            out["children"] = out["children"][:MAX_CHILDREN]
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


# ── implementer (skill: spec-implement, profile: implementer) ────────────

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

Reply with ONLY a JSON object (no prose, no fence):
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
    ws._write(f"src/{fn}.py", src, "code")
    ws._write(f"tests/test_{fn}.py", test, "test")
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
            ws._write(rel, body if body.endswith("\n") else body + "\n", kind)
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
        out = (_implement_chat(ctx, ws_root, nid, fn) if _chat_only()
               else _implement_claude(ctx, ws_root, nid, fn))
        if claim is not None:
            claims.BOARD.complete(nid, claim["hash"])
        return out

    def _implement_claude(ctx: dict, ws_root: str, nid: str, fn: str) -> Any:
        prompt = _IMPLEMENT_TASK.format(title=ctx["title"], id=nid,
                                        spec=ctx["spec"], fn=fn) \
            + memory.recall_block_for("implementer", ctx["title"]) \
            + _ASK_RULE
        note = channel.poll_note() if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        _log_call_start("implementer", nid, int(ctx.get("depth", -1)), model)
        raw = _dialog_round(prompt, role="implementer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=ws_root, model=model, channel=channel)
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
        _log_call_start("implementer", nid, int(ctx.get("depth", -1)), model)
        raw = _dialog_round(prompt, role="implementer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=ws_root, model=model, channel=channel)
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
                               model=model, role="implementer")
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
        _log_call_start("reviewer", str(ctx.get("node", "?")), int(ctx.get("depth", -1)), model)
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model, role="reviewer")
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
        _log_call_start("researcher", str(ctx.get("node", "?")), int(ctx.get("depth", -1)), model)
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model, role="researcher")
        out = _extract_json(raw)
        llm_log.log_outcome(role="researcher", worker=True, node=ctx.get("node", "?"),
                            depth=-1, model=model, prompt=prompt, reply=raw, ok=True)
        return {"recommendation": str(out.get("recommendation", "")).strip(),
                "basis": str(out.get("basis", "")).strip()}

    return research
