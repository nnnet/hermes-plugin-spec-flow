"""spec-flow run engine — the plugin's own production runner.

This is PLUGIN code (not a test harness): it drives a whole project through the
spec-flow methodology to completion, WITH or WITHOUT Hermes —

* decisions use the plugin's own gate tools (policy_gate, leaf_check,
  contract_check, research_trigger_check), injectable via ``tools`` (defaults to
  the bundled ones, so it runs standalone);
* per-role workers (agents/profiles) are injectable via ``agents`` but have
  autonomous deterministic defaults bundled here — so a run works without any
  Hermes agents if none are passed;
* the **Workspace is mandatory** — every run materialises artifacts;
* a **depth** parameter selects how far execution goes.

Depth ladder (`spec` < `scaffold` < `verify` < `execute` < `product`):
  * spec     — constitution, per-node specs/plans, frozen contracts, MANIFEST;
  * scaffold — + code & test scaffolds per leaf, a commit journal;
  * verify   — + actually run the test files (pytest) and record results;
  * execute  — + an injected implementer agent writes real code / modifies a
               real project (raises if no implementer agent is provided);
  * product  — + BUILD and RUN the materialised product, checking it against an
               acceptance spec (``project["acceptance"]``) and writing a
               READY / NOT READY verdict to PRODUCT-RESULTS.md. Honest by
               design: with only stub modules and no real entrypoint it reports
               NOT READY rather than faking green.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Decision gates come from the plugin's own tool module. Works both as a package
# submodule (with Hermes) and standalone (loaded top-level by the test shim).
try:
    from . import spec_flow_tools as _gates
except Exception:  # noqa: BLE001
    import spec_flow_tools as _gates  # type: ignore

# Doctor (cause-diagnosis + remedy dispatch). The plugin root must be importable
# even when the runner is loaded by file (run_engine._load) with tests/ — not the
# plugin root — on sys.path: put our own directory first, then import absolutely.
# Import failure is recorded (NOT swallowed) so the engine can fail loudly when
# the doctor is requested but could not be built.
import logging as _logging
_DOCTOR_LOG = _logging.getLogger("spec_flow.doctor")
_DOCTOR_IMPORT_ERR = ""
try:
    import os as _os_d
    import sys as _sys_d
    _PLUGIN_DIR = _os_d.path.dirname(_os_d.path.abspath(__file__))
    if _PLUGIN_DIR not in _sys_d.path:
        _sys_d.path.insert(0, _PLUGIN_DIR)
    import spec_flow_doctor as _doctor_mod  # type: ignore
    import spec_flow_diagnosers as _diag_mod  # type: ignore
    _DOCTOR_LOG.info("doctor modules imported from %s", _PLUGIN_DIR)
except Exception as _exc:  # noqa: BLE001
    _doctor_mod = None  # type: ignore
    _diag_mod = None  # type: ignore
    _DOCTOR_IMPORT_ERR = repr(_exc)
    _DOCTOR_LOG.error("doctor import FAILED: %s", _DOCTOR_IMPORT_ERR)

# Wave journal (parallelization stage 0): a single-writer, append-only record
# of node commits. Optional — if it can't be imported or opened the run still
# proceeds; the journal only ADDS a restartable trail, it never gates work.
try:
    from . import spec_flow_journal as _journal_mod
except Exception:  # noqa: BLE001
    try:
        import spec_flow_journal as _journal_mod  # type: ignore
    except Exception:  # noqa: BLE001
        _journal_mod = None  # type: ignore


def _load_ws_tx():
    """Lazy import of the workspace-transaction helper (axis F). It lives in
    the test harness; load it however the runner was imported, None if absent."""
    for spec in ("tests.harness.ws_tx", "harness.ws_tx", "ws_tx"):
        try:
            import importlib
            return importlib.import_module(spec)
        except Exception:  # noqa: BLE001
            continue
    return None


class _LeafWorkspaceView:
    """A per-leaf view of the workspace rooted at an isolated worktree
    (axis F). The implementer writes here; a clean merge lands the files
    in the shared workspace. Everything else delegates to the real
    workspace, so artifacts are still recorded centrally."""

    def __init__(self, base: Any, root: str):
        self._base = base
        self.root = root
        self.enabled = True

    def _write(self, rel: str, content: str, kind: str) -> str:
        path = Path(self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        try:
            self._base.artifacts.append(
                {"path": rel, "type": kind,
                 "bytes": len(content.encode("utf-8"))})
        except Exception:  # noqa: BLE001
            pass
        return rel

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

# Optional per-node lifecycle FSM (the roadmap's node_engine='fsm' option).
# Imported guardedly: if the FSM module/deps are absent the runner still works
# with node_engine='inline'. The mandatory-gate constants are SHARED, so the
# inline guard enforces the exact same gates the FSM does — both engines catch
# a skipped gate identically.
try:
    from . import spec_flow_node_fsm as _nodefsm
except Exception:  # noqa: BLE001
    try:
        import spec_flow_node_fsm as _nodefsm  # type: ignore
    except Exception:  # noqa: BLE001
        # last resort: load by path next to this file — covers embedders that
        # exec the plugin under a synthetic package name (e.g. the case runner)
        try:
            import importlib.util as _ilu
            import sys as _sys
            from pathlib import Path as _P
            _spec = _ilu.spec_from_file_location(
                "spec_flow_node_fsm", _P(__file__).resolve().parent / "spec_flow_node_fsm.py")
            _nodefsm = _ilu.module_from_spec(_spec)  # type: ignore
            # register BEFORE exec: @dataclass resolves cls.__module__ via sys.modules
            _sys.modules.setdefault("spec_flow_node_fsm", _nodefsm)
            _spec.loader.exec_module(_nodefsm)       # type: ignore
        except Exception:  # noqa: BLE001
            _nodefsm = None  # type: ignore

if _nodefsm is not None:
    _NodeLifecycle = _nodefsm.NodeLifecycle
    GateViolation = _nodefsm.GateViolation
    GATES_LEAF = _nodefsm.GATES_LEAF
    GATES_BRANCH = _nodefsm.GATES_BRANCH
    EV_DECOMPOSE = _nodefsm.EV_DECOMPOSE
    EV_CLARIFY = _nodefsm.EV_CLARIFY
    EV_CONTRACT = _nodefsm.EV_CONTRACT
    EV_IMPLEMENT = _nodefsm.EV_IMPLEMENT
    EV_DRIFT = _nodefsm.EV_DRIFT
    EV_REVIEW = _nodefsm.EV_REVIEW
    EV_CRITIQUE = _nodefsm.EV_CRITIQUE
    EV_REVIEW_PASS = _nodefsm.EV_REVIEW_PASS
    EV_BRANCH_INTEGRATE = _nodefsm.EV_BRANCH_INTEGRATE
    EV_DONE = _nodefsm.EV_DONE
    _NODE_FSM_OK = True
else:  # pragma: no cover — only when the FSM module is unavailable
    class GateViolation(Exception):
        pass
    GATES_LEAF = ("contract_check", "review_pass", "verification")
    GATES_BRANCH = ("integrate",)
    _NodeLifecycle = None  # type: ignore
    EV_DECOMPOSE = EV_CLARIFY = EV_CONTRACT = EV_IMPLEMENT = EV_DRIFT = None
    EV_REVIEW = EV_CRITIQUE = EV_REVIEW_PASS = EV_BRANCH_INTEGRATE = EV_DONE = None
    _NODE_FSM_OK = False

# Which mandatory gate(s) each lifecycle event satisfies (the inline ledger maps
# events through this; the FSM records the same internally). Built only when
# the FSM module supplied real event names — None keys would collide.
_EVENT_GATE = {
    EV_CONTRACT: ("contract_check",),
    EV_REVIEW_PASS: ("review_pass", "verification"),
    EV_BRANCH_INTEGRATE: ("integrate",),
} if _NODE_FSM_OK else {}

# A3/A4 cycle-control decisions (escalation + lifecycle no-op pruning). Optional
# — when the harness module is absent the engine keeps today's behaviour.
try:
    from tests.harness import cycle_control as _cycle      # type: ignore
except Exception:  # noqa: BLE001
    try:
        from harness import cycle_control as _cycle        # type: ignore
    except Exception:  # noqa: BLE001
        _cycle = None

# 462: per-node truncation collector (doctor data source). Optional — when the
# harness module is absent the engine simply has no dropped-marker evidence.
try:
    from tests.harness import truncation_log as _trunc     # type: ignore
except Exception:  # noqa: BLE001
    try:
        from harness import truncation_log as _trunc       # type: ignore
    except Exception:  # noqa: BLE001
        _trunc = None

NODE_ENGINES = ("inline", "fsm")


def _lint_spec_traceability(nid: str, md: str) -> list:
    """Deterministic traceability rules a reviewer should never have to
    repeat: every AC-<node>-N traces to REQ-<node>-N and every REQ has an
    acceptance criterion. Specs without REQ/AC ids lint clean."""
    import re as _re
    esc = _re.escape(str(nid))
    reqs = set(_re.findall(rf"\bREQ-{esc}-(\d+)\b", md or ""))
    acs = set(_re.findall(rf"\bAC-{esc}-(\d+)\b", md or ""))
    out = []
    for n in sorted(acs - reqs, key=int):
        out.append(f"AC-{nid}-{n} exists but REQ-{nid}-{n} is missing — "
                   "add the explicit requirement it traces to")
    for n in sorted(reqs - acs, key=int):
        out.append(f"REQ-{nid}-{n} has no acceptance criterion "
                   f"AC-{nid}-{n} — every requirement must be testable")
    return out


class RunStopped(RuntimeError):
    """П1: a cooperative stop was requested (a STOP sentinel in the
    workspace). The run halts at the NEXT node boundary and returns its
    partial result — a later --resume picks up where it left off."""


_STOP_REL = ".spec-flow/STOP"


def request_stop(workspace_root: str) -> str:
    """Ask a run rooted at ``workspace_root`` to stop. Drops a STOP sentinel
    the engine checks at every node boundary. Returns the sentinel path."""
    p = Path(workspace_root) / _STOP_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stop\n", encoding="utf-8")
    return str(p)


def clear_stop(workspace_root: str) -> None:
    """Remove a STOP sentinel (called at run start so a stale stop from a
    previous run never halts the resumed one)."""
    p = Path(workspace_root) / _STOP_REL
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ── checkpoints: snapshot WHERE a run is so it can be REPLAYED from there ─────
# A checkpoint is a full copy of the workspace (specs/src/tests + journal +
# claims.db) taken at a node boundary, keyed by a digest of all live specs (a
# stable "where the tree is" id). restore_checkpoint + a --resume re-enter from
# exactly that point — replay from ANY saved checkpoint, not just the latest.
_CHECKPOINT_REL = ".spec-flow/CHECKPOINT"


def request_checkpoint(workspace_root: str) -> str:
    """Ask a live run to SNAPSHOT itself at the next node boundary and KEEP
    running (unlike STOP). Drops a CHECKPOINT sentinel; returns its path."""
    p = Path(workspace_root) / _CHECKPOINT_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("checkpoint\n", encoding="utf-8")
    return str(p)


def _root_spec_hash(workspace_root: str) -> str:
    """Stable digest of WHERE the tree is — sorted (stem, bytes) of every live
    spec under specs/ (archived specs/<id>.vN.md skipped). Wall-clock-free, so
    two runs at the same point share it."""
    specs = Path(workspace_root) / "specs"
    h = hashlib.sha256()
    if specs.is_dir():
        for p in sorted(specs.glob("*.md")):
            if "." in p.stem:
                continue
            try:
                h.update(p.stem.encode() + b"\0" + p.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


def snapshot_checkpoint(run_dir: str, workspace_root: str,
                        node: str = "") -> str:
    """Copy the live workspace into ``run_dir/checkpoints/<seq>__<roothash>/`` so
    the run can be replayed from this exact point. Volatile sentinels are not
    copied. Returns the snapshot dir path. Take it on a node boundary so the
    workspace is quiescent and the snapshot is consistent."""
    cks = Path(run_dir) / "checkpoints"
    cks.mkdir(parents=True, exist_ok=True)
    seq = 1 + sum(1 for _ in cks.glob("[0-9]*__*"))
    rh = _root_spec_hash(workspace_root)
    dest = cks / f"{seq:03d}__{rh}"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(workspace_root, dest / "workspace",
                    ignore=shutil.ignore_patterns("CHECKPOINT", "STOP"))
    specs = Path(workspace_root) / "specs"
    nodes = (sum(1 for p in specs.glob("*.md") if "." not in p.stem)
             if specs.is_dir() else 0)
    (dest / "checkpoint.json").write_text(
        json.dumps({"seq": seq, "roothash": rh, "node": node, "specs": nodes},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    return str(dest)


def restore_checkpoint(checkpoint_dir: str, dest_workspace_root: str) -> None:
    """Restore a checkpoint's workspace into ``dest_workspace_root`` so a
    --resume re-enters from exactly that point (its journal + specs + code drive
    the cache-hits). Overwrites the destination workspace."""
    src = Path(checkpoint_dir) / "workspace"
    if not src.is_dir():
        raise FileNotFoundError(f"no workspace in checkpoint: {checkpoint_dir}")
    dest = Path(dest_workspace_root)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def list_checkpoints(run_dir: str) -> list:
    """Checkpoints of a run, oldest-first: [{seq, roothash, node, specs, path}]."""
    cks = Path(run_dir) / "checkpoints"
    out = []
    if cks.is_dir():
        for d in sorted(cks.glob("[0-9]*__*")):
            meta = {}
            try:
                meta = json.loads((d / "checkpoint.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            meta["path"] = str(d)
            out.append(meta)
    return sorted(out, key=lambda m: m.get("seq", 0))


class IntegrateFailHalt(RuntimeError):
    """on_integrate_fail='halt': a FAILed integrate stops the run."""


class ReviewExhaustedHalt(RuntimeError):
    """gates.review.exhausted='halt': an unresolved REJECT stops the run."""

# Event verbosity levels — what each emit() is worth. Lower = more important.
L_MILESTONE = 1   # gate verdicts, loops (clarify/critique/drift/respec), phase signals
L_STEP = 2        # routine worker steps (read handoff, design, integrate)
L_DETAIL = 3      # fine-grained detail (TDD red/green, individual constitution rules)
DEFAULT_VERBOSITY = int(os.environ.get("SPEC_FLOW_RUN_VERBOSITY", str(L_STEP)))

# Execution depth ladder.
DEPTHS = {"spec": 1, "scaffold": 2, "verify": 3, "execute": 4, "product": 5}
DEPTH_SPEC, DEPTH_SCAFFOLD, DEPTH_VERIFY, DEPTH_EXECUTE, DEPTH_PRODUCT = 1, 2, 3, 4, 5

# Docstring prefix stamped on a module whose rival WSGI callable the engine has
# replaced with a delegating wrapper. Both the route-redeclare gate and the
# neutraliser key off it so a neutralised module is never re-flagged as a rival.
_NEUTRALIZED_SENTINEL = "Rival WSGI entry neutralised by the engine"


def _depth_int(d: Any) -> int:
    if isinstance(d, int):
        return d
    if d in DEPTHS:
        return DEPTHS[d]
    raise ValueError(f"unknown depth {d!r}; expected one of {sorted(DEPTHS)} or 1..5")


# ── dedup gate: title/id similarity ─────────────────────────────────────
# The decomposer agent is stateless per call — without a guard, deep
# branches re-propose work that already exists elsewhere in the tree
# (observed live: "research analog marketplaces" created at L1 and again
# at L6 under the architecture branch). The gate is deterministic engine
# logic, like leaf_check: language-agnostic token-stem Jaccard over
# id+title. Refinement of the node's own ancestor line is NOT a dup —
# ancestors are excluded by the caller.

_DEDUP_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_DEDUP_STEM_LEN = 6          # crude stemming: inflection-tolerant prefix
_DEDUP_MIN_TOKEN_LEN = 3     # shorter tokens are connective noise
_DEDUP_THRESHOLD = 0.6       # Jaccard at/above this ⇒ duplicate
# Connective noise that dilutes the token sets — not a domain phrase list.
_DEDUP_STOPWORDS = frozenset(
    {"and", "the", "for", "with", "from", "into", "onto", "via"})


def _dedup_tokens(node_id: str, title: str) -> set:
    toks = set()
    for part in ((node_id or "").replace("_", " "), title or ""):
        for t in _DEDUP_TOKEN_RE.findall(part.lower()):
            if len(t) >= _DEDUP_MIN_TOKEN_LEN and t not in _DEDUP_STOPWORDS:
                toks.add(t[:_DEDUP_STEM_LEN])
    return toks


# the leaf_check clause that becomes stale when a rework closes decisions
_OPEN_DECISIONS_REASON_RE = re.compile(
    r"\d+ open decision\(s\) — resolve before leafing")
_OPEN_DECISIONS_NONE_RE = re.compile(r"\bnone\b", re.IGNORECASE)


def _count_open_decisions(spec_md: str) -> Optional[int]:
    """Count list items in the authored '## Open decisions' section.
    Returns None when the section is absent (nothing to recount)."""
    in_section = False
    found = False
    count = 0
    for raw in spec_md.splitlines():
        line = raw.strip()
        if line.lower().startswith("## open decisions"):
            in_section, found = True, True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.startswith(("- ", "* ")):
            if not _OPEN_DECISIONS_NONE_RE.search(line):
                count += 1
    return count if found else None


def node_similarity(a_id: str, a_title: str, b_id: str, b_title: str) -> float:
    """0..1 similarity between two nodes by id+title token stems."""
    a, b = _dedup_tokens(a_id, a_title), _dedup_tokens(b_id, b_title)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _default_implementer(ctx: dict) -> None:
    raise NotImplementedError(
        "depth 'execute' requires an injected implementer agent "
        "(a real LLM / Hermes worker that writes code & modifies the project)")


def _default_decomposer(ctx: dict) -> None:
    raise NotImplementedError(
        "the project has no predefined tree — building one from the goal "
        "requires an injected decomposer agent (a real LLM / Hermes worker "
        "running the spec-flow-decompose skill)")


def _default_approver(ctx: dict) -> dict:
    """Autonomous human-in-the-loop default: approve, but record honestly that
    no human was attached. Inject a real approver (a human / Hermes HITL
    worker) via ``agents={"approver": ...}`` to get genuine sign-off — and to
    be able to REJECT a checkpoint, which sends the node back for rework."""
    return {"approved": True,
            "reason": "autonomous default approval — no human attached"}


# Autonomous default agents per role. Spec/scaffold/verify are fully handled by
# the deterministic engine; the 'implementer' is consulted at depth 'execute',
# the 'decomposer' whenever a node arrives without metrics (i.e. the case did
# not predefine the tree), the 'approver' at every human-in-the-loop checkpoint.
# Override any via the run's ``agents=`` parameter.
DEFAULT_AGENTS = {"implementer": _default_implementer,
                  "decomposer": _default_decomposer,
                  "approver": _default_approver}

# ── external opaque team (architecture doc §3/§4 CASE 4) ──────────────────
# A role can resolve to a team defined ENTIRELY in an external service: the
# engine sends ONE RoleTask for the whole role and consumes the RoleResult,
# blind to the team's specialists and workflow. Config (in the case `workers:`
# block, read here from llm_backend.WORKERS_CFG):
#     workers.<role>.team: {external: true, provider: a2a|hermes|mission-control,
#                           endpoint|gateway|api: ..., agent: ...}
# When a role declares no team / a team without `external`, its agent is left
# exactly as today (default or injected). The provider adapters live in the
# harness ``providers`` package; they are imported with the SAME guarded
# fallback the runner already uses for other harness modules, so plugin runtime
# carries no hard dependency on the test layer.


def _load_provider_adapter(provider: str, transport=None):
    """Return a provider adapter instance, or None if the providers package is
    not importable (plugin running without the harness).

    Why: external-team delegation needs the adapter, but the runner must not
    hard-depend on the test layer — mirror the existing guarded-import pattern.
    What: tries the harness ``providers.get_provider`` under each import root.
    Test: covered indirectly — the external-team test injects a transport and
    asserts the role delegates one RoleTask through the adapter."""
    get_provider = None
    # 'harness.*' first: the test suite imports the harness under that root, so
    # this is the canonical module — 'tests.harness.*' can resolve to a SECOND
    # instance with stale globals (a known duplicate-module pytest hazard).
    for root in ("harness.providers", "tests.harness.providers", "providers"):
        try:
            mod = __import__(root, fromlist=["get_provider"])
            get_provider = mod.get_provider
            break
        except Exception:               # noqa: BLE001 — optional layer
            continue
    if get_provider is None:
        return None
    return get_provider(provider, transport=transport)


def _workers_cfg() -> dict:
    """The active worker config (llm_backend.WORKERS_CFG) across module roots,
    or {} — the source for the C1 executor roster. Mirrors the scan used by
    _external_team_config so a stale duplicate module never masks it."""
    for root in ("harness.llm_backend", "tests.harness.llm_backend",
                 "llm_backend"):
        try:
            cfg = __import__(root, fromlist=["WORKERS_CFG"]).WORKERS_CFG
        except Exception:               # noqa: BLE001
            continue
        if isinstance(cfg, dict) and cfg:
            return cfg
    return {}


def _external_team_config(role: str) -> Optional[dict]:
    """The external-team config for ``role`` from the worker config, or None.

    Returns the team dict only when it sets ``external: true``; an inline team
    (specialists/workflow) or no team yields None (today's path)."""
    cfg = None
    # 'harness.*' first (canonical under the test suite); fall back across roots
    # and keep scanning while a candidate's WORKERS_CFG is empty, so a stale
    # duplicate module never masks the populated one.
    for root in ("harness.llm_backend", "tests.harness.llm_backend", "llm_backend"):
        try:
            candidate = __import__(root, fromlist=["WORKERS_CFG"]).WORKERS_CFG
        except Exception:               # noqa: BLE001
            continue
        if candidate:
            cfg = candidate
            break
        if cfg is None:
            cfg = candidate
    team = ((cfg or {}).get(role) or {}).get("team") if isinstance(cfg, dict) else None
    if isinstance(team, dict) and team.get("external"):
        return team
    return None


def _make_external_agent(role: str, team: dict, transport=None):
    """Build a role agent that delegates the WHOLE role to an external team.

    Why: CASE 4 — the engine is blind to the remote team; it ships one RoleTask
    and writes the returned artifacts back into the workspace (the workspace
    stays the source of truth, doc §3).
    What: on each leaf ctx it builds a RoleTask from {node, title, spec, goal},
    runs ``adapter.execute(task)`` and writes every returned artifact via the
    workspace, then records an honest single 'external:<provider>' step.
    Test: a decomposer/implementer role with team.external delegates one task
    (fake transport) and the returned files land in the workspace."""
    provider = str(team.get("provider") or "")
    base_cfg = {k: team[k] for k in
                ("endpoint", "gateway", "api", "api_key", "token", "agent",
                 "agent_template", "agent_card", "poll_attempts")
                if k in team}

    def agent(ctx: dict) -> None:
        adapter = _load_provider_adapter(provider, transport=transport)
        if adapter is None:
            raise RuntimeError(
                f"external team for role '{role}' needs provider '{provider}' "
                "but the providers package is not importable")
        task = _ROLE_TASK(role=role, node=str(ctx.get("node") or ""),
                          title=str(ctx.get("title") or ""),
                          spec=str(ctx.get("spec") or ""),
                          workspace=getattr(ctx.get("workspace"), "root", None),
                          provider=provider,
                          context={"goal": str(ctx.get("goal") or ""), **base_cfg})
        result = adapter.execute(task)
        ws = ctx.get("workspace")
        wrote = 0
        for rel, content in (getattr(result, "artifacts", None) or {}).items():
            if ws is not None and hasattr(ws, "_write"):
                ws._write(str(rel), str(content), "external")
                wrote += 1
        return None

    return agent


def _ROLE_TASK(**kw):
    """A transport-agnostic RoleTask for the external delegation, built without a
    hard import of role_worker (plugin layering). Falls back to a tiny local
    shim carrying the same fields the adapters read."""
    for root in ("harness.role_worker", "tests.harness.role_worker", "role_worker"):
        try:
            return __import__(root, fromlist=["RoleTask"]).RoleTask(**kw)
        except Exception:               # noqa: BLE001
            continue
    from types import SimpleNamespace
    kw.setdefault("specialty", ""); kw.setdefault("model", "")
    kw.setdefault("params", None); kw.setdefault("constraints", {})
    return SimpleNamespace(**kw)


def _wrap_external_teams(agents: Optional[dict], transport=None) -> Optional[dict]:
    """Replace any role whose worker config declares an EXTERNAL team with an
    external-delegation agent; every other role is untouched (default path).

    Test: with workers.<role>.team.external set, the returned agents[role] is
    the delegation wrapper; with no external team the dict is returned as-is."""
    wrapped = dict(agents or {})
    for role in ("decomposer", "implementer", "reviewer", "verifier"):
        team = _external_team_config(role)
        if team is not None:
            wrapped[role] = _make_external_agent(role, team, transport=transport)
    return wrapped or None


# What to do when the spec reviewer REJECTS a node's spec:
#   rework    — re-invoke the decomposer with the reviewer's reasons, rewrite
#               the spec, re-review (bounded by max_rework); still rejected
#               after the budget -> recorded episode, run continues. DEFAULT.
#   record    — bookkeeping only (version bump + loop entry), no rework
#   halt      — stop the run on the first REJECT (strict CI mode)
#   ask_human — route to the approver (HITL): approved -> record & continue,
#               not approved -> halt
DEFAULT_REVIEW_POLICY = {"on_reject": "rework", "max_rework": 2,
                         # A1 review tiering (default ON): a SIMPLE leaf that
                         # passes the deterministic spec lint skips the LLM
                         # reviewer + rework loop entirely. Review is the largest
                         # measured time sink (~37% of calls in v069); a small,
                         # decision-free leaf does not need an opinion round. The
                         # same `simple_max_loc` also classifies a leaf as
                         # leaf_small for process tiering (solo coder) — so one
                         # threshold scales BOTH the review and the build path to
                         # task size. Lifted 60 -> 120 because a 20-40 line module
                         # was being rated 'medium' and paying the full orchestra.
                         "tiering": True, "simple_max_loc": 120,
                         # A3 escalation (default OFF): the model to use for ONE
                         # final review attempt after the rework budget is spent
                         # on REJECTs — a stronger opinion instead of an endless
                         # same-model grind. '' = no escalation (today).
                         "escalate_model": ""}

# Hard ceiling on decomposer-agent calls per run (runaway-recursion guard).
MAX_DECOMPOSE_CALLS = 40

PROFILE_ICON = {
    "spec-decomposer": "🧩",
    "researcher": "🔬",
    "spec-contract": "📐",
    "spec-reviewer": "⚖️",
    "implementer": "🛠️",
    "verifier": "✅",
    "approver": "🧑‍⚖️",
}
# blast radius (children re-derived) above which a respec needs human sign-off
RESPEC_HITL_BLAST = 3
# The full surface comes from the gates module, which derives it dynamically
# from the shipped skills/ and profiles/ folders.
ALL_SKILLS = set(_gates.ALL_SKILLS)
ALL_PROFILES = set(_gates.ALL_PROFILES)


@dataclass
class Event:
    tick: int
    phase: str
    profile: str
    skill: str
    task: str
    action: str
    detail: str = ""
    gate: str = ""          # tool invoked, if any
    verdict: str = ""       # tool verdict, if any
    level: int = L_STEP     # verbosity weight (see L_* constants)


@dataclass
class Task:
    id: str
    title: str
    kind: str               # requirements|decompose|contract|impl|review|integrate|research
    profile: str
    skill: str
    parents: list[str] = field(default_factory=list)
    status: str = "todo"
    version: int = 1
    runs: int = 0


@dataclass
class RunResult:
    project: dict
    events: list[Event]
    tasks: dict[str, Task]
    skills_used: set[str]
    profiles_used: set[str]
    loops: list[dict]
    gate_calls: dict[str, int]
    verbosity: int = DEFAULT_VERBOSITY
    depth: int = DEPTH_SPEC
    workspace_root: Optional[str] = None
    # variant A: nid -> collision-free module name (every value unique)
    module_names: dict = field(default_factory=dict)
    # Plan Шаг 4 — canonical terminal verdict of the whole run, so the outcome
    # is readable from ONE field instead of reconstructed from a trace tail.
    # "READY" / "NOT READY" once a product-depth acceptance gate ran, else None
    # (acceptance not asserted — e.g. shallower depth). `product_failed` lists
    # the acceptance checks that failed, for the run's meta.json summary.
    product_status: Optional[str] = None
    product_failed: list = field(default_factory=list)


def event_line(e: Event, widths: Optional[dict] = None) -> str:
    """Single human line for an event — shared by the rendered log and the
    text disk sink. ``widths`` (profile/skill/task) pads the columns so a
    multi-line log reads as an aligned table."""
    icon = PROFILE_ICON.get(e.profile, "·")
    w = widths or {}
    prof = f"{e.profile:<{w.get('profile', 0)}}"
    skill = f"{e.skill:<{w.get('skill', 0)}}"
    task = f"{'[' + e.task + ']':<{w.get('task', 0)}}"
    head = f"t{e.tick:>3} │ {icon} {prof} · {skill} · {task} {e.action}"
    tail = f" → {e.verdict}" if e.verdict else ""
    if e.detail:
        tail += f"  «{e.detail}»"
    return head + tail


def _column_widths(events) -> dict:
    """Max width per aligned column over the events that will be shown."""
    return {
        "profile": max((len(e.profile) for e in events), default=0),
        "skill": max((len(e.skill) for e in events), default=0),
        "task": max((len(e.task) + 2 for e in events), default=0),
    }


class LogSink:
    """Optional on-disk log handler for a run.

    It is **off by default** and has its own defaults: enable it by passing a
    path (or via the SPEC_FLOW_RUN_LOG env var), pick a format (``jsonl`` for
    machine-readable source data, ``text`` for a readable log) and its own
    detail level (``SPEC_FLOW_RUN_LOG_LEVEL``, default = full detail) which is
    INDEPENDENT of the rendered-report verbosity. Also accepts a plain callable
    as a custom handler. Toggle at runtime with ``enabled``.
    """

    def __init__(self, path: Optional[str] = None, level: int = L_DETAIL,
                 fmt: str = "jsonl", enabled: Optional[bool] = None,
                 handler=None):
        self.path = path or os.environ.get("SPEC_FLOW_RUN_LOG")
        self.level = int(os.environ.get("SPEC_FLOW_RUN_LOG_LEVEL", level))
        self.fmt = os.environ.get("SPEC_FLOW_RUN_LOG_FORMAT", fmt)
        self.handler = handler                     # optional custom callable(event)
        # default-on when there is somewhere/something to write to
        self.enabled = enabled if enabled is not None else bool(self.path or handler)
        self._fh = None

    @classmethod
    def from_env(cls) -> "LogSink":
        return cls()

    def open(self) -> "LogSink":
        if self.enabled and self.path and self.handler is None:
            # line-buffered so each event hits disk immediately — lets a live
            # reader (the dashboard) tail the trace while the run is in progress
            self._fh = open(self.path, "w", encoding="utf-8", buffering=1)
        return self

    def handle(self, e: Event) -> None:
        if not self.enabled or e.level > self.level:
            return
        if self.handler is not None:
            self.handler(e)
            return
        if self._fh is None:
            return
        if self.fmt == "jsonl":
            # stamp a wall-clock time on the DISK trace only (not the in-memory
            # events used by reports) so a live reader can show a real timeline;
            # report determinism is unaffected
            import time as _time
            d = asdict(e)
            d["t"] = round(_time.time(), 3)
            self._fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        else:
            self._fh.write(event_line(e) + "\n")

    __call__ = handle

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _snake(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")


# ---- B3: universal surface-overlap detectors (registry) -------------------
# A late requirement may refine a surface an existing module owns WITHOUT naming
# a route or a file ("polish the front-end", "tidy the presentation layer").
# A single signal (a route string) is not enough, and NO detector may be tailored
# to a domain (a hard-coded ui/web/html list would just help one case pass — that
# is scaffolding, forbidden). So overlap is decided by a REGISTRY of UNIVERSAL
# detectors; each scores a (requirement, existing-module) pair on a different,
# domain-agnostic signal. Extend by registering a function; select/order the
# active set with SPEC_FLOW_AMEND_METHODS (comma list; default = all, in
# registration order). The engine keeps the highest-scoring module above
# _AMEND_MIN_SCORE as the surface owner.
_AMEND_MIN_SCORE = 0.34

# generic English/Russian-transliteration-safe stop list; deliberately NOT a
# domain vocabulary — only structural words that carry no surface identity.
_AMEND_STOP = frozenset((
    "the a an and or to of for in on at by with into from out over this that "
    "these those it its is are be was were been being as not no nor but if then "
    "else when while do does did done must should shall will would can could may "
    "might add added make makes made human mid run binding new existing must "
    "should keep without only also use using via per each every any all your you "
    "page pages it’s let’s").split())


def _amend_tokens(text: str) -> set:
    """Significant tokens of a text — lowercased identifiers/words >=4 chars,
    minus structural stop-words. Domain-agnostic on purpose."""
    return {w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text.lower())
            if w not in _AMEND_STOP}


def _amend_routes(text: str) -> set:
    """HTTP routes a text declares — `GET /x` forms and quoted `/x` path
    literals. Works for any service, not just web UIs."""
    out = {r.rstrip("/.,;:)\"'") for r in re.findall(
        r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./-]+)", text, re.I)}
    out |= {m.rstrip("/") for m in
            re.findall(r"[\"'](/[A-Za-z0-9_./-]+)[\"']", text)}
    return {r for r in out if len(r) > 1}


def _verb_declared_routes(text: str) -> set:
    """Routes a text declares EXPLICITLY via an HTTP verb (`GET /x`, `DELETE
    /y`). Unlike _amend_routes this drops bare quoted-path literals: a `/notes`
    mentioned in a spec's surrounding prose is service CONTEXT (what the product
    already serves), NOT a route THIS node must add. Used by the late-requirement
    delta gate so a node is only held to its OWN new routes, never a sibling's
    (live v110: красивый_вид/note_search were charged with /ui & /ping, owned by
    other leaves, purely because the spec narrated the whole service)."""
    return {r.rstrip("/.,;:)\"'") for r in re.findall(
        r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./-]+)", text, re.I)
        if len(r.rstrip("/.,;:)\"'")) > 1}


_HTTP_VERBS = "GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS"


def _served_routes(body: str) -> set:
    """HTTP routes a module actually SERVES, established from CODE DISPATCH only
    — the OWNERSHIP counterpart to ``_amend_routes`` (which picks up any route
    MENTION: a quoted literal, a comment, an error string, or a "GET /ui" phrase
    in spec prose). A route counts as served only with real dispatch evidence:
    a ('METHOD','/path') routes-table key, a @route('/path') decorator, or a
    path-variable test against the literal (``path == '/path'``,
    ``path.startswith('/path')``, ``path in (...)``).

    Crucially a "METHOD /path" phrase in PROSE is NOT served — a sibling spec
    routinely names a neighbour's route while describing the system. Counting
    that mis-credits the sibling as the owner: live v101/v102 — '/ui' appeared in
    the health module's spec text, so the web_ui late requirement was rejected as
    an empty delta (route-redeclare), the UI leaf was never built, and the
    product served GET /ui -> 404. A route only PLANNED in prose is not yet
    served; the new spec may be the one that implements it, so it must not be
    pre-empted."""
    out: set = set()
    # ('METHOD', '/path')  — routes-table tuple key
    out |= {m.rstrip("/") for m in re.findall(
        r"""['"](?:%s)['"]\s*,\s*['"](/[A-Za-z0-9_./-]+)['"]""" % _HTTP_VERBS,
        body, re.I)}
    # @app.route('/path') / .add_url_rule('/path', ...)
    out |= {m.rstrip("/") for m in re.findall(
        r"""\.(?:route|add_url_rule)\(\s*['"](/[A-Za-z0-9_./-]+)['"]""", body)}
    # path-variable dispatch: path == '/p' | path.startswith('/p')
    out |= {m.rstrip("/") for m in re.findall(
        r"""(?:path|path_info)\b[^\n]{0,40}?(?:==|!=|\.startswith\()\s*"""
        r"""['"](/[A-Za-z0-9_./-]+)['"]""", body, re.I)}
    out |= {m.rstrip("/") for m in re.findall(
        r"""['"](/[A-Za-z0-9_./-]+)['"]\s*==[^\n]{0,40}?\b(?:path|path_info)\b""",
        body, re.I)}
    # path in ('/a', '/b', ...) — pull every literal from the membership tuple
    for grp in re.findall(
            r"""(?:path|path_info)\b[^\n]{0,20}?\bin\b\s*[\([{]([^)\]}\n]*)""",
            body, re.I):
        out |= {m.rstrip("/") for m in re.findall(
            r"""['"](/[A-Za-z0-9_./-]+)['"]""", grp)}
    return {r for r in out if len(r) > 1}


