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
from dataclasses import dataclass, field
from typing import Any, Optional

# Hermes provides ``tools.registry``; fall back to a no-op so the plugin
# imports, runs and is testable WITHOUT Hermes (the gate/runner logic is pure).
try:
    from tools.registry import registry, tool_error
except Exception:  # noqa: BLE001
    class _NoopRegistry:
        def register(self, **_kw):  # signature-compatible no-op
            return None

    registry = _NoopRegistry()

    def tool_error(msg: str) -> str:
        return json.dumps({"error": msg}, ensure_ascii=False)

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
# run_report — log-based "footprints" report + methodology audit
#
# Consumes a run TRACE (the JSONL event stream a spec-flow run emits — one event
# per line with tick/phase/profile/skill/task/action/gate/verdict/level/detail)
# and produces, with NO knowledge of the runner, two things a reviewer needs:
#   1. readable footprints — what the plugin did, step by step;
#   2. a methodology audit — where the spec-flow method was violated, so the
#      reviewer (at either revision level — spike or continuous revision) can
#      see the methodological errors and act.
# This is deterministic and works purely off the logs.
# ---------------------------------------------------------------------------

_PROFILE_ICON = {
    "spec-decomposer": "🧩", "researcher": "🔬", "spec-contract": "📐",
    "spec-reviewer": "⚖️", "implementer": "🛠️", "verifier": "✅",
}

# The plugin's full surface — what a run CAN use. Built DYNAMICALLY from the
# real shipped folders (skills/<name>/, profiles/<name>/), so adding or
# removing a skill/profile updates coverage everywhere; the literal sets are
# only a fallback for a stripped installation.
def _shipped(sub: str, fallback: set) -> set:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), sub)
    try:
        found = {n for n in os.listdir(base)
                 if os.path.isdir(os.path.join(base, n))}
    except OSError:
        found = set()
    return found or set(fallback)


ALL_SKILLS = _shipped("skills", {
    "spec-requirements", "spec-flow-decompose", "spec-contract", "spec-reviewer",
    "spec-implement", "spec-integrate", "spec-research", "drift-gate", "respec-gate",
})
ALL_PROFILES = _shipped("profiles", set(_PROFILE_ICON))


def _ev(e: dict, k: str) -> Any:
    return e.get(k, "")


def _index_trace(events: list[dict]) -> dict[str, Any]:
    idx: dict[str, Any] = {
        "leaf": {}, "leaf_detail": {}, "contract": [], "drift_gate": set(),
        "respec_node": [], "clarify": set(), "integrate": set(),
        "reviews": {}, "rederive": [], "policy_pass": False, "complete": False,
        "spike": False, "revision_fired": False,
    }
    for e in events:
        gate, verdict, task = _ev(e, "gate"), _ev(e, "verdict"), _ev(e, "task")
        skill, action, phase = _ev(e, "skill"), _ev(e, "action"), _ev(e, "phase")
        if gate == "leaf_check":
            idx["leaf"][task] = verdict
            idx["leaf_detail"][task] = _ev(e, "detail")
        elif gate == "contract_check":
            idx["contract"].append((task, verdict, _ev(e, "tick")))
        elif gate == "policy_gate" and verdict == "pass":
            idx["policy_pass"] = True
        elif gate == "research_trigger_check" and verdict == "trigger":
            idx["revision_fired"] = True
        if skill == "drift-gate":
            idx["drift_gate"].add(task)
        if skill == "respec-gate":
            idx["respec_node"].append(task)
        if skill == "spec-integrate":
            idx["integrate"].add(task)
        if skill == "spec-research" and phase == "research":
            idx["spike"] = True
        if "open decision" in action:
            idx["clarify"].add(task)
        if action.startswith("impl-review"):
            idx["reviews"].setdefault(task, []).append(verdict)
        if "re-derive" in action:
            idx["rederive"].append(_ev(e, "detail"))
        if "COMPLETE" in action:
            idx["complete"] = True
    return idx


