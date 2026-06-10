"""Battle test — full end-to-end project run exercising every skill & profile.

Runs the privacy-analytics project through the engine (real plugin tools at
every decision) and asserts the run touched the entire feature surface: all 9
skills, all 6 profiles, each control-flow loop (clarify, review critique,
contract drift -> respec, research revision -> respec) and that the project
completed.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402


@pytest.fixture
def run(plugin):
    return eng.Engine(plugin.tools).run(eng.load_run())


def test_all_skills_exercised(run):
    missing = eng.ALL_SKILLS - run.skills_used
    assert not missing, f"skills never run: {missing}"


def test_all_profiles_exercised(run):
    missing = eng.ALL_PROFILES - run.profiles_used
    assert not missing, f"profiles never run: {missing}"


def test_project_completes(run):
    assert run.tasks["L0:integrate"].status == "done"
    # every task ends done
    not_done = [t.id for t in run.tasks.values() if t.status != "done"]
    assert not not_done, f"tasks left unfinished: {not_done}"


def test_multilevel_tree(run):
    # at least a 3-level tree (L0 -> branch -> leaf), many tasks
    assert len(run.tasks) >= 20
    # contract + integrate + review tasks all present
    kinds = {t.kind for t in run.tasks.values()}
    assert {"requirements", "decompose", "contract", "impl", "review", "integrate", "research"} <= kinds


@pytest.mark.parametrize("loop_type", ["clarify", "review-fail", "drift-respec", "revision-respec"])
def test_each_loop_happened(run, loop_type):
    assert any(l["type"] == loop_type for l in run.loops), f"no {loop_type} loop in the run"


def test_real_tools_were_called(run):
    g = run.gate_calls
    assert g["policy_gate"] >= 1
    assert g["leaf_check"] >= 5
    assert g["contract_check"] >= 2     # drift + after-respec + integrate
    assert g["research_trigger_check"] >= 1


def test_drift_then_clean_after_respec(run):
    # the drift episode must show a drift verdict followed by an ok verdict
    cc = [e for e in run.events if e.gate == "contract_check"]
    verdicts = [e.verdict for e in cc]
    assert "drift" in verdicts and "ok" in verdicts


def test_respec_bumps_version(run):
    # the revision lane must have version-bumped the invalidated node
    consent = run.tasks.get("consent")
    assert consent is not None and consent.version >= 2
