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

## Testing (autonomous — no Hermes needed)

The deterministic engine is fully testable offline: `tools.registry` and
`toolsets` are stubbed and the seed commands degrade gracefully without the
`hermes` binary.

```bash
python3 -m pytest tests/ -q        # 60 tests: unit + battle
python3 tests/report.py            # human-readable report -> docs/test-report.md
```

The **battle tests** drive real projects (`tests/projects/*.yaml`) through the
real `leaf_check`, build the realized kanban DAG and compare its branching
graph against each project's ground-truth design; `contract_check` runs a real
OpenAPI-vs-code validator (`tests/harness/openapi_diff.py`) over real fixtures;
the research lane is replayed over event timelines. See
[`docs/test-report.md`](docs/test-report.md) for the rendered result (ASCII
decomposition trees with ✓/✗, a drift table and the lane timeline) and
[`tests/README.md`](tests/README.md) for details.

## Version note

This repo registers tools via `tools.registry.register`. A stock Hermes plugin
guide exposes `ctx.register_tool` / `ctx.register_command` instead — if you port
this, re-wrap the handlers; the bodies stay the same. The seed helpers shell to
`hermes kanban` and are resilient to a missing `hermes` binary. Tune the
`leaf_check` thresholds and `CONTRACT_VALIDATORS` at the top of
`spec_flow_tools.py`.