# severity: error = methodology violated; warn = risky; info = note
def audit_methodology(events: list[dict]) -> list[dict]:
    """Check a run trace against spec-flow methodology invariants. Returns a
    list of findings {rule, severity, where, why, fix}."""
    x = _index_trace(events)
    f: list[dict] = []

    def add(rule, severity, where, why, fix):
        f.append({"rule": rule, "severity": severity, "where": where, "why": why, "fix": fix})

    # R1 — constitution / policy gate ran and passed at the root
    if not x["policy_pass"]:
        add("R1-policy-gate", "error", "L0",
            "no passing policy_gate — constitution / measurable-target check missing",
            "run policy_gate on the goal before decomposing")

    # R2 — nothing implemented without a passing leaf gate
    for task in {t for e in events for t in [_ev(e, "task")] if str(t).endswith(":impl")}:
        node = task[:-5]
        if x["leaf"].get(node) != "leaf":
            add("R2-leaf-before-impl", "error", task,
                f"implemented '{node}' without a leaf_check=leaf verdict",
                "gate every node with leaf_check; only leaves get an impl task")

    # R3 — contract drift is never silent: drift -> drift-gate -> respec -> ok
    drift_tasks = [(t, tk) for (t, v, tk) in x["contract"] if v == "drift"]
    ok_tasks = {t for (t, v, _tk) in x["contract"] if v == "ok"}
    for t, _tk in drift_tasks:
        if t not in x["drift_gate"]:
            add("R3-silent-drift", "error", t,
                "contract_check reported drift but no drift-gate followed",
                "route every drift through drift-gate; never edit code silently")
        elif not x["respec_node"] or t not in ok_tasks:
            add("R3-unresolved-drift", "warn", t,
                "drift classified but no respec/clean re-check recorded",
                "after drift-gate, respec (spec-first) and re-run contract_check to ok")

    # R4 — every implementation is reviewed before it is done
    for task in {t for e in events for t in [_ev(e, "task")] if str(t).endswith(":impl")}:
        node = task[:-5]
        verdicts = x["reviews"].get(f"{node}:review", [])
        if "PASS" not in verdicts:
            add("R4-impl-not-reviewed", "error", task,
                f"'{node}' implemented without a passing spec-reviewer impl-review",
                "add a review node downstream of every impl; require PASS")

    # R5 — every branch converges via a bottom-up integrate node
    for node, verdict in x["leaf"].items():
        if verdict == "branch" and f"{node}:integrate" not in x["integrate"]:
            add("R5-branch-no-integrate", "error", node,
                "branch node has no Integrate & verify node",
                "create an integrate node whose parents are the branch's children")

    # R6 — an open decision is clarified before the level expands (feedback loop)
    for node, verdict in x["leaf"].items():
        if verdict == "branch" and "open decision" in x["leaf_detail"].get(node, "") \
                and node not in x["clarify"]:
            add("R6-expanded-past-open-decision", "warn", node,
                "level expanded while a decision was still open (feedback loop skipped)",
                "kanban_block on the open decision; expand only after it is resolved")

    # R7 — a revision that fired must re-derive the affected subtree
    if x["revision_fired"] and not x["rederive"]:
        add("R7-respec-no-rederive", "error", "revision",
            "research revision fired but no subtree re-derivation recorded",
            "respec-gate must version-bump and re-run the affected subtree, not just note it")

    # R8 — completion only on green (no review left on FAIL)
    for review, verdicts in x["reviews"].items():
        if verdicts and verdicts[-1] == "FAIL":
            add("R8-green-on-red", "error", review,
                "review ended on FAIL but the run continued / completed",
                "a failing review must loop to fix → re-run until PASS before done")
    if not x["complete"]:
        add("R8-not-complete", "info", "L0",
            "no L0 integrate-complete event in the trace",
            "run did not reach project completion (may be a partial trace)")

    # R9 — upfront research: an analogs/architecture spike must precede the
    # first implementation (build-vs-reuse and NFRs are decided before code)
    first_research = min((int(_ev(e, "tick") or 0) for e in events
                          if _ev(e, "skill") == "spec-research"), default=None)
    first_impl = min((int(_ev(e, "tick") or 0) for e in events
                      if _ev(e, "skill") == "spec-implement"), default=None)
    if first_impl is not None and (first_research is None or first_research > first_impl):
        add("R9-impl-before-research", "warn", "L1",
            "implementation started before any research (analogs / build-vs-reuse / architecture & NFRs)",
            "put a research/ADR node (analogs, differentiation, architecture, DB, load, security) before feature subtrees")

    return f


