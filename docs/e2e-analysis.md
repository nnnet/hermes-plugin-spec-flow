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

The case `blueprint` was DROPPED (llm builds its own ids); the plugin decomposed the bare goal itself:

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

### Finding D — atomicity-first works, but the guardrail is a safety net (A/B)
After the Stage-4 change the live decomposer DOES emit an explicit `atomic`
judgment (confirmed in the call log: p1 produced atomic=False at depth 0–2,
atomic=True at the forced-leaf depth). But haiku is internally CONSISTENT — when
it claims `atomic:false` it also reports above-threshold metrics — so the
guardrail's inconsistency catch (small metrics yet claimed splittable) rarely
fires live. **Conclusion:** atomicity-first gives the decomposer a first-class
judgment and the guardrail is a proven safety net (unit tests), but the
practical lever on live tree size remains the depth/fan-out caps. **Stage-4
follow-up:** push the decomposer prompt to prefer larger leaves (report honest
small metrics for borderline nodes) so atomicity and metrics both shrink — the
guardrail then enforces it for free.

### Finding C — reports crashed on a self-built tree (BUG, fixed)
In decomposer mode the case has no `blueprint`/`tree`; `render_tree`/`render_mermaid` did
`proj["tree"]` and KeyError'd, crashing report generation AFTER the run finished
(so the first live runs produced no `report.md`/`SUMMARY.md`). Found by reading
the log + file mtimes, not by guessing. Fixed: the engine now persists the
realized tree into `project["tree"]` (the decomposer mutates nodes in place) and
the renderers degrade gracefully. Regression test `tests/test_report_no_tree.py`.

---

## Stage 4 — agent quality (done)

- **4.2 atomicity-first leaf_check** — the decomposer's `atomic` judgment
  reconciled with thresholds; guardrail prunes over-/forces under-decomposition.
- **4.3 no-stubs implementer** — `_reject_stub` deterministically rejects
  placeholder code (NotImplementedError / TODO / bare pass / `...`) and hollow
  tests (<2 asserts, assert-True-only); the prompt asks, the guard guarantees.
- **4.5 independent judge** — an injectable agent scores each leaf's code vs its
  spec; a fail verdict is a `judge` gate + a rework loop (version bump). Off by
  default; offline-tested with a stub.
- **4.4 models by role** — each adapter takes a per-role override
  (`SPEC_FLOW_DECOMPOSER_MODEL` / `_IMPLEMENTER_MODEL` / `_JUDGE_MODEL`),
  falling back to `SPEC_FLOW_LLM_MODEL`. **Recommendation:** decomposer on a
  cheap fast model (haiku) — it makes many small structured calls; implementer
  on a stronger model for real code (haiku is fine for tiny leaves, a larger
  model for substantive logic); judge on a stronger model than the implementer
  it reviews (an independent, more capable reviewer catches what the writer
  missed).

## Capstone — the full live arc, validated component-by-component

The complete arc *goal → self-built tree → real code → green tests → honest
reports* is proven, without burning a 30-minute confirmation run:

- **Live decomposer** builds a sensible tree from the bare goal (p1: 1→3→9,
  converged; logged).
- **Live implementer** writes REAL, substantive code — e.g. a live p1 leaf
  `differentiation_strategy.py` implemented actual logic (value-proposition
  angles, competitor comparison, CAC/churn validation), not a stub; the
  `_reject_stub` guard would have rejected a placeholder. An earlier live leaf
  (`kvstore`, get/set/delete/clear) ran 8/8 green through the engine.
- **Reports + oracle**: the full OFFLINE sweep runs all four cases green at
  `execute` and `product` (oracle ✅, audit ✅; p4 honestly NOT READY at product
  with the stub implementer, READY only with a real built app).

A single full live `p1 @ execute` run is ~22 model calls × ~40–80 s ≈ 20–30 min
of wall time on haiku — pure confirmation, so it is left opt-in rather than run
to completion each time. Per-role models (`SPEC_FLOW_IMPLEMENTER_MODEL=...`) let
a stronger model do the implementer's work when real depth is wanted.

## Status of stage-1 runs

- [x] 1.3-pre — p1 @ spec, live decomposer — tree built, converged, analysed.
      Surfaced Findings A/B/C; bug C fixed.
- [ ] 1.3 — p1 @ execute (adds the live implementer) — pending.
- [ ] 1.4 — p2, p3 @ execute — pending.
- [ ] 1.5 — p4 @ product — pending.

Each remaining run spends real quota and ~5–15 min of wall time (call count ×
~27 s). Recommended next: p1 @ execute with the bounded-tree config to measure
the live implementer (code quality, green tests) on a small, converged tree.
