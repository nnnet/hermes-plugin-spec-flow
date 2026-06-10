#!/usr/bin/env python3
"""Build the spec-flow run report + methodology audit FROM A TRACE (logs).

The report is produced by the plugin's own log-based functions
(`spec_flow_tools.build_run_report` / `audit_methodology`), not by the test
harness — feed it any run trace (JSONL) and it renders footprints + an audit a
reviewer uses to spot methodological errors.

Usage:
    python3 tests/run_report.py                       # docs/full-run-trace.jsonl
    python3 tests/run_report.py path/to/trace.jsonl   # any trace
    python3 tests/run_report.py trace.jsonl --level 1 # only milestone footprints
    python3 tests/run_report.py trace.jsonl -o out.md # write somewhere else

In real Hermes the same thing is one tool call: run_report(trace_path=...).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent


def _load_plugin_tools():
    """Load spec_flow_tools standalone (stub the in-repo registry/toolsets)."""
    reg = types.ModuleType("tools.registry")
    reg.registry = type("R", (), {"register": lambda self, **k: None})()
    reg.tool_error = lambda m: json.dumps({"error": m})
    pkg = types.ModuleType("tools")
    pkg.registry = reg
    sys.modules["tools"] = pkg
    sys.modules["tools.registry"] = reg
    ts = types.ModuleType("toolsets")
    ts.TOOLSETS = {"kanban": {"tools": []}}
    sys.modules["toolsets"] = ts
    spec = importlib.util.spec_from_file_location(
        "spec_flow_tools", PLUGIN_DIR / "spec_flow_tools.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spec_flow_tools"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="spec-flow run report + methodology audit (from a trace)")
    p.add_argument("trace", nargs="?", default=str(PLUGIN_DIR / "docs" / "full-run-trace.jsonl"),
                   help="JSONL run trace (default: docs/full-run-trace.jsonl)")
    p.add_argument("--level", type=int, default=2, help="footprints verbosity 1..3 (default 2)")
    p.add_argument("-o", "--out", default=str(PLUGIN_DIR / "docs" / "full-run-report.md"),
                   help="output markdown path")
    p.add_argument("--title", default="spec-flow run")
    a = p.parse_args(argv)

    tools = _load_plugin_tools()
    if not Path(a.trace).exists():
        print(f"trace not found: {a.trace}", file=sys.stderr)
        return 2

    events = tools._load_trace(a.trace)
    report = tools.build_run_report(events, level=a.level, title=a.title)
    findings = tools.audit_methodology(events)
    errors = sum(1 for x in findings if x["severity"] == "error")

    Path(a.out).write_text(report, encoding="utf-8")
    print(report)
    print(f"[from {a.trace} → {a.out}; methodology errors: {errors}]")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
