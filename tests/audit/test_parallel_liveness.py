"""Audit STAGE 9 — capability liveness: PARALLELISM.

The v148 design hole this stage exists to catch BEFORE a run: the engine
declared a parallel capability (`parallel: {children: N}`) but three design
choices made it unreachable in exactly the runs that needed it — the fork
decision was taken ONCE against the initial tree shape (small products
collapse to one leaf → nothing to fork), late-injected requirements dripped
through a re-poll window that had NO parallel path at all, and a static
depth_limit starved forks when a leaf was recomposed into a branch online.

Principle (stage-wide): a declared engine capability must be PROVEN reachable
in its representative scenario by a dynamic offline test — "the code exists"
is not evidence. Parallelism is verified STRUCTURALLY (real thread overlap
inside the worker, measured with a lock-guarded peak counter), so the proof
holds even when a live provider serialises the actual LLM calls into one lane.
"""
import pathlib
import sys
import threading
import time

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import auto_implementer, run_engine as eng  # noqa: E402

_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}

_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


def _project(goal: str) -> dict:
    return {"name": "parallel-probe", "goal": goal,
            "target": "modules import cleanly; stdlib only",
            "constitution": ["Standard library only."],
            "acceptance": {"smoke": ["the build succeeds"]},
            "parallel": {"children": 3},
            "policy": dict(_POLICY)}


class _Witness:
    """Wrap a role worker and measure REAL thread overlap: the peak number of
    simultaneously active calls. A short sleep widens the overlap window so
    the measurement is robust; the counter is lock-guarded so the peak is
    exact. This is the provider-independent proof of parallelism — it holds
    even when every LLM call downstream is serialised into one lane."""
    lock = threading.Lock()
    active = 0
    peak = 0

    def __init__(self, fn):
        self._fn = fn

    def __call__(self, ctx):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            time.sleep(0.25)
            return self._fn(ctx)
        finally:
            with cls.lock:
                cls.active -= 1

    @classmethod
    def reset(cls):
        cls.active = 0
        cls.peak = 0


def _dump(res) -> str:
    return "\n".join(repr(ev) for ev in res.events)


# ── S9.1 base fan-out: independent siblings DEVELOP concurrently ─────────────

# distinct concerns — near-identical siblings would (rightly) be pruned by
# the anti-duplicate gate, which is honest engine behaviour, not the subject
_FANOUT_KIDS = [
    {"id": "csv_parser", "title": "parse CSV rows into records"},
    {"id": "stats_engine", "title": "aggregate numeric statistics"},
    {"id": "report_writer", "title": "render a plain-text report"},
]


def _fanout_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [dict(c) for c in _FANOUT_KIDS]}
    return {"metrics": dict(_SMALL)}


def test_independent_siblings_develop_in_parallel(plugin, tmp_path):
    _Witness.reset()
    agents = {"decomposer": _fanout_decomposer,
              "implementer": _Witness(auto_implementer.implement)}
    res = eng.run_project(_project("Three independent utility modules:"
                                   " mod_0, mod_1, mod_2 — no web."),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents=agents)
    assert res is not None
    assert "fork opened at" in _dump(res), (
        "3 independent siblings under parallel.children=3 must open a fork —"
        " the decision (and its reason) must be visible in the trace")
    assert _Witness.peak >= 2, (
        f"declared parallelism must produce REAL thread overlap in the"
        f" implementer (peak={_Witness.peak}) — an event without overlap is"
        f" a claim, not a capability")


# ── S9.2 accumulation by INJECTIONS: late requirements fork too (v148) ───────

class _LateReqs:
    """Standing-requirements source that stays SILENT for the first poll and
    then surfaces three independent requirements — the v148 shape: complexity
    that accumulates online, AFTER the initial tree shape was decided."""

    def __init__(self):
        self.polls = 0

    def __call__(self):
        self.polls += 1
        if self.polls <= 1:
            return []
        return [("late_alpha", "Add module alpha.py with def alpha() -> 'a'."),
                ("late_beta", "Add module beta.py with def beta() -> 'b'."),
                ("late_gamma", "Add module gamma.py with def gamma() -> 'g'.")]


def _single_leaf_decomposer(ctx):
    # the small-product collapse shape: ONE base leaf, nothing to fork at start
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [{"id": "core", "title": "core module"}]}
    return {"metrics": dict(_SMALL)}


def test_late_injections_accumulate_into_a_fork(plugin, tmp_path):
    _Witness.reset()
    agents = {"decomposer": _single_leaf_decomposer,
              "implementer": _Witness(auto_implementer.implement)}
    res = eng.run_project(_project("A tiny core utility; more modules may be"
                                   " requested while it is being built."),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents=agents, standing_requirements=_LateReqs())
    assert res is not None
    dump = _dump(res)
    assert "fork opened at" in dump, (
        "requirements injected AFTER the initial (single-leaf) shape must"
        " accumulate into a parallel wave — v148 built them strictly one at a"
        " time because the late windows never re-evaluated the fork policy")
    assert _Witness.peak >= 2, (
        f"late-injected independent requirements must develop with real"
        f" thread overlap (peak={_Witness.peak})")


# ── S9.3 accumulation by RECOMPOSITION: growth DEEPER than the start shape ───

def _deep_growth_decomposer(ctx):
    # root holds a single branch; that branch recomposes into 3 leaves —
    # the fan-out appears at depth 1, deeper than the legacy static
    # depth_limit=0 default that used to starve it
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [{"id": "base", "title": "base branch"}]}
    if ctx["depth"] == 1 and ctx["node"]["id"] == "base":
        return {"metrics": dict(_BIG),
                "children": [dict(c) for c in _FANOUT_KIDS]}
    return {"metrics": dict(_SMALL)}


def test_recomposed_depth_growth_still_forks(plugin, tmp_path):
    _Witness.reset()
    agents = {"decomposer": _deep_growth_decomposer,
              "implementer": _Witness(auto_implementer.implement)}
    res = eng.run_project(_project("A base package that splits into three"
                                   " leaf modules during design."),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents=agents)
    assert res is not None
    assert "fork opened at" in _dump(res), (
        "a wave materialising at depth 1 (online recomposition) must fork —"
        " a depth gate fixed at config time starves runs that outgrow their"
        " initial shape")
    assert _Witness.peak >= 2, (
        f"recomposed fan-out must develop with real thread overlap"
        f" (peak={_Witness.peak})")
