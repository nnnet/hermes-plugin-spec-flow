"""spec_flow_board — the OFFLINE Kanban-execution core (roadmap Ф8.1/Ф8.2).

Why: the plan's F-block (tree = cards, dispatcher spawns a worker per node,
block→unblock on respec) is mostly LOGIC, not transport. This module holds that
logic as plain, dependency-free plugin code, verified without Hermes:

  * ``build_board_plan(run_result)`` — turn a finished/derived run into a board
    plan: one card per task with parents, assignee (profile) and skill. The
    plan replays onto a live board verbatim (``to_kanban_commands``).
  * ``Board`` — card states (todo/in_progress/done/blocked/failed) + the respec
    semantics: ``block_subtree`` blocks ONLY the affected DAG radius,
    ``unblock_subtree`` releases it after the re-derive.
  * ``dispatch`` — the offline scheduler: every wave takes the READY cards
    (all parents done) and runs a FRESH worker per card concurrently; depth is
    unbounded. With Hermes the gateway replaces only this loop — the waves
    stay the same.

What stays for Hermes: the transport — a live ``hermes kanban``, the process
gateway and the HITL delivery channel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

__all__ = ["build_board_plan", "Board", "dispatch", "to_kanban_commands"]

# card states (offline mirror of the kanban column set)
TODO, IN_PROGRESS, DONE, BLOCKED, FAILED = "todo", "in_progress", "done", "blocked", "failed"


def _tree_parents(project: dict) -> dict[str, str]:
    """node id -> parent node id from the project tree (the run's Task objects
    carry derived-task parents, but tree NODE tasks have none — the tree is the
    authority for those links)."""
    out: dict[str, str] = {}
    root = (project or {}).get("tree") or {}

    def walk(node: dict) -> None:
        for child in node.get("children", []) or []:
            out[child["id"]] = node["id"]
            walk(child)

    if root:
        walk(root)
    return out


def build_board_plan(run_result: Any) -> list[dict]:
    """One card per run task: id, title, parents, assignee (profile), skill.

    Reads the duck-typed ``run_result.tasks`` mapping (id -> task with .title,
    .kind, .profile, .skill, .parents). Derived tasks (impl/review/contract/...)
    already carry parent links; tree NODE tasks get theirs from the project
    tree, so the board holds the COMPLETE DAG."""
    tree_parent = _tree_parents(getattr(run_result, "project", {}) or {})
    cards: list[dict] = []
    for tid, t in run_result.tasks.items():
        parents = list(getattr(t, "parents", []) or [])
        if not parents and tid in tree_parent:
            parents = [tree_parent[tid]]
        cards.append({
            "id": tid,
            "title": getattr(t, "title", tid),
            "kind": getattr(t, "kind", ""),
            "assignee": getattr(t, "profile", "") or "implementer",
            "skill": getattr(t, "skill", "") or "spec-implement",
            "parents": parents,
            "status": TODO,
        })
    return cards


def to_kanban_commands(cards: list[dict], board: str) -> list[list[str]]:
    """Render the plan as replay-ready ``hermes kanban`` argv lists."""
    out: list[list[str]] = []
    for c in cards:
        argv = ["create", "--board", board,
                "--title", f"{c['id']}: {c['title']}",
                "--assignee", c["assignee"], "--skill", c["skill"]]
        for p in c["parents"]:
            argv += ["--parent", p]
        out.append(argv)
    return out


class Board:
    """Offline board: card states + the respec block/unblock semantics."""

    def __init__(self, cards: list[dict]):
        self.cards: dict[str, dict] = {c["id"]: dict(c) for c in cards}
        # children index for subtree walks (the DAG radius of a respec)
        self._children: dict[str, list[str]] = {}
        for c in self.cards.values():
            for p in c["parents"]:
                self._children.setdefault(p, []).append(c["id"])

    # -- queries -----------------------------------------------------------
    def status(self, cid: str) -> str:
        return self.cards[cid]["status"]

    def ready(self) -> list[str]:
        """Cards that can start NOW: todo, with every parent done (cards whose
        parent is not on the board are roots — ready by definition)."""
        out = []
        for cid, c in self.cards.items():
            if c["status"] != TODO:
                continue
            if all(self.cards[p]["status"] == DONE
                   for p in c["parents"] if p in self.cards):
                out.append(cid)
        return sorted(out)

    def subtree(self, root: str) -> list[str]:
        """root + every transitive dependent (the localized respec radius)."""
        seen, stack = [], [root]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.cards:
                continue
            seen.append(cur)
            stack.extend(self._children.get(cur, []))
        return seen

    # -- transitions ---------------------------------------------------------
    def mark(self, cid: str, status: str) -> None:
        self.cards[cid]["status"] = status

    def block_subtree(self, root: str, reason: str = "") -> list[str]:
        """Respec semantics: block ONLY the affected radius (root + dependents).
        Cards outside the subtree keep working — the blast is localized."""
        blocked = []
        for cid in self.subtree(root):
            c = self.cards[cid]
            c["_pre_block"] = c["status"]
            c["status"] = BLOCKED
            if reason:
                c["blocked_reason"] = reason
            blocked.append(cid)
        return blocked

    def unblock_subtree(self, root: str) -> list[str]:
        """After the re-derive: release the radius back to TODO (work that was
        already done is re-opened — the new spec supersedes it)."""
        released = []
        for cid in self.subtree(root):
            c = self.cards[cid]
            if c["status"] == BLOCKED:
                c["status"] = TODO
                c.pop("_pre_block", None)
                c.pop("blocked_reason", None)
                released.append(cid)
        return released

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.cards.values():
            out[c["status"]] = out.get(c["status"], 0) + 1
        return out


def dispatch(board: Board, worker: Callable[[dict], Any],
             max_workers: int = 4) -> dict:
    """Offline dispatcher (Ф8.2): run the board to completion in waves.

    Each wave takes every READY card and calls ``worker(card)`` concurrently —
    a fresh call per card models the gateway spawning a fresh worker per node;
    tree depth is unbounded. A worker exception fails its card; dependents of a
    failed card never become ready (they stay todo) and are reported as
    ``starved``. Returns {waves, order, completed, failed, starved}."""
    waves: list[list[str]] = []
    order: list[str] = []
    failed: list[str] = []
    while True:
        ready = board.ready()
        if not ready:
            break
        waves.append(ready)
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futs = {}
            for cid in ready:
                board.mark(cid, IN_PROGRESS)
                futs[cid] = pool.submit(worker, board.cards[cid])
            for cid, fut in futs.items():
                try:
                    fut.result()
                    board.mark(cid, DONE)
                    order.append(cid)
                except Exception:  # noqa: BLE001 — a worker died, card fails
                    board.mark(cid, FAILED)
                    failed.append(cid)
    starved = [cid for cid, c in board.cards.items() if c["status"] == TODO]
    return {"waves": waves, "order": order, "failed": failed,
            "starved": sorted(starved), "completed": len(order),
            "counts": board.counts()}
