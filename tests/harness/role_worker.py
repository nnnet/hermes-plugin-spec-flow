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
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from . import claude_cli, llm_backend, llm_log

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


def _model_for(role: str) -> str:
    # the shared default is the free OpenRouter pool — paid models are
    # forbidden for test runs (llm_backend enforces it); 'haiku' exists
    # only as llm_backend's exhaustion fallback
    return (os.environ.get(f"SPEC_FLOW_{role.upper().replace('-', '_')}_MODEL")
            or llm_backend.DEFAULT_FREE_MODEL)


def _chat_only() -> bool:
    """True when the backend is a plain chat API: no file/shell tools, so
    referenced files are inlined into prompts and the harness itself does
    the file writes and test runs."""
    return llm_backend.BACKEND == "openai"


def _call_model(prompt: str, *, system: str, allowed: list[str],
                disallowed: list[str], cwd: Optional[str], model: str) -> str:
    """ONE door to the model for every role worker (free-pool rule lives in
    llm_backend). The claude-CLI path keeps the real tool-policy flags."""
    if _chat_only():
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
                      disallowed=disallowed, cwd=cwd, model=model)
    try:
        probe = _extract_json(raw)
    except ValueError:
        return raw
    question = str(probe.get("question", "")).strip()
    if not question or len(probe) > 1:
        return raw
    answer = channel.ask(role, node, question) if channel is not None else None
    if answer:
        followup = (f"{prompt}\n\nYOU ASKED: {question}\n"
                    f"HUMAN ANSWER: {answer}\n"
                    "Fold the answer into your work and produce the normal "
                    "output now (no more questions).")
    else:
        followup = (f"{prompt}\n\nYOU ASKED: {question}\n"
                    "No human answer arrived. Proceed on your own best "
                    "judgement, STATE the assumption you made, and produce "
                    "the normal output now (no more questions).")
    return _call_model(followup, system=system, allowed=allowed,
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
    system = load_skill_md("spec-flow-decompose")
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
        if feedback:
            prompt += ("\n\nREWORK ROUND — the reviewer REJECTED the previous"
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
        note = channel.poll_note() if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        _log_call_start("decomposer", nid, ctx["depth"], model)
        raw = _dialog_round(prompt, role="decomposer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=workspace_dir, model=model, channel=channel)
        out = _handle_operator_reply(_extract_json(raw), role="decomposer",
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


def _run_pytest(ws_root: str, test_rel: str) -> tuple[bool, str]:
    """Run the leaf's tests for REAL. Returns (passed, output tail)."""
    import sys as _sys
    proc = subprocess.run(
        [_sys.executable, "-m", "pytest", test_rel, "-q", "--no-header", "-p",
         "no:cacheprovider"],
        capture_output=True, text=True, timeout=PYTEST_TIMEOUT, cwd=ws_root)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out[-1500:]


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
            wrote = True
    return wrote


def make_implementer(channel: Any = None) -> Callable[[dict], Any]:
    system = load_skill_md("spec-implement")
    allowed, disallowed = load_profile_policy("implementer")
    model = _model_for("implementer")

    def implement(ctx: dict) -> Any:
        ws_root = _ws_root(ctx.get("workspace"))
        nid = ctx["node"]
        fn = re.sub(r"\W+", "_", nid).strip("_").lower()
        if _chat_only():
            return _implement_chat(ctx, ws_root, nid, fn)
        prompt = _IMPLEMENT_TASK.format(title=ctx["title"], id=nid,
                                        spec=ctx["spec"], fn=fn) + _ASK_RULE
        note = channel.poll_note() if channel is not None else None
        if note:
            prompt += _NOTE_RULE.format(note=note)
        _log_call_start("implementer", nid, -1, model)
        raw = _dialog_round(prompt, role="implementer", node=nid, system=system,
                            allowed=allowed, disallowed=disallowed,
                            cwd=ws_root, model=model, channel=channel)
        try:
            _handle_operator_reply(_extract_json(raw), role="implementer",
                                   node=nid, note=note, channel=channel)
        except ValueError:
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
        _log_call_start("implementer", nid, -1, model)
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
                cwd=ws_root, model=model)
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
        passed, test_out = False, "(no files written)"
        if _write_reply_files(ws, out.get("files") or {}, fn):
            passed, test_out = _run_pytest(ws_root, f"tests/test_{fn}.py")
            if not passed:
                repair = (prompt + "\n\n"
                          + _REPAIR_TASK.format(output=test_out, fn=fn))
                raw2 = _call_model(repair, system=system, allowed=allowed,
                                   disallowed=disallowed, cwd=ws_root,
                                   model=model)
                out2 = _extract_json(raw2)
                if _write_reply_files(ws, out2.get("files") or {}, fn):
                    passed, test_out = _run_pytest(ws_root, f"tests/test_{fn}.py")
                raw = raw2
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
compliance. Binary verdict.
Reply with ONLY: {{"verdict": "PASS"|"REJECT", "reasons": ["..."]}}"""


def make_reviewer() -> Callable[[dict], dict]:
    system = load_skill_md("spec-reviewer")
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
            constitution="; ".join(ctx.get("constitution") or []))
        _log_call_start("reviewer", str(ctx.get("node", "?")), -1, model)
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model)
        out = _extract_json(raw)
        llm_log.log_outcome(role="reviewer", worker=True, node=ctx.get("node", "?"),
                            depth=-1, model=model, prompt=prompt, reply=raw, ok=True)
        verdict = str(out.get("verdict", "PASS")).upper()
        return {"verdict": "REJECT" if verdict == "REJECT" else "PASS",
                "reasons": [str(r) for r in out.get("reasons") or []]}

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
        _log_call_start("researcher", str(ctx.get("node", "?")), -1, model)
        raw = _call_model(prompt, system=system, allowed=allowed,
                          disallowed=disallowed, cwd=ctx.get("workspace_root"),
                          model=model)
        out = _extract_json(raw)
        llm_log.log_outcome(role="researcher", worker=True, node=ctx.get("node", "?"),
                            depth=-1, model=model, prompt=prompt, reply=raw, ok=True)
        return {"recommendation": str(out.get("recommendation", "")).strip(),
                "basis": str(out.get("basis", "")).strip()}

    return research
