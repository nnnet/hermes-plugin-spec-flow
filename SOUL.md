# spec-flow — SOUL.md

> Spec-Driven Development as a self-driving kanban DAG.

## What it does

`spec-flow` makes a Hermes kanban board run a full SDD lifecycle on its own:
recursive **top-down decomposition** with **feedback gates**, a frozen **L2
contract** per interface, automatic **drift detection** code↔contract, and an
optional **research/revision lane** that can re-open finished branches —
spirally — without losing provenance.

The recursion lives on the board, not in an agent's context: every tree node is
a kanban task, the dispatcher spawns a fresh worker per node, and each node sees
only its own spec plus the parent's approved handoff. That sidesteps the depth-3
cap of in-process `delegate_task` and survives restarts.

## The deterministic core (this plugin)

Six tools under the `kanban` toolset. Three are gates that must never be left to
judgement:

- `leaf_check` — a node is a **leaf** only under every hard threshold
  (modules≤1, tasks≤5, interfaces≤2, est. LOC≤100, no open decisions,
  single-concern, testable criteria); otherwise **branch** one level.
- `contract_check` — validates code against the frozen contract (OpenAPI by
  default; Zod/Protobuf; parallel mode). Any drift fails; in strict mode an
  unavailable validator fails too — never a silent pass.
- `research_trigger_check` — decides if the revision lane fires now
  (`every_n_tasks` / `m_test_errors` / `on_level_return` / `cron`, cooldown).

Plus three seed/inspect helpers: `specflow_init`, `specflow_start`,
`specflow_status`, which shell to `hermes kanban` and degrade gracefully when
the binary is absent.

## When it fires

The intelligence is in the bundled skills loaded by six cut-down role profiles
(`spec-decomposer`, `spec-contract`, `spec-reviewer`, `implementer`, `verifier`,
`researcher`). A role physically cannot do another's job: only `implementer` has
`terminal`/`code_execution`/`delegation`; the decomposer is orchestration-only.

## Invariants

- **Spec-first.** Code never silently diverges from a spec. On drift, fix the
  cause (the spec/contract) first via `drift-gate` / `respec-gate`, re-gate,
  then re-derive only the affected subtree by the DAG.
- **Traceability.** Every in-scope item carries `Traces-to: [REQ-n]`; an orphan
  is scope creep and is removed.
- **Provenance.** Superseded specs are archived (`superseded_by`), never
  deleted — durable kanban `runs` + git keep the trail whole.

## Dependencies

Stdlib only for the plugin (`json`, `os`, `subprocess`, `concurrent.futures`,
`shutil`). Real `contract_check` needs external validators
(`redocly`/`specmatic`, `tsc`, `buf`). The skills assume a Hermes kanban
dispatcher (gateway) and the bundled core skills (`writing-plans`,
`test-driven-development`, `subagent-driven-development`,
`requesting-code-review`, `verification-before-completion`).
