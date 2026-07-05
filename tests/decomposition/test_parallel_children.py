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


# A1 review tiering (engine default ON, simple_max_loc 120) skips the LLM
# reviewer for a "simple" leaf — the EXACT seam _OverlapProbe hooks. The
# fixtures' 80-LOC leaves classify as simple, so with tiering on the probe
# fires only for the root branch review (once, in the scheduler thread) and
# every peak measurement is vacuous: peak == 1 regardless of forking. Every
# probe-driven run below disables tiering so the reviewer runs for EVERY
# node and the probe measures REAL sibling overlap inside worker threads.
_FULL_REVIEW = {"tiering": False}


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
                    depth="spec", agents={"reviewer": seq},
                    review_policy=_FULL_REVIEW)
    assert seq.peak == 1, "no flag — strictly sequential (the default)"

    par = _OverlapProbe()
    eng.run_project(_project(4, parallel={"children": 4}),
                    workspace=str(tmp_path / "w2"),
                    depth="spec", agents={"reviewer": par},
                    review_policy=_FULL_REVIEW)
    assert par.peak >= 2, "the flag must produce real sibling overlap"
    assert sorted(n for n in par.nodes if n.startswith("leaf")) == \
        [f"leaf{i}" for i in range(4)], "every child still processed"


def test_trace_ticks_stay_single_writer_under_parallel(tmp_path):
    res = eng.run_project(_project(6, parallel={"children": 6}),
                          workspace=str(tmp_path / "wk"), depth="spec",
                          agents={"reviewer": _OverlapProbe(dwell=0.05)},
                          review_policy=_FULL_REVIEW)
    ticks = [e.tick for e in res.events]
    assert ticks == sorted(ticks) and len(ticks) == len(set(ticks)), \
        "tick sequence must stay strictly increasing and unique"


def test_branch_integrate_joins_all_children(tmp_path):
    res = eng.run_project(_project(5, parallel={"children": 5}),
                          workspace=str(tmp_path / "wk"), depth="spec",
                          agents={"reviewer": _OverlapProbe(dwell=0.05)},
                          review_policy=_FULL_REVIEW)
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
                          agents={"reviewer": reviewer},
                          review_policy=_FULL_REVIEW)
    for i in range(4):
        assert f"leaf{i}" in res.tasks, "every sibling still lands"
    assert "L0:integrate" in res.tasks


# ─── smart criteria: the parameters that decide WHEN forking happens ──

def test_min_siblings_guard(tmp_path):
    # 2 children with min_siblings=3 -> sequential despite the pool flag
    probe = _OverlapProbe()
    eng.run_project(_project(2, parallel={"children": 4, "min_siblings": 3}),
                    workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe},
                    review_policy=_FULL_REVIEW)
    assert probe.peak == 1


def test_depth_limit_keeps_deep_branches_sequential(tmp_path):
    # a depth-1 branch must NOT fork when depth_limit pins forks to depth 0.
    # NOTE: depth_limit used to DEFAULT to 0; the online fork policy (commit
    # 6d34695, operator directive) made the default unlimited — forks are now
    # governed by accumulated complexity, and a case pins depth_limit
    # explicitly when it wants a static bound. This pin is that explicit case.
    proj = _project(0, parallel={"children": 4, "depth_limit": 0})
    proj["tree"]["children"] = [
        {"id": "mid", "title": "Middle branch", "metrics": dict(_BRANCH),
         "children": [{"id": f"deep{i}", "title": _TITLES[i],
                       "metrics": dict(_LEAF)} for i in range(4)]}]
    probe = _OverlapProbe()
    eng.run_project(proj, workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe},
                    review_policy=_FULL_REVIEW)
    assert probe.peak == 1, "depth 1 branch stays sequential under the pin"


def test_max_workers_caps_global_concurrency(tmp_path):
    probe = _OverlapProbe(dwell=0.1)
    eng.run_project(_project(6, parallel={"children": 6, "max_workers": 2}),
                    workspace=str(tmp_path / "wk"), depth="spec",
                    agents={"reviewer": probe},
                    review_policy=_FULL_REVIEW)
    assert 2 <= probe.peak <= 2, f"global cap must hold, saw {probe.peak}"