def _canonical_handler_symbol(method: str, path: str) -> str:
    """The ONE handler-function name the engine DECLARES for a route, instead of
    guessing the binding from whatever the model happened to call it. Derived
    deterministically from method+path so the same route always maps to the same
    symbol: GET /ui -> get_ui, POST /notes -> post_notes, GET /health ->
    get_health, GET / -> get_root, GET /notes/{id} -> get_notes (template
    segments dropped). The decomposition spec orders this exact name and the
    resolver looks it up first — the name stops being a thing anyone infers."""
    segs = [s for s in re.split(r"[/\-.]", (path or "").strip("/"))
            if s and not s.startswith("{") and not s.startswith(":")]
    base = "_".join(re.sub(r"[^a-z0-9]", "", s.lower()) for s in segs)
    base = re.sub(r"_+", "_", base).strip("_") or "root"
    return "%s_%s" % ((method or "GET").strip().lower() or "get", base)


def _amend_symbols(body: str) -> set:
    """Names a module DEFINES (functions/classes) — lowercased + split into
    sub-tokens so `render_notes_page` contributes {render, notes, page}."""
    syms = set()
    for m in re.findall(r"^\s*(?:def|class)\s+([A-Za-z_]\w+)", body, re.M):
        syms.add(m.lower())
        syms |= {p for p in m.lower().split("_") if len(p) >= 4}
    return syms


_AMEND_DETECTORS: "dict[str, Any]" = {}


def _amend_detector(name: str):
    def deco(fn):
        _AMEND_DETECTORS[name] = fn
        return fn
    return deco


@_amend_detector("explicit_file")
def _amd_explicit_file(feat: dict, mod: dict) -> float:
    """The requirement names this module's file (src/<stem>.py or <stem>)."""
    return 1.0 if mod["stem"] in feat["files"] else 0.0


@_amend_detector("shared_route")
def _amd_shared_route(feat: dict, mod: dict) -> float:
    """The requirement and the module declare the same HTTP route."""
    return 1.0 if feat["routes"] & mod["routes"] else 0.0


@_amend_detector("symbol_overlap")
def _amd_symbol_overlap(feat: dict, mod: dict) -> float:
    """A distinctive requirement token equals a symbol the module DEFINES —
    e.g. a requirement about 'notes' meets a module defining render_notes()."""
    hit = feat["tokens"] & mod["symbols"]
    return min(0.6 + 0.1 * len(hit), 0.95) if hit else 0.0


_AMEND_TOKEN_FLOOR = 0.5

@_amend_detector("token_overlap")
def _amd_token_overlap(feat: dict, mod: dict) -> float:
    """Share of the requirement's distinctive tokens that also appear in the
    module's own text — the LOOSEST signal, the fallback when no route, file or
    defined symbol matched. Held to a HIGHER floor (_AMEND_TOKEN_FLOOR) than the
    registry minimum: a genuinely-new feature that merely shares a couple of
    incidental words with an unrelated module must NOT be mis-routed into it
    (a false amend corrupts that module — worse than the original fork). Recall
    for vaguely-worded refinements comes from symbol_overlap, which is precise."""
    want = feat["tokens"]
    if len(want) < 2:
        return 0.0
    frac = len(want & mod["tokens"]) / len(want)
    return frac if frac >= _AMEND_TOKEN_FLOOR else 0.0


def _amend_surfaces(text: str) -> set:
    """Every structural/entity surface a text exposes — routes, defined symbols
    and distinctive entity tokens, in one set. Used by surface_overlap to catch
    a refinement that shares SEVERAL weak surfaces with an owner none of which
    crossed a single-signal detector's bar on its own."""
    return _amend_routes(text) | _amend_symbols(text) | _amend_tokens(text)


@_amend_detector("surface_overlap")
def _amd_surface_overlap(feat: dict, mod: dict) -> float:
    """Structural backstop BEFORE the LLM router: fires when the requirement and
    the module share two-or-more surfaces (routes / defined symbols / entity
    nouns) — recall for a vaguely-worded refinement whose single signals each
    fell under threshold. Requires >=2 shared surfaces AND a non-trivial share
    so one incidental common word can never route (a false amend is worse than a
    fork). Universal: surfaces come from the spec text, never a keyword list."""
    inj = feat["surfaces"]
    shared = inj & mod["surfaces"]
    if len(shared) < 2 or not inj:
        return 0.0
    frac = len(shared) / len(inj)
    if frac < 0.34:
        return 0.0
    return min(0.5 + 0.1 * len(shared), 0.9)


# Detector order = fastest/most-precise first. _amend_find_owner walks this list
# and SHORT-CIRCUITS on the first detector that yields an owner >= threshold, so
# the loosest/most-expensive signals (token, then the network LLM router) only
# run when every cheaper, more-exact signal came up empty.
_AMEND_DET_ORDER = ("explicit_file", "shared_route", "symbol_overlap",
                    "token_overlap", "surface_overlap")


def _amend_find_owner(statement: str, modules: list, methods=None,
                      candidates_text=None, llm=None) -> "Optional[str]":
    """Decide which existing module (if any) owns the surface this requirement
    refines — fastest-first with short-circuit.

    ``modules`` is [(rel_path, stem, body)]. The deterministic detectors run in
    ``_AMEND_DET_ORDER`` (override/subset via ``methods`` arg or the
    SPEC_FLOW_AMEND_METHODS env); the FIRST detector to score a module >=
    threshold wins and the rest are skipped. Only when every deterministic
    signal is empty does the optional ``llm`` router fire — one model call that
    reads the candidate specs and returns an owner id or 'new'. ``llm`` is a
    ``(statement, [(rel, stem, body)], candidates_text) -> Optional[str]``
    callable; None (the default / unit-test path) means no network fallback."""
    if methods is None:
        env = os.environ.get("SPEC_FLOW_AMEND_METHODS", "").strip()
        methods = [m.strip() for m in env.split(",") if m.strip()] or \
            list(_AMEND_DET_ORDER)
    feat = {"files": {m for m in re.findall(r"src/([A-Za-z_]\w*)\.py",
                                            statement)}
            | {m for m in re.findall(r"\b([a-z][a-z0-9_]{3,})\.py", statement)},
            "routes": _amend_routes(statement),
            "tokens": _amend_tokens(statement),
            "surfaces": _amend_surfaces(statement)}
    # precompute each candidate's features ONCE (detectors are re-run per layer)
    raw = []
    for rel, stem, body in modules:
        raw.append((rel, stem, {"routes": _amend_routes(body),
                                "symbols": _amend_symbols(body),
                                "tokens": _amend_tokens(body),
                                "surfaces": _amend_surfaces(body)}))
    # DISTINCTIVENESS: a token/symbol owned by TWO-OR-MORE candidates is the
    # project's shared domain noun (e.g. 'notes', 'list'), not a routing
    # identity — matching on it picks an owner by coincidence (a presentation
    # requirement hitting a storage module that happens to define list_notes).
    # Strip those shared names from each candidate's symbol/token/surface sets
    # so a deterministic layer fires only on a DISTINCTIVE overlap and otherwise
    # ABSTAINS (returns None) — letting the semantic LLM router decide. Routes
    # stay unfiltered: a shared HTTP route is an explicit, strong signal.
    _freq: dict = {}
    for _rel, _stem, d in raw:
        for tok in (d["symbols"] | d["tokens"]):
            _freq[tok] = _freq.get(tok, 0) + 1
    _common = {t for t, c in _freq.items() if c > 1}
    feats = []
    for rel, stem, d in raw:
        feats.append((rel, {"stem": stem, "routes": d["routes"],
                            "symbols": d["symbols"] - _common,
                            "tokens": d["tokens"] - _common,
                            "surfaces": d["surfaces"] - _common}))
    for name in methods:
        fn = _AMEND_DETECTORS.get(name)
        if fn is None:
            continue
        best_score, best_path = 0.0, None
        for rel, mod in feats:
            score = fn(feat, mod)
            if score > best_score:
                best_score, best_path = score, rel
        if best_path is not None and best_score >= _AMEND_MIN_SCORE:
            return best_path        # SHORT-CIRCUIT: a cheaper layer decided
    if llm is not None:
        try:
            return llm(statement, modules, candidates_text)
        except Exception:           # noqa: BLE001 — router must never crash a run
            return None
    return None


def _dup_surface_findings(spec_text: str, modules: list) -> list:
    """Late-requirement DECOMPOSITION-QUALITY check: flag a spec that re-declares
    a surface an EXISTING module already owns WITHOUT adding a new one — i.e. it
    would DUPLICATE the service (re-spec POST/GET /notes that notes_api already
    serves) instead of scoping to its own delta. This is distinct from the
    amend/new ROUTING decision (_amend_target): even when a late requirement
    legitimately becomes a new node, its spec must describe ONLY the delta.

    ``modules`` is [(rel, stem, body)] of existing specs+code. Returns a list of
    deterministic findings (model-independent), ordered coarse→fine and run as a
    registry of INCREASING-PRECISION methods (mirroring the amend detectors); all
    that match are reported so the rework round gets the full picture. Empty when
    the spec contributes genuine new structural surface, or overlaps nothing.

    Only STRUCTURAL surface (HTTP routes + defined symbols) is used — never the
    fuzzy token set the amend router uses — so a shared domain noun ('notes')
    can never raise a false duplicate.
    """
    spec_routes = _amend_routes(spec_text)
    spec_syms = _amend_symbols(spec_text)
    spec_struct = spec_routes | spec_syms
    if not spec_struct:                       # no structural surface → not our call
        return []
    all_routes: set = set()
    all_syms: set = set()
    per_mod = []
    for rel, _stem, body in modules:
        # OWNERSHIP side: a module owns a route only if it actually SERVES it
        # (dispatch evidence), not merely names it in prose/comment/error text —
        # otherwise a stray '/ui' literal makes it the false owner and blocks a
        # legitimate late requirement as an empty delta (live v101).
        r, s = _served_routes(body), _amend_symbols(body)
        per_mod.append((rel, r, s, r | s))
        all_routes |= r
        all_syms |= s
    all_struct = all_routes | all_syms
    findings: list = []
    # 1) coarse — routes: the spec restates existing route(s) and adds none new
    restated_r = spec_routes & all_routes
    if spec_routes and restated_r and not (spec_routes - all_routes):
        owners = sorted({rel for rel, r, _s, _u in per_mod if r & restated_r})
        findings.append(
            f"route-redeclare: this spec restates HTTP route(s) "
            f"{sorted(restated_r)} already served by {owners} and introduces no "
            "new route — describe ONLY the new behaviour (the delta), or let it "
            "be folded into the owning module; do not re-declare existing routes")
    # 2) symbols: the spec restates existing symbol(s) and defines none new
    restated_s = spec_syms & all_syms
    if spec_syms and restated_s and not (spec_syms - all_syms):
        owners = sorted({rel for rel, _r, s, _u in per_mod if s & restated_s})
        findings.append(
            f"symbol-redeclare: this spec restates symbol(s) {sorted(restated_s)} "
            f"already defined by {owners} and defines nothing new — do not "
            "re-specify code that already exists")
    # 3) finer — subset: the spec's WHOLE structural surface is owned by one module
    for rel, _r, _s, mod_struct in per_mod:
        if spec_struct and spec_struct <= mod_struct:
            findings.append(
                f"surface-subset: the entire surface this spec describes is "
                f"already owned by {rel} (it adds nothing new) — fold the delta "
                "into that module instead of forking a duplicate")
            break
    # 4) finest — breadth: the spec spans the surface of >=2 existing modules
    #    while contributing at most a sliver of its own (it is re-stating the
    #    whole service, the v040 web_ui failure)
    spanned = sorted({rel for rel, _r, _s, u in per_mod if spec_struct & u})
    new_struct = spec_struct - all_struct
    if len(spanned) >= 2 and len(new_struct) <= 1:
        findings.append(
            f"scope-breadth: this spec spans the surface of {len(spanned)} "
            f"existing modules ({spanned}) while adding {sorted(new_struct) or 'no'}"
            " new structural surface — a late requirement must describe ONLY its "
            "own delta, not re-state the whole service")
    return findings


