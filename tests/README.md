# spec-flow tests

Two layers, all runnable **without Hermes** (`tools.registry` / `toolsets` are
stubbed; seed commands degrade gracefully when the `hermes` binary is absent).

```bash
python3 -m pytest tests/ -q     # run everything (60 tests)
python3 tests/lib/report.py         # render a human report -> ../docs/test-report.md
```

## Layout

| Path | What |
|---|---|
| `test_spec_flow.py` | unit/smoke: registration, toolset wiring, each gate's branches, seed-command resilience |
| `test_decomposition_graph.py` | **battle**: walk real projects through the real `leaf_check`, build the kanban DAG, compare branching graph vs ground truth |
| `test_contract_drift.py` | **battle**: `contract_check` against a real OpenAPI validator over clean / type-mismatch / missing-endpoint / parallel-subtree fixtures |
| `test_research_timeline.py` | **battle**: replay event timelines through `research_trigger_check`; assert exact fire points + cooldown |
| `test_policy_scenarios.py` | **battle**: feed deliberately-imprecise (but legitimate) business goals through `policy_gate`+`leaf_check`; assert the plugin **blocks/clarifies** them (not a human) and that the resolved variant proceeds |
| `scenarios/*.yaml` | the case pool: each carries an imprecise L0 + a resolved L0 (policy surface), a `blueprint` (EXECUTION INPUT for the deterministic decomposer — the plugin builds the tree itself from it, never read as a finished tree) and an `oracle` block (ANALYSIS reference: anchors/depth/episodes — never drives execution). **p4** is the complete exercise hitting every skill/profile/loop (both drift kinds, 2 contracts, 2 clarifies, 2 spikes, revision) |
| `run_cases.py` | **case runner**: the plugin ALWAYS builds its tree via a decomposer — `--decomposer blueprint` (default, deterministic, offline) or `--decomposer llm` (live). Drives every scenario into its own timestamped workspace `runs-out/<stamp>__<case>/`; at `--depth execute` injects an implementer |
| `harness/blueprint_decomposer.py` | deterministic decomposer fed by the case `blueprint`: serves one level per node through the real agent interface, so the engine builds + gates the tree itself — offline, no quota |
| `harness/llm_decomposer.py` | live decomposer agent (local `claude` CLI): the plugin builds the task tree ITSELF from the goal — `run_cases.py --decomposer llm` |
| `test_agent_decomposition.py` | **battle**: no predefined tree — a decomposer agent builds it from the goal; every built node is gated by the real `leaf_check`; no agent → loud failure; runaway recursion → capped |
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
picture run `tests/lib/report.py` — it writes two reports under `docs/`:

- `test-report.md` — one ASCII decomposition tree per project (✓/✗ per node),
  the DAG reconciliation, a contract-drift table and the research-lane timeline;
- `business-scenarios-report.md` — for each imprecise business goal, the gate
  pipeline (`policy_gate` inputs → verdict + reasons → `leaf_check`), the
  before/after (imprecise → resolved → proceeds) and a critical comparison
  against the template's must-catch list;
- `full-run-report.md` — the end-to-end run: the multi-level task tree (with
  versions/re-runs), a tick-by-tick execution log (which profile ran which
  skill, every clarify/critique/drift/respec), and a coverage summary proving
  all 9 skills and all 6 profiles ran;
- `full-run-trace.jsonl` — the **raw source data** the run report is built
  from: one JSON event per line (tick, phase, profile, skill, task, action,
  gate, verdict, level).

## Logging & verbosity (run engine)

The report is rendered from the in-memory `RunResult.events` stream, not from a
file. Each event carries a `level`: **1** = milestones (gate verdicts, loops),
**2** = routine steps, **3** = fine detail (TDD, constitution rules).

- **Render verbosity** — `render_report(res, level)` / `render_log(res, level)`
  show events with `level <= N`. Default via `SPEC_FLOW_RUN_VERBOSITY` (=2).
