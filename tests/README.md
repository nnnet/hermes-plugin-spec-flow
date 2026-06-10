# spec-flow tests

Two layers, all runnable **without Hermes** (`tools.registry` / `toolsets` are
stubbed; seed commands degrade gracefully when the `hermes` binary is absent).

```bash
python3 -m pytest tests/ -q     # run everything (60 tests)
python3 tests/report.py         # render a human report -> ../docs/test-report.md
```

## Layout

| Path | What |
|---|---|
| `test_spec_flow.py` | unit/smoke: registration, toolset wiring, each gate's branches, seed-command resilience |
| `test_decomposition_graph.py` | **battle**: walk real projects through the real `leaf_check`, build the kanban DAG, compare branching graph vs ground truth |
| `test_contract_drift.py` | **battle**: `contract_check` against a real OpenAPI validator over clean / type-mismatch / missing-endpoint / parallel-subtree fixtures |
| `test_research_timeline.py` | **battle**: replay event timelines through `research_trigger_check`; assert exact fire points + cooldown |
| `test_policy_scenarios.py` | **battle**: feed deliberately-imprecise (but legitimate) business goals through `policy_gate`+`leaf_check`; assert the plugin **blocks/clarifies** them (not a human) and that the resolved variant proceeds |
| `scenarios/*.yaml` | three business projects — each an imprecise L0 (vague niche / no metric / uncapped spend / outreach / legal exposure) + a resolved L0, with the template's must-catch list |
| `test_full_run.py` | **battle**: a full end-to-end project run exercising **all 9 skills & all 6 profiles**, asserting every loop (clarify / review critique / drift→respec / research revision) and project completion |
| `runs/privacy_analytics.yaml` | the end-to-end project (multi-level tree with a spike, a contract, a drift episode, a review failure and a research revision) |
| `harness/run_engine.py` | the dispatcher+worker run engine + execution-log / tree / coverage renderer |
| `projects/*.yaml` | project fixtures — a decomposition tree where each node carries the metrics `leaf_check` consumes plus its ground-truth verdict and the expected DAG summary |
| `contracts/*` | OpenAPI contracts + implementation manifests (clean / drifted) |
| `harness/simulator.py` | the dispatcher simulator + ASCII tree / report renderer |
| `harness/openapi_diff.py` | a small but real OpenAPI-vs-implementation drift validator |

## How a project fixture works

Each node lists the metrics the gate scores (`modules`, `tasks`, `interfaces`,
`estimated_loc`, `open_decisions`, `single_concern`, `testable_criteria`) plus
`expect: leaf|branch` (the human design's ground truth). The simulator calls
the **real** `leaf_check` at every node and:

- asserts the verdict equals `expect` (a divergence prints the actual tree),
- only expands `branch` nodes (mirrors the real dispatcher),
- builds the realized DAG (decompose / contract / impl / review / integrate
  tasks with parent edges) and reconciles its counts against `expect_dag`.

`ecommerce-checkout` deliberately includes a coupled `checkout_form`
(`single_concern: false`) to prove the gate **splits** a step the architect was
tempted to leave as one leaf; `data-pipeline` includes an `open_decisions: 1`
node to prove an unresolved decision forces expansion.

## Reading results

`pytest -q` gives pass/fail; on a battle-test failure the assertion message
includes the rendered ASCII tree and the exact diverging node. For a full
picture run `tests/report.py` — it writes two reports under `docs/`:

- `test-report.md` — one ASCII decomposition tree per project (✓/✗ per node),
  the DAG reconciliation, a contract-drift table and the research-lane timeline;
- `business-scenarios-report.md` — for each imprecise business goal, the gate
  pipeline (`policy_gate` inputs → verdict + reasons → `leaf_check`), the
  before/after (imprecise → resolved → proceeds) and a critical comparison
  against the template's must-catch list;
- `full-run-report.md` — the end-to-end run: the multi-level task tree (with
  versions/re-runs), a tick-by-tick execution log (which profile ran which
  skill, every clarify/critique/drift/respec), and a coverage summary proving
  all 9 skills and all 6 profiles ran.
