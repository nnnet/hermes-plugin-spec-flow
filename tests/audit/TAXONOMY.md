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
- S9.4 PARALLELISM/ordering: research precedes sibling implementation BY
  CONSTRUCTION, not by thread luck (was: flaky p4 research_before_impl under
  CPU load; task #146). Closed by the runner's research readiness gate:
  spikes decidable at schedule time are HOISTED into the scheduler's own
  thread before the worker pool starts (`_run_child_pool` pre-pass →
  `_run_node_spike`, idempotent); every LAUNCHED wave member is registered
  pending in the scheduler thread before its worker exists and released at
  its spike point (`_release_preamble`, release-on-failure in the worker's
  finally — a dead research never hangs impl); leaf implementation parks at
  `_await_research_preambles` (entry of `_leaf_pipeline`) until the set
  drains, lending its worker slot back so the drain stays deadlock-free.
  Members queued behind the pool limit follow sequential (declared-order)
  semantics. Test: `tests/nodes/test_research_wave_order.py` — adversarial
  emit-boundary scheduler starving the first worker-thread research emit;
  both engines (the fsm has no scheduling of its own — engine-agnostic).
- S9.5 (open) same liveness proof owed to: --resume, doctor remedies,
  worktree isolation, memory tiers — one representative dynamic test each.

## STAGE 10 — Assembly-seam honesty (`test_assembly_seam_honesty.py`,
S10.22 in `test_constitution_pinned_paths.py`)
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
- S10.13 the interface contract GROWS with late requirements: route adoption
  (`_adopted_route_tables`, the ONE datum the resolver and
  `_write_interface_contract` both read) covers not only module dispatch
  tables but also the engine-CANONICAL handler shape — a module-level
  `<method>_<segments>(payload, query)` def for a DECLARED product path adopts
  the (method, path) combo; new paths are never invented and declared routes
  are never overridden (v152: late req delete_note landed
  `delete_notes(payload, query)` in src/core.py via an in-place edit, yet
  contracts/interface.json kept only the 4 base routes and the entry answered
  405 on DELETE /notes).
