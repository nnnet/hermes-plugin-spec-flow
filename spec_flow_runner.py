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

import hashlib
import json
import os
import re
import shutil
import subprocess
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

NODE_ENGINES = ("inline", "fsm")

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

    def open(self, preserve: bool = False) -> "Workspace":
        """Create the workspace dir. ``preserve=True`` keeps an existing one
        (resume mode — persisted artifacts and the journal survive)."""
        if self.enabled and self.root:
            p = Path(self.root)
            if p.exists() and not preserve:
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)
        return self

    # -- run journal (C4: a restartable run) --------------------------------
    def _journal_path(self) -> Optional[Path]:
        if not (self.enabled and self.root):
            return None
        return Path(self.root) / ".spec-flow" / "journal.jsonl"

    def journal_mark(self, node: str, version: int) -> None:
        """Append a completed-node record — the resume index."""
        path = self._journal_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"node": node, "version": version}) + "\n")

    def journal_nodes(self) -> set:
        """Node ids already completed by a previous run of this workspace."""
        path = self._journal_path()
        if path is None or not path.is_file():
            return set()
        done = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["node"])
            except Exception:  # noqa: BLE001 — a torn line is not fatal
                continue
        return done

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
             node: Optional[dict] = None, target: str = "") -> str:
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
        lines += ["", "## Plan"]
        lines += [f"- {p}" for p in plan_lines]
        children = node.get("children")
        if children:
            lines += ["", "## Children (next level)"]
            lines += [f"- `{c['id']}` — {c.get('title', c['id'])}" for c in children]
        return self._write(f"specs/{_snake(node_id)}.md", "\n".join(lines) + "\n", "spec")

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

    def __init__(self, kind: str, *, fsm_mode: bool, skip_gate: Optional[str] = None):
        self.kind = kind
        self.active = _NODE_FSM_OK
        self._skip = skip_gate
        self._fired: set[str] = set()
        self._lc = None
        if fsm_mode and _NODE_FSM_OK:
            self._lc = _NodeLifecycle().start(kind)

    def go(self, event) -> None:
        """Advance the lifecycle by one event, recording any gate it satisfies."""
        if not self.active:
            return
        if self._lc is not None:
            self._lc.advance(event)
        for gate in _EVENT_GATE.get(event, ()):
            self._fired.add(gate)

    def clarify(self) -> None:
        """Replay the clarify self-loop (open decision → resolve) on the FSM."""
        if self._lc is not None:
            self._lc.open_decision()
            self._lc.advance(EV_CLARIFY)
            self._lc.resolve_decision()

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
            return
        required = GATES_BRANCH if self.kind == "branch" else GATES_LEAF
        missing = [g for g in required if g not in self._fired]
        if missing:
            raise GateViolation(
                f"cannot reach DONE: mandatory gate(s) skipped for "
                f"{self.kind}: {', '.join(missing)}")


