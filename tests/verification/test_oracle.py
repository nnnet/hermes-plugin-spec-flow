"""Smart oracle tests — the scenario ``tree`` is the run INPUT; correctness is
judged by the declared ``oracle:`` block against the realized RunResult, NOT by
replaying the tree back to itself.

Each tree case is driven through the real engine (depth 'spec') and its own
oracle must pass. A NEGATIVE test mutates the oracle spec (missing anchor,
impossible depth, unreachable loop/coverage floors) and asserts the oracle
REPORTS a failure — proving the checker is not vacuously green.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import oracle as orc  # noqa: E402
from harness import run_engine as eng  # noqa: E402

SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scenarios"
ORACLE_CASES = [
    p for p in sorted(SCENARIOS_DIR.glob("*.yaml"))
    if "oracle" in yaml.safe_load(p.read_text(encoding="utf-8"))
]
P4 = [p for p in ORACLE_CASES if "p4" in p.stem]


def _run(plugin, tmp_path, monkeypatch, path):
    """Why: drive a scenario through the production runner exactly like
    test_full_run / test_case_coverage do, with a fresh gate state per case.
    What: returns (case_dict, RunResult, summary_dict).
    Test: the returned RunResult.tasks contains 'L0:integrate' == done."""
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"hh_{path.stem}"))
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    res = eng.run_scenario(case, workspace=str(tmp_path / f"wk_{path.stem}"),
                           depth="spec", tools=plugin.tools,
                           contracts_dir=str(eng.CONTRACTS))
    # summarize_trace works on event dicts — feed it the dumped event stream.
    summary = plugin.tools.summarize_trace(
        [eng.asdict(e) for e in res.events])
    return case, res, summary


def test_pool_has_oracle_cases():
    assert len(ORACLE_CASES) >= 4, "every scenario should declare an oracle block"
    assert P4, "the full-exercise case p4 must declare an oracle"


@pytest.mark.parametrize("path", ORACLE_CASES, ids=[p.stem for p in ORACLE_CASES])
def test_each_case_passes_its_oracle(plugin, tmp_path, monkeypatch, path):
    """Why: the declared oracle must hold for its own scenario's realized run.
    Test: report.ok is True; on failure the rendered table names the offenders."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, path)
    report = orc.check(res, case["oracle"], summary)
    assert report.ok, "oracle failed:\n" + orc.render(report)
    # every declared expectation actually produced a row (nothing silently skipped)
    assert report.expectations, "oracle produced no expectations"


def test_p4_oracle_is_full_surface(plugin, tmp_path, monkeypatch):
    """Why: p4 is the full-exercise case — its oracle must demand and observe
    all episodes, both drift classes and the revision lane.
    Test: every required episode is present and report.ok is True."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    report = orc.check(res, case["oracle"], summary)
    assert report.ok, orc.render(report)
    names = {e.name for e in report.expectations}
    for ep in ("episode:drift_respec", "episode:drift_codefix", "episode:revision"):
        assert ep in names


def test_render_contains_table(plugin, tmp_path, monkeypatch):
    """Why: the renderer must yield a human-readable verdict table.
    Test: output has the PASS header and a row per expectation."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    report = orc.check(res, case["oracle"], summary)
    md = orc.render(report)
    assert "Oracle verdict" in md and "| Expectation |" in md
    assert md.count("\n") >= len(report.expectations)


# -- negative tests: the oracle must be able to FAIL --------------------------

def test_missing_anchor_fails(plugin, tmp_path, monkeypatch):
    """Why: an anchor that does not exist on the board must be reported, not
    silently passed — proves anchor checks are real.
    Test: adding a bogus anchor flips report.ok to False with that anchor named."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    spec = copy.deepcopy(case["oracle"])
    spec["anchor_nodes"] = list(spec["anchor_nodes"]) + ["nonexistent_node"]
    report = orc.check(res, spec, summary)
    assert not report.ok
    assert any(e.name == "anchor:nonexistent_node" and not e.ok
               for e in report.expectations)


def test_impossible_min_depth_fails(plugin, tmp_path, monkeypatch):
    """Why: a min_depth above the realized depth must fail — proves the depth
    bound is computed from the run, not assumed.
    Test: min_depth=99 yields a failing 'min_depth' expectation."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    spec = copy.deepcopy(case["oracle"])
    spec["min_depth"] = 99
    report = orc.check(res, spec, summary)
    assert not report.ok
    assert any(e.name == "min_depth" and not e.ok for e in report.expectations)


def test_unreachable_loop_count_fails(plugin, tmp_path, monkeypatch):
    """Why: demanding more loops than the run produced must fail — proves loop
    counts are measured, not declared.
    Test: requiring 99 clarify loops yields a failing 'loops:clarify'."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    spec = copy.deepcopy(case["oracle"])
    spec["expected_loops"] = {**spec.get("expected_loops", {}), "clarify": 99}
    report = orc.check(res, spec, summary)
    assert not report.ok
    assert any(e.name == "loops:clarify" and not e.ok for e in report.expectations)


def test_unreachable_coverage_fails(plugin, tmp_path, monkeypatch):
    """Why: a coverage floor above the surface must fail — proves coverage is
    read from the realized run.
    Test: min_skills=999 yields a failing 'coverage:skills'."""
    case, res, summary = _run(plugin, tmp_path, monkeypatch, P4[0])
    spec = copy.deepcopy(case["oracle"])
    spec["coverage"] = {**spec.get("coverage", {}), "min_skills": 999}
    report = orc.check(res, spec, summary)
    assert not report.ok
    assert any(e.name == "coverage:skills" and not e.ok
               for e in report.expectations)


def test_demanding_absent_episode_fails(plugin, tmp_path, monkeypatch):
    """Why: a case without contract drift must NOT pass an oracle demanding it —
    proves episode detection is content-driven.
    Test: p1 (no drift) fails an oracle requiring the drift_respec episode."""
    p1 = [p for p in ORACLE_CASES if "p1" in p.stem][0]
    case, res, summary = _run(plugin, tmp_path, monkeypatch, p1)
    spec = copy.deepcopy(case["oracle"])
    spec["expected_episodes"] = list(spec["expected_episodes"]) + ["drift_respec"]
    report = orc.check(res, spec, summary)
    assert not report.ok
    assert any(e.name == "episode:drift_respec" and not e.ok
               for e in report.expectations)