- S10.14 the small-product collapse purges dropped-module references
  EVERYWHERE, not only in the collapsed leaf spec: the ROOT's own
  spec_markdown and every already-written specs/*.md are retargeted at the
  surviving core module, and BOTH rewrites are journaled ('collapsed spec
  rewritten…' + 'collapse purged dropped-module references…') so the journal
  invariant can attribute the cleanup (v152: specs/l0.md and
  specs/product_entry.md still ordered src/db.py / `db.connect` though no
  node owned db.py after the collapse).
- S10.15 a gate FAIL requires an attributable resolution EVENT: when the card
  gate passes after a previous FAIL for the same node the engine emits a
  matching milestone PASS with the same action prefix ('card gate: …'), and
  the journal invariant accepts exactly that (same prefix + same task + PASS)
  as closure — never the mere absence of a later failure (v152: 'card gate:
  incomplete or non-atomic leaf card' FAILs #18/#19 core and #57/#65 web_ui
  were resolved by rework but left no event, so a genuinely-green run was
  flagged unresolved_milestone_fail ×4).
- S10.16 the ownership datum admits ONLY childless leaves: ownership means
  "this LEAF BUILDS the route", so a branch/root node — whose text names
  every route by construction — never enters `_route_owners`, and
  re-derivation drops a node's stale rows so a reworked spec (or a node that
  gained children) falls OUT of the datum (v152 ticks 111-113, investigator
  finding #1: the ROOT L0 was recorded as an owner of POST /notes, GET
  /notes and GET /health — "3 owner leaves (L0, core, web_ui)").
- S10.17 a DUPLICATE route owner is a root-integrate FAIL milestone +
  doctor cause, recorded so the completion gate blocks: an UNRESOLVED
  duplicate ends an honest NOT READY. The gate reads ONLY the
  `_route_owners` datum — never a per-scenario match. Orphan findings
  (0 owners) stay informational: serving is Phase 7's boot/suite authority,
  and an adopted handler can honestly serve a route with no text-owning
  leaf (v152 ticks 111-115, investigator finding #3: the duplicate finding
  was logged with a NEUTRAL verdict at root integrate, the product went
  READY at tick 114 and the doctor closed with treatments: 0 at tick 115 —
  the engine suppressed its own RULE_ROUTE_OWNERSHIP violation from the
  terminal).
- S10.18 a FOREIGN-surface spec reds EARLY at the card gate: a leaf whose
  own text claims a route ANOTHER node already owns in `_route_owners`
  gets a finding NAMING the owner and demanding removal of the foreign
  route text or explicit re-ownership through decomposition. Amend nodes
  (code_target) stay exempt — they QUOTE the owner's source as edit
  context (the S10.10/v151 class). (v152 investigator finding #2: web_ui's
  spec copy-pasted the frozen JSON API routes with contradictory HTML
  semantics; core.py and web_ui.py both defined post_notes/get_notes,
  assembly adopted core's and web_ui's became dead rival code — caught
  only at root integrate, which is LATE.)
- S10.19 route ownership derives from the node's OWN claim, never from
  inherited or dependency prose: a node carrying typed `exposes` (C2) owns
  EXACTLY the declared routes (prose ignored); the prose fallback reads only
  requirement/title/authored spec minus inherited 'Traces-to'/'Goal:' quote
  lines; and a prose-matched route ALREADY recorded to another leaf in
  `_route_owners` is a DEPENDENCY — excluded from `owned`
  (first-owner-wins, deterministic by the datum) and journaled as 'route
  dependency (owned by <leaf>)'. A declared (exposes) claim of a foreign
  route stays in the datum: it is a REAL duplicate for S10.17/S10.18
  (v154: web_ui's requirement said "reusing the existing notes storage and
  /notes logic" and its spec's Traces-to quoted the parent goal —
  `_leaf_owned_routes` matched both, so `_leaf_route_binding` itself
  ORDERED rival post_notes/get_notes handlers in specs/web_ui.md lines
  26-27; the engine manufactured the duplicate S10.17 then honestly redded
  at root integrate, event ~139).
- S10.20 dropped-module hygiene is DURABLE for the whole run, not a
  one-shot purge: the collapse records the retargeted stems in
  `_dropped_modules`, sanitizes the node's prose fields (spec_markdown,
  requirement, plan), and installs the SAME retarget rule on the spec
  write door, so every LATER specs/*.md write is cleaned too; modules
  never dropped stay untouched (v154: event #11 'collapse purged
  dropped-module references — retargeted at src/core.py in: specs/l0.md',
  yet the FINAL l0.md ordered src/db.py again — REQ-L0-5 + the module
  list, re-authored by the lint rework from unsanitized data — and
  specs/product_entry.md, written later than the purge, named it too).
- S10.21 the foreign-route claim is its OWN attributable card-gate
  milestone ('card gate: foreign route claim', FAIL) with a doctor cause
  and a loop entry, emitted at the LEAF before any integrate-level
  duplicate FAIL — never a fillable card gap: the acceptance-fill rework
  can only add acceptance/examples and can never remove a route claim
  (v154: the S10.18 finding WAS computed but drowned in the generic 'card
  gate: incomplete' events 64/78, truncated at 300 chars behind the
  acceptance finding, got no doctor cause, and the later 'spec lint clean'
  PASS shared the gate id — the run proceeded to order the rival handlers,
  redding only at integrate event ~139). A single-owner plan produces no
  such event.
- S10.22 a module path a constitution rule names LITERALLY ("storage
  through sqlite3 in src/db.py") is IMMOVABLE human data every engine
  transformation must respect (`_constitution_pinned_paths`, entry
  excluded — entry synthesis owns it): the small-product collapse keeps a
  pinned non-entry module as its OWN child leaf (plan = pinned leaves +
  core; core accesses the pinned responsibility ONLY through import), the
  dropped-module purge / durable spec-write sanitizer never retarget a
  pinned stem, and the plan-ownership gate reds — root integrate FAIL +
  doctor cause — when a pinned path has NO owner node in the realized
  plan, naming the path AND the rule (v155 events 14-24: the collapse
  swallowed pinned src/db.py into src/core.py, the S10.20 purge retargeted
  every db.py reference, the spec then openly contradicted the
  constitution and the reviewer rightly rejected it forever — doctor
  oscillated reconcile_check → goal_coverage → redecompose_parent, rework
  exhausted, root RED; v152 shipped READY silently violating the SAME
  rule — no src/db.py was ever built, the reviewer just could not see it
  because the specs still SAID db.py — Phase 7 boot/suite cannot detect a
  module-layout violation, so the plan gate is the only authority). A
  constitution pinning nothing collapses exactly as before (green edge).
- S10.23 the ENTRY leaf's tests are held to the CONTRACTED success status
  of EVERY declared (and adopted) route — the code_target exemption of the
  test-status gate is scoped to feature amends, never the assembly leaf
  (v156: tests/test_app.py asserted 200 for POST /notes while the
  single-source contract says 201; `_leaf_owned_routes` returns [] for any
  code_target node, so S10.4/S10.6 were a no-op for product_entry, the
  wrong TEST reached assembly, the doctor read 'assert 201 == 200',
  diagnosed task_check_mismatch and REWORKED THE WRONG ARTIFACT — core,
  three times, events 139-143 — regressing it on the way). Green edges: an
  entry test asserting the contracted statuses passes; a feature amend
  without an engine-bound route keeps the exemption.
- S10.24 a late requirement routed to AMEND an owner module that implies a
  genuinely NEW (method, path) BINDS that route as engine data
  (`_late_req_bound_route` → `binds_route`): the PATH is never invented —
  it must be a path the amend target ALREADY serves per `_route_owners`
  minus fixed-body liveness paths; the METHOD comes from the requirement's
  own verbs (`_METHOD_SYNONYMS`) and must be new on that path; any
  ambiguity binds nothing. The bound route GROWS `_product_contract` /
  `_declared_route_set` / contracts/interface.json (canonical handler +
  `_route_success_status`), the amend leaf OWNS it (so the route binding,
  handler gate and test-status gate enforce the canonical
  `def delete_notes(payload, query)`), and the entry resolver wires it
  (v156: 'ALLOW REMOVING A NOTE' amended core and went to_done, but NO
  datum carried (DELETE, /notes) — prose named no literal route, S10.13
  adoption matches only the CANONICAL name on a DECLARED path and the
  coder, given no binding, named its handler `delete_note` (checkpoint
  007) — interface.json shipped without DELETE, src/app.py answered 405
  forever, and the integrate rework then silently dropped the handler).
  Green edges: a new PATH named only in prose never enters the contract;
  two new-method verbs at once bind nothing.
- S10.25 a phantom dependency symbol is refused at the ONE write door
  (`_phantom_dependency_symbols` inside `_delivery_lint`, both `_write`
  doors pass the workspace root): ``import X`` + ``X.attr`` where
  <root>/src/X.py exists and defines no module-level ``attr`` never lands
  — in src/ OR tests/ (v156: the integrate rework shipped
  ``db.list_notes()`` while src/db.py defines get_notes, and
  tests/test_core.py calling ``db.connect()``; the assembled product
  500'd GET /notes and the shared boot oracle failed EVERY smoke/e2e
  check, including '[smoke] GET /health -> 200' whose note honestly
  carried the real reason 'GET /notes -> 500' — /health itself answered
  200). Green edges (no false red on build order / dynamic surfaces): a
  defined attr, stdlib imports, a not-yet-built dependency, an
  ``import *`` dependency, dunder attrs and a self-import are clean;
  ``from X import Y`` stays S10.2's seam (assembly import repair).
- S10.26 the late-req delta gate excludes routes the ownership DATUM
  records to ANOTHER leaf — the sibling-served subtraction reads dispatch
  EVIDENCE (`_served_routes`) and misses an owner whose canonical handler
  carries no literal path (v156 events 79-80: web_ui's spec quoted
  'GET /health', core's get_health has no '/health' string, the gate
  FAILed web_ui with "no handler for ['/health']" and the doctor cause
  web_ui:empty_delta stayed OPEN for the whole run — the final root report
  listed remediated noise as an open hole). Green edge: the node's OWN new
  route with no delivered handler (v062 about_page class) stays red.
  v156 postscript — 'doctor causes still open: L0:integrate:
  task_check_mismatch' was HONEST remediation residue (reconcile → rework
  → escalate over the S10.23 wrong-test class, red run end keeps the cause
  open); its root class is S10.23, no separate gate owed.
- S10.27 a leaf test asserting a FOREIGN BODY SHAPE on a contracted route
  reds AT THE LEAF, at the same seam as the status gate
  (`test_leaf_test_body_shape_gate.py`): the contracted body MEDIUM of a
  route is ONE datum (`_route_media_map`: `_product_contract`'s media map —
  human wording around each METHOD-route mention, JSON literal vs page/HTML
  words, conflict = unclassified, the window never crosses a statement —
  plus the fixed-body routes), read by BOTH the interface-contract writer
  (row `media` in contracts/interface.json) and `_leaf_test_status_gate`; a
  POSITIVE membership assertion of an HTML tag marker (`'<ul' in body`,
  `assertIn('<h1>', body)`) after calling a JSON-media route is a finding
  NAMING the contracted shape and the HTML-owning route. Marker-level only
  — never an HTML parser; the amend exemption stays STATUS-scope (v158: the
  'nice to read' amend's tester wrote HTML assertions against GET /notes
  into tests/test_core.py — GET /notes' body is frozen JSON, the HTML
  surface belongs to GET /ui — the status gate saw only codes, the foreign
  tests reached assembly, redded exactly two suite tests at event 139 and
  the doctor reworked CORE, the wrong artifact, twice). Green edges: the
  same HTML assertion in the HTML route's own test; JSON assertions on the
  JSON route; an unclassified route is never flagged (v151 lesson).
- S10.28 a provider reply of ANY content shape degrades, never crashes the
  calling step (`test_provider_reply_shape.py`, real local HTTP server —
  no monkeypatch on the LLM path): `_message_text` normalizes str /
  content-parts list / part dicts / `reasoning` fallback at the ONE parse
  seam, and the shape-except includes TypeError/AttributeError so a
  residual surprise becomes a retryable 'bad response shape' attempt (v158
  root of the OPEN `about_page:empty_delta`: the delta gate was RIGHT —
  the leaf delivered NOTHING because a list-shaped `message.content` hit
  `text.strip()` → AttributeError escaped both the narrow shape-except and
  ask()'s RuntimeError-only chain except and killed the orchestra coder
  AND tester steps, llm-log 155/160: `'list' object has no attribute
  'strip'`; the run-end honestly kept the cause open).
- S10.29 an ENGINE-written support module has a legitimate owner in the
  realized tree (`test_engine_artifact_ownership.py`): the writer of
  src/_product_logic.py is `_harvest_entry_handlers` (entry synthesis
  relocates a monolithic entry's handlers so the resolver can wire them —
  the `import *` synth pattern left in v124, the WRITER stayed); a
  successful harvest now records the module in the entry-owning node's
  `artifacts` (tree DATA the layer-1 `owned_modules` reader honours — no
  name exemption, historical recordless runs keep their honest
  orphan_src_file finding, pinned on v158), and `_drop_stale_harvest`
  removes a harvest the final synthesized entry no longer references
  (v157/v158: the zombie was re-flagged run after run).
  v156 investigator postscript (findings 3-4, assessed v158):
  * 'core forced into non-atomic multi-concern ownership' — the storage
    concern is structurally solved by S10.22 (v158 plan: src/db.py is its
    own pinned leaf, S11 module contracts held with zero ImportError); the
    remaining accumulation of late amends on core (req_a54f9144,
    about_page) is the DELIBERATE amend-in-place design (S4.2, B3
    anti-duplication registry) guarded by the delta gate and S10.24 route
    binding — forking a module per late requirement is the worse v152/v154
    rival-module class. No new gate owed.
  * 'integration gate accepted PASS against partial state' — covered: the
    pre-assembly acceptance PASS (v158 event 136) is not terminal
    authority; entry synthesis follows (138), the assembled suite is
    re-verified (139-145) and Phase 7's assembled-suite conjunction
    (`_assembled_suite_failures`, tests/coverage/
    test_assembled_suite_conjunction.py) plus the root_red honest AND
    (S3.1) decide the verdict — v158 ended NOT READY (147/149) despite
    the early PASS. No new gate owed.

## STAGE 11 — Inter-module symbol contracts (`test_module_symbol_contract.py`)
THE META-CLASS (v149/v150/v156/v157 lineage): a value TWO leaves must agree
on — the symbol surface between an exporting module and its importer — never
existed as ONE engine-declared datum before both sides were coded. Each LLM
guessed, the collision surfaced only at ASSEMBLY, and the doctor's rework
re-guessed (v157: src/core.py line 3 `from db import init_db, store_note,
list_notes` vs src/db.py exporting different names → ImportError at boot,
whole suite + smoke red, doctor churned; v156: rework shipped
`db.list_notes()` while db.py had get_notes; v149/v150: the SAME class on
status codes and response bodies). The engine already solved this for HTTP
routes — contracts/interface.json, single source, both sides read it, gates
enforce. STAGE 11 GENERALIZES that route-contract solution to inter-module
Python symbols:
- S11.1 THE DATUM: for every dependency pair (typed `needs` edges among
  siblings; the S10.22 collapsed plan — core imports each constitution-pinned
  leaf) the engine MATERIALIZES the exporter's symbol contract at PLAN time
  (`_register_module_import` → `_module_contracts`), before either side is
  coded, and persists it to `contracts/modules.json` (module ->
  [{name, args}]) via the ONE write door from the same in-memory dict every
  consumer reads. Sources, deterministic, in priority order: the exporter's
  `exposes` entries ('name(args)' shapes, unioned with canonical route
  handlers via `_leaf_exposed_symbols`); else derivation from the exporter's
  OWN requirement/spec text and the constitution rules — `<stem>.<name>`
  references, the same way route bindings derive from human text (real p6:
  'db.connect reads it per call' is the only storage symbol the human ever
  stated; `<stem>.py` filename mentions are structural, never symbols);
  else the EMPTY set — NO domain default is ever substituted.
- S11.2 BOTH BINDINGS PRINT THE CONTRACT (`_leaf_bindings` →
  `_module_contract_binding`): the exporter's spec orders 'you MUST define
  AT MODULE LEVEL exactly: …', every importer's spec orders 'you may
  import/call ONLY: …' — the SAME `_render_module_surface` string from the
  same datum, so prompt and gate cannot drift. HONEST-RED PATH: an exporter
  with importers and an EMPTY contract reds at the card gate
  (`_card_completeness_findings`) demanding `exposes`, and every importer's
  binding says 'do NOT import from it' — the plan must state the surface,
  the engine never invents one.
- S11.3 TWO DELIVERY GATES at the ONE write door
  (`_module_contract_violations` inside `_delivery_lint`; both `_write`
  doors pass the datum): (a) EXPORTER completeness — `src/<stem>.py` with a
  non-empty contract must bind every contracted name at module level
  (missing → refusal NAMING them); (b) IMPORTER restraint — `from X import
  Y` and `X.Y` where X is contracted and Y outside the contract is refused
  naming the contracted surface. Checked against the CONTRACT datum, never
  the live file, so build order does not matter — this closes the
  from-import exemption of S10.25 order-independently; the v157 collision
  cannot land: whichever side disagrees with the datum reds at ITS delivery,
  never at assembly. Documented edges: an EMPTY-contract module may not be
  imported from at all (finding demands exposes); `from X import *` on a
  contracted module is refused (it bypasses the pinned surface); bare
  `import X` with no attribute use is clean; modules with NO contract
  (stdlib, not-in-plan) stay untouched — S10.25 owns those seams; dunder
  attrs and self-import are skipped; a star-import surface in the exporter
  itself stays lenient (v151: one false positive sinks a run).
- S11.4 DOCTOR/REWORK PRESERVES THE CONTRACT: the module repair directive
  (`_remedy_rework_module`) re-prints the contract block, so a rewriting
  LLM sees the frozen surface instead of re-guessing it (v157: three core
  reworks, each a fresh guess) — and the write door enforces the same datum
  regardless.

## STAGE 12 — Request/config surface as data (`test_request_shape_gate.py`,
`test_rework_preserves_route_surface.py`, `test_unserved_route_fastfail.py`,
`test_doctor_cause_attribution.py`, `test_boot_shape_probe.py`,
`test_smeared_status_autofix.py`, `test_finding_addressee.py`,
`test_request_shape_autofix.py`)
The v159 class: what a route ACCEPTS (request body fields) and what the
product reads from the ENVIRONMENT (config) are two different human-stated
surfaces; when neither exists as engine data, two agents can agree on the
same wrong reading and stay green until product e2e.
- S12.1 the REQUEST shape of a route is engine data
  (`_route_request_fields`: 'POST /notes accepts {"text": ...}' -> ['text'];
  bodyless GET/DELETE routes contract the EMPTY shape) and constitution env
  vars are CONFIG data (`_constitution_env_vars`, the S10.22 analogue). The
  binding prints both ('NOTES_DB is an environment variable, never a request
  field'); contracts/interface.json carries `request_fields` per route; the
  leaf gate (`_leaf_request_shape_gate`) reds a HANDLER whose required-field
  checks (payload[...] / `x in payload`) name a field outside the contract —
  attributing the config-vs-payload confusion by name — and a leaf TEST that
  SENDS fields outside the contract (v159: the assembled product answered
  POST/GET /notes with 400 "missing required field: 'NOTES_DB'" — a config
  KeyError surfaced as request validation and both leaf artifacts had agreed
  on it). Lenient: unshaped body routes and optional `.get(...)` access are
  never flagged.
- S12.2 a rework/redelivery of an owner module PRESERVES the contracted
  route surface: (method, path) -> owner MODULE is engine data
  (`_route_handler_modules`, recorded where ownership is recorded — an amend
  maps to the module it edits via `code_target`); the write door refuses a
  delivery to `src/<stem>.py` missing any contracted route handler
  (`_route_handler_erasure`, S11.3 semantics — any module-level binding
  counts), and the module-rework directive re-prints the route contract
  (`_module_route_binding_text`, the S11.4 twin for routes). v159 root
  cause of the open `product_entry:empty_delta` / /about 404: the amend
  about_page LANDED get_about in src/core.py (commit #6, checkpoint 008);
  the integrate doctor's "rework core (acceptance blamed it)" then rewrote
  the module and the weak model dropped get_about AND delete_notes — the
  module SYMBOL contract was empty (core had no leaf importers), so no door
  could refuse the erasure and the doctor looped on 'weak_implementer'
  without ever naming it.
- S12.3 a DECLARED route with NO resolvable handler fails FAST: at the
  re-verify barrier (`_verify_tests`, right after entry synthesis, BEFORE
  the suite runs) `_unserved_route_gate` resolves every declared route and
  emits a named FAIL milestone per miss — route, canonical handler, owner
  leaf and its module — feeding the doctor at the FIRST assembly. The
  inlined health ok_route is exempt (the synthesized entry serves it);
  an all-served plan is silent; non-web projects are a no-op. v159:
  /about's 404 was honest but surfaced route-attributed only in the LAST
  plan check (tick 170), after the doctor had burnt every repair round on
  generic 'weak_implementer'.
- S12.4 a gate finding opens a doctor cause NAMED BY ITS GATE
  (`test_status_gate` -> `test_status`, `request_shape_gate` ->
  `request_shape`), never shoved into an unrelated bucket
  (`spec_flow_diagnosers._gate_cause`: the `_scope_findings` fallback derives
  the cause id from the originating gate; only delta-flavoured gates and
  hollow-delta texts keep `empty_delta` = 'delivered nothing'). v160 (event
  128/129): the test-status finding «asserts membership over [200, 201]» was
  diagnosed `empty_delta -> reject_empty` although files WERE delivered — a
  mislabeled ledger entry the delta revalidation could never close.
  (`test_doctor_cause_attribution.py`)
- S12.5 an open doctor cause CLOSES ATTRIBUTABLY when its node reaches DONE
  and the OPENING gate re-runs clean on the CURRENT artifacts — the S10.15
  class (attributable resolution) applied to the doctor ledger. The gate FAIL
  registers a side-effect-free recheck (`_register_gate_recheck`, quiet gate
  re-run); root-gate evaluation (`_prune_stale_causes`) re-derives every open
  cause of a DONE node with it: clean -> resolved event naming the gate
  («cause resolved: <gate> re-ran clean»); still red -> the cause stays open
  and holds the root red; causes on non-DONE nodes untouched. NEVER a blind
  auto-close — the verdict is the gate's own re-run. v160 (events 130-138 /
  145): rework fixed the assert, the node reached DONE through
  contract_check + review_pass + verification, yet the (mislabeled, S12.4)
  cause stayed open forever and single-handedly flipped a fully green
  product to NOT READY. (`test_doctor_cause_attribution.py`)
- S12.6 config-vs-request confusion is judged by LIVE BEHAVIOUR at the
  boot-gate, code-shape-independent: `_ROOT_BOOT_PROBE` exercises EVERY
  contracted route with its CONTRACTED example payload (the S12.1
  `request_fields` datum; GET/DELETE = no body) with the constitution's env
  vars ABSENT from the environment; a 4xx naming a required field OUTSIDE
  the contracted shape is a deterministic RED naming the field, the route,
  and — for a constitution env var — 'X is an environment variable
  (constitution), never a request field'. A config-starved 5xx stays legal
  (an honest server-side config error); a 4xx naming a CONTRACTED field
  cannot occur (the probe sends every contracted field) and is deliberately
  not judged. v161: the v159 class landed AGAIN despite S12.1 — the
  required-field surface lived in the SHARED dispatch wrapper (the router
  maps ANY KeyError to 400 "missing required field" while src/core.py reads
  `os.environ['NOTES_DB']` in every handler), so even bodyless GET /ui
  400-ed 'NOTES_DB'; no `payload[...]` shape exists anywhere, making the
  AST leaf gate (`_leaf_request_shape_gate`) structurally blind — and the
  probe's own env defaults (`os.environ.setdefault`) masked the behaviour.
  (`test_boot_shape_probe.py`)
- S12.7 a MECHANICALLY fixable test defect is repaired by the ENGINE, never
  round-tripped through model rework: a smeared success-membership assert on
  an owned route whose set CONTAINS the contracted status carries zero
  ambiguity (the contracted value is engine data), so
  `_leaf_test_status_gate` rewrites `assert code in (200, 201)` ->
  `assert code == 201` (and `assertIn` -> `assertEqual`) itself — AST-span
  text surgery (`_rewrite_exact_status_asserts`), journaled as a
  `test-status-autofix` loop + `test_status_autofix` ENFORCED milestone.
  A smear WITHOUT the contracted member has no mechanical answer and stays
  an honest red; the quiet S12.5 recheck stays side-effect-free. v160+v161:
  the identical finding text «asserts membership over [200, 201] — assert
  exactly the contracted status 201» went into rework in BOTH runs and the
  worker delivered the SAME smear back both times — the final v161
  tests/test_app.py:89 still read `assert code in (200, 201)` and the open
  cause vetoed the terminal. (`test_smeared_status_autofix.py`)
- S12.8 the S12.5 recheck is a PER-GATE OBLIGATION: every re-runnable leaf
  gate that opens a doctor cause MUST register its side-effect-free recheck
  (`_register_gate_recheck`), else `_prune_stale_causes` has nothing to
  re-derive the cause with and `continue`s — an honest repair is then vetoed
  by a STALE ledger entry, the exact S12.5 class one gate over. v161 (event
  144): 'web_ui:handler' held the root red although rework HAD restored
  get_ui and the node reached DONE — `_leaf_handler_gate` was the one gate
  without a recheck. Verified from the same trace: the
  product_entry:test_status recheck DID run and held HONESTLY (the smear
  was still in the final artifact — the S12.7 class), so the ledger is
  honest wherever the wiring exists. (`test_doctor_cause_attribution.py`)
- S12.9 a gate is only real on the paths a run actually TRAVERSES, and a
  probe that skips is a LOGGED skip, never an invisible cap. v162
  (2026-07-03T19-55-37): the final artifact carried BOTH target behaviours
  (POST/GET /notes -> 400 'NOTES_DB', GET /ui + /about -> 404) yet the trace
  had ZERO S12.6 findings and ZERO unserved-route findings until the final
  plan check. Two silencing layers, neither a missing datum (request_fields
  was materialised — hypothesis (a) rejected) nor env leakage (the probe
  pops the constitution env vars — hypothesis (c) rejected):
  (1) `_ROOT_BOOT_PROBE` is fail-fast (`fail()` = SystemExit) and the S12.6
  request-shape section sat LAST, so the 404 on one unwired late route
  exited the probe before the shape section ever ran; the section now runs
  FIRST (an unwired route only 404s there — no field-demand message — so it
  cannot false-red it). (2) `_unserved_route_gate` ran once per
  `_verify_tests`, BEFORE the first suite run — at that instant every route
  resolved (get_ui/get_about still lived in core.py), so it was silent
  legitimately; the round-1 'rework core' then DROPPED both handlers and
  the repair loop re-synthesized the entry WITHOUT re-running the gate —
  the 404s the repair itself created stayed nameless. The gate now re-runs
  after every in-loop re-synthesis (deduped per route; a served-then-
  dropped route re-fires). Anti-silence twin: `_request_shape_datum_gate` —
  a contracted POST/PUT/PATCH route ABSENT from the S12.1 datum is a logged
  SKIP event (gate=request_shape_probe, verdict=SKIP), and when the human
  text names body fields next to that route (a brace list that is not a
  return/response shape) it is a RED 'request-shape-datum' finding: the
  derivation missed a human-stated shape. (`test_boot_shape_probe.py`,
  `test_unserved_route_fastfail.py`)
- S12.10 the integrate-repair blame picker attributes by the ROUTE-OWNERSHIP
  datum of the FAILING TEST's exercised route. v162: the assembled suite
  failed on test_get_about_* / test_get_ui_* (owned by about_page/web_ui),
  no src/<file>.py frame existed (pure assertion failures), and the doctor
  ran 'module repair: rework core (acceptance blamed it)' 3x (ticks
  144/147/150) — the round-1 core rework is what DROPPED get_ui/get_about
  in the first place. Two holes in the old fallback
  (`_blamed_module_from_routes`): it greps ALL route paths out of the whole
  pytest dump, so a co-failing core-owned test steals the blame; and it
  resolves owners through the resolved-HANDLER mapping, which by
  construction has no entry for an unserved route — the exact class that
  needs blame the most. Now `_blamed_module_from_failing_tests` parses the
  failing test ids, extracts each failing test's OWN route calls with the
  same ("METHOD", "/path") constant scan the test-status gate uses, and
  maps routes to owner modules through `_route_handler_modules` /
  `_route_owners` -> `_module_for` (majority vote, deterministic
  tie-break, entry never blamed here). A real src frame still wins (a crash
  site is the strongest signal); a genuinely core-owned failure still
  blames core. (`test_repair_blame_ownership.py`)
- S12.11 an amend for a late requirement that LITERALLY names a new
  "METHOD /path" binds + owns that route. v162: 'ADD AN ABOUT PAGE ...
  Serve GET /about ...' was routed to amend src/core.py; about_page was
  never renamed and no datum rows were dropped — the node simply NEVER
  owned anything: `_late_req_bound_route` only binds a NEW METHOD on a
  path the target already serves (the v156 DELETE /notes shape), so the
  literal new PATH bound nothing, `_leaf_owned_routes` returned [] for the
  amend, `_route_handler_modules` never carried /about -> core, the S12.2
  rework directive/write door had no surface to defend, the round-1 core
  rework erased get_about, and the final plan check (tick 154) reported
  'route GET /about: no owner leaf (orphan)'. The binder now takes a
  literal route the requirement itself names FIRST — only when it is
  already DECLARED (the contract grew from the same human text; nothing is
  invented from prose) and owned by NO leaf; ownership + the module-surface
  datum register at ATTACH time, not lazily at the first gate that asks.
  Greens: a pure presentation refinement (красивый_вид) binds nothing; a
  mention of a route another leaf owns stays a dependency (v145); two
  literal candidates = ambiguity, bind nothing.
  (`test_late_req_route_growth.py`)
- S12.12 a gate finding is ADDRESSED TO THE ARTIFACT OWNER: the doctor
  cause, the rework loop and the S12.5 recheck land on the node that OWNS
  the offending FILE (`_artifact_owner_nid` — reverse lookup of the file's
  module stem in the collision-free node->module registry; for
  tests/test_<m>.py that is <m>'s leaf), never on the owner of the route
  the file merely touches — the route owner is context NAMED IN the
  finding text, not the addressee. v163 (2026-07-03T21-53-39): the
  assembled product was FULLY green (every v149-v162 class gone), yet the
  terminal was NOT READY on one open cause `about_page:request_shape`
  (event 159). The finding (events 126-127) was honest — tests/test_core.py
  sent 'irrelevant' to `GET /about` (contracted shape []) — but the amend's
  gate run attributed it to about_page (the ROUTE owner, its own nid) while
  the offending ARTIFACT is CORE's test file: rework of about_page can
  never edit core's file, so the cause could never close — an eternal root
  red over a green product, the addressing twin of the S12.10 blame hole.
  The request-shape gate now groups findings per offending artifact and
  advises/journals/rechecks per addressee (a quiet recheck is scoped to ONE
  addressee via `for_nid`, so a foreign artifact's red never vetoes another
  node's close); the test-status gate (which also carries the S10.27
  body-shape findings) had the SAME bug and addresses its single artifact
  the same way. Green: a violation in a leaf's OWN test file still lands on
  that leaf itself. (`test_finding_addressee.py`)
- S12.13 a junk payload field sent to a BODYLESS route (contracted request
  shape EMPTY) is a MECHANICALLY fixable test defect — the S12.7 class one
  gate over: an empty shape leaves exactly ONE correct payload ({}), so
  `_strip_junk_payload_dicts` empties the junk dict literal itself
  (`get_about({"irrelevant": 1}, {})` -> `get_about({}, {})`, AST-span text
  surgery) instead of round-tripping a zero-ambiguity edit through model
  rework — journaled as a `request-shape-autofix` loop +
  `request_shape_autofix` ENFORCED milestone addressed to the file owner
  (S12.12). Repaired ONLY when the call shape is CANONICAL: a direct
  canonical-handler call with a dict literal as the first positional arg
  and every key a string constant. Honest reds preserved: a junk field on a
  route with a NON-empty contract is ambiguous (junk? typo of a contracted
  field?) and is NEVER autofixed; non-canonical shapes (the string-route
  `_call("GET", "/about", {...})` form, kwargs payloads) stay classic red
  findings; an already-clean call is untouched byte-identical; the quiet
  S12.5 recheck never rewrites. v163: this junk field was the sole red
  holding a fully correct live product NOT READY.
  (`test_request_shape_autofix.py`)

- S12.14 the ENGINE'S OWN router is never a launderer: a 400 "missing
  required field" comes ONLY from the router's own validation against the
  CONTRACTED request shape (the S12.1 `_route_request_fields` datum baked
  into the synthesized entry as `_REQUIRED`) — so it can only ever name a
  contracted field; an exception ESCAPING a handler (a config KeyError from
  `os.environ['NOTES_DB']` included) is an honest
  500 {"error": "internal: KeyError: 'NOTES_DB'"} naming the exception,
  NEVER fabricated request validation. Malformed-JSON/empty-write 400s and
  a handler's own explicit (400, {...}) return stay untouched. v164: the
  v159/v161 class landed a THIRD time and the laundering wrapper was the
  engine's own `_synthesize_entry_code` template all along — its
  `except KeyError -> 400 "missing required field: %s"` branch turned the
  handlers' NOTES_DB config starvation into a client error on every route;
  the S12.6 probe red-ed the behaviour but the source was engine-owned HTTP
  glue, not model code. (`test_router_exception_honesty.py`)
- S12.15 media conformance is BEHAVIOURAL: the boot-gate probe
  (`_ROOT_BOOT_PROBE`, the S12.6 seam) checks every contracted route's live
  2xx response against its contract-declared media (text/html -> HTML
  marker; application/json -> json.loads succeeds); a mismatch is a
  deterministic RED naming the route, its OWNER LEAF and BOTH medias.
  Lenient edges: no media datum = untouched; non-2xx statuses are owned by
  the other probe sections; the engine's fixed-body defaults
  (`_route_media_map` setdefaults GET /health to json) are NOT judged —
  only the human-worded contract media rows are (a promoted rival serving a
  plain-text health the human never shaped must not false-red, the v151
  lesson). Root fix alongside: the media-derivation window in
  `_product_contract` now honours the statement boundary it always claimed
  (';'/newline) — a brace vote leaking from the neighbouring clause had
  contracted GET /health as json ("... responds {id}; GET /health 200").
  v164: GET /about answered 200 JSON while contracts/interface.json said
  text/html; every media reader was code-shape or test-side, so the rework
  that swapped the HTML page for a JSON dict sailed through the boot-gate
  and only the leaf suite red-ed downstream. (`test_boot_media_probe.py`)
- S12.16 the module-rework directive carries the CONTRACTED MEDIA of every
  route it re-prints — the S11.4/S12.2 frozen-surface reprint pattern
  extended to the body medium: `_module_route_binding_text` appends the
  media line from the SAME datum the interface contract prints
  (`_route_media_map`; text/html routes additionally spell out "an HTML
  page string, never a JSON dict"); a route with no media datum gets no
  media line (the engine never invents a medium). v164: the "rework core
  (acceptance blamed it)" directive re-printed GET /about's handler and
  owner leaf but no medium, and the rewriting model swapped the HTML page
  for {"name": "notes-service", "version": "1.0"} while
  contracts/interface.json contracted text/html the whole time — the exact
  v159 re-guess class, one datum over. (`test_rework_media_binding.py`)
- S12.17 the synthesized router validates request-body field TYPES, not
  only presence (node H4, plan 2026-07-04T00-45; principles-audit F3). When
  the IR (a decomposer machine fragment) declares a request field's JSON
  type, `_route_request_field_types` bakes it into the entry as `_FIELD_TYPES`
  and the router 400s a wrong-typed value AFTER the presence gate, naming the
  field — `{"text": 12345}` for a string field no longer sails into the
  handler. The JSON->Python mapping is EXACT (integer rejects bool, string
  rejects int); a field whose type the IR never recorded (`{f: {}}`, a
  prose-derived shape) is presence-only — the engine invents no type (S13.1
  honest gap). (`test_request_field_types.py`)
- S12.18 A LEAF OWING A CONTRACTED HANDLER CANNOT REACH DONE (node J1,
  `test_late_route_edit_in_place.py`). v165: the `_leaf_handler_gate` FAILed
  (trace tick 144, `get_ping` missing) but its verdict was advisory — the leaf
  reached to_done (tick 154) with the contracted handler still absent, so the
  miss only surfaced at assembly (tick 178) and the run ended NOT READY without
  ever reworking the handler in. A missing contracted handler is a leaf-level
  RED, not a deferred assembly detail. Fix: the handler gate records the owed
  handler(s) per node (`_leaf_handler_missing`, cleared the instant the handler
  appears); `_leaf_ready_for_done` reads it and the leaf lifecycle BLOCKS the
  DONE transition while a handler is owed — it reworks the OWNER module in place
  (the doctor's `_remedy_rework_module` channel, bounded by
  `_handler_max_rework`) and, if the handler is STILL absent after the budget,
  keeps the leaf REJECTED (out of the completed count; resume re-does it) rather
  than green-washing it. GREEN direction: a leaf that defines its handler clears
  the flag and reaches DONE unimpeded.

## STAGE 13 — Spec-IR: one machine interface per node (`test_ir_closed_world.py`,
`test_ir_openapi_conformance.py`, `test_ir_scenarios_schema.py`)
Phase A of the spec-IR rearchitecture (plan 2026-07-04T00-45). Every drift
class S10.9-S12.17 is ONE shape: two artifacts disagreeing on a value that
never existed as data. Instead of catching each pairwise drift with its own
gate, the IR merges the already-recorded datums — route ownership
(`_route_owners`), success status (`_route_success_status`), media
(`_route_media_map`), request shape (`_route_request_fields`), fixed bodies
(`_route_fixed_body`), module symbol contracts (S11 `_module_contracts` /
`_module_importers`), env vars (`_constitution_env_vars`), pinned paths
(`_constitution_pinned_paths`), product entry (`_product_contract`) — into
ONE closed structure per node (`spec_ir.build_ir`), and a validator
(`spec_ir.validate_ir`) refuses anything the interface does not declare.
Ratchet evidence (RED before code): all three test files were committed
against a not-yet-existing `spec_ir` and PROVEN RED — `pytest` collection:
`ModuleNotFoundError: No module named 'spec_ir'`, 3 errors — before the
implementation commit turned them green.
- S13.1 THE BUILDER NEVER INVENTS: `build_ir(engine)` assembles the IR
  purely from recorded datums; a datum the engine never recorded (no media
  for a route, no request-body values) leaves the IR field ABSENT and
  `validate_ir` reports it as an INCOMPLETENESS finding — an honest gap,
  never a guessed default (the v151 no-false-positive discipline applied to
  construction). The engine dumps `ir.json` into the workspace ONCE at plan
  time — right after the realized tree lands — with one journal event
  `ir_written` (greppable in trace.jsonl), so every run carries the
  artifact. Phase A only writes it; compiling specs/skeletons/tests FROM it
  is Phases B/C.
- S13.2 CLOSED WORLD: everything not declared is an error, and every error
  is a PLAIN STRING naming the node id and the offending value (P4). The
  named rules: an unknown key anywhere (IR, node entry, symbols, env,
  scenario, our openapi fragment levels — `x-*` specification extensions
  stay legal per the OpenAPI standard itself); a scenario touching a route
  absent from every node's openapi; a symbol consumed but exposed by no
  node; an env var used in a scenario but declared by no node; two nodes
  owning the same (method, path) — including a drifted ownership DATUM,
  never silently deduplicated; a route owned by a node with children
  (S10.16: ownership means "this LEAF builds it"). GREEN direction: a fully
  consistent IR and the normal branch-owns-nothing tree shape validate with
  ZERO errors.
- S13.3 REAL OpenAPI 3.1: the per-node interface fragment is a genuine
  OpenAPI 3.1 document (`openapi: "3.1.0"`, `info`, `paths`) so
  off-the-shelf contract tools (Specmatic / Schemathesis, Phase B) consume
  it unmodified: requestBody schema with `required` fields and
  `additionalProperties: false` from the S12.1 shape datum; responses keyed
  by `_route_success_status` with content media from `_route_media_map`;
  the ONE contracted fixed body as a JSON Schema `const`; the canonical
  handler as `x-spec-flow-handler`.
- S13.4 SCENARIOS FIRST-CLASS: the G-W-T that stayed prose in cards since
  v150 becomes engine data — NOT full Gherkin (a parser would be its own
  failure source) but a tiny closed schema the engine owns: given {env,
  state: [prior when-steps]} / when {method, path, body} / then {status,
  media, body_check = exactly one of equals|contains|json_subset}.
  Scenarios group by requirement id; the builder derives them from the SAME
  datums the openapi fragment reads (boot `json_roundtrip` becomes the
  GET's given.state carrying the prior POST), so scenario and interface
  cannot disagree by construction; a scenario's then.status/then.media must
  match the owning openapi response — cross-checked at validation.
- S13.5 PAST FAILURE CLASSES ARE IR ERRORS (named known-answer cases):
  v157's phantom `from db import store_note` (symbol consumed, exposed by
  no node) reds naming node and symbol; v164's media drift (/about serving
  a JSON dict against contracted text/html) reds as a scenario/openapi
  media mismatch carrying BOTH medias; v149's status guess (a scenario
  asserting a status the interface never declares) reds naming the status.
- S13.6 THE ENGINE MARKS ITS OWN INVENTIONS (node H2, principles-audit F5):
  a success status derived from the engine's REST convention
  (`_route_success_status`: POST->201, else 200) carries
  `x-spec-flow-status-source: convention` on the operation, and a body the
  engine inlined (`_route_fixed_body`, e.g. /health -> {"status": "ok"})
  carries `x-spec-flow-body-source: convention`. Reading ir.json the invented
  bits are now VISIBLE — separable from spec-declared facts, and later
  demandable from the spec. A decomposer machine fragment replaces the whole
  operation (the IR path never flows through `_node_openapi`), so it carries
  no marker — absence reads as spec-declared. (`test_status_source_
  provenance.py`)
- S13.7 THE SPEC CAN REQUEST A THIRD-PARTY LIBRARY (node H5, principles-audit
  finding F8 — the wall blocking the third-party-first principle from
  products): `product.requirements` is a list of deps (a bare name or
  `{name, version?}`); `node.dependencies` names, per node, which of them
  that node imports; an undeclared node dependency (not in
  product.requirements) is a closed-world error. The import door
  (`_allowed_modules`) admits a declared dependency — `import flask` is no
  longer a phantom-import finding when the spec requested flask — while the
  wall still stands for anything undeclared. `requirements_txt(ir)` compiles
  a pip manifest from the requested deps (versions pinned, bare names kept;
  empty when nothing was requested — stdlib-only stays the default), written
  to the workspace beside ir.json. The `effects` node key (S17.5) is also
  schema-legal now, its values checked against the known effect classes.
  (`test_ir_dependencies.py`)
- S13.8 A THIRD-PARTY STRUCTURAL ORACLE VALIDATES THE IR (node H7,
  principles-audit — the external-oracle pattern applied to the IR itself):
  `spec_ir.jsonschema_errors(ir)` runs a draft-2020-12 JSON Schema
  (`IR_JSON_SCHEMA`) over the IR as a SECOND, independent witness of
  structure, closing the same top/product/node levels validate_ir closes
  (unknown key, wrong type, bad requirement shape). The two oracles AGREE on
  valid IR (both silent) and on structural garbage (both red) — a divergence
  is the signal this node exists to catch. validate_ir keeps the closed-world
  SEMANTICS (route ownership, phantom consumes) a schema cannot express, so
  it stays on top; jsonschema is a dev/test oracle (imported lazily), never a
  runtime dependency of the engine. (`test_ir_jsonschema_oracle.py`)

## STAGE 14 — Scenario runner: THE interface oracle
(`test_scenario_runner_oracle.py`, `test_scenario_engine_wiring.py`)

Node B1 of the spec-IR rearchitecture (plan 2026-07-04T00-45). Phase A made
the G-W-T scenarios DATA; Stage 14 makes them the JUDGEMENT: `spec_scenarios.
run_scenarios(ir, wsgi_app|entry_path)` executes every IR scenario end-to-end
through the WSGI surface as a black box — given.env applied, given.state
replayed, when performed, then judged (status, media, body_check equals|
contains|json_subset) — and the engine consults it at final verification.
Static IR consistency (Stage 13) plus a green suite is still not evidence the
RUNNING product honours the interface; the runner closes exactly that gap.

Ratchet evidence (RED before code): both test files were committed against a
not-yet-existing `spec_scenarios` and unwired engine, PROVEN RED — oracle
file fails pytest collection (`ModuleNotFoundError: No module named
'spec_scenarios'`), wiring file 5 failed (missing `_ir_scenario_gate`, no
re-dump, no scenario_red/scenario_green events) — before the implementation
commit.

- S14.1 THE RUNNER IS THE ORACLE, FAILURES ATTRIBUTABLE (P4): every failure
  names the scenario's requirement id, the OWNING node, the step, and
  expected vs got as plain JSON-safe strings. Named red case: v164's media
  drift caught BEHAVIOURALLY — the LIVE GET /about answers application/json
  while then.media contracts text/html; Stage 13 reds the IR-level
  disagreement, the runner reds the running product even over an internally
  consistent IR. given.state is REPLAYED: a product that drops state
  (POST-then-GET returns nothing) reds on the json_subset check. GREEN
  direction: a conforming app and a stateful roundtrip pass with zero
  failures and a positive passed count.
- S14.2 AN INVALID IR IS REFUSED, NEVER RUN (v149): a scenario asserting a
  status the interface never declares is already an IR VALIDATION error
  (S13.5); the runner refuses to execute — no request is ever issued (a spy
  app records ZERO calls). Refusing beats "helpfully" running an IR whose
  meaning is broken. GREEN direction: a valid IR is never refused.
- S14.3 NEVER A GUESSED VALUE: a when.body ABSENT while the route's
  requestBody schema has required fields is a NAMED incompleteness finding
  (requirement, node, step, the unvalued fields); the scenario is NOT
  executed and NOT counted as passed. No random generation, no defaults —
  a gap is a finding, not a fabricated red and not a silent green. GREEN
  direction: a valued body is executed and judged normally.
- S14.4 ENGINE-OWNED ENV VALUE FACTORY (closes Phase A open question #3):
  deterministic, derived from the IR env entry — a var whose RULE mentions
  a path/file/db gets a fresh temp path UNDER the run workspace; same var
  name -> same value within one run; a NEW run gets a FRESH path (a shared
  value would be cross-run memory, the rejected cache crutch). Behaviour
  comes from the rule text, never from product-specific name literals.
  given.env values are applied to the live process environment and reach
  the app (an app ignoring them reds behaviourally). GREEN direction: a
  non-filesystem rule yields a deterministic non-path token.
- S14.5 THE GATE IS WIRED (final verification + late-injection TDD): the
  engine's `_ir_scenario_gate` runs the scenarios in a hermetic subprocess
  alongside the boot probe inside `_verify_tests`; ANY scenario violation
  makes the verdict NOT READY (P7) with the finding ROUTED to the owner
  node (a `scenario-fail` loop entry + `scenario_gate` FAIL event carrying
  the node id, doctor advised). ir.json is RE-DUMPED whenever the realized
  route set GROWS (the v156 late-binding seam) so the workspace copy is
  always current, and `ir_written` fires again with a reason field (closes
  Phase A open question #4). A late injection is a TDD loop EXPRESSIBLE IN
  THE JOURNAL: the landing is journalled `scenario_red` (nothing serves the
  new route yet) and the rework flips the SAME node to `scenario_green` —
  red-then-green is evidence, not narrative. GREEN direction: a pure
  refinement that binds no route triggers no re-dump; a conforming product
  passes the gate with no scenario-fail loops.
- S14.6 ir.json IS A WORKING ARTIFACT, NOT AN END-OF-RUN REPORT (node I1,
  user request 2026-07-06 "переворот" — the first rung of inverting the IR to
  be the source): (a) `_write_ir` is guarded by `_ir_write_lock` so parallel
  leaves cannot interleave a build+write; (b) the on-disk ir.json is replaced
  ATOMICALLY (tmp + os.replace) — a concurrent reader (the live dashboard)
  always parses a complete document, never the empty truncate window of a
  `write_text('w')`; (c) `_write_ir_incremental` re-dumps after each realized
  leaf so ir.json GROWS during the run instead of appearing only at the end.
  This makes the IR live and inspectable; the deeper inversion (IR as the
  accumulated source, datums as projections) is nodes I2/I3.
  (`test_ir_incremental_write.py`)
- S14.7 THE LIVE BODY IS JUDGED BY THE SCHEMA LIBRARY, NOT ONLY BY HAND
  (nodes K1b + L1, library-inventory audit; user superpriority 2026-07-06
  "use the OpenAPI library"): when the IR declares a CLOSED response schema
  for the scenario's route+achieved status (the S21 shape — properties /
  required / additionalProperties:false; the honest-gap bare `{}` and a
  `const` are NOT closed and stay on the historical paths), the runner
  validates the LIVE response body against that schema with the THIRD-PARTY
  `openapi-schema-validator` (OAS31Validator, wrapping jsonschema). This
  catches what the hand-rolled equals/contains/json_subset structurally
  CANNOT: a WRONG type on a declared field, and an EXTRA field under
  additionalProperties:false — both of which `_json_subset` (subset-by-key,
  leaf-by-equality) silently passes, the F1 exploit at RUNTIME. The check is
  ADDITIVE: the hand body_check kinds a schema does not express (equals /
  contains, and json_subset for open shapes) are UNCHANGED and remain the
  fallback. The library is imported LAZILY as a dev/test oracle
  (tests/requirements-dev.txt); if it is absent the runner falls back to the
  hand check with a note and never hard-crashes. This wires
  openapi-schema-validator into genuine use, resolving its dead pin (K1b).
  GREEN direction: an honest body matching the closed schema stays a pass;
  a route with no closed schema is judged exactly as before.
  (`test_scenario_body_oracle.py`)
## STAGE 15 — Decomposer emits IR (`test_decomposer_emits_ir.py`)
Node E1 of the spec-IR rearchitecture (plan 2026-07-04T00-45; Stage 14 is
reserved by the scenario-runner node B1, developed in parallel). Until now
the decomposer handed the engine PROSE (requirement / title / card text) and
the engine re-derived interface facts by grepping it — every S10.19 prose
path was a guess deferred to assembly. E1 makes the decomposer's output
carry a MACHINE part: `"ir"`, a spec-flow IR v1 document (`spec_ir.py`)
with per-proposed-node files, a real OpenAPI 3.1 fragment for the routes
the node will OWN (method/path/status/media/request required fields),
symbols (exposes/consumes), env, and the tiny closed G-W-T scenarios. The
ENGINE validates the machine part with `spec_ir.validate_ir` at the exact
seam where the output is received (`_expand_node` →
`_accept_decomposer_ir`), against the MERGED document of every fragment
accepted so far, BEFORE any assembly starts.
Ratchet evidence (RED before code): `test_decomposer_emits_ir.py` was
committed against an engine without `_accept_decomposer_ir` and a prompt
without the machine part, PROVEN RED — 9 failed, 1 passed (the
GREEN-direction legacy case) — before the implementation commit.
- S15.1 MISSING MACHINE PART IS A NAMED REFUSAL: an IR-capable decomposer
  (`emits_ir` capability, carried through every wrapper) whose output has
  no `"ir"` gets a milestone FAIL on gate `decomposer_ir` naming the node,
  a recorded `decomposer-ir-refused` loop, and ONE bounded re-ask whose
  context carries the exact errors (`ir_errors`) — the same retry chain
  bad JSON already drives inside the worker (llm_backend model fallback).
  Never a silent fallback to prose-only. GREEN direction: a legacy /
  simulated decomposer that never declared the capability keeps the
  historical contract untouched — no refusal, no retry, no new events.
- S15.2 INVALID IR IS ATTRIBUTABLE (P4): the refusal errors are plain
  strings naming the node id and the offending value (unknown key, a route
  claimed by a node WITH children — spec_ir's closed world). A refused
  fragment never enters the engine's IR registry, so a later consumer can
  never read a value that failed validation.
- S15.3 ONE OWNER PER (METHOD, PATH) ACROSS CALLS: the seam validates the
  MERGED document, so a second decomposer call claiming a route an earlier
  fragment owns reds AT THE SEAM naming both owners (first-owner-wins in
  the registry) — the v154 rival-handler class caught at proposal time
  instead of assembly time. Consumed-but-not-yet-exposed symbols are NOT
  refused mid-growth (the tree is still being proposed); the full-tree
  closed-world check (`_write_ir`, S13) still reds a phantom that never
  materialises.
- S15.4 PROSE IS NO LONGER THE CARRIER: when the decomposer supplied a
  fragment for a node, `_leaf_owned_routes` reads the fragment's openapi
  paths — the prose claim text is not consulted — and journals
  `interface_source: ir`; without a fragment the S10.19 derivation stays
  as the fallback and journals `interface_source: prose-derived`. Request
  required fields and body media declared in accepted fragments win over
  prose-derived guesses in `_route_request_fields` / `_route_media_map`.
- S15.5 THE DECOMPOSER IS ASKED FOR VALUES, THE ENGINE NEVER GUESSES THEM
  (Phase A open question #1): the prompt orders concrete example values in
  scenario `when.body` for routes with required request fields; a scenario
  the model still left without a body is accepted as an honest
  INCOMPLETENESS finding (spec_ir reports it; the accept event carries the
  tally) — the engine never injects an invented body.
- S15.7 THE PROSE FALLBACK IS GATED, NOT SILENT (node H8, principles-audit
  F4): the engine carries an `interface_policy` (default `ir-required`);
  an unknown value is refused LOUDLY at construction (never a silent
  typo-to-default). The knob is a case datum (`run_cases` reads
  `case["interface_policy"]`) and a `run_project`/`Engine` parameter — the
  same plumbing as `review_policy`.
- S15.8 UNDER `ir-required`, a node whose interface was prose-derived
  (`_log_interface_source(nid, "prose-derived")`, the S10.19b fallback) is
  recorded in `_interface_prose_nodes` and surfaces as a FAILING product
  check (`_interface_policy_failures` → `_product_check` record → NOT READY)
  naming the node — following the decomposer_ir FAIL precedent. The event is
  a MILESTONE, not a whispered DETAIL, so the prose channel is visible.
- S15.9 UNDER `allow-prose`, the historical fallback runs unchanged and
  produces NO violation — the consent is explicit and the journal still
  carries `interface_source: prose-derived (interface_policy: allow-prose)`.
- S15.10 an `ir`-sourced interface never violates any policy (it is exactly
  what `ir-required` demands). (`test_interface_policy.py`)
## STAGE 16 — OpenAPI compilation from the IR (`test_openapi_compiler.py`)
Phase B node B2 (`contract-oracle`) of the spec-IR rearchitecture (plan
2026-07-04T00-45). The per-node OpenAPI 3.1 fragments inside `ir.json` are
compiled by `spec_openapi.compile_openapi` into ONE product-level document
that external contract oracles (Schemathesis property fuzzing, Specmatic
contract tests) consume unmodified; `spec_openapi.lint_openapi` is the
stdlib structural self-check over that document. The compiler NEVER invents:
paths come verbatim from the fragments, and the injected error responses
mirror EXACTLY what the engine's synthesized router
(`_synthesize_entry_code`) actually does — this closes Phase A open
question #2 (router error responses as shared data). Stage numbers 14–15
are reserved for the parallel B1 (scenario-runner) / E1 (decomposer-IR)
nodes.
Ratchet evidence (RED before code): `test_openapi_compiler.py` was
committed against a not-yet-existing `spec_openapi` and PROVEN RED —
pytest collection: `ModuleNotFoundError: No module named 'spec_openapi'`,
1 error — before the implementation commit turned it green.
- S16.1 ONE DOCUMENT, OWNERSHIP HONORED: all node fragments merge into a
  single `openapi`/`info`/`paths` document; every operation carries its
  owning node as `x-spec-flow-node`. Two nodes claiming the same
  (method, path) is a NAMED refusal — `DuplicateRouteError` carrying the
  method, the path and BOTH node ids (P4) — never a silent
  last-writer-wins merge. GREEN direction: distinct routes merge with
  every fragment value verbatim.
- S16.2 ROUTER-TRUTHFUL ERROR RESPONSES: per operation the compiler
  injects exactly the error responses the synthesized router produces —
  404 unknown path (`{"error": "not found"}`), 405 declared path with an
  undeclared method (`{"error": "method not allowed"}`), 500 escaped
  handler exception (`{"error": "internal: <type>: <msg>"}`), and 400
  ONLY on operations whose contracted requestBody has required fields
  (the router's S12.1 validation; the same writes also 400 on
  malformed/empty JSON bodies). All are `$ref`s into ONE shared
  `components.responses` section quoting the router's actual bodies; a
  status the fragment already declares is NEVER overwritten (the
  fragment is the contract source). RED direction: a GET or an
  empty-shape write gaining a 400 would be invented behavior.
- S16.3 STRUCTURAL SELF-LINT: `lint_openapi` checks the OpenAPI 3.1
  rules verifiable with stdlib alone — required top-level keys, the 3.1
  version string, well-formed paths/operations/responses/content, and
  resolvable local `$ref`s — returning NAMED findings
  (`{rule, where, message}`). An operation without `responses` is a
  finding; the compiled document from a valid IR lints CLEAN (zero
  findings), otherwise we ship a document the oracles reject.
- S16.4 HONEST MEDIA GAP: a fragment response with no recorded media
  compiles to a response WITHOUT `content` plus a gap finding in the
  document's `x-spec-flow-gaps` (naming node, method, path) — never a
  guessed content type (the S13.1 no-invention discipline carried
  downstream). A description-only response is legal OpenAPI: the gap is
  a finding, not a lint failure.
- S16.5 EXTERNAL ORACLE HARNESSES: `tests/tools/run_schemathesis.py` and
  `tests/tools/run_specmatic.py` assemble the exact CLI invocations
  (`<cli> run <schema> --url <base>`; `java -jar <jar> test <spec>
  --testBaseURL=<base>`) and probe tool availability honestly (which
  binary/package/jar is missing). Argument assembly is unit-tested at
  inspection level, so the harness stays correct even in environments
  without pip-install rights or java — there the plan node is marked
  `[!]` with the precise human-needed list.
- S16.6 405 CARRIES `Allow` (RFC 9110) — node B4 `router-allow-header`
  (`test_router_allow_header.py`), born from an honest Schemathesis
  finding during the B2 demo: RFC 9110 §15.5.6 makes `Allow` MANDATORY
  on a 405, and the router already holds the truth in its own `_ROUTES`
  table. RED direction: a synthesized-router 405 without `Allow`, or
  with a value that is not EXACTLY the path's contracted methods sorted
  and comma-separated (`GET, POST`; single-method paths pinned too);
  the OpenAPI mirror reds when the shared `RouterMethodNotAllowed`
  response does not declare `headers.Allow` (OpenAPI 3.1 response
  `headers`, string schema) with the description saying so. GREEN
  direction: a 404 carries NO `Allow` (nothing is contracted there —
  listing methods would be invented behavior) and success responses
  stay untouched. Pure engine data (route table / compiled document),
  never guessed. Ratchet evidence (RED before code): 3 failed —
  `Allow` absent from both 405 cases (`got headers {'Content-Type':
  'application/json'}`) and `headers.Allow` absent from the compiled
  405 component — with the 2 green guards already passing.
- S16.7 THE COMPILED OPENAPI IS VALIDATED BY THE STANDARD LIBRARY (node K1,
  user superpriority 2026-07-06): `spec_openapi.validate_openapi_library(doc)`
  runs the third-party openapi-spec-validator (a maintained OpenAPI 3.1
  oracle, pinned in tests/requirements-dev.txt, imported lazily) alongside
  the hand-rolled lint_openapi. Our compile_openapi output passes the library
  unmodified (the compiled form IS standard 3.1, not a private dialect); a
  broken document (missing required field, wrong type) is caught by the
  library. First rung of "OpenAPI via LIBRARY, not hand-rolled": the deeper
  переворот is the decomposer emitting an OpenAPI document as the PRIMARY
  spec (K2) and prose .md becoming a derived readout (K3).
  (`test_openapi_library_validator.py`)

## STAGE 17 — Skeleton compiler: the ENGINE writes the module skeleton
(`test_skeleton_compiler.py`, `test_skeleton_write_door.py`)

Node C1 of the spec-IR rearchitecture (plan 2026-07-04T00-45). Stage 13
made the interface DATA; Stage 17 removes the model's freedom to restate
it: the module skeleton — signatures, allowed imports, env access points,
per-function contract anchors — is COMPILED from the node's IR entry by
the engine (`spec_skeletons.compile_skeleton`), the model fills ONLY the
function bodies, and the ONE write door judges every delivery against the
skeleton (`spec_skeletons.skeleton_conformance`).

Ratchet evidence (RED before code): both test files were committed against
a not-yet-existing `spec_skeletons` module and an unwired engine, PROVEN
RED — pytest collection fails with `ModuleNotFoundError: No module named
'spec_skeletons'` (2 errors) — before any implementation commit.

- S17.1 SKELETON IS A COMPILER OUTPUT, NEVER A MODEL GUESS: handler defs
  come from openapi `x-spec-flow-handler` plus the platform
  `(payload, query)` ABI; exposed-symbol defs from `symbols.exposes`;
  the import line-up from `symbols.consumes` ONLY; env access points from
  the declared `env` entries with their rules; each def carries a single
  `raise NotImplementedError` placeholder under an
  `AICODE-NOTE: skeleton-contract <symbol> ...` anchor naming method,
  path, contracted status and request fields. Deterministic: the same IR
  compiles to a byte-identical skeleton; a node with no IR interface
  (branch, unknown id) compiles to NO skeleton.
- S17.2 CONFORMANCE IS CLOSED-WORLD AND ATTRIBUTABLE (P4): a delivery is
  refused when it renames a contracted function or changes its argument
  list (the v143 rename class), imports a module outside
  `symbols.consumes` + stdlib (the v157 phantom-import class dies AT THE
  DOOR instead of at boot), defines a public function/route the IR never
  contracted (closed world), or presents engine skeleton text with the
  anchors stripped/tampered (a skeleton edit). Every finding names the
  node and the offending symbol. GREEN direction: the honest bodies-only
  delivery passes with ZERO findings; private `_helpers` and stdlib
  imports are the implementation's own business; a FRESH conforming module
  that carries no engine text is judged on the data checks alone — the
  anchor rule is engine-text integrity, not a comment tax (the v151
  false-positive lesson: `test_dynamic_collapse_bindings_share_contract`'s
  conforming deterministic implementer must keep landing); a SyntaxError
  is the suite's verdict, not the door's (the S10.12 convention).
- S17.3 ONE DOOR, EXISTING REFUSAL SEMANTICS: the check runs inside
  `_delivery_lint` for every registered code file — a refused delivery
  never lands and leaves the standard `refused_code` artifact with the
  findings as the reason. Files with NO registered skeleton keep today's
  path untouched (fallback, no behavior change); the product entry module
  stays engine-synthesized (the v164 template-revision lesson), never
  skeletoned.
- S17.4 REGISTRATION STAYS CURRENT: the engine compiles and registers the
  skeleton at leaf time (`_ir_skeleton_for`) and hands it to the coder via
  ictx + the worker prompt block (both chat and claude paths). A late
  requirement that grows the module's route surface DROPS the stale
  per-node registration on the IR re-dump — otherwise the honest rework
  that ADDS the late handler would be refused as "uncontracted" (a
  self-made deadlock); the S12.2 erasure gate owns the co-owned surface.
- S17.5 THE BODY LIVES IN A CLOSED EFFECT WORLD (node H6, principles-audit
  finding F2): the skeleton door closed the import world and the public
  surface, but a delivered body could still spawn processes, open sockets,
  mutate the process environment or write arbitrary files — an effectful
  body passed with ZERO findings. `spec_skeletons.body_effect_findings`
  walks the body AST; an effectful stdlib surface is a finding unless its
  class is in the node's optional `effects` datum (ABSENT = deny all).
  Classes: `subprocess` (subprocess import, os.system/exec*/spawn*/fork/kill/
  popen), `network` (socket/ssl/ftplib/smtplib/socketserver/http.client/
  urllib.request imports — urllib.parse stays green), `env-write`
  (os.environ mutation, os.putenv/unsetenv — env READS are the C1-anchored
  access pattern and stay green), `fs-write` (open() in a write/unprovable
  mode, os fs mutators, pathlib write methods, shutil/tempfile imports),
  `dynamic-import` (__import__/importlib — the trivial bypass of BOTH closed
  worlds). READ is green, WRITE is a finding; the checker is SHARED with the
  doctor's repair door (`apply_function_body(..., allowed_effects=...)`) —
  `open(path,'w')` needs no import, so the module-shape refusal never fired
  and the repair door was a second silent hole. Both directions: the honest
  leaf vocabulary (json/datetime/uuid/math/re/sqlite3 + env-read persistence)
  yields ZERO findings, and str.replace is not a pathlib false positive (the
  v151 lesson). (`test_body_effect_door.py`)
