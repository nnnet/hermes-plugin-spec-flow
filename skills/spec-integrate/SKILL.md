---
name: spec-integrate
description: >
  Use on a spec-flow "Integrate & verify" node (parents = the node's children).
  Bottom-up fold of a subtree: merge children's worktrees in dependency order
  (DB → logic → API → UI), run a parallel contract_check across ALL subtree
  contracts, then end-to-end acceptance criteria (integration behaviour, not
  leaf unit tests). The L0 integrate node done = project complete.
---

# spec-integrate

You fold a finished subtree into a verified whole. You run **only** when every
child of your node is `done`.

## Steps

1. **Read child handoffs** — each child's `kanban_complete` summary + metadata
   (`changed_files`, `contract_path`, ...).
2. **Merge in dependency order** — integrate the children's git worktrees
   DB → logic → API → UI. On a merge conflict: block and route back.
3. **Parallel contract check.** Collect the union of every child's
   `contract_artifacts` and `contract_types`, then call once:
   `contract_check(contract_artifacts=[...all...], types=[...all...])`. The tool
   validates the whole set in a thread pool — this catches drift a leaf passed
   in isolation but that breaks after merging with a sibling's contract → route
   to `drift-gate`.
4. **End-to-end acceptance.** Run the node's cross-cutting acceptance criteria
   (integration behaviour), with `verification-before-completion` discipline.
   Never close "green on red": an open criterion / drift / conflict = block +
   route back, not done.

Completing the **L0** integrate node means the project is fully implemented.