- **Disk log** is an optional, off-by-default handler (`run_engine.LogSink`)
  with its own level and format, independent of the render verbosity:
  ```python
  sink = LogSink(path="run.jsonl", level=L_DETAIL, fmt="jsonl", enabled=True)
  Engine(tools, sink=sink).run(load_run())          # writes run.jsonl
  Engine(tools, sink=lambda e: ...).run(load_run()) # or a custom callable
  ```
  Or enable via env without code: `SPEC_FLOW_RUN_LOG=run.jsonl`,
  `SPEC_FLOW_RUN_LOG_LEVEL=1`, `SPEC_FLOW_RUN_LOG_FORMAT=text|jsonl`.
  `dump_trace(res)` returns the full event stream as JSONL.
- **Material artifacts** — an optional `Workspace` (also off by default; enable
  with a path or `SPEC_FLOW_RUN_WORKSPACE`) writes the run's real deliverables a
  reviewer can open and evaluate: `specs/<node>.md` (plan per node), the frozen
  `contracts/<file>`, `src/` + `tests/` scaffolds per leaf, a `COMMITS.md`
  journal and `MANIFEST.json` (type/size/sha256). `tests/lib/report.py` writes them
  to `docs/run-workspace/`. Code/test files are honest scaffolds (header +
  NotImplementedError / failing assert), since no real LLM authored them; the
  specs, contract and manifest are real content.

## Report & methodology audit (built by the plugin, from logs)

The run report is **not** rendered by the harness — it is built by the plugin's
own log-based functions (`spec_flow_tools.build_run_report` /
`audit_methodology`, exposed as the `run_report` tool) that consume a trace
(JSONL) and emit footprints + a methodology audit. Run it on any trace:

```bash
python3 tests/lib/run_report.py                      # docs/full-run-trace.jsonl
python3 tests/lib/run_report.py tests/runs/flawed_run.jsonl   # demo: audit catches errors
python3 tests/lib/run_report.py <trace> --level 1 -o out.md
```

`tests/lib/report.py` writes two run reports (footprints as a markdown table +
audit): `docs/full-run-report.md` (the real/clean run → audit green) and
`docs/flawed-run-report.md` (the deliberately-broken sample → audit lists the
violations). The audit checks the **run**, not the plugin code.

In Hermes it is a single tool call: `run_report(trace_path=...)`. The audit
checks invariants R1–R8 (policy gate ran, no impl without a leaf gate, no silent
contract drift, every impl reviewed, every branch integrated, open decisions
clarified, revisions re-derive their subtree, no green-on-red) and lists each
violation with where/why/fix. `tests/runs/flawed_run.jsonl` is a seeded-error
trace proving the audit catches real methodological mistakes.

## Case runner — one workspace per run (`run_cases.py`)

Every scenario in `scenarios/*.yaml` is driven as a **real production run** of
the plugin, each into its **own timestamped workspace**:

```bash
python3 tests/lib/run_cases.py                          # all cases, depth=spec
python3 tests/lib/run_cases.py --depth scaffold         # deeper: + code/test scaffolds
python3 tests/lib/run_cases.py --case p4 --depth verify # one case, + real pytest run
```

Output per case — everything in one folder, nothing outside it:

```
tests/runs-out/<YYYY-MM-DDTHH-MM-SS>__<case>/
├── workspace/        materialised artifacts (constitution, specs/, contracts/,
│                     MANIFEST.json; deeper: src/, tests/, COMMITS.md,
│                     TEST-RESULTS.md with a per-test verdict list)
├── workflow.md       the goal/task tree (versions, ↻ re-runs, episode tags) +
│                     execution log + the loops table (where the run cycled)
├── trace.jsonl       raw event stream (full detail) — the report's source
├── log.txt           readable execution log
├── report.md         footprints + methodology audit (built by the plugin)
├── policy-report.md  policy_gate catching the imprecise goal (if the case has one)
└── SUMMARY.md        depth, verdicts, coverage, file map
```

`runs-out/` is gitignored and excluded from pytest collection (the scaffolds
inside are deliberately red).