- S17.6 A LATE ROUTE ON AN EDIT-IN-PLACE MODULE IS ADMITTED, NEVER DEADLOCKED
  (node J1, `test_late_route_edit_in_place.py`). v165 (p6-micro-notes): the
  human injected 'ADD A LIVENESS PING (GET /ping -> "pong", 200)' mid-run; the
  engine BOUND the route (the contract, interface.json and the plan all carried
  `get_ping`) yet get_ping was NEVER written to src/core.py and the run ended
  NOT READY — an ENGINE defect, not a weak model. The co-owned owner module
  (core, built first) kept its stale per-node write-door skeleton whose closed
  public surface did NOT include get_ping, so a delivery ADDING get_ping was
  refused 'public function get_ping is not in the IR interface'. The redump
  path only DROPPED the stale registration, leaving NO closed world at all
  (any public function admitted) AND no skeleton get_ping could belong to — a
  self-made deadlock the S17.4 docstring warned of but did not close. Fix: a
  grown co-owned module is RE-REGISTERED against the module-surface UNION
  (`_module_surface_ir`: the openapi over EVERY route recorded to the module in
  `_route_handler_modules`, keeping the owner's consumes/env import world) — the
  door then ADMITS every contracted handler of the whole module AND still
  refuses an uncontracted extra (closed world holds). The refresh runs on the
  IR re-dump AND unconditionally from `_attach_late_req` when a route binds into
  a registered module (the redump no-ops before the first plan-time dump — the
  ordering that left the door shut). Both directions: get_ping lands with ZERO
  findings; a `backdoor` public function is still refused.

## STAGE 18 — Conformance tests compiled from the IR (`test_ir_compiled_tests.py`)
Phase B node B3 (`retire-llm-tester`) of the spec-IR rearchitecture (plan
2026-07-04T00-45; Stage 17 is reserved by the parallel C1 skeleton-compiler
node). Until now the leaf's `tests/test_<module>.py` was AUTHORED BY AN LLM
tester over the same facts the IR already carries — every interface assert
was a re-guess. v149 ("the tester guessed a status") and v150 ("success on a
foreign route", smeared `assertIn(code, (200, 201))`) are one class: tests
inventing interface facts. `spec_conformance.compile_leaf_tests(ir, node_id)`
makes the ENGINE the author: a deterministic pytest file derived ONLY from
the node's IR entry — one test per contracted (route, status, media) with the
request built from the openapi required fields valued by the node's scenario
bodies, plus scenario-derived multi-step tests (given.env overlay, given.state
replay, then judged). The LLM tester is retired from interface coverage;
optional domain edge-cases beyond the interface stay out of scope.
Ratchet evidence (RED before code): `test_ir_compiled_tests.py` was committed
against a not-yet-existing `spec_conformance`, an engine without
`_compile_ir_leaf_tests` / `_reassert_ir_leaf_tests` and a harness without
the tester-retirement seams, PROVEN RED — pytest collection:
`ModuleNotFoundError: No module named 'spec_conformance'`, 1 error — before
the implementation commit turned it green.
- S18.1 ONLY CONTRACTED DATA CAN APPEAR: every route, status, media and
  request value in the compiled file traces to an IR datum — the compiler
  has no other input. Known-answer cases: the file for a node contains ONLY
  that node's contracted routes (the v150 foreign-route success is dead for
  IR leaves); the POST request value is the scenario's recorded body datum.
- S18.2 FOREIGN DATA IS REFUSED AT COMPILE: an IR that fails
  `spec_ir.validate_ir` (the v149 scenario asserting an undeclared status)
  raises `ValueError` naming the offending value BEFORE any file content
  exists — an undeclared status is impossible by construction, not filtered
  postfactum. An unknown node id refuses the same way. Mid-growth
  "consumed but not yet exposed" findings stay non-fatal (the S15.3 seam
  rule; the full-tree closed world still owns phantoms).
- S18.3 EXACT STATUS ASSERTS: a compiled test asserts `== <contracted>`;
  a membership set of statuses cannot appear in the output (AST-pinned) —
  the S12.7 smear class has no author anymore.
- S18.4 A MISSING DATUM IS A VISIBLE SKIP: a bodied route whose required
  fields no scenario values, a bodied route with no recorded request shape,
  a non-success status no scenario yields, and a scenario stepping onto a
  route owned by another node all compile to `pytest.mark.skip` whose reason
  NAMES the gap (route, fields, owner) — an honest gap, never an invented
  value and never a silent drop (S13.1 discipline carried downstream).
- S18.5 THE ENGINE IS THE AUTHOR, THE TESTER IS RETIRED (wired): in the
  leaf pipeline a leaf with an accepted IR fragment gets
  `tests/test_<module>.py` WRITTEN by the engine before the worker runs
  (journal `leaf_tests_source: ir-compiled`; the import module name is the
  engine's `_module_for` datum, overriding the model-proposed stem); the
  worker context carries `tests_precompiled`, the orchestra drops the tester
  step through `_active_team` (journaled `orchestra_step_skipped`), the
  write door refuses worker writes and diff repairs aimed at the compiled
  file, the file is seeded into an isolated worktree, and after the worker
  returns the engine RE-ASSERTS the compiled content over any rewrite
  (`ir_tests_authority` ENFORCED) — enforcement is code, not prompt hope.
  A compiler refusal falls back to the LLM path carrying the refusal reason.
  GREEN direction: a leaf without an IR fragment keeps the historical path
  byte-for-byte and journals `leaf_tests_source: llm`.
- S18.6 EXISTING GATES HOLD, NEVER WEAKENED: the S10.6/S12.7 status gate and
  the S12.1 request-shape gate run unchanged over compiled files and pass
  trivially — the compiled asserts ARE the contracted datums the gates read.
- S18.7 AN EDIT-IN-PLACE LATE ROUTE GETS A COMPILED CONFORMANCE TEST (node J1,
  `test_late_route_edit_in_place.py`). v165: `_compile_ir_leaf_tests` hard-
  skipped an edit-in-place leaf ('edit-in-place leaf keeps the llm path'), so
  no IR conformance test was compiled for the bound GET /ping — nothing stayed
  hard-RED until get_ping existed and the LLM tester wrote `from app import
  get_ping` (never touching core.py). Fix: an edit-in-place leaf that OWNS a
  bound route has NO decomposer fragment (the route is ENGINE-derived data), so
  the engine synthesizes the fragment from its OWN `build_ir` node (which
  carries the bound route's openapi) and compiles a DEDICATED conformance file
  `tests/test_<nid>_conformance.py` — the owner's own test file is left
  untouched (append-safe). The compiled test imports the contracted handler
  from the owner module and is RED while it is absent, GREEN once defined; the
  reassert (S18.5) targets the recorded conformance path, not the caller's
  tests/test_<module>.py. GREEN direction: a pure-refinement amend that binds
  NO route keeps the historical llm path (`leaf_tests_source: llm`).

## STAGE 19 — Counterexample repair: re-ask ONE function (`test_counterexample_repair.py`)
Phase D node D1 (`counterexample-repair`) of the spec-IR rearchitecture (plan
2026-07-04T00-45). Until now every repair path re-asked the model with the
WHOLE module plus the raw pytest dump (`_REPAIR_TASK` / `_REPAIR_DIFF_TASK`
carry full src + full test + output tail), inviting a rewrite of everything
in sight — v159 "the core rework re-guessed the module and erased routes"
and the v160 eternal-open causes are one class: a repair whose degrees of
freedom exceed the defect. B3 (Stage 18) made the leaf's contract tests an
engine-compiled artifact and C1 (Stage 17) made the skeleton engine-owned;
this stage composes them: a failing compiled contract test is reduced by
DETERMINISTIC ENGINE CODE to a COUNTEREXAMPLE — function, input, expected,
got — and the remedy re-asks the model for ONE function body with FRESH
context (Ralph loop: one task per iteration, objective exit = the real
contract-test run going green). Model-independence via minimum degrees of
freedom: the re-ask carries only the one function's slot, and the write door
accepts only that function's body — a full-file rewrite is impossible BY
CONSTRUCTION, not by review.
Ratchet evidence (RED before code): `test_counterexample_repair.py` was
committed against a `spec_flow_doctor` without `Counterexample` /
`extract_counterexamples` / `function_slot` / `repair_prompt` /
`apply_function_body` / `counterexample_repair` and a `spec_flow_remedies`
without `counterexample_config`, PROVEN RED at pytest collection — before
the implementation commit turned it green.
- S19.1 EXTRACTION IS DETERMINISTIC ENGINE CODE: the counterexample is
  parsed from the compiled-test failure output (pytest failure sections) —
  never an LLM call; attribution is the FAILING STEP (a scenario red on its
  given.state POST blames `post_notes`, never the unreached when-step
  handler); a green run or garbage output yields `[]`, total and crash-free;
  same output -> same counterexamples.
- S19.2 ONE FUNCTION, FRESH CONTEXT: `function_slot` is the engine-owned
  def line plus the C1 contract anchor — never the failed body; the repair
  prompt carries the slot and the counterexample (input/expected/got) and
  NOTHING else — no sibling function, no module body, no raw pytest dump.
  The `BODY_ONLY_RULE` sentence in the prompt is the same contract the door
  enforces (prompt states, code guarantees).
- S19.3 THE DOOR ACCEPTS ONLY THAT FUNCTION'S BODY: an unknown function, a
  module-shaped reply (imports / column-0 defs beyond the one slot), a
  renamed def or a rewritten argument list are REFUSED with a named
  ValueError; a def hidden inside a block lands as a harmless NESTED def
  while every byte outside the target block stays identical
  (source-segment pinned) — both smuggling vectors dead; the splice keeps the
  engine signature and the AICODE-NOTE anchor, and Stage 17's
  `skeleton_conformance` stays green over it — D1 composes with the C1 door,
  never bypasses it.
- S19.4 OBJECTIVE EXIT, ONE TASK PER ITERATION, BOUNDED: `green` means the
  REAL test run passed — a model that answers every round but never fixes
  the defect ends `green=False` at `max_rounds`; each iteration re-extracts
  the counterexample from the LATEST run (iteration 2 quotes the new
  observed value, never a replay); a red run with no extractable
  counterexample stops WITHOUT an LLM call, journaling the reason — the
  remedy never guesses which function to blame.
- S19.5 KNOBS ARE DATA: `counterexample_config` lives in the remedies
  5-layer merge (factory default -> env JSON -> case YAML -> overrides), so
  `max_rounds` is a per-case knob, not a literal in the loop.

Node D2 (`wire-counterexample-repair`, `test_counterexample_wiring.py`)
connects the D1 mechanism to the repairman seam of the harness. Both repair
call sites — `_orchestra_run`'s fixer step and `make_implementer`'s repair
round — used to re-ask with `_REPAIR_DIFF_TASK` (full module + full test +
raw pytest dump) regardless of whether the leaf's tests were engine-compiled.
Ratchet evidence (RED before code): the wiring rules were committed against a
`role_worker` without `_counterexample_repair_step`, PROVEN RED, before the
wiring commit turned them green.
- S19.6 ENGAGEMENT: with `ctx['tests_precompiled']` set AND a failing
  compiled run, `_counterexample_repair_step` hands repair to the doctor's
  Ralph loop — the model receives ONLY the one-function slot +
  counterexample prompt (BODY_ONLY_RULE; no sibling function, no module
  body, no test source, no SEARCH/REPLACE diff task) and a correct body
  turns the leaf green through the SAME two-tier `_leaf_bar`; the journal
  event `counterexample_repair` names the re-asked function(s) — the proof
  the re-ask was one-function-scoped, asserted, never assumed.
- S19.7 THE DOOR HOLDS AT THE SEAM: a module-shaped reply (the v159
  rewrite class) cannot land — the workspace module stays byte-identical
  and the leaf ends honestly red; the constraint is the D1 write door
  (`apply_function_body`), enforced by code at the seam, not by prompt hope.
- S19.8 FALLBACK IS BYTE-FOR-BYTE LEGACY: without the flag, or when the red
  run yields no extractable counterexample, the step declines (None) with
  ZERO LLM calls, ZERO writes and ZERO journal events — the caller walks
  the historical whole-file repair path unchanged (the GREEN direction of
  the node's acceptance).
- S19.9 THE SEAMS ARE CONSULTED: both call sites resolve repair through
  `_counterexample_repair_step` BEFORE `_REPAIR_DIFF_TASK` (source-pinned,
  the S18.5 `_active_team` lesson) — the diff task survives as
  fallback-only, so a leaf on the legacy LLM-test path keeps it.
- S19.10 THE DIFF APPLIER IS HAND-ROLLED ON PURPOSE (node H1b,
  `test_diff_repair_reason.py`): the fallback SEARCH/REPLACE path
  (`_REPAIR_DIFF_TASK`) applies its blocks through `harness/diff_repair.py`,
  an aider-style applier written by hand rather than on a diff library
  (diff-match-patch / unidiff / python-patch). This was the ONLY hand-rolled
  harness component without a recorded reason — the wave scheduler (H1) and
  the closed-world IR (H7) both document theirs. The reason is now pinned in
  the module docstring and cannot silently return: a generic diff library
  does not give the model-facing block FORMAT contract (the `FILE:` +
  `<<<SEARCH/===/>>>REPLACE` grammar tolerant of marker runs >= 3), the
  empty-SEARCH = whole-file escape hatch, the EXACTLY-ONCE refusal (0 = gone,
  >1 = ambiguous → refuse, never a fuzzy/blind write — the opposite of
  diff-match-patch's threshold matching), or the write-door tie-in (`allowed`
  allowlist + per-file accumulation + staged files-to-write). The audit
  parses the docstring and asserts BOTH a candidate library name AND the
  domain markers are present — the reason is checked by MARKER, not one
  sentence, so it may be reworded but never dropped.

## STAGE 20 — Universality battery: generated specs across domains (`test_universality_battery.py`)
Node F1 (`universality-battery`) of the spec-IR rearchitecture (plan
2026-07-04T00-45, "Целевая форма"): the product goal is UNIVERSAL software —
new tasks every time, no memory of past projects, the spec as the only source
of truth. Sixteen live runs of the SAME p6 case reward per-case tuning and
prove nothing about universality. This stage puts a BATTERY of small generated
specs (different domains, different STRUCTURE: route counts, method sets,
required request fields, media mixes, module ownership, scenario counts,
service AND library product kinds) next to the hand-written cases. The
generator (`tests/lib/battery_gen.py`) is deterministic engine-side code
(seed -> byte-identical set, stdlib only, no LLM); each output is a case YAML
the EXISTING conveyor consumes unchanged (run_cases.py glob + run-detached.sh
launcher — the battery is data plus a thin driver, never a second harness).
A failure of ANY battery spec in a live sweep is a ratchet investigation:
honest red, never softened, never patched per-case.
Ratchet evidence (RED before code): `test_universality_battery.py` was
committed against a repo without `tests/lib/battery_gen.py` and without any
committed `bat*.yaml` / `battery-manifest.json` under tests/scenarios/,
PROVEN RED at pytest collection (ModuleNotFoundError: battery_gen) — before
the implementation commit turned it green.
- S20.1 DETERMINISM: `generate(seed, count)` twice -> byte-identical file
  set; seed+1 -> a different set (the seed is live, not decorative).
- S20.2 COMMITTED SET == GENERATOR: the battery checked into tests/scenarios/
  byte-equals a regeneration from the committed manifest's seed/count (a
  hand-edited battery spec is DRIFT and reds here); >= 6 specs so p6 stops
  being the only yardstick; no stray `bat*.yaml` beside the manifest.
- S20.3 VALIDITY, SMALL, IR-SURFACE: every spec parses with the same loader
  run_cases uses and carries the required case keys (name == file stem,
  boot pre_gate on, non-empty acceptance); services declare 2-5 routes with
  methods/media inside the IR schema surface (`spec_ir._OA_METHODS` /
  `spec_ir._MIME`) and LITERAL paths (no templates); libs declare 2-4
  capabilities; <= 3 modules. Manifest dims are BOUND to the spec text
  ("METHOD /path" appears verbatim) — no parallel truth.
- S20.4 HONESTY (no leakage): no `seed_files`/`blueprint`/`solution`-class
  keys, no code fences, no function-body fragments, no injections block —
  the spec states desired behaviour ONLY; the worker derives everything.
  Both directions: the linter (`battery_gen.lint_case_text`) is silent on
  the legitimate battery AND reds on tampered known-answer inputs. Selector
  safety: every battery stem starts with `bat` and no other scenario stem
  contains it, so `--case bat` selects exactly the battery.
- S20.5 VARIETY: the default battery spans the structural dimensions — >= 2
  distinct route counts, >= 2 method sets, json-only AND json+html media
  mixes, >= 2 required-field counts, >= 2 module counts, >= 2 scenario
  counts, both product kinds, >= 6 non-repeating domains. Breadth is the
  point; renamed nouns over one shape do not pass.
- S20.6 EXISTING LAUNCHER, NO MEMORY: the manifest's recorded live command
  goes through tests/run-detached.sh (`--case bat --depth product --workers
  real --decomposer llm`); fresh workspace per spec is the conveyor's own
  behaviour (run_cases mints a fresh run dir + fresh HERMES_HOME per case);
  no battery spec smuggles cross-run project memory (memory.project.mode,
  when present, is `fresh`).

## STAGE 21 — Closed response body shape (`test_closed_response_schemas.py`)

Node H3 (plan 2026-07-04T00-45), principles-audit finding F1. `compile_openapi`
emitted `{"schema": {}}` for a media-only success response and
`compile_leaf_tests` asserted only `isinstance(body, dict)` — a weak model
could return invented fields and stay GREEN. The empty schema READ like a
closed "any object" contract while it was an unrecorded GAP. Stage 21 closes
the response body the way Stage 13/16 closed the request body: declared shape
is pinned, undeclared shape is a VISIBLE gap.

- S21.1 A success response whose IR entry carries a CLOSED object schema
  (`type: object` + `properties`/`required`/`additionalProperties: false`)
  compiles to asserts that the required fields are PRESENT, each declared
  field type holds (`isinstance(body[f], <pytype>)`), and — when
  additionalProperties is false — `set(body)` carries no field outside the
  declared union. Field types map JSON→Python once (string→str, integer→int,
  number→(int,float), boolean→bool, object→dict, array→list); an unknown type
  is skipped, never guessed.
- S21.2 The F1 exploit is dead BY CONSTRUCTION, proven behaviourally: a
  handler returning the contracted fields plus an invented one fails the
  compiled test in-process; the honest handler passes.
- S21.3 A media-only response with NO recorded body shape stays an honest
  `isinstance` check AND is NAMED in the compiled document's
  `x-spec-flow-gaps` ("... response <status> body shape not recorded") — the
  engine invents no fields but the silence is made visible (S13.1 discipline
  carried to the response body; mirrors the media gap).
- S21.4 A `const` fixed body keeps its exact `== body` assert — the S18
  contract is not weakened by the new shape path (GREEN direction).

## STAGE 22 — Decomposer emits OpenAPI as the PRIMARY node interface,
library-validated (`test_decomposer_emits_openapi.py`)

Node K2 of the spec-IR rearchitecture (plan 2026-07-04T00-45; user
superpriority 2026-07-06 — "use the OpenAPI library; the machine document is
the primary interface carrier, prose .md a derived fallback"). E1 (Stage 15)
made the decomposer emit a MACHINE part — per-node `openapi` fragments (a real
OpenAPI 3.1 document each) — validated at `_accept_decomposer_ir` with
`spec_ir.validate_ir`, the engine's HAND-ROLLED closed-world check. K1
(Stage 16.7) added `spec_openapi.validate_openapi_library` — a maintained
third-party OpenAPI 3.1 oracle — but only over the COMPILED product document,
never over the per-node fragment at the decomposer seam. So a node whose
OpenAPI document was invalid by the STANDARD (a wrong-typed `schema`, a dangling
local `$ref`, a list where the 3.1 spec wants an object) passed the seam
SILENTLY — validate_ir had nothing to say and the library never ran. That is
the v165 lost-route class one layer up: an interface fact accepted as truth
without ever being library-checked.
Ratchet evidence (RED before code): `test_decomposer_emits_openapi.py` was
committed against the pre-K2 engine and PROVEN RED — 1 failed, 5 passed: a node
with a library-invalid OpenAPI document was accepted by `_accept_decomposer_ir`
with ZERO errors (`assert errs` -> `assert []`) — before the seam library-check
turned it green.
- S22.1 A NODE'S OpenAPI DOCUMENT IS LIBRARY-VALIDATED AT THE SEAM: when
  `spec_ir.validate_ir` is satisfied, `_accept_decomposer_ir` additionally runs
  EVERY proposed node's `openapi` document through the third-party
  openapi-spec-validator (`spec_openapi.node_openapi_library_errors`); a
  standard violation is a NAMED refusal (P4 — the error string names the node),
  a MILESTONE FAIL on its OWN gate `decomposer_openapi` (the `decomposer_ir`
  precedent), and the fragment NEVER enters the `_decomposer_ir_nodes` registry
  — a later consumer can never read a document that failed the standard oracle.
  Known-answer RED: a response `schema` that is a string, not a JSON Schema
  object, slips past validate_ir (premise pinned by
  `test_library_invalid_doc_slips_past_hand_rolled_validate_ir`) and is caught
  only here.
- S22.2 NO FALSE REFUSAL, ORACLE-OPTIONAL: a standard-valid node document is
  accepted with zero errors, no `decomposer_openapi` FAIL, and the
  `decomposer_ir` PASS still journals (GREEN direction). The library is a
  dev/test oracle (tests/requirements-dev.txt) imported lazily; when it is
  ABSENT `node_openapi_library_errors` returns `[]` (probed via
  `openapi_library_available`) — the seam skips the extra check, never a false
  refusal, and the engine keeps no hard runtime dependency.
- S22.3 THE ROUTE LIVES IN THE MACHINE DOCUMENT FROM THE FIRST STEP (the v165
  class impossible): an accepted node's route is present in its library-valid
  `openapi` document at acceptance, and `_leaf_owned_routes` binds ownership
  from that document (`interface_source: ir`) — never a prose guess deferred to
  assembly. A route can no longer travel as prose only to be dropped later.
- S22.4 PROSE-AS-CARRIER IS A REFUSAL UNDER ir-required (H8/S15.8 connective):
  a node whose interface was prose-derived (no accepted machine OpenAPI
  document) surfaces as a FAILING product check naming the node under
  `interface_policy: ir-required`; under `allow-prose` the historical fallback
  runs clean (GREEN direction) — the policy connective is symmetric, K2 keeps
  it honest.

## STAGE 23 — specs/*.md is COMPILED FROM the machine OpenAPI, prose is not a
source (`test_prose_is_derived.py`)

Node K3 of the spec-IR rearchitecture (plan 2026-07-04T00-45), the finale of
the format flip. E1/K2 (Stages 15/22) made the machine OpenAPI the PRIMARY node
interface — it lives at `ir[nodes][nid][openapi]`. But the human-readable
`specs/<nid>.md` still carried the interface only as decomposer PROSE
(`node["spec_markdown"]`): `Workspace.spec` rendered the engine header plus the
worker's free-text, and NEVER read the node's OpenAPI document. So the written
spec was a CARRIER of the interface command, not a reader of it — the exact v165
lost-route shape (`GET /ping -> "pong"` reached the .md as a sentence, drifted
from the machine artifacts, and `get_ping` was dropped).
The flip: the interface section of the .md is now COMPILED DETERMINISTICALLY
FROM the node's OpenAPI document by `_openapi_interface_markdown` in the engine
— same document always renders the same section; the prose becomes a downstream
reader whose edits cannot change what the interface says.
Ratchet evidence (RED before code): `test_prose_is_derived.py` was proven RED on
the pre-K3 engine — 2 failed, 1 passed. With the OpenAPI-to-markdown compiler
call removed from `Workspace.spec`, the .md carried NO structured `GET /ping`
entry (S23.1) and the interface facts changed with the prose (S23.2); only the
no-OpenAPI case (S23.3) was already green.
- S23.1 THE .md RENDERS THE INTERFACE FROM THE MACHINE OpenAPI: a node carrying
  an `openapi` document (built exactly as `build_ir` builds it, via
  `spec_ir._node_openapi` over the pure route helpers) writes each owned
  `METHOD /path` AND its declared `x-spec-flow-handler` symbol into the .md,
  read straight from the document — the node carried NO prose interface at all.
- S23.2 THE INTERFACE SECTION IS A FUNCTION OF THE OpenAPI ONLY: the same
  OpenAPI document rendered under two DIFFERENT decomposer proses yields a
  byte-identical structured interface section (`**METHOD /path**` entries plus
  their handler/status/field detail lines). Editing the prose cannot move the
  interface — prose is derived, the build reads the document. The detector
  matches STRUCTURED entries only, so a sentence that merely names a route in
  prose (a legitimate GREEN mention) is never mistaken for the interface.
- S23.3 NO OpenAPI -> NO PHANTOM SECTION (historical output preserved): a node
  with no `openapi` document (a non-service/library leaf) grows no interface
  section — the compiler stays silent when the node owns no routes, so the .md
  of a pure helper is unchanged.
## STAGE 24 — Narrow OpenAPI interface diff built ON the compiled machine document (node K4, `test_openapi_diff_compiled.py`)
- S24.1 THE DIFF'S INTERFACE IS THE COMPILER'S DOCUMENT, NOT A PRIVATE WALK:
  `tests/harness/openapi_diff.py` no longer re-derives the endpoint/field
  interface with its own `paths -> method -> responses.200.content...properties`
  dict walk — a second copy of the extraction `spec_openapi.compile_openapi`
  already performs merging node fragments. The raw contract fragment is wrapped
  in a minimal one-node IR (`{"nodes": {"contract": {"openapi": fragment}}}`),
  compiled, and the interface is read off the resulting machine document. One
  source, one truth; the diff inherits router-status injection, `x-spec-flow-node`
  ownership and duplicate-route refusal for free instead of being blind to them.
- S24.2 A COMPILER-NAMED GAP IS DRIFT, NEVER A FALSE GREEN: a success response
  with NO media, or an empty `{}` schema, is a hollow contract the compiler
  NAMES in `x-spec-flow-gaps` (media gap / body-shape gap). The old hand walk
  read both as "endpoint present, zero fields" and returned `[]`/exit 0, so ANY
  implementation passed a shapeless contract. The rebuilt diff carries each
  named gap as a `contract_gap` drift record with nonzero exit. Ratchet
  evidence: RED committed against the pre-K4 hand walk (media-gap and empty-schema
  contracts both returned `[]`) — turns GREEN only once the interface derives
  from the compiled document.
- S24.3 REAL FIELD DRIFT IS UNWEAKENED: for a fully-recorded contract the
  compiled-document diff keeps exact `missing_endpoint` / `missing_field` /
  `type_mismatch` semantics (BOTH-DIRECTIONS: exact match compiles clean,
  each drift kind surfaces). Real fixtures (billing/orders/cart/query/
  url_shortener) record their fields honestly and raise ZERO `contract_gap` —
  the new record fires only on genuinely hollow contracts.
- S24 NOTE — LIBRARY vs HAND-ROLL DECISION: no third-party OpenAPI *diff*
  library was pulled. `openapi-spec-validator` / `openapi-schema-validator`
  (already pinned) validate a document but do not compute an interface diff
  against an implementation manifest; and the interface the engine cares about
  is exactly what `compile_openapi` already produces. The remaining field-level
  comparison is a tiny in-house walk over the COMPILED document — kept in-house
  because the domain object it diffs (the machine OpenAPI + `x-spec-flow-gaps`)
  is spec-flow's own, and a generic library would not know the gap channel.
  This is duplication REMOVED (interface extraction now single-sourced), not a
  new hand-rolled seam (contrast S19.10, where the reason to hand-roll is the
  model-facing block format the library lacks).
## STAGE 25 — ONE in-memory IR accumulator; datum projections are DERIVED from it (node I2, `test_ir_single_accumulator.py`)
- S25a THE ENGINE HOLDS ONE IR STRUCTURE: after a write under the I1 lock
  (`_write_ir_locked`) the engine keeps `self._ir` (and the raw-datum snapshot
  `self._ir_sources` it was built from) in memory. `build_ir`'s result is the
  single source retained across readers, not a throwaway rebuilt on every call.
- S25b INTERFACE.JSON IS DERIVED FROM THE ACCUMULATOR, NOT RE-ASSEMBLED:
  `_write_interface_contract` reads its route media / request_fields projection
  from `self._ir_sources`, no longer re-calling `_route_media_map()` /
  `_route_request_fields()` on its own. Direction proof: after the accumulator
  is built the RAW datum is mutated (monkeypatched); a re-derived interface
  would pick the mutation up (two directions, the pairwise-drift class), a
  derived-from-accumulator interface ignores it (one direction: datums ->
  accumulator -> {IR, interface}, the datum never read a second time behind it).
- S25c THE HELD IR IS A DUMP, NEVER A LIVE REBUILD: `_ir_snapshot()` returns the
  same held structure after a raw-datum mutation — two reads under changing
  datums cannot drift. The datum is upstream of the accumulator, never queried
  behind it. Consolidation REMOVED (media/request_fields single-sourced), not a
  new seam: the raw datums are captured ONCE by `spec_ir.collect_ir_sources`.

## STAGE 26 — the HOT PATH reads the held IR accumulator, never a fresh rebuild of it (node I3, `test_ir_hotpath_reads_accumulator.py`)
  Closes the "переворот": node I2 (S25) made `self._ir` the single accumulator
  and `interface.json` its projection, but three skeleton hot-path readers
  still called `spec_ir.build_ir(self)` with NO `sources` argument —
  `_ir_skeleton_for` (write-door skeleton per leaf), the late-route
  `binds_route` refresh feeding `_refresh_ir_skeletons`, and the IR-compiled
  conformance test writer's engine fragment. Each re-ran `collect_ir_sources`
  and read every raw datum (`_route_owners` / `_route_media_map` /
  `_route_request_fields` / env / pins) a SECOND time BEHIND the accumulator,
  so an update to `self._ir` was invisible to them: the pairwise-drift class
  the accumulator exists to remove. A single `_held_ir()` helper now serves
  every hot read from the held structure (building + holding once under the I1
  lock only if no locked dump has run yet); the datum methods stay strictly
  upstream of the accumulator.
- S26a THE SKELETON READER COMPILES FROM THE HELD IR: an env access point
  injected into the held node's IR entry appears in the compiled skeleton — a
  rebuild from raw datums (which never saw the injection) would drop it.
  Behavioural, not textual: mutate `self._ir`, assert the hot read reflects it.
- S26b THE HELD `nodes` OBJECT IS USED AS-IS, NOT RE-DERIVED: the object
  `compile_skeleton` receives IS `self._ir["nodes"]` (identity) — two
  independent assemblers cannot drift when there is only one.
- S26c TEXT GATE (secondary): no hot-path reader carries a `build_ir(self)`
  CALL with no sources snapshot; the only sanctioned rebuild is the write door
  (`collect_ir_sources` -> `build_ir(self, sources)` under the I1 lock). A bare
  mention in prose is not an offender — the gate matches the call as an
  expression head, so the ratchet does not fossilize the docstring wording.

## STAGE 27 — the contract_check MILESTONE carries the per-record drift as structured detail (node M2, `test_contract_drift_milestone_detail.py`)
  Closes the last hole after K4 (S24): the diff validator `openapi_diff.py`
  already PRINTS a JSON list of drift records (`contract_gap` /
  `duplicate_route` / `missing_endpoint` / `missing_field` / `type_mismatch`,
  each naming its route/field), but the engine, when it emitted the
  `contract_check` MILESTONE event on drift, dropped that list: only
  `res["drift"][0]["detail"]` — the RAW stdout of the FIRST validator — reached
  the event's free-text `detail`, and records from any second validator were
  lost entirely. The live dashboard renders milestones generically off
  `level` / `verdict` / structured fields, so per-file contract drift (which
  route drifted, which field, gap vs duplicate vs type) never surfaced as data.
  M2 adds `Event.details` (a structured list, default empty, serialized by
  `asdict` into the jsonl trace) and `Engine._contract_drift_records`, which
  flattens EVERY validator's drift records into one list; all four
  `contract_check` milestone emissions (subtree-parallel, drift-vs-frozen,
  after-respec, after-code-fix) attach it. The dashboard then shows per-file
  contract drift with no dashboard code change.
- S27.1 THE DRIFT MILESTONE CARRIES THE PARSED RECORD LIST: a real drift run
  (privacy-analytics with the real `openapi_diff` wired in) produces a
  `contract_check` drift event whose `details` is a non-empty list of record
  dicts, not an opaque string.
- S27.2 EACH RECORD NAMES ITS KIND AND ITS LOCATION: every attached record
  carries a known drift `kind` and, where the validator provides one, the
  route (`endpoint`) or `field` it drifted on — the columns the dashboard
  renders per file.
- Precondition guard: the audit first asserts a drift episode actually occurred,
  so a green result is earned by real drift, never by its absence.

## STAGE 28 — Dashboard surfaces the FULL IR accumulator + OpenAPI-first spec
(node M1, `tests/dashboard/test_dashboard_ir.py`)
The dashboard is the human window onto the engine's data; if a datum the IR
accumulator carries is invisible there, a design/wiring hole hides from review.
Two obligations, both RED-first:
- S28.1 EVERY IR datum per node is rendered STRUCTURALLY (not only inside the
  folded raw-json dump): routes (method/path/status/media/handler + origin
  badge, H2 `x-spec-flow-status-source`), exposes/consumes symbols with
  signatures, env, dependencies, effects, Given/When/Then scenarios (given.env
  + given.state prior steps, when, then status/media/body_check), owned files
  and child node ids. A `_structural()` slice (HTML minus the raw dump) proves
  the block is a real render, never a hit inside the dump. A thin node (routes
  only) emits NO empty scenario/files/children block — the IR's own "missing
  datum => absent field" law mirrored on the dashboard. ir.json is re-read
  fresh (no cache) so incremental accumulator states stay visible (I2/I3).
- S28.2 OPENAPI-FIRST provenance is stated at a glance: a node whose IR carries
  an OpenAPI document with paths marks the machine document
  'primary: machine OpenAPI 3.1' and the specs/*.md prose
  'derived / compiled from OpenAPI' (K3/S23). The badge is engine data
  (`_node_spec_provenance_html`), threaded into per-node state as `spec_primary`
  and rendered by the client spec tab above the (derived) markdown. A node with
  no interface document claims no provenance (empty badge). GREEN direction: a
  non-service leaf's spec panel carries no false primary/derived claim.

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
