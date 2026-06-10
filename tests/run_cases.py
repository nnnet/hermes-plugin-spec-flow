#!/usr/bin/env python3
"""Run every scenario case as a REAL production run of the plugin.

For each case a separate, timestamped workspace folder is created:

    tests/runs-out/<YYYY-MM-DDTHH-MM-SS>__<case>/

EVERYTHING the run produces lands inside that folder:

    workspace/         materialised run artifacts (constitution, specs/,
                       contracts/, MANIFEST.json; deeper depths add src/,
                       tests/, COMMITS.md, TEST-RESULTS.md)
    trace.jsonl        raw event stream (full detail) — the report's source
    log.txt            readable execution log (text)
    report.md          footprints + methodology audit, built by the PLUGIN
                       (build_run_report) from trace.jsonl
    policy-report.md   policy_gate catching the imprecise goal variant
                       (only for cases that carry imprecise/resolved)
    SUMMARY.md         what ran, at which depth, verdicts, file map

Cases live in tests/scenarios/*.yaml. A case with a ``tree`` gets a full
engine run; a case with ``imprecise``/``resolved`` gets the policy pipeline;
p4 has both.

Usage:
    python3 tests/run_cases.py                          # all cases, depth=spec
    python3 tests/run_cases.py --depth scaffold         # deeper run
    python3 tests/run_cases.py --case p4 --depth verify # one case, deeper
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
SCENARIOS_DIR = HERE / "scenarios"
OUT_DIR = HERE / "runs-out"
sys.path.insert(0, str(HERE))
from harness import run_engine as eng  # noqa: E402
from harness import scenarios as scn  # noqa: E402


def _load_tools():
    """Load the plugin standalone (the same registry/toolsets stubs the tests
    use) and return its tools module."""
    reg_tools = {}

    class _Reg:
        def register(self, **kw):
            reg_tools[kw["name"]] = kw

    tools_pkg = types.ModuleType("tools")
    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = _Reg()
    reg_mod.tool_error = lambda m: json.dumps({"error": m})
    tools_pkg.registry = reg_mod
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg_mod
    ts = types.ModuleType("toolsets")
    ts.TOOLSETS = {"kanban": {"tools": []}}
    sys.modules["toolsets"] = ts

    spec = importlib.util.spec_from_file_location(
        "spec_flow_cases", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["spec_flow_cases"] = pkg
    spec.loader.exec_module(pkg)
    pkg.register(object())
    return sys.modules["spec_flow_cases.spec_flow_tools"]


def _run_full(case: dict, case_dir: Path, depth: str, tools) -> dict:
    """The real production run: mandatory workspace inside the case folder,
    disk sink at full detail, the plugin's own report built from the trace."""
    tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    trace = case_dir / "trace.jsonl"
    sink = eng.LogSink(path=str(trace), level=eng.L_DETAIL, fmt="jsonl", enabled=True)
    res = eng.run_project(case, workspace=str(case_dir / "workspace"), depth=depth,
                          tools=tools, contracts_dir=str(eng.CONTRACTS), sink=sink)

    (case_dir / "log.txt").write_text(
        "\n".join(eng.event_line(e) for e in res.events) + "\n", encoding="utf-8")
    events = tools._load_trace(str(trace))
    report = tools.build_run_report(events, title=f"{case['name']} (depth={depth})")
    (case_dir / "report.md").write_text(report, encoding="utf-8")

    findings = tools.audit_methodology(events)
    summary = tools.summarize_trace(events)
    return {
        "skills": f"{len(res.skills_used)}/{len(eng.ALL_SKILLS)}",
        "profiles": f"{len(res.profiles_used)}/{len(eng.ALL_PROFILES)}",
        "tasks": len(res.tasks),
        "loops": {l["type"] for l in res.loops},
        "gate_calls": res.gate_calls,
        "audit_errors": sum(1 for f in findings if f["severity"] == "error"),
        "audit_warns": sum(1 for f in findings if f["severity"] == "warn"),
        "complete": summary["complete"],
    }