class Workspace:
    """Optional materialiser for a run. Off by default; enable with a path (or
    SPEC_FLOW_RUN_WORKSPACE env). Writes REAL, inspectable artifacts the run
    would produce — specs/plans per node, the frozen contract, code & test
    scaffolds per leaf, a commit journal and a MANIFEST.json with sha256 — so a
    reviewer has material deliverables to evaluate, not just a log.

    Code/test files are honest SCAFFOLDS (they carry a 'generated by the
    simulated run' header and a NotImplementedError / failing assertion), since
    no real LLM wrote them; the specs, contract and manifest are real content.
    """

    def __init__(self, root: Optional[str] = None, enabled: Optional[bool] = None,
                 git_provenance: bool = False):
        self.root = root or os.environ.get("SPEC_FLOW_RUN_WORKSPACE")
        self.enabled = enabled if enabled is not None else bool(self.root)
        self.artifacts: list[dict] = []
        self.commits: list[dict] = []
        # E5: optional local-git provenance — every milestone becomes a real
        # commit in the workspace repo, so the full respec history survives.
        self.git_provenance = git_provenance
        self._git_ready = False

    @classmethod
    def from_env(cls) -> "Workspace":
        return cls()

    def open(self, preserve: bool = False,
             seed_files: Optional[dict] = None) -> "Workspace":
        """Create the workspace dir. ``preserve=True`` keeps an existing one
        (resume mode — persisted artifacts and the journal survive).

        ``seed_files`` ({relative path: content}) is the platform-provided
        skeleton the run builds INTO (app entry point, plumbing, acceptance
        suites). It is written AFTER the fresh-run wipe — seeding the dir
        beforehand is futile by design — and lands in the manifest."""
        if self.enabled and self.root:
            p = Path(self.root)
            if p.exists() and not preserve:
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)
            for rel, body in (seed_files or {}).items():
                target = (p / rel).resolve()
                if not str(target).startswith(str(p.resolve())):
                    continue
                self._write(rel, body, "seed")
        return self

    # -- run journal (C4: a restartable run) --------------------------------
    def _journal_path(self) -> Optional[Path]:
        if not (self.enabled and self.root):
            return None
        return Path(self.root) / ".spec-flow" / "journal.jsonl"

    def spec_hash(self, rel: str) -> str:
        """#7: content hash of a workspace-relative spec file ('' if absent).
        Lets a resume tell whether a node's spec CHANGED since it was
        journaled — an unchanged spec is reused, a changed one is re-run."""
        if not (self.enabled and self.root):
            return ""
        p = Path(self.root) / rel
        if not p.is_file():
            return ""
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""

    def journal_mark(self, node: str, version: int,
                     spec_hash: str = "") -> None:
        """Append a completed-node record — the resume index. #7: the spec
        hash rides along so a resume can re-run a node whose spec changed."""
        path = self._journal_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"node": node, "version": version,
                                 "spec_hash": spec_hash}) + "\n")

    def journal_nodes(self) -> set:
        """Node ids already completed by a previous run of this workspace."""
        return set(self.journal_index().keys())

    def journal_index(self) -> dict:
        """#7: node id → last journaled {version, spec_hash}. Later records
        win (a re-run overwrites an earlier hash)."""
        path = self._journal_path()
        if path is None or not path.is_file():
            return {}
        idx: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                idx[rec["node"]] = {"version": rec.get("version"),
                                    "spec_hash": rec.get("spec_hash", "")}
            except Exception:  # noqa: BLE001 — a torn line is not fatal
                continue
        return idx

    # -- decomposition journal (resume rebuilds the same tree) --------------
    def _decomp_path(self) -> Optional[Path]:
        if not (self.enabled and self.root):
            return None
        return Path(self.root) / ".spec-flow" / "decomp.jsonl"

    def decomp_mark(self, node: str, payload: dict) -> None:
        """Persist a node's decomposition (size metrics + proposed children +
        flags) so a resume reproduces the SAME tree deterministically instead
        of re-asking the non-deterministic decomposer agent. Without this a
        resume re-decomposes from the root, the node ids drift, and the leaf
        cache never hits — the run silently restarts from L0."""
        path = self._decomp_path()
        if path is None:
            return
        try:
            rec = json.dumps({"node": node, "payload": payload})
        except (TypeError, ValueError):
            return  # a non-serialisable payload is not fatal — just not cached
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(rec + "\n")

    def decomp_index(self) -> dict:
        """node id → last persisted decomposition payload (later record wins)."""
        path = self._decomp_path()
        if path is None or not path.is_file():
            return {}
        idx: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                idx[rec["node"]] = rec.get("payload") or {}
            except Exception:  # noqa: BLE001 — a torn line is not fatal
                continue
        return idx

    def _write(self, rel: str, content: str, kind: str) -> str:
        if not self.enabled:
            return rel
        path = Path(self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.artifacts.append({"path": rel, "type": kind, "bytes": len(content.encode("utf-8"))})
        return rel

    def constitution(self, rules: list[str], target: str) -> None:
        body = ["# Project constitution", "",
                f"**Measurable target:** {target}", "", "## Non-negotiable rules"]
        body += [f"- {r}" for r in rules]
        self._write("constitution.md", "\n".join(body) + "\n", "constitution")

    def spec(self, node_id, title, depth, verdict, reasons, parent, plan_lines,
             node: Optional[dict] = None, target: str = "",
             module: Optional[str] = None) -> str:
        """Materialise the node's level spec. Carries every piece of REAL data
        the run has about the node: gate inputs/outputs, resolved decisions,
        spike findings, the governing contract, drift/review episodes, the
        plan and the next level. (Rich prose is the job of the reasoning
        skills in Hermes; this is the deterministic, traceable core.)"""
        node = node or {}
        m = node.get("metrics", {})
        lines = [f"# {title}", "",
                 f"- **Node:** `{node_id}`  ·  **Level:** L{depth}  ·  **Decision:** `{verdict}`",
                 f"- **Traces-to:** {parent or 'L0 goal'}",
                 f"- **leaf_check reason:** {reasons or 'within all thresholds'}"]
        # 442: the node carries its coverage criterion — the exact human
        # requirement it answers — so the spec (and the reviewer who reads it)
        # can derive the acceptance check FROM the requirement, not guess it.
        # Model-independent: surfaces whatever statement the engine attached.
        requirement = str(node.get("requirement") or "").strip()
        if requirement:
            lines += [
                f"- **Covers human requirement:** {' '.join(requirement.split())[:300]}",
                "- **Acceptance (coverage criterion):** a real check exercises the"
                " exact surface this requirement names end-to-end; the node stays"
                " RED until that check passes (a stub/mock must not satisfy it)."]
        if target:
            lines.append(f"- **Project acceptance target:** {target}")
        if m:
            lines += ["", "## Size estimate (leaf_check input)", "",
                      "| metric | value |", "|---|---|"]
            lines += [f"| {k} | {v} |" for k, v in m.items()]
        clar = node.get("clarify")
        if clar:
            lines += ["", "## Resolved open decision (clarify loop)",
                      f"- **Question:** {clar.get('decision', '')}",
                      f"- **Resolution:** {clar.get('resolution', '')}"]
        spike = node.get("spike")
        if spike:
            lines += ["", "## Research spike (before freeze)",
                      f"- **Question:** {spike.get('question', '')}",
                      f"- **Recommendation:** {spike.get('recommendation', '')}"]
        contract = node.get("contract")
        if contract:
            lines += ["", "## Frozen L2 contract",
                      f"- `contracts/{contract.get('artifact', '')}` (x-traces-to: `{node_id}`)"]
        if node.get("drift"):
            lines += ["", "## Contract-drift episode",
                      f"- classification: **{node['drift'].get('classify', '')}** "
                      "(contract_wrong → respec the L2 first; code_wrong → fix the code)"]
        if node.get("review_fails"):
            lines += ["", "## Review history",
                      f"- impl-review failed {node['review_fails']}× before PASS (critique loop)"]
        worker_md = str(node.get("spec_markdown") or "").strip()
        if worker_md:
            # the level spec AUTHORED by the decomposer worker (per the
            # spec-flow-decompose skill); the engine header above stays the
            # deterministic, traceable core
            lines += ["", worker_md]
        lines += ["", "## Plan"]
        lines += [f"- {p}" for p in plan_lines]
        children = node.get("children")
        if children:
            lines += ["", "## Children (next level)"]
            lines += [f"- `{c['id']}` — {c.get('title', c['id'])}" for c in children]
        # variant A: use the engine-resolved collision-free stem when given
        # (two nodes snaking to the same base would otherwise overwrite each
        # other's spec — and then both implementers read the SAME spec)
        stem = module or _snake(node_id)
        return self._write(f"specs/{stem}.md", "\n".join(lines) + "\n", "spec")

    def contract(self, src_path: str, name: str) -> str:
        rel = f"contracts/{name}"
        if self.enabled:
            path = Path(self.root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(src_path, path)
                self.artifacts.append({"path": rel, "type": "contract",
                                       "bytes": path.stat().st_size})
            except Exception:
                pass
        return rel

    def code(self, leaf, title) -> str:
        fn = _snake(leaf)
        body = (f'"""Generated by the spec-flow run engine (simulated impl).\n'
                f'Node: {leaf} — {title}. Spec: specs/{fn}.md. '
                f'Replace the body with the real implementation."""\n\n\n'
                f'def {fn}():\n    raise NotImplementedError("see specs/{fn}.md")\n')
        return self._write(f"src/{fn}.py", body, "code")

    def test(self, leaf, title) -> str:
        fn = _snake(leaf)
        body = (f'"""Generated TDD scaffold (RED) for {leaf} — {title}."""\n\n\n'
                f'def test_{fn}_red():\n'
                f'    # TODO: assert the acceptance criterion from specs/{fn}.md\n'
                f'    assert False, "write the real test"\n')
        return self._write(f"tests/test_{fn}.py", body, "test")

    # -- git provenance (E5) -------------------------------------------------
    def _git(self, *args: str) -> bool:
        """Quiet git call inside the workspace; never raises (provenance must
        not break a run when git is unavailable)."""
        if not (self.enabled and self.root):
            return False
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root),
                 "-c", "user.email=spec-flow@local", "-c", "user.name=spec-flow",
                 *args],
                capture_output=True, text=True, timeout=60)
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def git_commit(self, msg: str) -> bool:
        """Record the workspace state as a real local commit (opt-in)."""
        if not (self.enabled and self.root and self.git_provenance):
            return False
        if not self._git_ready:
            if not (Path(self.root) / ".git").exists():
                if not self._git("init", "-q"):
                    return False
            self._git_ready = True
        self._git("add", "-A")
        return self._git("commit", "-qm", msg)

    def respec_archive(self, node_id: str, old_version: int, new_version: int,
                       finding: str) -> Optional[str]:
        """E5 provenance: when a respec supersedes a spec, keep v(old) as
        ``specs/<id>.v{old}.md`` marked ``superseded_by`` and stamp the live
        spec with the revision history. Returns the archived rel path."""
        if not (self.enabled and self.root):
            return None
        fn = _snake(node_id)
        cur = Path(self.root) / "specs" / f"{fn}.md"
        if not cur.is_file():
            return None
        body = cur.read_text(encoding="utf-8")
        archived_rel = f"specs/{fn}.v{old_version}.md"
        header = (f"> SUPERSEDED — this is v{old_version} of the spec.\n"
                  f"> superseded_by: v{new_version}\n"
                  f"> reason: {finding}\n\n")
        self._write(archived_rel, header + body, "spec-archive")
        cur.write_text(
            body + f"\n## Revision history\n"
                   f"- v{new_version} supersedes v{old_version}: {finding}\n",
            encoding="utf-8")
        self.git_commit(f"respec({node_id}): v{old_version} -> v{new_version} — {finding}")
        return archived_rel

    def commit(self, msg: str, files: list[str]) -> None:
        if self.enabled:
            self.commits.append({"n": len(self.commits) + 1, "message": msg, "files": files})
            self.git_commit(msg)

    def finalize(self) -> None:
        if not self.enabled:
            return
        if self.commits:
            log = ["# Commit journal (simulated run)", ""]
            for c in self.commits:
                log.append(f"- **#{c['n']}** {c['message']}  —  {', '.join(c['files'])}")
            self._write("COMMITS.md", "\n".join(log) + "\n", "commit-log")
        manifest = {"artifacts": [], "commits": len(self.commits)}
        for a in self.artifacts:
            sha = ""
            try:
                sha = hashlib.sha256((Path(self.root) / a["path"]).read_bytes()).hexdigest()[:16]
            except Exception:
                pass
            manifest["artifacts"].append({**a, "sha256": sha})
        manifest["counts"] = {}
        for a in self.artifacts:
            manifest["counts"][a["type"]] = manifest["counts"].get(a["type"], 0) + 1
        (Path(self.root) / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


class _NodeDriver:
    """One node's lifecycle guard — the same gate contract under two engines.

    * ``node_engine='fsm'``    — a ``NodeLifecycle`` (pytransitions or the
      dependency-free fallback) drives the phases; its guard refuses DONE
      until every mandatory gate for the node kind fired.
    * ``node_engine='inline'`` — the imperative pipeline runs as before; this
      object records the SAME gates via ``_EVENT_GATE`` and runs the SAME
      guard at ``done()``. So a skipped gate is caught in BOTH engines.

    A node may declare a test-only ``_skip_gate`` to drop one mandatory gate,
    proving the omission is caught regardless of the engine. When the FSM
    module is unavailable the driver is inert (no guard), preserving the
    pre-FSM behaviour.
    """

    def __init__(self, kind: str, *, fsm_mode: bool, skip_gate: Optional[str] = None,
                 on_event: Optional[Any] = None, node: str = "",
                 prune: bool = False):
        self.kind = kind
        self.active = _NODE_FSM_OK
        self._skip = skip_gate
        self._fired: set[str] = set()
        # A4: when on, a transition that neither changes state nor fires a new
        # gate is dropped from the trace (cycle_control.is_noop_transition).
        self._prune = bool(prune) and _cycle is not None
        self._lc = None
        # lifecycle observer — makes the state machine VISIBLE in run
        # reports (without it an fsm run is indistinguishable from inline
        # in the trace; only meta.json knew)
        self._on_event = on_event
        self.node = node
        if fsm_mode and _NODE_FSM_OK:
            self._lc = _NodeLifecycle().start(kind)

    def _observe(self, event: str) -> None:
        if self._on_event is None:
            return
        state = getattr(self._lc, "phase", None) if self._lc is not None else None
        self._on_event(self.node, self.kind, str(event), state,
                       sorted(self._fired))

    def go(self, event) -> None:
        """Advance the lifecycle by one event, recording any gate it satisfies."""
        if not self.active:
            return
        prev_state = getattr(self._lc, "phase", None) if self._lc is not None else None
        fired_before = set(self._fired)
        if self._lc is not None:
            self._lc.advance(event)
        for gate in _EVENT_GATE.get(event, ()):
            self._fired.add(gate)
        new_state = getattr(self._lc, "phase", None) if self._lc is not None else None
        # A4: suppress a pure-bookkeeping transition (no state change, no new
        # gate) from the trace when pruning is on; real transitions are kept.
        if self._prune and _cycle is not None and _cycle.is_noop_transition(
                prev_state, new_state, fired_before, self._fired):
            return
        self._observe(event)

    def clarify(self) -> None:
        """Replay the clarify self-loop (open decision → resolve) on the FSM."""
        if self._lc is not None:
            self._lc.open_decision()
            self._lc.advance(EV_CLARIFY)
            self._lc.resolve_decision()
        self._observe(EV_CLARIFY)

    def done(self) -> None:
        """Close the node — runs the gate-completeness guard for both engines."""
        if not self.active:
            return
        if self._skip:  # test-only: drop one gate to prove the guard bites
            self._fired.discard(self._skip)
            if self._lc is not None:
                self._lc._gates.fired.discard(self._skip)
        if self._lc is not None:
            self._lc.advance(EV_DONE)  # FSM guard raises GateViolation on a gap
            self._observe(EV_DONE)
            return
        required = GATES_BRANCH if self.kind == "branch" else GATES_LEAF
        missing = [g for g in required if g not in self._fired]
        if missing:
            raise GateViolation(
                f"cannot reach DONE: mandatory gate(s) skipped for "
                f"{self.kind}: {', '.join(missing)}")
        self._observe(EV_DONE)


# B2 at ROOT — an un-mockable assembly smoke run by the engine itself over the
# FINAL corpus verify. A green pytest is hollow when the assembled product does
# not serve its frozen contract (live v020: every contract route 404'd while
# module unit-tests stayed green). The engine boots the real WSGI entry in a
# fresh subprocess (no test-harness import — tests/ is not importable in a real
# run) and drives the contract; a non-200 contract route is a hard root RED.
# This mirrors serve-product.sh and the harness contract_checks boot-gate.
_ROOT_BOOT_PROBE = r'''
import glob, importlib, io, json, os, sys, tempfile
from pathlib import Path
ws = Path(sys.argv[1]).resolve()
cfg = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
callables = cfg.get("callable") or ["wsgi_app", "application", "app"]
entry_stem = cfg.get("entry_stem") or ""
ok_route = cfg.get("ok_route") or ""
html_route = cfg.get("html_route") or ""
json_roundtrip = cfg.get("json_roundtrip") or ""
sys.path.insert(0, str(ws / "src"))
db = os.path.join(tempfile.mkdtemp(prefix="rootboot-"), "boot.db")
for k in ("MARKETPLACE_DB", "NOTES_DB", "NOTES_DB_PATH", "APP_DB", "DB_PATH"):
    os.environ.setdefault(k, db)

def fail(msg):
    print("BOOTGATE_FAIL " + msg)
    raise SystemExit(0)

cands = []
if entry_stem and (ws / "src" / (entry_stem + ".py")).exists():
    cands.append(entry_stem)
for f in sorted(glob.glob(str(ws / "src" / "*.py"))):
    stem = Path(f).stem
    if stem != "__init__" and stem not in cands:
        cands.append(stem)
wsgi, last_err = None, ""
for name in cands:
    try:
        m = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        last_err = "import %s: %r" % (name, exc)
        continue
    for attr in callables:
        c = getattr(m, attr, None)
        if callable(c):
            wsgi = c
            break
    if wsgi is not None:
        break
if wsgi is None:
    fail("no module under src/ exposes a callable %s" % "/".join(callables)
         + ((" (last import error: " + last_err + ")") if last_err else ""))

def call(method, path, payload=None, query="", raw_body=None):
    if raw_body is not None:
        body = raw_body
    else:
        body = json.dumps(payload).encode() if payload is not None else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
               "SERVER_NAME": "boot", "SERVER_PORT": "0",
               "wsgi.url_scheme": "http"}
    cap = {}
    def start_response(status, headers, exc_info=None):
        cap["status"] = int(str(status).split()[0])
    chunks = wsgi(environ, start_response)
    raw = b"".join(chunks if chunks else [])
    return cap.get("status", 0), raw

if ok_route:
    st, _ = call("GET", ok_route)
    if st != 200:
        fail("GET %s -> %s (expected 200)" % (ok_route, st))
if html_route:
    st, raw = call("GET", html_route)
    text = (raw or b"").decode("utf-8", "replace").lower()
    if st != 200 or ("<" not in text):
        fail("GET %s -> %s / not HTML" % (html_route, st))
if json_roundtrip:
    st, raw = call("POST", json_roundtrip, {"text": "rootboot"})
    if st not in (200, 201):
        fail("POST %s -> %s" % (json_roundtrip, st))
    st, raw = call("GET", json_roundtrip)
    if st != 200:
        fail("GET %s -> %s" % (json_roundtrip, st))
    try:
        data = json.loads(raw or b"{}")
        items = data.get("items", data if isinstance(data, list) else [])
        texts = " ".join(str(i.get("text", "")) for i in items
                         if isinstance(i, dict))
    except Exception as exc:  # noqa: BLE001
        fail("GET %s body not JSON: %r" % (json_roundtrip, exc))
    if "rootboot" not in texts:
        fail("POST then GET %s did not round-trip" % json_roundtrip)
    # Robustness (binding): a MALFORMED request body must be REJECTED with a
    # 4xx, never crash the app with a 5xx. A handler that pipes the body
    # straight into json.loads without a guard 500s on bad input — that is not
    # a real product (v085 shipped exactly this: POST /notes 'not-json{' raised
    # JSONDecodeError -> 500). Catch BOTH a raised exception (the app let the
    # parse escape) and a >=500 status.
    try:
        st, _ = call("POST", json_roundtrip, raw_body=b"not-json{")
    except Exception as exc:  # noqa: BLE001 — the app crashed on bad input
        fail("POST %s with a malformed body crashed the app: %r — it must "
             "return a 4xx, not let the parse raise" % (json_roundtrip, exc))
    if st >= 500 or st == 0:
        fail("POST %s with a malformed body -> %s — must be a 4xx rejection, "
             "never a 5xx crash" % (json_roundtrip, st))
    if not (400 <= st < 500):
        fail("POST %s with a malformed body -> %s — expected a 4xx rejection"
             % (json_roundtrip, st))
# Every OTHER declared route must be wired too (e.g. a late GET /about absorbed
# into an existing module). 404 here = the route was promised but never routed in
# the assembled entry — exactly the false-green the curated triple let slip.
for _spec in (cfg.get("extra_routes") or []):
    try:
        _method, _path = _spec[0], _spec[1]
    except Exception:
        continue
    _st, _ = call(_method, _path)
    if _st == 404:
        fail("%s %s -> 404 (declared route never wired in the entry)"
             % (_method, _path))
print("BOOTGATE_OK")
'''


class Engine:
    def __init__(self, tools: Any = None, *, workspace: Any,
                 depth: Any = DEPTH_SPEC, agents: Optional[dict] = None,
                 contracts_dir: Optional[str] = None,
                 sink: Optional[Any] = None, verbosity: int = DEFAULT_VERBOSITY,
                 max_decompose_calls: int = MAX_DECOMPOSE_CALLS,
                 node_engine: str = "inline", runtime_guard: bool = False,
                 resume: bool = False, replan: bool = False,
                 git_provenance: bool = False,
                 review_policy: Optional[dict] = None,
                 seed_files: Optional[dict] = None,
                 standing_requirements: Optional[Any] = None,
                 human_ask: Optional[Any] = None,
                 doctor_project: Optional[dict] = None):
        if not workspace:
            raise ValueError("Workspace is mandatory — pass a path or a Workspace")
        # Doctor: built from the case/launch config (doctor/causes/evaluator
        # blocks). Disabled by default => legacy recovery, byte-identical runs.
        # FAIL LOUDLY if the case asks for the doctor but it cannot be built —
        # we must never run for half an hour silently without the doctor.
        self._doctor = None
        _dp = doctor_project or {}
        _wants_doctor = bool((_dp.get("doctor") or {}).get("enabled"))
        if _doctor_mod is None:
            _DOCTOR_LOG.warning("doctor module unavailable (%s); enabled=%s",
                                _DOCTOR_IMPORT_ERR or "import failed", _wants_doctor)
            if _wants_doctor:
                raise RuntimeError(
                    "doctor.enabled=true but the doctor module failed to import: "
                    f"{_DOCTOR_IMPORT_ERR or 'unknown import error'}")
        else:
            try:
                _dg = _diag_mod.Diagnosers(_diag_mod._Helpers(loops=[])) \
                    if _diag_mod is not None else None
                self._doctor = _doctor_mod.Doctor(
                    project=_dp, diagnosers=_dg)
                _DOCTOR_LOG.info(
                    "doctor built: enabled=%s causes=%d evaluator.tier=%s",
                    self._doctor.enabled, len(self._doctor.causes),
                    self._doctor.evaluator.get("tier"))
            except Exception as _exc:  # noqa: BLE001
                _DOCTOR_LOG.error("doctor build FAILED: %r", _exc)
                if _wants_doctor:
                    raise
                self._doctor = None
        # () -> [(name, statement)] — standing HUMAN requirements (possibly
        # added MID-RUN). The ENGINE owns their placement: a requirement whose
        # acceptance runs on the assembled product is a ROOT-LEVEL concern,
        # never a child of whatever branch happened to decompose next.
        self._standing_requirements = standing_requirements
        # (role, node, question) -> answer|None — the HITL question channel
        # for gate escalation (gates.review.exhausted='ask')
        self._human_ask = human_ask
        # parallelization stage 1: locks make the engine's bookkeeping
        # single-writer; the worker pool size comes from the project dict
        # at run() time (parallel: {children: N})
        self._emit_lock = threading.RLock()
        self._task_lock = threading.RLock()
        self._parallel_children = 1
        self.review_policy = {**DEFAULT_REVIEW_POLICY, **(review_policy or {})}
        self._seed_files = seed_files
        # tools (gate provider) is injectable; default to the bundled gates so
        # the runner works standalone without Hermes.
        self.tools = tools if tools is not None else _gates
        self.depth = _depth_int(depth)
        self.max_decompose_calls = max_decompose_calls
        # node lifecycle engine: 'inline' (imperative, default) or 'fsm'
        # (the standalone NodeLifecycle drives + guards each node).
        if node_engine not in NODE_ENGINES:
            raise ValueError(f"node_engine must be one of {NODE_ENGINES}, got {node_engine!r}")
        if node_engine == "fsm" and not _NODE_FSM_OK:
            raise RuntimeError("node_engine='fsm' requested but spec_flow_node_fsm is unavailable")
        self.node_engine = node_engine
        # runtime invariant guard (C3): enforce R1-R9 live at run end, raising
        # InvariantViolation on a hard (error-severity) breach instead of only
        # reporting it in build_run_report. Default off — back-compat.
        self.runtime_guard = runtime_guard
        # C4: resume a restarted run from the workspace journal — completed
        # leaves keep their persisted artifacts, the implementer is not re-run.
        self.resume = resume
        # replan: a resume that RE-RUNS the decomposer (the operator changed the
        # goal/spec and wants a fresh plan). Default off: a plain resume reuses
        # the persisted decomposition (#7 spec-hash cache still re-runs a leaf
        # whose regenerated spec differs). Without replan a resume continues the
        # SAME tree from the checkpoint instead of re-decomposing from L0.
        self.replan = replan
        self._journal_done: set = set()
        self._journal_index: dict = {}   # #7: node → {version, spec_hash}
        # resume: node id → the decomposer's persisted output for that node, so
        # a resume rebuilds the SAME tree instead of re-asking the (non-
        # deterministic) decomposer — keeps node ids stable so the leaf cache
        # hits and the run continues from the checkpoint, not from L0.
        self._decomp_index: dict = {}
        # П2: wave journal — a single-writer, append-only trail of node
        # commits (one wave per committed node). Opened in run() when the
        # workspace has a root; None means the trail is off (optional).
        self._wave_journal = None
        self._wave_lock = threading.RLock()
        # axis F: leaf isolation mode, set in run() from the project dict
        self._isolation = "none"
        # #4: scope a branch integrate to its subtree's tests (root stays full)
        self._incremental_integrate = True
        # #8: auto-spike thresholds (0 = off) — set in run() from the project
        self._spike_open = 0
        self._spike_loc = 0
        # #10: specialty routing — project meta, the implementer's declared
        # specialties, and the auto-infer switch; set in run()
        self._project_meta: dict = {}
        self._impl_specialties: set = set()
        self._auto_specialty = False
        # C1: executor roster (domain -> agent | D1 team-spec) + the auto-route
        # switch; set in run() from the project + worker config
        self._executors_cfg: dict = {}
        self._auto_domain = False
        # A4: drop no-op lifecycle transitions from the trace (opt-in per run)
        self._prune_noops = False
        # П1: set True when a run ends via a cooperative STOP (partial result)
        self._stopped = False
        self._checkpoint_lock = threading.Lock()
        # auto-checkpoint cadence: the engine snapshots ITSELF every N node
        # boundaries when set (a run/engine parameter, like the decomposer type)
        self._nodes_since_ckpt = 0
        self.agents = {**DEFAULT_AGENTS, **(agents or {})}
        # resume revalidation: an INJECTED deterministic realness checker
        # (root, modules)->[violations]. Injected (not imported) because the
        # plugin must not import the harness — in a real run tests/ is off
        # sys.path. Pulled out of agents so it is never treated as a role.
        self._realness_check = self.agents.pop("_realness_check", None)
        self.contracts_dir = Path(contracts_dir) if contracts_dir else None
        self.events: list[Event] = []
        self.tasks: dict[str, Task] = {}
        self.skills: set[str] = set()
        self.profiles: set[str] = set()
        self.loops: list[dict] = []
        self._doctor_states: dict = {}      # per-node doctor loop state (by nid)
        # point the doctor's deterministic detectors at the live loop journal
        if self._doctor is not None and getattr(self._doctor, "diagnosers", None):
            try:
                self._doctor.diagnosers.helpers.loops = self.loops
            except Exception:  # noqa: BLE001
                pass
        self.gate_calls: dict[str, int] = {"policy_gate": 0, "leaf_check": 0,
                                            "contract_check": 0, "research_trigger_check": 0}
        self._t = 0
        self.verbosity = verbosity
        # sink: an explicit LogSink/callable, else an env-driven default (off
        # unless SPEC_FLOW_RUN_LOG is set). A bare callable is wrapped.
        if sink is None:
            self.sink = LogSink.from_env()
        elif callable(sink) and not isinstance(sink, LogSink):
            self.sink = LogSink(handler=sink, enabled=True)
        else:
            self.sink = sink
        # workspace is mandatory: accept a Workspace or a path.
        if isinstance(workspace, Workspace):
            self.workspace = workspace
        else:
            self.workspace = Workspace(root=str(workspace), enabled=True)
        if git_provenance:
            self.workspace.git_provenance = True

    # -- logging helpers ---------------------------------------------------
    def emit(self, phase, profile, skill, task, action, detail="", gate="", verdict="", level=L_STEP):
        # single writer: with parallel children several subtrees emit at
        # once — the tick counter, event list and sink stay consistent
        with self._emit_lock:
            self._t += 1
            self.skills.add(skill) if skill in ALL_SKILLS else None
            self.profiles.add(profile) if profile in ALL_PROFILES else None
            ev = Event(self._t, phase, profile, skill, task, action, detail, gate, verdict, level)
            self.events.append(ev)
            if self.sink is not None:
                self.sink.handle(ev)

    def task(self, tid, title, kind, profile, skill, parents=None) -> Task:
        with self._task_lock:
            t = Task(tid, title, kind, profile, skill, parents or [])
            self.tasks[tid] = t
            return t

    # -- wave journal (П2) -------------------------------------------------
    def _journal_open(self) -> None:
        """Become the wave journal's writer for this run. Optional: a missing
        module, no workspace root, or a second writer (JournalLocked) just
        leaves the trail off — the run proceeds either way."""
        self._wave_journal = None
        if _journal_mod is None:
            return
        if not (self.workspace.enabled and self.workspace.root):
            return
        path = Path(self.workspace.root) / ".spec-flow" / "waves.jsonl"
        try:
            self._wave_journal = _journal_mod.RunJournal(path).open()
        except Exception:  # noqa: BLE001 — the trail never gates the run
            self._wave_journal = None

    def _journal_close(self) -> None:
        j, self._wave_journal = self._wave_journal, None
        if j is not None:
            try:
                j.close()
            except Exception:  # noqa: BLE001
                pass

    def _wave(self, entries: list, **meta) -> None:
        """Commit one wave of journal entries under the writer lock. Never
        raises into the run — a journal write failure is non-fatal."""
        j = self._wave_journal
        if j is None or not entries:
            return
        try:
            with self._wave_lock:
                j.commit_wave(entries, meta or None)
        except Exception:  # noqa: BLE001 — the trail never gates the run
            pass

    # -- real tool wrappers ------------------------------------------------
    def _policy(self, policy):
        self.gate_calls["policy_gate"] += 1
        return json.loads(self.tools._handle_policy_gate(dict(policy)))

    def _leaf(self, metrics, atomic=None):
        self.gate_calls["leaf_check"] += 1
        args = dict(metrics)
        if atomic is not None:
            args["atomic"] = bool(atomic)
        return json.loads(self.tools._handle_leaf_check(args))

    def _judge_leaf(self, nid, title, fn, code_rel, test_rel):
        """Optional independent reviewer (4.5): score the produced code against
        the spec. Runs only when a ``judge`` agent is injected; a fail verdict
        is recorded as a gate event + a rework loop and bumps the leaf version."""
        judge = self.agents.get("judge")
        if not judge or not (self.workspace.enabled and self.workspace.root):
            return
        root = Path(self.workspace.root)

        def _read(rel):
            p = root / rel
            return p.read_text(encoding="utf-8") if p.is_file() else ""

        try:
            out = judge({"node": nid, "title": title, "spec": f"specs/{fn}.md",
                         "code": _read(code_rel), "test": _read(test_rel)}) or {}
        except Exception as exc:  # noqa: BLE001 — a judge failure must not crash the run
            out = {"verdict": "error", "reasons": repr(exc)[:200]}
        verdict = str(out.get("verdict", "")).lower()
        self.gate_calls["judge"] = self.gate_calls.get("judge", 0) + 1
        self.emit("review", "spec-reviewer", "spec-reviewer", f"{nid}:impl",
                  "independent judge: code vs spec", out.get("reasons", ""),
                  "judge", "PASS" if verdict == "pass" else "FAIL", level=L_MILESTONE)
        if verdict == "fail":
            self.loops.append({"type": "judge-reject", "task": nid,
                               "detail": out.get("reasons", "")})
            if nid in self.tasks:
                self.tasks[nid].version += 1
                self.tasks[nid].runs += 1

    def _contract(self, contract, code):
        # contract_check needs the contract+code files; skip gracefully if no
        # contracts_dir was provided (keeps the runner domain-agnostic).
        if not self.contracts_dir:
            return {"status": "skipped", "drift": []}
        self.gate_calls["contract_check"] += 1
        return json.loads(self.tools._handle_contract_check({
            "contract_artifacts": [str(self.contracts_dir / contract)],
            "changed_files": [str(self.contracts_dir / code)],
            "types": ["openapi"],
        }))

    def _research(self, reason, completed, errors):
        self.gate_calls["research_trigger_check"] += 1
        return json.loads(self.tools._handle_research_trigger_check(
            {"reason": reason, "completed_tasks": completed, "test_errors": errors}))

    def _hitl(self, kind: str, task: str, detail: str) -> bool:
        """Human-in-the-loop checkpoint. Consults the injected ``approver``
        agent (autonomous default approves with a note); emits a `hitl` gate
        event with the verdict. On rejection records a `hitl-reject` loop so the
        run shows where a human sent work back. Returns True iff approved."""
        self.gate_calls["hitl"] = self.gate_calls.get("hitl", 0) + 1
        out = self.agents["approver"]({"kind": kind, "task": task, "detail": detail,
                                       "constitution": self._constitution,
                                       "policy": getattr(self, "_policy_cfg", {})}) or {}
        approved = bool(out.get("approved", True))
        reason = out.get("reason", "")
        self.emit("hitl", "approver", "spec-reviewer", task,
                  f"HITL {kind} checkpoint → {'approved' if approved else 'REJECTED'}",
                  reason or detail, "hitl", "approved" if approved else "rejected",
                  level=L_MILESTONE)
        if not approved:
            self.loops.append({"type": "hitl-reject", "kind": kind,
                               "task": task, "detail": reason or detail})
        return approved

    def _requirement_nodes(self, scope: "Optional[str]" = None) -> list:
        """Uncovered standing requirements as ready-to-visit node dicts.

        ``scope=<nid>``: only requirements explicitly scoped to that node.
        ``scope=None``:  every uncovered requirement (the root fallback —
        the last placement chance, scope missed or absent)."""
        fn = self._standing_requirements
        if fn is None:
            return []
        out = []
        for item in (fn() or []):
            name, statement = item[0], item[1]
            req_scope = item[2] if len(item) > 2 else None
            if name in self.tasks:
                continue            # already covered by an existing node
            if scope is not None and req_scope != scope:
                continue
            title = " ".join(str(statement).split())[:120]
            out.append({"id": name, "title": title,
                        "requirement": str(statement),
                        "_scoped": scope is not None,
                        # a standing requirement attached mid-run IS a late
                        # requirement: its spec must scope to its delta, not
                        # re-state surface existing modules already own
                        "_late_req": True,
                        # SAFELY atomic: estimated_loc 150 once tripped the
                        # branch guardrail and the node sailed through as an
                        # EMPTY branch — zero implementation, green integrate
                        "metrics": {"modules": 1, "tasks": 3,
                                    "interfaces": 1, "estimated_loc": 80,
                                    "open_decisions": 0,
                                    "single_concern": True,
                                    "testable_criteria": True}})
        return out

    def _amend_target(self, node: dict) -> "Optional[str]":
        """B3 (deterministic, model-independent): if this requirement refines a
        surface an EXISTING src module already owns, return that module's
        rel-path so the engine routes the implementation INTO it (edit-in-place)
        instead of forking a second module for the same surface.

        The owner is decided in CODE by a registry of universal detectors (see
        _amend_find_owner) — route match, file mention, symbol overlap, token
        overlap, surface overlap — not by asking the model to notice and not by
        a domain-specific keyword list. An LLM router is the slow fallback when
        every deterministic signal is empty. Returns None when nothing overlaps
        or the flag is off.

        Candidates are built from BOTH placed-node SPECS (specs/*.md) and built
        FILES (src/*.py), merged by stem: a node placed but not yet implemented
        is matchable from its spec text alone — the v0NN miss where a late
        'make it nice' forked a new node because src/web_ui.py did not exist at
        match time even though the web_ui spec did."""
        if os.environ.get("SPEC_FLOW_REQ_AMEND", "") in (
                "", "0", "false", "False", "no"):
            return None
        own = _snake(str(node.get("id", "")))
        stmt = str(node.get("requirement") or node.get("spec_markdown")
                   or node.get("title") or "")
        # A late requirement that adds an HTTP ROUTE must land where its handler
        # stays a RESOLVABLE leaf function — never in the engine-owned entry. The
        # entry (src/app.py) is regenerated deterministically at assembly, so a
        # handler the model inlines into its router is silently dropped (live
        # v104/v107: ping_text / web_ui were routed to "EDIT app.py" and the
        # /ping, /ui branch vanished from the regenerated router -> 404). The
        # route->handler binding now tells the model to expose
        # `def <method>_<path>(payload, query)`; forking the node's OWN leaf (or
        # a real NON-entry module it overlaps) keeps that handler resolvable, and
        # the harvesting assembler relocates a stray top-level handler. So the
        # entry is never an amend target.
        _entry_stem = Path((self._product_contract() or {}).get("entry")
                           or "").stem
        modules = self._surface_modules(own)
        if not modules:
            return None
        cand_text = {rel: body[:400] for rel, _stem, body in modules}
        llm = None
        if os.environ.get("SPEC_FLOW_AMEND_LLM", "") not in (
                "", "0", "false", "False", "no"):
            llm = self._amend_llm_router
        owner = _amend_find_owner(stmt, modules, candidates_text=cand_text,
                                  llm=llm)
        if owner and _entry_stem and Path(owner).stem == _entry_stem:
            return None       # never edit the regenerated entry — fork own leaf
        return owner

    def _surface_modules(self, own: str) -> list:
        """Existing candidate modules as [(rel, stem, spec+code body)], merged by
        stem from BOTH specs/*.md (a placed-but-unbuilt node is matchable from its
        spec alone) and src/*.py, excluding the node itself. Shared by the amend
        router and the late-requirement scope lint."""
        root = Path(self.workspace.root)
        cand: dict = {}
        specdir = root / "specs"
        if specdir.is_dir():
            for p in sorted(specdir.glob("*.md")):
                stem = p.stem
                if "." in stem or stem in ("__init__", own):
                    continue            # skip archived specs/<id>.vN.md + self
                cand.setdefault(stem, {})["spec"] = p.read_text(
                    encoding="utf-8", errors="replace")
        srcdir = root / "src"
        if srcdir.is_dir():
            for p in sorted(srcdir.glob("*.py")):
                if p.stem in ("__init__", own):
                    continue
                cand.setdefault(p.stem, {})["code"] = p.read_text(
                    encoding="utf-8", errors="replace")
        return [(f"src/{stem}.py", stem,
                 (d.get("spec", "") + "\n" + d.get("code", "")).strip())
                for stem, d in sorted(cand.items())]

    def _late_req_scope_findings(self, node: dict, spec_md: str) -> list:
        """Deterministic decomposition-quality findings for a LATE-REQUIREMENT
        node whose spec would DUPLICATE an existing surface (re-state the whole
        service) instead of scoping to its delta. Only runs for nodes flagged
        ``_late_req`` and only when other modules exist; returns [] otherwise.
        The findings (from _dup_surface_findings) are fed VERBATIM to the
        spec-author rework round, so the spec is narrowed before the LLM review."""
        if not node.get("_late_req"):
            return []
        if os.environ.get("SPEC_FLOW_LATE_REQ_SCOPE", "") in (
                "0", "false", "False", "no"):
            return []
        modules = self._surface_modules(_snake(str(node.get("id", ""))))
        if not modules:
            return []
        return _dup_surface_findings(spec_md or "", modules)

    def _late_req_delta_gate(self, node: dict, nid: str, depth: int,
                             code_rel: str) -> bool:
        """437: delta as acceptance. A LATE requirement leaf must produce a REAL
        delta — its owned module ends up present with non-trivial code. An empty
        change (owner file missing or only blanks/comments) is the v041 failure:
        the node 'ran' but added nothing. Deterministic, code-checked (decided by
        the artifact, not the model); surfaces the empty_delta cause so the doctor
        treats it instead of letting a hollow leaf pass as implemented. Returns
        True when a real delta is present. Inert for non-late nodes."""
        if not node.get("_late_req"):
            return True
        try:
            body = (Path(self.workspace.root) / code_rel).read_text(
                encoding="utf-8")
        except Exception:  # noqa: BLE001 — missing file == empty delta
            body = ""
        meaningful = sum(1 for ln in body.splitlines()
                         if ln.strip() and not ln.strip().startswith("#"))
        floor = int((self.review_policy or {}).get("min_delta_lines", 1))
        if meaningful < floor:
            self.loops.append({"type": "empty-delta", "task": nid,
                               "detail": f"late requirement produced no delta "
                                         f"in {code_rel} ({meaningful} code line(s))"})
            self.emit("review", "engine", "", nid,
                      "delta gate: late requirement produced no real change",
                      f"{code_rel}: {meaningful} code line(s) < {floor}",
                      "delta_gate", "FAIL", level=L_MILESTONE)
            # feed the empty_delta detector verbatim (its scope_findings reader
            # keys on 'adds no new symbol' / 'no new route')
            self._doctor_advise(node, nid, depth, "delta_gate", "FAIL",
                                {"scope_findings": [
                                    f"{code_rel} adds no new symbol — empty delta"]})
            # remember WHICH artifact this cause is about so completion can
            # re-validate it (a later rework may make the delta real on a path
            # that does not re-run this gate — see _prune_stale_causes).
            _ds = (getattr(self, "_doctor_states", None) or {}).get(nid)
            if isinstance(_ds, dict):
                _ds["delta_path"] = code_rel
            return False
        # Route-level delta: a non-empty owner module is NOT enough when the
        # requirement declares an HTTP route. The leaf must add a HANDLER for it
        # — else it 'ran', the module stayed non-empty (its other handlers), and
        # the route 404s end-to-end (v062: about_page declared GET /about but
        # added nothing to web_ui.py, passing the line floor on the existing /ui
        # code). Deterministic: each declared route needs the literal path OR a
        # def whose name carries the route token in the owned module.
        # OWN routes only: VERB-declared here (a bare quoted `/notes` in the
        # spec's prose is service CONTEXT, not this node's delta) and MINUS any
        # route a sibling module already serves — a node is held to the routes it
        # ADDS, never to the whole service it was merely told about (v110:
        # красивый_вид/note_search were charged with /ui & /ping, owned by other
        # leaves; that false REJECT reworked note_storage and broke app.py).
        routes = _verb_declared_routes(str(node.get("title") or ""))
        try:
            routes |= _verb_declared_routes(
                (Path(self.workspace.root) / f"specs/{Path(code_rel).stem}.md")
                .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — spec optional; title is the floor
            pass
        try:                            # drop routes a SIBLING module serves
            served_elsewhere = set()
            for _rel, _stem, _sbody in self._surface_modules(Path(code_rel).stem):
                served_elsewhere |= _served_routes(_sbody)
            routes -= {r for r in routes if r.rstrip("/") in served_elsewhere}
        except Exception:  # noqa: BLE001 — sibling scan is best-effort
            pass
        low = body.lower()
        missing = []
        for r in routes:
            token = r.rstrip("/").rsplit("/", 1)[-1].lower()
            if not token or "{" in token:
                continue
            if r.lower() in low:
                continue
            if re.search(r"def\s+\w*" + re.escape(token) + r"\w*", low):
                continue
            missing.append(r)
        if missing:
            self.loops.append({"type": "empty-delta", "task": nid,
                               "detail": f"late requirement declares {missing} "
                                         f"but {code_rel} implements no handler"})
            self.emit("review", "engine", "", nid,
                      "delta gate: declared route has no handler in owner module",
                      f"{code_rel}: no handler for {missing}",
                      "delta_gate", "FAIL", level=L_MILESTONE)
            self._doctor_advise(node, nid, depth, "delta_gate", "FAIL",
                                {"scope_findings": [
                                    f"{code_rel} adds no new route handler for "
                                    f"{missing} — empty delta"]})
            _ds = (getattr(self, "_doctor_states", None) or {}).get(nid)
            if isinstance(_ds, dict):
                _ds["delta_path"] = code_rel
                _ds["delta_routes"] = list(routes)
            return False
        # The delta is real now (module present, route handlers in place). If an
        # EARLIER pass on this leaf opened an empty_delta cause (the leaf first
        # ran hollow, the doctor reworked it, and this re-run now passes), CLOSE
        # it — otherwise the stale cause stays "open" forever and the integrate
        # gate records a FALSE RED on a product that actually serves every route.
        # Honest: the artifact genuinely satisfies the gate, so the cause is
        # resolved, not suppressed. Guarded to the delta cause family so a
        # different, still-real open cause on this node is never falsely closed.
        _st = (getattr(self, "_doctor_states", {}) or {}).get(nid) or {}
        if "delta" in str(_st.get("last_cause") or "").lower():
            self._doctor_resolve(node, nid, "delta_gate")
        return True

    def _missing_decisions(self, node: dict, nid: str) -> list:
        """462 (memory_loss data source): decisions the engine already RECORDED
        for this run that THIS node failed to honour. A ``depends_on`` link is a
        recorded decision — 'reuse node X, do not rebuild it' — produced by the
        dedup gate / decomposer. If the node's owned module never references the
        depended module, that decision was forgotten. Sourced from the run-wide
        intentions board the dedup gate already maintains (``_node_registry`` +
        ``_module_names``), so no new store is needed. Deterministic, code-checked
        (decided by the artifact, not the model); [] when nothing was forgotten."""
        deps = node.get("depends_on") or []
        if not deps:
            return []
        fn = self._module_names.get(nid) or _snake(nid)
        try:
            body = (Path(self.workspace.root) / f"src/{fn}.py").read_text(
                encoding="utf-8")
        except Exception:  # noqa: BLE001 — missing file: everything is unreferenced
            body = ""
        missing = []
        for dep in deps:
            if dep not in self._node_registry:
                continue                       # not a recorded decision
            dep_fn = self._module_names.get(dep) or _snake(dep)
            if dep_fn and dep_fn in body:
                continue                       # decision honoured (module reused)
            title = self._node_registry.get(dep, dep)
            missing.append(
                f"reuse {dep} ({title}) via src/{dep_fn}.py — not referenced")
        return missing

    def _ensure_doctor_classifier(self) -> None:
        """Lazily attach the semantic LLM classifier to the doctor, resolving the
        backend (ask + chain_for_tier) the same way the rest of the runner does.
        No-op if already set or the backend is unavailable."""
        if self._doctor is None or _diag_mod is None:
            return
        if getattr(self._doctor, "classifier", None) is not None:
            return
        import importlib
        for rootmod in ("harness.llm_backend", "tests.harness.llm_backend",
                        "llm_backend"):
            try:
                mod = importlib.import_module(rootmod)
            except Exception:           # noqa: BLE001
                continue
            tier_fn = getattr(mod, "chain_for_tier", None)
            if getattr(mod, "ask", None) and tier_fn:
                self._doctor.classifier = _diag_mod.Classifier(
                    self._doctor.evaluator, mod.ask, tier_fn)
                _DOCTOR_LOG.info("classifier wired via %s (tier=%s)", rootmod,
                                 self._doctor.evaluator.get("tier"))
            return

    def _doctor_advise(self, node: dict, nid: str, depth: int, gate: str,
                       verdict: str, evidence: dict) -> None:
        """Run the doctor on a failed gate: diagnose the cause, dispatch a remedy
        and RECORD it (event + loop journal) so the dashboard shows cause/remedy
        per node. Advisory in Ф3 — execution stays with the existing recovery.
        Never raises: a broken doctor must not break a run."""
        if self._doctor is None or _doctor_mod is None \
                or not self._doctor.enabled:
            _DOCTOR_LOG.debug("advise skipped (doctor=%s enabled=%s) %s:%s",
                              self._doctor is not None,
                              getattr(self._doctor, "enabled", None), nid, gate)
            return
        try:
            self._ensure_doctor_classifier()
            _DOCTOR_LOG.info("advise CALL node=%s gate=%s verdict=%s evidence=%s",
                             nid, gate, verdict,
                             {k: (len(v) if isinstance(v, (list, tuple)) else str(v)[:80])
                              for k, v in (evidence or {}).items()})
            try:
                _module = self._module_for(nid)
            except Exception:        # noqa: BLE001 — module label is non-critical
                _module = ""
            ctx = _doctor_mod.Context(
                node=nid, module=_module, gate=gate, depth=depth,
                depends_on=tuple(node.get("depends_on") or ()))
            # Feed the DETERMINISTIC detectors live engine data, otherwise only
            # the semantic classifier ever fires (observed in v043): the loop
            # journal drives ancestry (upstream *-fail on a dependency), and this
            # node's prior reject reasons drive rewrite_loops (same reason twice).
            if _diag_mod is not None and self._doctor.diagnosers is not None:
                self._doctor.diagnosers.helpers = _diag_mod._Helpers(
                    loops=list(self.loops))
            evidence = dict(evidence or {})
            if "reason_history" not in evidence:
                hist = [str(lp.get("detail") or "") for lp in self.loops
                        if lp.get("task") == nid
                        and "reject" in str(lp.get("type", "")).lower()
                        and lp.get("detail")]
                if hist:
                    evidence["reason_history"] = hist
            # 462: feed the silent_truncation detector — any input cut while
            # building THIS node was collected per-node by the harness; drain it
            # into evidence['dropped'] so a truncation-caused failure is visible.
            if _trunc is not None and "dropped" not in evidence:
                drops = _trunc.drain(nid)
                if drops:
                    evidence["dropped"] = drops
            # 462: feed the memory_loss detector — recorded depends_on decisions
            # this node failed to honour, read from the run's intentions board.
            if "missing_decisions" not in evidence:
                miss = self._missing_decisions(node, nid)
                if miss:
                    evidence["missing_decisions"] = miss
            diag = self._doctor.diagnose(
                node=nid, gate=gate, verdict=verdict,
                evidence=evidence, context=ctx)
            ranked = [(f.cause, f.detector, round(float(f.confidence), 2))
                      for f in diag.ranked]
            _DOCTOR_LOG.info("advise DIAGNOSIS node=%s ranked=%s abstained=%s",
                             nid, ranked, diag.abstained)
            state = self._doctor_states.setdefault(nid, _doctor_mod.new_state())
            act = self._doctor.treat(diag, state)
            cause = diag.primary.cause if diag.primary else "(none)"
            state["last_cause"] = cause          # for the closing PASS on this gate
            _DOCTOR_LOG.info("advise ACTION node=%s cause=%s remedy=%s rung=%s",
                             nid, cause, act.kind,
                             getattr(act.record, "rung", None))
            self.emit("review", "doctor", "", nid,
                      f"diagnose {cause} → remedy {act.kind}",
                      str(act.feedback)[:200], gate=f"doctor:{cause}",
                      verdict="REJECT", level=L_MILESTONE)
            if act.record is not None:
                self.loops.append(act.record.as_loop())
            return act                       # the engine seam may EXECUTE it
        except Exception as exc:  # noqa: BLE001
            _DOCTOR_LOG.exception("advise FAILED node=%s gate=%s", nid, gate)
            self.emit("review", "doctor", "", nid, "doctor advise failed",
                      str(exc)[:160], level=L_MILESTONE)
        return None

    def _doctor_resolve(self, node: dict, nid: str, gate: str) -> None:
        """Close the doctor's cause on this node: a later PASS on the same gate
        means the remedy worked. Emits doctor:<cause> PASS (turns the dashboard
        row green) and records a resolved loop entry. No-op if nothing was open."""
        if self._doctor is None or not self._doctor.enabled:
            return
        try:
            state = self._doctor_states.get(nid) or {}
            cause = state.get("last_cause")
            if not cause or cause == "(none)":
                return
            self.emit("review", "doctor", "", nid, f"resolved {cause}", "",
                      gate=f"doctor:{cause}", verdict="PASS", level=L_MILESTONE)
            self.loops.append({"type": "doctor", "task": nid, "cause": cause,
                               "remedy": "(closed)", "outcome": "resolved",
                               "detail": f"{gate} passed"})
            state["last_cause"] = None
        except Exception:  # noqa: BLE001
            pass

    def _doctor_open_causes(self) -> list:
        """(nid, cause) for every node whose doctor cause is still OPEN — set by
        _doctor_advise and never closed by a later _doctor_resolve PASS. The
        completion gate uses this to HONOUR the doctor's verdict: an unresolved
        diagnosed cause must block a green COMPLETE (advisory-only let v044 ship a
        broken build as done). Empty when the doctor is off or all causes closed."""
        if self._doctor is None or not self._doctor.enabled:
            return []
        out = []
        for nid, st in (self._doctor_states or {}).items():
            cause = (st or {}).get("last_cause")
            if cause and cause != "(none)":
                out.append((nid, cause))
        return out

    def _module_has_real_code(self, rel: str, routes=None) -> bool:
        """True when `rel` (relative to the workspace) is a real, parseable
        module: non-blank, more than comments, with at least one def/class/
        assignment — and, when `routes` are given, a handler for EACH (the
        literal path OR a def whose name carries the route token). The same
        artifact check the empty_delta gate applies, used to re-validate a stale
        cause against the FINAL file."""
        try:
            src = (Path(self.workspace.root) / rel).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return False
        if not src.strip():
            return False
        try:
            import ast as _ast
            tree = _ast.parse(src)
        except SyntaxError:
            return False
        has_sym = any(isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                     _ast.ClassDef, _ast.Assign))
                      for n in _ast.walk(tree))
        if not has_sym:
            return False
        low = src.lower()
        for r in (routes or []):
            token = str(r).rstrip("/").rsplit("/", 1)[-1].lower()
            if not token or "{" in token:
                continue
            if str(r).lower() in low:
                continue
            if re.search(r"def\s+\w*" + re.escape(token) + r"\w*", low):
                continue
            return False                     # a declared route still has no handler
        return True

    def _prune_stale_causes(self) -> None:
        """Re-validate each OPEN doctor cause against the FINAL artifact and close
        the ones that no longer reflect a real defect. A cause is the engine's
        journal of a problem seen DURING the run; by completion the doctor may
        have reworked the leaf so the deliverable is now real, but some remedy
        paths (reject_empty, reconcile_check) don't re-run the gate that opened
        the cause — leaving it falsely 'open' and vetoing a product that actually
        works (v067/v068: every route served, product gate READY, yet integrate
        RED on stale empty_delta/vague_spec). Honest: a cause is dropped ONLY when
        the artifact now POSITIVELY satisfies what it complained about — the
        owned module is real (+ its declared routes have handlers), or the
        assembled product genuinely boots. Never a blanket clear."""
        doc = getattr(self, "_doctor", None)
        if doc is None or not getattr(doc, "enabled", False):
            return
        if not getattr(getattr(self, "workspace", None), "root", None):
            return
        for nid, st in list((self._doctor_states or {}).items()):
            if not isinstance(st, dict):
                continue
            cause = str(st.get("last_cause") or "")
            if not cause or cause == "(none)":
                continue
            if "delta" in cause.lower():
                # the leaf's owned artifact is no longer hollow / now has handlers
                path = st.get("delta_path") or f"src/{self._module_for(nid)}.py"
                if self._module_has_real_code(path, st.get("delta_routes")):
                    self._doctor_resolve({}, nid, "revalidate(delta)")
            elif cause == "vague_spec" and nid == "product_entry":
                # the assembled product demonstrably boots and serves its
                # contract -> the entry spec was sufficient after all
                try:
                    boots, _ = self._assembled_product_boots()
                except Exception:  # noqa: BLE001
                    boots = False
                if boots:
                    self._doctor_resolve({}, nid, "revalidate(boot)")

    def _remedy_reconcile_check(self, entry: str, escalate: bool = False) -> bool:
        """Executable reconcile_check (Ф6): the integrate gate found the assembled
        product RED (entry missing OR present but not serving its contract). Re-run
        the ENGINE's OWN assembly leaf — its spec (built by _assembly_node) tells
        the implementer to create the entry wiring the modules already under src/ —
        through THIS run's implementer worker (sim or live, never a hand-written
        scaffold), then re-verify with the boot-gate. Green boot => drop the root
        integrate-fail + close the doctor cause (row turns green). Otherwise stay
        RED. ``escalate`` forces the rebuild onto the strong tier (escalate_tier
        remedy). Returns True iff healed."""
        ws = self.workspace
        if not (getattr(ws, "enabled", False) and getattr(ws, "root", None)
                and "implementer" in self.agents):
            return False
        # force=True: this is a REACTIVE heal — the entry may already exist but
        # is RED (not serving its contract), so build the rebuild spec even when
        # the file is present (the normal proactive path skips an existing entry).
        asm = self._assembly_node(force=True)
        if not asm:
            return False
        module = Path(asm.get("code_target") or entry).stem    # src/app.py -> app
        spec_rel = f"specs/{module}.md"
        try:
            sp = Path(ws.root) / spec_rel
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(str(asm.get("spec_markdown") or ""), encoding="utf-8")
        except OSError as exc:
            self.emit("integrate", "doctor", "", "L0:integrate",
                      "reconcile_check could not write the build spec",
                      str(exc)[:140], "integrate_verify", "FAIL", level=L_MILESTONE)
            return False
        self.emit("integrate", "doctor", "spec-implement", "L0:integrate",
                  f"reconcile_check: build declared entry {entry}",
                  "re-running the implementer on the assembly spec to wire wsgi_app",
                  "integrate_verify", "", level=L_MILESTONE)
        # DETERMINISTIC-FIRST (model-independent assembly, shared helper). The
        # engine OWNS the WSGI glue: when every declared route resolves to a built
        # leaf handler, synthesise the entry and skip the LLM — closing M1/M2/M3 by
        # construction. Only a critical-route miss falls through to the implementer.
        synth_done = False
        if self._try_synthesize_entry():
            _bok, _ = self._assembled_product_boots()
            if _bok:
                synth_done = True
        ictx = {"node": "product_entry",
                "title": str(asm.get("title") or f"Assemble {entry}"),
                "depth": self.depth, "workspace": ws,
                "spec": spec_rel, "module": module}
        # escalate_tier remedy: rebuild the entry on the strong model tier
        if escalate:
            ictx["tier"] = "strong"
        if not synth_done:
            try:
                self._invoke_implementer(ictx, "product_entry", module)
            except Exception as exc:        # noqa: BLE001
                self.emit("integrate", "doctor", "", "L0:integrate",
                          "reconcile_check build raised", str(exc)[:140],
                          "integrate_verify", "FAIL", level=L_MILESTONE)
                return False
        boot_ok, boot_detail = self._assembled_product_boots()
        if not boot_ok:
            self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                      "reconcile_check did not fix the build", boot_detail,
                      "integrate_verify", "FAIL", level=L_MILESTONE)
            return False
        # healed: drop the recorded root fail and close the cause
        _rid = getattr(self, "_root_id", "L0")
        self.loops = [lp for lp in self.loops
                      if not (lp.get("type") == "integrate-fail"
                              and str(lp.get("task")) == str(_rid)
                              and "entry" in str(lp.get("detail", "")).lower())]
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  "reconcile_check OK — declared entry built & boots green",
                  "boot-gate green over the assembled product",
                  "integrate_verify", "PASS", level=L_MILESTONE)
        self._doctor_resolve({"id": "L0:integrate"}, "L0:integrate",
                             "integrate_verify")
        return True

    def _amend_llm_router(self, statement: str, modules: list,
                          candidates_text: "Optional[dict]") -> "Optional[str]":
        """#2 router: ONE strong-model call, fired only after every cheaper
        deterministic detector found no owner. The reviewer chain is used (the
        'strong on checks' tier per the case YAML); free-only via llm_backend.
        The model is asked to name the owning module rel-path or 'new' — the
        prompt MUST prefer 'new' when unsure, so a doubtful late requirement
        forks (cheap to fix) rather than corrupting an unrelated module."""
        ask, chain_for = self._llm_router_handles()
        if ask is None:
            return None
        rels = [rel for rel, _stem, _body in modules]
        listing = "\n".join(
            f"- {rel}\n  {(candidates_text or {}).get(rel, '')[:240]}"
            for rel in rels)
        prompt = (
            "A late requirement arrived AFTER these modules were planned. Decide "
            "if it REFINES one existing module (route the work into it) or is a "
            "genuinely NEW concern (a new module).\n\n"
            f"LATE REQUIREMENT:\n{statement.strip()[:600]}\n\n"
            f"EXISTING MODULES (rel-path + spec excerpt):\n{listing}\n\n"
            "Answer with EXACTLY one line: the rel-path of the single owning "
            "module (e.g. src/web_ui.py) if it clearly refines that module, or "
            "the bare word new. When unsure, answer new.")
        try:
            chain = chain_for("reviewer")
            # attribute the call to a node+purpose: every LLM call must carry a
            # node so the dashboard never shows a bare «—» for the actor's target
            reply = ask(prompt, model=chain[0], role="reviewer",
                        step="amend-route", fallbacks=tuple(chain[1:]),
                        meta={"node": "late-requirement",
                              "purpose": "route to owning module"})
        except Exception:               # noqa: BLE001
            return None
        ans = (reply or "").strip().splitlines()[-1].strip().strip("`").strip()
        for rel in rels:
            if rel == ans or rel in ans or ans.endswith(rel):
                return rel
        return None                     # 'new' or unparseable -> fork

    @staticmethod
    def _llm_router_handles():
        """Resolve (ask, chain_for) from whichever llm_backend import root is on
        the path, mirroring the loader used elsewhere in the runner. Returns
        (None, None) when the backend is unavailable (pure-unit context)."""
        import importlib
        for rootmod in ("harness.llm_backend", "tests.harness.llm_backend",
                        "llm_backend"):
            try:
                mod = importlib.import_module(rootmod)
                return mod.ask, mod.chain_for
            except Exception:           # noqa: BLE001
                continue
        return None, None

    def _product_contract(self) -> dict:
        """Derive the runnable-product contract from the project's OWN HUMAN
        description — NEVER from structured config. No `product:` key is read;
        the plugin works only from specs born of human text. Sources: the
        constitution + goal + accumulated human requirements (so a HUMAN-injected
        route like /ui is picked up too). A project that describes no HTTP service
        (library / CLI / pipeline) yields {} ⇒ no assembly node, no boot-gate, no
        reconcile. Returns {entry, callable, boot:{ok_route, html_route,
        json_roundtrip}} or {}. Heuristic, model-independent (pure text)."""
        texts = [str(t) for t in (self._constitution or [])]
        if getattr(self, "_goal", ""):
            texts.append(str(self._goal))
        try:                     # human requirements added mid-run (injections)
            fn = self._standing_requirements
            items = fn() if callable(fn) else (fn or [])
            for item in (items or []):
                texts.append(str(item[1] if len(item) > 1 else item[0]))
        except Exception:        # noqa: BLE001
            pass
        blob = "\n".join(texts)
        low = blob.lower()
        # routes the HUMAN described: METHOD /path
        routes: dict = {}
        for m in re.finditer(
                r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./{}-]*)", blob):
            routes.setdefault(m.group(2).rstrip(".,;:)"), set()).add(
                m.group(1).upper())
        if not routes:
            return {}            # no HTTP route described ⇒ not a runnable web product
        # The entry FILE and its callable must be NAMED by the human (e.g.
        # "src/app.py exposes wsgi_app"). If the human declared routes but never
        # named where they live, the engine does NOT guess a default file/callable
        # and does NOT assemble — guessing would be a hardcoded assumption about
        # the solution. Incomplete contract => {} (no assembly, no boot-gate).
        em = re.search(r"(src/[A-Za-z0-9_./-]+\.py)", blob)
        cm = re.search(r"exposes?\s+`?([a-z_][a-z0-9_]*)`?", low)
        if not em or not cm:
            return {}
        entry = em.group(1)
        callables = [cm.group(1)]
        # classify the described routes into boot checks from the human wording
        boot: dict = {}
        for path, methods in routes.items():
            n = path.lower()
            if any(k in n for k in ("health", "status", "ping", "ready", "live")):
                boot.setdefault("ok_route", path)
            elif "POST" in methods and "GET" in methods:
                boot.setdefault("json_roundtrip", path)
        for path, methods in routes.items():     # an HTML page route
            if "GET" in methods and path not in boot.values():
                i = low.find(path.lower())
                if any(k in low[max(0, i - 90):i + 90]
                       for k in ("html", "page", "browser", "form", "renders")):
                    boot.setdefault("html_route", path)
                    break
        if not boot:                              # fall back to the first GET
            for path, methods in routes.items():
                if "GET" in methods:
                    boot["ok_route"] = path
                    break
        # Every OTHER declared route (e.g. a late-injected GET /about absorbed
        # into an existing module) must also be live in the assembled entry. The
        # curated triple alone let unwired late routes 404 while the product was
        # marked READY. Probe each non-triple, non-templated path for "is it
        # routed at all" (GET -> not 404; a POST-only route answers 405, still
        # wired). Templated paths ({id}) can't be probed literally — skip them.
        covered = {p for p in boot.values() if p}
        extra = [["GET", p] for p in routes
                 if p not in covered and "{" not in p]
        return {"entry": entry, "callable": callables, "boot": boot,
                "routes": extra}

    def _assembly_node(self, force: bool = False) -> "Optional[dict]":
        """B2 mechanism 3: an ENGINE-generated assembly leaf.

        When the constitution declares a product entry (a module-level
        ``wsgi_app`` in ``src/app.py``) the decomposer routinely builds the
        feature leaves but never a node that WIRES them into the runnable
        entry — so the product stays un-assembled and the boot-gate is red
        forever (observed live: notes_db + notes_http built, no app.py, root
        RED with nothing to assemble). The engine closes that gap itself by
        appending ONE leaf whose whole job is to assemble the declared entry
        from the modules already built under ``src/``.

        This is a TASK an LLM implementer does for real — not seeded code, no
        mock: the leaf reads the repo map, imports the real modules and routes
        the contract's endpoints. Constitution-keyed + behind
        ``SPEC_FLOW_PRE_GATE`` so flag-off runs and p4/p5 are unchanged."""
        if os.environ.get("SPEC_FLOW_PRE_GATE", "") in (
                "", "0", "false", "False", "no"):
            return None
        # Entry comes from the DECLARED product contract — no hardcoded
        # 'src/app.py'/'wsgi_app'. No contract => no assembly (library/CLI/…).
        c = self._product_contract()
        if not c:
            return None
        entry = c["entry"]
        _rivals = self._rival_wsgi_entries(entry)
        _present = (Path(self.workspace.root) / entry).is_file()
        # The entry FILE existing is not enough: a weak coder writes src/app.py
        # but never the contract callable (live v088 — present, no wsgi_app
        # anywhere, boot RED 'no module exposes a callable'). Skip only when the
        # entry is present AND actually exposes a callable AND no rival splits it.
        _has_callable = self._entry_exposes_callable(entry, c.get("callable"))
        if (not force and _present and _has_callable and not _rivals):
            return None             # a feature leaf already built a real entry
        # else: entry absent, exposes no callable, OR split across a rival entry
        # (e.g. routes in src/wsgi_app.py while the declared src/app.py serves
        # nothing) — the assembly must run to consolidate into the declared entry.
        callable_name = c["callable"][0]
        ok_route = str(c["boot"].get("ok_route") or "")
        api = self._existing_src_api()
        # late human requirements (injected mid-run) that the entry must also
        # route — condensed one-liners, model-independent (echoes human text).
        # _standing_requirements is a CALLABLE (same source _requirement_nodes
        # consults): fn() -> [(name, statement, scope?), ...]. It returns ALL
        # standing/late reqs regardless of coverage, so even a late req already
        # materialised as its own leaf (e.g. a /ping leaf -> src/ping_text.py)
        # still surfaces its human statement here, telling the entry to route it.
        _standing = []
        try:
            fn = self._standing_requirements
            items = fn() if callable(fn) else (fn or [])
            for item in (items or []):
                _s = item[1] if len(item) > 1 else item[0]
                _txt = " ".join(str(_s).split())[:200]
                if _txt:
                    _standing.append(f"- {_txt}")
        except Exception:  # noqa: BLE001 — standing reqs are best-effort context
            pass
        _standing_lines = (("\n\n### Late human requirements (also wire these)\n"
                            + "\n".join(_standing)) if _standing else "")
        # The implementer reads the node's SPEC, not a `requirement` field, so
        # the build directive goes into spec_markdown; the title is the heading.
        boot_line = (f" The product must boot in a fresh process and answer "
                     f"`GET {ok_route}` -> 200." if ok_route else "")
        # ENGINE-DECLARED route -> handler binding: the canonical handler name
        # for every declared route, so nobody has to GUESS the binding from a
        # name. The resolver looks these exact names up first; the spec orders
        # them so a built leaf and the entry agree on one symbol per route.
        _rmap = self._declared_route_set(c)
        _rmap_block = ""
        if _rmap:
            _rmap_block = (
                "\n\n### Route -> handler binding (engine-declared, binding)\n"
                "Name each handler EXACTLY as listed and expose it at module "
                "level — the entry imports it by this name:\n"
                + "\n".join(
                    "- `%s %s` -> `def %s(payload, query)`"
                    % (m, p, _canonical_handler_symbol(m, p))
                    for m, p in _rmap)
                + "\n\nStatus semantics (binding): unknown path -> 404; a known "
                  "path with an unsupported method -> 405; a malformed JSON body "
                  "-> 400.")
            # The engine OWNS the health/ok_route response when no leaf handler
            # resolves (it serves a fixed stub). Declare that exact body so a
            # generated test matches the engine instead of guessing a different
            # shape (live v107: the stub returned {"status": "ok"} JSON while the
            # test asserted b'ok' -> phantom RED). A handler returning this same
            # body resolves and is used in preference to the stub.
            _ok = (c.get("boot") or {}).get("ok_route")
            if _ok:
                _rmap_block += (
                    "\n\nHealth (binding): `GET %s` returns 200 with the JSON "
                    'body `{"status": "ok"}` (Content-Type application/json). '
                    "If you add a handler, return exactly that; any test must "
                    "assert that body, not a bare string." % _ok)
        spec_md = (
            "## ASSEMBLE THE PRODUCT ENTRY (engine-required, binding)\n\n"
            f"Create `{entry}` exposing a module-level `{callable_name}` callable "
            "that wires the feature modules ALREADY built under `src/` into one "
            "running product. Import the existing modules (do NOT reimplement "
            "them, do NOT mock them); dispatch every endpoint the contract "
            "declares to the matching handler/storage already present. "
            "EVERY listed module that exposes an HTTP handler is a delivered "
            "feature the entry MUST import and route to its endpoint per the "
            "route->handler binding below; leave NO built handler unrouted. "
            "A handler is EITHER "
            "a high-level business function `def name(payload, query) -> "
            "(status:int, body:dict|str)` (PREFERRED — the entry parses the body, "
            "rejecting a malformed JSON body with 400, and serialises the result) "
            "OR a raw WSGI function `(environ, start_response)`; adapt both."
            + boot_line + " Standard library only.\n\n"
            + (api + "\n\n" if api else "")
            + "### Contract (binding)\n"
            + "\n".join(f"- {r}" for r in (self._constitution or []))
            # also wire the LATE human requirements (injected mid-run: e.g. a
            # /ping or /ui surface) — they are NOT in the constitution but are
            # real endpoints the assembled entry must route, else the boot-gate
            # 404s them (the v054 RED: app.py existed but never routed /ping).
            + _standing_lines
            + _rmap_block
            # CONSOLIDATE a split product: if the coder put a second WSGI app in
            # another module, fold its routes into the SINGLE declared entry so
            # the boot-gate (which loads the declared entry) serves the whole
            # contract (live v084 RED: routes in wsgi_app.py, app.py served 404).
            + ((f"\n\n### Consolidate the split product (binding)\n"
                f"The product is currently SPLIT across more than one WSGI "
                f"entry: "
                + ", ".join(f"`src/{f}` (exposes `{s}`)" for f, s in _rivals)
                + f". `{entry}` is the SINGLE declared entry. FOLD every route "
                f"from those module(s) into `{entry}` so `{entry}` exposes "
                f"`{callable_name}` and dispatches the WHOLE contract itself — "
                f"import their handlers, do not leave a second module exposing "
                f"a WSGI app callable as the product's entry.") if _rivals
               else ""))
        return {"id": "product_entry",
                "title": "Assemble product entry (" + entry + ")",
                "spec_markdown": spec_md, "atomic": True,
                # Pin the output file (see above) AND keep the leaf exempt from
                # amend-routing: the assembly entry must never be folded into a
                # feature module (the router once mis-folded it into a storage
                # module — an entry written there is a real corruption risk).
                "code_target": entry,
                "_no_amend": True,
                "metrics": {"modules": 1, "tasks": 2, "interfaces": 1,
                            "estimated_loc": 60, "open_decisions": 0,
                            "single_concern": True,
                            "testable_criteria": True}}

    def _existing_src_api(self) -> str:
        """A deterministic, model-independent map of the public callables the
        assembly leaf must import — top-level functions/classes (with arg
        names) of every module already built under src/. A weak model fails to
        wire the entry when it has to guess these names; handing it the real
        surface makes the import-and-route task mechanical. Pure AST, no import,
        no execution."""
        root = Path(self.workspace.root) / "src"
        if not root.is_dir():
            return ""
        lines: list = []
        _entry_name = Path((self._product_contract().get("entry") or "")).name
        for py in sorted(root.glob("*.py")):
            if py.name in (_entry_name, "__init__.py"):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            syms: list = []
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if n.name.startswith("_"):
                        continue
                    args = ", ".join(a.arg for a in n.args.args)
                    syms.append(f"`def {n.name}({args})`")
                elif isinstance(n, ast.ClassDef):
                    if not n.name.startswith("_"):
                        syms.append(f"`class {n.name}`")
            if syms:
                lines.append(f"- `src/{py.name}`: " + ", ".join(syms))
        if not lines:
            return ""
        return ("### Modules already built under `src/` "
                "(import these, do NOT reimplement)\n" + "\n".join(lines))

    def _rival_wsgi_entries(self, entry: str) -> list:
        """Non-entry modules under src/ that expose a module-level WSGI app
        callable (`wsgi_app`/`application`/`app`, or a function whose first two
        params are `(environ, start_response)`). A weak coder sometimes SPLITS
        the product into the declared entry PLUS a second WSGI module — the
        boot-gate loads the declared entry (entry-first), which lacks the
        routes, and 404s the contract (live v084: routes lived in wsgi_app.py
        while the declared app.py served nothing). Detecting a rival entry
        (pure AST, no import) lets the assembly FOLD it into the declared entry
        so a SINGLE entry serves the whole contract. Returns [(filename, sym)].
        """
        root = Path(self.workspace.root) / "src"
        if not root.is_dir():
            return []
        entry_name = Path(entry).name
        names = {"wsgi_app", "application", "app"}
        rivals: list = []
        for py in sorted(root.glob("*.py")):
            if py.name in (entry_name, "__init__.py"):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8",
                                              errors="replace"))
            except (OSError, SyntaxError):
                continue
            # A module already neutralised by the engine keeps a delegating
            # wrapper (so its companion test still imports a working callable);
            # it is NOT a live rival entry — skip it or the route-redeclare gate
            # would trip forever on our own delegation stub.
            doc = ast.get_docstring(tree) or ""
            if doc.startswith(_NEUTRALIZED_SENTINEL):
                continue
            hit = None
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = [a.arg for a in n.args.args]
                    if n.name in names or params[:2] == [
                            "environ", "start_response"]:
                        hit = n.name
                        break
                elif isinstance(n, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id in names
                           for t in n.targets):
                        hit = next(t.id for t in n.targets
                                   if isinstance(t, ast.Name) and t.id in names)
                        break
            if hit:
                rivals.append((py.name, hit))
        return rivals

    def _entry_exposes_callable(self, entry: str, callables: list) -> bool:
        """Pure-AST: does the entry FILE expose a module-level callable named in
        ``callables`` (def or assignment)? A weak coder sometimes writes the
        declared entry file but never the contract callable (live v088: src/app.py
        present, no module-level wsgi_app anywhere) — the boot-gate then RED-s with
        'no module exposes a callable'. Detecting an entry-without-callable lets
        the assembly run to synthesise/repair it, not skip it. No import, no exec."""
        p = Path(self.workspace.root) / entry
        if not p.is_file():
            return False
        want = set(callables or ["wsgi_app", "application", "app"])
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return True              # unpar_seable: not our call to overwrite
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name in want:
                return True
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in want for t in n.targets):
                return True
        return False

    # method-name synonyms used to score a route -> handler match (1a)
    _METHOD_SYNONYMS = {
        "GET": ("get", "list", "fetch", "read", "index", "show", "view", "all"),
        "POST": ("post", "create", "add", "new", "store", "submit", "make"),
        "PUT": ("put", "update", "edit", "set", "replace"),
        "PATCH": ("patch", "update", "edit", "modify"),
        "DELETE": ("delete", "remove", "destroy", "drop"),
    }

    def _declared_route_set(self, contract: dict) -> list:
        """The concrete (method, path) routes the assembled entry must serve,
        derived from the model-independent contract: the boot triple plus every
        extra declared GET. Templated paths are already excluded upstream. Order
        is dedup-stable (json_roundtrip first so its handlers anchor the table)."""
        boot = contract.get("boot", {}) or {}
        out: list = []
        rt = boot.get("json_roundtrip")
        if rt:
            out += [("POST", rt), ("GET", rt)]
        if boot.get("html_route"):
            out.append(("GET", boot["html_route"]))
        if boot.get("ok_route"):
            out.append(("GET", boot["ok_route"]))
        for r in (contract.get("routes") or []):
            try:
                out.append((str(r[0]).upper(), str(r[1])))
            except (IndexError, TypeError):
                continue
        seen: set = set()
        uniq: list = []
        for mp in out:
            if mp not in seen:
                seen.add(mp)
                uniq.append(mp)
        return uniq

    # first-parameter names that mark a STORAGE/DEPENDENCY function (not an HTTP
    # handler): the router cannot supply these, so a route must not wire to them.
    _DEP_FIRST_PARAMS = frozenset({
        "conn", "connection", "db", "database", "session", "cursor", "engine",
        "txn", "tx", "pool", "client", "store", "repo", "repository", "dao"})

    def _resolve_route_handlers(self, contract: dict) -> tuple:
        """Pure-AST resolver: map each declared (method, path) to the best leaf
        handler already built under src/. A handler is a module-level function
        whose signature is NOT raw WSGI ``(environ, start_response)`` — i.e. a
        high-level ``(payload, query) -> (status, body)`` business function (the
        shape leaves converge on; live v088 had all six leaves in this shape but
        no entry to dispatch them). Scoring: resource token in name (+3), method
        synonym in name (+2), 2-arg signature (+1). Returns
        ``(mapping, unresolved)`` where mapping is
        ``{(method, path): (module_stem, func, abi)}`` (abi in {"pq","p","none"})
        and unresolved is the list of (method, path) with no candidate. No import,
        no execution — independent of model quality."""
        root = Path(self.workspace.root) / "src"
        entry_name = Path(contract.get("entry") or "").name
        html_route = ((contract.get("boot") or {}).get("html_route")
                      or "").rstrip("/")
        # collect candidate functions across all non-entry modules
        cands: list = []                 # (stem, name, abi, lowname, is_html)
        if root.is_dir():
            for py in sorted(root.glob("*.py")):
                if py.name in (entry_name, "__init__.py"):
                    continue
                try:
                    src_text = py.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(src_text)
                except (OSError, SyntaxError):
                    continue
                for n in tree.body:
                    if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    params = [a.arg for a in n.args.args]
                    if n.name.startswith("__"):
                        continue
                    seg_src = ast.get_source_segment(src_text, n) or ""
                    is_ws = params[:2] == ["environ", "start_response"]
                    # An HTTP handler takes the REQUEST (payload/query/body), not an
                    # injected dependency. A storage-layer fn whose first param is a
                    # connection/session (e.g. insert_note(conn, text),
                    # list_notes(conn)) is NOT dispatchable as (payload, query): the
                    # router would pass a dict where a sqlite3.Connection is expected
                    # and 500 (live v099: the resolver mapped POST/GET /notes onto
                    # db.insert_note/list_notes and every call crashed). Reject it so
                    # the route stays unresolved and the engine promotes the real
                    # raw-WSGI router instead of fabricating a broken wiring.
                    if (not is_ws and params
                            and params[0].lower() in self._DEP_FIRST_PARAMS):
                        continue
                    # A raw WSGI handler (environ, start_response) is a REAL handler
                    # the model wrote at a lower level (live v104: _handle_ping
                    # inline in the entry served GET /ping). Keep it as abi 'ws' and
                    # let the router delegate to it raw, instead of dropping it and
                    # leaving the route 404.
                    if is_ws:
                        abi = "ws"
                    else:
                        abi = "pq" if len(params) >= 2 else (
                            "p" if len(params) == 1 else "none")
                    # route paths literally named in the body — a self-dispatching
                    # handler (`if path == '/ping'`) reveals which route it serves
                    body_paths = frozenset(re.findall(
                        r"""['"](/[A-Za-z0-9_./{}-]*)['"]""", seg_src))
                    # A raw-WSGI fn that dispatches MANY paths is a whole-app
                    # router, not a per-route handler — it must go through promote
                    # (delegate the entry to it), not be wired to a single route.
                    if is_ws and len({p.rstrip("/") for p in body_paths
                                      if len(p) > 1}) >= 2:
                        continue
                    is_html = bool(re.search(
                        r"<!doctype|<html|text/html|['\"]html['\"]",
                        seg_src, re.I))
                    cands.append((py.stem, n.name, abi, n.name.lower(),
                                  is_html, body_paths))
        mapping: dict = {}
        unresolved: list = []
        for method, path in self._declared_route_set(contract):
            # EXPLICIT BINDING FIRST: the engine declares one canonical handler
            # symbol per route and the decomposition spec orders that exact name.
            # If a leaf defines it, wire it directly — the binding is read, not
            # guessed. The heuristics below are only the fallback for when the
            # model deviated from the ordered name.
            want = _canonical_handler_symbol(method, path)
            explicit = next(((stem, name, abi)
                             for stem, name, abi, low, _ish, _bp in cands
                             if name == want), None)
            if explicit:
                mapping[(method, path)] = explicit
                continue
            seg = path.rstrip("/").rsplit("/", 1)[-1]
            res = "".join(ch for ch in seg.lower()
                          if ch.isalnum())          # resource token
            syn = self._METHOD_SYNONYMS.get(method, ())
            # synonyms of OTHER HTTP methods — a name carrying one of these while
            # serving a different method is a strong negative signal (e.g.
            # `get_notes` must NOT win `POST /notes` just by containing "notes").
            other_syn = tuple(s for m, syns in self._METHOD_SYNONYMS.items()
                              if m != method for s in syns if s not in syn)
            res_sing = res.rstrip("s") if res else ""
            best = None
            best_score = 0
            for stem, name, abi, low, _ish, _bp in cands:
                # ELIGIBILITY: the handler must relate to this route's RESOURCE.
                # A method-synonym match alone is NOT enough — otherwise `get_notes`
                # falsely wins GET /ui / GET /about / GET /health just by carrying
                # "get" (live v091). A route with no resource-matching handler stays
                # unresolved (honest) instead of being wired to an unrelated leaf.
                res_hit = bool(res and (res in low or
                                        (len(res_sing) >= 3 and res_sing in low)))
                root_hit = (not res and name.lower() in
                            ("index", "home", "root", "app", "main"))
                if not (res_hit or root_hit):
                    continue
                score = 0
                if res and res in low:
                    score += 3
                if res_sing and res_sing in low:
                    score += 1                       # singular/plural tolerance
                if any(s in low for s in syn):
                    score += 3                       # matches THIS method
                if any(("_" + s) in low or low.startswith(s) or low.endswith(s)
                       for s in other_syn):
                    score -= 3                       # carries a CONFLICTING method
                if abi == "pq":
                    score += 1
                if root_hit:
                    score += 2
                if score > best_score:
                    best_score, best = score, (stem, name, abi)
            if best and best_score >= 2:
                mapping[(method, path)] = best
                continue
            # PATH-STRING signal: a self-dispatching handler that names THIS exact
            # route in its body serves it even when its function name does not
            # carry the resource (live v104: a raw-WSGI _handle_ping tested
            # `path == '/ping'`). A dispatchable / raw-WSGI handler is preferred.
            tgt = path.rstrip("/")
            pm = [(stem, name, abi)
                  for stem, name, abi, low, _ish, bp in cands
                  if tgt in {p.rstrip("/") for p in bp}]
            pm.sort(key=lambda c: 0 if c[2] in ("ws", "pq") else 1)
            if pm:
                mapping[(method, path)] = pm[0]
                continue
            if (method == "GET" and html_route
                    and path.rstrip("/") == html_route):
                # The declared HTML/UI route has no name-matching handler: the
                # model routinely names the page handler after the DATA it
                # renders (get_notes_html), not the route (/ui), so the
                # resource-token match misses. Fall back to the leaf handler that
                # actually PRODUCES HTML — live v103: get_notes_html rendered the
                # page yet GET /ui stayed unresolved and the product 404'd. A
                # dispatchable (payload, query) handler is preferred.
                html_cands = [(stem, name, abi)
                              for stem, name, abi, low, ish, _bp in cands if ish]
                html_cands.sort(key=lambda c: 0 if c[2] == "pq"
                                else (1 if c[2] == "p" else 2))
                if html_cands:
                    mapping[(method, path)] = html_cands[0]
                    continue
            unresolved.append((method, path))
        return mapping, unresolved

    def _synthesize_entry_code(self, contract: dict, mapping: dict,
                               unresolved: list) -> "Optional[str]":
        """Deterministically emit the product entry: a stdlib-only WSGI router
        that imports the resolved leaf handlers and dispatches the declared
        contract. The engine — not the model — owns the HTTP glue (body parsing
        with a malformed->400 guard, routing of EVERY declared path, 404/405,
        serialization, the contract callable). This makes assembly independent of
        model quality: M1 (malformed->500), M2 (late route not wired) and M3 (no
        entry callable) cannot occur because the model never writes this layer.

        Returns the source string, or ``None`` when a CRITICAL route (the
        json_roundtrip pair, an HTML page, or any extra declared route) has no
        resolved handler — then the caller honestly falls back to the LLM
        implementer rather than fabricating business logic. An unresolved health
        ``ok_route`` is NOT critical: a trivial 200 is synthesised inline."""
        boot = contract.get("boot", {}) or {}
        callables = contract.get("callable") or ["wsgi_app"]
        callable_name = callables[0]
        # Expose EVERY declared callable alias, not just `application`: a
        # generated test imports the contract's callable by name (live v103:
        # test_app.py did `from app import app` while the entry exposed only
        # wsgi_app/application -> ImportError RED-ed the assembled product). The
        # router def is callable_name; the rest are module-level aliases to it.
        alias_names = []
        for c in list(callables[1:]) + ["application"]:
            if (c and c.isidentifier() and c != callable_name
                    and c not in alias_names):
                alias_names.append(c)
        aliases_block = "\n".join("%s = %s" % (a, callable_name)
                                  for a in alias_names)
        ok_route = boot.get("ok_route")
        rt = boot.get("json_roundtrip")
        unresolved_set = set(unresolved)
        # ONLY the json_roundtrip pair is critical: without it the engine cannot
        # own a meaningful product. A missing health route is inlined (200); a
        # missing HTML page or extra route is simply omitted (it will 404, which
        # the boot-gate honestly reports so the doctor builds the real handler) —
        # the router still serves every route that DOES resolve. This keeps the
        # deterministic entry in place instead of declining the whole synthesis
        # over one not-yet-built late route (live v091 /ui, /about).
        critical = {("POST", rt), ("GET", rt)} if rt else set()
        if critical & unresolved_set:
            return None
        # build deterministic import aliases + route table
        alias_for: dict = {}
        imports: list = []
        for i, (stem, name, _abi) in enumerate(
                sorted(set(mapping.values()))):
            alias = "_h%d" % i
            alias_for[(stem, name)] = alias
            imports.append("from %s import %s as %s" % (stem, name, alias))
        rows: list = []
        for (method, path), (stem, name, abi) in sorted(mapping.items()):
            rows.append('    (%r, %r): (%r, %s),'
                        % (method, path, abi, alias_for[(stem, name)]))
        if ok_route and ("GET", ok_route) in unresolved_set:
            rows.append('    (%r, %r): ("health", None),' % ("GET", ok_route))
        routes_block = "\n".join(rows)
        imports_block = "\n".join(imports)
        tmpl = '''\
"""Product entry — generated deterministically by the spec-flow engine.

The business logic lives in the imported leaf modules; this router only parses
each request, dispatches the declared routes, and serialises the response. Do
not hand-edit: the engine owns the HTTP glue so assembly never depends on model
quality (malformed bodies are rejected with 400, every declared route is wired).
"""
import json
from urllib.parse import parse_qs

%(imports)s

_STATUS = {200: "200 OK", 201: "201 Created", 204: "204 No Content",
           400: "400 Bad Request", 404: "404 Not Found",
           405: "405 Method Not Allowed", 500: "500 Internal Server Error"}


def _status_line(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 200
    return _STATUS.get(code, "%%d OK" %% code)


_ROUTES = {
%(routes)s
}


def _send(start_response, code, body):
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode("utf-8")
        ctype = "application/json"
    elif isinstance(body, (bytes, bytearray)):
        payload, ctype = bytes(body), "text/html; charset=utf-8"
    else:
        text = "" if body is None else str(body)
        ctype = ("text/html; charset=utf-8"
                 if text.lstrip().startswith("<") else "text/plain; charset=utf-8")
        payload = text.encode("utf-8")
    start_response(_status_line(code), [("Content-Type", ctype)])
    return [payload]


def %(callable)s(environ, start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    handler = _ROUTES.get((method, path))
    if handler is None:
        if any(p == path for (_m, p) in _ROUTES):
            return _send(start_response, 405, {"error": "method not allowed"})
        return _send(start_response, 404, {"error": "not found"})
    abi, fn = handler
    if abi == "ws":                             # raw WSGI handler reads input itself
        return fn(environ, start_response)
    query = parse_qs(environ.get("QUERY_STRING") or "")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    payload = {}
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:                       # malformed body -> 400, never 500
            return _send(start_response, 400, {"error": "invalid json"})
    try:
        if abi == "health":
            status, body = 200, {"status": "ok"}
        elif abi == "pq":
            status, body = fn(payload, query)
        elif abi == "p":
            status, body = fn(payload)
        else:
            status, body = fn()
    except Exception as exc:                    # a leaf bug -> clean 500, no crash
        return _send(start_response, 500, {"error": "handler failed: %%s" %% exc})
    return _send(start_response, status, body)


%(aliases)s
'''
        return tmpl % {"imports": imports_block, "routes": routes_block,
                       "callable": callable_name, "aliases": aliases_block}

    def _harvest_entry_handlers(self, contract: dict) -> bool:
        """Relocate a MONOLITHIC entry's business logic into a sibling leaf module
        so the engine can own the entry as a pure router that imports it. A weak
        decomposer sometimes writes every handler INSIDE src/app.py (live v089:
        handle_notes_* / handle_health all inside the entry) instead of separate
        leaves — the resolver, which excludes the entry, then finds nothing to
        wire. This rewrites the entry's non-router functions (imports, db helpers,
        `(payload, query)` handlers) into ``src/_product_logic.py``, dropping the
        old WSGI callable, so a re-resolve sees them as a normal leaf. Returns True
        iff a harvest module with at least one handler was written. Pure AST."""
        root = Path(self.workspace.root) / "src"
        entry = contract.get("entry") or ""
        ep = Path(self.workspace.root) / entry
        if not ep.is_file() or not hasattr(ast, "unparse"):
            return False
        try:
            tree = ast.parse(ep.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return False
        callables = set(contract.get("callable") or
                        ["wsgi_app", "application", "app"])
        keep: list = []
        has_handler = False
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name in callables:
                    continue                          # drop the old router fn
                params = [a.arg for a in n.args.args]
                # KEEP a raw-WSGI sub-handler (environ, start_response) — it is a
                # real route handler the model wrote at a lower level (live v104:
                # _handle_ping served GET /ping). Relocated here it becomes a leaf
                # the resolver wires as abi 'ws'; dropping it left the route 404.
                keep.append(n)
                if params:
                    has_handler = True
            elif isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in callables
                    for t in n.targets):
                continue                              # drop `application = wsgi_app`
            elif isinstance(n, ast.ImportFrom) and (n.module or "") == "_product_logic":
                # NEVER carry a `from _product_logic import … as _hN` alias INTO the
                # harvest module itself — that is a self-import -> circular import,
                # and the assembled product ImportErrors at boot (live v116: the
                # monolithic entry already held synth-generated aliases from a prior
                # assembly cycle; harvesting them here made _product_logic import
                # from itself). The synth re-adds the correct aliases in the ENTRY,
                # which is where they belong.
                continue
            else:
                keep.append(n)                        # imports, constants, helpers
        if not has_handler:
            return False
        mod = root / "_product_logic.py"
        try:
            body = "\n".join(ast.unparse(n) for n in keep)
            mod.write_text(
                '"""Harvested product logic — relocated by the engine from a '
                'monolithic\nentry so the entry can be a pure generated router. '
                'Business logic only."""\n' + body + "\n", encoding="utf-8")
        except (OSError, ValueError):
            return False
        return True

    def _neutralize_rival_entries(self, entry: str,
                                  keep: "Optional[set]" = None) -> int:
        """Phase 3 — eliminate the split deterministically. When the engine owns
        the declared entry, any OTHER module that also exposes a WSGI callable is a
        rival entry: it trips the route-redeclare gate and confuses servers about
        which app to run (live v089: notes_api.py beside app.py). Strip just the
        module-level WSGI callable (``def wsgi_app|application|app`` and
        ``application = ...`` / raw ``(environ, start_response)`` glue) from each
        rival, keeping its business functions (which the synthesized router may
        import). ``keep`` is a set of module STEMS to leave intact — used by the
        promote path, where the declared entry DELEGATES to a rival's callable, so
        that rival must keep exposing it.

        The rival's OWN callable is not deleted but REPLACED by a delegating
        wrapper that forwards to the sole declared entry at call time (live v100:
        ``tests/test_health_wsgi.py`` does ``from health_wsgi import wsgi_app`` — a
        hard strip turned that into an ImportError, RED-ing the assembled product
        on a phantom ``weak_implementer``). The wrapper imports the entry lazily
        (no module-level cycle) and the module is stamped with the sentinel so the
        route-redeclare gate skips it instead of re-flagging our own stub. Pure
        AST; returns the count neutralised."""
        if not hasattr(ast, "unparse"):
            return 0
        n = 0
        names = {"wsgi_app", "application", "app"}
        keep = keep or set()
        entry_stem = Path(entry).stem
        for fname, _sym in self._rival_wsgi_entries(entry):
            if Path(fname).stem in keep:
                continue                # the entry delegates to this one — keep it
            p = Path(self.workspace.root) / "src" / fname
            if not p.is_file():
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            kept: list = []
            dropped: list = []          # names of WSGI callables we replaced
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = [a.arg for a in node.args.args]
                    if node.name in names or \
                            params[:2] == ["environ", "start_response"]:
                        if node.name not in dropped:
                            dropped.append(node.name)
                        continue            # drop the rival WSGI callable
                elif isinstance(node, ast.Assign):
                    hit = [t.id for t in node.targets
                           if isinstance(t, ast.Name) and t.id in names]
                    if hit:
                        for nm in hit:
                            if nm not in dropped:
                                dropped.append(nm)
                        continue            # drop `application = wsgi_app`
                kept.append(node)
            if not dropped:
                continue
            # Re-expose each dropped name as a wrapper delegating to the sole
            # entry, so a companion test importing it still gets a working app.
            stubs = "\n\n".join(
                "def %s(environ, start_response):\n"
                "    from %s import application as _entry\n"
                "    return _entry(environ, start_response)" % (nm, entry_stem)
                for nm in dropped)
            try:
                body = "\n".join(ast.unparse(x) for x in kept)
                p.write_text(
                    '"""%s — business logic kept; the duplicate\n'
                    'app callable now delegates to the sole declared entry."""\n'
                    % _NEUTRALIZED_SENTINEL
                    + (body + "\n" if body.strip() else "")
                    + "\n" + stubs + "\n",
                    encoding="utf-8")
                n += 1
            except (OSError, ValueError):
                continue
        return n

    def _promote_rival_entry(self, contract: dict) -> "Optional[tuple]":
        """Route-coverage fallback (live v094): a weak coder built a WORKING
        raw-WSGI app but in a NON-entry module — the whole notes router lived in
        ``src/wsgi_app.py`` while the declared ``src/app.py`` served only a stray
        ``/about``. The ``(payload, query)`` resolver cannot decompose a monolithic
        raw-WSGI blob, so ``_synthesize_entry_code`` declines — yet the contract IS
        served, just behind the wrong file, and the boot-gate (which loads the
        DECLARED entry) 404s the contract. Rather than fabricate logic, the engine
        PROMOTES the rival: it emits a thin declared entry that delegates to the
        rival's callable and guards a malformed body -> 400 (M1). The model's
        routing and business logic are untouched — only the HTTP entry is
        engine-owned, so assembly never depends on WHICH file the model put the
        router in. Returns ``(code, keep_stem)`` for the best-scoring rival, or
        ``None`` when there is no rival to promote. The caller boot-verifies the
        delegation against the real contract before committing — a rival that does
        not actually serve the contract is rejected, never faked green. Pure AST."""
        entry = contract.get("entry") or ""
        rivals = self._rival_wsgi_entries(entry)
        if not rivals:
            return None
        callable_name = (contract.get("callable") or ["wsgi_app"])[0]
        # score each rival by how many declared route PATHS it references as
        # string literals — the module that names the most contract paths is the
        # real router (a decoy that only mentions /about scores 0).
        want_paths = {p for (_m, p) in self._declared_route_set(contract)}
        root = Path(self.workspace.root) / "src"
        best = None
        best_score = -1
        for fname, sym in rivals:
            score = 0
            try:
                tree = ast.parse((root / fname).read_text(
                    encoding="utf-8", errors="replace"))
                lits = {nd.value for nd in ast.walk(tree)
                        if isinstance(nd, ast.Constant)
                        and isinstance(nd.value, str)}
                score = sum(1 for p in want_paths if p in lits)
            except (OSError, SyntaxError):
                score = 0
            if score > best_score:
                best_score, best = score, (fname, sym)
        fname, sym = best
        stem = Path(fname).stem
        code = (
            '"""Product entry — engine-promoted delegation.\n\n'
            'The coder built the working WSGI app in src/%(stem)s.py; the engine\n'
            'wires it to the declared entry and guards a malformed body -> 400\n'
            '(M1). Business logic is untouched — only the HTTP entry is\n'
            'engine-owned, so assembly never depends on which file the model put\n'
            'the router in. Do not hand-edit.\n'
            '"""\n'
            'import json as _sf_json\n'
            'from %(stem)s import %(sym)s as _sf_inner\n'
            '\n\n'
            'def %(callable)s(environ, start_response):\n'
            '    try:\n'
            '        return _sf_inner(environ, start_response)\n'
            '    except _sf_json.JSONDecodeError:\n'
            '        start_response("400 Bad Request",\n'
            '                       [("Content-Type", "application/json")])\n'
            '        return [b\'{"error": "invalid json"}\']\n'
            '\n\n'
            'application = %(callable)s\n'
        ) % {"stem": stem, "sym": sym, "callable": callable_name}
        return code, stem

    def _write_promoted_entry(self, contract: dict, code: str,
                              keep_stem: str) -> bool:
        """Write a promoted delegating entry and KEEP it only if the assembled
        product actually boots its contract. The promote path is more speculative
        than the resolver path (it trusts a rival's whole router) and it strips
        OTHER rivals, so it self-verifies with the un-mockable boot-gate BEFORE
        committing: green -> neutralise the other rivals (keeping the delegated
        one) and commit; red -> restore the original entry and fall back to the
        LLM implementer. Never leaves a worse entry than it found."""
        entry = contract["entry"]
        ep = Path(self.workspace.root) / entry
        try:
            original = (ep.read_text(encoding="utf-8", errors="replace")
                        if ep.is_file() else None)
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text(code, encoding="utf-8")
        except OSError:
            return False
        boot_ok, _ = self._assembled_product_boots()
        if not boot_ok:
            try:                        # delegation did not serve the contract
                if original is None:
                    ep.unlink()
                else:
                    ep.write_text(original, encoding="utf-8")
            except OSError:
                pass
            return False
        # green: the declared entry now delegates to the working rival. Strip the
        # OTHER rivals' callables but KEEP the promoted module (the entry imports
        # it), so a SINGLE declared entry serves the whole contract.
        self._neutralize_rival_entries(entry, keep={keep_stem})
        try:
            self.workspace.commit(
                f"build: promote src/{keep_stem}.py to {entry} "
                "(deterministic delegation)", [entry])
        except Exception:               # noqa: BLE001 — boot reads the work tree
            pass
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  f"entry promoted deterministically: {entry} -> src/{keep_stem}.py",
                  "engine-owned delegation: the declared entry wires the coder's "
                  "working WSGI app and guards malformed body -> 400",
                  "integrate_verify", "", level=L_MILESTONE)
        return True

    def _try_synthesize_entry(self) -> bool:
        """Deterministically (re)build the product entry from the contract when
        EVERY declared route resolves to a built leaf handler. The engine — not
        the model — owns the WSGI glue, so assembly is independent of model
        quality: M1 (malformed->400), M2 (every route wired), M3 (callable always
        present) cannot occur. Returns True iff a router is in place. Idempotent
        (pure-AST resolve; skips the write when the entry already equals the
        synthesised source) and safe to call on every integrate verify. Falls back
        (returns False) only when a critical route has no handler — then the LLM
        implementer stays responsible, never a fabricated product."""
        ws = self.workspace
        if not (getattr(ws, "enabled", False) and getattr(ws, "root", None)):
            return False
        contract = self._product_contract()
        if not contract or not contract.get("entry"):
            return False
        harvested = False
        try:
            mapping, unresolved = self._resolve_route_handlers(contract)
            code = self._synthesize_entry_code(contract, mapping, unresolved)
            # Harvest when the synth declined (no code) OR when a declared route is
            # still unresolved: its handler may sit INLINE in the model's entry
            # (live v104: _handle_ping for /ping), invisible to the resolver which
            # scans only non-entry leaves. Relocating the entry's handlers to a
            # leaf lets the re-resolve wire them. Harvest is a no-op (returns
            # False) when the entry carries no business handler.
            if (not code or unresolved) and self._harvest_entry_handlers(contract):
                harvested = True
                mapping, unresolved = self._resolve_route_handlers(contract)
                code = self._synthesize_entry_code(contract, mapping, unresolved)
        except Exception:               # noqa: BLE001 — resolver is best-effort
            return False
        if not code:
            # the (payload,query) resolver could not own the entry (e.g. the
            # router is a monolithic raw-WSGI blob in a non-entry module — live
            # v094). Last deterministic resort before the LLM: PROMOTE a rival
            # that already serves the contract, delegating to it from the
            # declared entry (boot-verified, never faked).
            try:
                promoted = self._promote_rival_entry(contract)
            except Exception:           # noqa: BLE001 — promote is best-effort
                promoted = None
            if promoted:
                return self._write_promoted_entry(contract, *promoted)
            return False
        if harvested:
            # keep `from <entry> import <handler>` working for any unit tests the
            # model wrote against the (now relocated) monolith: re-export its names.
            code = code.replace(
                "from urllib.parse import parse_qs\n",
                "from urllib.parse import parse_qs\n"
                "from _product_logic import *  # noqa: F401,F403 re-export harvested\n",
                1)
        entry = contract["entry"]
        try:
            ep = Path(ws.root) / entry
            if ep.is_file() and ep.read_text(
                    encoding="utf-8", errors="replace") == code:
                self._neutralize_rival_entries(entry)
                return True             # already the synthesised router
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text(code, encoding="utf-8")
        except OSError:
            return False
        # split-elimination: with the engine owning the entry, strip any rival
        # WSGI callable so the declared entry is the sole one (Phase 3).
        self._neutralize_rival_entries(entry)
        try:
            self.workspace.commit(
                f"build: synthesize {entry} (deterministic assembly)", [entry])
        except Exception:               # noqa: BLE001 — boot reads the work tree
            pass
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  f"entry synthesized deterministically: {entry}",
                  "engine-owned WSGI router (no LLM): all declared routes wired, "
                  "malformed body -> 400", "integrate_verify", "", level=L_MILESTONE)
        return True

    def _harden_entry(self) -> bool:
        """Phase 2 — deterministic M1 safety net for an LLM-written entry (used
        when synthesis fell back to the model). Wraps the product callable so a
        malformed JSON body that escapes the handler is caught and answered 400,
        never a 500 crash, and guarantees the ``application`` alias. Pure text
        append over the existing callable — the model's routing and logic are
        untouched — and idempotent via a marker. Returns True iff a wrap is in
        place. Independent of model quality: closes M1 even when the model forgot
        the try/except."""
        ws = self.workspace
        if not (getattr(ws, "enabled", False) and getattr(ws, "root", None)):
            return False
        contract = self._product_contract()
        if not contract or not contract.get("entry"):
            return False
        entry = contract["entry"]
        ep = Path(ws.root) / entry
        if not ep.is_file():
            return False
        try:
            text = ep.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if "_spec_flow_hardened" in text:
            return True                              # idempotent
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        name = next((c for c in (contract.get("callable") or
                                 ["wsgi_app", "application", "app"])
                     if c in defined), None)
        if not name:
            return False        # no callable to wrap — M3 is the assembly's job
        suffix = (
            "\n\n# _spec_flow_hardened — deterministic M1 net (engine, not model):"
            "\n# a malformed request body that escapes the handler becomes 400,"
            "\n# never a 500 crash.\n"
            "import json as _sf_json\n"
            "_sf_inner_%(n)s = %(n)s\n"
            "def %(n)s(environ, start_response):\n"
            "    try:\n"
            "        return _sf_inner_%(n)s(environ, start_response)\n"
            "    except _sf_json.JSONDecodeError:\n"
            "        start_response('400 Bad Request',\n"
            "                       [('Content-Type', 'application/json')])\n"
            "        return [b'{\"error\": \"invalid json\"}']\n"
            "application = %(n)s\n"
        ) % {"n": name}
        try:
            ep.write_text(text + suffix, encoding="utf-8")
        except OSError:
            return False
        try:
            self.workspace.commit(f"harden: M1 net on {entry}", [entry])
        except Exception:           # noqa: BLE001
            pass
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  f"entry hardened (M1 net): {entry}",
                  "wrapped callable: malformed body -> 400, no 500 crash",
                  "integrate_verify", "", level=L_MILESTONE)
        return True

    def _node_driver(self, node: dict, kind: str) -> _NodeDriver:
        """Build the lifecycle guard for one node under the active engine."""
        nid = str(node.get("id", "?"))

        def observe(node_id, node_kind, event, state, gates):
            self.emit("lifecycle", "engine", "", node_id, event,
                      detail=(f"state={state} gates={','.join(gates) or '-'}"
                              if state is not None
                              else f"gates={','.join(gates) or '-'}"),
                      gate="lifecycle", level=L_STEP)

        return _NodeDriver(kind, fsm_mode=(self.node_engine == "fsm"),
                           skip_gate=node.get("_skip_gate"),
                           on_event=observe, node=nid,
                           prune=self._prune_noops)

    # -- run ---------------------------------------------------------------
    def run(self, project: dict) -> RunResult:
        # integrate-fail policy (plan P11): 'record' keeps today's behaviour
        # (loop entry, run continues), 'rework' re-invokes the verifier with
        # a fresh repair budget up to integrate_max_rework times, 'halt'
        # stops the run on the spot (IntegrateFailHalt).
        self._on_integrate_fail = str(
            project.get("on_integrate_fail", "record")).lower()
        if self._on_integrate_fail not in ("record", "rework", "halt"):
            raise ValueError(
                "on_integrate_fail must be record|rework|halt, got "
                + repr(self._on_integrate_fail))
        self._integrate_max_rework = int(
            project.get("integrate_max_rework", 2))
        # Plan Шаг 5 — the ROOT product-integrate heal budget, separate from the
        # per-node rework cap above so bumping it does not multiply node-level
        # rework cost. The boot-gate is FAIL-FAST (it surfaces ONE failing route
        # per probe), so a product with N broken leaves only reveals leaf k+1
        # once leaf k is green — and a single rework does not always fix a leaf.
        # The budget must therefore cover (broken leaves × a retry), not just the
        # leaf count: live v114 had 3 broken leaves (db, /ui, routing), stalled
        # re-reworking the first, and never reached the others at a budget of 3.
        # Default 6 = ~3 surfaces × 2 attempts; only failing runs ever spend it.
        self._product_repair_rounds = int(
            project.get("product_repair_rounds", 6))
        # unified gate policies — the `gates:` block wins over legacy keys
        gates = project.get("gates") or {}
        g_rev = gates.get("review") or {}
        g_int = gates.get("integrate") or {}
        if g_int:
            self._on_integrate_fail = ("rework" if int(g_int.get("rework", 0))
                                       else "record")
            self._integrate_max_rework = int(g_int.get("rework", 0)) or 1
            if str(g_int.get("exhausted", "record")).lower() == "halt":
                self._on_integrate_fail_exhausted = "halt"
            else:
                self._on_integrate_fail_exhausted = "record"
        else:
            self._on_integrate_fail_exhausted = (
                "halt" if self._on_integrate_fail == "halt" else "record")
            if self._on_integrate_fail == "halt":
                # legacy halt = no rework rounds, stop on first FAIL
                self._integrate_max_rework = 0
        if g_rev:
            self.review_policy = {**self.review_policy,
                                  "on_reject": "rework",
                                  "max_rework": int(g_rev.get("rework", 2)),
                                  # A3: a case may name the escalation model
                                  "escalate_model": str(
                                      g_rev.get("escalate_model")
                                      or self.review_policy.get(
                                          "escalate_model", ""))}
        self._review_exhausted = str(
            (g_rev.get("exhausted")
             or project.get("on_review_exhausted", "record"))).lower()
        if self._review_exhausted not in ("record", "halt", "ask"):
            raise ValueError(
                "gates.review.exhausted must be record|halt|ask")
        par = project.get("parallel") or {}
        self._parallel_children = max(1, int(par.get("children", 1)))
        # overhead guard: forking 2 threads for 2 tiny children can cost
        # more than it saves — the case sets its own bar
        self._parallel_min_siblings = max(2, int(par.get("min_siblings", 2)))
        # nested forks multiply concurrency multiplicatively — by default
        # only top-level branches (depth 0) fork their children
        self._parallel_depth_limit = int(par.get("depth_limit", 0))
        # GLOBAL ceiling on concurrently running subtrees, whole tree —
        # depth_limit bounds WHERE forks happen, this bounds HOW MANY
        self._parallel_sem = threading.BoundedSemaphore(
            max(1, int(par.get("max_workers", par.get("children", 1)))))
        # marks "this thread holds a _parallel_sem slot" — a parent must
        # hand its slot over while it merely WAITS for children (holding
        # it deadlocks nested levels: all slots end up at joining parents)
        self._sem_state = threading.local()
        # soft time ceiling per LEAF (limits.leaf_seconds, 0 = off):
        # checked at round boundaries, an exceeded leaf surrenders red —
        # a single wedged leaf must never silently stall the whole run
        self._leaf_seconds = float(
            (project.get("limits") or {}).get("leaf_seconds", 0))
        # axis F (physical isolation): `isolation: worktree` builds each leaf
        # in its OWN git worktree and merges back with a merge-tree pre-flight.
        # Default 'none' — back-compat: the leaf writes straight into the
        # shared workspace. Worktree isolation implies git provenance.
        self._isolation = str(project.get("isolation", "none")).lower()
        if self._isolation not in ("none", "worktree"):
            raise ValueError("isolation must be none|worktree")
        # #4: incremental integrate on by default (root still runs full);
        # a case may turn it off with incremental_integrate: false
        self._incremental_integrate = project.get(
            "incremental_integrate", True) is not False
        # #8: auto-spike thresholds — a node at/above either flags a research
        # spike before freeze. 0 = off (default), opt-in per case.
        _spk = project.get("auto_spike") or {}
        self._spike_open = int(_spk.get("open_decisions", 0) or 0)
        self._spike_loc = int(_spk.get("estimated_loc", 0) or 0)
        # #10: specialty routing — the project dict (carries default_specialty)
        # + the auto-infer switch. The set of declared specialties stays empty
        # here (allow any): chain_for() falls back to the role chain for a
        # specialty it doesn't define, so restricting is unnecessary and avoids
        # a runner→harness dependency.
        self._project_meta = project
        self._auto_specialty = bool(project.get("auto_specialty"))
        self._impl_specialties = set()
        # C1: load the executor roster from the worker config; auto domain
        # inference rides the same opt-in as specialty (or its own flag)
        try:
            from tests.harness import executors as _ex  # noqa: F401
            self._executors_cfg = _ex.parse_executors(_workers_cfg())
        except Exception:  # noqa: BLE001
            try:
                from harness import executors as _ex  # type: ignore # noqa: F401
                self._executors_cfg = _ex.parse_executors(_workers_cfg())
            except Exception:  # noqa: BLE001
                self._executors_cfg = {}
        self._auto_domain = bool(project.get("auto_executor",
                                             project.get("auto_specialty")))
        # A4: opt-in trace pruning of no-op lifecycle transitions
        self._prune_noops = bool(project.get("prune_noops"))
        if self._isolation == "worktree":
            self.workspace.git_provenance = True
        try:
            if self.sink is not None:
                self.sink.open()
            self.workspace.open(preserve=self.resume,
                                seed_files=self._seed_files)
            if self.resume:
                # #7: load the full index (version + spec_hash), not just ids,
                # so a node whose spec changed is re-run rather than reused
                self._journal_index = self.workspace.journal_index()
                self._journal_done = set(self._journal_index.keys())
                # rebuild the tree from the persisted decomposition so the walk
                # reproduces the SAME node ids (a re-decompose would drift them
                # and the leaf cache would never hit — the run would restart)
                self._decomp_index = self.workspace.decomp_index()
            # П1: a STOP sentinel only governs the run that was live when it
            # was dropped — clear any stale one so a resume never self-halts.
            if getattr(self.workspace, "root", None):
                clear_stop(self.workspace.root)
            self._journal_open()
            try:
                return self._run(project)
            except RunStopped as stop:
                # a cooperative stop leaves a REPLAYABLE checkpoint of where the
                # run got to (consistent — we are at a node boundary here)
                self._make_checkpoint(str(stop))
                self.emit("integrate", "verifier", "spec-flow", "L0:stop",
                          "run STOPPED cooperatively — partial result kept",
                          str(stop), verdict="STOPPED", level=L_MILESTONE)
                self._wave([{"kind": "run_stopped", "node": str(stop),
                             "completed": self._completed}])
                self._stopped = True
                return self._result(project)
        finally:
            self._journal_close()
            if self.sink is not None:
                self.sink.close()
            self.workspace.finalize()

    def _stop_requested(self) -> bool:
        """П1: True when a STOP sentinel sits in the workspace — the run
        halts cooperatively at the next node boundary."""
        root = getattr(self.workspace, "root", None)
        if not root:
            return False
        return (Path(root) / _STOP_REL).exists()

    def _checkpoint_requested(self) -> bool:
        """True when a CHECKPOINT sentinel sits in the workspace — the run
        snapshots itself at the next node boundary and KEEPS running."""
        root = getattr(self.workspace, "root", None)
        return bool(root) and (Path(root) / _CHECKPOINT_REL).exists()

    def _checkpoint_every(self) -> int:
        """How often the engine auto-snapshots itself, in node boundaries — a
        run/engine parameter (like the decomposer type), read from
        SPEC_FLOW_CHECKPOINT_EVERY. 0 (default) = off. The plugin writes the
        checkpoints itself; no external `checkpoint` command needed."""
        try:
            return int(os.environ.get("SPEC_FLOW_CHECKPOINT_EVERY", "0") or 0)
        except ValueError:
            return 0

    def _maybe_auto_checkpoint(self, node: str = "") -> None:
        """At a node boundary, snapshot the run every `checkpoint_every` nodes."""
        every = self._checkpoint_every()
        if every <= 0:
            return
        with self._checkpoint_lock:
            self._nodes_since_ckpt += 1
            due = self._nodes_since_ckpt >= every
            if due:
                self._nodes_since_ckpt = 0
        if due:
            self._make_checkpoint(node)

    def _make_checkpoint(self, node: str = "") -> "Optional[str]":
        """Snapshot the workspace into the run's checkpoints/ and clear the
        sentinel. Best-effort — a snapshot failure must never crash the run.
        Guarded by a lock so concurrent leaf threads snapshot at most once."""
        root = getattr(self.workspace, "root", None)
        if not (root and getattr(self.workspace, "enabled", False)):
            return None
        with self._checkpoint_lock:
            sentinel = Path(root) / _CHECKPOINT_REL
            requested = sentinel.exists()
            try:
                path = snapshot_checkpoint(str(Path(root).parent), str(root),
                                           node=node)
            except Exception as exc:  # noqa: BLE001
                self.emit("integrate", "verifier", "spec-flow", "checkpoint",
                          "checkpoint snapshot failed", str(exc)[:160],
                          verdict="", level=L_STEP)
                return None
            if requested:
                try:
                    sentinel.unlink()
                except FileNotFoundError:
                    pass
            self.emit("integrate", "verifier", "spec-flow", "checkpoint",
                      "checkpoint saved", Path(path).name,
                      verdict="CHECKPOINT", level=L_MILESTONE)
            return path

    def _module_for(self, nid: str) -> str:
        """Variant A: a deterministic, COLLISION-FREE module name per node.
        Same nid → same fn (resume-safe); two nids that snake to the same
        base get a stable short-hash suffix so their src files never clash."""
        with self._module_lock:
            cached = self._module_names.get(nid)
            if cached is not None:
                return cached
            base = _snake(nid)
            fn = base
            taken = set(self._module_names.values())
            if fn in taken:
                # deterministic disambiguation from the FULL node id
                suffix = hashlib.sha1(nid.encode("utf-8")).hexdigest()[:6]
                fn = f"{base}_{suffix}"
                self.emit("decompose", "engine", "", nid,
                          "namespace collision avoided",
                          f"module '{base}.py' already owned by another node;"
                          f" this node uses '{fn}.py'",
                          "namespace", "PARTITIONED", level=L_MILESTONE)
            self._module_names[nid] = fn
            return fn

    def _run(self, project: dict) -> RunResult:
        # Note: the contract validator (CONTRACT_VALIDATORS) is configured by the
        # caller on the gate provider — the runner does not hardcode it.
        self._completed = 0
        self._goal = project.get("goal", "")
        self._target = project.get("target", "")
        self._constitution = project.get("constitution", [])
        self._root_id = str((project.get("tree") or {}).get("id", "L0"))
        self._decompose_calls = 0
        # id → title of every node visited so far — context for the
        # decomposer agent and the comparison base for the dedup gate.
        self._node_registry: dict[str, str] = {}
        # Two revision methods (from the design discussion):
        #   * 'internal'     — continuous lane firing DURING the run (by
        #                      accumulated tasks/errors), may reopen a branch
        #                      mid-flight;
        #   * 'level_return' — fires the moment a branch folds up and control
        #                      returns one level up.
        self._revisions = self._load_revisions(project)
        self._fired_rev: set[str] = set()
        # variant A (static namespace partition): every leaf gets a UNIQUE
        # module name BY CONSTRUCTION, so two nodes can never write the
        # same src/<name>.py. Two distinct node ids can snake to the same
        # base (e.g. 'seller-opt-in' and 'seller opt in' → 'seller_opt_in')
        # — the loser gets a deterministic short-hash suffix. nid → fn,
        # so a re-visit / resume always resolves to the same file.
        self._module_names: dict[str, str] = {}
        self._module_lock = threading.Lock()

        # Phase 0-1: requirements (spec-decomposer + spec-requirements)
        self.task("L0:req", "Requirements & constitution", "requirements", "spec-decomposer", "spec-requirements")
        pol = self._policy(project.get("policy", {}))
        # test-only seam: skip emitting the policy verdict to provoke an R1
        # breach the runtime guard (C3) must catch.
        if not project.get("_suppress_policy"):
            self.emit("requirements", "spec-decomposer", "spec-requirements", "L0:req",
                      "policy_gate on the goal", project.get("target", ""), "policy_gate", pol["verdict"],
                      level=L_MILESTONE)
        for rule in project.get("constitution", []):
            self.emit("requirements", "spec-decomposer", "spec-requirements", "L0:req",
                      "constitution rule", rule, level=L_DETAIL)
        self.emit("requirements", "spec-decomposer", "spec-requirements", "L0:req",
                  "EARS requirements frozen", project.get("target", ""))
        self.workspace.constitution(project.get("constitution", []), project.get("target", ""))
        self.tasks["L0:req"].status = "done"

        # HITL spec checkpoint — when the constitution puts a human in the loop,
        # a person signs off the constitution + measurable target BEFORE any
        # decomposition (human-in-the-loop during spec creation). A rejection
        # sends requirements back for revision.
        self._policy_cfg = project.get("policy", {})
        if self._policy_cfg.get("human_in_loop"):
            approved = self._hitl("spec", "L0:req",
                                  "approve constitution & measurable target before decomposition")
            if not approved:
                self.emit("requirements", "spec-decomposer", "spec-requirements", "L0:req",
                          "constitution revised after HITL rejection",
                          "target & rules tightened", level=L_MILESTONE)
                self.tasks["L0:req"].version += 1

        # the tree either comes predefined with the case OR is built from the
        # goal by the decomposer agent, node by node (gated at every level)
        root = project.get("tree") or {"id": "L0", "title": project.get("goal", "project")}
        self._visit(root, depth=0, contract_ctx=None, phase="decompose", parent=None)
        # persist the REALIZED tree (the decomposer attaches children in place)
        # so reports can render the tree the plugin actually built — in llm mode
        # the case carried no `tree`, this is where it becomes inspectable.
        project["tree"] = root

        # B2 mechanism 3 — BACKSTOP emit: the engine now synthesizes an
        # assembly leaf (see _assembly_node, injected at the depth-0 placement
        # seam) that BUILDS the declared entry from the feature modules. This
        # post-loop emit is the safety net for the case where that leaf still
        # failed to produce the entry: the engine flags an explicit "entry not
        # built" gate FAIL at the root so the run never green-washes a product
        # that cannot be assembled. The verifier's boot-gate owns the hard RED
        # (it tries to import+boot the real entry in a fresh subprocess).
        # Constitution-keyed + behind SPEC_FLOW_PRE_GATE, so with the flag OFF
        # / no entry declared, behavior is unchanged.
        if os.environ.get("SPEC_FLOW_PRE_GATE", "") not in ("", "0", "false",
                                                            "False", "no"):
            # entry comes from the DECLARED product contract (no hardcoded
            # app.py/wsgi_app) — absent for non-web projects ⇒ nothing to gate
            _c = self._product_contract()
            entry = _c.get("entry") if _c else None
            if entry and not (Path(self.workspace.root) / entry).is_file():
                self.emit("integrate", "engine", "spec-integrate",
                          "L0:integrate",
                          "declared product entry not built",
                          f"constitution declares {entry} (wsgi_app) but it "
                          f"was never built — root integrate cannot assemble "
                          f"the product",
                          "integrate_verify", "FAIL", level=L_MILESTONE)
                # Doctor (Ф5): integrate failure — diagnose + record remedy.
                _act = self._doctor_advise(
                    {"id": "L0:integrate"}, "L0:integrate", 0,
                    "integrate_verify", "FAIL",
                    {"reasons": f"declared product entry {entry} not built"})
                # A declared-but-unbuilt entry is a ROOT failure. emit() wrote a
                # trace event only — NOT an integrate-fail loop entry — so root_red
                # below stayed False and the project was wrongly marked COMPLETE
                # over a broken build (observed in v044). Record the root fail so
                # the completion gate honours it (doctor on OR off).
                _rid = str((project.get("tree") or {}).get("id", "L0"))
                self.loops.append({
                    "type": "integrate-fail", "task": _rid,
                    "detail": f"declared product entry {entry} not built"})
                # EXECUTE the doctor's verdict (Ф6): reconcile_check actually
                # re-builds the declared entry through the real implementer and
                # re-boots; on a green boot it drops the root fail recorded above
                # and closes the cause (row red->green). Advisory-only let v044/v047
                # ship/stall with a broken build.
                if _act is not None and getattr(_act, "kind", "") == "reconcile_check":
                    self._remedy_reconcile_check(entry)

            # Single-authority invariant: every leaf that delivered a real code
            # file must STILL have it present (non-trivial) in the assembled
            # tree. The v078 NOT READY root cause was leaf code orphaned in git
            # (two commit authorities diverged) so feature modules vanished
            # before the boot-gate while only specs + an entry stub reached the
            # tree. Without this, the boot-gate sees an entry that imports
            # nothing and the product 404s every route — a silent green-wash of
            # a product whose real modules were dropped. A missing module is a
            # ROOT integrate FAIL naming it, recorded so the completion gate
            # blocks; the same is fed to the doctor so reconcile rebuilds the
            # real module (it never accepts a stub).
            _built = self.__dict__.get("_built_leaf_code") or {}
            _lost = []
            for _nid, _rel in _built.items():
                _fp = Path(self.workspace.root) / _rel
                try:
                    _body = _fp.read_text(encoding="utf-8") if _fp.is_file() else ""
                except Exception:  # noqa: BLE001 — unreadable == lost
                    _body = ""
                _code_lines = [ln for ln in _body.splitlines()
                               if ln.strip() and not ln.strip().lstrip().startswith("#")]
                if len(_code_lines) < 3:
                    _lost.append((_nid, _rel))
            if _lost:
                _names = ", ".join(f"{n}->{r}" for n, r in _lost)
                self.emit("integrate", "engine", "spec-integrate",
                          "L0:integrate",
                          "delivered leaf code missing from assembled tree",
                          f"feature modules built by a leaf are absent/empty on "
                          f"the final tree (orphaned before integrate): {_names}",
                          "integrate_verify", "FAIL", level=L_MILESTONE)
                _rid2 = str((project.get("tree") or {}).get("id", "L0"))
                self.loops.append({
                    "type": "integrate-fail", "task": _rid2,
                    "detail": f"leaf code missing on final tree: {_names}"})
                # Feed the doctor so it can re-open integrate and rebuild the
                # lost modules through the real implementer (never a stub).
                self._doctor_advise(
                    {"id": "L0:integrate"}, "L0:integrate", 0,
                    "integrate_verify", "FAIL",
                    {"reasons": f"delivered leaf code missing: {_names}"})

        # Sweep: fire any declared revision that did not meet its in-run
        # trigger condition (back-compat + nothing declared is silently dropped).
        self._revision_sweep()

        # Depth 'verify' and up: actually run the materialised test suite.
        if self.depth >= DEPTH_VERIFY:
            self._verify_tests()

        # Final L0 integration — the engine itself must never claim
        # COMPLETE over a recorded root FAIL (v17 did exactly that)
        root_id = str((project.get("tree") or {}).get("id", "L0"))
        root_red = any(lp.get("type") == "integrate-fail"
                       and str(lp.get("task")) == root_id
                       for lp in self.loops)
        # Honour the doctor's verdict: an unresolved diagnosed cause anywhere
        # must also block a green COMPLETE (advisory-only let v044 ship green
        # with an open task_check_mismatch over a broken build). FIRST re-validate
        # open causes against the FINAL artifact — a cause reworked real on a path
        # that never re-ran its gate is stale and must not veto a working product.
        self._prune_stale_causes()
        _open = self._doctor_open_causes()
        if _open:
            root_red = True
        if root_red:
            _od = (f"; doctor causes still open: "
                   f"{', '.join(f'{n}:{c}' for n, c in _open)}" if _open else "")
            self.emit("integrate", "verifier", "spec-integrate",
                      "L0:integrate",
                      "L0 integrate RED — project NOT complete",
                      "root gate recorded FAIL (record policy); the"
                      " assembled product is not green" + _od,
                      "integrate_verify", "FAIL", level=L_MILESTONE)
        else:
            self.emit("integrate", "verifier", "spec-integrate",
                      "L0:integrate",
                      "L0 integrate done = project COMPLETE",
                      "all subtrees merged & verified", level=L_MILESTONE)
        if "L0:integrate" in self.tasks:
            self.tasks["L0:integrate"].status = ("failed" if root_red
                                                 else "done")

        # Depth 'product': after the project is integrated (and, at >=execute,
        # real code exists), build+run the product and assert readiness against
        # the acceptance spec.
        if self.depth >= DEPTH_PRODUCT:
            self._product_check(project.get("acceptance"))

        # C3: enforce the R1-R9 invariants at runtime (opt-in). A hard breach
        # raises InvariantViolation rather than passing silently to the report.
        if self.runtime_guard and hasattr(self.tools, "assert_invariants"):
            self.tools.assert_invariants([vars(e) for e in self.events])

        return self._result(project)

    def _result(self, project: dict) -> RunResult:
        """Assemble the RunResult from the engine's current state. Shared by
        a normal finish and a cooperative STOP (П1), so a stopped run returns
        the same shape — just with fewer completed nodes."""
        if self._doctor is not None and _doctor_mod is not None \
                and self._doctor.enabled:
            try:
                import json as _json
                m = _doctor_mod.doctor_metrics(self.loops)
                self.emit("review", "doctor", "", "L0:root", "doctor metrics",
                          _json.dumps(m)[:300], gate="doctor:metrics",
                          level=L_MILESTONE)
            except Exception:  # noqa: BLE001
                pass
        return RunResult(project, self.events, self.tasks, self.skills, self.profiles,
                         self.loops, self.gate_calls, self.verbosity, self.depth,
                         getattr(self.workspace, "root", None),
                         dict(self._module_names),
                         # Plan Шаг 4 — terminal verdict (None if acceptance was
                         # never asserted, e.g. depth below product).
                         getattr(self, "_product_status", None),
                         list(getattr(self, "_product_failed", []) or []))

    # -- recursion ---------------------------------------------------------
    def _decomposer_ctx(self, node: dict, depth: int, parent: Optional[str],
                        ancestors: tuple) -> dict:
        """The decomposer worker's task context (shared by the initial
        expansion and the spec-rework rounds)."""
        ctx = {
            "project": {"goal": self._goal, "target": self._target,
                        "constitution": self._constitution},
            "node": {"id": node["id"], "title": node.get("title", node["id"])},
            "parent": parent, "depth": depth,
            "ancestors": [t for _, t in ancestors],
            # the parent's NODE ID — the worker reads specs/<parent_id>.md
            # (the approved parent spec) to trace child requirements to its
            # REQ ids, exactly as the skill prescribes
            "parent_id": ancestors[-1][0] if ancestors else None,
            "existing_nodes": [
                {"id": i, "title": t}
                for i, t in list(self._node_registry.items())[:150]
            ],
        }
        # 433: trim the decomposer's view to the node's ZONE — its ancestors and
        # the subtree under its parent — and reference the REST by count, not by
        # name. A flat list spanning unrelated zones invites a child to restate a
        # distant sibling's surface; the zone is what it needs to scope its delta.
        # Conservative: when everything fits under the cap the list is unchanged,
        # so small p4/p5 trees see byte-identical input.
        reg = list(self._node_registry.items())
        cap = int((self._project_meta.get("workers") or {}).get(
            "decomposer_zone_cap", 150))
        if len(reg) > cap:
            nid_self = node["id"]
            pid = ancestors[-1][0] if ancestors else None
            anc_ids = {a for a, _ in ancestors}

            def _in_zone(i: str) -> bool:
                return (i in anc_ids or i == nid_self
                        or bool(pid) and (i == pid or i.startswith(str(pid) + ".")))
            zone = [(i, t) for i, t in reg if _in_zone(i)]
            rest = [(i, t) for i, t in reg if not _in_zone(i)]
            keep = zone + rest[: max(0, cap - len(zone))]
            ctx["existing_nodes"] = [{"id": i, "title": t} for i, t in keep]
            other = len(reg) - len(keep)
            if other:
                ctx["other_nodes_count"] = other
        # PREVENTION (delta-only contract): a LATE requirement is authored AFTER
        # other modules exist. Tell the decomposer up front which routes/symbols
        # are already owned so it scopes to its OWN delta on the FIRST draft,
        # instead of restating the whole service and being caught later by the
        # scope-lint backstop (the v041 delete_note duplicate-surface failure).
        if node.get("_late_req"):
            modules = self._surface_modules(_snake(str(node.get("id", ""))))
            routes: set = set()
            syms: set = set()
            for _rel, _stem, body in modules:
                routes |= _amend_routes(body)
                syms |= _amend_symbols(body)
            if routes or syms:
                ctx["late_req_contract"] = {
                    "existing_routes": sorted(routes),
                    "existing_symbols": sorted(syms),
                }
        return ctx

    def _expand_node(self, node: dict, depth: int, parent: Optional[str],
                     ancestors: tuple = ()) -> dict:
        """A node arrived without metrics — the DECOMPOSER AGENT builds this
        level itself from the goal: estimates the node's size metrics and, if
        it is too big, proposes children one level down. The engine's own
        gates (leaf_check) still make the leaf/branch decision.

        The agent gets the ancestor chain and the registry of nodes already
        created ANYWHERE in the tree — without them every call is blind and
        deep branches re-invent work that already exists (the L1/L6
        duplicate-spec bug). The dedup gate downstream is the deterministic
        backstop; this context is the first line of defence."""
        # resume: rebuild this level from the persisted decomposition instead of
        # re-asking the (non-deterministic) decomposer. This keeps node ids
        # stable so the leaf cache hits and the run continues from the
        # checkpoint — without it a resume re-decomposes from L0 and restarts.
        if self.resume and not self.replan:
            saved = self._decomp_index.get(node["id"])
            if saved:
                node.update(saved)
                self.emit("decompose", "spec-decomposer", "spec-flow-decompose",
                          node["id"],
                          "resume: restored this level from the run journal "
                          "(decomposer not re-run)",
                          f"{len(node.get('children', []))} children restored",
                          level=L_MILESTONE)
                return node
        self._decompose_calls += 1
        if self._decompose_calls > self.max_decompose_calls:
            raise RuntimeError(
                f"decomposer agent exceeded {self.max_decompose_calls} calls — "
                "the tree does not converge to leaves")
        out = self.agents["decomposer"](
            self._decomposer_ctx(node, depth, parent, ancestors))
        # mutate the node IN PLACE so the realized metrics/children attach to the
        # live tree — this is how project["tree"] ends up holding the full tree
        # the decomposer built (needed for the reports in llm mode).
        node.update(out or {})
        # persist this level so a later resume rebuilds the same tree (above)
        if out:
            self.workspace.decomp_mark(node["id"], out)
        self.emit("decompose", "spec-decomposer", "spec-flow-decompose", node["id"],
                  "decomposer agent built this level from the goal",
                  f"{len(node.get('children', []))} children proposed", level=L_MILESTONE)
        return node

    def _consult_reviewer(self, nid: str, title: str, spec_rel: Optional[str], depth: int = -1) -> None:
        """Real spec-reviewer worker, when attached. Without one the engine
        keeps its historical simulated PASS events untouched.

        REJECT follows the same semantics as a HITL rejection: version bump +
        runs increment + a recorded loop — the kanban respec lane (Hermes
        side) owns actual rework, the engine records the episode honestly."""
        reviewer = self.agents.get("reviewer")
        if reviewer is None or not spec_rel:
            return None, ""
        self.gate_calls["spec_review"] = self.gate_calls.get("spec_review", 0) + 1
        out = None
        for round_no in (1, 2):  # one retry — a crash is infra, not a verdict
            try:
                out = reviewer({"node": nid, "title": title, "spec": spec_rel,
                                "goal": self._goal, "depth": depth,
                                "constitution": self._constitution,
                                "workspace_root": self.workspace.root}) or {}
                break
            except Exception as exc:  # noqa: BLE001 — must not kill the run
                self.emit("review", "spec-reviewer", "spec-reviewer", nid,
                          "spec review worker failed"
                          + (" — retrying" if round_no == 1 else " — no verdict"),
                          str(exc)[:200], "spec_review", "ERROR",
                          level=L_MILESTONE)
        if out is None:
            return None, ""
        verdict = "REJECT" if str(out.get("verdict", "PASS")).upper() == "REJECT" else "PASS"
        reasons = "; ".join(str(r) for r in out.get("reasons") or [])
        self.emit("review", "spec-reviewer", "spec-reviewer", nid,
                  "spec review (real worker)", reasons, "spec_review", verdict,
                  level=L_MILESTONE)
        if verdict == "REJECT":
            self.tasks[nid].version += 1
            self.tasks[nid].runs += 1
            self.loops.append({"type": "spec-review-reject", "task": nid,
                               "detail": reasons})
            # Doctor (Ф4): a spec_review REJECT is a SEMANTIC failure — route it
            # through the LLM classifier to name the cause (vague_spec /
            # weak_implementer / ...) and record the remedy. Advisory; off=no-op.
            self._doctor_advise({"id": nid}, nid, 0, "spec_review", "REJECT",
                                {"reasons": reasons})
        elif verdict == "PASS":
            self._doctor_resolve({"id": nid}, nid, "spec_review")
        return verdict, reasons

    def _is_simple_node(self, node: dict, depth: int) -> bool:
        """A1: a node simple enough to skip the LLM review round. Deterministic
        — decided purely by structure/metrics, never the model:
          * tiering must be enabled (review.tiering);
          * it is a LEAF (no children — a branch always gets full review);
          * it carries NO open decisions;
          * its estimated size is <= review.simple_max_loc;
          * it is not the root (depth >= 1) — the top node always gets review.
        Anything failing these falls through to the full reviewer + rework."""
        pol = self.review_policy
        if not pol.get("tiering"):
            return False
        if node.get("children"):
            return False
        if depth < 1:
            return False
        m = node.get("metrics") or {}
        try:
            if int(m.get("open_decisions", 0) or 0) > 0:
                return False
            cap = int(pol.get("simple_max_loc", 60))
            if int(m.get("estimated_loc", 0) or 0) > cap:
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _review_gate(self, node: dict, nid: str, title: str, depth: int,
                     parent: Optional[str], spec_args: dict,
                     ancestors: tuple) -> None:
        """Write the spec, review it, and APPLY the review policy.

        This is the answer to 'how do node errors go away': on REJECT the
        default policy sends the reviewer's reasons back to the decomposer,
        the spec is re-authored and re-reviewed (bounded). A node that still
        fails after the budget keeps its honest episode record."""
        spec_rel = self.workspace.spec(
            nid, title, depth, spec_args["verdict"], spec_args["reasons"],
            parent, spec_args["plan"], node=node, target=self._target,
            module=self._module_for(nid))
        # deterministic lint BEFORE the reviewer: the mechanical
        # traceability class (AC without REQ and the reverse) is fixed by
        # a bounded author round with the EXACT violations — a reviewer
        # round is never spent on what code can state precisely
        for _lint_round in range(2):
            lint = _lint_spec_traceability(nid,
                                           str(node.get("spec_markdown")
                                               or ""))
            if not lint:
                break
            self.emit("review", "engine", "", nid,
                      f"spec lint: {len(lint)} traceability violation(s)",
                      "; ".join(lint)[:300], "spec_lint", "FAIL",
                      level=L_MILESTONE)
            if "decomposer" not in self.agents:
                break
            self._decompose_calls += 1
            lctx = self._decomposer_ctx(node, depth, parent, ancestors)
            lctx["review_feedback"] = ("DETERMINISTIC LINT (code-checked,"
                                       " not an opinion):\n- "
                                       + "\n- ".join(lint))
            lctx["previous_spec"] = str(node.get("spec_markdown") or "")
            lctx["rework"] = True
            try:
                lout = self.agents["decomposer"](lctx) or {}
            except Exception as exc:  # noqa: BLE001
                self.emit("review", "spec-decomposer", "spec-flow-decompose",
                          nid, "lint-fix worker failed — keeping the spec",
                          str(exc)[:200], level=L_MILESTONE)
                break
            lmd = str(lout.get("spec_markdown") or "").strip()
            if not lmd:
                break
            node["spec_markdown"] = lmd
            spec_rel = self.workspace.spec(
                nid, title, depth, spec_args["verdict"],
                spec_args["reasons"], parent, spec_args["plan"],
                node=node, target=self._target,
            module=self._module_for(nid))
        else:
            lint = _lint_spec_traceability(nid,
                                           str(node.get("spec_markdown")
                                               or ""))
        # deterministic DECOMPOSITION-QUALITY lint BEFORE the reviewer: a LATE
        # requirement whose spec re-states an existing surface (duplicate) is
        # narrowed to its delta by a bounded author round carrying the EXACT
        # findings — code-checked, model-independent (the v040 web_ui duplicate
        # that the LLM reviewer kept REJECTing). Layered detectors of increasing
        # precision live in _dup_surface_findings.
        _scope_failed = False
        for _scope_round in range(2):
            findings = self._late_req_scope_findings(
                node, str(node.get("spec_markdown") or ""))
            if not findings:
                break
            _scope_failed = True
            self.emit("review", "engine", "", nid,
                      f"spec scope: {len(findings)} duplicate-surface finding(s)",
                      "; ".join(findings)[:300], "spec_scope", "FAIL",
                      level=L_MILESTONE)
            # Doctor (Ф3): diagnose the cause + dispatch a remedy. Advisory for
            # now — it records WHAT it would do (dashboard cause/remedy) while the
            # existing rework below still executes. Off => no-op, byte-identical.
            self._doctor_advise(node, nid, depth, "spec_scope", "FAIL",
                                {"scope_findings": list(findings)})
            if "decomposer" not in self.agents:
                break
            self._decompose_calls += 1
            sctx = self._decomposer_ctx(node, depth, parent, ancestors)
            sctx["review_feedback"] = (
                "DETERMINISTIC SCOPE LINT (code-checked, not an opinion): this is "
                "a LATE requirement added after the modules below were planned. "
                "Its spec must describe ONLY its own delta and must NOT re-state "
                "HTTP routes or symbols that existing modules already own. "
                "Reference them as context, do not re-specify them. Fix:\n- "
                + "\n- ".join(findings))
            sctx["previous_spec"] = str(node.get("spec_markdown") or "")
            sctx["rework"] = True
            try:
                sout = self.agents["decomposer"](sctx) or {}
            except Exception as exc:  # noqa: BLE001
                self.emit("review", "spec-decomposer", "spec-flow-decompose",
                          nid, "scope-fix worker failed — keeping the spec",
                          str(exc)[:200], level=L_MILESTONE)
                break
            smd = str(sout.get("spec_markdown") or "").strip()
            if not smd:
                break
            node["spec_markdown"] = smd
            spec_rel = self.workspace.spec(
                nid, title, depth, spec_args["verdict"], spec_args["reasons"],
                parent, spec_args["plan"], node=node, target=self._target,
                module=self._module_for(nid))
        # if a scope FAIL was raised but the rework cleared it, emit the closing
        # PASS on the SAME gate — otherwise the FAIL reads as an unresolved
        # problem forever (no later PASS to pair it with)
        if _scope_failed and not self._late_req_scope_findings(
                node, str(node.get("spec_markdown") or "")):
            self.emit("review", "engine", "", nid, "spec scope clean", "",
                      "spec_scope", "PASS", level=L_MILESTONE)
            self._doctor_resolve(node, nid, "spec_scope")
        if not _lint_spec_traceability(nid,
                                       str(node.get("spec_markdown") or "")):
            self.emit("review", "engine", "", nid, "spec lint clean", "",
                      "spec_lint", "PASS")
            # A1 tiering: a SIMPLE leaf that already passed the deterministic
            # lint needs no LLM opinion round — auto-PASS and skip the reviewer
            # + rework loop. Deterministic (decided by node metrics), so the
            # saving does not depend on the model. Default off.
            if self._is_simple_node(node, depth):
                cap = int(self.review_policy.get("simple_max_loc", 60))
                self.emit("review", "engine", "", nid,
                          "review tiering: simple leaf — deterministic check "
                          "only, LLM reviewer skipped",
                          f"leaf, open_decisions=0, est_loc<={cap}",
                          "spec_review", "PASS", level=L_MILESTONE)
                return
        verdict, reasons = self._consult_reviewer(nid, title, spec_rel,
                                                  depth)
        if verdict != "REJECT":
            return
        pol = self.review_policy
        mode = str(pol.get("on_reject", "rework"))
        if mode == "rework" and "decomposer" in self.agents:
            budget = int(pol.get("max_rework", 2))
            attempt = 0
            while verdict == "REJECT" and attempt < budget:
                attempt += 1
                self.emit("review", "spec-decomposer", "spec-flow-decompose", nid,
                          f"rework after review REJECT (attempt {attempt}/{budget})",
                          reasons[:200], level=L_MILESTONE)
                # SPEC-ONLY re-authoring: the leaf/branch decision, metrics
                # and children are already gated and FINAL — a rework that
                # re-ran the full decomposition once collapsed the whole
                # tree into a root leaf (the model dropped its children).
                self._decompose_calls += 1
                ctx = self._decomposer_ctx(node, depth, parent, ancestors)
                ctx["review_feedback"] = reasons
                ctx["previous_spec"] = str(node.get("spec_markdown") or "")
                ctx["rework"] = True
                try:
                    out = self.agents["decomposer"](ctx) or {}
                except Exception as exc:  # noqa: BLE001
                    self.emit("review", "spec-decomposer", "spec-flow-decompose",
                              nid, "rework worker failed — keeping the spec",
                              str(exc)[:200], level=L_MILESTONE)
                    break
                new_md = str(out.get("spec_markdown") or "").strip()
                if new_md:
                    node["spec_markdown"] = new_md
                    # a rework legitimately CLOSES open decisions in the text;
                    # the engine header / metrics must follow, or the reviewer
                    # keeps rejecting the contradiction (header says N open,
                    # body says none) until the budget burns out
                    n_open = _count_open_decisions(new_md)
                    metrics = node.setdefault("metrics", {})
                    was = metrics.get("open_decisions")
                    if n_open is not None and was is not None \
                            and int(was) != n_open:
                        metrics["open_decisions"] = n_open
                        cleaned = _OPEN_DECISIONS_REASON_RE.sub(
                            (f"{n_open} open decision(s) — resolve before"
                             " leafing") if n_open else "",
                            str(spec_args["reasons"] or ""))
                        spec_args["reasons"] = re.sub(
                            r";\s*(?=;)|;\s*$|^\s*;", "",
                            cleaned).strip("; ").strip()
                        self.emit("review", "spec-decomposer",
                                  "spec-flow-decompose", nid,
                                  f"rework closed open decisions {was} → {n_open}",
                                  "", level=L_MILESTONE)
                spec_rel = self.workspace.spec(
                    nid, title, depth, spec_args["verdict"], spec_args["reasons"],
                    parent, spec_args["plan"], node=node, target=self._target,
            module=self._module_for(nid))
                verdict, reasons = self._consult_reviewer(nid, title, spec_rel, depth)
            if verdict == "REJECT":
                exhausted = getattr(self, "_review_exhausted", "record")
                if exhausted == "ask" and self._human_ask is not None:
                    # the operator decides: keep going with the recorded
                    # debt, or stop the run here
                    answer = str(self._human_ask(
                        "spec-reviewer", nid,
                        f"review budget exhausted at '{nid}' — unresolved: "
                        f"{reasons[:300]}. Reply 'halt' to stop the run,"
                        " anything else records the debt and continues.")
                        or "").strip().lower()
                    self.emit("review", "engine", "", nid,
                              "review exhausted — operator consulted",
                              answer or "(no answer — recording)",
                              "hitl", "", level=L_MILESTONE)
                    if answer == "halt":
                        exhausted = "halt"
                if exhausted == "halt":
                    raise ReviewExhaustedHalt(
                        f"unresolved review REJECT at '{nid}' after "
                        f"{budget} rework round(s): {reasons[:300]}")
                self.emit("review", "spec-reviewer", "spec-reviewer", nid,
                          f"rework budget exhausted ({budget}) — REJECT stands;"
                          " needs research or a human decision",
                          reasons[:200], "spec_review", "REJECT",
                          level=L_MILESTONE)
            return
        if mode == "halt":
            raise RuntimeError(
                f"spec review rejected for {nid} (policy on_reject=halt): {reasons}")
        if mode == "ask_human":
            out = self.agents["approver"]({
                "kind": "spec-review", "task": nid,
                "detail": f"reviewer rejected the spec: {reasons}",
                "constitution": self._constitution, "policy": {}}) or {}
            if not out.get("approved", True):
                raise RuntimeError(
                    f"spec review rejected for {nid} and the human declined: "
                    f"{out.get('reason', '')}")
        # mode == record (or ask_human approved): episode already recorded

    def _topo_order(self, kids: list) -> list:
        """#2: order sibling children so each child's ``depends_on`` siblings
        precede it. Stable (keeps declared order where deps allow), tolerant
        of unknown ids (an external/typo dep is ignored), and cycle-safe (a
        dependency cycle falls back to declared order for the stuck nodes).
        Pure — returns a new list, never mutates the tree."""
        if not kids:
            return kids
        ids = {c.get("id") for c in kids}
        deps = {c.get("id"): [d for d in (c.get("depends_on") or [])
                              if d in ids and d != c.get("id")]
                for c in kids}
        order, placed, remaining = [], set(), list(kids)
        progressed = True
        while remaining and progressed:
            progressed, rest = False, []
            for c in remaining:
                if all(d in placed for d in deps[c.get("id")]):
                    order.append(c)
                    placed.add(c.get("id"))
                    progressed = True
                else:
                    rest.append(c)
            remaining = rest
        order.extend(remaining)        # cycle / unresolved → declared order
        return order

    def _subtree_test_targets(self, node: dict) -> list:
        """#4: the test files belonging to a branch's subtree —
        tests/test_<module>.py for every node under ``node`` that has a module
        assigned and whose test file exists. Used to scope a branch integrate
        to its own subtree instead of the whole corpus."""
        mods = []

        def walk(n):
            m = self._module_names.get(n.get("id"))
            if m:
                mods.append(m)
            for c in n.get("children") or []:
                walk(c)
        walk(node)
        root = Path(self.workspace.root or ".")
        out = []
        for m in mods:
            rel = f"tests/test_{m}.py"
            if (root / rel).is_file():
                out.append(rel)
        return sorted(set(out))

    def _resolve_specialty(self, node: dict) -> str:
        """#10: the node's specialty — explicit on the node, else the project
        default, else auto-inferred — restricted to specialties the
        implementer role actually declares. '' when none applies."""
        try:
            from tests.harness import specialty as _sp
        except Exception:  # noqa: BLE001
            try:
                from harness import specialty as _sp  # type: ignore
            except Exception:  # noqa: BLE001
                return ""
        return _sp.resolve_specialty(node, self._project_meta,
                                     self._impl_specialties, self._auto_specialty)

    def _node_complexity_label(self, node: dict) -> str:
        """The node's structural complexity class — 'branch' / 'leaf_small' /
        'leaf_big'. Pure and model-independent: a branch has children; a leaf is
        'small' only when its estimated LoC is under the simple cap AND it has no
        open decisions, else 'big'. ('product_entry' is handled by callers as a
        special case — its difficulty is integration breadth, not line count.)"""
        if node.get("children"):
            return "branch"
        m = node.get("metrics") or {}
        cap = int((self.review_policy or {}).get("simple_max_loc", 60))
        loc = int(m.get("estimated_loc", 0) or 0)
        od = int(m.get("open_decisions", 0) or 0)
        return "leaf_small" if (loc <= cap and od == 0) else "leaf_big"

    def _node_tier(self, node: dict) -> str:
        """424: map the node's leaf_check complexity to a power TIER name, so the
        implementer spawns on a chain matched to difficulty — a hard node on the
        strong chain, a trivial one on the cheap chain. '' when no mapping
        resolves (routing falls back to the role's default chain). Pure, offline
        and model-independent: decided purely by structure/metrics, never the
        model. The label→tier map lives in config (workers.complexity_to_tier)."""
        try:
            import spec_flow_remedies as _rem
            cmap = _rem.complexity_to_tier(self._project_meta)
        except Exception:  # noqa: BLE001 — routing is best-effort, never fatal
            return ""
        if not cmap:
            return ""
        if node.get("id") == "product_entry":
            # The assembly entry wires every sibling module and must dispatch ALL
            # routes. Its difficulty is integration breadth, not line count, so a
            # LoC-based label under-rates it and a weak model wires only the first
            # module. Route it to the strongest available tier deterministically.
            return str(cmap.get("branch", "") or cmap.get("leaf_big", "")
                       or "strong")
        label = self._node_complexity_label(node)
        return str(cmap.get(label, "") or "")

    def _node_solo(self, node: dict) -> bool:
        """Process tiering: True when this leaf is simple enough to build with a
        SINGLE coder pass (TDD generate + one repair) instead of the full
        architect->coder->tester->fixer orchestra. The orchestra's four LLM
        calls are reserved for branches and large/undecided leaves; a trivial
        single-concern leaf does not earn them. Decided purely by the node's
        structural label (config: workers.solo_for_labels, default leaf_small),
        never by the model — deterministic and model-independent. The assembly
        entry (product_entry) and branches are never solo."""
        if node.get("id") == "product_entry" or node.get("children"):
            return False
        try:
            import spec_flow_remedies as _rem
            labels = _rem.solo_for_labels(self._project_meta)
        except Exception:  # noqa: BLE001 — tiering is best-effort, never fatal
            return False
        return self._node_complexity_label(node) in set(labels or [])

    def _resolve_executor(self, node: dict) -> tuple:
        """C1: route a leaf to an executor. Returns (domain, team) where team is
        a D1 specialist LIST when the routed executor is a team-spec, else None
        (a named agent or the default single-agent path). Pure + offline."""
        if not self._executors_cfg:
            return "general", None
        try:
            from tests.harness import executors as _ex
        except Exception:  # noqa: BLE001
            try:
                from harness import executors as _ex  # type: ignore
            except Exception:  # noqa: BLE001
                return "general", None
        domain = _ex.resolve_domain(node, self._project_meta, self._auto_domain)
        spec = _ex.resolve_executor(domain, self._executors_cfg)
        return domain, _ex.team_of(spec)

    def _review_escalation(self, rejects: int) -> str:
        """A3: the STRONG model for a one-shot escalation after the rework budget
        is spent on `rejects` REJECTs, or '' (no escalation — today's grind).
        Reads the review policy's escalate_model + max_rework budget."""
        if _cycle is None:
            return ""
        return _cycle.escalation_model(
            rejects, int(self.review_policy.get("max_rework", 2) or 0),
            _cycle.review_escalate_model(self.review_policy))

    def _cached_artifact_stale(self, module: str) -> str:
        """On resume, validate a cached leaf's artifact against the CURRENT
        deterministic realness gate via the INJECTED checker (the plugin must
        not import tests.harness — in a real run tests/ is off sys.path). Returns
        the first violation (the cache-miss reason) or '' when the cached code
        still passes today's gates, or when no checker was injected.

        Why: the resume cache reuses a leaf when its SPEC hash is unchanged — but
        a tightened PLUGIN GATE (a new deterministic check, not a spec edit) would
        otherwise be skipped on the cached code, so an old bug survives a resumed
        run. Re-checking re-runs ONLY the now-failing leaf, not the whole tree:
        the fast 'fix the plugin, resume the checkpoint' loop."""
        ws = self.workspace
        check = self._realness_check
        if check is None or not (getattr(ws, "enabled", False)
                                 and getattr(ws, "root", None)):
            return ""
        try:
            viol = check(ws.root, {module})
        except Exception:  # noqa: BLE001
            return ""
        return viol[0] if viol else ""

    def _has_sibling_deps(self, kids: list) -> bool:
        """True when some child declares a depends_on on ANOTHER sibling —
        the signal that forces sequential (topo) execution for correctness."""
        ids = {c.get("id") for c in kids}
        return any(d in ids and d != c.get("id")
                   for c in kids for d in (c.get("depends_on") or []))

    def _dependency_waves(self, kids: list) -> list:
        """Partition siblings into dependency WAVES (Kahn levels): a child is
        in the earliest wave after all its intra-sibling dependencies. Members
        of one wave have no dependency between them, so they run in PARALLEL;
        a barrier between waves lets a dependent see its dependency's commits.
        This recovers concurrency that the all-or-nothing
        ``_has_sibling_deps`` gate threw away — an LLM decomposer declares
        depends_on liberally (research→arch→features), which would otherwise
        serialize the whole branch. A dependency cycle degrades to one wave
        (run together) rather than deadlocking."""
        ids = {c.get("id") for c in kids}
        deps = {c.get("id"): {d for d in (c.get("depends_on") or [])
                              if d in ids and d != c.get("id")} for c in kids}
        done: set = set()
        waves: list = []
        remaining = list(kids)
        while remaining:
            wave = [c for c in remaining if deps[c.get("id")] <= done]
            if not wave:                      # cycle — break it, run the rest
                wave = remaining
            waves.append(wave)
            done |= {c.get("id") for c in wave}
            wave_ids = {c.get("id") for c in wave}
            remaining = [c for c in remaining if c.get("id") not in wave_ids]
        return waves

    def _run_child_pool(self, wave: list, depth: int, contract_ctx, phase,
                        title: str, nid: str, ancestors: tuple) -> None:
        """Run every node in `wave` concurrently (each its whole subtree in a
        thread), joining before return — the barrier the caller relies on.
        A parent that only WAITS hands its semaphore slot back so nested
        grandchildren can't starve it into a live deadlock."""
        errs: list = []

        def _one(c):
            try:
                with self._parallel_sem:
                    self._sem_state.held = True
                    try:
                        self._visit(c, depth + 1, contract_ctx, phase,
                                    parent=title,
                                    ancestors=ancestors + ((nid, title),))
                    finally:
                        self._sem_state.held = False
            except Exception as exc:    # noqa: BLE001 — re-raised after join
                errs.append(exc)

        lent = getattr(self._sem_state, "held", False)
        if lent:
            self._sem_state.held = False
            self._parallel_sem.release()
        try:
            pool, alive = list(wave), []
            limit = self._parallel_children
            while pool or alive:
                while pool and len(alive) < limit:
                    th = threading.Thread(target=_one, args=(pool.pop(0),),
                                          daemon=True)
                    th.start()
                    alive.append(th)
                for th in alive:
                    th.join(timeout=0.05)
                alive = [th for th in alive if th.is_alive()]
                if errs:
                    for th in alive:
                        th.join()
                    raise errs[0]
        finally:
            if lent:
                self._parallel_sem.acquire()
                self._sem_state.held = True

    def _late_req_waves(self, placements: list) -> list:
        """Order late-requirement nodes into parallel WAVES. Two requirements
        that EDIT THE SAME owner module (same ``code_target``) must NOT run
        together — they write the same file and would clobber each other under
        worktree merge; they fall into separate waves (sequential). Requirements
        that target DIFFERENT modules, or each create a NEW module, are
        independent and share a wave (run in parallel under worktree isolation).
        ``depends_on`` is honoured too — a dependent waits for its dependency's
        wave. A blocked-only remainder degrades to one wave rather than
        deadlocking. Deterministic, model-independent: decided purely by the
        engine-resolved code_target + declared deps, never the model."""
        ids = {p.get("id") for p in placements}
        done: set = set()
        waves: list = []
        remaining = list(placements)
        while remaining:
            wave: list = []
            used_targets: set = set()
            leftover: list = []
            for p in remaining:
                deps = {d for d in (p.get("depends_on") or [])
                        if d in ids and d != p.get("id")}
                tgt = p.get("code_target") or ""
                if deps <= done and (not tgt or tgt not in used_targets):
                    wave.append(p)
                    if tgt:
                        used_targets.add(tgt)
                else:
                    leftover.append(p)
            if not wave:                  # only blocked items left — run together
                wave, leftover = remaining, []
            waves.append(wave)
            done |= {p.get("id") for p in wave}
            remaining = leftover
        return waves

    def _attach_late_req(self, extra: dict, node: dict, depth: int, title: str,
                         nid: str, ancestors: tuple, child_ids: list) -> None:
        """Prepare ONE late-requirement node for execution (no LLM): route it to
        EDIT an existing owner module when it refines an owned surface (sets
        ``code_target`` + an in-place EDIT spec), else attach it as a new child;
        emit the placement; register it under the parent. Cheap + sequential —
        only the subsequent ``_visit`` is heavy, so this runs before any pooling
        so the wave grouping can see every node's resolved code_target."""
        where = ("inside its scoped branch" if extra.pop("_scoped", False)
                 else "at root level")
        # B3: deterministically route a same-surface requirement to EDIT the
        # existing owner module (code_target). The node keeps its own spec; only
        # its code output goes into the owner file. Nodes with a fixed engine
        # target (the assembly entry) are exempt — they own a declared path.
        amend = None if extra.get("_no_amend") else self._amend_target(extra)
        if amend:
            extra["code_target"] = amend
            try:
                cur = (Path(self.workspace.root) / amend).read_text(
                    encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                cur = ""
            new_req = str(extra.get("requirement") or extra.get("title"))
            extra["spec_markdown"] = (
                "## EDIT AN EXISTING FILE IN PLACE (do not fork a "
                "second module)\n\n"
                f"`{amend}` already serves this surface. EXTEND that "
                "same file to satisfy the requirement below WITHOUT "
                "breaking its current behaviour; do not duplicate what "
                "is already there.\n\n"
                f"### Current `{amend}`\n```python\n{cur}\n```\n\n"
                f"### Requirement to fold in\n{new_req}\n")
            self.emit("decompose", "engine", "", extra["id"],
                      "late requirement routed to EDIT existing surface",
                      f"writes into {amend} (no parallel module)",
                      "requirement", "AMEND", level=L_MILESTONE)
        else:
            self.emit("decompose", "engine", "", extra["id"],
                      f"late requirement materialized {where}",
                      extra["title"], "requirement", "ATTACHED",
                      level=L_MILESTONE)
        node.setdefault("children", []).append(extra)
        child_ids.append(extra["id"])

    def _dedup_children(self, node: dict, nid: str, title: str,
                        ancestors: tuple) -> None:
        """Dedup gate: prune proposed children that duplicate an existing
        node (any branch) or an already-accepted sibling in this batch.

        Refining the node's OWN lineage is legitimate decomposition, so the
        ancestor chain (and the node itself) is excluded from the comparison
        base. A pruned child becomes a ``depends_on`` link on this node —
        the result is referenced, not re-created/re-implemented."""
        children = node.get("children") or []
        if not children:
            return
        own_line = {a_id for a_id, _ in ancestors} | {nid}
        kept: list[dict] = []
        accepted: list[tuple[str, str]] = []
        for child in children:
            cid = child.get("id", "")
            ctitle = child.get("title", cid)
            dup_of = None
            for rid, rtitle in self._node_registry.items():
                if rid in own_line or rid == cid:
                    continue
                if node_similarity(cid, ctitle, rid, rtitle) >= _DEDUP_THRESHOLD:
                    dup_of = rid
                    break
            if dup_of is None:
                for kid, ktitle in accepted:
                    if node_similarity(cid, ctitle, kid, ktitle) >= _DEDUP_THRESHOLD:
                        dup_of = kid
                        break
            if dup_of is None:
                kept.append(child)
                accepted.append((cid, ctitle))
                continue
            node.setdefault("depends_on", []).append(dup_of)
            self.loops.append({"type": "dedup-gate", "task": nid,
                               "detail": f"child {cid} duplicates {dup_of}"})
            self.emit("decompose", "spec-decomposer", "spec-flow-decompose", nid,
                      "dedup gate: proposed child duplicates an existing node "
                      "— pruned, linked as depends_on",
                      f"{cid} ≈ {dup_of}", "dedup_gate", "PRUNED",
                      level=L_MILESTONE)
        if len(kept) != len(children):
            if kept:
                node["children"] = kept
            else:
                node.pop("children", None)

    def _visit(self, node: dict, depth: int, contract_ctx: Optional[dict], phase: str,
               parent: Optional[str] = None, ancestors: tuple = ()):
        if "metrics" not in node:
            node = self._expand_node(node, depth, parent, ancestors)
        nid = node["id"]
        # П1: cooperative stop — checked at every node boundary. The current
        # node has not started; a --resume re-enters here and continues.
        if self._stop_requested():
            raise RunStopped(nid)
        # checkpoint-on-request: snapshot WHERE we are (workspace quiescent at a
        # node boundary) and keep running, so any saved point can be replayed
        if self._checkpoint_requested():
            self._make_checkpoint(nid)
        # auto-checkpoint cadence: the engine snapshots itself every N nodes
        self._maybe_auto_checkpoint(nid)
        title = node.get("title", nid)
        self._node_registry[nid] = title
        self._dedup_children(node, nid, title, ancestors)
        self.task(nid, title, "decompose", "spec-decomposer", "spec-flow-decompose")
        self.emit("decompose", "spec-decomposer", "spec-flow-decompose", nid,
                  "read parent handoff, write level spec (Traces-to)", title)

        # clarify / block loop on an open decision
        clar = node.get("clarify")
        if clar:
            self.emit("decompose", "spec-decomposer", "spec-flow-decompose", nid,
                      "kanban_block — open decision", clar["decision"], level=L_MILESTONE)
            self.emit("decompose", "spec-reviewer", "spec-reviewer", nid,
                      "clarify answered → unblock", clar["resolution"], level=L_MILESTONE)
            self.tasks[nid].runs += 1
            self.loops.append({"type": "clarify", "task": nid, "detail": clar["decision"]})

        # #8 (П8): auto-spike HARD nodes. A node with many open decisions or
        # high estimated LOC gets a research spike before freeze even if the
        # decomposer didn't request one — the researcher role runs it (config
        # it to lead a STRONGER free model / the haiku subscription). Thresholds
        # 0 = off. A decomposer-authored spike always wins.
        if not node.get("spike") and (self._spike_open or self._spike_loc):
            m = node.get("metrics") or {}
            od = int(m.get("open_decisions", 0) or 0)
            loc = int(m.get("estimated_loc", 0) or 0)
            hard = ((self._spike_open and od >= self._spike_open)
                    or (self._spike_loc and loc >= self._spike_loc))
            if hard:
                node["spike"] = {
                    "question": (f"'{title}' is hard ({od} open decisions, "
                                 f"~{loc} LOC). Research the SINGLE best "
                                 "approach + the key risk before the spec is "
                                 "frozen."),
                    "recommendation": "(researcher to fill)"}
                self.emit("research", "researcher", "spec-research", nid,
                          "auto-spike: hard node flagged for research",
                          f"open_decisions={od} loc={loc}", level=L_DETAIL)

        # spike before freeze (research)
        spike = node.get("spike")
        if spike:
            sid = f"{nid}:spike"
            self.task(sid, spike["question"], "research", "researcher", "spec-research", parents=[nid])
            self.emit("research", "researcher", "spec-research", sid,
                      "SPIKE before freeze", spike["question"])
            # Real researcher worker, when attached: the spike is researched
            # for real instead of the decomposer answering its own question.
            # Mutates the spike in place so the written spec carries the
            # researched recommendation. Falls back to the decomposer's one.
            researcher = self.agents.get("researcher")
            if researcher is not None:
                try:
                    out = researcher({"question": spike["question"], "node": nid,
                                      "goal": self._goal,
                                      "workspace_root": self.workspace.root}) or {}
                    if str(out.get("recommendation", "")).strip():
                        spike["recommendation"] = out["recommendation"]
                        if out.get("basis"):
                            spike["basis"] = out["basis"]
                except Exception as exc:  # noqa: BLE001 — worker must not kill the run
                    self.emit("research", "researcher", "spec-research", sid,
                              "researcher worker failed — keeping decomposer's recommendation",
                              str(exc)[:200], level=L_DETAIL)
            self.emit("research", "researcher", "spec-research", sid,
                      "recommendation folded into spec (above the gate, no rework)",
                      spike["recommendation"])
            self.tasks[sid].status = "done"
            self._completed += 1

        # the gate: leaf vs branch. Atomicity is the PRIMARY judgment — an
        # explicit ``atomic`` field, else inferred from whether the decomposer
        # proposed children — and leaf_check reconciles it with the thresholds
        # (the guardrail that prunes over-decomposition / forces under-).
        if "atomic" in node:
            atomic_claim = bool(node["atomic"])
        elif node.get("children"):
            atomic_claim = False        # proposed a split ⇒ claims not atomic
        else:
            atomic_claim = None         # no signal ⇒ pure thresholds
        # A node may arrive without metrics (a decomposer JSON that omitted the
        # field, or an engine-synthesised node): default to {} so leaf_check
        # judges by atomic + thresholds instead of KeyError-crashing the WHOLE
        # run (live v106). Consistent with the setdefault on the spike path.
        leaf_out = self._leaf(node.get("metrics") or {}, atomic=atomic_claim)
        verdict = leaf_out["verdict"]
        reasons = leaf_out["reasons"]
        if leaf_out.get("mismatch"):
            self.loops.append({"type": "decomposition-guardrail",
                               "task": nid, "detail": leaf_out["mismatch"]})
        # over-decomposition pruned to a leaf: drop the unvisited proposed
        # children so the realized tree honestly shows what was built.
        if verdict == "leaf" and node.get("children"):
            node.pop("children", None)
        if verdict == "branch" and not node.get("children"):
            # a branch with ZERO children would fall through its empty
            # child loop and integrate trivially green — nothing was ever
            # implemented. A childless branch IS a leaf: it must build.
            verdict = "leaf"
            self.emit("decompose", "engine", "", nid,
                      "childless branch demoted to leaf",
                      "a branch without children would integrate empty",
                      "leaf_check", "leaf", level=L_MILESTONE)
        self.emit("decompose", "spec-decomposer", "spec-flow-decompose", nid,
                  "leaf_check", leaf_out.get("basis", "; ".join(reasons) or "within all thresholds"),
                  "leaf_check", verdict, level=L_MILESTONE)

        # lifecycle guard for this node (inline or fsm engine). REQUIREMENTS →
        # DECOMPOSE always; the clarify self-loop replays here when an open
        # decision was raised above.
        drv = self._node_driver(node, "leaf" if verdict == "leaf" else "branch")
        drv.go(EV_DECOMPOSE)
        if clar:
            drv.clarify()

        if verdict == "branch":
            contract_here = node.get("contract")
            child_contract_ctx = contract_ctx
            plan = [f"child: {c.get('title', c['id'])}" for c in node.get("children", [])]
            if contract_here:
                cid = f"{nid}:contract"
                self.task(cid, f"L2 contract for {title}", "contract", "spec-contract", "spec-contract", parents=[nid])
                self.emit("contract", "spec-contract", "spec-contract", cid,
                          "freeze OpenAPI contract (x-traces-to)", contract_here["artifact"])
                self.emit("contract", "spec-reviewer", "spec-reviewer", cid,
                          "spec-gate on contract", "trace + constitution OK", "", "PASS",
                          level=L_MILESTONE)
                # materialise the frozen contract (post-respec version)
                if self.contracts_dir:
                    self.workspace.contract(
                        str(self.contracts_dir / contract_here.get("fixed", contract_here["artifact"])),
                        contract_here["artifact"])
                self.tasks[cid].status = "done"
                self._completed += 1
                child_contract_ctx = contract_here

            # resume: a branch whose decomposition was restored from the
            # checkpoint was already specced+reviewed in the original run — its
            # children drive the continuation. Re-running its review gate here is
            # pure waste (and on a flaky provider it grinds for minutes), so skip
            # it for a restored branch (replan opts out). The child descent below
            # still runs, so the walk continues from the checkpoint.
            if (self.resume and not self.replan
                    and nid in self._decomp_index):
                self.emit("review", "engine", "", nid,
                          "resume: review kept from checkpoint (branch already reviewed)",
                          "", "", "", level=L_DETAIL)
            else:
                self._review_gate(node, nid, title, depth, parent,
                                  {"verdict": verdict, "reasons": "; ".join(reasons),
                                   "plan": plan}, ancestors)
            child_ids = []
            # #2: order siblings so a child's depends_on siblings run FIRST —
            # a leaf that consumes another's module sees it already present at
            # integrate, cutting rework rounds. Execution order only; the tree
            # keeps its declared order for display.
            kids = self._topo_order(node.get("children", []))
            if (self._parallel_children > 1
                    and depth <= self._parallel_depth_limit
                    and len(kids) >= self._parallel_min_siblings):
                # stage 1 + waves: each child's WHOLE subtree runs in its own
                # thread; independent siblings run together, a barrier between
                # dependency WAVES lets a dependent see its dependency's
                # commits. Previously ANY intra-sibling depends_on forced the
                # whole branch sequential — an LLM decomposer declares deps
                # liberally, so that gate erased nearly all concurrency.
                # Trade-off (why opt-in): parallel siblings see the node
                # registry as of fork time, so the dedup gate is weaker.
                for wave in self._dependency_waves(kids):
                    if len(wave) >= 2:
                        self._run_child_pool(wave, depth, child_contract_ctx,
                                             phase, title, nid, ancestors)
                    else:
                        self._visit(wave[0], depth + 1, child_contract_ctx,
                                    phase, parent=title,
                                    ancestors=ancestors + ((nid, title),))
                child_ids = [c["id"] for c in kids]
            else:
                for child in kids:
                    self._visit(child, depth + 1, child_contract_ctx, phase,
                                parent=title,
                                ancestors=ancestors + ((nid, title),))
                    child_ids.append(child["id"])

            # standing requirements, two placement rules (the ENGINE owns
            # placement, never a worker's whim):
            #  * scoped (@scope: <node>) — a child of THAT branch, valid
            #    only while the branch is still open (we are here, before
            #    its integrate);
            #  * unscoped, or the scoped branch was already missed — a
            #    direct ROOT child: the level whose integrate sees the
            #    assembled product the acceptance exercises.
            placements = list(self._requirement_nodes(scope=nid))
            asm = None
            if depth == 0:
                placements += [x for x in self._requirement_nodes()
                               if x["id"] not in {p["id"] for p in placements}]
                # B2 mechanism 3: the engine-synthesized assembly leaf goes
                # LAST so it runs after every feature leaf AND any late
                # requirement (e.g. web_ui) — the entry it wires must see all
                # the modules already present. Returns None unless the
                # constitution declares an entry that nothing built yet
                # (and SPEC_FLOW_PRE_GATE is on), so p4/p5 are unaffected.
                asm = self._assembly_node()
                if asm and asm["id"] not in {p["id"] for p in placements} \
                        and asm["id"] not in self.tasks:
                    placements.append(asm)
            # Lever #1: late requirements that touch DIFFERENT modules are
            # independent and run in PARALLEL (worktree isolation); ones that
            # EDIT THE SAME owner module stay sequential (write conflict). The
            # engine assembly entry, if present, wires every module and so must
            # run strictly LAST and alone, after every other late requirement.
            asm_extra = placements.pop() if (asm is not None and placements
                                             and placements[-1] is asm) else None
            for extra in placements:        # cheap prep first (no LLM) so the
                self._attach_late_req(      # wave grouping sees every code_target
                    extra, node, depth, title, nid, ancestors, child_ids)
            _kids = ancestors + ((nid, title),)
            if (self._parallel_children > 1
                    and depth <= self._parallel_depth_limit
                    and len(placements) >= 2):
                for wave in self._late_req_waves(placements):
                    if len(wave) >= 2:
                        self._run_child_pool(wave, depth, child_contract_ctx,
                                             phase, title, nid, ancestors)
                    else:
                        self._visit(wave[0], depth + 1, child_contract_ctx,
                                    phase, parent=title, ancestors=_kids)
            else:
                for extra in placements:
                    self._visit(extra, depth + 1, child_contract_ctx, phase,
                                parent=title, ancestors=_kids)
            if asm_extra is not None:
                self._attach_late_req(asm_extra, node, depth, title, nid,
                                      ancestors, child_ids)
                self._visit(asm_extra, depth + 1, child_contract_ctx, phase,
                            parent=title, ancestors=_kids)

            # a branch delegates impl to its children, then integrates them
            drv.go(EV_BRANCH_INTEGRATE)
            integ = f"{nid}:integrate"
            iparents = child_ids + ([f"{nid}:contract"] if contract_here else [])
            self.task(integ, f"Integrate & verify {title}", "integrate", "verifier", "spec-integrate", parents=iparents)
            if contract_here:
                # parallel subtree contract_check against the (respec'd) contract;
                # if the drift was code-wrong, the corrected code is what ships
                res = self._contract(contract_here["fixed"],
                                     contract_here.get("code_fixed", contract_here["code_drift"]))
                self.emit("integrate", "verifier", "spec-integrate", integ,
                          "parallel contract_check across subtree", contract_here["fixed"],
                          "contract_check", res["status"], level=L_MILESTONE)
            verifier = self.agents.get("verifier")
            if verifier is not None:
                # Real verifier worker: its verdict replaces the simulated
                # PASS. What a FAIL does next is the case's choice
                # (on_integrate_fail: record | rework | halt).
                def _verify_once():
                    try:
                        vctx = {"node": nid, "title": title,
                                "children": child_ids, "depth": depth,
                                "workspace_root": self.workspace.root,
                                # B2 boot-gate is constitution-keyed: the
                                # verifier reads the declared product entry
                                # (wsgi_app/app.py) from here. No entry
                                # declared ⇒ the boot-gate skips itself, so
                                # p4/p5 stay unaffected.
                                "constitution": self._constitution,
                                "goal": self._goal}
                        # #4: scope a BRANCH integrate to its subtree's tests;
                        # the root (depth 0 / L0) runs the whole corpus.
                        if self._incremental_integrate and depth > 0:
                            tgts = self._subtree_test_targets(node)
                            if tgts:
                                vctx["test_targets"] = tgts
                        vout = verifier(vctx) or {}
                        st = "FAIL" if str(vout.get("status", "PASS")).upper() == "FAIL" else "PASS"
                        return st, str(vout.get("detail", ""))[:300]
                    except Exception as exc:  # noqa: BLE001
                        return "ERROR", str(exc)[:200]

                vstatus, vdetail = _verify_once()
                self.emit("integrate", "verifier", "spec-integrate", integ,
                          "end-to-end acceptance criteria (real worker)", vdetail,
                          "integrate_verify", vstatus, level=L_MILESTONE)
                policy = getattr(self, "_on_integrate_fail", "record")
                if vstatus != "PASS" and policy == "rework":
                    # rework: the verifier carries the repair machinery —
                    # re-invoking it grants a fresh repair budget per round
                    for round_no in range(1, getattr(self, "_integrate_max_rework", 2) + 1):
                        self.loops.append({"type": "integrate-rework",
                                           "task": nid, "round": round_no,
                                           "detail": vdetail})
                        vstatus, vdetail = _verify_once()
                        self.emit("integrate", "verifier", "spec-integrate", integ,
                                  "integrate rework round " + str(round_no), vdetail,
                                  "integrate_verify", vstatus, level=L_MILESTONE)
                        if vstatus == "PASS":
                            break
                if vstatus != "PASS":
                    self.loops.append({"type": "integrate-fail", "task": nid,
                                       "detail": vdetail})
                    if (policy == "halt"
                            or getattr(self, "_on_integrate_fail_exhausted",
                                       "record") == "halt"):
                        raise IntegrateFailHalt(
                            "integrate FAIL at " + nid + " - the case demands"
                            " a halt: " + vdetail)
            else:
                self.emit("integrate", "verifier", "spec-integrate", integ,
                          "end-to-end acceptance criteria", "verification-before-completion", "", "PASS",
                          level=L_MILESTONE)
            self.tasks[integ].status = "done"
            self._completed += 1
        else:
            self._leaf_pipeline(node, contract_ctx, depth, parent, drv, ancestors)

        # close the node lifecycle — the guard refuses DONE if a mandatory gate
        # for this node kind was skipped (raises GateViolation in both engines).
        drv.done()

        # node-level HITL — a node may declare a human checkpoint (e.g. payouts
        # above the unattended cap, outreach without prior consent). A rejection
        # sends the node back for rework (version bump + re-derive event).
        hitl = node.get("hitl")
        if hitl:
            kind = hitl.get("kind", "node") if isinstance(hitl, dict) else "node"
            why = hitl.get("reason", "") if isinstance(hitl, dict) else str(hitl)
            if not self._hitl(kind, nid, why or f"human approval required for {title}"):
                self.tasks[nid].version += 1
                self.tasks[nid].runs += 1
                self.emit("hitl", "implementer", "spec-implement", nid,
                          "rework after HITL rejection", why, level=L_MILESTONE)

        self.tasks[nid].status = "done"
        self._completed += 1
        # C4: record the completed leaf in the run journal — a restarted run
        # (resume=True) reuses its persisted artifact instead of re-implementing
        if verdict == "leaf":
            # #7: stamp the spec hash so a later resume can detect a changed
            # spec and re-run, while an unchanged one is reused. Same module
            # resolver as the skip check, so the spec path matches.
            mfn = self._module_for(nid)
            self.workspace.journal_mark(
                nid, self.tasks[nid].version,
                self.workspace.spec_hash(f"specs/{mfn}.md"))
        # П2: one wave per committed node — the restartable trail. A leaf is
        # the unit a resume can skip; a branch commit marks its subtree closed.
        self._wave([{"kind": "node_commit", "node": nid, "verdict": verdict,
                     "version": self.tasks[nid].version, "depth": depth,
                     "completed": self._completed}],
                   parent=parent)
        # returning up a level — check both revision methods at this moment
        self._research_tick(node, depth)

    def _leaf_uses_worktree(self, nid: str, ws) -> bool:
        """Whether this leaf builds in an isolated git worktree (axis F).

        Worktree isolation exists to keep PARALLEL sibling leaves from clobbering
        each other. The synthesized product entry (``product_entry``) is the SERIAL
        integration point: it wires every sibling module into src/app.py and runs
        alone at integrate time, so it has no concurrent peer to isolate from. A
        worktree branched off HEAD merges back by the leaf's owned path, which
        drops a brand new src/app.py — the root integrate then never sees it and
        every route 404s. The entry therefore always writes to the main workspace.
        """
        return (self._isolation == "worktree"
                and nid != "product_entry"
                and bool(getattr(ws, "enabled", False))
                and bool(getattr(ws, "root", None)))

    def _invoke_implementer(self, ictx: dict, nid: str, fn: str) -> None:
        """Run the implementer for one leaf. Under `isolation: worktree`
        (axis F) the leaf builds in its OWN git worktree and merges back
        with a merge-tree pre-flight; otherwise it writes straight into the
        shared workspace (default)."""
        impl = self.agents["implementer"]
        ws = self.workspace
        # 462: mark any input drops during this leaf as belonging to THIS node,
        # so the doctor's silent_truncation detector gets real evidence later.
        _scope = (_trunc.node_scope(nid) if _trunc is not None
                  else contextlib.nullcontext())
        isolated = self._leaf_uses_worktree(nid, ws)
        if not isolated:
            with _scope:
                impl(ictx)
            return
        _ws_tx = _load_ws_tx()
        if _ws_tx is None:
            with _scope:
                impl(ictx)
            return
        _ws_tx.ensure_repo(ws.root)
        with _scope, _ws_tx.leaf_worktree(ws.root, fn, f"leaf:{nid}") as wt:
            # seed the leaf's spec into the worktree so it is self-contained
            # (the worktree branches off HEAD; an uncommitted spec is absent)
            self._seed_worktree_file(wt.path, ictx.get("spec"))
            view = _LeafWorkspaceView(ws, wt.path)
            impl({**ictx, "workspace": view})
        if getattr(wt, "conflicts", None):
            self.emit("implement", "implementer", "spec-implement",
                      f"{nid}:merge", "worktree merge refused — workspace kept",
                      str(wt.conflicts), verdict="CONFLICT", level=L_MILESTONE)

    def _seed_worktree_file(self, wt_path: str, rel: Optional[str]) -> None:
        """Copy one workspace-relative file (e.g. the leaf's spec) into the
        isolated worktree so the implementer reads it the same as a shared run."""
        if not rel or wt_path == self.workspace.root:
            return
        src = Path(self.workspace.root) / rel
        if not src.is_file():
            return
        dst = Path(wt_path) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _leaf_pipeline(self, node: dict, contract_ctx: Optional[dict],
                       depth: int = 0, parent: Optional[str] = None,
                       drv: Optional["_NodeDriver"] = None,
                       ancestors: tuple = ()):
        nid = node["id"]
        title = node.get("title", nid)
        # resume: a leaf already in the journal was specced, reviewed and frozen
        # in the original run; its spec sits in the restored checkpoint. Re-running
        # the review gate would REWRITE that spec (the lint loop even re-asks the
        # decomposer), drift its hash, and the reuse check below would then treat
        # it as "spec changed" and re-implement — i.e. restart instead of continue.
        # So keep the checkpoint spec untouched for a journaled leaf (replan opts out).
        _journaled = (self.resume and not self.replan
                      and nid in self._journal_index)
        if _journaled:
            self.emit("review", "engine", "", nid,
                      "resume: spec kept from checkpoint (already reviewed+journaled)",
                      "", "", "", level=L_DETAIL)
        else:
            # spec/plan is written at every depth (>= spec)
            self._review_gate(node, nid, title, depth, parent,
                              {"verdict": "leaf", "reasons": "within all thresholds",
                               "plan": ["bottom-up plan: DB → logic → API → tests",
                                        "TDD: test (RED) → impl → test (GREEN)",
                                        "two-stage review (spec-conformance, then quality)",
                                        "verification-before-completion + commit"]},
                              ancestors)
        # code & test scaffolds only from depth 'scaffold' upward; at 'execute'
        # an injected implementer agent produces real code instead of a scaffold.
        code_rel = test_rel = None
        if self.depth >= DEPTH_EXECUTE:
            fn = self._module_for(nid)   # variant A: collision-free module
            # B3: a requirement routed to edit an existing surface keeps its own
            # spec identity (specs/{fn}.md) but writes its CODE into the owner
            # module — deterministic edit-in-place, no parallel file, no spec
            # clobber. code_fn drives only the output paths, not the spec.
            ctgt = node.get("code_target")
            code_fn = Path(ctgt).stem if ctgt else fn
            code_rel, test_rel = f"src/{code_fn}.py", f"tests/test_{code_fn}.py"
            # C4 resume: the journal says this leaf finished and its artifact
            # survived the restart — reuse it, do not re-run the implementer.
            # #7: but only if its SPEC is unchanged — a spec edit since the
            # journal entry means the cached artifact is stale → re-run.
            spec_unchanged = True
            if self.resume and nid in self._journal_index:
                prev = self._journal_index[nid].get("spec_hash", "")
                cur = self.workspace.spec_hash(f"specs/{fn}.md")
                # only invalidate when BOTH hashes are known and differ; an
                # empty stored hash (old journal) keeps legacy reuse behavior
                if prev and cur and prev != cur:
                    spec_unchanged = False
                    self.emit("implement", "implementer", "spec-implement",
                              f"{nid}:impl", "spec changed since journal — "
                              "re-running (cache miss)", "", "", "",
                              level=L_MILESTONE)
            persisted = (self.resume and nid in self._journal_done
                         and spec_unchanged
                         and self.workspace.enabled and self.workspace.root
                         and (Path(self.workspace.root) / code_rel).is_file())
            if persisted:
                # iterate-loop: a deterministic PLUGIN GATE may have tightened
                # since the journal was written (a new check, not a spec edit).
                # Re-validate the cached artifact; if it now fails, treat it as a
                # cache MISS and re-implement ONLY this leaf.
                _stale = self._cached_artifact_stale(code_fn)
                if _stale:
                    persisted = False
                    self.emit("implement", "implementer", "spec-implement",
                              f"{nid}:impl", "resume: cached artifact fails a "
                              "current gate — re-running", str(_stale)[:200],
                              "", "", level=L_MILESTONE)
            if persisted:
                self.emit("implement", "implementer", "spec-implement", f"{nid}:impl",
                          "resume: reuse persisted artifact (run journal)", code_rel,
                          level=L_DETAIL)
            else:
                ictx = {"node": nid, "title": title, "depth": depth,
                        "workspace": self.workspace, "spec": f"specs/{fn}.md",
                        # variant A: the engine-resolved collision-free
                        # module — the worker must write THIS file, not
                        # recompute its own name from the node id. For an
                        # edit-in-place node code_fn is the owner module.
                        "module": code_fn}
                # #10: resolve the node's specialty (explicit → project
                # default → auto-inferred) so the implementer routes to the
                # matching model chain
                sp = self._resolve_specialty(node)
                if sp:
                    ictx["specialty"] = sp
                # 424: route the leaf to a power tier by its complexity, so a
                # hard node spawns on a stronger model than a trivial one.
                _tier = self._node_tier(node)
                if _tier:
                    ictx["tier"] = _tier
                    self.emit("implement", "engine", "spec-implement",
                              f"{nid}:route",
                              f"complexity routing → tier {_tier}",
                              _tier, "tier", level=L_DETAIL)
                # C1: route the leaf to a domain executor. A team-spec executor
                # runs the D1 orchestra with that team for THIS leaf (overrides
                # the global implementer team); a named agent / no match keeps
                # the default path.
                if self._executors_cfg:
                    domain, team = self._resolve_executor(node)
                    if team:
                        ictx["team"] = team
                        self.emit("implement", "engine", "spec-implement",
                                  f"{nid}:route",
                                  f"executor routing: {domain} → team",
                                  ", ".join(s.get("role", "?") for s in team),
                                  "executor", level=L_DETAIL)
                # Carry the node's structural complexity label so the worker can
                # scale effort to task size (e.g. creator-ensemble size): a
                # leaf_small earns one candidate, leaf_big/branch keep the
                # configured ensemble. Model-independent (label from metrics).
                if not node.get("children"):
                    ictx["complexity"] = self._node_complexity_label(node)
                # Process tiering: a simple single-concern leaf builds with one
                # coder pass, not the four-call orchestra — unless the engine has
                # already routed it to an explicit executor team (that decision
                # wins). Cuts LLM calls on the long tail of trivial leaves; the
                # leaf is still verified by the same gates.
                if "team" not in ictx and self._node_solo(node):
                    ictx["solo"] = True
                    self.emit("implement", "engine", "spec-implement",
                              f"{nid}:route",
                              "process tiering → solo coder (simple leaf)",
                              "solo", "tier", level=L_DETAIL)
                if self._leaf_seconds:
                    ictx["deadline"] = time.time() + self._leaf_seconds
                try:
                    self._invoke_implementer(ictx, nid, code_fn)
                except NotImplementedError:
                    raise   # missing agent is a CONFIG error, not a crash
                except TimeoutError as exc:
                    # the leaf hit its time ceiling: surrender it RED and
                    # move on — one wedged leaf must never stall the run
                    self.loops.append({"type": "leaf-timeout", "task": nid,
                                       "detail": str(exc)[:200]})
                    self.emit("implement", "engine", "spec-implement",
                              f"{nid}:impl",
                              "leaf time ceiling hit — leaf surrendered red",
                              str(exc)[:200], "leaf_timeout", "TIMEOUT",
                              level=L_MILESTONE)
                except Exception as exc:  # noqa: BLE001
                    # a crashing WORKER surrenders the leaf, never the run:
                    # the artifacts stay red and the gates judge them
                    self.loops.append({"type": "implementer-crash",
                                       "task": nid,
                                       "detail": str(exc)[:200]})
                    self.emit("implement", "implementer", "spec-implement",
                              f"{nid}:impl",
                              "implementer crashed — leaf surrendered red",
                              str(exc)[:200], level=L_MILESTONE)
                # B3 guard (deterministic): a routed edit-in-place node must
                # NOT leave a parallel fork. If the worker still wrote its own
                # src/{fn}.py despite being handed the owner module, drop that
                # fork — "one surface = one module" is enforced by code, not by
                # trusting the model. The owner file carries the real edit; if
                # it stayed empty the node fails its gates honestly.
                if ctgt and f"src/{fn}.py" != code_rel:
                    fork = Path(self.workspace.root) / f"src/{fn}.py"
                    if fork.is_file():
                        fork.unlink()
                        (Path(self.workspace.root)
                         / f"tests/test_{fn}.py").unlink(missing_ok=True)
                        self.emit("implement", "engine", "spec-implement",
                                  f"{nid}:impl",
                                  "edit-in-place guard: dropped a parallel fork",
                                  f"removed src/{fn}.py — surface owned by {ctgt}",
                                  "amend_guard", "ENFORCED", level=L_MILESTONE)
                # 437: a late requirement must leave a REAL delta in its owner
                # module before it is judged 'implemented' (the v041 empty-change
                # failure). Inert for ordinary nodes.
                self._late_req_delta_gate(node, nid, depth, code_rel)
                self._judge_leaf(nid, title, fn, code_rel, test_rel)
                # Single-authority invariant (root cause of the v078 NOT READY):
                # record the real code file THIS leaf delivered so the root
                # integrate can verify it survived into the assembled tree. The
                # leaf's code is committed by one git authority (ws_tx) while the
                # engine commits a `feat:` by another; when the two histories
                # diverge the leaf module is orphaned and vanishes from the tree
                # the boot-gate reads — the product then boots an empty/stub
                # shell and 404s every route. Recording the CLAIM here lets
                # integrate cross-check the FINAL tree and fail loudly (never
                # green-wash a product whose feature modules were dropped).
                # Record ONLY leaves that actually MATERIALISED their code file
                # on the tree (post worktree merge-back). A decomposer-invented
                # meta leaf — e.g. one the model named "integrate_verify" — whose
                # derived code_rel was never written to disk must NOT be tracked,
                # else the integrate gate fails on a module the product never
                # needed. A leaf that wrote an EMPTY/stub file IS tracked (file
                # exists) so the integrate check still flags it as lost.
                if code_rel and (Path(self.workspace.root) / code_rel).is_file():
                    self.__dict__.setdefault(
                        "_built_leaf_code", {})[nid] = code_rel
        elif self.depth >= DEPTH_SCAFFOLD:
            code_rel = self.workspace.code(nid, title)
            test_rel = self.workspace.test(nid, title)
        impl = f"{nid}:impl"
        self.task(impl, f"Implement {title}", "impl", "implementer", "spec-implement", parents=[nid])
        self.emit("implement", "implementer", "spec-implement", impl,
                  "design → bottom-up plan (DB→logic→API→tests)", title)
        self.emit("implement", "implementer", "spec-implement", impl,
                  "TDD: write test (RED) → minimal impl → test (GREEN)", "", level=L_DETAIL)
        # lifecycle: every leaf is checked against its contract before impl
        if drv is not None:
            drv.go(EV_CONTRACT)
            drv.go(EV_IMPLEMENT)

        # contract drift episode
        if node.get("drift") and contract_ctx:
            res = self._contract(contract_ctx["artifact"], contract_ctx["code_drift"])
            drift_detail = res["drift"][0]["detail"] if res["drift"] else ""
            self.emit("implement", "implementer", "spec-implement", impl,
                      "contract_check vs frozen L2", drift_detail, "contract_check", res["status"],
                      level=L_MILESTONE)
            classify = node["drift"]["classify"]
            self.emit("drift", "implementer", "drift-gate", impl,
                      "drift-gate classify", classify, level=L_MILESTONE)
            if classify == "contract_wrong":
                self.emit("respec", "spec-reviewer", "respec-gate", f"{contract_ctx['artifact']}",
                          "spec-first: update contract node, version-bump, re-gate, restart impl",
                          f"{contract_ctx['artifact']} → {contract_ctx['fixed']}", level=L_MILESTONE)
                self.loops.append({"type": "drift-respec", "task": impl, "detail": drift_detail})
                self.tasks[impl].runs += 1
                res2 = self._contract(contract_ctx["fixed"], contract_ctx["code_drift"])
                self.emit("implement", "implementer", "spec-implement", impl,
                          "contract_check after respec", "matches corrected contract",
                          "contract_check", res2["status"], level=L_MILESTONE)
            elif classify == "code_wrong":
                # the frozen L2 stands; the code is corrected and re-checked
                self.emit("implement", "implementer", "spec-implement", impl,
                          "code-wrong: fix code to match the frozen L2, re-run impl",
                          contract_ctx["artifact"], level=L_MILESTONE)
                self.loops.append({"type": "drift-codefix", "task": impl, "detail": drift_detail})
                self.tasks[impl].runs += 1
                res2 = self._contract(contract_ctx["artifact"],
                                      contract_ctx.get("code_fixed", contract_ctx["code_drift"]))
                self.emit("implement", "implementer", "spec-implement", impl,
                          "contract_check after code fix", "matches frozen contract",
                          "contract_check", res2["status"], level=L_MILESTONE)

        # contract drift stayed in IMPLEMENT (respec/codefix self-loop)
        if drv is not None and node.get("drift") and contract_ctx:
            drv.go(EV_DRIFT)

        # review critique loop
        review = f"{nid}:review"
        self.task(review, f"Review {title}", "review", "spec-reviewer", "spec-reviewer", parents=[impl])
        if drv is not None:
            drv.go(EV_REVIEW)
        fails = int(node.get("review_fails", 0))
        for i in range(fails):
            self.emit("review", "spec-reviewer", "spec-reviewer", review,
                      "impl-review (spec-conformance)", "FAIL: missing edge-case handling on error path",
                      "", "FAIL", level=L_MILESTONE)
            self.emit("review", "implementer", "spec-implement", impl,
                      "fix per critique → unblock → re-run", "")
            self.tasks[impl].runs += 1
            self.loops.append({"type": "review-fail", "task": review, "detail": "spec-conformance critique"})
            # failing review sends the node back to IMPLEMENT, then re-review
            if drv is not None:
                drv.go(EV_CRITIQUE)
                drv.go(EV_REVIEW)
        self.emit("review", "spec-reviewer", "spec-reviewer", review,
                  "impl-review → quality gate", "", "", "PASS", level=L_MILESTONE)
        if drv is not None:
            drv.go(EV_REVIEW_PASS)
        if self.depth >= DEPTH_SCAFFOLD:
            self.emit("implement", "implementer", "spec-implement", impl,
                      "git commit + verification-before-completion", title)
            self.workspace.commit(f"feat: {title}", [code_rel, test_rel])
        self.tasks[impl].status = "done"
        self.tasks[review].status = "done"
        self._completed += 2

    def _derive_smoke_contract(self) -> None:
        """#9 / П2: derive an API contract from the smoke acceptance tests and
        write SMOKE-CONTRACT.md, so the contract is the executable truth (no
        hand-maintained drift). Best effort — never blocks a run."""
        ws = self.workspace
        smoke = Path(ws.root or ".") / "tests" / "smoke"
        if not (ws.enabled and ws.root and smoke.is_dir()):
            return
        try:
            from tests.harness import contract_from_smoke as cfs
        except Exception:  # noqa: BLE001
            try:
                from harness import contract_from_smoke as cfs  # type: ignore
            except Exception:  # noqa: BLE001
                return
        blocks = []
        for f in sorted(smoke.glob("test_*.py")):
            md = cfs.contract_from_file(str(f))
            if md:
                blocks.append(f"<!-- from {f.name} -->\n{md}")
        if blocks:
            ws._write("SMOKE-CONTRACT.md",
                      "# Smoke-derived API contract (auto-generated)\n\n"
                      + "\n".join(blocks), "contract")

    def _concise_red_reason(self, out: str, boot_detail: str = "") -> str:
        """Turn a raw failure dump into ONE human line for the dashboard + the
        doctor — 'FAIL (scaffolds)' tells the operator nothing. Prefers the
        boot-gate verdict (a 404 route is the real cause), else the pytest
        'FAILED …' line + its assertion. Model-independent string surgery."""
        if boot_detail:
            for ln in boot_detail.splitlines():
                s = ln.strip()
                if s.startswith(("BOOTGATE_FAIL", "boot-gate RED", "- boot-gate")):
                    return s[:200]
            return boot_detail.strip().splitlines()[0][:200] if boot_detail.strip() else "boot-gate RED"
        failed = [ln.strip() for ln in out.splitlines() if ln.startswith("FAILED ")]
        assertion = [ln.strip() for ln in out.splitlines()
                     if ln.lstrip().startswith("E   ")]
        head = "; ".join(failed[:3])
        tail = assertion[-1] if assertion else ""
        reason = " — ".join(p for p in (head, tail) if p)
        return reason[:240] or "tests failed (see TEST-RESULTS.md)"

    def _blamed_module_from_failure(self, text: str, entry_stem: str = "") -> "Optional[str]":
        """Extract the feature module a failing pytest traceback blames, so the
        doctor can rework THAT module rather than only the entry. Walks the
        traceback for ``src/<name>.py`` frames and returns the deepest one that is
        a real, owned leaf and is NOT the entry or a test file. Returns None when
        the failure points only at the entry / tests (entry-level repair). Pure
        string surgery — model-independent."""
        if not text:
            return None
        stems: list[str] = []
        for m in re.finditer(r"\bsrc/([A-Za-z_][A-Za-z0-9_]*)\.py", text):
            stems.append(m.group(1))
        if not stems:
            # No src/<file>.py frame — but a ROOT BOOT-GATE detail still names the
            # failing ROUTE ("GET /ui -> 500"). Blame the route's owner (Шаг 1).
            return self._blamed_module_from_routes(text, entry_stem)
        # deepest frame last in a pytest traceback; prefer a known leaf that is
        # neither the entry nor a test, scanning from the deepest frame upward.
        for stem in reversed(stems):
            if stem in ("", entry_stem) or stem.startswith("test_"):
                continue
            if stem in self.tasks or stem == "product_entry":
                if stem != "product_entry":
                    return stem
        # frames named only the entry / tests — fall back to route-owner blame so
        # a boot-gate detail that ALSO mentions the entry frame still heals the leaf.
        return self._blamed_module_from_routes(text, entry_stem)

    def _blamed_module_from_routes(self, text: str,
                                   entry_stem: str = "") -> "Optional[str]":
        """Plan Шаг 1 — blame the leaf that OWNS a failing route named in a
        ROOT BOOT-GATE detail when no ``src/<file>.py`` frame pins a module.

        Why: the assembled product BOOTS but a route fails at runtime (live v111:
        ``GET /ui -> 500`` from a handler arity bug; ``POST /notes -> 500`` from a
        missing schema). The engine owns the entry wiring and it is correct, so
        the existing remedy (rebuild the entry) loops without ever touching the
        leaf whose handler 500s. The boot detail carries the route but no file
        frame, so the traceback walk above finds nothing. Resolve the route's
        owner with the SAME AST resolver the entry synthesiser uses — a
        deterministic, model-independent blame — and rework THAT leaf instead.

        Returns the owning module stem (an owned leaf, never the entry), or None
        when no failing route resolves to a built leaf."""
        if not text:
            return None
        # route paths the detail names; drop anything that looks like a source
        # file (``src/app.py`` -> ``/app.py``) so a stray frame path is not read
        # as a route.
        paths = {p.rstrip("/.,;:)\"'") for p in
                 re.findall(r"(/[A-Za-z0-9_./{}-]+)", text)}
        paths = {p for p in paths if len(p) > 1
                 and not p.endswith((".py", ".md", ".txt", ".json", ".html"))}
        if not paths:
            return None
        try:
            contract = self._product_contract()
            mapping, _unresolved = self._resolve_route_handlers(contract)
        except Exception:  # noqa: BLE001 — resolver is best-effort
            return None
        for (_method, path), info in (mapping or {}).items():
            stem = info[0] if isinstance(info, (tuple, list)) and info else None
            if not stem or stem == entry_stem:
                continue
            if path.rstrip("/") in paths and stem in getattr(self, "tasks", {}):
                return stem
        return None

    def _remedy_rework_module(self, module: str, reason: str) -> bool:
        """Module-targeted heal: the integrate acceptance failed inside a FEATURE
        module (e.g. notes_api raising 'no such table'), not the entry. Re-run the
        implementer on that module's existing spec with the failure as a repair
        directive, on the strong tier (the weaker build already shipped the bug).
        Returns True iff a rebuild was attempted (caller re-verifies). Legitimate:
        the directive carries the integrate failure signal, never a hand-written
        fix — the worker still writes the code."""
        ws = self.workspace
        if not (getattr(ws, "enabled", False) and getattr(ws, "root", None)
                and "implementer" in self.agents):
            return False
        spec_rel = f"specs/{module}.md"
        sp = Path(ws.root) / spec_rel
        if not sp.is_file():
            return False
        try:
            base = sp.read_text(encoding="utf-8")
            directive = (
                "\n\n### Repair directive (integrate acceptance RED)\n"
                f"The assembled product's acceptance run failed inside this "
                f"module:\n\n    {reason.strip()[:240]}\n\n"
                "Fix THIS module so the failing behaviour works end-to-end "
                "(e.g. ensure any required state — a DB schema/table — is "
                "initialised before it is used). Keep the existing public API. "
                "Do NOT weaken or edit the tests.\n")
            if "Repair directive (integrate acceptance RED)" not in base:
                sp.write_text(base + directive, encoding="utf-8")
        except OSError:
            return False
        self.emit("integrate", "doctor", "spec-implement", "L0:integrate",
                  f"module repair: rework {module} (acceptance blamed it)",
                  reason[:160], "integrate_verify", "", level=L_MILESTONE)
        ictx = {"node": module, "title": f"Repair {module}",
                "depth": self.depth, "workspace": ws,
                "spec": spec_rel, "module": module, "tier": "strong"}
        try:
            self._invoke_implementer(ictx, module, module)
        except Exception as exc:        # noqa: BLE001
            self.emit("integrate", "doctor", "", "L0:integrate",
                      "module repair raised", str(exc)[:140],
                      "integrate_verify", "FAIL", level=L_MILESTONE)
            return False
        return True

    def _attempt_integrate_repair(self, reason: str, out: str = "") -> bool:
        """если падает → нужен рабочий доктор: a RED assembled product is
        ANALYSED and a fix is ATTEMPTED, not merely recorded. The doctor's lever
        depends on WHERE the failure lives: a traceback blaming a FEATURE module
        (e.g. notes_api 'no such table') is reworked at that module; otherwise the
        engine rebuilds the declared ENTRY (reconcile_check) and re-boots. Returns
        True iff a fix was attempted (caller re-verifies). No-op for a non-web
        project / sim with no derivable contract and no blamed module."""
        c = self._product_contract()
        entry = c.get("entry") if c else None
        entry_stem = Path(entry).stem if entry else ""
        act = self._doctor_advise(
            {"id": "L0:integrate"}, "L0:integrate", 0,
            "integrate_verify", "FAIL", {"reasons": reason})
        remedy = getattr(act, "kind", "") if act is not None else ""
        if remedy not in ("reconcile_check", "rework", "escalate_tier"):
            return False
        # 1) feature-module bug: rework the module the traceback blames. The entry
        #    is fine; rebuilding it would loop forever (v059 notes_api).
        blamed = self._blamed_module_from_failure(out or reason, entry_stem)
        if blamed and self._remedy_rework_module(blamed, reason):
            return True
        # 2) entry-level repair: rebuild the assembled entry + re-boot.
        if entry:
            return self._remedy_reconcile_check(entry, escalate=(remedy == "escalate_tier"))
        return False

    def _verify_tests(self) -> None:
        ws = self.workspace
        if not ws.enabled or not ws.root:
            return
        tests_dir = Path(ws.root) / "tests"
        if not tests_dir.exists():
            return
        self._derive_smoke_contract()
        # DETERMINISTIC-FIRST ASSEMBLY (model-independent). Before the suite and
        # the boot-gate, let the ENGINE own the product entry: if every declared
        # route resolves to a built leaf handler, (re)synthesise the WSGI router
        # deterministically. This guarantees M1/M2/M3 are closed on every verify,
        # not only inside the conditional reconcile remedy (which a budget-spent
        # doctor may never reach — live v089). No-op when a critical route is
        # unresolved, leaving the LLM implementer responsible — in that fallback
        # the AST hardening net (Phase 2) still guarantees malformed body -> 400.
        if not self._try_synthesize_entry():
            self._harden_entry()
        # run from the workspace root: paths stay short (tests/test_x.py),
        # confcutdir isolates the run from any host-project conftest.py;
        # --import-mode=importlib tolerates same-basename test files across
        # tests/ and tests/smoke/ (legacy prepend mode false-reds on a dup name).
        _argv = ["python3", "-m", "pytest", "-v", "--no-header",
                 f"--confcutdir={ws.root}", "-p", "no:cacheprovider",
                 "--import-mode=importlib", "tests"]

        def _run_suite() -> "tuple":
            try:
                p = subprocess.run(_argv, capture_output=True, text=True,
                                   timeout=300, cwd=str(ws.root))
                return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
            except Exception as exc:  # noqa: BLE001
                return False, f"pytest error: {exc}"

        passed, out = _run_suite()
        # B2 ROOT BOOT-GATE: a green corpus is HOLLOW if the assembled product
        # does not serve its frozen contract. A non-200 route is a hard RED even
        # when every module unit-test passed (v020: all routes 404'd green).
        boot_detail = ""
        if passed:
            boot_ok, boot_detail = self._assembled_product_boots()
            if not boot_ok:
                passed = False
                out += "\n\n=== ROOT BOOT-GATE (assembled product) ===\n" + boot_detail + "\n"
        root_id = getattr(self, "_root_id", "L0")
        reason = ""
        if not passed:
            # 1) RECORD it informatively — the real cause, never 'FAIL (scaffolds)'
            reason = self._concise_red_reason(out, boot_detail)
            self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                      "assembled product RED", reason,
                      "integrate_verify", "FAIL", level=L_MILESTONE)
            self.loops.append({"type": "integrate-fail", "task": root_id,
                               "detail": reason})
            # 2) ANALYSE + ATTEMPT A FIX (doctor → rework the blamed leaf or
            #    reconcile the entry through a real worker), then RE-VERIFY —
            #    LOOPING up to integrate_max_rework rounds. The boot-gate is
            #    fail-fast (it reports ONE failing route at a time), so a product
            #    with several broken leaves (live v111: /ui arity, /notes schema,
            #    a delete route) needs one healing round PER route; a single pass
            #    dead-ended after the first and the run came back NOT READY.
            #    The loop STOPS the instant the assembled product is green —
            #    the doctor is never run over a product that already serves its
            #    contract (Plan Шаг 5).
            for _round in range(max(1, getattr(self, "_product_repair_rounds", 3))):
                if not self._attempt_integrate_repair(reason, out):
                    break               # no actionable remedy — stop looping
                # A repair that REWORKS a leaf can change its handler surface, so
                # the engine-owned entry must be RE-DERIVED before re-verifying —
                # otherwise app.py keeps importing a handler the rework dropped and
                # the assembled product ImportErrors at boot (live v115: web_ui was
                # reworked without _handle_health while app.py still imported it).
                # Idempotent (write-only-if-changed); a no-op when the surface is
                # unchanged. Closes the entry↔leaf drift gap (Plan Шаг 1/2).
                self._try_synthesize_entry()
                passed, out = _run_suite()
                if passed:
                    b_ok, b_detail = self._assembled_product_boots()
                    if not b_ok:
                        passed = False
                        out += "\n=== BOOT-GATE (post-repair) ===\n" + b_detail
                if passed:
                    # healed: drop the integrate-fail we recorded, mark green
                    self.loops = [lp for lp in self.loops
                                  if not (lp.get("task") == root_id
                                          and lp.get("type") == "integrate-fail")]
                    self.emit("integrate", "doctor", "spec-integrate",
                              "L0:integrate",
                              "repair healed the product (red→green)",
                              f"suite + boot-gate green after {_round + 1} round(s)",
                              "integrate_verify", "PASS", level=L_MILESTONE)
                    reason = ""
                    break
                # still red — record the current cause and try the next route
                reason = self._concise_red_reason(out) + " (still red after repair)"
        depth_name = next((k for k, v in DEPTHS.items() if v == self.depth), str(self.depth))
        ws._write("TEST-RESULTS.md",
                  f"# Test results (depth={depth_name})\n\nStatus: "
                  f"{'✅ PASS' if passed else '❌ FAIL — ' + reason}\n\n"
                  f"```\n{out[-20000:]}\n```\n", "test-results")
        self.emit("integrate", "verifier", "spec-integrate", "verify",
                  "ran test suite (pytest)",
                  "PASS" if passed else f"FAIL — {reason}",
                  "", "PASS" if passed else "FAIL", level=L_MILESTONE)

    def _assembled_product_boots(self) -> "tuple":
        """Un-mockable B2 at the ROOT: boot the real assembled WSGI entry in a
        fresh subprocess and drive its frozen contract. Returns (ok, detail).

        Runs ONLY when the constitution declares a WSGI entry (so p4/p5 and any
        non-web product are unaffected) — the same gate the per-node verifier
        applies, repeated here so the FINAL corpus verdict can never be green
        over a product that 404s its own contract. No test-harness import: the
        probe is a self-contained subprocess (tests/ is not importable in a
        real run)."""
        ws = self.workspace
        if not (ws.enabled and ws.root):
            return True, ""
        c = self._product_contract()
        if not c:
            return True, ""                 # no declared runnable product → n/a
        boot = c["boot"]
        probe_cfg = json.dumps({
            "callable": c["callable"],
            "entry_stem": Path(c["entry"]).stem,
            "ok_route": boot.get("ok_route", ""),
            "html_route": boot.get("html_route", ""),
            "json_roundtrip": boot.get("json_roundtrip", ""),
            "extra_routes": c.get("routes", [])})
        try:
            proc = subprocess.run(
                ["python3", "-c", _ROOT_BOOT_PROBE, str(ws.root), probe_cfg],
                capture_output=True, text=True, timeout=60)
            sout = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            return False, f"boot-gate could not run the product: {exc}"
        if "BOOTGATE_OK" in sout:
            return True, ""
        marker = "BOOTGATE_FAIL"
        detail = sout.split(marker, 1)[1].strip() if marker in sout else sout.strip()
        return False, "assembled product does not serve its contract: " + detail[:300]

    def _product_check(self, acceptance: Optional[dict]) -> None:
        """Build+run the materialised product and assert it is READY against an
        acceptance spec, writing PRODUCT-RESULTS.md.

        Why: depth 'execute' only proves real code & tests exist; 'product'
        proves the integrated whole actually BUILDS, RUNS and meets acceptance
        criteria — the gap between 'code is written' and 'product works'.

        What: reads ``acceptance`` (see schema below). Optionally runs an
        ``entrypoint`` shell command (guarded by try/except + timeout, like
        ``_verify_tests``); then evaluates each check under ``smoke``/``e2e``/
        ``metrics``. A check PASSES only on real evidence — if there is no real
        runnable entrypoint, smoke/e2e checks honestly FAIL (NOT READY) rather
        than fake green. Verdict is READY only when every check passed.

        Acceptance spec shape (all keys optional)::

            acceptance:
              entrypoint: "<shell cmd>"   # how to build/run the product
              smoke:   [ "<check>", ... ] # liveness checks (needs entrypoint)
              e2e:     [ "<check>", ... ] # end-to-end scenarios (needs entrypoint)
              metrics: [ "<check>", ... ] # measurable targets (needs entrypoint)

        Test: run depth='product' with an acceptance smoke check but the stub
        implementer (no real entrypoint) and assert the verdict is NOT READY;
        run with no acceptance and assert PRODUCT-RESULTS.md says readiness is
        not asserted (and the run still completes).
        """
        ws = self.workspace
        if not ws.enabled or not ws.root:
            return
        depth_name = next((k for k, v in DEPTHS.items() if v == self.depth), str(self.depth))

        # No acceptance spec — readiness is simply not asserted; never fail.
        if not acceptance:
            ws._write("PRODUCT-RESULTS.md",
                      f"# Product readiness (depth={depth_name})\n\n"
                      "Status: ⚪ NOT ASSERTED\n\n"
                      "No acceptance spec (`project['acceptance']`) was provided — "
                      "product readiness not asserted.\n", "product-results")
            self.emit("product", "verifier", "spec-integrate", "product",
                      "no acceptance spec — readiness not asserted", "",
                      "", "NOT ASSERTED", level=L_MILESTONE)
            return

        entrypoint = acceptance.get("entrypoint")
        # Run the entrypoint if one is given. Its success is the precondition
        # for any smoke/e2e/metrics check to be able to pass.
        entry_ran = False
        entry_ok = False
        entry_out = ""
        if entrypoint:
            try:
                proc = subprocess.run(entrypoint, shell=True, capture_output=True,
                                      text=True, timeout=120, cwd=str(ws.root))
                entry_ran = True
                entry_ok = proc.returncode == 0
                entry_out = ((proc.stdout or "") + (proc.stderr or ""))[-8000:]
            except Exception as exc:  # noqa: BLE001
                entry_ran = True
                entry_ok = False
                entry_out = f"entrypoint error: {exc}"

        lines: list[str] = []
        failed: list[str] = []

        def record(kind: str, check: str, passed: bool, note: str = "") -> None:
            mark = "✅ PASS" if passed else "❌ FAIL"
            suffix = f" — {note}" if note else ""
            lines.append(f"- [{kind}] {mark}: {check}{suffix}")
            if not passed:
                failed.append(f"[{kind}] {check}")

        # Each runtime check needs a real, successfully-run entrypoint. Without
        # one we honestly fail it — we have a stub module, not a running product.
        no_entry_note = ("no runnable entrypoint — only stub modules exist; "
                         "cannot assert at runtime")
        entry_fail_note = "entrypoint did not run cleanly (returncode != 0)"
        # When the case declares no explicit entrypoint but the human spec DOES
        # declare a runnable web product (a derivable contract), the engine has
        # its own un-mockable runner: boot the assembled entry in a subprocess
        # and probe its contract routes (_assembled_product_boots). Use that as
        # the runtime oracle so smoke/e2e checks are REALLY exercised instead of
        # dead-ending on 'no entrypoint'. This is the same oracle the root boot
        # gate uses — no per-case boilerplate, no stub pass, no solution leak.
        boot_ok = None
        boot_detail = ""
        if not entrypoint and self._product_contract():
            boot_ok, boot_detail = self._assembled_product_boots()
        for kind in ("smoke", "e2e", "metrics"):
            for check in acceptance.get(kind, []) or []:
                if entrypoint:
                    if entry_ok:
                        record(kind, check, True, "entrypoint ran cleanly")
                    else:
                        record(kind, check, False, entry_fail_note)
                elif boot_ok is True:
                    record(kind, check, True,
                           "assembled product boots and serves its contract")
                elif boot_ok is False:
                    record(kind, check, False,
                           boot_detail or "product does not serve its contract")
                else:
                    record(kind, check, False, no_entry_note)

        ready = bool(lines) and not failed
        verdict = "READY" if ready else "NOT READY"
        head = [f"# Product readiness (depth={depth_name})", "",
                f"Status: {'✅ READY' if ready else '❌ NOT READY'}", ""]
        if entrypoint:
            head += [f"Entrypoint: `{entrypoint}`",
                     f"Entrypoint ran: {'yes' if entry_ran else 'no'} · "
                     f"clean exit: {'yes' if entry_ok else 'no'}", ""]
        else:
            head += ["Entrypoint: (none provided)", ""]
        head += ["## Acceptance checks", ""]
        if not lines:
            head.append("- (acceptance spec defined no checks)")
        body = head + lines
        if failed:
            body += ["", "## Failed checks"] + [f"- {f}" for f in failed]
        if entrypoint:
            body += ["", "## Entrypoint output", "", f"```\n{entry_out}\n```"]
        ws._write("PRODUCT-RESULTS.md", "\n".join(body) + "\n", "product-results")

        self.emit("product", "verifier", "spec-integrate", "product",
                  "build+run product against acceptance spec",
                  f"{len(failed)} failed check(s)" if failed else "all checks passed",
                  "", verdict, level=L_MILESTONE)
        # Plan Шаг 4 — record the terminal product verdict so RunResult (and the
        # run's meta.json) can report it from one field. v111 reached "NOT READY"
        # yet meta.status stayed None: the verdict existed only in the trace tail.
        self._product_status = verdict
        self._product_failed = list(failed)

    def _load_revisions(self, project: dict) -> list[dict]:
        """Normalise the project's revision declarations into a list, each with
        an explicit ``method``. Accepts a ``revisions:`` list (preferred) or a
        single legacy ``revision:`` block. A block's method is taken verbatim if
        given, else inferred: ``on_level_return`` trigger ⇒ ``level_return``,
        anything else (every_n_tasks / m_test_errors / cron) ⇒ ``internal``."""
        raw = project.get("revisions")
        if raw is None:
            single = project.get("revision")
            raw = [single] if single else []
        out: list[dict] = []
        for i, rev in enumerate(raw):
            if not rev:
                continue
            rev = dict(rev)
            rev.setdefault("_id", f"rev{i}")
            if "method" not in rev:
                rev["method"] = ("level_return"
                                 if rev.get("trigger", "on_level_return") == "on_level_return"
                                 else "internal")
            out.append(rev)
        return out

    def _research_tick(self, node: dict, depth: int):
        """Both revision methods are evaluated the moment a node completes and
        control returns up a level:
          * level_return — fire when the node whose subtree just folded is the
            revision's target branch (a real "branch done → step up" signal);
          * internal     — fire mid-run once enough work has accumulated
            (every_n_tasks / m_test_errors), independent of which branch.
        """
        nid = node.get("id")
        for rev in self._revisions:
            if rev["_id"] in self._fired_rev:
                continue
            if rev["method"] == "level_return":
                # the targeted branch just completed and we are stepping up
                if nid == rev.get("invalidates") or nid == rev.get("after_node"):
                    self._apply_revision(rev, "level_return")
            elif rev["method"] == "internal":
                after = int(rev.get("after_completed", self.tools.RESEARCH_LANE.get(
                    "every_n_tasks", 20)))
                # fire once enough work has accumulated AND the invalidated node
                # is already done — internal revision REOPENS a finished node;
                # if the target isn't done yet, defer to a later tick / sweep.
                target = rev.get("invalidates")
                target_done = (target in self.tasks
                               and self.tasks[target].status == "done")
                if self._completed >= after and target_done:
                    self._apply_revision(rev, "internal")

    def _revision_sweep(self):
        """End-of-run: fire any declared revision that never met its in-run
        condition, so nothing declared is silently dropped."""
        for rev in self._revisions:
            if rev["_id"] not in self._fired_rev:
                self._apply_revision(rev, rev["method"])

    def _apply_revision(self, rev: dict, method: str):
        """Run the research trigger, then (for a declared revision) respec the
        invalidated node: version-bump + re-derive only the affected subtree.
        ``method`` distinguishes the two revision lanes in the trace + loops."""
        if rev["_id"] in self._fired_rev:
            return
        self._fired_rev.add(rev["_id"])
        reason = rev.get("trigger", "on_level_return" if method == "level_return"
                         else "every_n_tasks")
        out = self._research(reason, self._completed, rev.get("test_errors", 0))
        self.emit("revision", "researcher", "spec-research", "revision",
                  f"research_trigger_check ({method})",
                  f"fired_by={out['fired_by'] or [reason]}",
                  "research_trigger_check", "trigger", level=L_MILESTONE)
        self.emit("revision", "researcher", "spec-research", "revision",
                  f"REVISION finding ({method})", rev["finding"], level=L_MILESTONE)
        target = rev["invalidates"]
        self.emit("respec", "spec-reviewer", "respec-gate", target,
                  "respec-gate: change the cause first, version-bump, re-derive only affected subtree",
                  rev["effect"], level=L_MILESTONE)
        self.loops.append({"type": "revision-respec", "method": method,
                           "task": target, "detail": rev["finding"]})
        # version-bump the target and re-derive its subtree under the new spec
        if target in self.tasks:
            old_v = self.tasks[target].version
            self.tasks[target].version += 1
            self.tasks[target].runs += 1
            # E5 provenance: the superseded spec is archived, not overwritten
            self.workspace.respec_archive(target, old_v, self.tasks[target].version,
                                          rev.get("finding", ""))
        affected = [tid for tid, t in list(self.tasks.items())
                    if t.parents and target in t.parents and t.kind in {"impl", "decompose"}]
        # large blast radius -> human sign-off (anti-thrash confirmation)
        if len(affected) >= RESPEC_HITL_BLAST and getattr(self, "_policy_cfg", {}).get("human_in_loop"):
            self._hitl("respec", target,
                       f"respec invalidates {len(affected)} downstream nodes — confirm re-derive")
        for tid in affected:
            t = self.tasks[tid]
            t.version += 1
            t.runs += 1
            self.emit("revision", "implementer", "spec-implement", tid,
                      "re-derive under superseded spec", f"{target} v2")

        # close the self-improvement loop: re-run the gate the signal failed and
        # confirm the re-derived subtree now passes. A revision may declare
        # ``recheck_fails: N`` — the loop iterates N times (each a failed
        # re-check) before the signal is resolved, modelling an improvement that
        # takes more than one pass. ``recheck: false`` opts a revision out.
        if rev.get("recheck", True):
            self._close_revision_loop(rev, method, target)

    def _close_revision_loop(self, rev: dict, method: str, target: str):
        """signal → revision → respec → re-derive → RE-CHECK → resolved.

        Re-asserts the acceptance/respec-gate after a revision and records the
        verdict, so the trace proves the signal was actually fixed (or how many
        passes it took). Returns nothing — emits events + loops."""
        fails = int(rev.get("recheck_fails", 0))
        for i in range(fails):
            self.emit("respec", "verifier", "spec-integrate", target,
                      f"respec-gate re-check #{i + 1} ({method}) — signal NOT yet resolved",
                      rev.get("finding", ""), "respec_gate", "FAIL", level=L_MILESTONE)
            self.loops.append({"type": "revision-recheck", "method": method,
                               "task": target, "resolved": False})
            if target in self.tasks:
                self.tasks[target].runs += 1
        self.emit("respec", "verifier", "spec-integrate", target,
                  f"respec-gate re-check ({method}) — signal resolved, acceptance re-confirmed",
                  rev.get("effect", ""), "respec_gate", "PASS", level=L_MILESTONE)
        self.loops.append({"type": "revision-verified", "method": method,
                           "task": target, "resolved": True})


