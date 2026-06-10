"""spec-flow — deterministic gates and seed commands for Spec-Driven
Development on a Hermes kanban board.

This module is the thin, *deterministic* half of the spec-flow suite. The
intelligent half lives in the bundled skills (``skills/*/SKILL.md``) that the
worker profiles load. The split is on purpose: the LLM owns the reasoning
(decompose a node, write a spec, review a contract), and this plugin owns the
*hard, repeatable* checks that must not be left "to the eye" — leaf-vs-branch
classification, contract-drift detection and research-lane triggers.

It registers six tools under the ``kanban`` toolset:

* ``leaf_check``             — is this node atomic (leaf) or must it expand
                               one more level (branch)?
* ``contract_check``         — does the code drift from the frozen L2
                               contract (OpenAPI by default; Zod / Protobuf
                               optional; parallel mode for extra assurance)?
* ``research_trigger_check`` — should the continuous research/revision lane
                               fire right now (every_n_tasks / m_test_errors /
                               on_level_return / cron, with cooldown)?
* ``specflow_init``          — create the board + ``constitution.md`` +
                               ``specs/`` workspace for a project.
* ``specflow_start``         — seed the L0 decomposition task on the board.
* ``specflow_status``        — compact summary of the project board.

Version note: this repo registers tools via ``tools.registry.register``
(``name``/``toolset``/``schema``/``handler``/``check_fn``/``emoji``). The
upstream Hermes plugin guide instead exposes ``ctx.register_tool`` /
``ctx.register_command``. We follow the in-repo contract used by
``chief-tools``; if you port this to a stock Hermes that only offers the
``ctx.*`` surface, re-wrap the handlers — the handler bodies stay the same.

The seed helpers shell out to ``hermes kanban`` via ``_run_kanban`` which is
resilient to a missing ``hermes`` binary (returns a structured error instead
of crashing the agent loop), so the plugin loads cleanly in a sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — adjust to your project. These are the "hard levels" that make
# decomposition reproducible instead of eyeballed.
# ---------------------------------------------------------------------------

# leaf_check thresholds: a node is a LEAF only when it stays under all of
# these. Anything above means "expand one more level" (branch).
MAX_MODULES = 1        # one cohesive module per leaf
MAX_TASKS = 5          # at most a handful of bite-sized tasks
MAX_INTERFACES = 2     # touches at most two interface surfaces
MAX_LOC = 100          # ~one commit; > this is too big for a single leaf

# contract_check: validator command templates. ``{contract}`` and ``{code}``
# are substituted with the contract artifact path and a code path/dir. Swap in
# your own binaries. Default contract type is OpenAPI.
CONTRACT_VALIDATORS: dict[str, list[str]] = {
    # OpenAPI: lint the contract, then verify the implementation against it.
    "openapi": ["redocly", "lint", "{contract}"],
    # Zod schema compiled/checked via the TypeScript compiler.
    "zod": ["tsc", "--noEmit", "{contract}"],
    # Protobuf via buf.
    "protobuf": ["buf", "lint", "{contract}"],
}
DEFAULT_CONTRACT_TYPES = ["openapi"]

# Research lane — the "continuous revision" trigger configuration. Any subset
# may fire; cooldown prevents thrash. Tune per project or override via the
# SPEC_FLOW_RESEARCH_LANE env var (JSON).
RESEARCH_LANE: dict[str, Any] = {
    "enabled": True,
    "every_n_tasks": 20,        # fire after N completed tasks since baseline
    "m_test_errors": 10,        # ... or after M accumulated test errors
    "on_level_return": True,    # ... or when decomposition returns up a level
    "cron": None,               # ... or on an external cron (handled outside)
    "cooldown_tasks": 5,        # don't re-fire within this many tasks
}

# Policy / constitution constraints enforced deterministically by policy_gate.
# These encode the non-negotiable rules a vague or risky spec must satisfy
# before it may be decomposed into implementation work — the deterministic
# half of the constitution check (the L1 constitution + spec-reviewer skill
# own the rest). Override via the SPEC_FLOW_POLICY env var (JSON).
POLICY: dict[str, Any] = {
    "max_unattended_spend_usd": 50,     # spend above this needs human approval
    "outreach_requires_consent": True,  # mass outreach must be opt-in / consented
    "require_legality_review": True,    # nodes with legal exposure must be reviewed
    "require_measurable_target": True,  # goals must carry a measurable acceptance target
}


# ---------------------------------------------------------------------------
# Paths — never hard-code absolute paths; derive from HERMES_HOME (env) and
# fall back to the conventional per-user home.
# ---------------------------------------------------------------------------

def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser(os.path.join("~", ".hermes"))


def _spec_flow_dir() -> str:
    path = os.path.join(_hermes_home(), "spec-flow")
    os.makedirs(path, exist_ok=True)
    return path


def _policy_config() -> dict[str, Any]:
    raw = os.environ.get("SPEC_FLOW_POLICY")
    merged = dict(POLICY)
    if raw:
        try:
            merged.update(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("spec-flow: bad SPEC_FLOW_POLICY env (%s)", exc)
    return merged


def _research_lane_config() -> dict[str, Any]:
    raw = os.environ.get("SPEC_FLOW_RESEARCH_LANE")
    if not raw:
        return dict(RESEARCH_LANE)
    try:
        override = json.loads(raw)
        merged = dict(RESEARCH_LANE)
        merged.update(override)
        return merged
    except Exception as exc:  # noqa: BLE001 — never let bad config crash a tool
        logger.warning("spec-flow: bad SPEC_FLOW_RESEARCH_LANE env (%s)", exc)
        return dict(RESEARCH_LANE)


# ---------------------------------------------------------------------------
# Gating — spec-flow tools live alongside kanban orchestration. Keep the gate
# permissive (these are utility/orchestration tools, not side-effecting on
# their own); schema visibility is still controlled by the enabled toolsets.
# ---------------------------------------------------------------------------

def _check_specflow() -> bool:
    return True


# ---------------------------------------------------------------------------
# leaf_check
# ---------------------------------------------------------------------------

LEAF_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "leaf_check",
        "description": (
            "Deterministically classify a decomposition node as a LEAF "
            "(atomic — emit implementation tasks) or a BRANCH (expand one "
            "more level). A node is a leaf only when it stays under every "
            "threshold: modules<=1, tasks<=5, interfaces<=2, estimated "
            "LOC<=100, has no open decisions, is single-concern (not "
            "'frontend+backend at once'), and all acceptance criteria are "
            "testable. Use this instead of judging by eye."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modules": {"type": "integer", "description": "Distinct cohesive modules the node touches."},
                "tasks": {"type": "integer", "description": "Number of bite-sized implementation tasks."},
                "interfaces": {"type": "integer", "description": "Interface surfaces (APIs/contracts) involved."},
                "estimated_loc": {"type": "integer", "description": "Estimated lines of code for the whole node."},
                "open_decisions": {"type": "integer", "description": "Count of unresolved design decisions; >0 forces branch."},
                "single_concern": {"type": "boolean", "description": "True if the node is one concern (e.g. only DB, only API). 'frontend+backend together' is False."},
                "testable_criteria": {"type": "boolean", "description": "True if every acceptance criterion is mechanically testable."},
            },
            "required": ["modules", "tasks", "interfaces", "estimated_loc"],
        },
    },
}


def _handle_leaf_check(args: dict[str, Any], **_: Any) -> str:
    modules = int(args.get("modules", 0))
    tasks = int(args.get("tasks", 0))
    interfaces = int(args.get("interfaces", 0))
    estimated_loc = int(args.get("estimated_loc", 0))
    open_decisions = int(args.get("open_decisions", 0))
    single_concern = bool(args.get("single_concern", True))
    testable_criteria = bool(args.get("testable_criteria", True))

    reasons: list[str] = []
    if modules > MAX_MODULES:
        reasons.append(f"modules {modules} > {MAX_MODULES}")
    if tasks > MAX_TASKS:
        reasons.append(f"tasks {tasks} > {MAX_TASKS}")
    if interfaces > MAX_INTERFACES:
        reasons.append(f"interfaces {interfaces} > {MAX_INTERFACES}")
    if estimated_loc > MAX_LOC:
        reasons.append(f"estimated_loc {estimated_loc} > {MAX_LOC} (not one commit)")
    if open_decisions > 0:
        reasons.append(f"{open_decisions} open decision(s) — resolve before leafing")
    if not single_concern:
        reasons.append("coupled step (multiple concerns) — split into single-concern leaves")
    if not testable_criteria:
        reasons.append("acceptance criteria not all testable")

    verdict = "leaf" if not reasons else "branch"
    payload = {
        "verdict": verdict,
        "reasons": reasons,
        "thresholds": {
            "max_modules": MAX_MODULES,
            "max_tasks": MAX_TASKS,
            "max_interfaces": MAX_INTERFACES,
            "max_loc": MAX_LOC,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# contract_check
# ---------------------------------------------------------------------------

CONTRACT_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "contract_check",
        "description": (
            "Validate code against the frozen L2 contract(s) and detect "
            "drift. Default contract type is OpenAPI; pass types=['zod'] or "
            "['protobuf'] to switch, or types=['openapi','zod'] to run "
            "several validators in parallel for extra assurance. Any drift "
            "fails; in strict mode an unavailable validator also fails "
            "(never silently passes). Use as the drift gate before a leaf is "
            "considered done; on drift, route to the drift-gate / respec-gate "
            "skill instead of silently editing code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract_artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to frozen contract files (e.g. openapi.yaml).",
                },
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Code paths/dirs to validate against the contract.",
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["openapi", "zod", "protobuf"]},
                    "description": "Contract types to validate. Default ['openapi'].",
                },
                "strict": {
                    "type": "boolean",
                    "description": "If true, a missing/unavailable validator is a failure, not a skip.",
                },
            },
            "required": ["contract_artifacts"],
        },
    },
}


def _run_validator(ctype: str, contract: str, code: str) -> dict[str, Any]:
    template = CONTRACT_VALIDATORS.get(ctype)
    if not template:
        return {"type": ctype, "available": False, "ok": False, "detail": f"no validator configured for '{ctype}'"}
    binary = template[0]
    if shutil.which(binary) is None:
        return {"type": ctype, "available": False, "ok": False, "detail": f"validator binary '{binary}' not found on PATH"}
    cmd = [part.replace("{contract}", contract).replace("{code}", code) for part in template]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {"type": ctype, "available": True, "ok": False, "detail": f"validator error: {exc}"}
    ok = proc.returncode == 0
    detail = (proc.stdout or "") + (proc.stderr or "")
    return {"type": ctype, "available": True, "ok": ok, "detail": detail.strip()[:2000]}


def _handle_contract_check(args: dict[str, Any], **_: Any) -> str:
    contracts = args.get("contract_artifacts") or []
    changed = args.get("changed_files") or []
    types = args.get("types") or DEFAULT_CONTRACT_TYPES
    strict = bool(args.get("strict", False))

    if not contracts:
        return tool_error("contract_check requires at least one contract_artifacts path")

    code_target = changed[0] if changed else "."

    jobs: list[tuple[str, str]] = [(ctype, contract) for ctype in types for contract in contracts]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(jobs) or 1)) as pool:
        futures = [pool.submit(_run_validator, ctype, contract, code_target) for ctype, contract in jobs]
        for fut in futures:
            results.append(fut.result())

    drift = [r for r in results if r.get("available") and not r.get("ok")]
    unavailable = [r for r in results if not r.get("available")]

    failed = bool(drift) or (strict and bool(unavailable))
    payload = {
        "status": "drift" if failed else "ok",
        "drift": drift,
        "unavailable": unavailable,
        "validated": [r for r in results if r.get("available") and r.get("ok")],
        "strict": strict,
        "note": "on drift do NOT edit code silently — go to drift-gate (fix code) or respec-gate (fix the contract first)",
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# research_trigger_check
# ---------------------------------------------------------------------------

RESEARCH_TRIGGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "research_trigger_check",
        "description": (
            "Decide whether the continuous research/revision lane should fire "
            "now. Tracks deltas (completed tasks, test errors) against a "
            "baseline in the spec-flow state file and applies the configured "
            "triggers (every_n_tasks / m_test_errors / on_level_return / cron) "
            "with a cooldown. Call it on level-return and on a cron/manual "
            "revise. Returns trigger=true|false and the reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["on_level_return", "cron", "manual", "tick"],
                    "description": "Why the check is being made.",
                },
                "completed_tasks": {"type": "integer", "description": "Total completed tasks so far (absolute counter)."},
                "test_errors": {"type": "integer", "description": "Total accumulated test errors so far (absolute counter)."},
            },
            "required": ["reason"],
        },
    },
}


def _load_lane_state() -> dict[str, Any]:
    path = os.path.join(_spec_flow_dir(), "research_lane_state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"baseline_completed": 0, "baseline_errors": 0, "last_fire_completed": 0}


def _save_lane_state(state: dict[str, Any]) -> None:
    path = os.path.join(_spec_flow_dir(), "research_lane_state.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("spec-flow: cannot persist research lane state (%s)", exc)


def _handle_research_trigger_check(args: dict[str, Any], **_: Any) -> str:
    cfg = _research_lane_config()
    reason = args.get("reason", "tick")
    completed = int(args.get("completed_tasks", 0))
    errors = int(args.get("test_errors", 0))

    if not cfg.get("enabled", True):
        return json.dumps({"trigger": False, "reason": "lane disabled"}, ensure_ascii=False)

    state = _load_lane_state()
    d_completed = completed - int(state.get("baseline_completed", 0))
    d_errors = errors - int(state.get("baseline_errors", 0))
    since_fire = completed - int(state.get("last_fire_completed", 0))

    cooldown = int(cfg.get("cooldown_tasks", 0))
    on_cooldown = since_fire < cooldown and int(state.get("last_fire_completed", 0)) > 0

    fired: list[str] = []
    if cfg.get("on_level_return") and reason == "on_level_return":
        fired.append("on_level_return")
    if reason == "cron" and cfg.get("cron") is not None:
        fired.append("cron")
    if reason == "manual":
        fired.append("manual")
    every_n = cfg.get("every_n_tasks")
    if every_n and d_completed >= int(every_n):
        fired.append(f"every_n_tasks>={every_n}")
    m_err = cfg.get("m_test_errors")
    if m_err and d_errors >= int(m_err):
        fired.append(f"m_test_errors>={m_err}")

    trigger = bool(fired) and not on_cooldown
    if trigger:
        state["last_fire_completed"] = completed
        state["baseline_completed"] = completed
        state["baseline_errors"] = errors
        _save_lane_state(state)

    payload = {
        "trigger": trigger,
        "fired_by": fired,
        "on_cooldown": on_cooldown,
        "deltas": {"completed": d_completed, "errors": d_errors, "since_last_fire": since_fire},
        "action": "route to spec-research (revision mode) → respec-gate if findings invalidate a spec" if trigger else "continue",
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Seed commands — exposed as tools (this repo does not register CLI
# subcommands). They shell to `hermes kanban` so the durable board owns the
# recursion. Resilient to a missing `hermes` binary.
# ---------------------------------------------------------------------------

def _run_kanban(kanban_args: list[str]) -> dict[str, Any]:
    if shutil.which("hermes") is None:
        return {"ok": False, "error": "hermes binary not found on PATH — run from a host with Hermes installed", "argv": ["hermes", "kanban", *kanban_args]}
    try:
        proc = subprocess.run(["hermes", "kanban", *kanban_args], capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"hermes kanban failed: {exc}", "argv": ["hermes", "kanban", *kanban_args]}
    return {"ok": proc.returncode == 0, "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip(), "returncode": proc.returncode}


SPECFLOW_INIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "specflow_init",
        "description": (
            "Initialise a spec-flow project: create the kanban board, a "
            "constitution.md (non-negotiable project rules) and a specs/ "
            "workspace under the project dir. Run once per project before "
            "specflow_start."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project / board name."},
                "dir": {"type": "string", "description": "Project workspace directory (absolute path on the host)."},
            },
            "required": ["project", "dir"],
        },
    },
}


def _handle_specflow_init(args: dict[str, Any], **_: Any) -> str:
    project = args.get("project")
    workdir = args.get("dir")
    if not project or not workdir:
        return tool_error("specflow_init requires 'project' and 'dir'")

    created: dict[str, Any] = {}
    try:
        specs_dir = os.path.join(workdir, "specs")
        os.makedirs(specs_dir, exist_ok=True)
        constitution = os.path.join(workdir, "constitution.md")
        if not os.path.exists(constitution):
            with open(constitution, "w", encoding="utf-8") as fh:
                fh.write(
                    "# Project constitution\n\n"
                    "Non-negotiable rules every spec and every leaf must honour.\n"
                    "Edit before starting decomposition.\n\n"
                    "- (add principle)\n"
                )
        created["specs_dir"] = specs_dir
        created["constitution"] = constitution
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"specflow_init workspace setup failed: {exc}")

    board = _run_kanban(["create-board", "--name", str(project)])
    return json.dumps({"workspace": created, "board": board}, ensure_ascii=False)


SPECFLOW_START_SCHEMA = {
    "type": "function",
    "function": {
        "name": "specflow_start",
        "description": (
            "Seed the L0 decomposition task on the project board. The "
            "dispatcher then spawns a fresh spec-flow-decompose worker that "
            "expands the tree level-by-level until the L0 integrate task is "
            "done. Run after specflow_init."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Top-level project goal (the L0 spec seed)."},
                "project": {"type": "string", "description": "Board name created by specflow_init."},
                "dir": {"type": "string", "description": "Project workspace directory."},
            },
            "required": ["goal", "project"],
        },
    },
}


def _handle_specflow_start(args: dict[str, Any], **_: Any) -> str:
    goal = args.get("goal")
    project = args.get("project")
    workdir = args.get("dir")
    if not goal or not project:
        return tool_error("specflow_start requires 'goal' and 'project'")
    kanban_args = [
        "create", "--board", str(project),
        "--title", f"Decompose: {goal}",
        "--assignee", "spec-decomposer",
        "--skill", "spec-flow-decompose",
    ]
    if workdir:
        kanban_args += ["--workspace", f"dir:{workdir}"]
    result = _run_kanban(kanban_args)
    return json.dumps({"seeded": result, "next": "start the gateway dispatcher; the board now drives itself"}, ensure_ascii=False)


SPECFLOW_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "specflow_status",
        "description": "Compact summary of a spec-flow project board (counts per status, blocked tasks).",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Board name."},
            },
            "required": ["project"],
        },
    },
}


def _handle_specflow_status(args: dict[str, Any], **_: Any) -> str:
    project = args.get("project")
    if not project:
        return tool_error("specflow_status requires 'project'")
    result = _run_kanban(["show", "--board", str(project)])
    return json.dumps({"board": project, "result": result}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# policy_gate — deterministic constitution check
# ---------------------------------------------------------------------------

POLICY_GATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "policy_gate",
        "description": (
            "Deterministically check a spec node against the project "
            "constitution BEFORE it is decomposed or implemented. Catches the "
            "imprecision a vague/risky goal hides: no measurable target, "
            "unattended spend above the cap, mass outreach without consent, "
            "legal exposure not reviewed. Returns verdict 'pass', 'clarify' "
            "(ambiguity to resolve) or 'block' (constitution violation — fix "
            "the spec first, do not proceed). This is the deterministic half "
            "of the requirements/spec gate; the spec-reviewer skill owns the "
            "rest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "measurable_target": {"type": "boolean", "description": "Does the goal carry a measurable acceptance target (e.g. revenue>=$N in 90d, ROI, Sharpe)?"},
                "spend_per_action_usd": {"type": "number", "description": "Max money the node may spend per action."},
                "human_in_loop": {"type": "boolean", "description": "Is there a human approval gate on spend/sends for this node?"},
                "involves_outreach": {"type": "boolean", "description": "Does the node send outreach (email/DM/ads) at scale?"},
                "consent_obtained": {"type": "boolean", "description": "Is the outreach opt-in / consented?"},
                "legal_exposure": {"type": "boolean", "description": "Does the node carry legal/jurisdiction/ToS exposure?"},
                "legality_reviewed": {"type": "boolean", "description": "Has the legal exposure been reviewed and confirmed compliant?"},
            },
            "required": [],
        },
    },
}


def _handle_policy_gate(args: dict[str, Any], **_: Any) -> str:
    cfg = _policy_config()
    measurable = bool(args.get("measurable_target", False))
    spend = float(args.get("spend_per_action_usd", 0) or 0)
    human = bool(args.get("human_in_loop", False))
    outreach = bool(args.get("involves_outreach", False))
    consent = bool(args.get("consent_obtained", False))
    legal = bool(args.get("legal_exposure", False))
    reviewed = bool(args.get("legality_reviewed", False))

    clarifications: list[str] = []
    blocks: list[str] = []

    if cfg.get("require_measurable_target") and not measurable:
        clarifications.append("no measurable acceptance target — goal is unverifiable as stated")

    cap = float(cfg.get("max_unattended_spend_usd", 0) or 0)
    if spend > cap and not human:
        blocks.append(f"spend ${spend:g}/action exceeds unattended cap ${cap:g} — requires human approval")
    if outreach and cfg.get("outreach_requires_consent") and not consent:
        blocks.append("mass outreach without consent/opt-in — constitution requires consented audiences")
    if legal and cfg.get("require_legality_review") and not reviewed:
        blocks.append("legal/jurisdiction/ToS exposure not reviewed — must be confirmed compliant first")

    if blocks:
        verdict = "block"
    elif clarifications:
        verdict = "clarify"
    else:
        verdict = "pass"

    payload = {
        "verdict": verdict,
        "blocks": blocks,
        "clarifications": clarifications,
        "constraints": {
            "max_unattended_spend_usd": cap,
            "outreach_requires_consent": bool(cfg.get("outreach_requires_consent")),
            "require_legality_review": bool(cfg.get("require_legality_review")),
            "require_measurable_target": bool(cfg.get("require_measurable_target")),
        },
        "action": {
            "block": "do NOT decompose — fix the spec (constitution violation), then re-gate",
            "clarify": "resolve via clarify / kanban_block before expanding",
            "pass": "proceed to leaf_check / decomposition",
        }[verdict],
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="leaf_check",
    toolset="kanban",
    schema=LEAF_CHECK_SCHEMA,
    handler=_handle_leaf_check,
    check_fn=_check_specflow,
    emoji="🍃",
)

registry.register(
    name="contract_check",
    toolset="kanban",
    schema=CONTRACT_CHECK_SCHEMA,
    handler=_handle_contract_check,
    check_fn=_check_specflow,
    emoji="📐",
)

registry.register(
    name="research_trigger_check",
    toolset="kanban",
    schema=RESEARCH_TRIGGER_SCHEMA,
    handler=_handle_research_trigger_check,
    check_fn=_check_specflow,
    emoji="🔬",
)

registry.register(
    name="policy_gate",
    toolset="kanban",
    schema=POLICY_GATE_SCHEMA,
    handler=_handle_policy_gate,
    check_fn=_check_specflow,
    emoji="⚖️",
)

registry.register(
    name="specflow_init",
    toolset="kanban",
    schema=SPECFLOW_INIT_SCHEMA,
    handler=_handle_specflow_init,
    check_fn=_check_specflow,
    emoji="🌱",
)

registry.register(
    name="specflow_start",
    toolset="kanban",
    schema=SPECFLOW_START_SCHEMA,
    handler=_handle_specflow_start,
    check_fn=_check_specflow,
    emoji="🚦",
)

registry.register(
    name="specflow_status",
    toolset="kanban",
    schema=SPECFLOW_STATUS_SCHEMA,
    handler=_handle_specflow_status,
    check_fn=_check_specflow,
    emoji="📊",
)