def summarize_trace(events: list[dict]) -> dict[str, Any]:
    tasks = {_ev(e, "task") for e in events if _ev(e, "task")}
    gates: dict[str, int] = {}
    skill_events: dict[str, int] = {}
    profile_events: dict[str, int] = {}
    for e in events:
        g = _ev(e, "gate")
        if g:
            gates[g] = gates.get(g, 0) + 1
        sk, pr = _ev(e, "skill"), _ev(e, "profile")
        if sk:
            skill_events[sk] = skill_events.get(sk, 0) + 1
        if pr:
            profile_events[pr] = profile_events.get(pr, 0) + 1
    x = _index_trace(events)
    return {
        "events": len(events),
        "skills": sorted(skill_events), "profiles": sorted(profile_events),
        "skill_events": skill_events, "profile_events": profile_events,
        "skills_missing": sorted(ALL_SKILLS - set(skill_events)),
        "profiles_missing": sorted(ALL_PROFILES - set(profile_events)),
        "tasks": len(tasks), "gate_calls": gates,
        "revision_levels": {"spike": x["spike"], "continuous_revision": x["revision_fired"]},
        "complete": x["complete"],
    }


_PHASE_RU = {
    "requirements": "Требования", "decompose": "Декомпозиция", "research": "Ресёрч",
    "contract": "Контракт", "implement": "Реализация", "drift": "Дрейф",
    "respec": "Respec", "review": "Ревью", "integrate": "Интеграция",
    "revision": "Ревизия",
}
_VERDICT_ICON = {
    "pass": "✅", "ok": "✅", "PASS": "✅", "leaf": "🍃", "branch": "🌿",
    "drift": "⚠️", "FAIL": "❌", "block": "⛔", "clarify": "🟡", "trigger": "🔬",
}


def _md(s: Any) -> str:
    """Make a value safe for a markdown table cell."""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def _plain_outcome(e: dict) -> tuple[str, str]:
    """Translate a trace event into (kind, plain words) — the simplest possible
    description of what was actually produced and what it means, so a child or a
    bot understands the result and its consequence."""
    a = str(_ev(e, "action")).lower()
    skill = _ev(e, "skill")
    v = _ev(e, "verdict")
    if "complete" in a:
        return "🏁 Готово", "всё собрано и проверено — ПРОЕКТ ГОТОВ"
    if "policy_gate" in a:
        return "📋 Проверка цели", ("цель измеримая и легальная — начинаем" if v == "pass"
                                    else "цель размытая/рискованная — надо уточнить")
    if "constitution rule" in a:
        return "📜 Правило", "записали правило проекта (что нельзя нарушать)"
    if "ears" in a or "requirements frozen" in a:
        return "📋 Требования", "зафиксировали, что система должна уметь"
    if "leaf_check" in a:
        return ("🧩 Разбили", "задача большая → разбили на подзадачи") if v == "branch" \
            else ("🍃 К работе", "задача маленькая → можно писать код")
    if "open decision" in a:
        return "🟡 Вопрос", "нашли непонятку → остановились и спросили"
    if "clarify answered" in a:
        return "✅ Ответ", "получили ответ → пошли дальше"
    if skill == "spec-research" and "revision finding" in a:
        return "🔬 Ревизия", "ревизия нашла важное → влияет на проект"
    if skill == "spec-research" and "spike" in a:
        return "🔬 Мини-ресёрч", "перед заморозкой проверили неизвестное"
    if skill == "spec-research" and "recommendation" in a:
        return "🔬 Вывод ресёрча", "вписали вывод исследования в план"
    if "research_trigger_check" in a:
        return ("🔬 Пора ревизию", "накопились причины — запускаем ревизию") if v == "trigger" \
            else ("🔬 Ревизия не нужна", "причин для ревизии пока нет")
    if "freeze openapi" in a or skill == "spec-contract":
        return "📐 Контракт", "заморозили правила API (контракт) ДО кода"
    if "spec-gate on contract" in a:
        return "📐 Контракт проверен", "проверили контракт — ок" if v == "PASS" else "контракт — есть замечания"
    if "bottom-up plan" in a or ("design" in a and skill == "spec-implement"):
        return "📝 План кода", "расписали порядок: БД→логика→API→тесты"
    if "tdd" in a:
        return "💻🧪 Код+тест", "сначала тест, потом код — тест прошёл"
    if "contract_check after respec" in a:
        return "✅ Совпало", "код и контракт снова совпадают"
    if "code-wrong" in a:
        return "🔧 Чиним код", "контракт прав, код неправ → исправляем код"
    if "contract_check after code fix" in a:
        return "✅ Совпало", "код исправлен и снова совпадает с контрактом"
    if "parallel contract_check" in a:
        return "✅ Все контракты", "сверили все контракты ветки разом — ок"
    if "contract_check" in a:
        return ("⚠️ Расхождение", "код разошёлся с контрактом — поймали") if v == "drift" \
            else ("✅ Контракт ок", "код совпадает с контрактом")
    if skill == "drift-gate":
        return "🔧 Разбор дрейфа", "решили, кто неправ: код или контракт"
    if skill == "respec-gate":
        return "📜 Правка спеки", "сначала чиним причину (спеку/контракт), потом код"
    if "impl-review" in a:
        return ("❌ Завернули", "ревью нашло недочёт → вернули на доработку") if v == "FAIL" \
            else ("✅ Принято", "ревью пройдено — код принят")
    if "fix per critique" in a:
        return "🔁 Переделка", "исправили по замечанию и переделали"
    if "git commit" in a:
        return "📦 Коммит", "сохранили готовый код в репозиторий"
    if "re-derive" in a:
        return "🔁 Пересборка", "пересобрали задетые задачи под новую спеку"
    if "acceptance" in a:
        return "✅ Сборка ок", "собрали кусок и проверили целиком — работает"
    if "write level spec" in a or "parent handoff" in a:
        return "📝 План/спека", "написали план этого уровня"
    return "·", _md(_ev(e, "action"))