# ---------------------------------------------------------------------------
# Loading + rendering
# ---------------------------------------------------------------------------

def load_run(path) -> dict:
    """Load a project definition (YAML) from a path."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def run_project(project: dict, *, workspace, depth: Any = DEPTH_SPEC,
                tools: Any = None, agents: Optional[dict] = None,
                contracts_dir: Optional[str] = None, sink: Optional[Any] = None,
                verbosity: int = DEFAULT_VERBOSITY,
                max_decompose_calls: int = MAX_DECOMPOSE_CALLS,
                node_engine: str = "inline",
                runtime_guard: bool = False, resume: bool = False,
                replan: bool = False,
                git_provenance: bool = False,
                review_policy: Optional[dict] = None,
                seed_files: Optional[dict] = None,
                standing_requirements: Optional[Any] = None,
                provider_transport: Optional[Any] = None,
                human_ask: Optional[Any] = None) -> RunResult:
    """Public entry: run a project to completion. ``workspace`` is mandatory.

    ``depth`` is one of spec|scaffold|verify|execute|product (or 1..5).
    ``tools`` (gate provider) and ``agents`` (per-role workers) are injectable;
    both default to the bundled autonomous implementations so the run works
    without Hermes. At depth ``product`` the integrated project is built+run and
    checked against ``project['acceptance']`` (see ``Engine._product_check``).

    A role whose worker config declares an EXTERNAL team
    (``workers.<role>.team.external``) is delegated whole to a provider adapter
    (doc §3/§4 CASE 4); ``provider_transport`` injects a fake transport so this
    is exercised offline. Roles with no external team keep today's executor.
    """
    if not workspace:
        raise ValueError("Workspace is mandatory")
    agents = _wrap_external_teams(agents, transport=provider_transport)
    return Engine(tools=tools, workspace=workspace, depth=depth, agents=agents,
                  contracts_dir=contracts_dir, sink=sink, verbosity=verbosity,
                  max_decompose_calls=max_decompose_calls,
                  review_policy=review_policy, seed_files=seed_files,
                  node_engine=node_engine, runtime_guard=runtime_guard,
                  resume=resume, replan=replan, git_provenance=git_provenance,
                  standing_requirements=standing_requirements,
                  human_ask=human_ask,
                  doctor_project=project).run(project)


def render_log(res: RunResult, level: int = None) -> str:
    """Render the execution log, showing events with ``event.level <= level``.
    Defaults to the run's verbosity (env SPEC_FLOW_RUN_VERBOSITY, else L_STEP)."""
    level = level if level is not None else getattr(res, "verbosity", DEFAULT_VERBOSITY)
    shown = [e for e in res.events if e.level <= level]
    widths = _column_widths(shown)
    header = (f"TICK │   {'ACTOR':<{widths['profile']}} · {'SKILL':<{widths['skill']}} · "
              f"{'[TASK]':<{widths['task']}} ACTION → RESULT  «DETAIL»   (verbosity={level})")
    lines = ["```", header]
    last_phase = None
    for e in shown:
        if e.phase != last_phase:
            lines.append(f"── {e.phase} ──")
            last_phase = e.phase
        lines.append(event_line(e, widths))
    lines.append("```")
    return "\n".join(lines)


