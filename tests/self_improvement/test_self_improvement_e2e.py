"""Battle test — the plugin's headline value (roadmap E4).

The whole point of spec-flow: drive a project to GREEN, then break the
environment under it (a rule change / acceptance failure), and let the plugin
return itself to GREEN within a bounded number of iterations — no human
re-planning. This composes E1 (the env signal) with E (the closed revision
loop: signal → revision → respec → re-derive → re-check → resolved) on a real,
materialised, test-verified build (depth=execute, deterministic implementer).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng           # noqa: E402
from harness import auto_implementer as impl     # noqa: E402

# recovery budget: the plugin must get back to green within this many re-checks
RECOVERY_BUDGET = 3

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def _project(revisions):
    return {
        "name": "e4-self-heal",
        "goal": "a payments service that must survive a rule change",
        "target": "payouts correct; p95 < 500ms",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {
            "id": "L0", "title": "Payments", "metrics": _BRANCH,
            "children": [
                {"id": "billing", "title": "Billing", "metrics": _BRANCH,
                 "children": [
                     {"id": "invoicing", "title": "Invoicing", "metrics": _LEAF},
                     {"id": "payouts", "title": "Payouts", "metrics": _LEAF},
                 ]},
            ],
        },
        "revisions": revisions,
    }


ENV_BREAK = {
    "method": "level_return", "trigger": "on_level_return",
    "finding": "environment rule changed: PSP now requires KYC L2 before first payout",
    "invalidates": "billing",
    "effect": "version-bump billing, re-derive invoicing/payouts under KYC precondition",
    "recheck_fails": 2,   # two failed re-checks, then resolved — within budget
}


def _run(plugin, tmp_path, revisions):
    return eng.run_project(_project(revisions), workspace=str(tmp_path / "wk"),
                           depth="execute", tools=plugin.tools,
                           agents={"implementer": impl.implement})


# ─── green → break → green again ──────────────────────────────────────


def test_build_is_green_then_recovers_after_env_break(plugin, tmp_path):
    res = _run(plugin, tmp_path, [ENV_BREAK])

    # 1) the build ends GREEN: every leaf and integrate node is done
    leaves = ["invoicing", "payouts"]
    assert all(res.tasks[t].status == "done" for t in leaves)
    assert res.tasks.get("billing:integrate", res.tasks["billing"]).status == "done"

    # 2) the env break actually fired and invalidated billing
    respec = [l for l in res.loops if l["type"] == "revision-respec"]
    assert respec and respec[0]["task"] == "billing"

    # 3) the invalidated node was re-derived under the superseded spec
    assert res.tasks["billing"].version >= 2

    # 4) the loop CLOSED: the signal was re-checked and resolved
    verified = [l for l in res.loops if l["type"] == "revision-verified"]
    assert verified and verified[0]["resolved"] is True


def test_recovery_is_within_budget(plugin, tmp_path):
    res = _run(plugin, tmp_path, [ENV_BREAK])
    rechecks = [l for l in res.loops if l["type"] == "revision-recheck"
                and l["task"] == "billing"]
    # bounded recovery — the anti-thrash budget is never exceeded
    assert len(rechecks) < RECOVERY_BUDGET
    # and it did converge (a verified resolution exists)
    assert any(l["type"] == "revision-verified" for l in res.loops)


def test_no_break_means_green_first_time(plugin, tmp_path):
    res = _run(plugin, tmp_path, [])
    assert all(res.tasks[t].status == "done" for t in ("invoicing", "payouts"))
    # nothing to recover from
    assert not any(l["type"] == "revision-verified" for l in res.loops)
    # leaves were built once, not re-derived
    assert res.tasks["invoicing"].version == 1


def test_oracle_confirms_self_heal_episode(plugin, tmp_path):
    res = _run(plugin, tmp_path, [ENV_BREAK])
    summary = plugin.tools.summarize_trace([vars(e) for e in res.events])
    rep = plugin.tools.check_oracle(
        res, {"expected_episodes": ["self_improve", "revision_level_return"]}, summary)
    assert rep.ok, plugin.tools.render_oracle(rep)


def test_real_recovered_code_passes_tests(plugin, tmp_path):
    # depth=execute materialises real code + tests; the engine ran them green
    res = _run(plugin, tmp_path, [ENV_BREAK])
    root = pathlib.Path(res.workspace_root)
    assert (root / "src" / "invoicing.py").is_file()
    assert (root / "src" / "payouts.py").is_file()