def render_footprints(events: list[dict], level: int = 2) -> str:
    """Readable markdown table of the run — one row per step. The 'Простыми
    словами' column says, in the plainest terms, what was produced and what it
    means, so a non-technical reviewer (or a bot) follows the consequences."""
    rows = [
        "| # | Кто | Что делал (технически) | 👶 Простыми словами: что вышло | Тип |",
        "|--:|---|---|---|---|",
    ]
    for e in events:
        if int(_ev(e, "level") or 2) > level:
            continue
        icon = _PROFILE_ICON.get(_ev(e, "profile"), "·")
        who = f"{icon} {_ev(e, 'profile')}"
        kind, plain = _plain_outcome(e)
        rows.append(
            f"| {int(_ev(e,'tick') or 0)} | {who} | {_md(_ev(e,'action'))} "
            f"| {plain} | {kind} |"
        )
    return "\n".join(rows)


_SEV_ICON = {"error": "❌", "warn": "🟡", "info": "ℹ️"}


def build_run_report(events: list[dict], level: int = 2, title: str = "spec-flow run") -> str:
    s = summarize_trace(events)
    findings = audit_methodology(events)
    errors = [x for x in findings if x["severity"] == "error"]
    rl = s["revision_levels"]
    out: list[str] = []
    out.append(f"# {title} — отчёт по логам (footprints + методологический аудит)")
    out.append("")
    out.append(f"> Источник: трейс из {s['events']} событий. "
               f"Скиллы: {len(s['skills'])} · профили: {len(s['profiles'])} · "
               f"задач: {s['tasks']} · завершён: {'✅' if s['complete'] else '❌'}.")
    out.append(f"> Ревизия: spike (уровень 1) {'✅' if rl['spike'] else '—'} · "
               f"непрерывная ревизия (уровень 2) {'✅' if rl['continuous_revision'] else '—'}.")
    out.append(f"> **Методологический вердикт: {'❌ есть ошибки' if errors else '✅ нарушений не найдено'}** "
               f"({len(errors)} error, {len(findings) - len(errors)} прочих).")
    out.append("")
    out.append("## Методологический аудит (для ревизионера)")
    out.append("")
    out.append("> Аудит проверяет **этот прогон** (его лог) на соответствие методологии "
               "spec-flow — это **не баги кода плагина**. «Зелено» = прогон шёл по методу; "
               "находки = где метод нарушен *в этом прогоне*, и как починить **процесс** "
               "(добавить пропущенный гейт, провести дрейф через drift-gate и т.п.).")
    out.append("")
    if findings:
        out.append("| Уровень | Правило | Где (задача) | Что не так | Как починить прогон |")
        out.append("|---|---|---|---|---|")
        for x in findings:
            out.append(f"| {_SEV_ICON.get(x['severity'],'')} {x['severity']} | `{x['rule']}` | "
                       f"`{_md(x['where'])}` | {_md(x['why'])} | {_md(x['fix'])} |")
    else:
        out.append("_Нарушений методологии не обнаружено: все инварианты соблюдены._")
    out.append("")
    out.append(f"## Footprint — что делалось по шагам (детализация ≤ {level})")
    out.append("")
    out.append("> Колонка **«Простыми словами»** — самым простым языком: что реально "
               "получилось (создан план, написан код, прогнан тест, сделан коммит, "
               "пройдено ревью, собрана сборка) и каково последствие.")
    out.append("")
    out.append(render_footprints(events, level))
    out.append("")
    out.append("## Покрытие — посчитано кодом из лога")
    out.append("")
    used_sk = len(ALL_SKILLS) - len(s["skills_missing"])
    used_pr = len(ALL_PROFILES) - len(s["profiles_missing"])
    out.append(f"Скиллы: **{used_sk}/{len(ALL_SKILLS)}** · "
               f"Профили: **{used_pr}/{len(ALL_PROFILES)}** "
               f"(события каждого посчитаны по полям `skill`/`profile` трейса)")
    out.append("")
    out.append("| Скилл | Событий | · | Профиль | Событий |")
    out.append("|---|--:|---|---|--:|")
    sk_rows = [(k, s["skill_events"].get(k, 0)) for k in sorted(ALL_SKILLS)]
    pr_rows = [(k, s["profile_events"].get(k, 0)) for k in sorted(ALL_PROFILES)]
    for i in range(max(len(sk_rows), len(pr_rows))):
        sk = f"`{sk_rows[i][0]}` | {sk_rows[i][1] or '— не использован'}" \
            if i < len(sk_rows) else " | "
        pr = f"{_PROFILE_ICON.get(pr_rows[i][0], '')} `{pr_rows[i][0]}` | {pr_rows[i][1] or '— не использован'}" \
            if i < len(pr_rows) else " | "
        out.append(f"| {sk} | · | {pr} |")
    out.append("")
    out.append(f"- Вызовы тулзов: " + ", ".join(f"{k}×{v}" for k, v in s["gate_calls"].items()))
    if s["skills_missing"] or s["profiles_missing"]:
        out.append(f"- ⚠️ Не использованы: "
                   + ", ".join(f"`{k}`" for k in s["skills_missing"] + s["profiles_missing"]))
    return "\n".join(out) + "\n"