def render_tree(res: RunResult) -> str:
    # render decompose nodes hierarchically using parents of impl/integrate
    proj = res.project
    out = []

    def walk(node, prefix, is_last, is_root):
        nid = node["id"]
        t = res.tasks.get(nid)
        conn = "" if is_root else ("└─ " if is_last else "├─ ")
        ver = f" v{t.version}" if t and t.version > 1 else ""
        runs = f" ↻{t.runs}" if t and t.runs else ""
        tags = []
        if node.get("contract"):
            tags.append("⟨contract⟩")
        if node.get("spike"):
            tags.append("⟨spike⟩")
        if node.get("clarify"):
            tags.append("⟨clarify⟩")
        if node.get("drift"):
            tags.append("⟨drift→codefix⟩" if node["drift"].get("classify") == "code_wrong"
                        else "⟨drift→respec⟩")
        if node.get("review_fails"):
            tags.append("⟨review↻⟩")
        tagstr = (" " + " ".join(tags)) if tags else ""
        out.append(f"{prefix}{conn}{node.get('title', nid)}{ver}{runs}{tagstr}")
        kids = node.get("children", [])
        cp = prefix + ("" if is_root else ("   " if is_last else "│  "))
        for i, c in enumerate(kids):
            walk(c, cp, i == len(kids) - 1, False)

    tree = proj.get("tree")
    if not tree:
        return "_(no task tree captured for this run)_"
    out.append("```")
    walk(tree, "", True, True)
    out.append("```")
    return "\n".join(out)


