"""Battle test — the offline Kanban-execution core (roadmap Ф8.1 + Ф8.2).

F1/F3/F2 logic without the Hermes transport: a run becomes a board PLAN
(cards + parents, replayable as `hermes kanban` commands), the Board applies
respec block/unblock to ONLY the affected DAG radius, and the offline
dispatcher executes the board in deterministic waves with a fresh worker per
card. When Hermes arrives, only the transport changes.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

bd = eng.board

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

PROJECT = {
    "name": "board-case",
    "goal": "service for the offline board",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Service", "metrics": _BRANCH,
        "children": [
            {"id": "auth", "title": "Auth", "metrics": _BRANCH,
             "children": [
                 {"id": "signup", "title": "Signup", "metrics": _LEAF},
                 {"id": "login", "title": "Login", "metrics": _LEAF},
             ]},
            {"id": "billing", "title": "Billing", "metrics": _LEAF},
        ],
    },
}


@pytest.fixture
def run(plugin, tmp_path):
    return eng.run_project(dict(PROJECT), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools)


# ─── Ф8.1: board plan from a run ──────────────────────────────────────


def test_plan_has_one_card_per_task(run):
    cards = bd.build_board_plan(run)
    ids = {c["id"] for c in cards}
    assert ids == set(run.tasks)
    # parent links survive into the plan
    by = {c["id"]: c for c in cards}
    assert by["signup"]["parents"]          # child points at its parent chain


def test_plan_replays_as_kanban_commands(run):
    cards = bd.build_board_plan(run)
    cmds = bd.to_kanban_commands(cards, "myboard")
    assert len(cmds) == len(cards)
    first = cmds[0]
    assert first[0] == "create" and "--board" in first and "myboard" in first
    # cards with parents carry --parent
    with_parent = [c for c in cmds if "--parent" in c]
    assert with_parent


def test_respec_blocks_only_the_affected_radius(run):
    board = bd.Board(bd.build_board_plan(run))
    blocked = board.block_subtree("auth", reason="KYC rule changed")
    # auth + its dependents are blocked...
    assert "auth" in blocked
    assert board.status("auth") == "blocked"
    # ...but billing (outside the radius) is untouched
    assert board.status("billing") == "todo"
    assert "billing" not in blocked


def test_unblock_releases_the_radius_back_to_todo(run):
    board = bd.Board(bd.build_board_plan(run))
    board.block_subtree("auth")
    released = board.unblock_subtree("auth")
    assert set(released) == set(board.subtree("auth"))
    assert board.status("auth") == "todo"


# ─── Ф8.2: offline dispatcher ─────────────────────────────────────────


def test_dispatch_runs_to_completion_in_dependency_order(run):
    board = bd.Board(bd.build_board_plan(run))
    seen = []
    lock = threading.Lock()

    def worker(card):
        with lock:
            seen.append(card["id"])

    rep = bd.dispatch(board, worker, max_workers=4)
    assert rep["completed"] == len(board.cards)
    assert not rep["failed"] and not rep["starved"]
    # every parent is done strictly before its child starts
    pos = {cid: i for i, cid in enumerate(rep["order"])}
    for cid, c in board.cards.items():
        for p in c["parents"]:
            if p in pos:
                assert pos[p] < pos[cid], f"{p} must finish before {cid}"


def test_dispatch_waves_are_parallel_groups(run):
    board = bd.Board(bd.build_board_plan(run))
    rep = bd.dispatch(board, lambda card: None)
    # more than one wave (a tree is layered), first wave = roots only
    assert len(rep["waves"]) > 1
    roots = {cid for cid, c in board.cards.items()
             if not any(p in board.cards for p in c["parents"])}
    assert set(rep["waves"][0]) <= roots


def test_failed_worker_starves_dependents_only(run):
    board = bd.Board(bd.build_board_plan(run))

    def worker(card):
        if card["id"] == "auth:integrate":
            raise RuntimeError("worker died")

    rep = bd.dispatch(board, worker)
    assert "auth:integrate" in rep["failed"]
    # dependents of the failed card never started; the rest completed
    assert all(board.cards[s]["parents"] for s in rep["starved"])
    assert board.status("billing") == "done"


def test_fresh_worker_per_card(run):
    # each card gets its own worker CALL (no shared mutable state required)
    board = bd.Board(bd.build_board_plan(run))
    calls = []
    lock = threading.Lock()

    def worker(card):
        with lock:
            calls.append(card["id"])

    rep = bd.dispatch(board, worker)
    assert sorted(calls) == sorted(board.cards)
    assert rep["counts"] == {"done": len(board.cards)}