def _load_trace(trace_path: str) -> list[dict]:
    events: list[dict] = []
    with open(trace_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


RUN_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_report",
        "description": (
            "Build a readable run report + methodology audit from a spec-flow "
            "run TRACE (the JSONL event log). Returns footprints (what the "
            "plugin did, step by step), a summary, and an audit listing where "
            "the spec-flow methodology was violated (impl without a leaf gate, "
            "silent contract drift, branch without integration, revision "
            "without re-derivation, green-on-red, etc.). A reviewer initiates "
            "this to understand methodological errors. Pass either trace_path "
            "(JSONL file) or trace (inline list of event objects)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trace_path": {"type": "string", "description": "Path to a JSONL run trace."},
                "trace": {"type": "array", "items": {"type": "object"}, "description": "Inline list of event objects (alternative to trace_path)."},
                "level": {"type": "integer", "description": "Footprints verbosity: 1=milestones, 2=steps, 3=detail. Default 2."},
                "title": {"type": "string", "description": "Optional report title."},
            },
            "required": [],
        },
    },
}


def _handle_run_report(args: dict[str, Any], **_: Any) -> str:
    events = args.get("trace")
    if events is None:
        path = args.get("trace_path")
        if not path:
            return tool_error("run_report requires 'trace_path' or 'trace'")
        try:
            events = _load_trace(path)
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"run_report cannot read trace: {exc}")
    level = int(args.get("level", 2))
    title = args.get("title", "spec-flow run")
    findings = audit_methodology(events)
    return json.dumps({
        "summary": summarize_trace(events),
        "findings": findings,
        "errors": sum(1 for x in findings if x["severity"] == "error"),
        "report": build_run_report(events, level=level, title=title),
    }, ensure_ascii=False)


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
    name="run_report",
    toolset="kanban",
    schema=RUN_REPORT_SCHEMA,
    handler=_handle_run_report,
    check_fn=_check_specflow,
    emoji="🐾",
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