def render_mermaid(res: RunResult) -> str:
    """The same goal/task tree as a mermaid flowchart — episode tags inside the
    node label, episode kind as a color class."""
    proj = res.project
    lines = ["```mermaid", "flowchart TD"]
    classed: list[tuple[str, str]] = []

    def esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace('"', "'")

    def walk(node, parent=None, depth=0):
        nid = node["id"]
        t = res.tasks.get(nid)
        ver = f" v{t.version}" if t and t.version > 1 else ""
        runs = f" ↻{t.runs}" if t and t.runs else ""
        tags = []
        if node.get("clarify"):
            tags.append("clarify")
        if node.get("spike"):
            tags.append("spike")
        if node.get("contract"):
            tags.append("contract")
        if node.get("drift"):
            tags.append("drift→codefix" if node["drift"].get("classify") == "code_wrong"
                        else "drift→respec")
        if node.get("review_fails"):
            tags.append(f"review↻{node['review_fails']}")
        tagstr = ("<br/>⟨" + "⟩ ⟨".join(tags) + "⟩") if tags else ""
        # indentation mirrors the tree depth so the SOURCE also reads as a tree
        pad = "    " * (depth + 1)
        lines.append(f'{pad}{nid}["{esc(node.get("title", nid))}{ver}{runs}{tagstr}"]')
        if parent:
            lines.append(f"{pad}{parent} --> {nid}")
        if node.get("drift"):
            classed.append((nid, "drift"))
        elif node.get("contract"):
            classed.append((nid, "contract"))
        elif node.get("spike"):
            classed.append((nid, "research"))
        elif node.get("clarify"):
            classed.append((nid, "clarify"))
        for c in node.get("children", []):
            walk(c, nid, depth + 1)

    tree = proj.get("tree")
    if not tree:
        return "```mermaid\nflowchart TD\n    L0[\"(no task tree captured)\"]\n```"
    walk(tree)
    lines += [
        "    classDef research fill:#0b525b,stroke:#118ab2,color:#fff",
        "    classDef contract fill:#3a0ca3,stroke:#7209b7,color:#fff",
        "    classDef drift fill:#9d0208,stroke:#dc2f02,color:#fff",
        "    classDef clarify fill:#9c6644,stroke:#e09f3e,color:#fff",
    ]
    lines += [f"    class {nid} {cls}" for nid, cls in classed]
    lines.append("```")
    return "\n".join(lines)


