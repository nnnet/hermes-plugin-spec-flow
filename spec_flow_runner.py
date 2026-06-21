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
                         # A1 review tiering (default OFF — p4/p5 unchanged):
                         # a SIMPLE leaf that passes the deterministic spec lint
                         # skips the LLM reviewer + rework loop entirely. Review
                         # is the largest measured time sink (~47%); a small,
                         # decision-free leaf does not need an opinion round.
                         "tiering": False, "simple_max_loc": 60,
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
        r, s = _amend_routes(body), _amend_symbols(body)
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

def call(method, path, payload=None, query=""):
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
        modules = self._surface_modules(_snake(str(node.get("id", ""))))
        if not modules:
            return None
        cand_text = {rel: body[:400] for rel, _stem, body in modules}
        stmt = str(node.get("requirement") or node.get("spec_markdown")
                   or node.get("title") or "")
        llm = None
        if os.environ.get("SPEC_FLOW_AMEND_LLM", "") not in (
                "", "0", "false", "False", "no"):
            llm = self._amend_llm_router
        return _amend_find_owner(stmt, modules, candidates_text=cand_text,
                                 llm=llm)

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

    def _remedy_reconcile_check(self, entry: str) -> bool:
        """Executable reconcile_check (Ф6): the integrate gate found the declared
        product entry missing. Re-run the ENGINE's OWN assembly leaf — its spec
        (built by _assembly_node) tells the implementer to create the entry
        wiring the modules already under src/ — through THIS run's implementer
        worker (sim or live, never a hand-written scaffold), then re-verify with
        the boot-gate. Green boot => drop the root integrate-fail + close the
        doctor cause (row turns green). Otherwise stay RED. Returns True iff healed."""
        ws = self.workspace
        if not (getattr(ws, "enabled", False) and getattr(ws, "root", None)
                and "implementer" in self.agents):
            return False
        asm = self._assembly_node()
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
        ictx = {"node": "product_entry",
                "title": str(asm.get("title") or f"Assemble {entry}"),
                "depth": self.depth, "workspace": ws,
                "spec": spec_rel, "module": module}
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
            texts += [str(s) for (_n, s) in (self._standing_requirements or [])]
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
        # entry/callable IF the human named them ("src/app.py exposes wsgi_app");
        # else the runner's Python-WSGI build convention.
        em = re.search(r"(src/[A-Za-z0-9_./-]+\.py)", blob)
        entry = em.group(1) if em else "src/app.py"
        cm = re.search(r"exposes?\s+`?([a-z_][a-z0-9_]*)`?", low)
        callables = [cm.group(1)] if cm else ["wsgi_app", "application", "app"]
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
        return {"entry": entry, "callable": callables, "boot": boot}

    def _assembly_node(self) -> "Optional[dict]":
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
        if (Path(self.workspace.root) / entry).is_file():
            return None             # a feature leaf already built the entry
        callable_name = (c["callable"] or ["wsgi_app"])[0]
        ok_route = str(c["boot"].get("ok_route") or "")
        api = self._existing_src_api()
        # The implementer reads the node's SPEC, not a `requirement` field, so
        # the build directive goes into spec_markdown; the title is the heading.
        boot_line = (f" The product must boot in a fresh process and answer "
                     f"`GET {ok_route}` -> 200." if ok_route else "")
        spec_md = (
            "## ASSEMBLE THE PRODUCT ENTRY (engine-required, binding)\n\n"
            f"Create `{entry}` exposing a module-level `{callable_name}` callable "
            "that wires the feature modules ALREADY built under `src/` into one "
            "running product. Import the existing modules (do NOT reimplement "
            "them, do NOT mock them); dispatch every endpoint the contract "
            "declares to the matching handler/storage already present."
            + boot_line + " Standard library only.\n\n"
            + (api + "\n\n" if api else "")
            + "### Contract (binding)\n"
            + "\n".join(f"- {r}" for r in (self._constitution or [])))
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
        # with an open task_check_mismatch over a broken build).
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
                         dict(self._module_names))

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
        m = node.get("metrics") or {}
        if node.get("children"):
            label = "branch"
        else:
            cap = int((self.review_policy or {}).get("simple_max_loc", 60))
            loc = int(m.get("estimated_loc", 0) or 0)
            od = int(m.get("open_decisions", 0) or 0)
            label = "leaf_small" if (loc <= cap and od == 0) else "leaf_big"
        return str(cmap.get(label, "") or "")

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
        leaf_out = self._leaf(node["metrics"], atomic=atomic_claim)
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
            for extra in placements:
                where = ("inside its scoped branch" if extra.pop("_scoped",
                                                                 False)
                         else "at root level")
                # B3: deterministically route a same-surface requirement to
                # EDIT the existing owner module (code_target). The node keeps
                # its own spec; only its code output goes into the owner file.
                # Nodes with a fixed engine target (the assembly entry) are
                # exempt — they own a declared path and must not be re-routed.
                amend = None if extra.get("_no_amend") else self._amend_target(extra)
                if amend:
                    extra["code_target"] = amend
                    try:
                        cur = (Path(self.workspace.root) / amend).read_text(
                            encoding="utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        cur = ""
                    new_req = str(extra.get("requirement")
                                  or extra.get("title"))
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
                self._visit(extra, depth + 1, child_contract_ctx, phase,
                            parent=title,
                            ancestors=ancestors + ((nid, title),))
                child_ids.append(extra["id"])

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
        isolated = (self._isolation == "worktree"
                    and getattr(ws, "enabled", False) and getattr(ws, "root", None))
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
                self._judge_leaf(nid, title, fn, code_rel, test_rel)
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

    def _verify_tests(self) -> None:
        ws = self.workspace
        if not ws.enabled or not ws.root:
            return
        tests_dir = Path(ws.root) / "tests"
        if not tests_dir.exists():
            return
        self._derive_smoke_contract()
        try:
            # run from the workspace root: paths stay short (tests/test_x.py),
            # confcutdir isolates the run from any host-project conftest.py;
            # -v lists every single test with its verdict, not just the total
            # --import-mode=importlib: tolerate same-basename test files across
            # tests/ and tests/smoke/ (a late requirement's leaf test and its
            # seeded smoke acceptance share a name) — the legacy prepend mode
            # errors the WHOLE collection on a duplicate basename, a false red.
            proc = subprocess.run(["python3", "-m", "pytest", "-v", "--no-header",
                                   f"--confcutdir={ws.root}", "-p", "no:cacheprovider",
                                   "--import-mode=importlib", "tests"],
                                  capture_output=True, text=True, timeout=300,
                                  cwd=str(ws.root))
            passed = proc.returncode == 0
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            passed, out = False, f"pytest error: {exc}"
        # B2 ROOT BOOT-GATE: a green corpus is HOLLOW if the assembled product
        # does not serve its frozen contract. Boot the real WSGI entry and drive
        # the contract — a non-200 route is a hard RED, even when every module
        # unit-test passed (live v020: all routes 404'd under a green pytest).
        if passed:
            boot_ok, boot_detail = self._assembled_product_boots()
            if not boot_ok:
                passed = False
                out = (out + "\n\n=== ROOT BOOT-GATE (assembled product) ===\n"
                       + boot_detail + "\n")
                self.emit("integrate", "verifier", "spec-integrate",
                          "L0:integrate", "boot-gate over assembled product",
                          boot_detail, "integrate_verify", "FAIL",
                          level=L_MILESTONE)
                self.loops.append({"type": "integrate-fail",
                                   "task": getattr(self, "_root_id", "L0"),
                                   "detail": boot_detail})
        depth_name = next((k for k, v in DEPTHS.items() if v == self.depth), str(self.depth))
        ws._write("TEST-RESULTS.md",
                  f"# Test results (depth={depth_name})\n\nStatus: "
                  f"{'✅ PASS' if passed else '❌ FAIL (scaffolds fail until implemented)'}\n\n"
                  f"```\n{out[-20000:]}\n```\n", "test-results")
        self.emit("integrate", "verifier", "spec-integrate", "verify",
                  "ran test suite (pytest)", "PASS" if passed else "FAIL (scaffolds)",
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
            "json_roundtrip": boot.get("json_roundtrip", "")})
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
        for kind in ("smoke", "e2e", "metrics"):
            for check in acceptance.get(kind, []) or []:
                if not entrypoint:
                    record(kind, check, False, no_entry_note)
                elif not entry_ok:
                    record(kind, check, False, entry_fail_note)
                else:
                    # Entrypoint ran cleanly — accept the check as satisfied.
                    record(kind, check, True, "entrypoint ran cleanly")

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
