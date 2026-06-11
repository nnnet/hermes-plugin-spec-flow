"""Battle test — implementer 'no stubs' guard + independent judge (4.3 / 4.5).

4.3: the live implementer's 'no stubs' contract is ENFORCED deterministically —
placeholder code (NotImplementedError / TODO / bare pass / `...`) and hollow
tests (<2 asserts, assert-True-only) are rejected, not just discouraged.
4.5: an optional injected judge scores each leaf's code against its spec; a fail
verdict is recorded as a `judge` gate + a rework loop. Both verified OFFLINE.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng              # noqa: E402
from harness import auto_implementer as auto_impl  # noqa: E402
from harness import llm_implementer as li          # noqa: E402
from harness import llm_judge as lj                # noqa: E402

GOOD_CODE = ('def f(payload=None):\n'
             '    out = {"status": "ok"}\n'
             '    if payload:\n        out["n"] = len(payload)\n'
             '    return out\n')
GOOD_TEST = ('from f import f\n\n'
             'def test_ok():\n    assert f()["status"] == "ok"\n'
             'def test_n():\n    assert f([1, 2])["n"] == 2\n')


# ─── 4.3: stub rejection ──────────────────────────────────────────────


def test_real_code_passes():
    li._reject_stub(GOOD_CODE, GOOD_TEST)   # no raise


@pytest.mark.parametrize("bad", [
    'def f(payload=None):\n    raise NotImplementedError\n',
    'def f(payload=None):\n    pass  # stub\n',
    'def f(payload=None):\n    # TODO implement\n    return {}\n',
    'def f(payload=None):\n    ...\n',
])
def test_stub_code_rejected(bad):
    with pytest.raises(ValueError):
        li._reject_stub(bad, GOOD_TEST)


def test_hollow_test_rejected():
    with pytest.raises(ValueError):
        li._reject_stub(GOOD_CODE, 'def test_x():\n    assert True\n')


def test_one_assert_test_rejected():
    with pytest.raises(ValueError):
        li._reject_stub(GOOD_CODE, 'from f import f\ndef test_x():\n    assert f()\n')


def test_implementer_raises_on_stub_reply(plugin, tmp_path):
    # a model that returns a stub -> the implementer adapter raises
    stub_reply = json.dumps({"code": "def kv(payload=None):\n    raise NotImplementedError\n",
                             "test": "def test_x():\n    assert True\n"})
    impl = li.make_implementer(ask=lambda p: stub_reply)

    class _WS:
        def _write(self, *a, **k):
            pass

    with pytest.raises(ValueError):
        impl({"node": "kv", "title": "KV", "workspace": _WS(), "spec": "s"})


# ─── 4.5: independent judge ───────────────────────────────────────────

_LEAF_PROJECT = {
    "name": "judge-case",
    "goal": "a leaf to be judged",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {"id": "widget", "title": "Widget",
             "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
                         "open_decisions": 0, "single_concern": True, "testable_criteria": True}},
}


def _run(plugin, tmp_path, judge=None):
    agents = {"implementer": auto_impl.implement}
    if judge:
        agents["judge"] = judge
    return eng.run_project(dict(_LEAF_PROJECT), workspace=str(tmp_path / "wk"),
                           depth="execute", tools=plugin.tools, agents=agents)


def test_no_judge_no_judge_gate(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    assert res.gate_calls.get("judge", 0) == 0


def test_passing_judge_records_gate(plugin, tmp_path):
    res = _run(plugin, tmp_path, judge=lambda ctx: {"verdict": "pass", "reasons": "ok"})
    assert res.gate_calls.get("judge", 0) == 1
    assert any(e.gate == "judge" and e.verdict == "PASS" for e in res.events)
    assert not any(l["type"] == "judge-reject" for l in res.loops)


def test_failing_judge_triggers_rework(plugin, tmp_path):
    res = _run(plugin, tmp_path,
               judge=lambda ctx: {"verdict": "fail", "reasons": "missing edge case"})
    assert any(e.gate == "judge" and e.verdict == "FAIL" for e in res.events)
    rej = [l for l in res.loops if l["type"] == "judge-reject"]
    assert rej and rej[0]["task"] == "widget"
    assert res.tasks["widget"].version >= 2


def test_judge_receives_real_code(plugin, tmp_path):
    seen = {}

    def judge(ctx):
        seen.update(ctx)
        return {"verdict": "pass", "reasons": ""}

    _run(plugin, tmp_path, judge=judge)
    assert seen["node"] == "widget"
    assert "def" in seen["code"]            # the real produced code was passed
    assert "assert" in seen["test"]


def test_judge_error_does_not_crash_run(plugin, tmp_path):
    def boom(ctx):
        raise RuntimeError("judge down")

    res = _run(plugin, tmp_path, judge=boom)
    # run still completes; gate recorded as a non-pass
    assert res.tasks["widget"].status == "done"
    assert res.gate_calls.get("judge", 0) == 1


# ─── judge adapter parsing (offline) ──────────────────────────────────


def test_judge_parse_pass_fail():
    j = lj.make_judge(ask=lambda p: '{"verdict": "fail", "reasons": "nope"}')
    out = j({"node": "x", "code": "c", "test": "t"})
    assert out["verdict"] == "fail" and out["reasons"] == "nope"


def test_judge_parse_rejects_bad_verdict():
    j = lj.make_judge(ask=lambda p: '{"verdict": "maybe"}')
    with pytest.raises(ValueError):
        j({"node": "x"})