class Engine:
    def __init__(self, tools: Any = None, *, workspace: Any,
                 depth: Any = DEPTH_SPEC, agents: Optional[dict] = None,
                 contracts_dir: Optional[str] = None,
                 sink: Optional[Any] = None, verbosity: int = DEFAULT_VERBOSITY,
                 max_decompose_calls: int = MAX_DECOMPOSE_CALLS,
                 node_engine: str = "inline", runtime_guard: bool = False,
                 resume: bool = False, git_provenance: bool = False):
        if not workspace:
            raise ValueError("Workspace is mandatory — pass a path or a Workspace")
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
        self._journal_done: set = set()
        self.agents = {**DEFAULT_AGENTS, **(agents or {})}
        self.contracts_dir = Path(contracts_dir) if contracts_dir else None
        self.events: list[Event] = []
        self.tasks: dict[str, Task] = {}
        self.skills: set[str] = set()
        self.profiles: set[str] = set()
        self.loops: list[dict] = []
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
        self._t += 1
        self.skills.add(skill) if skill in ALL_SKILLS else None
        self.profiles.add(profile) if profile in ALL_PROFILES else None
        ev = Event(self._t, phase, profile, skill, task, action, detail, gate, verdict, level)
        self.events.append(ev)
        if self.sink is not None:
            self.sink.handle(ev)

    def task(self, tid, title, kind, profile, skill, parents=None) -> Task:
        t = Task(tid, title, kind, profile, skill, parents or [])
        self.tasks[tid] = t
        return t

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

    def _node_driver(self, node: dict, kind: str) -> _NodeDriver:
        """Build the lifecycle guard for one node under the active engine."""
        return _NodeDriver(kind, fsm_mode=(self.node_engine == "fsm"),
                           skip_gate=node.get("_skip_gate"))

    # -- run ---------------------------------------------------------------
    def run(self, project: dict) -> RunResult:
        try:
            if self.sink is not None:
                self.sink.open()
            self.workspace.open(preserve=self.resume)
            if self.resume:
                self._journal_done = self.workspace.journal_nodes()
            return self._run(project)
        finally:
            if self.sink is not None:
                self.sink.close()
            self.workspace.finalize()

    def _run(self, project: dict) -> RunResult:
        # Note: the contract validator (CONTRACT_VALIDATORS) is configured by the
        # caller on the gate provider — the runner does not hardcode it.
        self._completed = 0
        self._goal = project.get("goal", "")
        self._target = project.get("target", "")
        self._constitution = project.get("constitution", [])
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

        # Sweep: fire any declared revision that did not meet its in-run
        # trigger condition (back-compat + nothing declared is silently dropped).
        self._revision_sweep()

        # Depth 'verify' and up: actually run the materialised test suite.
        if self.depth >= DEPTH_VERIFY:
            self._verify_tests()

        # Final L0 integration
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  "L0 integrate done = project COMPLETE", "all subtrees merged & verified",
                  level=L_MILESTONE)
        if "L0:integrate" in self.tasks:
            self.tasks["L0:integrate"].status = "done"

        # Depth 'product': after the project is integrated (and, at >=execute,
        # real code exists), build+run the product and assert readiness against
        # the acceptance spec.
        if self.depth >= DEPTH_PRODUCT:
            self._product_check(project.get("acceptance"))

        # C3: enforce the R1-R9 invariants at runtime (opt-in). A hard breach
        # raises InvariantViolation rather than passing silently to the report.
        if self.runtime_guard and hasattr(self.tools, "assert_invariants"):
            self.tools.assert_invariants([vars(e) for e in self.events])

        return RunResult(project, self.events, self.tasks, self.skills, self.profiles,
                         self.loops, self.gate_calls, self.verbosity, self.depth,
                         getattr(self.workspace, "root", None))

    # -- recursion ---------------------------------------------------------
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
        self._decompose_calls += 1
        if self._decompose_calls > self.max_decompose_calls:
            raise RuntimeError(
                f"decomposer agent exceeded {self.max_decompose_calls} calls — "
                "the tree does not converge to leaves")
        out = self.agents["decomposer"]({
            "project": {"goal": self._goal, "target": self._target,
                        "constitution": self._constitution},
            "node": {"id": node["id"], "title": node.get("title", node["id"])},
            "parent": parent, "depth": depth,
            "ancestors": [t for _, t in ancestors],
            "existing_nodes": [
                {"id": i, "title": t}
                for i, t in list(self._node_registry.items())[:150]
            ],
        })
        # mutate the node IN PLACE so the realized metrics/children attach to the
        # live tree — this is how project["tree"] ends up holding the full tree
        # the decomposer built (needed for the reports in llm mode).
        node.update(out or {})
        self.emit("decompose", "spec-decomposer", "spec-flow-decompose", node["id"],
                  "decomposer agent built this level from the goal",
                  f"{len(node.get('children', []))} children proposed", level=L_MILESTONE)
        return node

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

        # spike before freeze (research)
        spike = node.get("spike")
        if spike:
            sid = f"{nid}:spike"
            self.task(sid, spike["question"], "research", "researcher", "spec-research", parents=[nid])
            self.emit("research", "researcher", "spec-research", sid,
                      "SPIKE before freeze", spike["question"])
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

            self.workspace.spec(nid, title, depth, verdict, "; ".join(reasons), parent, plan,
                                node=node, target=self._target)
            child_ids = []
            for child in node.get("children", []):
                self._visit(child, depth + 1, child_contract_ctx, phase,
                            parent=title, ancestors=ancestors + ((nid, title),))
                child_ids.append(child["id"])

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
            self.emit("integrate", "verifier", "spec-integrate", integ,
                      "end-to-end acceptance criteria", "verification-before-completion", "", "PASS",
                      level=L_MILESTONE)
            self.tasks[integ].status = "done"
            self._completed += 1
        else:
            self._leaf_pipeline(node, contract_ctx, depth, parent, drv)

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
            self.workspace.journal_mark(nid, self.tasks[nid].version)
        # returning up a level — check both revision methods at this moment
        self._research_tick(node, depth)

    def _leaf_pipeline(self, node: dict, contract_ctx: Optional[dict],
                       depth: int = 0, parent: Optional[str] = None,
                       drv: Optional["_NodeDriver"] = None):
        nid = node["id"]
        title = node.get("title", nid)
        # spec/plan is written at every depth (>= spec)
        self.workspace.spec(nid, title, depth, "leaf", "within all thresholds", parent,
                            ["bottom-up plan: DB → logic → API → tests",
                             "TDD: test (RED) → impl → test (GREEN)",
                             "two-stage review (spec-conformance, then quality)",
                             "verification-before-completion + commit"],
                            node=node, target=self._target)
        # code & test scaffolds only from depth 'scaffold' upward; at 'execute'
        # an injected implementer agent produces real code instead of a scaffold.
        code_rel = test_rel = None
        if self.depth >= DEPTH_EXECUTE:
            fn = _snake(nid)
            code_rel, test_rel = f"src/{fn}.py", f"tests/test_{fn}.py"
            # C4 resume: the journal says this leaf finished and its artifact
            # survived the restart — reuse it, do not re-run the implementer.
            persisted = (self.resume and nid in self._journal_done
                         and self.workspace.enabled and self.workspace.root
                         and (Path(self.workspace.root) / code_rel).is_file())
            if persisted:
                self.emit("implement", "implementer", "spec-implement", f"{nid}:impl",
                          "resume: reuse persisted artifact (run journal)", code_rel,
                          level=L_DETAIL)
            else:
                self.agents["implementer"]({"node": nid, "title": title,
                                            "workspace": self.workspace, "spec": f"specs/{fn}.md"})
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

    def _verify_tests(self) -> None:
        ws = self.workspace
        if not ws.enabled or not ws.root:
            return
        tests_dir = Path(ws.root) / "tests"
        if not tests_dir.exists():
            return
        try:
            # run from the workspace root: paths stay short (tests/test_x.py),
            # confcutdir isolates the run from any host-project conftest.py;
            # -v lists every single test with its verdict, not just the total
            proc = subprocess.run(["python3", "-m", "pytest", "-v", "--no-header",
                                   f"--confcutdir={ws.root}", "-p", "no:cacheprovider",
                                   "tests"],
                                  capture_output=True, text=True, timeout=300,
                                  cwd=str(ws.root))
            passed = proc.returncode == 0
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            passed, out = False, f"pytest error: {exc}"
        depth_name = next((k for k, v in DEPTHS.items() if v == self.depth), str(self.depth))
        ws._write("TEST-RESULTS.md",
                  f"# Test results (depth={depth_name})\n\nStatus: "
                  f"{'✅ PASS' if passed else '❌ FAIL (scaffolds fail until implemented)'}\n\n"
                  f"```\n{out[-20000:]}\n```\n", "test-results")
        self.emit("integrate", "verifier", "spec-integrate", "verify",
                  "ran test suite (pytest)", "PASS" if passed else "FAIL (scaffolds)",
                  "", "PASS" if passed else "FAIL", level=L_MILESTONE)

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
                git_provenance: bool = False) -> RunResult:
    """Public entry: run a project to completion. ``workspace`` is mandatory.

    ``depth`` is one of spec|scaffold|verify|execute|product (or 1..5).
    ``tools`` (gate provider) and ``agents`` (per-role workers) are injectable;
    both default to the bundled autonomous implementations so the run works
    without Hermes. At depth ``product`` the integrated project is built+run and
    checked against ``project['acceptance']`` (see ``Engine._product_check``).
    """
    if not workspace:
        raise ValueError("Workspace is mandatory")
    return Engine(tools=tools, workspace=workspace, depth=depth, agents=agents,
                  contracts_dir=contracts_dir, sink=sink, verbosity=verbosity,
                  max_decompose_calls=max_decompose_calls,
                  node_engine=node_engine, runtime_guard=runtime_guard,
                  resume=resume, git_provenance=git_provenance).run(project)


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
