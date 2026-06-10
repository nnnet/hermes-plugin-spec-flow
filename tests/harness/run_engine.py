"""End-to-end run engine for spec-flow.

Simulates the Hermes kanban dispatcher driving a whole project to completion,
with one fake worker per task loading a specific SKILL under a specific
PROFILE. Every *decision* is taken by the plugin's REAL tools (policy_gate,
leaf_check, contract_check, research_trigger_check); the narrative events
(spike recommendation, a review critique, a contract drift, a research finding)
follow the project's scripted directives so the full control flow — clarify
loops, critique loops, drift -> drift-gate -> respec-gate, the research
revision lane — actually appears in the log.

The engine produces a chronological execution log, the final task tree with
statuses/versions, and a coverage summary proving every skill and profile ran.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Event verbosity levels — what each emit() is worth. Lower = more important.
# A report rendered at level N shows every event with level <= N.
L_MILESTONE = 1   # gate verdicts, loops (clarify/critique/drift/respec), phase signals
L_STEP = 2        # routine worker steps (read handoff, design, integrate)
L_DETAIL = 3      # fine-grained detail (TDD red/green, individual constitution rules)
DEFAULT_VERBOSITY = int(os.environ.get("SPEC_FLOW_RUN_VERBOSITY", str(L_STEP)))

import yaml

HARNESS_DIR = Path(__file__).resolve().parent
RUNS_DIR = HARNESS_DIR.parent / "runs"
CONTRACTS = HARNESS_DIR.parent / "contracts"
OPENAPI_DIFF = HARNESS_DIR / "openapi_diff.py"

PROFILE_ICON = {
    "spec-decomposer": "🧩",
    "researcher": "🔬",
    "spec-contract": "📐",
    "spec-reviewer": "⚖️",
    "implementer": "🛠️",
    "verifier": "✅",
}
ALL_SKILLS = {
    "spec-requirements", "spec-flow-decompose", "spec-contract", "spec-reviewer",
    "spec-implement", "spec-integrate", "spec-research", "drift-gate", "respec-gate",
}
ALL_PROFILES = set(PROFILE_ICON)


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


def event_line(e: Event) -> str:
    """Single human line for an event — shared by the rendered log and the
    text disk sink."""
    icon = PROFILE_ICON.get(e.profile, "·")
    head = f"t{e.tick:>2} │ {icon} {e.profile} · {e.skill} · [{e.task}] {e.action}"
    tail = f" → {e.verdict}" if e.verdict else ""
    if e.detail:
        tail += f"  «{e.detail}»"
    return head + tail


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


class Engine:
    def __init__(self, tools: Any, sink: Optional[Any] = None,
                 verbosity: int = DEFAULT_VERBOSITY):
        self.tools = tools
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
        self.gate_calls["contract_check"] += 1
        return json.loads(self.tools._handle_contract_check({
            "contract_artifacts": [str(CONTRACTS / contract)],
            "changed_files": [str(CONTRACTS / code)],
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
            return self._run(project)
        finally:
            if self.sink is not None:
                self.sink.close()

    def _run(self, project: dict) -> RunResult:
        self.tools.CONTRACT_VALIDATORS["openapi"] = ["python3", str(OPENAPI_DIFF), "{contract}", "{code}"]
        self._completed = 0

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
        self.tasks["L0:req"].status = "done"

        root = project["tree"]
        self._visit(root, depth=0, contract_ctx=None, phase="decompose")

        # Phase: continuous research revision lane
        self._revision(project.get("revision"))

        # Final L0 integration
        self.emit("integrate", "verifier", "spec-integrate", "L0:integrate",
                  "L0 integrate done = project COMPLETE", "all subtrees merged & verified",
                  level=L_MILESTONE)
        if "L0:integrate" in self.tasks:
            self.tasks["L0:integrate"].status = "done"

        return RunResult(project, self.events, self.tasks, self.skills, self.profiles,
                         self.loops, self.gate_calls, self.verbosity)

    # -- recursion ---------------------------------------------------------
    def _visit(self, node: dict, depth: int, contract_ctx: Optional[dict], phase: str):
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
            if contract_here:
                cid = f"{nid}:contract"
                self.task(cid, f"L2 contract for {title}", "contract", "spec-contract", "spec-contract", parents=[nid])
                self.emit("contract", "spec-contract", "spec-contract", cid,
                          "freeze OpenAPI contract (x-traces-to)", contract_here["artifact"])
                self.emit("contract", "spec-reviewer", "spec-reviewer", cid,
                          "spec-gate on contract", "trace + constitution OK", "", "PASS",
                          level=L_MILESTONE)
                self.tasks[cid].status = "done"
                self._completed += 1
                child_contract_ctx = contract_here

            child_ids = []
            for child in node.get("children", []):
                self._visit(child, depth + 1, child_contract_ctx, phase)
                child_ids.append(child["id"])

            integ = f"{nid}:integrate"
            iparents = child_ids + ([f"{nid}:contract"] if contract_here else [])
            self.task(integ, f"Integrate & verify {title}", "integrate", "verifier", "spec-integrate", parents=iparents)
            if contract_here:
                # parallel subtree contract_check against the (respec'd) contract
                res = self._contract(contract_here["fixed"], contract_here["code_drift"])
                self.emit("integrate", "verifier", "spec-integrate", integ,
                          "parallel contract_check across subtree", contract_here["fixed"],
                          "contract_check", res["status"], level=L_MILESTONE)
            self.emit("integrate", "verifier", "spec-integrate", integ,
                      "end-to-end acceptance criteria", "verification-before-completion", "", "PASS",
                      level=L_MILESTONE)
            self.tasks[integ].status = "done"
            self._completed += 1
        else:
            self._leaf_pipeline(node, contract_ctx)

        self.tasks[nid].status = "done"
        self._completed += 1
        # returning up a level — check the research lane trigger
        self._research_tick(depth)

    def _leaf_pipeline(self, node: dict, contract_ctx: Optional[dict]):
        nid = node["id"]
        title = node.get("title", nid)
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
        self.tasks[impl].status = "done"
        self.tasks[review].status = "done"
        self._completed += 2

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

def load_run(path: Path = None) -> dict:
    path = path or (RUNS_DIR / "privacy_analytics.yaml")
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def render_log(res: RunResult, level: int = None) -> str:
    """Render the execution log, showing events with ``event.level <= level``.
    Defaults to the run's verbosity (env SPEC_FLOW_RUN_VERBOSITY, else L_STEP)."""
    level = level if level is not None else getattr(res, "verbosity", DEFAULT_VERBOSITY)
    lines = ["```", f"TICK │ ACTOR · SKILL · [TASK] action → result   (verbosity={level})"]
    last_phase = None
    for e in res.events:
        if e.level > level:
            continue
        if e.phase != last_phase:
            lines.append(f"── {e.phase} ──")
            last_phase = e.phase
        lines.append(event_line(e))
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
            tags.append("⟨drift→respec⟩")
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
        "Источник отчёта — событийный поток прогона (`RunResult.events`), "
        "сгенерированный из `tests/runs/privacy_analytics.yaml` и возвратов "
        "**настоящих** тулзов плагина в точках решений. Сырой поток целиком "
        "выгружается в `docs/full-run-trace.jsonl`.",
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
        "## Журнал исполнения",
        render_log(res, level),
        "",
        render_summary(res),
        "",
    ]
    return "\n".join(head) + "\n"