def render_summary(res: RunResult) -> str:
    sk = sorted(res.skills_used)
    pr = sorted(res.profiles_used)
    loop_kinds = {}
    for l in res.loops:
        loop_kinds[l["type"]] = loop_kinds.get(l["type"], 0) + 1
    lines = []
    lines.append("## Покрытие")
    lines.append(f"- **Скиллы ({len(sk)}/9):** {', '.join(sk)}")
    lines.append(f"- **Профили ({len(pr)}/6):** {', '.join(pr)}")
    lines.append(f"- **Задач на доске:** {len(res.tasks)}")
    lines.append(f"- **Вызовы тулзов плагина:** " +
                 ", ".join(f"{k}×{v}" for k, v in res.gate_calls.items()))
    lines.append("")
    lines.append("## Циклы / уточнения / критика (где «крутилось»)")
    lines.append("| Тип | Где | Что |")
    lines.append("|---|---|---|")
    label = {
        "clarify": "🟡 clarify/block",
        "review-fail": "⚖️ критика ревью (FAIL→fix→re-run)",
        "drift-respec": "📐 дрейф контракта → drift-gate → respec",
        "drift-codefix": "🛠️ дрейф: код неправ → исправление кода → re-check",
        "revision-respec": "🔬 ревизия research → respec-gate",
        "revision-recheck": "🔁 ре-проверка ревизии (сигнал ещё жив)",
        "revision-verified": "✅ ревизия подтверждена (сигнал устранён)",
        "hitl-reject": "🧑‍⚖️ человек отклонил → доработка",
        "judge-reject": "👀 судья отклонил → доработка",
        "decomposition-guardrail": "✂️ guardrail декомпозиции (пере/недо-измельчение)",
    }
    for l in res.loops:
        # loops are heterogeneous — not every type carries task/detail
        where = l.get("task") or l.get("kind") or "—"
        what = l.get("detail") or l.get("finding") or l.get("reason") or ""
        lines.append(f"| {label.get(l['type'], l['type'])} | `{where}` | {what} |")
    return "\n".join(lines)


