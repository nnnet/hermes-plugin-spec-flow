"""Deterministic spec-traceability lint + minimal-edit rework.

The product_discovery class: a reviewer repeated 'AC-5 lacks REQ-5'
through the whole rework budget while rework re-authored from scratch.
Prevention — code checks the mechanical rule BEFORE a reviewer round;
repair — rework edits the PREVIOUS spec instead of re-rolling it."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

lint = eng._runner._lint_spec_traceability

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


# ─── the lint function itself ─────────────────────────────────────────

def test_ac_without_req_flagged():
    md = "REQ-disc-1 lists\nAC-disc-1 ok\nAC-disc-5 pagination defaults"
    out = lint("disc", md)
    assert len(out) == 1 and "REQ-disc-5 is missing" in out[0]


def test_req_without_ac_flagged():
    md = "REQ-disc-1 lists\nREQ-disc-2 search\nAC-disc-1 ok"
    out = lint("disc", md)
    assert len(out) == 1 and "AC-disc-2" in out[0]


def test_clean_and_idless_specs_pass():
    assert lint("disc", "REQ-disc-1 x\nAC-disc-1 y") == []
    assert lint("disc", "a plain spec without ids") == []
    # ids of ANOTHER node never leak into this node's lint
    assert lint("disc", "AC-other-3 belongs elsewhere") == []


# ─── engine wiring: lint fires before the reviewer ────────────────────

# goal-only project: the DECOMPOSER authors specs (a pre-built tree with
# metrics never consults the agent, so there would be nothing to lint)
PROJECT = {
    "name": "lint-case",
    "goal": "one-leaf service", "target": "x",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}

BAD_MD = "## Requirements\nREQ-disc-1 listing\n## Acceptance\nAC-disc-1 ok\nAC-disc-5 pagination"
GOOD_MD = ("## Requirements\nREQ-disc-1 listing\nREQ-disc-5 pagination\n"
           "## Acceptance\nAC-disc-1 ok\nAC-disc-5 pagination")


def _decomposer_with_lint_fix(calls):
    def dec(ctx):
        if ctx.get("rework"):
            calls.append(ctx)
            return {"spec_markdown": GOOD_MD}
        if ctx["depth"] == 0:
            return {"metrics": dict(_BRANCH),
                    "children": [{"id": "disc", "title": "Discovery"}]}
        return {"metrics": dict(_LEAF), "spec_markdown": BAD_MD}
    return dec


def test_lint_fix_round_runs_before_reviewer(tmp_path):
    calls = []
    seen_by_reviewer = []

    def reviewer(ctx):
        seen_by_reviewer.append(ctx["node"])
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(dict(PROJECT), workspace=str(tmp_path / "wk"),
                          depth="spec",
                          agents={"decomposer": _decomposer_with_lint_fix(calls),
                                  "reviewer": reviewer})
    fails = [e for e in res.events
             if e.gate == "spec_lint" and e.verdict == "FAIL"]
    assert fails and "REQ-disc-5 is missing" in fails[0].detail
    # the fix round received the EXACT violation and the previous spec
    assert calls, "lint must trigger an author round"
    assert "DETERMINISTIC LINT" in calls[0]["review_feedback"]
    assert "REQ-disc-5" in calls[0]["review_feedback"]
    assert calls[0]["previous_spec"] == BAD_MD
    # the spec the reviewer finally saw is the FIXED one
    assert any(e.gate == "spec_lint" and e.verdict == "PASS"
               for e in res.events)


def test_unfixable_lint_is_bounded_and_run_survives(tmp_path):
    def stubborn(ctx):
        if ctx.get("rework"):
            return {"spec_markdown": BAD_MD}     # never fixes it
        if ctx["depth"] == 0:
            return {"metrics": dict(_BRANCH),
                    "children": [{"id": "disc", "title": "Discovery"}]}
        return {"metrics": dict(_LEAF), "spec_markdown": BAD_MD}

    res = eng.run_project(dict(PROJECT), workspace=str(tmp_path / "wk"),
                          depth="spec", agents={"decomposer": stubborn})
    fails = [e for e in res.events
             if e.gate == "spec_lint" and e.verdict == "FAIL"]
    assert 1 <= len(fails) <= 2, "the lint loop is bounded"
    assert "disc" in res.tasks       # the run carried on


# ─── worker prompt: minimal-edit rework carries the previous spec ─────

def test_rework_prompt_contains_previous_spec(monkeypatch, fake_openai):
    import json as _json

    from harness import role_worker as rw
    from harness_fakeapi import ok

    # a real local server answers the decomposer; we then read the prompt the
    # harness actually put on the wire (no stubbing of our code). Resolve the
    # decomposer to a free id (config seam) so the free-only gate lets the real
    # request reach the local server.
    monkeypatch.setattr(rw, "_model_for",
                        lambda role, specialty="": "openrouter/x:free")
    srv = fake_openai([(200, ok(_json.dumps(
        {"atomic": True, "metrics": dict(_LEAF), "spec_markdown": GOOD_MD})))])
    dec = rw.make_decomposer()
    dec({"project": {"goal": "g", "target": "t", "constitution": []},
         "node": {"id": "disc", "title": "D"}, "parent": "L0", "depth": 3,
         "ancestors": [], "rework": True,
         "review_feedback": "AC-disc-5 lacks REQ-disc-5",
         "previous_spec": BAD_MD})
    sent = " ".join(m.get("content", "")
                    for m in srv.requests[0]["messages"])
    assert "YOUR PREVIOUS SPEC (verbatim)" in sent
    assert BAD_MD in sent
    assert "MINIMAL edit" in sent
