"""Pure-unit tests for the declarative orchestra workflow driver.

These exercise orchestra_workflow.py in ISOLATION — no LLM, no I/O, no
role_worker — proving the step-ordering logic on its own: sequential order, a
conditional tester<->fixer loop that exits on green, the max-iterations guard,
and that an unknown `when` is treated as never."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import orchestra_workflow as owf   # noqa: E402


def _drive(plan, outcomes):
    """Run a plan, feeding each yielded role its scripted pass/fail outcome.

    `outcomes` maps role -> list of (passed, wrote) the role returns on each of
    its (1st, 2nd, ...) visits; a missing role/visit defaults to (False, True).
    Returns the ordered list of roles the driver yielded."""
    state = owf.WorkflowState()
    seen = []
    counts: dict[str, int] = {}
    for role in plan.run(state):
        seen.append(role)
        i = counts.get(role, 0)
        counts[role] = i + 1
        script = outcomes.get(role, [])
        passed, wrote = script[i] if i < len(script) else (False, True)
        state.passed, state.wrote = passed, wrote
    return seen


# ── sequential (default / today) ──────────────────────────────────────────

def test_sequential_yields_declared_order_once():
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    plan = owf.WorkflowPlan.from_team(team)
    assert plan.is_sequential
    assert _drive(plan, {}) == ["architect", "coder", "tester", "fixer"]


def test_sequential_keeps_duplicate_roles():
    team = [{"role": "coder"}, {"role": "coder"}, {"role": "tester"}]
    plan = owf.WorkflowPlan.from_team(team)
    assert _drive(plan, {}) == ["coder", "coder", "tester"]


def test_process_value_other_than_graph_is_sequential():
    team = [{"role": "coder"}, {"role": "tester"}]
    plan = owf.WorkflowPlan.from_team(team, process="parallel")
    assert plan.is_sequential
    assert _drive(plan, {}) == ["coder", "tester"]


def test_roles_accept_bare_strings():
    plan = owf.WorkflowPlan.from_team(["architect", "coder"])
    assert _drive(plan, {}) == ["architect", "coder"]


# ── graph: evaluator-optimizer tester<->fixer loop ────────────────────────

_GRAPH = {
    "start": "architect",
    "edges": [
        {"from": "architect", "to": "coder"},
        {"from": "coder", "to": "tester"},
        {"from": "tester", "to": "fixer", "when": "tests_failed"},
        {"from": "fixer", "to": "tester", "when": "retry"},
        {"from": "tester", "to": "DONE", "when": "tests_passed"},
    ],
}


def test_graph_loops_tester_fixer_then_exits_on_green():
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    plan = owf.WorkflowPlan.from_team(team, workflow=_GRAPH)
    assert not plan.is_sequential
    # architect/coder neutral; tester RED on its 1st visit -> fixer (still RED,
    # so `retry` fires) -> tester GREEN on 2nd visit -> DONE.
    outcomes = {
        "tester": [(False, True), (True, True)],
        "fixer": [(False, True)],
    }
    assert _drive(plan, outcomes) == \
        ["architect", "coder", "tester", "fixer", "tester"]


def test_graph_exits_immediately_when_tester_green_first_try():
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    plan = owf.WorkflowPlan.from_team(team, workflow=_GRAPH)
    outcomes = {"tester": [(True, True)]}      # green on the first run
    assert _drive(plan, outcomes) == ["architect", "coder", "tester"]


def test_graph_start_defaults_to_first_role_when_unset():
    team = [{"role": "coder"}, {"role": "tester"}]
    wf = {"edges": [{"from": "coder", "to": "tester"},
                    {"from": "tester", "to": "DONE", "when": "always"}]}
    plan = owf.WorkflowPlan.from_team(team, workflow=wf)
    assert plan.start == "coder"
    assert _drive(plan, {}) == ["coder", "tester"]


# ── guards & condition vocabulary ─────────────────────────────────────────

def test_max_iterations_guard_bounds_a_runaway_loop():
    # tester never goes green, fixer always retries -> would spin forever.
    team = [{"role": "tester"}, {"role": "fixer"}]
    wf = {"start": "tester", "edges": [
        {"from": "tester", "to": "fixer", "when": "tests_failed"},
        {"from": "fixer", "to": "tester", "when": "retry"},
    ]}
    plan = owf.WorkflowPlan.from_team(team, workflow=wf)
    seen = _drive(plan, {})                 # all visits default to RED
    # guard == len(roles)*3 == 6; the walk must stop at that cap, not hang.
    assert len(seen) == plan.max_iterations == 6
    assert set(seen) == {"tester", "fixer"}


def test_unknown_when_is_treated_as_never():
    team = [{"role": "coder"}, {"role": "tester"}]
    wf = {"start": "coder", "edges": [
        {"from": "coder", "to": "tester", "when": "whenever_i_feel_like_it"},
    ]}
    plan = owf.WorkflowPlan.from_team(team, workflow=wf)
    # coder's only edge has an unknown `when` -> never fires -> DONE after coder.
    assert _drive(plan, {}) == ["coder"]


def test_edge_matches_vocabulary():
    s_red = owf.WorkflowState(passed=False)
    s_green = owf.WorkflowState(passed=True)
    assert owf._edge_matches("always", s_red)
    assert owf._edge_matches("tests_passed", s_green)
    assert not owf._edge_matches("tests_passed", s_red)
    assert owf._edge_matches("tests_failed", s_red)
    assert not owf._edge_matches("tests_failed", s_green)
    assert owf._edge_matches("retry", s_red)
    assert not owf._edge_matches("retry", s_green)
    assert not owf._edge_matches("bogus", s_red)


# ── config sourcing helpers ───────────────────────────────────────────────

def test_workflow_of_extracts_graph_from_dict():
    raw = {"specialists": [{"role": "coder"}], "workflow": _GRAPH,
           "process": "sequential"}
    wf, proc = owf.workflow_of(raw)
    assert wf is _GRAPH and proc == "sequential"


def test_workflow_of_bare_list_is_none():
    assert owf.workflow_of([{"role": "coder"}]) == (None, None)


def test_team_config_raw_prefers_env_json():
    import json
    env = {"SPEC_FLOW_IMPLEMENTER_TEAM":
           json.dumps({"specialists": [{"role": "coder"}], "workflow": _GRAPH})}
    fake_lb = type("LB", (), {"WORKERS_CFG": {"implementer": {"team": []}}})
    raw = owf.team_config_raw(fake_lb, env)
    assert isinstance(raw, dict) and raw["workflow"] == _GRAPH


def test_team_config_raw_falls_back_to_workers_cfg():
    fake_lb = type("LB", (), {
        "WORKERS_CFG": {"implementer": {"team": {"specialists": [],
                                                 "process": "sequential"}}}})
    raw = owf.team_config_raw(fake_lb, {})
    assert raw == {"specialists": [], "process": "sequential"}


def test_team_config_raw_bad_env_json_returns_none():
    raw = owf.team_config_raw(
        type("LB", (), {"WORKERS_CFG": {}}), {"SPEC_FLOW_IMPLEMENTER_TEAM": "{x"})
    assert raw is None
