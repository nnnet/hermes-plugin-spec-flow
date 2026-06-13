#!/usr/bin/env python3
"""Compare spec-flow runs of one case side by side.

Plan П5: a measurement tool so 'memory on/off', 'parallel on/off',
'pipeline on/off' are answerable with numbers, not impressions. It reads
each run's trace.jsonl + llm-log.jsonl + meta.json and tabulates the
metrics that actually move between runs.

Usage
-----
  python3 tests/compare_runs.py                # all runs of the latest case
  python3 tests/compare_runs.py --case p4      # all p4 runs
  python3 tests/compare_runs.py <dir> <dir>    # specific run dirs
  python3 tests/compare_runs.py --json         # machine-readable

A run that never finished (no SUMMARY.md) is still reported — its
duration is wall-clock of the trace, marked '~' as ongoing/aborted.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

RUNS = pathlib.Path(__file__).resolve().parent / "runs-out"


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def metrics(run_dir: pathlib.Path) -> dict:
    trace = _read_jsonl(run_dir / "trace.jsonl")
    llm = _read_jsonl(run_dir / "llm-log.jsonl")
    meta = {}
    if (run_dir / "meta.json").exists():
        try:
            meta = json.loads((run_dir / "meta.json").read_text("utf-8"))
        except json.JSONDecodeError:
            pass

    ts = [e.get("t") for e in trace if isinstance(e.get("t"), (int, float))]
    duration = (max(ts) - min(ts)) if len(ts) >= 2 else 0.0
    finished = (run_dir / "SUMMARY.md").exists()

    def count_action(needle: str) -> int:
        return sum(1 for e in trace if needle in str(e.get("action", "")))

    def count_event(name: str) -> int:
        return sum(1 for e in llm if e.get("event") == name)

    # the realized tree size
    tree_nodes = 0
    if (run_dir / "tree.json").exists():
        try:
            tj = json.loads((run_dir / "tree.json").read_text("utf-8"))

            def _n(node):
                return 1 + sum(_n(c) for c in node.get("children", []) or [])
            tree_nodes = _n(tj)
        except (json.JSONDecodeError, AttributeError):
            pass

    integ_pass = sum(1 for e in trace
                     if e.get("gate") == "integrate_verify"
                     and e.get("verdict") == "PASS")
    root_id = (meta.get("worker_models") and "L0") or "L0"
    root_red = any("RED" in str(e.get("action", ""))
                   and str(e.get("task", "")).startswith(root_id)
                   for e in trace)
    reqs_attached = sum(1 for e in trace
                        if e.get("gate") == "requirement"
                        and e.get("verdict") == "ATTACHED")

    mem = meta.get("memory") or {}
    par = meta.get("parallel") or {}

    return {
        "run": run_dir.name.split("__")[1] if "__" in run_dir.name
        else run_dir.name,
        "dir": run_dir.name,
        "finished": finished,
        "duration_s": round(duration, 1),
        "duration": _fmt_dur(duration) + ("" if finished else "~"),
        "ticks": max((e.get("tick", 0) for e in trace), default=0),
        "tree_nodes": tree_nodes,
        "llm_calls": count_event("call_start"),
        "reviews_rejected": count_action("REJECT"),
        "lint_reworks": count_action("rework closed open decisions"),
        "demotions": count_action("demoted to leaf"),
        "crashes": count_action("crashed"),
        "leaf_timeouts": count_action("time ceiling"),
        "integrate_pass": integ_pass,
        "integrate_red": count_event("integrate_red"),
        "quota_waits": count_event("quota_wait"),
        "auto_answers": count_event("auto_answer"),
        "reqs_attached": reqs_attached,
        "root_red": root_red,
        "mem_roles": (mem.get("roles") or {}).get("mode", "—")
        if isinstance(mem, dict) else "—",
        "concurrency": (meta.get("workers") or {}).get("concurrency", "—")
        if isinstance(meta.get("workers"), dict) else "—",
    }


def _runs_for(case: str | None, explicit: list[str]) -> list[pathlib.Path]:
    if explicit:
        return [pathlib.Path(p) for p in explicit]
    dirs = sorted(d for d in RUNS.iterdir()
                  if d.is_dir() and "__" in d.name)
    if not case:
        # latest case = the case slug of the newest dir
        if not dirs:
            return []
        case = dirs[-1].name.split("__")[-1]
    return [d for d in dirs if d.name.endswith(case)]


# columns shown in the table: (key, header, width)
_COLS = [
    ("run", "run", 7), ("duration", "time", 8), ("ticks", "ticks", 6),
    ("tree_nodes", "nodes", 6), ("llm_calls", "llm", 5),
    ("reviews_rejected", "rejX", 5), ("demotions", "demo", 5),
    ("crashes", "crash", 6), ("leaf_timeouts", "tmout", 6),
    ("integrate_pass", "intg✓", 6), ("integrate_red", "intgR", 6),
    ("quota_waits", "qwait", 6), ("auto_answers", "auto", 5),
    ("reqs_attached", "reqs", 5), ("root_red", "rootRED", 8),
]


def render_table(rows: list[dict]) -> str:
    head = " ".join(h.ljust(w) for _, h, w in _COLS)
    out = [head, "-" * len(head)]
    for r in rows:
        cells = []
        for key, _, w in _COLS:
            v = r.get(key, "")
            v = ("yes" if v else "no") if isinstance(v, bool) else str(v)
            cells.append(v.ljust(w))
        out.append(" ".join(cells))
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Compare spec-flow runs")
    ap.add_argument("dirs", nargs="*", help="explicit run dirs")
    ap.add_argument("--case", help="case slug filter (e.g. p4)")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args(argv)

    case = args.case
    if case and not case.startswith("p") and "-" not in case:
        case = None  # let it resolve
    runs = _runs_for(args.case, args.dirs)
    if not runs:
        print("no matching runs", file=sys.stderr)
        return 1
    rows = [metrics(d) for d in runs]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