# ---------------------------------------------------------------------------
# Smart verification oracle (PLUGIN code — builds the oracle report itself)
# ---------------------------------------------------------------------------
# A scenario's `tree` is the INPUT that drives a run; re-using it as a 1:1
# ground-truth equality check is circular. This oracle instead checks the
# realized run against declared OUTCOME invariants (depth bounds, reference
# anchor nodes that must reach done, which methodology episodes happened,
# control-flow loop counts, coverage floors, research-before-implementation).
# Like build_run_report, the report is produced by plugin code, not the harness.

_ORACLE_EPISODE_TO_LOOP = {
    "clarify": "clarify",
    "review_critique": "review-fail",
    "drift_respec": "drift-respec",
    "drift_codefix": "drift-codefix",
    "revision": "revision-respec",
    "spike": None,        # detected from events, not loops
    "contract": None,
}
_ORACLE_LOOP_KEY_TO_TYPE = {
    "clarify": "clarify",
    "review_fail": "review-fail",
    "drift_respec": "drift-respec",
    "drift_codefix": "drift-codefix",
    "revision_respec": "revision-respec",
}


@dataclass
class Expectation:
    """A single checked invariant within an oracle report."""

    name: str
    ok: bool
    reason: str
    expected: Any = None
    actual: Any = None


@dataclass
class OracleReport:
    """Aggregate oracle result: per-expectation verdicts + a roll-up flag."""

    expectations: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(e.ok for e in self.expectations)

    @property
    def failures(self) -> list:
        return [e for e in self.expectations if not e.ok]

    def add(self, name, ok, reason, expected=None, actual=None) -> None:
        self.expectations.append(Expectation(name, ok, reason, expected, actual))


def _oracle_realized_depth(run_result) -> int:
    """Max nesting level the run actually decomposed to (root = 0)."""
    tree = run_result.project.get("tree") or {}

    def walk(node, depth):
        kids = node.get("children") or []
        return depth if not kids else max(walk(c, depth + 1) for c in kids)

    return walk(tree, 0)


def _oracle_done_ids(run_result) -> set:
    return {tid for tid, t in run_result.tasks.items()
            if getattr(t, "status", "") == "done"}


def _oracle_loop_counts(run_result) -> dict:
    counts: dict = {}
    for loop in run_result.loops:
        t = loop.get("type", "")
        counts[t] = counts.get(t, 0) + 1
    return counts


def _oracle_episode_present(run_result, episode: str) -> bool:
    loop_type = _ORACLE_EPISODE_TO_LOOP.get(episode)
    if loop_type is not None:
        return any(l.get("type") == loop_type for l in run_result.loops)
    if episode == "spike":
        return any(e.skill == "spec-research" and e.phase == "research"
                   for e in run_result.events)
    if episode == "contract":
        return any(e.gate == "contract_check" for e in run_result.events)
    return False


def _oracle_first_tick(run_result, *, skill=None, phase=None):
    ticks = [e.tick for e in run_result.events
             if (skill is None or e.skill == skill)
             and (phase is None or e.phase == phase)]
    return min(ticks) if ticks else None


