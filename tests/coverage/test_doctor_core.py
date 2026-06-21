"""Doctor core (Ф0): pure control-loop math, config layering, ladder dispatch.

Offline — no LLM, no engine. Locks the invariants the runner will lean on.
"""
import pathlib
import sys

# repo root (spec-flow/) holds the plugin modules; the runner imports them via a
# resilient fallback, the test just puts the root on the path.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import spec_flow_remedies as rem            # noqa: E402
import spec_flow_doctor as doc              # noqa: E402
from spec_flow_doctor import (Context, Finding, Diagnosis)  # noqa: E402


# --- config layering ---------------------------------------------------------
def test_defaults_present():
    cfg = rem.doctor_config()
    assert cfg["enabled"] is False
    assert "rework" in cfg["default_ladder"]
    assert len(rem.causes_config()) == 15
    assert rem.tiers_config()["strong"]["models"] == ["xiaomimimo/mimo-v2.5-pro"]


def test_case_yaml_overrides_defaults():
    project = {"doctor": {"enabled": True},
               "causes": {"task_too_large": {"ladder": ["split"]}},
               "evaluator": {"votes": 3},
               "workers": {"tiers": {"weak": {"models": ["x/y"]}}}}
    assert rem.doctor_config(project)["enabled"] is True
    assert rem.causes_config(project)["task_too_large"]["ladder"] == ["split"]
    assert rem.evaluator_config(project)["votes"] == 3
    # deep-merge keeps siblings: strong tier still present after weak override
    tiers = rem.tiers_config(project)
    assert tiers["weak"]["models"] == ["x/y"]
    assert "strong" in tiers


def test_launch_overrides_beat_case():
    project = {"doctor": {"enabled": False}}
    over = {"doctor": {"enabled": True}}
    assert rem.doctor_config(project, over)["enabled"] is True


# --- pure math ---------------------------------------------------------------
def test_ladder_step_walks_then_escalates():
    state = doc.new_state()
    ladder = ["rework", "escalate_tier", "record"]
    seen = []
    for _ in range(4):
        name, rung = doc.ladder_step("c", "n:g", state, ladder)
        seen.append(name)
    assert seen == ["rework", "escalate_tier", "record", "escalate_level"]


def test_ladder_counter_is_per_place():
    state = doc.new_state()
    ladder = ["a", "b"]
    assert doc.ladder_step("c", "n1:g", state, ladder)[0] == "a"
    # different place starts fresh
    assert doc.ladder_step("c", "n2:g", state, ladder)[0] == "a"
    # same place advances
    assert doc.ladder_step("c", "n1:g", state, ladder)[0] == "b"


def test_oscillation_detects_cycle_not_repeat():
    assert doc.is_oscillation(["A", "B", "A", "B"]) is True
    assert doc.is_oscillation(["A", "B", "A"]) is True          # A-B-A
    assert doc.is_oscillation(["A", "A", "A", "A"]) is False     # same cause
    assert doc.is_oscillation(["A", "B", "C"]) is False
    assert doc.is_oscillation(["A", "B", "C", "A", "B", "C"]) is True


def test_rank_root_before_symptom():
    order = ["hidden_deps", "weak_implementer"]
    f1 = Finding(cause="weak_implementer", detector="prompt")
    f2 = Finding(cause="hidden_deps", detector="deps")
    ranked = doc.rank_causes([f1, f2], order)
    assert ranked[0].cause == "hidden_deps"


def test_set_shrinking():
    assert doc.set_shrinking(None, frozenset({"a"})) is True
    assert doc.set_shrinking(frozenset({"a", "b"}), frozenset({"a"})) is True
    assert doc.set_shrinking(frozenset({"a"}), frozenset({"a"})) is False
    # same size but a prior cause removed (b->c) counts as progress
    assert doc.set_shrinking(frozenset({"a", "b"}), frozenset({"a", "c"})) is True


# --- facade treat() ----------------------------------------------------------
def _diag(cause, gate="spec_review", node="L1"):
    ctx = Context(node=node, gate=gate)
    return Diagnosis(context=ctx, ranked=(Finding(cause=cause, detector="d",
                                                  evidence="ev"),))


def test_treat_walks_cause_ladder():
    d = doc.Doctor(project={"doctor": {"enabled": True}})
    state = doc.new_state()
    # weak_implementer ladder: rework -> escalate_tier -> ...
    a1 = d.treat(_diag("weak_implementer"), state)
    a2 = d.treat(_diag("weak_implementer"), state)
    assert a1.kind == "rework"
    assert a2.kind == "escalate_tier"
    assert a1.record.cause == "weak_implementer"


def test_treat_abstain_routes_to_human():
    d = doc.Doctor(project={"doctor": {"enabled": True}})
    state = doc.new_state()
    diag = Diagnosis(context=Context(node="L1", gate="spec_review"),
                     ranked=(), abstained=True)
    act = d.treat(diag, state)
    assert act.kind in ("ask_human", "redecompose_parent", "respec", "record")


