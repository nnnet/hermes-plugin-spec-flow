"""Deterministic detectors fire only when the engine FEEDS them data (the gap
v043 exposed: with empty loops / no reason_history only the semantic classifier
ran). These tests pin the data contract the runner's _doctor_advise must honour:
the loop journal drives ancestry, prior reject reasons drive rewrite_loops."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_diagnosers as dg          # noqa: E402
import spec_flow_doctor as doc             # noqa: E402


def _ctx(node, deps=()):
    return doc.Context(node=node, module="", gate="spec_review", depth=1,
                       depends_on=tuple(deps))


def test_ancestry_fires_on_upstream_fail():
    # a dependency 'db' recorded an integrate-fail -> garbage_accumulation here
    loops = [{"type": "integrate-fail", "task": "db"}]
    d = dg.Diagnosers(dg._Helpers(loops=loops))
    found = d.run(node="api", gate="spec_review", verdict="REJECT",
                  evidence={}, context=_ctx("api", deps=["db"]))
    causes = {f.cause for f in found}
    assert "garbage_accumulation" in causes, causes


def test_ancestry_silent_without_loops():
    # SAME call but empty journal (the v043 state) -> detector cannot fire
    d = dg.Diagnosers(dg._Helpers(loops=[]))
    found = d.run(node="api", gate="spec_review", verdict="REJECT",
                  evidence={}, context=_ctx("api", deps=["db"]))
    assert "garbage_accumulation" not in {f.cause for f in found}


def test_rewrite_loops_fires_on_repeated_reason():
    # same reject reason twice in a row -> fix-by-rewrite repeating the error
    ev = {"reason_history": ["missing REQ-id", "missing REQ-id"]}
    d = dg.Diagnosers(dg._Helpers(loops=[]))
    found = d.run(node="api", gate="spec_review", verdict="REJECT",
                  evidence=ev, context=_ctx("api"))
    assert "rewrite_loops" in {f.cause for f in found}


def test_rewrite_loops_quiet_on_distinct_reasons():
    ev = {"reason_history": ["missing REQ-id", "not EARS"]}
    d = dg.Diagnosers(dg._Helpers(loops=[]))
    found = d.run(node="api", gate="spec_review", verdict="REJECT",
                  evidence=ev, context=_ctx("api"))
    assert "rewrite_loops" not in {f.cause for f in found}
