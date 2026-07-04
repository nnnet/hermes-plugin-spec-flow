# AGENTS.md — spec-flow control center

This repo IS the control center of the spec-flow mission: an engine that
turns a human description into detailed specs, decomposes them into a graph
of small nodes and deterministically assembles a WORKING service without
guessing. Honest READY (conjunction of gates); honest red beats false green.

## Discovery protocol (read this way, in this order)

1. `tree docs tests/audit -L 2` FIRST; read only what the task needs.
2. `tests/audit/TAXONOMY.md` — the registry of every audit rule (S1-S12.x),
   each born from a dissected run failure. New failure class => new rule
   here, RED before the fix.
3. `tests/audit/CHARTER.md` — principles P1-P8 (single source of truth,
   liveness, attributable failure, prose-is-not-data, sterile oracle,
   honest conjunction, finite budgets).
4. `.claude/HANDOFF.md` — current state and the exact next step.
5. `.claude/plans/` — active plans; newest date wins. Current course:
   `2026-07-04T00-45__spec-ir-compiler-rearchitecture.md` (spec-IR,
   executable scenarios, engine-as-compiler).

## Layout

- `spec_flow_runner.py` — the engine (graph, gates, assembly, doctor).
- `spec_flow_diagnosers.py` / `spec_flow_remedies.py` / `spec_flow_doctor.py`
  — cause diagnosis, remedies, treatment loop.
- `tests/harness/` — LLM backend (single door: `llm_backend.ask`), workers,
  decomposer, verifier. NO monkeypatching on the LLM path — real seams only.
- `tests/audit/` — the three-layer audit: deterministic gates + CLI
  checkers (`consistency.py`, `journal_invariants.py`), LLM investigator
  (`investigator.py`), code revision (`revision.py`).
- `tests/scenarios/` — cases (p4/p6/p7...); `tests/run-detached.sh` — the
  ONLY sanctioned way to launch a live run (audit-gated, checkpointed).
- `tests/runs-out/` — records: every run dir carries trace.jsonl,
  llm-log.jsonl, checkpoints/, layer1-findings.md, investigation.md,
  layers-summary.md. Post-mortems live next to their evidence.

## Hard rules (engine-enforced, do not fight them)

- Ratchet: every fix ships with an audit rule that was RED before it.
- Every gate needs BOTH a red and a green known-answer case.
- Prose is not data: any value two artifacts must agree on exists as ONE
  engine-declared datum (contracts/, modules.json, media, request_fields).
- One write door: delivered code passes `_delivery_lint` (English/ASCII,
  no host paths, contract conformance) — dirty code never lands.
- No vanity tests: interface-level tests derive from contracts/scenarios,
  never from an LLM's imagination. Bad tests are bad requirements.
- Runs are detached (setsid), checkpointed, exit-code-attributed.
- Free-tier models only for runs (claude = subscription is allowed);
  provider degradation is infra noise — never mixed into engine dissection.

## Sibling projects

- `../..` (hermes-plugins-collection) — plugin host; spec-flow is a plugin.
- `infra/hermes` (repo root: nnnet/AiManager) — Hermes gateway stack that
  hosts the live TG-driven runs; Bifrost LLM proxy at 127.0.0.1:8080.
- Dashboard: `tests/lib/live_dashboard.py` on :8092 (auto-follows the
  latest run).

## Anchor comments

Greppable anchors in code: `AICODE-NOTE:` (constraint worth keeping),
`AICODE-TODO:` (known debt), `AICODE-QUESTION:` (open design question).
Search before adding yours: `grep -rn "AICODE-" --include=*.py`.
