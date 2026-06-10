# spec-flow

**Spec-Driven Development on a Hermes kanban board.** Recursive top-down
decomposition with feedback gates, frozen L2 contracts, drift detection and a
research/revision lane — driven as durable kanban tasks instead of fragile
in-process sub-agent swarms.

The design comes from the "Реализация ecom-agent в Hermes" conversation
(transcript in [`docs/conversation.md`](docs/conversation.md); analysis in
[`docs/analysis.md`](docs/analysis.md)).

## Why a board, not nested sub-agents

`delegate_task` caps nesting at depth 3 and lives inside one process. A spec
tree can be arbitrarily deep. spec-flow puts every node on **one kanban board**
as a task (`parents → child` edges); the dispatcher spawns a fresh worker per
node, so depth is unbounded and survives restarts. Each node sees only its own
spec + the parent handoff — context stays clean.

## Two halves

| Half | What | Where |
|---|---|---|
| Deterministic gates | hard checks that must not be eyeballed | the plugin (`spec_flow_tools.py`) |
| Reasoning | decompose, write/review specs, integrate | the skills (`skills/*/SKILL.md`) |

### Tools (kanban toolset)

| Tool | Role |
|---|---|
| `leaf_check` | leaf vs branch by hard levels (modules≤1, tasks≤5, interfaces≤2, LOC≤100, no open decisions, single-concern, testable) |
| `contract_check` | code↔contract drift; OpenAPI default, Zod/Protobuf, parallel mode; strict = unavailable validator also fails |
| `research_trigger_check` | fire the revision lane: `every_n_tasks` / `m_test_errors` / `on_level_return` / `cron`, with cooldown |
| `policy_gate` | deterministic constitution check — measurable target, unattended spend cap, outreach consent, legality review; verdict `pass`/`clarify`/`block`. Catches a vague/risky spec before it is decomposed |
| `run_report` | log-based report builder + **methodology audit** — consumes a run trace (JSONL) and returns footprints (what the plugin did, step by step) plus findings where the method was violated (silent drift, impl without a leaf gate, branch without integration, revision without re-derivation, green-on-red). A reviewer initiates it to see methodological errors |
| `specflow_init` | board + `constitution.md` + `specs/` |
| `specflow_start` | seed the L0 decomposition task |
| `specflow_status` | compact board summary |

### Skills

| Skill | Phase / role |
|---|---|
| `spec-requirements` | L0/L1 — constitution + EARS + clarify gate |
| `spec-flow-decompose` | one node, one level down; leaf gate; integrate nodes |
| `spec-contract` | L2 — freeze OpenAPI/Zod/Protobuf with `x-traces-to` |
| `spec-reviewer` | requirements-gate / spec-gate / impl-review (binary verdict) |
| `spec-implement` | leaf pipeline: design → bottom-up plan → TDD → review → contract_check → verify |
| `spec-integrate` | bottom-up subtree fold + parallel contract_check + e2e acceptance |
| `drift-gate` | on code↔contract drift: classify code-wrong vs contract-wrong |
| `respec-gate` | spec-first inversion at any level; version + re-derive only the affected subtree |
| `spec-research` | spike (before freeze) + continuous revision lane |

### Profiles

Six role profiles with cut-down toolsets so a role physically cannot do
another's job (the decomposer has no `terminal`/`code_execution`/`delegation`;
only `implementer` codes). See `profiles/`.

## Four echelons

`L1 requirements → L2 contract → L3 plan → L4 atomic`. A leaf is only created
after its governing **L2 contract node is `done`**.

## Install

```bash
# installs skills -> $HERMES_HOME/shared-skills, plugin -> $HERMES_HOME/plugins,
# and stages per-profile toolset configs
profiles/setup-profiles.sh
```

Then merge each `profiles/<role>/config.yaml.spec-flow` into the profile's
`config.yaml` (it carries only the `tools.cli` + `skills` block, not your
model/keys), and install contract validators if you use `contract_check` for
real (`redocly`/`specmatic`, `tsc`, `buf`).

## Run

