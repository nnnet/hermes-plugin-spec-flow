# Architecture: roles → executors → specialists → providers (local / Hermes / A2A / Mission-Control)

> Generalises the implementer "team/orchestra" so a role can be a single local
> LLM, a locally-orchestrated team, a third-party agent, or a fully-external
> team the plugin knows nothing about — and any mix. Back-compatible: no `team`
> ⇒ today's single local worker.

## 1. Problem

Each pipeline role (decomposer / implementer / reviewer / verifier) is today a
single local LLM call. We want, for ANY role:

1. **No team** → an "orchestra of one" (the current single specialist).
2. A role filled by a **third-party agent** (a Hermes agent, or an external
   service reachable over **A2A**), not a local LLM.
3. An **orchestra whose specialists are each, independently**, local or
   third-party.
4. A **team defined in an external service** — the plugin hands off the whole
   role and is BLIND to the team's composition and workflow.
5. The **reverse** — the team's specialists AND workflow fully described in YAML.
6. **Mixed** — inline orchestration locally, but some specialists are remote;
   or a local lead that delegates one step to an external crew.

## 2. Core abstractions

```
role  ──resolves to──>  Executor
                          ├─ Specialist        (a single unit of work)
                          └─ Team               (specialists + workflow)
                                ├─ inline        (plugin orchestrates)
                                └─ external       (delegated whole; opaque)

Specialist / Team / external-Team are all backed by a Provider:
  Provider ∈ { local, hermes, a2a, mission-control, external-orchestrator }
```

- **Provider** — WHERE the work runs and HOW we talk to it (adapter).
- **Specialist** — one worker in a role (`role` = its function: architect/coder/
  tester/fixer/…; backed by a provider + its config). Terminology is strict:
  **specialist** = the participant, **role** = its function. A specialist is NOT
  a role.
- **Executor** — what a pipeline role resolves to: a single Specialist OR a Team.
- **Team** — specialists + a workflow. `inline` (plugin runs the workflow over
  the specialists) or `external` (a remote orchestrator owns both).
- **Workflow** — declarative (sequential shorthand, or a graph of edges for
  loops/branches). Modelled on CrewAI `process` + LangGraph edges; runs on the
  existing FSM (`workflow-engine`). Never hand-rolled per case.

## 3. The uniform contract (the seam that makes all cases identical)

Every Executor — local LLM, local orchestra, Hermes agent, A2A service, MC
crew — implements ONE interface:

```
execute(RoleTask) -> RoleResult
```

`RoleTask` (engine → executor):
- `role`: decomposer | implementer | reviewer | verifier
- `node`: { id, title, spec_path, spec_body }
- `context`: { goal, constitution, repo_map, ancestors, handoff }   # handoff = prior specialist's output
- `workspace`: a path (local) OR a bundle of input artifacts (remote)
- `constraints`: { stdlib_only, frozen_contract, deadline_s }

`RoleResult` (executor → engine):
- `kind`: files | spec | verdict
- `artifacts`: { "src/x.py": "...", "tests/test_x.py": "..." }   # implementer
- `spec`: { spec_markdown, children, metrics }                    # decomposer
- `verdict`: { pass: bool, reasons: [...] }                       # reviewer/verifier
- `meta`: { provider, agent, model, specialists_run: [...], latencies, cost }

Because the contract is uniform, the engine's pipeline does not branch on
provider. A Team just composes child RoleTasks; an external executor serialises
the RoleTask over the wire and deserialises the RoleResult. For remote
providers the engine ships only the needed inputs as artifacts and writes the
returned artifacts back into the workspace (the workspace stays the plugin's
source of truth).

## 4. Providers (adapters) — one module each, stdlib transport

| provider | how it runs the RoleTask | discovery / auth |
|---|---|---|
| `local` | built-in `role_worker` LLM call with `model` + `params` (temperature, max_tokens, …) | model chain / free-pool rules (unchanged) |
| `hermes` | POST the RoleTask as a message to a Hermes **agent/profile** via the Hermes gateway; collect reply + written files | gateway URL + agent id |
| `a2a` | **A2A protocol** client: fetch the Agent Card (`/.well-known/agent.json`), `tasks/send` the RoleTask message, stream task state (submitted→working→completed) over SSE, collect `artifacts` | endpoint URL + (optional) card; bearer/oauth |
| `mission-control` | create an MC **task** assigned to an MC agent (or agent-template), poll until done, collect the task output as artifacts | MC API base + agent ref |
| `external-orchestrator` | same as `a2a`/`hermes` but the remote owns a whole TEAM; we send the role-task, get the role-result; we never see specialists | as above |

A2A is the open standard for "external agent / stood-up service" (Agent Cards +
task lifecycle + artifacts over JSON-RPC/HTTP+SSE) — we adopt it rather than
invent a bespoke RPC. Hermes and MC are first-party adapters. All adapters
return the SAME `RoleResult`, so observability and the workspace write-back are
provider-independent.

## 5. YAML structure

A role's value is resolved by shape (back-compat first):

