---
name: spec-flow-decompose
description: >
  Use when a spec-flow decomposition task runs. Processes exactly ONE node of
  a top-down Spec-Driven Development tree: read the parent's approved spec,
  write this level's spec, pass the gate, then either expand one level
  (branch) or emit implementation tasks (leaf). The recursion lives on the
  kanban board, not in context — never decompose the whole project at once.
---

# spec-flow-decompose

You decompose **one node, one level down**. The dispatcher spawns a fresh copy
of you for every child task, so depth is unbounded on the board (the
`delegate_task` depth cap of 3 does NOT apply — children are board tasks, not
nested sub-agents). Resist any temptation to plan the whole tree in your head.

## Four echelons (SDD)

- **L1 requirements** — what & why; constitution + EARS requirements.
- **L2 contract** — the interface frozen as a real artifact (OpenAPI by
  default; Zod / Protobuf). A node at this level pins `spec-contract`.
- **L3 plan** — bottom-up task plan (DB → logic → API → tests).
- **L4 atomic** — a single-concern, ~1-commit leaf.

A leaf may only be created once its governing **L2 contract node is `done`**.

## Procedure (one run = one level)

1. **Read the parent handoff** — the parent's `kanban_complete(summary,
   metadata)` is your approved input spec. Do not invent scope.
2. **Write this level's spec** into the `dir:` workspace under `specs/`. Every
   in-scope item carries a `Traces-to: [REQ-n]` back-reference. Any item
   without a trace to a parent requirement is **scope creep — delete it**.
3. **Gate BEFORE expanding (feedback loop).** If there is an open question or a
   conflict with `constitution.md`, call `kanban_block(reason=...)` and stop.
   A human or `spec-reviewer` comments and unblocks; your next run reads the
   thread and continues. This is the feedback loop — never expand past an open
   question.
4. **Spike-before-freeze.** If an unknown could change this level, create a
   `spec-research` (spike) child *before* freezing the spec; its recommendation
   feeds the spec. Research above the gate causes no late rework.
5. **Classify with `leaf_check`** (deterministic — never by eye). Pass the
   node's `modules / tasks / interfaces / estimated_loc / open_decisions /
   single_concern / testable_criteria`. Verdict `branch` → go to 6a; `leaf` →
   6b.
6a. **Branch.** `kanban_create(..., parents=[this])` one child per sub-node on
    the **same board**; each child pins `spec-flow-decompose`. If this is an
    L1→L2 step, one child is the L2 **contract node** pinning `spec-contract`.
    Also create the matching `Integrate & verify` node (pins `spec-integrate`)
    whose parents are this node's children — the bottom-up merge.
6b. **Leaf.** Emit the implementation task(s) pinning `spec-implement`, and a
    `spec-reviewer` review node downstream.
7. **Close the node** with `kanban_complete(summary, metadata)` — children read
   this as their approved spec. Recommended metadata: `changed_files`,
   `contract_path`, `dependencies`, `residual_risk`.

## Triggers up the tree

On returning up a level call `research_trigger_check(reason="on_level_return",
...)`; if it returns `trigger:true`, open a `spec-research` revision task.

## No duplicate work — reference, don't re-create

Before proposing a child, check the engine-provided context (`ancestors`,
`existing_nodes` — the registry of every node already created in the tree).
If the work is already covered by ANY existing node (not just your own
ancestor line), do NOT create a new child for it — declare the dependency
instead: `"depends_on": ["<existing-node-id>"]`. Re-creating existing work
(e.g. an L6 branch re-spawning the L1 "research analogs") burns real worker
runs twice and forks the source of truth. The engine's dedup gate prunes
such children deterministically — a pruned child means this rule was
violated.

Root-only shaping: upfront research / architecture-NFR children belong to
the ROOT decomposition only. Deeper nodes must not re-introduce them.

## Anti-temptation

One run expands exactly one level. Never "just finish the tree". Depth and
fan-out are paid for in real worker processes.