```bash
# tool calls from an orchestrator (or via the kanban toolset):
specflow_init(project="myapp", dir="/abs/path/to/project")
specflow_start(goal="Build <project>", project="myapp", dir="/abs/path/to/project")
hermes gateway start    # the dispatcher; the board now drives itself
```

The board self-drives: the dispatcher promotes `todo → ready` as parents
complete and spawns a fresh decomposer per child until the L0 `Integrate &
verify` task is `done` = project complete.

## Spiral mode (research)

With the continuous revision lane on, the project is honestly spiral
(research → respec → implement → research), not waterfall. `respec-gate`'s
anti-thrash brakes (evidence threshold, re-open budget, manual confirmation on
large blast radius) are what make it converge.

## Running the engine (depth levels)

The plugin ships its own production runner (`spec_flow_runner.py`) — it drives a
project to completion **with or without Hermes**. The **Workspace is mandatory**;
a **depth** selects how far execution goes. Gate tools and per-role agents are
injectable but have autonomous defaults, so it runs standalone.

```python
from spec_flow_runner import run_project, load_run

run_project(
    load_run("project.yaml"),
    workspace="out/",          # MANDATORY — where artifacts land
    depth="spec",              # spec | scaffold | verify | execute
    tools=None,                # gate provider; default = bundled (no Hermes)
    agents=None,               # per-role workers; default = bundled autonomous
    contracts_dir="contracts", # where contract files live (optional)
)
```

| depth | what the run produces |
|---|---|
| `spec` | constitution, per-node specs/plans, frozen contracts, `MANIFEST.json` |
| `scaffold` | + code & test scaffolds per leaf, a commit journal |
| `verify` | + actually runs the test files (pytest), writes `TEST-RESULTS.md` |
| `execute` | + an injected **implementer agent** writes real code / modifies a real project (raises if none is provided) |

## Testing (autonomous — no Hermes needed)

The deterministic engine is fully testable offline: `tools.registry` and
`toolsets` are stubbed and the seed commands degrade gracefully without the
`hermes` binary.

```bash
python3 -m pytest tests/ -q        # 125 tests: unit + battle
python3 tests/report.py            # human-readable reports -> docs/*.md
python3 tests/run_cases.py         # real runs: one timestamped workspace per case
                                   #   -> tests/runs-out/<stamp>__<case>/ (artifacts,
                                   #      trace, log, report, policy-report, SUMMARY)
```

The **battle tests** drive real projects (`tests/projects/*.yaml`) through the
real `leaf_check`, build the realized kanban DAG and compare its branching
graph against each project's ground-truth design; `contract_check` runs a real
OpenAPI-vs-code validator (`tests/harness/openapi_diff.py`) over real fixtures;
the research lane is replayed over event timelines; and a **full end-to-end
run** (`tests/test_full_run.py`) drives one project to completion exercising
**all 9 skills and all 6 profiles**. Rendered reports:

- [`docs/test-report.md`](docs/test-report.md) — decomposition trees, drift
  table, research timeline;
- [`docs/business-scenarios-report.md`](docs/business-scenarios-report.md) —
  the gate catching deliberately-imprecise business goals;
- [`docs/full-run-report.md`](docs/full-run-report.md) — the real run, built by
  the plugin's `run_report` from the trace: footprints (markdown table) +
  methodology audit (green);
- [`docs/flawed-run-report.md`](docs/flawed-run-report.md) — the same builder on
  a deliberately-broken sample trace, so the audit's error findings are visible.

The audit checks the **run** (its log), not the plugin code.

See [`tests/README.md`](tests/README.md) for details.

## Version note

This repo registers tools via `tools.registry.register`. A stock Hermes plugin
guide exposes `ctx.register_tool` / `ctx.register_command` instead — if you port
this, re-wrap the handlers; the bodies stay the same. The seed helpers shell to
`hermes kanban` and are resilient to a missing `hermes` binary. Tune the
`leaf_check` thresholds and `CONTRACT_VALIDATORS` at the top of
`spec_flow_tools.py`.
