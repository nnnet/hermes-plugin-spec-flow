#!/usr/bin/env python3
"""Analyse an LLM debug log (SPEC_FLOW_LLM_LOG output) — evidence, not guessing.

Reads the JSONL written by tests/harness/llm_log.py and reports: call counts per
role, latency (sum/mean/max + the slowest call), errors, the tree the decomposer
actually built (depth distribution, branch/leaf split, biggest fan-outs) and
whether deep nodes converged to leaves. Prints a table and writes a markdown
sibling ``<log>.analysis.md``.

Usage: python3 tests/analyze_llm_log.py <llm-log.jsonl>
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def analyse(rows: list[dict]) -> dict:
    calls = [r for r in rows if r.get("event") == "call_ok"]
    errors = [r for r in rows if r.get("event") == "call_error"]
    outcomes = [r for r in rows if r.get("event") == "outcome"]

    per_role = Counter(r["role"] for r in calls)
    lat = [r["latency_s"] for r in calls if "latency_s" in r]
    slowest = max(calls, key=lambda r: r.get("latency_s", 0), default=None)

    dec_out = [r for r in outcomes if r.get("role") == "decomposer"]
    depth_hist = Counter(r.get("depth") for r in dec_out)
    verdict_by_depth = defaultdict(Counter)
    for r in dec_out:
        verdict_by_depth[r.get("depth")][r.get("verdict")] += 1
    fanouts = sorted(((len(r.get("children", [])), r.get("node"))
                      for r in dec_out if r.get("verdict") == "branch"),
                     reverse=True)[:5]
    # convergence: any node at depth>=3 that still produced children is a leak
    deep_branches = [r["node"] for r in dec_out
                     if isinstance(r.get("depth"), int) and r["depth"] >= 3
                     and r.get("verdict") == "branch"]

    impl_out = [r for r in outcomes if r.get("role") == "implementer"]
    impl_fail = [r for r in impl_out if r.get("parse") == "fail"]

    return {
        "total_calls": len(calls),
        "errors": len(errors),
        "per_role": dict(per_role),
        "latency": {"sum": round(sum(lat), 1), "mean": round(sum(lat) / len(lat), 2) if lat else 0,
                    "max": max(lat) if lat else 0},
        "slowest": {"node": slowest.get("node"), "role": slowest.get("role"),
                    "latency_s": slowest.get("latency_s")} if slowest else None,
        "decomposer_nodes": len(dec_out),
        "depth_hist": {str(k): v for k, v in sorted(depth_hist.items(), key=lambda x: (x[0] is None, x[0]))},
        "verdict_by_depth": {str(k): dict(v) for k, v in sorted(verdict_by_depth.items(), key=lambda x: (x[0] is None, x[0]))},
        "top_fanouts": fanouts,
        "deep_branch_leaks": deep_branches,
        "impl_calls": len(impl_out),
        "impl_parse_failures": [r.get("node") for r in impl_fail],
        "error_detail": [(r.get("node"), r.get("error")) for r in errors][:10],
    }


def render(a: dict) -> str:
    L = ["# LLM run analysis", ""]
    L.append(f"- **Total model calls:** {a['total_calls']}  (errors: {a['errors']})")
    L.append(f"- **By role:** " + ", ".join(f"{k}={v}" for k, v in a["per_role"].items()))
    lt = a["latency"]
    L.append(f"- **Latency (s):** sum {lt['sum']} · mean {lt['mean']} · max {lt['max']}")
    if a["slowest"]:
        s = a["slowest"]
        L.append(f"- **Slowest call:** {s['role']} `{s['node']}` — {s['latency_s']}s")
    L.append(f"- **Decomposer nodes:** {a['decomposer_nodes']}  ·  impl calls: {a['impl_calls']}")
    L.append("")
    L.append("## Tree shape")
    L.append(f"- depth histogram: {a['depth_hist']}")
    L.append(f"- verdict by depth: {a['verdict_by_depth']}")
    if a["top_fanouts"]:
        L.append("- biggest fan-outs: " + ", ".join(f"{n}×`{node}`" for n, node in a["top_fanouts"]))
    if a["deep_branch_leaks"]:
        L.append(f"- ⚠️ **convergence leak** (depth≥3 still branching): {a['deep_branch_leaks']}")
    else:
        L.append("- ✅ convergence: no depth≥3 node kept branching")
    L.append("")
    if a["impl_parse_failures"]:
        L.append(f"## ⚠️ implementer parse failures: {a['impl_parse_failures']}")
    if a["error_detail"]:
        L.append("## Errors")
        for node, err in a["error_detail"]:
            L.append(f"- `{node}`: {err}")
    return "\n".join(L) + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: analyze_llm_log.py <llm-log.jsonl>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"no such log: {path}")
        return 2
    rows = _load(path)
    if not rows:
        print("log is empty")
        return 1
    a = analyse(rows)
    md = render(a)
    print(md)
    out = path.with_suffix(".analysis.md")
    out.write_text(md, encoding="utf-8")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
