"""Spec-flow decomposition simulator.

Mimics what the Hermes kanban dispatcher + the spec-flow-decompose skill do,
but drives every leaf-vs-branch decision through the plugin's **real**
``leaf_check`` tool. Given a project decomposition tree (a fixture), it walks
the tree top-down and builds:

* a decision graph  (node id -> actual leaf_check verdict), and
* a realized kanban DAG (decompose / contract / impl / review / integrate
  tasks with parent edges) following the documented rules:
    branch -> child decompose tasks (+ a contract child when flagged) + an
              integrate node whose parents are the children (+ contract);
    leaf   -> an impl task (depending on the governing contract, if any) + a
              spec-reviewer review node.

Tests compare the decision graph against the fixture's ground-truth verdicts
and the realized DAG summary against the fixture's expectations, and the
reporter renders all of it as an ASCII tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

HARNESS_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = HARNESS_DIR.parent / "projects"
OPENAPI_DIFF = HARNESS_DIR / "openapi_diff.py"


# ---------------------------------------------------------------------------
# Fixture model
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    title: str
    metrics: dict[str, Any]
    expect: str                      # ground-truth 'leaf' | 'branch'
    contract: bool = False           # this branch emits an L2 contract child
    children: list["Node"] = field(default_factory=list)


def _build(d: dict) -> Node:
    return Node(
        id=d["id"],
        title=d.get("title", d["id"]),
        metrics=d.get("metrics", {}),
        expect=d["expect"],
        contract=bool(d.get("contract", False)),
        children=[_build(c) for c in d.get("children", [])],
    )


@dataclass
class Project:
    name: str
    goal: str
    root: Node
    expect_dag: dict[str, Any]
    raw: dict


def load_project(path: Path) -> Project:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Project(
        name=data["name"],
        goal=data.get("goal", ""),
        root=_build(data["tree"]),
        expect_dag=data.get("expect_dag", {}),
        raw=data,
    )


def all_projects() -> list[Project]:
    return [load_project(p) for p in sorted(PROJECTS_DIR.glob("*.yaml"))]


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    kind: str            # decompose | contract | impl | review | integrate
    skill: str
    parents: list[str]


@dataclass
class SimResult:
    project: Project
    decisions: dict[str, str]            # node id -> actual verdict
    expected: dict[str, str]             # node id -> fixture verdict
    depth: dict[str, int]                # node id -> depth
    tasks: dict[str, Task]               # realized DAG
    fixture_errors: list[str]

    # ---- derived summary -------------------------------------------------
    @property
    def summary(self) -> dict[str, int]:
        kinds = [t.kind for t in self.tasks.values()]
        return {
            "leaves": kinds.count("impl"),
            "integrates": kinds.count("integrate"),
            "contracts": kinds.count("contract"),
            "reviews": kinds.count("review"),
            "decomposes": kinds.count("decompose"),
            "max_depth": max(self.depth.values()) if self.depth else 0,
        }

    @property
    def mismatches(self) -> list[str]:
        out = []
        for nid, actual in self.decisions.items():
            exp = self.expected.get(nid)
            if exp != actual:
                out.append(f"{nid}: expected {exp}, got {actual}")
        return out


def simulate(project: Project, tools: Any) -> SimResult:
    decisions: dict[str, str] = {}
    expected: dict[str, str] = {}
    depth: dict[str, int] = {}
    tasks: dict[str, Task] = {}
    fixture_errors: list[str] = []

    def add(task: Task) -> None:
        tasks[task.id] = task

    def visit(node: Node, lvl: int, contract_dep: Optional[str]) -> None:
        depth[node.id] = lvl
        expected[node.id] = node.expect
        verdict = json.loads(tools._handle_leaf_check(dict(node.metrics)))["verdict"]
        decisions[node.id] = verdict

        # fixture sanity: branch must have children, leaf must not
        if verdict == "branch" and not node.children:
            fixture_errors.append(f"{node.id}: branch verdict but no children in fixture")
        if verdict == "leaf" and node.children:
            fixture_errors.append(f"{node.id}: leaf verdict but children present in fixture")

        add(Task(node.id, "decompose", "spec-flow-decompose", parents=[]))

        if verdict == "branch":
            contract_id = None
            if node.contract:
                contract_id = f"{node.id}:contract"
                add(Task(contract_id, "contract", "spec-contract", parents=[node.id]))
            child_ids = []
            for child in node.children:
                visit(child, lvl + 1, contract_dep=contract_id)
                child_ids.append(child.id)
            integ_parents = child_ids + ([contract_id] if contract_id else [])
            add(Task(f"{node.id}:integrate", "integrate", "spec-integrate", parents=integ_parents))
        else:
            impl_parents = [node.id] + ([contract_dep] if contract_dep else [])
            impl_id = f"{node.id}:impl"
            add(Task(impl_id, "impl", "spec-implement", parents=impl_parents))
            add(Task(f"{node.id}:review", "review", "spec-reviewer", parents=[impl_id]))

    visit(project.root, 0, contract_dep=None)
    return SimResult(project, decisions, expected, depth, tasks, fixture_errors)


# ---------------------------------------------------------------------------
# Rendering (human-readable)
# ---------------------------------------------------------------------------

_MARK = {"leaf": "🍃 leaf", "branch": "🌿 branch"}


def render_tree(result: SimResult) -> str:
    """ASCII decomposition tree with per-node verdict and a ✓/✗ vs expected."""
    lines: list[str] = []

    def walk(node: Node, prefix: str, is_last: bool, is_root: bool) -> None:
        actual = result.decisions[node.id]
        exp = result.expected[node.id]
        ok = "✓" if actual == exp else f"✗(exp {exp})"
        connector = "" if is_root else ("└─ " if is_last else "├─ ")
        badge = _MARK[actual]
        ctag = " ⟨contract⟩" if (node.contract and actual == "branch") else ""
        lines.append(f"{prefix}{connector}{node.title} [{badge}{ctag}] {ok}")
        child_prefix = prefix + ("" if is_root else ("   " if is_last else "│  "))
        kids = node.children if actual == "branch" else []
        for i, c in enumerate(kids):
            walk(c, child_prefix, i == len(kids) - 1, is_root=False)

    walk(result.project.root, "", True, is_root=True)
    return "\n".join(lines)


def render_report(results: list[SimResult]) -> str:
    out: list[str] = []
    out.append("# spec-flow — отчёт боевого тестирования\n")
    out.append("Каждый узел проекта классифицирован **настоящим** `leaf_check`. "
               "Дерево ниже — фактический отклик плагина; ✓ = совпало с ожиданием.\n")
    for r in results:
        s = r.summary
        status = "✅ OK" if not r.mismatches and not r.fixture_errors else "❌ MISMATCH"
        out.append(f"\n## {r.project.name} — {status}")
        out.append(f"_{r.project.goal}_\n")
        out.append("```")
        out.append(render_tree(r))
        out.append("```")
        out.append(
            f"Узлов: {len(r.decisions)} · листьев(impl): {s['leaves']} · "
            f"веток(integrate): {s['integrates']} · контрактов: {s['contracts']} · "
            f"ревью: {s['reviews']} · глубина: {s['max_depth']}"
        )
        exp = r.project.expect_dag
        if exp:
            checks = []
            for key in ("leaves", "integrates", "contracts", "max_depth"):
                if key in exp:
                    got = s[key]
                    checks.append(f"{key} {got}/{exp[key]} {'✓' if got == exp[key] else '✗'}")
            out.append("Сверка с ожидаемым DAG: " + " · ".join(checks))
        if r.mismatches:
            out.append("Расхождения вердиктов: " + "; ".join(r.mismatches))
        if r.fixture_errors:
            out.append("Ошибки фикстуры: " + "; ".join(r.fixture_errors))
    return "\n".join(out) + "\n"