def test_treat_ceiling_records_debt():
    d = doc.Doctor(project={"doctor": {"enabled": True,
                                       "levels": 1, "attempts_per_level": 1}})
    state = doc.new_state()
    state["node_attempts"] = 1          # already at ceiling
    act = d.treat(_diag("weak_implementer"), state)
    assert act.kind == "record"
    assert act.record.outcome == "capped"


# --- diagnosers (Ф1) ---------------------------------------------------------
import spec_flow_diagnosers as dgn       # noqa: E402


def test_scope_findings_to_empty_delta():
    dg = dgn.Diagnosers()
    ev = {"scope_findings": [
        "route-redeclare: restates ['/health','/notes'] and introduces no new route"]}
    out = dg.run(node="delete_note", gate="spec_review", verdict="FAIL",
                 evidence=ev, context=Context(node="delete_note", gate="spec_review"))
    assert any(f.cause == "empty_delta" for f in out)


def test_leaf_metrics_to_task_too_large():
    dg = dgn.Diagnosers()
    out = dg.run(node="big", gate="leaf_check", verdict="branch",
                 evidence={"leaf_reasons": ["modules=8 > 5"]},
                 context=Context(node="big", gate="leaf_check"))
    assert out and out[0].cause == "task_too_large"


def test_hidden_deps_detector():
    dg = dgn.Diagnosers(dgn._Helpers(completed=["a"]))
    ctx = Context(node="c", gate="integrate_verify", depends_on=("a", "b"))
    out = dg.run(node="c", gate="integrate_verify", verdict="FAIL",
                 evidence={}, context=ctx)
    assert out and out[0].cause == "hidden_deps"


def test_find_root_picks_earliest_dep_fail():
    loops = [{"type": "spec-review-reject", "task": "x"},
             {"type": "integrate-fail", "task": "dep1"},
             {"type": "integrate-fail", "task": "dep2"}]
    assert doc.find_root(loops, "c", depends_on=["dep1", "dep2"]) == "dep1"
    assert doc.find_root(loops, "c", depends_on=[]) == "c"


def test_doctor_metrics():
    loops = [
        {"type": "doctor", "cause": "empty_delta", "remedy": "rework", "outcome": "resolved"},
        {"type": "doctor", "cause": "empty_delta", "remedy": "split", "outcome": "open"},
        {"type": "spec-review-reject", "task": "x"},
    ]
    m = doc.doctor_metrics(loops)
    assert m["treatments"] == 2
    assert m["by_cause"]["empty_delta"] == 2
    assert m["resolved_ratio"] == 0.5


def test_classifier_majority_and_cite():
    calls = {"n": 0}

    def fake_ask(prompt, **kw):
        calls["n"] += 1
        return "vague_spec | the spec is ambiguous about the delete path"

    def fake_chain(_tier):
        return ["fake/model"]

    clf = dgn.Classifier({"tier": "strong", "votes": 3, "majority": 2,
                          "require_cite": True}, fake_ask, fake_chain)
    out = clf.run(node="n", gate="spec_review", verdict="REJECT",
                  evidence={"reasons": ["ambiguous"]},
                  context=Context(node="n", gate="spec_review"))
    assert out["abstained"] is False
    assert out["findings"][0].cause == "vague_spec"
    assert calls["n"] == 3


def test_classifier_abstains_without_cite():
    clf = dgn.Classifier({"votes": 1, "require_cite": True},
                         lambda p, **k: "weak_implementer", lambda t: ["m"])
    out = clf.run(node="n", gate="spec_review", verdict="REJECT",
                  evidence={"reasons": ["x"]},
                  context=Context(node="n", gate="spec_review"))
    assert out["abstained"] is True          # no '|' citation -> discarded


def test_classifier_abstain_token():
    clf = dgn.Classifier({"votes": 1, "majority": 1},
                         lambda p, **k: "abstain | nothing clear", lambda t: ["m"])
    out = clf.run(node="n", gate="spec_review", verdict="REJECT",
                  evidence={"reasons": ["x"]},
                  context=Context(node="n", gate="spec_review"))
    assert out["abstained"] is True


def test_diagnose_integrates_and_ranks():
    d = doc.Doctor(project={"doctor": {"enabled": True}},
                   diagnosers=dgn.Diagnosers(dgn._Helpers(completed=[])))
    ctx = Context(node="c", gate="spec_review", depends_on=("x",))
    diag = d.diagnose(node="c", gate="spec_review", verdict="FAIL",
                      evidence={"scope_findings": ["no new route"]}, context=ctx)
    # hidden_deps (root) must rank before empty_delta (symptom)
    causes = [f.cause for f in diag.ranked]
    assert "hidden_deps" in causes and "empty_delta" in causes
    assert causes.index("hidden_deps") < causes.index("empty_delta")
