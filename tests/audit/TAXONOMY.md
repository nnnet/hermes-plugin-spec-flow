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
`test_rework_preserves_route_surface.py`, `test_unserved_route_fastfail.py`)
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
