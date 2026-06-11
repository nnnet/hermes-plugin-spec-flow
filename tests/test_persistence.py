"""Battle test — durable node & run state (roadmap Ф8.3 / C4).

Two layers, both offline:
  * ``NodeLifecycle.snapshot()/restore()`` — one node's lifecycle survives a
    worker restart: same phase, same fired gates; the DONE guard stays armed.
  * the run journal + ``resume=True`` — a restarted run keeps the workspace,
    reads ``.spec-flow/journal.jsonl`` and does NOT re-run the implementer for
    leaves whose artifacts survived. Kanban-runs/redis later replace only the
    storage, not the semantics.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng                 # noqa: E402
from harness import auto_implementer as auto_impl     # noqa: E402

import spec_flow_node_fsm as fsm  # noqa: E402 — plugin dir is on sys.path

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

PROJECT = {
    "name": "resume-case",
    "goal": "a service that survives a restart",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Service", "metrics": _BRANCH,
        "children": [
            {"id": "alpha", "title": "Alpha", "metrics": _LEAF},
            {"id": "beta", "title": "Beta", "metrics": _LEAF},
        ],
    },
}


# ─── node snapshot / restore ──────────────────────────────────────────


@pytest.mark.parametrize("prefer_engine", [True, False])
def test_node_survives_restart_mid_lifecycle(prefer_engine):
    n = fsm.NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    n.advance(fsm.EV_DECOMPOSE)
    n.advance(fsm.EV_CONTRACT)
    snap = n.snapshot()
    # the worker dies here; a fresh one restores the node
    n2 = fsm.NodeLifecycle.restore(snap, prefer_engine=prefer_engine)
    assert n2.phase == n.phase
    assert n2.gates_fired == n.gates_fired
    # and finishes the lifecycle normally
    for ev in (fsm.EV_IMPLEMENT, fsm.EV_REVIEW, fsm.EV_REVIEW_PASS, fsm.EV_DONE):
        n2.advance(ev)
    assert n2.phase == fsm.DONE


@pytest.mark.parametrize("prefer_engine", [True, False])
def test_restored_node_keeps_the_done_guard(prefer_engine):
    n = fsm.NodeLifecycle(prefer_engine=prefer_engine).start("leaf")
    n.advance(fsm.EV_DECOMPOSE)
    n.advance(fsm.EV_CONTRACT)
    n.advance(fsm.EV_IMPLEMENT)
    n.advance(fsm.EV_REVIEW)
    n.advance(fsm.EV_REVIEW_PASS)
    snap = n.snapshot()
    snap["gates"] = [g for g in snap["gates"] if g != "review_pass"]  # tamper
    n2 = fsm.NodeLifecycle.restore(snap, prefer_engine=prefer_engine)
    with pytest.raises(fsm.GateViolation):
        n2.advance(fsm.EV_DONE)


def test_snapshot_roundtrip_is_json_safe():
    import json
    n = fsm.NodeLifecycle().start("branch")
    n.advance(fsm.EV_DECOMPOSE)
    snap = json.loads(json.dumps(n.snapshot()))
    assert fsm.NodeLifecycle.restore(snap).phase == n.phase


def test_restore_rejects_garbage():
    with pytest.raises(ValueError):
        fsm.NodeLifecycle.restore({"kind": "alien", "phase": "DONE"})
    with pytest.raises(ValueError):
        fsm.NodeLifecycle.restore({"kind": "leaf", "phase": "no-such-phase"})


# ─── run journal + resume ─────────────────────────────────────────────


def _counting_impl(counter):
    def impl(ctx):
        counter["n"] += 1
        auto_impl.implement(ctx)
    return impl


def test_resume_skips_completed_leaves(plugin, tmp_path):
    ws = str(tmp_path / "wk")
    c1 = {"n": 0}
    first = eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                            tools=plugin.tools,
                            agents={"implementer": _counting_impl(c1)})
    assert c1["n"] == 2                                  # both leaves built
    assert first.tasks["alpha"].status == "done"

    # the "restart": same workspace, resume mode
    c2 = {"n": 0}
    second = eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                             tools=plugin.tools, resume=True,
                             agents={"implementer": _counting_impl(c2)})
    assert c2["n"] == 0                                  # nothing re-implemented
    # outcome identical: same tasks, all done
    assert {t: v.status for t, v in second.tasks.items()} == \
           {t: v.status for t, v in first.tasks.items()}
    # the persisted artifacts are still there
    root = pathlib.Path(second.workspace_root)
    assert (root / "src" / "alpha.py").is_file()
    assert (root / "src" / "beta.py").is_file()


def test_resume_reimplements_when_artifact_lost(plugin, tmp_path):
    ws = str(tmp_path / "wk")
    eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                    tools=plugin.tools, agents={"implementer": auto_impl.implement})
    # the artifact for beta is lost in the crash — journal alone is not enough
    (pathlib.Path(ws) / "src" / "beta.py").unlink()
    c = {"n": 0}
    eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                    tools=plugin.tools, resume=True,
                    agents={"implementer": _counting_impl(c)})
    assert c["n"] == 1                                   # only beta rebuilt


def test_fresh_run_without_resume_wipes_and_rebuilds(plugin, tmp_path):
    ws = str(tmp_path / "wk")
    c1 = {"n": 0}
    eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                    tools=plugin.tools, agents={"implementer": _counting_impl(c1)})
    c2 = {"n": 0}
    eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                    tools=plugin.tools, agents={"implementer": _counting_impl(c2)})
    assert c2["n"] == 2                                  # no resume -> full rebuild


def test_journal_written_in_workspace(plugin, tmp_path):
    ws = str(tmp_path / "wk")
    eng.run_project(dict(PROJECT), workspace=ws, depth="execute",
                    tools=plugin.tools, agents={"implementer": auto_impl.implement})
    journal = pathlib.Path(ws) / ".spec-flow" / "journal.jsonl"
    assert journal.is_file()
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2                               # one record per leaf
