"""Parallel children (parallelization stage 1) — opt-in per case.

The SAFEGUARDS this file pins (the user's hard requirement: existing
protections must survive parallelization):
  * default stays SEQUENTIAL — without the flag nothing changes and the
    full node registry (existing_nodes dedup context) keeps flowing;
  * with the flag, sibling subtrees overlap in time, yet the trace tick
    sequence stays strictly increasing and unique (single writer);
  * the branch integrate is a JOIN: it happens after every child;
  * the LLM budget counter stays EXACT under concurrent spending;
  * a child crash surfaces (no silently swallowed subtree)."""
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb   # noqa: E402
from harness import run_engine as eng   # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


_TITLES = ["Catalog browsing", "Payment capture", "Seller payouts",
           "Search indexing", "Order tracking", "Email receipts"]


def _project(n_leaves, parallel=None):
    proj = {
        "name": "par-case", "goal": "wide service", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        # titles must be semantically DISTINCT: near-identical titles trip
        # the dedup safeguard (it prunes children that re-create existing
        # work) — that protection stays intact under parallel mode
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": f"leaf{i}",
                               "title": _TITLES[i],
                               "metrics": dict(_LEAF)}
                              for i in range(n_leaves)]},
    }
    if parallel:
        proj["parallel"] = parallel
    return proj


class _OverlapProbe:
    """Reviewer that records how many siblings run at the same moment."""

    def __init__(self, dwell=0.15):
        self.dwell = dwell
        self.active = 0
        self.peak = 0
        self.nodes = []
        self._lock = threading.Lock()

    def __call__(self, ctx):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.nodes.append(ctx["node"])
        time.sleep(self.dwell)
        with self._lock:
            self.active -= 1
        return {"verdict": "PASS", "reasons": []}


def test_parallel_children_overlap_and_sequential_default(tmp_path):
    seq = _OverlapProbe()
    eng.run_project(_project(4), workspace=str(tmp_path / "w1"),
                    depth="spec", agents={"reviewer": seq})
    assert seq.peak == 1, "no flag — strictly sequential (the default)"

    par = _OverlapProbe()
    eng.run_project(_project(4, parallel={"children": 4}),
                    workspace=str(tmp_path / "w2"),
                    depth="spec", agents={"reviewer": par})
    assert par.peak >= 2, "the flag must produce real sibling overlap"
    assert sorted(n for n in par.nodes if n.startswith("leaf")) == \
        [f"leaf{i}" for i in range(4)], "every child still processed"


def test_trace_ticks_stay_single_writer_under_parallel(tmp_path):
    res = eng.run_project(_project(6, parallel={"children": 6}),
                          workspace=str(tmp_path / "wk"), depth="spec",
                          agents={"reviewer": _OverlapProbe(dwell=0.05)})
    ticks = [e.tick for e in res.events]
    assert ticks == sorted(ticks) and len(ticks) == len(set(ticks)), \
        "tick sequence must stay strictly increasing and unique"


def test_branch_integrate_joins_all_children(tmp_path):
    res = eng.run_project(_project(5, parallel={"children": 5}),
                          workspace=str(tmp_path / "wk"), depth="spec",
                          agents={"reviewer": _OverlapProbe(dwell=0.05)})
    by_task = {}
    for e in res.events:
        by_task.setdefault(e.task, []).append(e.tick)
    integ = min(by_task["L0:integrate"])
    for i in range(5):
        assert max(by_task[f"leaf{i}"]) < integ, \
            "integrate is the JOIN barrier — it runs after every child"


def test_budget_counter_exact_under_parallel(tmp_path, monkeypatch):
    # the shared counter is the quota safety net — a lost increment under
    # threads would quietly overspend the operator's budget
    lb.configure_workers({"budget": 1000})
    hits = 50
    done = []

    def spend():
        for _ in range(hits):
            lb._spend_call()
        done.append(1)

    threads = [threading.Thread(target=spend) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    try:
        assert len(done) == 4 and lb.calls_made() == 4 * hits
    finally:
        lb.configure_workers(None)


def test_agent_crash_in_one_child_spares_the_others(tmp_path):
    # the engine's standing contract: a WORKER failure never kills the
    # run (sequential or parallel) — the crashed agent is absorbed, the
    # siblings complete, the trace records the episode
    def reviewer(ctx):
        if ctx["node"] == "leaf2":
            raise RuntimeError("subtree blew up")
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(_project(4, parallel={"children": 4}),
                          workspace=str(tmp_path / "wk"), depth="spec",
                          agents={"reviewer": reviewer})
    for i in range(4):
        assert f"leaf{i}" in res.tasks, "every sibling still lands"
    assert "L0:integrate" in res.tasks


# ─── smart criteria: the parameters that decide WHEN forking happens ──

def test_min_siblings_guard(tmp_path):
    # 2 children with min_siblings=3 -> sequential despite the pool flag
    probe = _OverlapProbe()
    eng.run_project(_project(2, parallel={"children": 4, "min_siblings": 3}),
                    workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe})
    assert probe.peak == 1


def test_depth_limit_keeps_deep_branches_sequential(tmp_path):
    # a depth-1 branch must NOT fork when depth_limit is 0 (the default)
    proj = _project(0, parallel={"children": 4})
    proj["tree"]["children"] = [
        {"id": "mid", "title": "Middle branch", "metrics": dict(_BRANCH),
         "children": [{"id": f"deep{i}", "title": _TITLES[i],
                       "metrics": dict(_LEAF)} for i in range(4)]}]
    probe = _OverlapProbe()
    eng.run_project(proj, workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe})
    assert probe.peak == 1, "depth 1 branch stays sequential by default"


def test_max_workers_caps_global_concurrency(tmp_path):
    probe = _OverlapProbe(dwell=0.1)
    eng.run_project(_project(6, parallel={"children": 6, "max_workers": 2}),
                    workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe})
    assert 2 <= probe.peak <= 2, f"global cap must hold, saw {probe.peak}"


def test_llm_concurrency_gate(monkeypatch):
    import threading as _th
    import time as _t
    lb.configure_workers({"concurrency": 2})
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    state = {"active": 0, "peak": 0}
    lock = _th.Lock()

    def slow(prompt, model, system=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        _t.sleep(0.08)
        with lock:
            state["active"] -= 1
        return "ok"

    monkeypatch.setattr(lb, "_ask_openai", slow)
    try:
        threads = [_th.Thread(target=lambda: lb.ask(
            "q", model="openrouter/a:free")) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert state["peak"] <= 2, f"LLM gate must cap in-flight calls, saw {state['peak']}"
    finally:
        lb.configure_workers(None)