```yaml
workers:

  # CASE 1 — single LOCAL specialist (orchestra of one). Bare `models` = today.
  verifier:
    models: ["claude/haiku", "openrouter/qwen/qwen3-coder:free"]

  # CASE 2 — role is ONE third-party agent (no local LLM)
  reviewer:
    specialist:
      provider: hermes
      agent: aegis-reviewer            # a Hermes agent
      params: {temperature: 0.0}

  decomposer:
    specialist:
      provider: a2a
      endpoint: https://planner.example/a2a
      # agent_card optional; discovered from /.well-known/agent.json otherwise

  # CASE 3/5/6 — role is a TEAM described INLINE; specialists mix providers
  implementer:
    team:
      process: sequential              # shorthand; or use `workflow:` below
      specialists:
        - {role: architect, provider: local,           model: "openrouter/qwen/qwen3-next-80b-a3b-instruct:free", params: {temperature: 0.2}}
        - {role: coder,     provider: local,           model: "openrouter/qwen/qwen3-coder:free",                  params: {temperature: 0.4, max_tokens: 4000}}
        - {role: tester,    provider: hermes,          agent: qa-bot}
        - {role: fixer,     provider: mission-control, agent_template: Linter}
      # OPTIONAL explicit non-linear workflow (LangGraph-style edges); when
      # absent, `process` defines the flow (sequential = the list order).
      workflow:
        start: architect
        edges:
          - {from: architect, to: coder}
          - {from: coder,     to: tester}
          - {from: tester,    to: fixer,  when: tests_failed}
          - {from: fixer,     to: tester, when: retry}      # evaluator-optimizer loop
          - {from: tester,    to: DONE,   when: tests_passed}

  # CASE 4 — team is FULLY EXTERNAL; plugin is blind to composition + workflow
  decomposer:
    team:
      external: true
      provider: a2a
      endpoint: https://planner-crew.example/a2a
      # engine sends the RoleTask, receives the RoleResult; the remote owns the
      # specialists and their workflow. Optional: it may STREAM sub-steps back
      # (A2A artifacts/status) which we surface on the dashboard if present.
```

Resolution rules (deterministic, by shape):
- `models:` (or nothing) → CASE 1, local single specialist. **Default / back-compat.**
- `specialist:` → a single specialist on the named provider (CASE 2).
- `team: { specialists: [...] }` → inline orchestra (CASES 3/5/6); each
  specialist resolves its own provider; flow from `workflow:` else `process:`.
- `team: { external: true, provider, endpoint }` → opaque external team (CASE 4).

Per-specialist `params` (temperature, max_tokens, top_p, stop, …) are passed
through to that specialist's provider; `local` maps them onto the model call,
remote providers forward them in the RoleTask.

## 6. Worked mixed example (own + Hermes + Mission-Control + A2A)

A dev crew for the implementer role:
- **architect** — our OWN local definition (qwen3-next, low temperature);
- **coder** — a **Hermes** agent `senior-dev` (reuses Hermes skills/tools);
- **tester** — a **Mission-Control** agent from template `Aegis`;
- **fixer** — an **external A2A** repair service.
- Workflow: architect→coder→tester; tester loops to fixer on failure, fixer
  back to tester, exit on green (evaluator-optimizer).

```yaml
implementer:
  team:
    specialists:
      - {role: architect, provider: local,           model: "openrouter/qwen/qwen3-next-80b-a3b-instruct:free", params: {temperature: 0.2}}
      - {role: coder,     provider: hermes,          agent: senior-dev}
      - {role: tester,    provider: mission-control, agent_template: Aegis}
      - {role: fixer,     provider: a2a,             endpoint: https://fix-bot.example/a2a}
    workflow:
      start: architect
      edges:
        - {from: architect, to: coder}
        - {from: coder,     to: tester}
        - {from: tester,    to: fixer,  when: tests_failed}
        - {from: fixer,     to: tester, when: retry}
        - {from: tester,    to: DONE,   when: tests_passed}
```

The engine runs architect (local) → coder (Hermes) → tester (MC) and loops
tester↔fixer (A2A) — each via its adapter, all returning the same RoleResult,
all writing into the one workspace.

## 7. Observability (feeds the dashboard work already queued)

- Each specialist invocation logs through `llm_log.timed_ask` with open-schema
  meta `{ role, specialist, provider, model, step, mode, node, wall, latency_s }`
  (the ⏱ logging already added real call/response timestamps).
- The dashboard **team card** reads the resolved team (specialists → role →
  provider → model → params).
- The **"Поток выполнения"** tab renders the specialist call sequence per node
  from those records (architect→coder→tester→fixer with real times, loops shown).
- External teams that stream A2A sub-steps surface those; opaque external teams
  show a single "external: <provider>" step (honest: we don't know inside).

## 8. Implementation phases (each: flag-gated, default off, tests, p6 re-run)

1. **Contract + local** — define `RoleTask`/`RoleResult`, refactor the current
   single worker + the existing `_orchestra_run` to the `execute()` seam with a
   `local` provider and per-specialist `model`+`params`. No behaviour change at
   default. (engine + role_worker)
2. **Declarative workflow** — `process: sequential` + `workflow.edges` on the
   existing FSM; sequential == today. (engine)
3. **Provider adapters** — `hermes`, then `mission-control`, then `a2a`
   (Agent Card + tasks/send + artifacts). One module per adapter; stdlib HTTP.
4. **External team** — `team.external` delegates the whole role over a provider.
5. **Dashboard** — team card + specialist call sequence in the flow tab.

## 9. Hard rules carried in

- Free-pool only for the local provider on p6 tests; remote providers are
  config, not paid local calls.
- No `from tests.harness import` inside plugin runtime (layering).
- Default (no `team`, no `specialist`) is byte-for-byte today.
- Adapters are capability-named (`provider: hermes`), transport/auth lives
  inside the adapter, not in the role schema.
