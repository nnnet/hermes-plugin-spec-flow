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

## STAGE 10 — Assembly-seam honesty (`test_assembly_seam_honesty.py`)
Every value TWO independent artifacts must agree on (status codes, symbol
names, module owners) must exist as ONE engine-declared datum both sides
read — and the suite that judges the product must see ONLY the product.
v149: the card pinned no success status (coder 201 vs tester 200 — guesses
colliding only at assembly), and a host editable-install .pth satisfied a
tester-invented `db` module with an unrelated repo's code.
- S10.1 the success status of a route is a single engine function
  (`_route_success_status`) — the binding prints it for the coder and the
  leaf test-status gate enforces it on the tester.
- S10.2 assembly-time import repair covers tests/ (not only src/), re-points
  a phantom `from X import Y` to the unique real owner, and leaves unowned
  symbols untouched (an honest red, never a guess).
- S10.3 the verification suite is HERMETIC: the engine plants an
  oracle-isolation conftest; a decoy visible through PYTHONPATH/.pth must
  stay invisible (dynamic subprocess proof).
- S10.4 a leaf test contradicting the contracted status reds AT THE LEAF
  (test-status gate) with the exact expected value — never first at assembly.
- S10.5 a leaf spec PLANNING src files NO node owns reds at the card gate
  (v150 core.md ordered `src/db.py` + `src/app.py` inside one atomic leaf —
  a mini-architecture smuggled past the graph; the v149 phantom import was
  ORDERED by that prose). Atomicity is enforced on the TEXT, not only on
  metrics: one leaf = one module; several modules = children in the graph.
- (engine, verified via S10.3 seams) the sterile-oracle boundary is ONE
  place (`_prepare_hermetic_suite`): hermetic imports conftest, loopback-only
  network fence (opt-out `project.oracle.allow_network`), assembly import
  repair, workspace-boundary path gate, and the machine interface contract
  (`contracts/interface.json`, minimal OpenAPI-shaped subset) dumped from the
  same functions every consumer reads.
- S10.9 lost-ness of delivered leaf code is judged by WHAT THE FILE DEFINES
  (`_leaf_code_lost`: empty / no module-level def/class/assignment), never by
  a size threshold (v151: one-function `ping_text.py` was flagged
  "absent/empty" by a <3-code-lines count and that single false FAIL flipped
  the run NOT READY).
- S10.10 route ownership in the plan report is DATA (`_route_owners`,
  recorded by `_leaf_owned_routes`), never a spec-prose grep (v151: amend
  specs quote the owner module's source as edit context — the grep reported
  "7 owner leaves — duplicate" for every declared route). Charter P5.
- S10.11 node ids are engine-normalized ASCII snake_case (`_ascii_node_id`)
  at BOTH adoption points (decomposer children, standing requirements), with
  the rename journaled and sibling depends_on remapped; the id style is also
  stated to the decomposer via engine_rules (v151: a human note became node
  «красивый_вид» inside workspace file names).
- S10.12 ONE write door for delivered code: `_delivery_lint` (English/ASCII
  identifiers, no Cyrillic in code files, no absolute host paths, ASCII
  file paths) runs inside BOTH workspace `_write` doors — dirty code never
  lands, the refusal is recorded, and the writer sees the reason via the
  write_refused history. The rule is ONE constant (RULE_CODE_STYLE) rendered
  into the implementer and reviewer prompts — prompt and gate cannot drift.
  Human prose artifacts (specs, notes) are data and are never linted.

## Convention
- A check is HONEST: it reds on a real hole, is never softened to pass.
- Fixes are real engine capabilities, never per-case crutches.
- run-detached.sh will not launch while `pytest tests/audit` is red.
- BOTH DIRECTIONS (v151 lesson): every engine gate gets a RED known-answer
  case (it catches the bad input) AND a GREEN known-answer case (it stays
  silent on a legitimate edge input — tiny-by-design module, quoted context,
  foreign-script id). A gate audited only for misses can still sink a run
  with one false positive: v151's ONLY red was the lost-leaf check
  false-flagging a real one-function module.
- A docstring that claims "single source / shared by X and Y" is a CONTRACT:
  pin it with a consumer test (S10.10 pattern) — v151's ownership report
  grepped prose while its own docstring promised the datum.
