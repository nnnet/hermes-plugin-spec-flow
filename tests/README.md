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
picture run `tests/report.py` — it writes `docs/test-report.md` with one ASCII
decomposition tree per project (✓/✗ per node), the DAG reconciliation, a
contract-drift table and the research-lane timeline.
