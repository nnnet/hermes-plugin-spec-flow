"""S9.4 / task #146 — research-before-impl must hold BY CONSTRUCTION.

Why: p4's oracle `research_before_impl` flaked under CPU load: within one
parallel wave the research spike's thread could lose the CPU to a sibling
impl thread — ticks are assigned at emission time under `_emit_lock`, so
tick order is raw cross-thread emission order with no happens-before edge
for research. Kahn waves honour only declared sibling `depends_on`; p4
declares none among L0 children, so `market_research` and impl-bearing
feature branches land in ONE wave and the spike wins only by scheduling
luck. Engine-agnostic: 'inline' and 'fsm' invert identically (the fsm has
no scheduling of its own).

What: an adversarial scheduler at the emit boundary — a WORKER thread about
to emit its first `spec-research` event yields until some thread has
emitted `spec-implement` — models CPU starvation of the research subtree
deterministically: no sleeps, no load. If the runner orders research by
construction (spikes run in the wave scheduler's own thread BEFORE any
worker thread exists), the adversary never fires and the oracle passes;
under ordering-by-luck the ticks invert and the oracle fails.

Test: run p4 under both engines with the seam installed; assert the oracle
row `research_before_impl` holds for both.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scenarios"


@pytest.mark.parametrize("engine", ["inline", "fsm"])
def test_research_precedes_impl_under_adversarial_scheduler(
        plugin, tmp_path, monkeypatch, engine):
    """Why: the ordering must survive a worst-case thread schedule, not rely
    on the research code path being shorter than a sibling's impl path.
    What: install the starvation seam on Engine.emit, run p4, read the
    oracle's research_before_impl expectation.
    Test: first research tick < first impl tick under BOTH engines even when
    every worker-thread research emit is deferred until an impl emit."""
    runner = sys.modules["spec_flow_runner"]
    case = yaml.safe_load(
        (SCENARIOS_DIR / "p4_b2b_marketplace.yaml").read_text(encoding="utf-8"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"hh_{engine}"))
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]

    impl_emitted = threading.Event()
    scheduler_thread = threading.current_thread()
    starved: list = []          # one-shot: starve the FIRST research emit
    orig_emit = runner.Engine.emit

    def adversarial_emit(self, phase, profile, skill, task, action, *a, **kw):
        # The WORKER thread about to emit the run's first research event
        # loses the CPU until some thread has emitted an implement event —
        # the exact schedule CPU contention produces. Research emitted from
        # the scheduler's own thread skips the wait (hoisted spikes ARE the
        # by-construction ordering). The bounded wait is an escape hatch,
        # not a synchronization point: a mis-ordered run goes RED long
        # before it elapses (impl emits in milliseconds), and a correctly
        # ordered run parks implementation at the runner's readiness gate,
        # so the timeout expiring WITHOUT an impl emit is itself the proof.
        if (skill == "spec-research" and not impl_emitted.is_set()
                and not starved
                and threading.current_thread() is not scheduler_thread):
            starved.append(task)
            impl_emitted.wait(timeout=5)
        out = orig_emit(self, phase, profile, skill, task, action, *a, **kw)
        if skill == "spec-implement":
            impl_emitted.set()
        return out

    monkeypatch.setattr(runner.Engine, "emit", adversarial_emit)
    res = eng.run_scenario(
        case, workspace=str(tmp_path / f"wk_{engine}"), depth="spec",
        tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
        node_engine=engine)
    summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
    rep = plugin.tools.check_oracle(res, case["oracle"], summary)
    row = next(e for e in rep.expectations if e.name == "research_before_impl")
    assert row.ok, (
        f"{engine}: research_before_impl inverted under adversarial "
        f"scheduling — {row.reason}")
