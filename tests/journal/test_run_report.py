"""Battle tests — the plugin's log-based report + methodology audit.

The report builder lives in the PLUGIN (spec_flow_tools.build_run_report /
audit_methodology / run_report tool) and works purely off a run trace. We feed
it (a) the real clean run's trace -> no methodology errors, and (b) a hand-made
flawed trace -> the audit catches each violation a reviewer must see.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

FLAWED = pathlib.Path(__file__).resolve().parent.parent / "runs" / "flawed_run.jsonl"


@pytest.fixture
def clean_trace(plugin, tmp_path):
    path = tmp_path / "clean.jsonl"
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    sink = eng.LogSink(path=str(path), level=eng.L_DETAIL, fmt="jsonl", enabled=True)
    eng.run_project(eng.load_run(), workspace=str(tmp_path / "wk"), depth="scaffold",
                    tools=plugin.tools, contracts_dir=str(eng.CONTRACTS), sink=sink)
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def flawed_trace():
    return [json.loads(l) for l in FLAWED.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestCleanRunPassesAudit:
    def test_no_methodology_errors(self, plugin, clean_trace):
        findings = plugin.tools.audit_methodology(clean_trace)
        errors = [f for f in findings if f["severity"] == "error"]
        assert not errors, f"clean run flagged: {errors}"

    def test_summary_reports_both_revision_levels(self, plugin, clean_trace):
        s = plugin.tools.summarize_trace(clean_trace)
        assert s["revision_levels"]["spike"] is True
        assert s["revision_levels"]["continuous_revision"] is True
        assert s["complete"] is True

    def test_report_has_footprints_and_audit(self, plugin, clean_trace):
        rep = plugin.tools.build_run_report(clean_trace)
        assert "Footprint" in rep and "Методологический аудит" in rep
        assert "Простыми словами" in rep  # plain-language column present
        assert "нарушений не найдено" in rep.lower() or "✅" in rep

    def test_summary_counts_skill_and_profile_events(self, plugin, clean_trace):
        # the counts come from the LOG (skill/profile fields), computed by code
        s = plugin.tools.summarize_trace(clean_trace)
        assert s["skill_events"]["spec-research"] >= 1
        assert s["skill_events"]["spec-reviewer"] >= 1
        assert s["profile_events"]["researcher"] >= 1
        assert s["skills_missing"] == [] and s["profiles_missing"] == []

    def test_report_renders_coverage_table(self, plugin, clean_trace):
        rep = plugin.tools.build_run_report(clean_trace)
        assert "Покрытие — посчитано кодом из лога" in rep
        assert "`spec-research`" in rep and "`spec-reviewer`" in rep

    def test_report_flags_unused_surface(self, plugin, flawed_trace):
        rep = plugin.tools.build_run_report(flawed_trace)
        assert "не использован" in rep  # partial trace -> gaps are visible


class TestFlawedRunCaught:
    def test_audit_flags_violations(self, plugin, flawed_trace):
        rules = {f["rule"] for f in plugin.tools.audit_methodology(flawed_trace)}
        # silent drift, impl without leaf gate, impl not reviewed, branch w/o integrate
        assert "R3-silent-drift" in rules
        assert "R2-leaf-before-impl" in rules
        assert "R4-impl-not-reviewed" in rules
        assert "R5-branch-no-integrate" in rules

    def test_has_errors(self, plugin, flawed_trace):
        findings = plugin.tools.audit_methodology(flawed_trace)
        assert sum(1 for f in findings if f["severity"] == "error") >= 4

    def test_open_decision_warning(self, plugin, flawed_trace):
        rules = {f["rule"] for f in plugin.tools.audit_methodology(flawed_trace)}
        assert "R6-expanded-past-open-decision" in rules

    def test_impl_before_research_warning(self, plugin, flawed_trace):
        # R9: the flawed run implements without any upfront research
        rules = {f["rule"] for f in plugin.tools.audit_methodology(flawed_trace)}
        assert "R9-impl-before-research" in rules


class TestRunReportTool:
    def test_tool_registered(self, plugin):
        assert "run_report" in plugin.reg.tools

    def test_tool_from_path(self, plugin):
        out = json.loads(plugin.tools._handle_run_report({"trace_path": str(FLAWED)}))
        assert out["errors"] >= 4
        assert "report" in out and out["findings"]

    def test_tool_inline_trace(self, plugin, flawed_trace):
        out = json.loads(plugin.tools._handle_run_report({"trace": flawed_trace, "level": 1}))
        assert out["summary"]["events"] == len(flawed_trace)

    def test_tool_requires_input(self, plugin):
        out = json.loads(plugin.tools._handle_run_report({}))
        assert "error" in out
