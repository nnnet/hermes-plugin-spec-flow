"""П3 measurement: does the phase pipeline actually overlap?

"Phase pipeline" means one leaf is REVIEWED while a sibling is still being
IMPLEMENTED — the win parallel siblings (#28) already deliver. Rather than
add engine logic, this tool PROVES the overlap from a finished run's trace:
it reconstructs each leaf's active interval and its phase spans, then reports

  * peak_concurrency — most leaves active at the same instant (>1 ⇒ siblings
    truly run in parallel, not one-after-another);
  * phase_overlap_s — wall-clock seconds where ONE leaf's implement span
    overlaps ANOTHER leaf's review span (the pipeline effect itself);
  * a sequential-vs-actual wall-clock delta — the time the overlap saved.

Zero extra cost, zero quota: it reads trace.jsonl only. compare_runs shows
the run-to-run wall-clock; this shows WHERE inside one run the time went.

Usage:  python3 tests/phase_overlap.py <run_dir-or-trace.jsonl> [--json]
Test:   tests/test_phase_overlap.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable


# phases the engine emits while a LEAF is being built (see _leaf_pipeline);
# everything else (decompose, integrate, …) is structural, not leaf work.
_IMPLEMENT_PHASES = {"implement"}
_REVIEW_PHASES = {"review"}


def _base_node(task: str) -> str:
    """'cart_api:impl' / 'cart_api:review' → 'cart_api'. The leaf id is the
    part before the first ':' role suffix."""
    return task.split(":", 1)[0] if task else task


def _events(trace_path: Path) -> list:
    out = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _spans(events: Iterable[dict]) -> dict:
    """Per leaf: {'start','end','impl':[t..],'review':[t..]} from event times.
    A phase span is [min,max] of that phase's event timestamps for the leaf."""
    spans: dict = {}
    for e in events:
        t = e.get("t")
        if t is None:
            continue
        nid = _base_node(e.get("task", ""))
        if not nid:
            continue
        s = spans.setdefault(nid, {"start": t, "end": t,
                                   "impl": [], "review": []})
        s["start"] = min(s["start"], t)
        s["end"] = max(s["end"], t)
        ph = e.get("phase", "")
        if ph in _IMPLEMENT_PHASES:
            s["impl"].append(t)
        elif ph in _REVIEW_PHASES:
            s["review"].append(t)
    return spans


def _interval(times: list) -> tuple | None:
    return (min(times), max(times)) if times else None


def _overlap(a: tuple | None, b: tuple | None) -> float:
    if not a or not b:
        return 0.0
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _peak_concurrency(spans: dict) -> int:
    """Sweep line over leaf [start,end] intervals: the most that are ever
    active simultaneously."""
    edges = []
    for s in spans.values():
        if s["end"] > s["start"]:
            edges.append((s["start"], 1))
            edges.append((s["end"], -1))
    edges.sort()
    cur = peak = 0
    for _, d in edges:
        cur += d
        peak = max(peak, cur)
    return peak


def analyze(trace_path: Path) -> dict:
    events = _events(trace_path)
    spans = _spans(events)
    leaves = {n: s for n, s in spans.items()
              if s["impl"] or s["review"]}

    # phase-pipeline overlap: leaf A implement span vs leaf B review span
    pipeline_overlap = 0.0
    pairs = []
    items = list(leaves.items())
    for i, (na, sa) in enumerate(items):
        ia = _interval(sa["impl"])
        for nb, sb in items[i + 1:]:
            rb = _interval(sb["review"])
            o1 = _overlap(ia, rb)
            ra = _interval(sa["review"])
            ib = _interval(sb["impl"])
            o2 = _overlap(ra, ib)
            if o1 > 0 or o2 > 0:
                pairs.append({"a": na, "b": nb,
                              "implA_reviewB_s": round(o1, 1),
                              "reviewA_implB_s": round(o2, 1)})
            pipeline_overlap += o1 + o2

    actual = 0.0
    if leaves:
        actual = max(s["end"] for s in leaves.values()) \
            - min(s["start"] for s in leaves.values())
    sequential = sum(max(0.0, s["end"] - s["start"]) for s in leaves.values())

    return {
        "leaves": len(leaves),
        "peak_concurrency": _peak_concurrency(leaves),
        "phase_overlap_s": round(pipeline_overlap, 1),
        "actual_wall_s": round(actual, 1),
        "sequential_wall_s": round(sequential, 1),
        "saved_s": round(max(0.0, sequential - actual), 1),
        "overlapping_pairs": sorted(
            pairs, key=lambda p: p["implA_reviewB_s"] + p["reviewA_implB_s"],
            reverse=True)[:10],
    }


def _resolve(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p / "trace.jsonl"
    return p


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Measure phase-pipeline overlap")
    ap.add_argument("run", help="run dir or trace.jsonl path")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)
    trace = _resolve(args.run)
    if not trace.is_file():
        print(f"no trace at {trace}", file=sys.stderr)
        return 2
    rep = analyze(trace)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"leaves measured     : {rep['leaves']}")
    print(f"peak concurrency    : {rep['peak_concurrency']}  "
          f"(>1 ⇒ siblings run in parallel)")
    print(f"phase overlap       : {rep['phase_overlap_s']}s  "
          f"(implement of one ∩ review of another)")
    print(f"actual wall-clock   : {rep['actual_wall_s']}s")
    print(f"sequential estimate : {rep['sequential_wall_s']}s")
    print(f"time saved          : {rep['saved_s']}s")
    if rep["overlapping_pairs"]:
        print("top overlapping leaf pairs:")
        for p in rep["overlapping_pairs"]:
            print(f"  {p['a']} ⇄ {p['b']}: "
                  f"implA∩revB={p['implA_reviewB_s']}s "
                  f"revA∩implB={p['reviewA_implB_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
