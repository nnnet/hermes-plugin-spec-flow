"""Battle test — the two node-lifecycle engines: 'inline' vs 'fsm'.

The runner can drive each node's lifecycle two ways (roadmap Phase 5):
  * ``node_engine='inline'`` — the imperative pipeline (the default);
  * ``node_engine='fsm'``    — the standalone ``NodeLifecycle`` (pytransitions
    or its dependency-free fallback) drives the phases and guards the gates.

PARITY: a whole case run under both engines must produce the SAME outcome —
the same tasks and statuses, the same gate counts, the same loops, the same
coverage, and the same oracle verdict. The engine is an implementation detail,
not a behaviour change.

NEGATIVE: a skipped mandatory gate must be caught by BOTH engines. A node may
carry a test-only ``_skip_gate`` that drops one gate; closing that node then
raises ``GateViolation`` whichever engine drives it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent / "scenarios"
CASES = ["p1_earn_for_living.yaml", "p4_b2b_marketplace.yaml"]


def _run(plugin, tmp_path, monkeypatch, case_file, engine):
    case = yaml.safe_load((SCENARIOS_DIR / case_file).read_text(encoding="utf-8"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"hh_{case_file}_{engine}"))
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    return eng.run_project(
        case, workspace=str(tmp_path / f"wk_{case_file}_{engine}"),
        depth="spec", tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
        node_engine=engine)


def _fingerprint(res):
    """Engine-independent shape of a run: tasks, gates, loops, coverage."""
    tasks = {tid: (t.status, t.version, t.runs) for tid, t in res.tasks.items()}
    loops = sorted((l["type"], l.get("task", l.get("kind", ""))) for l in res.loops)
    return {
        "tasks": tasks,
        "gate_calls": dict(res.gate_calls),
        "loops": loops,
        "skills": set(res.skills_used),
        "profiles": set(res.profiles_used),
    }


# ─── the fsm engine must actually be wired in ─────────────────────────


def test_fsm_engine_is_available():
    assert eng._NODE_FSM_OK is True
    assert "fsm" in eng.NODE_ENGINES and "inline" in eng.NODE_ENGINES


def test_unknown_engine_rejected(plugin, tmp_path):
    with pytest.raises(ValueError):
        eng.run_project({"goal": "x", "tree": {"id": "L0", "title": "x",
                         "metrics": {"modules": 1, "tasks": 1, "interfaces": 1,
                                     "estimated_loc": 10, "open_decisions": 0,
                                     "single_concern": True, "testable_criteria": True}}},
                        workspace=str(tmp_path / "wk"), tools=plugin.tools,
                        node_engine="quantum")


# ─── parity: same outcome under both engines ──────────────────────────


@pytest.mark.parametrize("case_file", CASES)
def test_engines_produce_identical_run(plugin, tmp_path, monkeypatch, case_file):
    """Why: switching the node lifecycle engine must not change the result.
    What: run the case with inline and fsm, compare the engine-independent
    fingerprint (tasks/statuses/versions, gate counts, loops, coverage).
    Test: the two fingerprints are equal."""
    inline = _run(plugin, tmp_path, monkeypatch, case_file, "inline")
    fsm = _run(plugin, tmp_path, monkeypatch, case_file, "fsm")
    assert _fingerprint(inline) == _fingerprint(fsm)


@pytest.mark.parametrize("case_file", CASES)
def test_engines_agree_on_oracle(plugin, tmp_path, monkeypatch, case_file):
    """Why: the smart oracle verdict is the run's contract — it must be the
    same regardless of engine.
    What: build the plugin oracle report for both runs, assert both pass and
    expose the same realised depth / done-anchors.
    Test: both .ok and the rendered episode set match."""
    case = yaml.safe_load((SCENARIOS_DIR / case_file).read_text(encoding="utf-8"))
    oracle_spec = case["oracle"]
    out = {}
    for engine in ("inline", "fsm"):
        res = _run(plugin, tmp_path, monkeypatch, case_file, engine)
        summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
        rep = plugin.tools.check_oracle(res, oracle_spec, summary)
        out[engine] = rep
    assert out["inline"].ok and out["fsm"].ok, (
        plugin.tools.render_oracle(out["inline"]) + "\n---\n"
        + plugin.tools.render_oracle(out["fsm"]))
    # same per-expectation verdicts under both engines
    verdicts = {eng_: [(e.name, e.ok) for e in rep.expectations]
                for eng_, rep in out.items()}
    assert verdicts["inline"] == verdicts["fsm"]


# ─── negative: a skipped gate is caught in BOTH engines ───────────────

_SKIP_PROJECT = {
    "name": "skip-gate-case",
    "goal": "tiny service with a leaf that skips its review gate",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Tiny",
        "metrics": {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
                    "open_decisions": 0, "single_concern": False, "testable_criteria": True},
        "children": [
            {"id": "good", "title": "Good leaf",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
            # this leaf drops its review_pass gate — closing it must raise
            {"id": "broken", "title": "Leaf with a skipped gate",
             "_skip_gate": "review_pass",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
        ],
    },
}


@pytest.mark.parametrize("engine", ["inline", "fsm"])
def test_skipped_gate_raises_in_both_engines(plugin, tmp_path, engine):
    with pytest.raises(eng.GateViolation):
        eng.run_project(dict(_SKIP_PROJECT),
                        workspace=str(tmp_path / f"wk_skip_{engine}"),
                        tools=plugin.tools, node_engine=engine)


def test_clean_project_does_not_raise(plugin, tmp_path):
    # same project WITHOUT the skip key completes under both engines
    proj = dict(_SKIP_PROJECT)
    proj["tree"] = {**proj["tree"],
                    "children": [proj["tree"]["children"][0],
                                 {k: v for k, v in proj["tree"]["children"][1].items()
                                  if k != "_skip_gate"}]}
    for engine in ("inline", "fsm"):
        res = eng.run_project(dict(proj),
                              workspace=str(tmp_path / f"wk_ok_{engine}"),
                              tools=plugin.tools, node_engine=engine)
        assert res.tasks["broken"].status == "done"
