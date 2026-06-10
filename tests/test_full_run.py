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


# ---------------------------------------------------------------------------
# Disk log sink (optional handler) + verbosity control
# ---------------------------------------------------------------------------

import json  # noqa: E402


class TestLogSinkAndVerbosity:
    def test_sink_off_by_default(self, plugin, monkeypatch):
        monkeypatch.delenv("SPEC_FLOW_RUN_LOG", raising=False)
        e = eng.Engine(plugin.tools)
        assert e.sink.enabled is False  # no path/handler -> off

    def test_jsonl_sink_writes_source_data(self, plugin, tmp_path):
        path = tmp_path / "trace.jsonl"
        sink = eng.LogSink(path=str(path), level=eng.L_DETAIL, fmt="jsonl", enabled=True)
        res = eng.Engine(plugin.tools, sink=sink).run(eng.load_run())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        # every event (full detail) persisted, one JSON object per line
        assert len(lines) == len(res.events)
        rec = json.loads(lines[0])
        assert {"tick", "phase", "profile", "skill", "task", "action", "level"} <= set(rec)

    def test_sink_level_filters_disk_output(self, plugin, tmp_path):
        path = tmp_path / "milestones.jsonl"
        sink = eng.LogSink(path=str(path), level=eng.L_MILESTONE, fmt="jsonl", enabled=True)
        res = eng.Engine(plugin.tools, sink=sink).run(eng.load_run())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        milestones = [e for e in res.events if e.level <= eng.L_MILESTONE]
        assert len(lines) == len(milestones) < len(res.events)

    def test_text_format_sink(self, plugin, tmp_path):
        path = tmp_path / "log.txt"
        sink = eng.LogSink(path=str(path), fmt="text", enabled=True)
        eng.Engine(plugin.tools, sink=sink).run(eng.load_run())
        body = path.read_text(encoding="utf-8")
        assert "spec-decomposer" in body and "leaf_check" in body

    def test_callable_handler(self, plugin):
        seen = []
        eng.Engine(plugin.tools, sink=lambda e: seen.append(e)).run(eng.load_run())
        assert seen and all(hasattr(e, "level") for e in seen)

    def test_env_enables_sink(self, plugin, tmp_path, monkeypatch):
        path = tmp_path / "env.jsonl"
        monkeypatch.setenv("SPEC_FLOW_RUN_LOG", str(path))
        monkeypatch.setenv("SPEC_FLOW_RUN_LOG_LEVEL", str(eng.L_MILESTONE))
        e = eng.Engine(plugin.tools)
        assert e.sink.enabled is True and e.sink.level == eng.L_MILESTONE
        e.run(eng.load_run())
        assert path.exists()

    def test_render_verbosity_filters(self, run):
        full = eng.render_log(run, level=eng.L_DETAIL)
        milestones = eng.render_log(run, level=eng.L_MILESTONE)
        assert milestones.count("\n") < full.count("\n")
        # milestone log still carries the key decisions
        assert "leaf_check" in milestones and "drift" in milestones

    def test_dump_trace_has_all_events(self, run):
        lines = eng.dump_trace(run).strip().splitlines()
        assert len(lines) == len(run.events)


# ---------------------------------------------------------------------------
# Materialised workspace artifacts (optional)
# ---------------------------------------------------------------------------

class TestWorkspaceArtifacts:
    def test_workspace_off_by_default(self, plugin, monkeypatch):
        monkeypatch.delenv("SPEC_FLOW_RUN_WORKSPACE", raising=False)
        e = eng.Engine(plugin.tools)
        assert e.workspace.enabled is False

    def test_materialises_specs_code_tests_manifest(self, plugin, tmp_path):
        ws = eng.Workspace(root=str(tmp_path / "wk"), enabled=True)
        eng.Engine(plugin.tools, workspace=ws).run(eng.load_run())
        root = tmp_path / "wk"
        assert (root / "constitution.md").exists()
        assert (root / "MANIFEST.json").exists()
        assert (root / "COMMITS.md").exists()
        specs = list((root / "specs").glob("*.md"))
        srcs = list((root / "src").glob("*.py"))
        tests = list((root / "tests").glob("test_*.py"))
        assert specs and srcs and tests
        # one src + one test per leaf, and a commit per leaf
        assert len(srcs) == len(tests) == len(ws.commits)
        # a frozen contract was materialised
        assert list((root / "contracts").glob("*.yaml"))

    def test_manifest_has_sha_and_counts(self, plugin, tmp_path):
        ws = eng.Workspace(root=str(tmp_path / "wk"), enabled=True)
        eng.Engine(plugin.tools, workspace=ws).run(eng.load_run())
        manifest = json.loads((tmp_path / "wk" / "MANIFEST.json").read_text(encoding="utf-8"))
        assert manifest["counts"].get("spec", 0) >= 5
        assert manifest["counts"].get("code", 0) >= 1
        assert all(a["sha256"] for a in manifest["artifacts"])