def test_llm_concurrency_gate(monkeypatch, fake_openai):
    import threading as _th

    from harness_fakeapi import ok
    lb.configure_workers({"max_concurrent_llm_requests": 2})
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    # a REAL slow local server: each request lingers 0.08s, so 6 client calls
    # fired at once would peak at 6 concurrent on the wire WITHOUT the gate. The
    # threaded server records its own peak in-flight handler count — the gate
    # must keep it at 2.
    srv = fake_openai([(200, ok("ok"))], delay=0.08)
    try:
        threads = [_th.Thread(target=lambda: lb.ask(
            "q", model="openrouter/a:free", role="decomposer", step=""))
            for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert srv.peak_concurrency <= 2, \
            f"LLM gate must cap in-flight calls, saw {srv.peak_concurrency}"
        assert srv.call_count == 6, "all six calls really reached the server"
    finally:
        lb.configure_workers(None)


def test_nested_forks_do_not_deadlock_on_max_workers(tmp_path):
    # v16 live freeze: branch threads HELD max_workers slots while
    # join()ing their children — grandchildren starved for a slot and
    # the whole run sat in futex_wait forever. A joining parent must
    # lend its slot back for the duration of the wait.
    grand = [["Catalog browsing", "Payment capture", "Seller payouts"],
             ["Search indexing", "Order tracking", "Email receipts"],
             ["Refund handling", "Stock alerts", "Review moderation"]]
    sub = ["Storefront", "Fulfilment", "Trust safety"]
    proj = {
        "name": "nested-par", "goal": "deep wide service", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        # 3 branches x 3 leaves, forks allowed at depth 0 AND 1, but only
        # 2 worker slots: before the fix the 2 slots land on two joining
        # branch parents and every grandchild waits forever
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [
                     {"id": f"br{i}", "title": f"{sub[i]} area",
                      "metrics": dict(_BRANCH),
                      "children": [{"id": f"br{i}_leaf{j}",
                                    "title": grand[i][j],
                                    "metrics": dict(_LEAF)}
                                   for j in range(3)]}
                     for i in range(3)]},
        "parallel": {"children": 3, "min_siblings": 2,
                     "depth_limit": 2, "max_workers": 2},
    }
    box = {}

    def _run():
        box["res"] = eng.run_project(
            proj, workspace=str(tmp_path / "wk"), depth="spec",
            agents={"reviewer": _OverlapProbe(dwell=0.02)},
            review_policy=_FULL_REVIEW)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=60)
    assert not th.is_alive(), \
        "nested parallel run deadlocked on max_workers slots"
    res = box["res"]
    done = {t for t in res.tasks}
    for i in range(3):
        for j in range(3):
            assert f"br{i}_leaf{j}" in done, "every grandchild processed"


# ── dependency WAVES: independent siblings still parallelize ───────────────

def _project_waves(parallel=None):
    """5 children: research → arch → {auth, catalog, orders}. The three
    feature leaves depend on arch but NOT on each other, so they form one
    parallel wave; research and arch are their own (serial) waves."""
    deps = {"research": [], "arch": ["research"],
            "auth": ["arch"], "catalog": ["arch"], "orders": ["arch"]}
    titles = {"research": "Market research", "arch": "Architecture baseline",
              "auth": "Accounts and access", "catalog": "Catalog and search",
              "orders": "Orders and checkout"}
    proj = {
        "name": "wave-case", "goal": "wide service", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": k, "title": titles[k],
                               "metrics": dict(_LEAF), "depends_on": deps[k]}
                              for k in ["research", "arch", "auth",
                                        "catalog", "orders"]]},
    }
    if parallel:
        proj["parallel"] = parallel
    return proj


def test_independent_siblings_parallelize_despite_dependencies(tmp_path):
    # the regression this fixes: ANY depends_on used to force the whole
    # branch sequential (peak == 1). Now only dependency WAVES serialize;
    # the three arch-dependent-but-mutually-independent leaves overlap.
    probe = _OverlapProbe(dwell=0.15)
    eng.run_project(_project_waves(parallel={"children": 4}),
                    workspace=str(tmp_path / "w"), depth="spec",
                    agents={"reviewer": probe},
                    review_policy=_FULL_REVIEW)
    assert probe.peak >= 2, "independent siblings in a wave must overlap"
    order = [n for n in probe.nodes if n in
             ("research", "arch", "auth", "catalog", "orders")]
    # the dependency barrier holds: arch after research, features after arch
    assert order.index("research") < order.index("arch")
    for f in ("auth", "catalog", "orders"):
        assert order.index("arch") < order.index(f), \
            "a dependent wave starts only after its dependency wave"


def test_pure_dependency_chain_stays_serial(tmp_path):
    # a→b→c with each depending on the previous: every wave has one member,
    # so peak stays 1 (correctness preserved, no false parallelism)
    chain = {
        "name": "chain", "goal": "g", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [
                     {"id": "a", "title": "Stage alpha", "metrics": dict(_LEAF),
                      "depends_on": []},
                     {"id": "b", "title": "Stage beta", "metrics": dict(_LEAF),
                      "depends_on": ["a"]},
                     {"id": "c", "title": "Stage gamma", "metrics": dict(_LEAF),
                      "depends_on": ["b"]}]},
        "parallel": {"children": 3},
    }
    probe = _OverlapProbe(dwell=0.1)
    eng.run_project(chain, workspace=str(tmp_path / "w"), depth="spec",
                    agents={"reviewer": probe},
                    review_policy=_FULL_REVIEW)
    assert probe.peak == 1, "a pure chain must stay strictly serial"


def test_llm_concurrency_legacy_key(monkeypatch, fake_openai):
    # back-compat: a pre-rename run with the old `concurrency` key still gates
    from harness import llm_backend as lb
    lb.configure_workers({"concurrency": 2})
    assert lb._provider_gate("openrouter/a:free") is not None
