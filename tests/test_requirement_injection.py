"""Late-requirement INJECTION: which level, at which run stage.

The ENGINE owns placement (a worker's whim put web_ui under
buyer_discovery in a live run — wrong subtree):

  * unscoped requirement            → direct ROOT child (its acceptance
    runs on the assembled product — root integrate is its scope);
  * ``@scope: <branch>``            → child of THAT branch, while the
    branch is still open;
  * scope already missed / unknown  → ROOT fallback (last chance);
  * name already covered by a node  → no injection at all.

Stages are simulated through a stateful requirements source: the engine
re-reads it at every placement point, so a requirement can "arrive"
mid-run — after one branch closed, before another opened."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}

PROJECT = {
    "name": "req-injection-case",
    "goal": "two-branch service to test late-requirement placement",
    "target": "x",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Root", "metrics": dict(_BRANCH),
        "children": [
            {"id": "alpha", "title": "Alpha branch",
             "metrics": dict(_BRANCH),
             "children": [{"id": "a1", "title": "A1",
                           "metrics": dict(_LEAF)}]},
            {"id": "beta", "title": "Beta branch",
             "metrics": dict(_BRANCH),
             "children": [{"id": "b1", "title": "B1",
                           "metrics": dict(_LEAF)}]},
        ],
    },
}


def _run(tmp_path, reqs_fn, agents=None):
    return eng.run_project(dict(PROJECT), workspace=str(tmp_path / "wk"),
                           depth="spec", agents=agents,
                           standing_requirements=reqs_fn)


def _children_ids(tree, nid):
    if tree["id"] == nid:
        return [c["id"] for c in tree.get("children", [])]
    for c in tree.get("children", []):
        found = _children_ids(c, nid)
        if found is not None:
            return found
    return None


# ─── level selection ──────────────────────────────────────────────────

def test_unscoped_requirement_lands_at_root(tmp_path):
    res = _run(tmp_path, lambda: [("web_ui", "HTML pages over the whole"
                                   " product: catalog + payouts")])
    assert "web_ui" in res.tasks
    assert "web_ui" in _children_ids(res.project["tree"], "L0")
    assert "web_ui" not in _children_ids(res.project["tree"], "alpha")
    assert "web_ui" not in _children_ids(res.project["tree"], "beta")
    att = [e for e in res.events
           if e.gate == "requirement" and e.verdict == "ATTACHED"]
    assert att and "root level" in att[0].action


def test_scoped_requirement_lands_inside_its_branch(tmp_path):
    res = _run(tmp_path, lambda: [
        ("beta_audit", "@scope: beta\naudit log for beta operations",
         "beta")])
    assert "beta_audit" in _children_ids(res.project["tree"], "beta")
    assert "beta_audit" not in _children_ids(res.project["tree"], "L0")
    att = [e for e in res.events
           if e.gate == "requirement" and e.verdict == "ATTACHED"]
    assert att and "scoped branch" in att[0].action


def test_unknown_scope_falls_back_to_root(tmp_path):
    res = _run(tmp_path, lambda: [
        ("ghost", "scoped to a branch that does not exist", "no_such")])
    assert "ghost" in _children_ids(res.project["tree"], "L0")


def test_covered_name_is_never_injected(tmp_path):
    res = _run(tmp_path, lambda: [("alpha", "same name as a live branch")])
    # 'alpha' exists as a tree node — no duplicate child anywhere
    assert _children_ids(res.project["tree"], "L0").count("alpha") == 1
    assert not [e for e in res.events
                if e.gate == "requirement" and e.verdict == "ATTACHED"]


# ─── arrival stage (the source is re-read at every placement point) ───

def test_requirement_arriving_after_one_branch_targets_the_next(tmp_path):
    # arrives DURING the run: registered when alpha integrates; scoped to
    # beta, which is still open -> must land inside beta, not at root
    box = {"reqs": []}

    def verifier(ctx):
        if ctx["node"] == "alpha":
            box["reqs"] = [("beta_late", "@scope: beta\nlate beta concern",
                            "beta")]
        return {"status": "PASS"}

    res = _run(tmp_path, lambda: box["reqs"], agents={"verifier": verifier})
    assert "beta_late" in _children_ids(res.project["tree"], "beta")
    assert "beta_late" not in _children_ids(res.project["tree"], "L0")


def test_requirement_whose_scope_already_closed_falls_back_to_root(tmp_path):
    # arrives AFTER its scoped branch integrated: alpha is done when the
    # requirement appears (during beta) -> the root fallback must catch it
    box = {"reqs": []}

    def verifier(ctx):
        if ctx["node"] == "beta":
            box["reqs"] = [("alpha_late", "@scope: alpha\ntoo late for"
                            " alpha", "alpha")]
        return {"status": "PASS"}

    res = _run(tmp_path, lambda: box["reqs"], agents={"verifier": verifier})
    assert "alpha_late" in _children_ids(res.project["tree"], "L0")
    assert "alpha_late" not in _children_ids(res.project["tree"], "alpha")


def test_requirement_arriving_at_the_last_moment_still_materializes(tmp_path):
    # appears only when BOTH branches are done (during the last branch
    # integrate) — the root placement point is the final chance
    box = {"reqs": []}
    seen = []

    def verifier(ctx):
        seen.append(ctx["node"])
        if ctx["node"] == "beta":
            box["reqs"] = [("eleventh_hour", "very late, unscoped")]
        return {"status": "PASS"}

    res = _run(tmp_path, lambda: box["reqs"], agents={"verifier": verifier})
    assert "eleventh_hour" in _children_ids(res.project["tree"], "L0")
    assert "eleventh_hour" in res.tasks


# ─── channel parsing: @scope travels from REQUIREMENT.md ──────────────

def test_channel_parses_scope_line(tmp_path):
    from harness import hitl
    ch = hitl.HumanChannel(tmp_path / "hitl")
    d = ch.requirements_dir / "beta_audit"
    d.mkdir(parents=True)
    (d / "REQUIREMENT.md").write_text(
        "@scope: beta\nAudit log for beta operations.", encoding="utf-8")
    e = ch.requirements_dir / "web_ui"
    e.mkdir(parents=True)
    (e / "REQUIREMENT.md").write_text(
        "Cross-cutting HTML UI.", encoding="utf-8")
    reqs = ch.standing_requirements()
    assert ("beta_audit",) == tuple(r[0] for r in reqs if r[2] == "beta")
    assert [r for r in reqs if r[0] == "web_ui"][0][2] is None


def test_childless_branch_is_demoted_to_leaf(tmp_path):
    # v15: the requirement node's metrics tripped the branch guardrail,
    # it proposed no children and integrated EMPTY-green — zero code,
    # green verdict. A branch without children must become a LEAF.
    def dec(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BRANCH),
                    "children": [{"id": "big", "title": "Big feature"}]}
        # branch-sized metrics, NO children proposed
        return {"metrics": dict(_BRANCH)}

    res = eng.run_project(
        {"name": "empty-branch-case", "goal": "g", "target": "x",
         "policy": dict(PROJECT["policy"])},
        workspace=str(tmp_path / "wk"), depth="spec",
        agents={"decomposer": dec})
    demos = [e for e in res.events
             if e.action == "childless branch demoted to leaf"]
    assert demos and demos[0].task == "big"
    # a leaf has NO integrate task of its own
    assert "big:integrate" not in res.tasks
