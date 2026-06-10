#!/usr/bin/env python3
"""Human-readable spec-flow test report.

Runs the same engine the battle tests use (the real leaf_check / contract_check
/ research_trigger_check) over every project fixture and renders a readable
report: an ASCII decomposition tree per project with ✓/✗ vs the expected
design, a DAG reconciliation, a contract-drift table and the research-lane
timeline. Prints to the console and writes docs/test-report.md.

Usage:  python3 tests/report.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
sys.path.insert(0, str(HERE))
from harness import simulator as sim  # noqa: E402


# --- load the plugin standalone (same fakes the tests use) -----------------

def _load_tools():
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
        "spec_flow_rep", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["spec_flow_rep"] = pkg
    spec.loader.exec_module(pkg)
    pkg.register(object())
    return sys.modules["spec_flow_rep.spec_flow_tools"], reg_tools


def _contract_section(tools) -> str:
    contracts = HERE / "contracts"
    tools.CONTRACT_VALIDATORS["openapi"] = ["python3", str(sim.OPENAPI_DIFF), "{contract}", "{code}"]
    cases = [
        ("clean implementation", ["url_shortener.openapi.yaml"], "code_clean.json"),
        ("type mismatch (id: int->str)", ["url_shortener.openapi.yaml"], "code_type_mismatch.json"),
        ("missing endpoint", ["url_shortener.openapi.yaml"], "code_missing_endpoint.json"),
        ("subtree parallel (2 contracts)", ["url_shortener.openapi.yaml", "cart.openapi.yaml"], "code_clean.json"),
    ]
    rows = ["", "## Contract drift (real openapi_diff validator)", "",
            "| Scenario | status | drift |", "|---|---|---|"]
    for label, cs, code in cases:
        out = json.loads(tools._handle_contract_check({
            "contract_artifacts": [str(contracts / c) for c in cs],
            "changed_files": [str(contracts / code)],
            "types": ["openapi"],
        }))
        n = len(out["drift"])
        icon = "✅" if out["status"] == "ok" else "⚠️"
        rows.append(f"| {label} | {icon} {out['status']} | {n} |")
    return "\n".join(rows)


def _research_section(tools) -> str:
    timeline = [
        ("tick", 3, 0), ("on_level_return", 4, 0), ("tick", 6, 0),
        ("tick", 10, 12), ("tick", 12, 12), ("tick", 31, 12), ("cron", 31, 12),
    ]
    rows = ["", "## Research lane timeline (data-pipeline)", "",
            "| event | completed | errors | fired? | by |", "|---|---|---|---|---|"]
    for reason, c, e in timeline:
        out = json.loads(tools._handle_research_trigger_check(
            {"reason": reason, "completed_tasks": c, "test_errors": e}))
        icon = "🔬 yes" if out["trigger"] else "—"
        rows.append(f"| {reason} | {c} | {e} | {icon} | {','.join(out['fired_by']) or '-'} |")
    return "\n".join(rows)


def main() -> int:
    tools, reg_tools = _load_tools()
    results = [sim.simulate(p, tools) for p in sim.all_projects()]

    body = sim.render_report(results)
    # contract + research need an isolated HERMES_HOME for state; use a tmp dir
    import os
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="specflow-report-")
    body += "\n" + _contract_section(tools)
    body += "\n" + _research_section(tools)

    ok = all(not r.mismatches and not r.fixture_errors for r in results)
    header = (f"> Тулзов зарегистрировано: {len(reg_tools)} "
              f"({', '.join(sorted(reg_tools))})\n"
              f"> Проектов проверено: {len(results)} · "
              f"итог: {'✅ всё совпало с ожиданием' if ok else '❌ есть расхождения'}\n")
    report = body.split("\n", 1)
    full = report[0] + "\n\n" + header + "\n" + (report[1] if len(report) > 1 else "")

    out_path = PLUGIN_DIR / "docs" / "test-report.md"
    out_path.write_text(full + "\n", encoding="utf-8")
    print(full)
    print(f"\n[report written to {out_path}]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
