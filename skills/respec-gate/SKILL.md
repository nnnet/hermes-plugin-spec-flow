---
name: respec-gate
description: >
  Use when a finding or escalation invalidates a spec at ANY level (the general
  spec-first inversion, beyond contract-only drift-gate). Change the cause (the
  spec at node N), not the effect. Version it (supersedes/superseded_by, archive
  old, never delete), re-run spec-gate, then re-derive ONLY the affected subtree
  by the DAG. Anti-thrash brakes included.
---

# respec-gate

The generalisation of `drift-gate` to any level: when information flows **up**
(a research finding, a discovered impossibility), fix the **cause** — the spec
at the invalidated node N — and re-derive its consequences. Never patch the
effect silently.

## Procedure

1. **Block dependents.** `kanban_block` the affected subtree so nothing
   continues against the stale decision.
2. **Edit the cause.** Open a `respec` task on node N (the level whose decision
   is refuted). Change `spec.md` / the L2 contract there — the spec changes
   first.
3. **Version, don't delete.** New version links `supersedes` ← → old marked
   `superseded_by`; archive the old node (provenance lives in durable kanban
   `runs` + git history). The audit trail stays whole: "believed X → research R
   changed it to Y → here's why".
4. **Re-gate.** Run `spec-reviewer` (spec-gate) on the new spec — traceability
   to L1 / constitution must still hold; `Traces-to` edges re-point.
5. **Re-derive the subtree only.** By the DAG, invalidate strictly below N:
   affected tasks `block → unblock → re-run` against the new spec; new leaves
   may appear, some old ones archive; integrate nodes rebuild only the touched
   subtree. **Everything not depending on the changed decision stays as-is** —
   incremental rebuild, not project restart.

## Anti-thrash

A continuous revision lane can churn forever. Brakes (configurable):
- an **evidence threshold** before a respec is accepted;
- a **re-open budget** per branch;
- **manual confirmation** when the blast radius is large.

With the continuous lane on, the project is honestly **spiral** (research →
respec → implement → research), not strict waterfall — these brakes are what
make it converge.
