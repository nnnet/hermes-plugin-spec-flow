"""Coverage check — the scenario pool exercises every skill and profile.

Runs every scenario that carries a tree through the REAL engine and asserts:
(a) the full-exercise case p4 alone covers ALL 9 skills and ALL 6 profiles;
(b) EVERY tree case uses the research (spec-research) and reviewer
    (spec-reviewer) skills — the two revision levels are never skipped;
(c) the pool as a whole covers the entire skill/profile surface.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent / "scenarios"
TREE_CASES = [p for p in sorted(SCENARIOS_DIR.glob("*.yaml"))
              if "tree" in yaml.safe_load(p.read_text(encoding="utf-8"))]


def _run(plugin, tmp_path, monkeypatch, path):
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    # fresh gate state per case so research cooldown never leaks between runs
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"hh_{path.stem}"))
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    return eng.run_project(case, workspace=str(tmp_path / f"wk_{path.stem}"),
                           depth="spec", tools=plugin.tools,
                           contracts_dir=str(eng.CONTRACTS))


def test_scenario_pool_has_tree_cases():
    assert len(TREE_CASES) >= 4, "every scenario should carry a full-run tree"


def test_p4_alone_covers_all_skills_and_profiles(plugin, tmp_path, monkeypatch):
    p4 = [p for p in TREE_CASES if "p4" in p.stem]
    assert p4, "the full-exercise case p4 is missing"
    res = _run(plugin, tmp_path, monkeypatch, p4[0])
    assert eng.ALL_SKILLS - res.skills_used == set()
    assert eng.ALL_PROFILES - res.profiles_used == set()


@pytest.mark.parametrize("path", TREE_CASES, ids=[p.stem for p in TREE_CASES])
def test_every_case_runs_research_and_reviewer(plugin, tmp_path, monkeypatch, path):
    res = _run(plugin, tmp_path, monkeypatch, path)
    assert "spec-research" in res.skills_used, f"{path.stem}: no research lane"
    assert "spec-reviewer" in res.skills_used, f"{path.stem}: nothing was reviewed"
    assert "researcher" in res.profiles_used
    assert "spec-reviewer" in res.profiles_used


def test_pool_union_covers_everything(plugin, tmp_path, monkeypatch):
    skills, profiles = set(), set()
    for path in TREE_CASES:
        res = _run(plugin, tmp_path, monkeypatch, path)
        skills |= res.skills_used
        profiles |= res.profiles_used
    assert eng.ALL_SKILLS - skills == set(), f"skills never run: {eng.ALL_SKILLS - skills}"
    assert eng.ALL_PROFILES - profiles == set(), f"profiles never run: {eng.ALL_PROFILES - profiles}"
