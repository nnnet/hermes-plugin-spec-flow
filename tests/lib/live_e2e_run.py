#!/usr/bin/env python3
"""Live end-to-end run of ONE case (plan step 1.1) — spends real LLM quota.

The full autonomous arc with LIVE agents: the plugin builds the task tree itself
from the goal (live decomposer, the case `tree` is DROPPED and used only as the
oracle) and a live model writes the leaf code (live implementer). Everything the
run produces — workspace artifacts, the plugin-built reports, COST.md — lands in
a fresh timestamped workspace:

    tests/runs-out/<YYYY-MM-DDTHH-MM-SS>__<case>/

Default model is haiku (override with --model or SPEC_FLOW_LLM_MODEL).

Usage:
    python3 tests/live_e2e_run.py --case p1 --depth execute
    python3 tests/live_e2e_run.py --case p4 --depth product --model haiku

This script is INTENTIONALLY not part of the pytest suite: it costs quota. Its
plumbing (cost meter, report wiring) is covered offline by
tests/test_live_e2e_harness.py with stub agents.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import run_cases as rc                      # noqa: E402 — reuse the case machinery
from harness import cost as cost_mod        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Live e2e run of one scenario case")
    ap.add_argument("--case", default="p1",
                    help="substring of the case file name (default p1, the simplest)")
    ap.add_argument("--depth", default="execute",
                    choices=sorted(rc.eng.DEPTHS, key=rc.eng.DEPTHS.get))
    ap.add_argument("--model", default=os.environ.get("SPEC_FLOW_LLM_MODEL", "haiku"),
                    help="LLM model for the live agents (default haiku)")
    args = ap.parse_args()
    os.environ["SPEC_FLOW_LLM_MODEL"] = args.model

    matches = [p for p in sorted(rc.SCENARIOS_DIR.glob("*.yaml")) if args.case in p.stem]
    if not matches:
        print(f"no case matching {args.case!r} in {rc.SCENARIOS_DIR}")
        return 2
    path = matches[0]
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    name = case.get("name", path.stem)

    tools = rc._load_tools()
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    case_dir = rc.OUT_DIR / f"{stamp}__{name}__live"
    case_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix=f"specflow-live-{name}-")

    meter = cost_mod.Meter()
    print(f"LIVE e2e · case={name} · depth={args.depth} · model={args.model}")
    print(f"  workspace: {case_dir.relative_to(rc.PLUGIN_DIR)}/")
    full = rc._run_full(case, case_dir, args.depth, tools,
                        decomposer="llm", implementer="llm",
                        meter=meter, model=args.model)
    (case_dir / "SUMMARY.md").write_text(
        rc._summary_md(name, case.get("goal", ""), args.depth, full, None),
        encoding="utf-8")

    cost = meter.report()
    print(f"\n  oracle: {full['oracle_ok']} · audit errors: {full['audit_errors']} · "
          f"tasks: {full['tasks']} · product: {full['product']}")
    print(f"  cost: {cost['total_calls']} LLM calls "
          f"({', '.join(f'{k}={v}' for k, v in cost['calls'].items())})")
    print(f"  → {case_dir.relative_to(rc.PLUGIN_DIR)}/ (SUMMARY.md, report.md, "
          f"oracle-report.md, COST.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
