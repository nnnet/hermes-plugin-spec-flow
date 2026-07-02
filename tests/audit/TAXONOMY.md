# Preventive audit — taxonomy of stages & factors

The audit is ACTIVE and PREVENTIVE: it exercises the engine across every
analysis dimension BEFORE a live run, so a design hole reds in seconds — never
waits for an hour-long run to surface it. Every stage below is a standing
obligation; when a new failure mode is conceived, add its check here, do not
wait for a run to teach it to us.

## STAGE 1 — Static code hygiene (`test_static_hygiene.py`)
- S1.1 the package compiles (no syntax error) — every .py under the plugin.
- S1.2 engine-synthesized code carries no stub marker (NotImplementedError/TODO/
  FIXME/`pass  # stub`).
- S1.3 no absolute path literals in engine/runner code (project rule; paths from
  REPO_ROOT / workspace only).

## STAGE 2 — Router / assembly completeness (`test_honesty_invariants.py`)
- S2.1 synthesized router branches on 400 / 404 / 405 (not one catch-all).
- S2.2 every declared (method,path) resolves to a handler (no unrouted).
- S2.3 the router has a terminal fallback (unknown path never crashes).

## STAGE 3 — Honest readiness sign (`test_honesty_invariants.py`)
- S3.1 READY = logical AND: root integrate RED ⇒ product NOT READY.
- S3.2 a base-contract smoke cannot lift a project with a red/abandoned leaf.
- S3.3 every DECLARED address (incl. late) must be served for READY.

## STAGE 4 — Requirement-class routing matrix (`test_requirement_class_paths.py`)
- S4.1 every class has a non-rejecting path: new-route, amend-in-place,
  delete-behaviour, duplicate(reject), non-web capability, cross-cutting.
- S4.2 AMEND↔FORK EXCLUSIVITY: a node with `code_target` (amend) is exempt from
  ALL fork gates — owned-routes, exposed-symbols, scope-lint, handler-gate,
  card-gate — it owns nothing of its own (v145).
- S4.3 an amend node does not re-enter as a fresh decomposable leaf that loops
  through decompose→spec→implement repeatedly (v146 non-convergence).

## STAGE 5 — State-machine & doctor completeness (`test_state_machine_completeness.py`)
- S5.1 every doctor CAUSE maps to at least one REMEDY (no cause without treatment).
- S5.2 every remedy string the doctor can emit is dispatchable (reachable branch).
- S5.3 every lifecycle state has an outgoing transition (no dead-end / limbo).

## STAGE 6 — Termination / convergence caps (`test_termination_invariants.py`)
- S6.1 per-node review rework is bounded, finite, > 0 (max_rework).
- S6.2 decompose calls are globally bounded (MAX_DECOMPOSE_CALLS finite).
- S6.3 integrate rework is bounded and finite.
- S6.4 a leaf has a wall-clock / step deadline.
- S6.5 amend is a bounded targeted edit, not a full unbounded leaf lifecycle.
- S6.6 a standing (late) requirement is materialised at most once per identity —
  re-poll is de-duplicated, never re-processed unboundedly (v146).
- S6.7 a tiny global run-call budget HALTS a runaway run with an explicit FAIL
  milestone and an honest NOT READY (dynamic; the v146 catcher).
- S6.8 the detached launcher records the python exit code (and decodes
  rc>=128 as "killed by signal S") — a death that leaves no attributable
  record is an observability hole the ratchet cannot learn from (v148:
  external SIGKILL, zero evidence).

## STAGE 7 — Gate liveness / adversarial (`test_gate_liveness.py`)
- S7.1 each named gate reds on a crafted negative input (no dead gate that never
  fires).
- S7.2 each gate is reachable from the run pipeline (called, not orphaned code).

## STAGE 8 — Dynamic simulation / dry-run (`test_simulation_convergence.py`)
Exercise the WHOLE control flow offline with deterministic fake agents — the only
place loop/convergence bugs live.
- S8.1 a full product-depth run on a web project reaches a terminal verdict in a
  BOUNDED number of agent calls (well-behaved agents).
- S8.2 with an ADVERSARIAL agent (returns junk every time) the run still
  TERMINATES within a hard call ceiling and reports NOT READY honestly — never
  loops forever (the v146 catcher).
- S8.3 a late AMEND injection in simulation edits the owner and terminates
  bounded — never re-decomposes in a loop.

## STAGE 9 — Capability liveness (`test_parallel_liveness.py`)
A DECLARED engine capability must be PROVEN reachable in its representative
scenario by a dynamic offline test — "the code exists" is not evidence. This
is the stage that catches ARCHITECTURAL/DESIGN bugs: a capability whose
trigger conditions can never be met in the runs that need it (v148: parallel
development was configured but unreachable — the fork decision was one-shot
against the initial tree shape, late-injection windows had no parallel path,
and a static depth gate starved online growth).
- S9.1 PARALLELISM/base: independent siblings develop with REAL thread overlap
  (lock-guarded peak counter in the worker) — the proof is structural, valid
  even when the live provider serialises LLM calls into one lane.
- S9.2 PARALLELISM/injections: requirements injected AFTER the initial shape
  accumulate into a parallel wave (the fork policy is re-evaluated at every
  window, including re-poll).
- S9.3 PARALLELISM/recomposition: a fan-out materialising DEEPER than the
  initial shape (a branch recomposed online) still forks — no static depth
  starvation.
- S9.4 (open) PARALLELISM/ordering: cross-LEVEL declared dependencies must be
  honoured by wave partitioning — Kahn waves currently serialise only
  intra-sibling deps; a research node on one level can lose the race to an
  impl leaf under another branch (flaky p4 research_before_impl; task #146).
- S9.5 (open) same liveness proof owed to: --resume, doctor remedies,
  worktree isolation, memory tiers — one representative dynamic test each.

## Convention
- A check is HONEST: it reds on a real hole, is never softened to pass.
- Fixes are real engine capabilities, never per-case crutches.
- run-detached.sh will not launch while `pytest tests/audit` is red.
