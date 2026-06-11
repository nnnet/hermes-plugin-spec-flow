"""Deterministic decomposer driven by a scenario ``blueprint`` (no quota).

Why: a scenario's structure must NEVER drive the plugin directly — the plugin
builds its task tree ITSELF by calling a decomposer, node by node, applying
leaf_check / the atomicity guardrail at each step. The reference structure in a
scenario is for ANALYSIS only (the oracle's anchors/depth), not execution.

This decomposer lets the engine build the tree deterministically and offline:
it serves ONE level at a time through the exact same agent interface the live
LLM decomposer uses. The engine calls it per node; it returns that node's
metrics + per-node methodology fields (spike/clarify/contract/drift/hitl/...)
and its children as ``{id, title}`` stubs — the children's own details arrive
only when the engine visits them and calls back. So every node passes through
the real decomposer→gate→visit path, exactly like a live run.
"""

from __future__ import annotations

from typing import Any, Callable

# node keys the engine understands (everything except the nested child subtrees)
_PASSTHROUGH = ("metrics", "title", "spike", "clarify", "contract", "drift",
                "hitl", "review_fails", "atomic")


def make(blueprint: dict) -> Callable[[dict], dict]:
    """Build a decomposer agent that serves ``blueprint`` level-by-level.

    The blueprint is a nested ``{id, title, metrics, children:[...]}`` reference
    (the former scenario ``tree``, now execution input — NOT analysis). Returns
    a ``decompose(ctx)`` callable for ``run_project(agents={"decomposer": ...})``.
    """
    index: dict[str, dict] = {}

    def _flatten(node: dict) -> None:
        payload = {k: node[k] for k in _PASSTHROUGH if k in node}
        kids = node.get("children", []) or []
        payload["children"] = [{"id": c["id"], "title": c.get("title", c["id"])}
                               for c in kids]
        index[node["id"]] = payload
        for c in kids:
            _flatten(c)

    _flatten(blueprint)

    def decompose(ctx: dict) -> dict:
        nid = ctx["node"]["id"]
        node = index.get(nid)
        if node is None:
            # not in the blueprint — treat as an atomic leaf (defensive)
            return {"metrics": {"modules": 1, "tasks": 1, "interfaces": 1,
                                "estimated_loc": 10, "open_decisions": 0,
                                "single_concern": True, "testable_criteria": True},
                    "atomic": True}
        out = {k: v for k, v in node.items() if k != "children"}
        out["children"] = [dict(c) for c in node["children"]]   # fresh stubs
        return out

    return decompose


def root_id(blueprint: dict) -> str:
    return blueprint.get("id", "L0")