def _run_policy(path: Path, case_dir: Path, tools) -> dict:
    """The policy surface: the plugin (not a human) catches the imprecise goal."""
    sc = scn.load_scenario(path)
    (case_dir / "policy-report.md").write_text(
        scn.render_scenario(tools, sc) + "\n", encoding="utf-8")
    imprecise = scn.run_pipeline(tools, sc.imprecise)
    resolved = scn.run_pipeline(tools, sc.resolved)
    return {"imprecise": imprecise["policy_verdict"], "resolved": resolved["policy_verdict"]}


def _summary_md(name: str, goal: str, depth: str, full: dict | None,
                policy: dict | None) -> str:
    lines = [f"# {name} — итог прогона", "",
             f"**Цель:** {goal}", f"**Глубина:** `{depth}`", ""]
    if policy:
        lines += ["## Policy-разрез (плагин ловит неточность)",
                  f"- размытая постановка → **{policy['imprecise']}** (не декомпозируется)",
                  f"- уточнённая постановка → **{policy['resolved']}**", ""]
    if full:
        loops = ", ".join(sorted(full["loops"])) or "—"
        gates = ", ".join(f"{k}×{v}" for k, v in full["gate_calls"].items())
        audit = ("✅ нарушений нет" if not full["audit_errors"] and not full["audit_warns"]
                 else f"❌ ошибок {full['audit_errors']}, предупреждений {full['audit_warns']}")
        lines += ["## Полный прогон",
                  f"- скиллы: **{full['skills']}** · профили: **{full['profiles']}** · задач: **{full['tasks']}**",
                  f"- циклы: {loops}",
                  f"- вызовы гейтов: {gates}",
                  f"- завершён: {'✅' if full['complete'] else '❌'}",
                  f"- методологический аудит: {audit}", ""]
    lines += ["## Файлы",
              "- `workspace/` — артефакты прогона (конституция, спеки, контракты, MANIFEST)",
              "- `trace.jsonl` — сырой событийный поток (источник отчёта)",
              "- `log.txt` — журнал исполнения",
              "- `report.md` — footprint + методологический аудит (строит сам плагин)",
              "- `policy-report.md` — ловля размытой постановки (если есть policy-разрез)", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run scenario cases as real plugin runs")
    ap.add_argument("--depth", default="spec", choices=sorted(eng.DEPTHS, key=eng.DEPTHS.get))
    ap.add_argument("--case", default="", help="substring filter on the case file name")
    args = ap.parse_args()

    tools = _load_tools()
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    rows = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        if args.case and args.case not in path.stem:
            continue
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = case.get("name", path.stem)
        case_dir = OUT_DIR / f"{stamp}__{name}"
        case_dir.mkdir(parents=True, exist_ok=True)
        # fresh state per case so gate cooldowns never leak between cases
        os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix=f"specflow-{name}-")

        policy = _run_policy(path, case_dir, tools) if "imprecise" in case else None
        full = _run_full(case, case_dir, args.depth, tools) if "tree" in case else None

        (case_dir / "SUMMARY.md").write_text(
            _summary_md(name, case.get("goal", ""), args.depth, full, policy),
            encoding="utf-8")
        rows.append((name, case_dir, policy, full))

    print(f"depth={args.depth} · cases: {len(rows)}\n")
    for name, case_dir, policy, full in rows:
        bits = []
        if policy:
            bits.append(f"policy: {policy['imprecise']}→{policy['resolved']}")
        if full:
            audit = "audit ✅" if not full["audit_errors"] else f"audit ❌×{full['audit_errors']}"
            bits.append(f"skills {full['skills']}, profiles {full['profiles']}, "
                        f"tasks {full['tasks']}, {audit}")
        print(f"  {name:24s} {' · '.join(bits)}")
        print(f"  {'':24s} → {case_dir.relative_to(PLUGIN_DIR)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
