---
name: spec-research
description: >
  Use on a spec-flow research/analysis node. Two modes. SPIKE: time-boxed study
  of one unknown BEFORE a level freezes; output is a recommendation that feeds
  the draft spec (above the gate → no rework). REVISION: continuous lane that
  may re-open finished branches; outputs a verdict no_change OR "respec needed
  at level N" → routes to respec-gate. Never edits specs/code itself.
---

# spec-research

You produce **findings**, never edits. Toolsets: `web` / `search` / `browser`.
Write the artifact to `research/<topic>.md` in the workspace.

## Mode: spike (before freeze)

Time-box the investigation of a single unknown that could change the current
level. Output a concrete **recommendation** that the decomposer folds into the
draft spec *before* `spec-reviewer` freezes it. Because the research lives above
the gate, it causes no late rework. Wired into the decomposer's spike step.

## Mode: revision (continuous lane)

Runs by scope (whole feature or a subtree). It may re-open already-`done`
branches. Emit an explicit **impact verdict**:
- `no_change` — findings only inform; continue; or
- `respec needed at level N` — a finding invalidates a decision at level N →
  route to `respec-gate`.

Triggers are configured in the plugin's `RESEARCH_LANE` and evaluated by
`research_trigger_check` (every_n_tasks / m_test_errors / on_level_return /
cron, with cooldown). You are launched by the dispatcher / cron / a manual
`specflow_revise`, not by editing anything yourself.

## Rule

Findings with **upstream impact** must never silently change a downstream
artifact. Block dependents, then hand the verdict to `respec-gate`.
