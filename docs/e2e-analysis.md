# spec-flow — e2e calibration analysis (live agents, no Hermes)

Plan stage 1 (`2026-06-11T08-07__e2e-agents-pilot-hermes.md`). Evidence-driven:
every figure below comes from the per-call LLM log (`SPEC_FLOW_LLM_LOG`) and the
plugin-built reports inside each run workspace — not from estimation.

Each run: `tests/runs-out/<stamp>__<case>/` with `trace.jsonl`, `report.md`,
`oracle-report.md`, `llm-log.jsonl`, `llm-analysis.md`, `COST.md`, `SUMMARY.md`.

---

## Run 1 — p1 "earn for a living", depth=spec, live decomposer (haiku)

Config: `SPEC_FLOW_LLM_LEAF_DEPTH=2`, `SPEC_FLOW_LLM_MAX_CHILDREN=3` (see
"Finding A" for why the default config is intractable).

### What the plugin built (the goal → tree, fully self-derived)

The case `tree` was DROPPED; the plugin decomposed the bare goal itself:

```
L0  (goal: a niche micro-business that earns a living)
├── niche_research_validation
│   ├── market_competitor_research        (leaf)
│   ├── customer_validation               (leaf)
│   └── unit_economics_modeling           (leaf)
├── architecture_baseline
│   ├── architecture_design               (leaf)
│   ├── infra_baseline                    (leaf)
│   └── research_stack                    (leaf)
└── mvp_development
    ├── mvp_requirements_architecture      (leaf)
    ├── mvp_build_and_deploy               (leaf)
    └── research_niche_selection           (leaf)
```

Balanced 1→3→9, converged cleanly (no depth≥2 node kept branching). Run reached
`L0 integrate done = project COMPLETE`. The tree is sensible for the goal:
research → architecture → MVP, research-first (audit R9 holds).

### Cost & latency (from the call log)

| Metric | Value |
|---|---|
| Model calls | 13 (all decomposer; depth=spec has no implementer) |
| Errors | 0 |
| Latency sum | 353 s (~6 min) |
| Latency mean | 27 s / call |
| Latency max | 60 s (`architecture_baseline`) |

---

## Findings (calibration → inputs for Stage 4 "agent quality")

### Finding A — the default decomposer over-decomposes, blowing the call budget
First p1 run (default config) built fanout ~4 at every level and force-leafed
only at depth 3: 1+4+16+64 ≈ **85 nodes > the 80-call budget** → the run would
fail on non-convergence. Quantified directly from the log (4 calls in ~80 s,
fanout 4 at L0 and L1). Mitigation added: `SPEC_FLOW_LLM_LEAF_DEPTH` and
`SPEC_FLOW_LLM_MAX_CHILDREN` bound the tree; with leaf-depth 2 / fanout 3 a run
is 13 nodes and completes. **Stage-4 work:** make the decomposer prefer fewer,
larger leaves natively (and branch on a semantic atomicity judgment, with
`leaf_check` thresholds as the guardrail — see README "Principle").

### Finding B — per-call latency is high and variable (15–60 s on haiku)
Mean 27 s, max 60 s for a ~1.7 KB prompt → ~1 KB JSON reply. A run's wall time
is dominated by call count, so bounding the tree (Finding A) is also the main
lever on speed. **Stage-4 work:** smaller/sharper decomposer prompts; consider a
cheaper call for sizing vs a richer call only where a node is borderline.

### Finding C — reports crashed on a self-built tree (BUG, fixed)
In decomposer mode the case has no `tree`; `render_tree`/`render_mermaid` did
`proj["tree"]` and KeyError'd, crashing report generation AFTER the run finished
(so the first live runs produced no `report.md`/`SUMMARY.md`). Found by reading
the log + file mtimes, not by guessing. Fixed: the engine now persists the
realized tree into `project["tree"]` (the decomposer mutates nodes in place) and
the renderers degrade gracefully. Regression test `tests/test_report_no_tree.py`.

---

## Status of stage-1 runs

- [x] 1.3-pre — p1 @ spec, live decomposer — tree built, converged, analysed.
      Surfaced Findings A/B/C; bug C fixed.
- [ ] 1.3 — p1 @ execute (adds the live implementer) — pending.
- [ ] 1.4 — p2, p3 @ execute — pending.
- [ ] 1.5 — p4 @ product — pending.

Each remaining run spends real quota and ~5–15 min of wall time (call count ×
~27 s). Recommended next: p1 @ execute with the bounded-tree config to measure
the live implementer (code quality, green tests) on a small, converged tree.