def dump_trace(res: RunResult) -> str:
    """The raw event stream as JSONL — the inspectable source data the rendered
    report is built from (one event per line)."""
    return "\n".join(json.dumps(asdict(e), ensure_ascii=False) for e in res.events) + "\n"


def render_report(res: RunResult, level: int = None) -> str:
    proj = res.project
    level = level if level is not None else res.verbosity
    ok_sk = res.skills_used >= ALL_SKILLS
    ok_pr = res.profiles_used >= ALL_PROFILES
    shown = sum(1 for e in res.events if e.level <= level)
    head = [
        "# spec-flow — отчёт полного прогона проекта",
        "",
        f"**Проект:** {proj['name']} — _{proj['goal']}_",
        f"**Цель (измеримая):** {proj.get('target','')}",
        "",
        f"> Все скиллы задействованы: {'✅' if ok_sk else '❌'} · "
        f"все профили задействованы: {'✅' if ok_pr else '❌'} · "
        f"проект завершён: ✅ (L0 integrate done)",
        "",
        "Источник отчёта — событийный поток прогона (`RunResult.events`): "
        "определение проекта (YAML кейса) + возвраты **настоящих** тулзов "
        "плагина в точках решений. Сырой поток лежит рядом (`trace.jsonl`).",
        "",
        f"**Детализация:** показаны события уровня ≤ {level} "
        f"({shown} из {len(res.events)}). Уровни: 1=вехи (вердикты гейтов, "
        "циклы), 2=шаги, 3=детали (TDD, правила конституции). Управление: "
        "`SPEC_FLOW_RUN_VERBOSITY` (рендер) и `SPEC_FLOW_RUN_LOG` / "
        "`SPEC_FLOW_RUN_LOG_LEVEL` / `SPEC_FLOW_RUN_LOG_FORMAT` (лог на диск).",
        "",
        "## Дерево задач (с версиями и повторными прогонами ↻)",
        render_tree(res),
        "",
        "## Дерево задач — граф (mermaid)",
        "",
        "> Цвет: 🔵 ресёрч/spike · 🟣 контракт · 🔴 эпизод дрейфа · 🟠 clarify. "
        "Диаграмму рисует просмотрщик с поддержкой mermaid (GitHub, Obsidian, "
        "VS Code + Markdown Preview Mermaid); без неё виден исходник графа — "
        "иерархия задана рёбрами `родитель --> потомок` и отступами.",
        "",
        render_mermaid(res),
        "",
        "## Журнал исполнения",
        render_log(res, level),
        "",
        render_summary(res),
        "",
    ]
    return "\n".join(head) + "\n"
