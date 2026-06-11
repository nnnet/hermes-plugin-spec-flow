"""Offline test for the live-e2e plumbing (plan steps 1.1 / 1.2).

The live runner spends quota, so it is NOT in the suite — but its machinery (the
cost Meter, COST.md, the per-run reports produced by _run_full) is verified here
with STUB agents and the deterministic implementer. No quota, no network.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import cost as cost_mod  # noqa: E402


# ─── cost meter ───────────────────────────────────────────────────────


def test_meter_counts_calls_per_role():
    m = cost_mod.Meter()
    dec = m.wrap("decomposer", lambda ctx: {"metrics": {}})
    impl = m.wrap("implementer", lambda ctx: None)
    dec({}); dec({}); impl({})
    rep = m.report()
    assert rep["calls"] == {"decomposer": 2, "implementer": 1}
    assert rep["total_calls"] == 3


def test_meter_sums_tokens_when_reported():
    m = cost_mod.Meter()
    fn = m.wrap("decomposer", lambda ctx: {"_tokens": 120})
    fn({}); fn({})
    assert m.report()["tokens"]["decomposer"] == 240
    assert m.total_tokens() == 240


def test_write_cost_md_without_tokens(tmp_path):
    m = cost_mod.Meter()
    m.wrap("implementer", lambda c: None)({})
    out = tmp_path / "COST.md"
    cost_mod.write_cost_md(m, str(out), model="haiku", case="p1")
    text = out.read_text(encoding="utf-8")
    assert "haiku" in text and "implementer" in text
    assert "**total**" in text and "**1**" in text
    # the json sidecar is written too
    side = json.loads((tmp_path / "COST.json").read_text(encoding="utf-8"))
    assert side["total_calls"] == 1


def test_write_cost_md_with_tokens(tmp_path):
    m = cost_mod.Meter()
    m.wrap("decomposer", lambda c: {"_tokens": 50})({})
    out = tmp_path / "COST.md"
    cost_mod.write_cost_md(m, str(out))
    assert "Tokens" in out.read_text(encoding="utf-8")


# ─── _run_full wiring with the meter (deterministic agents) ───────────


def test_run_full_writes_cost_and_reports(tmp_path, monkeypatch):
    import run_cases as rc

    tools = rc._load_tools()
    case = {
        "name": "harness-case",
        "goal": "tiny service",
        "target": "x correct",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        # blueprint = deterministic decomposer input (the plugin builds the
        # tree itself); never read by the engine as a finished structure
        "blueprint": {"id": "L0", "title": "Svc",
                      "metrics": {"modules": 1, "tasks": 4, "interfaces": 1,
                                  "estimated_loc": 80, "open_decisions": 0,
                                  "single_concern": True, "testable_criteria": True}},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    case_dir = tmp_path / "run"
    case_dir.mkdir()
    meter = cost_mod.Meter()
    # implementer='llm' but we monkeypatch the live adapter to a stub -> the
    # meter still counts the call, no quota spent
    from harness import llm_implementer
    monkeypatch.setattr(llm_implementer, "implement", lambda ctx: None)

    full = rc._run_full(case, case_dir, "execute", tools,
                        decomposer="case", implementer="llm",
                        meter=meter, model="haiku")
    # the leaf was "implemented" once -> one metered call
    assert meter.calls["implementer"] == 1
    # all the per-run reports exist
    assert (case_dir / "report.md").is_file()
    assert (case_dir / "trace.jsonl").is_file()
    assert (case_dir / "COST.md").is_file()
    assert full["tasks"] >= 1
