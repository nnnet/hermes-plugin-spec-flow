"""Real-worker mode, offline part.

Covers everything that must hold BEFORE a live run is worth its quota:
  * profile yaml → claude tool policy mapping (the decomposer really cannot
    Bash; only the implementer can; disabled wins over enabled),
  * the worker prompts are built from the REAL SKILL.md files,
  * the engine consultation points: reviewer verdict (REJECT bumps version
    and records a loop), researcher overrides the spike recommendation,
    verifier verdict replaces the simulated integrate PASS.

The live claude session is stubbed — live behaviour is exercised by
``run_cases.py --workers real`` (the industrial run itself).
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng         # noqa: E402
from harness import role_worker as rw         # noqa: E402

BIG = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
       "open_decisions": 0, "single_concern": False, "testable_criteria": True}
SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

GOAL = {
    "name": "rw-goal",
    "goal": "Self-serve invoicing for freelancers",
    "target": "first invoice in < 3 min",
    "constitution": ["No card data stored."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}


def simple_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(BIG),
                "children": [{"id": "editor", "title": "Invoice editor"},
                             {"id": "delivery", "title": "Invoice delivery"}],
                "spike": {"question": "Reuse an OSS core?",
                          "recommendation": "decomposer's own guess"}}
    return {"metrics": dict(SMALL)}


# ── profile policy mapping ───────────────────────────────────────────────


def test_decomposer_profile_cannot_shell():
    allowed, disallowed = rw.load_profile_policy("spec-decomposer")
    assert "Bash" not in allowed          # terminal/code_execution are cut
    assert "Bash" in disallowed
    assert "Read" in allowed and "Write" in allowed   # file toolset


def test_implementer_profile_can_shell_but_not_browse():
    allowed, disallowed = rw.load_profile_policy("implementer")
    assert "Bash" in allowed
    assert "WebFetch" in disallowed       # browser disabled


def test_reviewer_profile_reads_but_cannot_delegate():
    allowed, disallowed = rw.load_profile_policy("spec-reviewer")
    assert "Read" in allowed
    assert "Task" in disallowed           # delegation disabled


def test_disabled_wins_over_enabled_overlap():
    # implementer enables code_execution AND terminal (both → Bash); if a
    # profile disabled one of them the tool must not survive in allowed
    allowed, disallowed = rw.load_profile_policy("spec-decomposer")
    assert not set(allowed) & set(disallowed)


# ── worker assembly uses the real skill text ────────────────────────────


def test_worker_session_gets_real_skill_md(monkeypatch):
    captured = {}

    def fake_run(prompt, *, system, allowed, disallowed, cwd, model):
        captured.update(system=system, allowed=allowed, prompt=prompt)
        return '{"atomic": true, "metrics": {"modules": 1, "tasks": 2, ' \
               '"interfaces": 1, "estimated_loc": 50, "open_decisions": 0, ' \
               '"single_concern": true, "testable_criteria": true}}'

    monkeypatch.setattr(rw, "_run_claude", fake_run)
    dec = rw.make_decomposer()
    out = dec({"project": {"goal": "g", "target": "t", "constitution": []},
               "node": {"id": "n1", "title": "Node"}, "parent": None,
               "depth": 1, "ancestors": ["Root"],
               "existing_nodes": [{"id": "a", "title": "A"}]})
    assert out["atomic"] is True
    # system prompt is the SKILL.md verbatim, not a paraphrase
    real_md = rw.load_skill_md("spec-flow-decompose")
    assert captured["system"] == real_md
    # tree context made it into the task prompt
    assert "a (A)" in captured["prompt"] and "Root" in captured["prompt"]


def test_leaf_depth_clamp_drops_children(monkeypatch):
    def fake_run(prompt, **kw):
        return '{"atomic": false, "metrics": {"modules": 2, "tasks": 9, ' \
               '"interfaces": 2, "estimated_loc": 400, "open_decisions": 0, ' \
               '"single_concern": false, "testable_criteria": true}, ' \
               '"children": [{"id": "x", "title": "X"}]}'
    monkeypatch.setattr(rw, "_run_claude", fake_run)
    dec = rw.make_decomposer()
    out = dec({"project": {"goal": "g", "target": "", "constitution": []},
               "node": {"id": "deep", "title": "Deep"}, "parent": "p",
               "depth": rw.LEAF_DEPTH})
    assert "children" not in out


# ── engine consultation points ───────────────────────────────────────────


def test_reviewer_reject_records_loop_and_bumps_version(plugin, tmp_path):
    rejected = []

    def reviewer(ctx):
        if ctx["node"] == "editor":
            rejected.append(ctx)
            return {"verdict": "REJECT", "reasons": ["acceptance not testable"]}
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "reviewer": reviewer})
    loops = [l for l in res.loops if l["type"] == "spec-review-reject"]
    assert loops and loops[0]["task"] == "editor"
    assert res.tasks["editor"].version >= 2          # bumped on reject
    assert rejected and rejected[0]["spec"].endswith(".md")
    verdicts = {e.verdict for e in res.events if e.gate == "spec_review"}
    assert {"PASS", "REJECT"} <= verdicts


def test_researcher_overrides_spike_recommendation(plugin, tmp_path):
    def researcher(ctx):
        assert "Reuse an OSS core?" == ctx["question"]
        return {"recommendation": "researched: build thin core", "basis": "n/a"}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "researcher": researcher})
    folded = [e for e in res.events if "folded into spec" in e.action]
    assert folded and "researched: build thin core" in folded[0].detail


def test_verifier_fail_is_recorded(plugin, tmp_path):
    def verifier(ctx):
        return {"status": "FAIL", "detail": "acceptance unmet: no e2e path"}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "verifier": verifier})
    fails = [l for l in res.loops if l["type"] == "integrate-fail"]
    assert fails and "acceptance unmet" in fails[0]["detail"]
    assert any(e.gate == "integrate_verify" and e.verdict == "FAIL"
               for e in res.events)


def test_without_workers_behaviour_unchanged(plugin, tmp_path):
    # no reviewer/researcher/verifier attached → the historical simulated
    # events stay exactly as before (no spec_review / integrate_verify gates)
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer})
    assert not [e for e in res.events if e.gate in ("spec_review", "integrate_verify")]
    assert not [l for l in res.loops
                if l["type"] in ("spec-review-reject", "integrate-fail")]
