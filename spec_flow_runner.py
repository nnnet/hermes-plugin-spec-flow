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

Depth ladder (`spec` < `scaffold` < `verify` < `execute`):
  * spec     — constitution, per-node specs/plans, frozen contracts, MANIFEST;
  * scaffold — + code & test scaffolds per leaf, a commit journal;
  * verify   — + actually run the test files (pytest) and record results;
  * execute  — + an injected implementer agent writes real code / modifies a
               real project (raises if no implementer agent is provided).
"""

from __future__ import annotations

import hashlib
import json
import os
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

# Event verbosity levels — what each emit() is worth. Lower = more important.
L_MILESTONE = 1   # gate verdicts, loops (clarify/critique/drift/respec), phase signals
L_STEP = 2        # routine worker steps (read handoff, design, integrate)
L_DETAIL = 3      # fine-grained detail (TDD red/green, individual constitution rules)
DEFAULT_VERBOSITY = int(os.environ.get("SPEC_FLOW_RUN_VERBOSITY", str(L_STEP)))

# Execution depth ladder.
DEPTHS = {"spec": 1, "scaffold": 2, "verify": 3, "execute": 4}
DEPTH_SPEC, DEPTH_SCAFFOLD, DEPTH_VERIFY, DEPTH_EXECUTE = 1, 2, 3, 4


def _depth_int(d: Any) -> int:
    if isinstance(d, int):
        return d
    if d in DEPTHS:
        return DEPTHS[d]
    raise ValueError(f"unknown depth {d!r}; expected one of {sorted(DEPTHS)} or 1..4")


def _default_implementer(ctx: dict) -> None:
    raise NotImplementedError(
        "depth 'execute' requires an injected implementer agent "
        "(a real LLM / Hermes worker that writes code & modifies the project)")


# Autonomous default agents per role. Spec/scaffold/verify are fully handled by
# the deterministic engine; only 'execute' consults an agent (the implementer).
# Override any via the run's ``agents=`` parameter.
DEFAULT_AGENTS = {"implementer": _default_implementer}

PROFILE_ICON = {
    "spec-decomposer": "🧩",
    "researcher": "🔬",
    "spec-contract": "📐",
    "spec-reviewer": "⚖️",
    "implementer": "🛠️",
    "verifier": "✅",
}
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
            self._fh = open(self.path, "w", encoding="utf-8")
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
            self._fh.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
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

    def __init__(self, root: Optional[str] = None, enabled: Optional[bool] = None):
        self.root = root or os.environ.get("SPEC_FLOW_RUN_WORKSPACE")
        self.enabled = enabled if enabled is not None else bool(self.root)
        self.artifacts: list[dict] = []
        self.commits: list[dict] = []

    @classmethod
    def from_env(cls) -> "Workspace":
        return cls()

    def open(self) -> "Workspace":
        if self.enabled and self.root:
            p = Path(self.root)
            if p.exists():
                shutil.rmtree(p)
            p.mkdir(parents=True, exist_ok=True)
        return self

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

    def commit(self, msg: str, files: list[str]) -> None:
        if self.enabled:
            self.commits.append({"n": len(self.commits) + 1, "message": msg, "files": files})

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


class Engine:
    def __init__(self, tools: Any = None, *, workspace: Any,
                 depth: Any = DEPTH_SPEC, agents: Optional[dict] = None,
                 contracts_dir: Optional[str] = None,
                 sink: Optional[Any] = None, verbosity: int = DEFAULT_VERBOSITY):
        if not workspace:
            raise ValueError("Workspace is mandatory — pass a path or a Workspace")
        # tools (gate provider) is injectable; default to the bundled gates so
        # the runner works standalone without Hermes.
        self.tools = tools if tools is not None else _gates
        self.depth = _depth_int(depth)
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

    def _leaf(self, metrics):
        self.gate_calls["leaf_check"] += 1
        return json.loads(self.tools._handle_leaf_check(dict(metrics)))

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

    # -- run ---------------------------------------------------------------
    def run(self, project: dict) -> RunResult:
        try:
            if self.sink is not None:
                self.sink.open()
            self.workspace.open()
            return self._run(project)
        finally:
            if self.sink is not None:
                self.sink.close()
            self.workspace.finalize()

    def _run(self, project: dict) -> RunResult:
        # Note: the contract validator (CONTRACT_VALIDATORS) is configured by the
        # caller on the gate provider — the runner does not hardcode it.
        self._completed = 0
        self._target = project.get("target", "")

        # Phase 0-1: requirements (spec-decomposer + spec-requirements)
        self.task("L0:req", "Requirements & constitution", "requirements", "spec-decomposer", "spec-requirements")
        pol = self._policy(project.get("policy", {}))
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

        root = project["tree"]
        self._visit(root, depth=0, contract_ctx=None, phase="decompose", parent=None)

        # Phase: continuous research revision lane
        self._revision(project.get("revision"))

        # Depth 'verify' and up: actually run the materialised test suite.
        if self.depth >= DEPTH_VERIFY:
            self._verify_tests()

        # Final L0 integration
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  "L0 integrate done = project COMPLETE", "all subtrees merged & verified",
                  level=L_MILESTONE)
        if "L0:integrate" in self.tasks:
            self.tasks["L0:integrate"].status = "done"

        return RunResult(project, self.events, self.tasks, self.skills, self.profiles,
                         self.loops, self.gate_calls, self.verbosity, self.depth,
                         getattr(self.workspace, "root", None))

    # -- recursion ---------------------------------------------------------
    def _visit(self, node: dict, depth: int, contract_ctx: Optional[dict], phase: str,
               parent: Optional[str] = None):
        nid = node["id"]
        title = node.get("title", nid)
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

        # the gate: leaf vs branch
        leaf_out = self._leaf(node["metrics"])
        verdict = leaf_out["verdict"]
        reasons = leaf_out["reasons"]
        self.emit("decompose", "spec-decomposer", "spec-flow-decompose", nid,
                  "leaf_check", "; ".join(reasons) or "within all thresholds",
                  "leaf_check", verdict, level=L_MILESTONE)

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
                self._visit(child, depth + 1, child_contract_ctx, phase, parent=title)
                child_ids.append(child["id"])

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
            self._leaf_pipeline(node, contract_ctx, depth, parent)

        self.tasks[nid].status = "done"
        self._completed += 1
        # returning up a level — check the research lane trigger
        self._research_tick(depth)

    def _leaf_pipeline(self, node: dict, contract_ctx: Optional[dict],
                       depth: int = 0, parent: Optional[str] = None):
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
            self.agents["implementer"]({"node": nid, "title": title,
                                        "workspace": self.workspace, "spec": f"specs/{_snake(nid)}.md"})
            code_rel, test_rel = f"src/{_snake(nid)}.py", f"tests/test_{_snake(nid)}.py"
        elif self.depth >= DEPTH_SCAFFOLD:
            code_rel = self.workspace.code(nid, title)
            test_rel = self.workspace.test(nid, title)
        impl = f"{nid}:impl"
        self.task(impl, f"Implement {title}", "impl", "implementer", "spec-implement", parents=[nid])
        self.emit("implement", "implementer", "spec-implement", impl,
                  "design → bottom-up plan (DB→logic→API→tests)", title)
        self.emit("implement", "implementer", "spec-implement", impl,
                  "TDD: write test (RED) → minimal impl → test (GREEN)", "", level=L_DETAIL)

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

        # review critique loop
        review = f"{nid}:review"
        self.task(review, f"Review {title}", "review", "spec-reviewer", "spec-reviewer", parents=[impl])
        fails = int(node.get("review_fails", 0))
        for i in range(fails):
            self.emit("review", "spec-reviewer", "spec-reviewer", review,
                      "impl-review (spec-conformance)", "FAIL: missing edge-case handling on error path",
                      "", "FAIL", level=L_MILESTONE)
            self.emit("review", "implementer", "spec-implement", impl,
                      "fix per critique → unblock → re-run", "")
            self.tasks[impl].runs += 1
            self.loops.append({"type": "review-fail", "task": review, "detail": "spec-conformance critique"})
        self.emit("review", "spec-reviewer", "spec-reviewer", review,
                  "impl-review → quality gate", "", "", "PASS", level=L_MILESTONE)
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

    def _research_tick(self, depth: int):
        # cheap signal: only check at the moment we return to the root level
        if depth != 0:
            return

    def _revision(self, rev: Optional[dict]):
        if not rev:
            return
        out = self._research(rev.get("trigger", "on_level_return"), self._completed, 0)
        self.emit("revision", "researcher", "spec-research", "revision",
                  "research_trigger_check", f"fired_by={out['fired_by']}",
                  "research_trigger_check", "trigger" if out["trigger"] else "—",
                  level=L_MILESTONE)
        if not out["trigger"]:
            return
        self.emit("revision", "researcher", "spec-research", "revision",
                  "REVISION finding (upstream impact)", rev["finding"], level=L_MILESTONE)
        target = rev["invalidates"]
        self.emit("respec", "spec-reviewer", "respec-gate", target,
                  "respec-gate: change the cause first, version-bump, re-derive only affected subtree",
                  rev["effect"], level=L_MILESTONE)
        self.loops.append({"type": "revision-respec", "task": target, "detail": rev["finding"]})
        # version-bump the target and re-run its leaves under the new spec
        if target in self.tasks:
            self.tasks[target].version += 1
            self.tasks[target].runs += 1
        for tid, t in list(self.tasks.items()):
            if t.parents and target in t.parents and t.kind in {"impl", "decompose"}:
                t.version += 1
                t.runs += 1
                self.emit("revision", "implementer", "spec-implement", tid,
                          "re-derive under superseded spec", f"{target} v2")


# ---------------------------------------------------------------------------
# Loading + rendering
# ---------------------------------------------------------------------------

def load_run(path) -> dict:
    """Load a project definition (YAML) from a path."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def run_project(project: dict, *, workspace, depth: Any = DEPTH_SPEC,
                tools: Any = None, agents: Optional[dict] = None,
                contracts_dir: Optional[str] = None, sink: Optional[Any] = None,
                verbosity: int = DEFAULT_VERBOSITY) -> RunResult:
    """Public entry: run a project to completion. ``workspace`` is mandatory.

    ``depth`` is one of spec|scaffold|verify|execute (or 1..4). ``tools`` (gate
    provider) and ``agents`` (per-role workers) are injectable; both default to
    the bundled autonomous implementations so the run works without Hermes.
    """
    if not workspace:
        raise ValueError("Workspace is mandatory")
    return Engine(tools=tools, workspace=workspace, depth=depth, agents=agents,
                  contracts_dir=contracts_dir, sink=sink, verbosity=verbosity).run(project)


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

    out.append("```")
    walk(proj["tree"], "", True, True)
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

    walk(proj["tree"])
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
    }
    for l in res.loops:
        lines.append(f"| {label.get(l['type'], l['type'])} | `{l['task']}` | {l['detail']} |")
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
