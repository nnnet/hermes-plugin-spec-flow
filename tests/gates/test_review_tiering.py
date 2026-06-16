"""A1 review tiering: a SIMPLE leaf that passes the deterministic spec lint
skips the LLM reviewer + rework loop entirely; a COMPLEX node still gets the
full review. The classification is deterministic (node metrics), so the saved
review round does not depend on the model. Default OFF — p4/p5 unchanged.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_SIMPLE = {"modules": 1, "tasks": 2, "interfaces": 1, "estimated_loc": 40,
           "open_decisions": 0, "single_concern": True,
           "testable_criteria": True}


def _project(leaf_metrics):
    return {
        "name": "tiering-case", "goal": "one-leaf service", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": "disc", "title": "Discovery",
                               "metrics": dict(leaf_metrics)}]},
    }


def _spy():
    consulted = []

    def reviewer(ctx):
        consulted.append(ctx.get("node"))
        return {"verdict": "PASS", "reasons": []}

    return reviewer, consulted


def _run(tmp_path, leaf=_SIMPLE, review_policy=None):
    reviewer, consulted = _spy()
    res = eng.run_project(_project(leaf), workspace=str(tmp_path / "wk"),
                          depth="spec", agents={"reviewer": reviewer},
                          review_policy=review_policy)
    return res, consulted


def test_simple_leaf_skips_the_reviewer_when_tiering_on(tmp_path):
    res, consulted = _run(tmp_path, review_policy={"tiering": True})
    assert "disc" not in consulted           # the LLM reviewer was skipped
    assert any("review tiering" in e.action for e in res.events
               if e.task == "disc")


def test_tiering_off_by_default_consults_reviewer(tmp_path):
    res, consulted = _run(tmp_path)           # no review_policy → default off
    assert "disc" in consulted


def test_large_leaf_still_reviewed_under_tiering(tmp_path):
    big = dict(_SIMPLE, estimated_loc=200)    # over simple_max_loc
    res, consulted = _run(tmp_path, leaf=big,
                          review_policy={"tiering": True})
    assert "disc" in consulted


def test_open_decisions_force_full_review(tmp_path):
    undecided = dict(_SIMPLE, open_decisions=1)
    res, consulted = _run(tmp_path, leaf=undecided,
                          review_policy={"tiering": True})
    assert "disc" in consulted


def test_branch_is_never_simple(tmp_path):
    # the root branch L0 is always reviewed even with tiering on
    res, consulted = _run(tmp_path, review_policy={"tiering": True})
    assert "L0" in consulted