def check_oracle(run_result, oracle_spec: dict, summary: dict) -> OracleReport:
    """Evaluate a scenario's declared `oracle:` block against the realized run.
    Checks semantic invariants (depth bounds, anchor nodes reaching done,
    required episodes, minimum loop counts, coverage floors, research-before-
    impl), NOT structural equality with the tree. Returns an OracleReport."""
    spec = oracle_spec or {}
    summary = summary or {}
    rep = OracleReport()

    depth = _oracle_realized_depth(run_result)
    if "min_depth" in spec:
        lo = int(spec["min_depth"])
        rep.add("min_depth", depth >= lo,
                f"realized depth {depth} {'>=' if depth >= lo else '<'} required {lo}",
                expected=f">= {lo}", actual=depth)
    if "max_depth" in spec:
        hi = int(spec["max_depth"])
        rep.add("max_depth", depth <= hi,
                f"realized depth {depth} {'<=' if depth <= hi else '>'} allowed {hi}",
                expected=f"<= {hi}", actual=depth)

    done = _oracle_done_ids(run_result)
    for anchor in spec.get("anchor_nodes", []) or []:
        exists = anchor in run_result.tasks
        is_done = anchor in done
        rep.add(f"anchor:{anchor}", exists and is_done,
                ("reached done" if exists and is_done
                 else ("exists but not done" if exists else "missing from board")),
                expected="done",
                actual=getattr(run_result.tasks.get(anchor), "status", "absent"))

    for episode in spec.get("expected_episodes", []) or []:
        present = _oracle_episode_present(run_result, episode)
        rep.add(f"episode:{episode}", present,
                "occurred" if present else "never occurred",
                expected=">= 1", actual="present" if present else "absent")

    counts = _oracle_loop_counts(run_result)
    for key, want in (spec.get("expected_loops") or {}).items():
        loop_type = _ORACLE_LOOP_KEY_TO_TYPE.get(key, key)
        have = counts.get(loop_type, 0)
        rep.add(f"loops:{key}", have >= int(want),
                f"{have} {'>=' if have >= int(want) else '<'} required {want}",
                expected=f">= {want}", actual=have)

    cov = spec.get("coverage") or {}
    if "min_skills" in cov:
        used = len(run_result.skills_used)
        rep.add("coverage:skills", used >= int(cov["min_skills"]),
                f"{used} skills used (missing per trace: {summary.get('skills_missing', [])})",
                expected=f">= {cov['min_skills']}", actual=used)
    if "min_profiles" in cov:
        used = len(run_result.profiles_used)
        rep.add("coverage:profiles", used >= int(cov["min_profiles"]),
                f"{used} profiles used (missing per trace: {summary.get('profiles_missing', [])})",
                expected=f">= {cov['min_profiles']}", actual=used)

    if spec.get("research_before_impl"):
        first_research = _oracle_first_tick(run_result, skill="spec-research")
        first_impl = _oracle_first_tick(run_result, skill="spec-implement")
        ok = (first_research is not None and first_impl is not None
              and first_research < first_impl)
        rep.add("research_before_impl", ok,
                f"first research tick={first_research}, first impl tick={first_impl}",
                expected="research < impl",
                actual=f"{first_research} vs {first_impl}")

    return rep


def render_oracle(report: OracleReport) -> str:
    """Render an OracleReport as a readable markdown table with a roll-up."""
    head = "✅ PASS" if report.ok else "❌ FAIL"
    n_ok = sum(1 for e in report.expectations if e.ok)
    n = len(report.expectations)
    lines = [
        f"## Oracle verdict — {head} ({n_ok}/{n} expectations)",
        "",
        "| Expectation | Verdict | Expected | Actual | Reason |",
        "|---|---|---|---|---|",
    ]
    for e in report.expectations:
        mark = "✅" if e.ok else "❌"
        lines.append(f"| `{e.name}` | {mark} | {e.expected} | {e.actual} | {e.reason} |")
    return "\n".join(lines) + "\n"


def build_oracle_report(run_result, oracle_spec: dict, summary: dict = None,
                        title: str = "spec-flow oracle") -> str:
    """Public report builder: evaluate the oracle and render a titled markdown
    report — the oracle counterpart of build_run_report, produced by plugin
    code from the realized run + the declared oracle spec."""
    if summary is None:
        summary = {}
    rep = check_oracle(run_result, oracle_spec, summary)
    body = render_oracle(rep)
    return f"# {title}\n\n{body}"
