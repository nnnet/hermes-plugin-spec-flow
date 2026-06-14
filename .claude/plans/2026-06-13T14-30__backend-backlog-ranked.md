# Backend backlog — ranked (2026-06-13)

Sort key, in order: **quality first → cost → time** (per user directive).
Risk and effort break ties: a low-risk concrete win beats a speculative one.

Grounding: the ⏱ idle analysis on the live p4 run showed the wall-clock
split — **spec review ~47%, integration ~27%**, quota waits ~0. So the time
is in review + integration, NOT throttling. Quality levers attack rework
and regressions; cost/time levers attack the integration tail.

## Ranked backlog

### 1. П3 — diff-based repair instead of whole-file rewrite  ⟵ START
**Quality + cost.** Today a failed integrate makes the repair worker
REWRITE whole files; the p5 retro recorded a case where this clobbered
working code and still didn't fix the red. Switch repair to emit
search/replace diff blocks with an applicability check — a non-applicable
diff is refused WITHOUT a write. Quality: surgical fix, fewer regressions.
Cost: regenerate a hunk, not a file. Time: repair is a measured sink.
Risk: medium (repair path). Effort: bounded.

### 2. Topological leaf ordering (dependencies first)
**Quality + cost + time.** Build a leaf's prerequisites before the leaf, so
an integrate sees its dependencies already present → fewer rework rounds,
higher first-pass rate. Dependency signal: spec cross-references / imports.
Risk: medium (ordering in the scheduler). Effort: medium.

### 3. Flaky-test quarantine
**Quality.** A non-deterministic acceptance test must not redden the root.
On a red test, re-run it K times in isolation; if the verdict is
inconsistent, QUARANTINE it (record + exclude from the gate verdict, never
silently). Honest root. Pairs with П4-bis poisoner bisection. Risk: must
not mask real bugs — quarantine only on proven non-determinism.

### 4. Incremental integrate test set (subtree at branch, full at root)
**Cost + time, quality-neutral.** A branch integrate runs only its
subtree's tests; the ROOT integrate still runs the whole corpus (the honest
full check stays). Removes the growing cost of every non-root gate (the 27%
sink). Risk: LOW (root unchanged). Effort: bounded.

### 5. Parallel integration of independent branches — DONE (measure-only)
**Already delivered by #28.** Measured on v023: peak 3 concurrent integrates,
5 overlapping branch-integrate pairs (e.g. payout_approval ∩
payout_calculation = 66.5s). Parallel sibling subtrees (#28) already run
independent branches' integrations concurrently. The remaining
serialization is STRUCTURAL — a parent integrate must wait for its children,
and the root is single — not removable without breaking dependency
semantics. A separate concurrency mechanism would duplicate #28 at high
risk. Closed without new code.

### 6. Token / cost accounting per role+model
**Observability → cost.** Record real token counts (not just call counts)
per role+model; surface in compare + the ⏱ tab. Turns "calls" into real
economics so cost decisions are grounded. Risk: low. Effort: low-medium.

### 7. Spec cache / incremental re-run (skip unchanged subtrees by hash)
**Cost.** Hash each node's spec; on re-run, an unchanged subtree reuses its
committed artifact instead of re-implementing. Builds on П1 resume + П2
journal. Risk: low-medium. Effort: medium.

### 8. П8 — spike research on a strong model for hard nodes
**Quality on hard nodes.** A node with many open decisions / high LOC gets
one research "spike" (best free model or the haiku subscription) before
implementing. Free-only rule holds. Risk: low. Effort: medium.

### 9. П2 — auto-derive contract from smoke
**Quality.** Parse the smoke test (AST: calls + asserts) → generate the
contract block; constitution references the generated one. Kills manual
contract drift. Risk: low. Effort: medium.

### 10. П13 — worker specialization (role × model × specialty)
**Routing quality.** Route domain-specific nodes to a better-fit model.
Risk: low. Effort: medium. (Speculative without measurement.)

### 11. П10 — multiple smoke scenarios
**Coverage.** Acceptance beyond p4/p5. Risk: low. Effort: medium.

### 12. Pluggable board backend beads/redis
**Distribution.** П15 left ClaimStore pluggable; add beads/redis for a
shared board across distributed runners. Low immediate value (single-host
today). Risk: low. Effort: medium.

## Convergence hardening (emerged from the live p4+web cycle, #21)

Each item is a real root-cause fix found by running p4 to completion and
diagnosing why the assembled corpus stayed RED. All shipped (tests + push).

- **CV1 — portable DB layer in the seed skeleton.** Features describe tables
  with `db.define_table(name, columns, indexes=, foreign_keys=)` and use
  `insert/select/update/delete/execute`; a swappable adapter generates dialect
  SQL. Cured the cascade where a worker's `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS` (illegal in SQLite) aborted the shared `connect()` and reddened
  127/160 tests. Column spec carries `not null`/`unique`/`primary key`;
  indexes + FKs are portable too.
- **CV2 — cascade root-cause detector in the verifier.** Clusters failures by
  exception signature (weighting real exceptions over assert symptoms); when
  one cause dominates, tells repair it is ONE shared bug and surfaces the
  schema-registering modules. Handles class-based & reason-less pytest output.
- **CV3 — duplicate `define_table` owner detector (deterministic).** Two/three
  worker modules defining the same table is pervasive and kills convergence
  (registry keeps the last → other modules lose columns → green alone, red
  together). Verifier names the conflict + owning modules for repair;
  constitution mandates one owner per table. No LLM needed.

## Execution
Top-down, each = tests + commit + push, separate commits. #1–#10 landed;
CV1–CV3 landed from the live cycle. Remaining: #11 (multiple smoke
scenarios) and #12 (pluggable beads/redis board) — both low immediate value
versus convergence, deferred until a p4 corpus goes fully green. Re-measure
via the ⏱ idle tab + compare_runs.

---

## Update 2026-06-14 — status audit + speed/quality/orchestra roadmap

### What is NOT done / changed
- **#11 multiple smoke scenarios** — still open. Partially superseded: a new
  minimal measurement case `p6_micro_notes` now exists (see below).
- **#12 persistent claim/board store (beads/redis) for STOP + RESUME** — still
  open. Real driver is NOT multi-host but durability: a run must be stoppable
  and later resumable with its claim/board state intact (which leaf is done,
  owned, in-rework). Today that state is in-process and lost on stop; #35
  `--resume` replays from the workspace but the claim ledger is volatile.
  *Goal:* persist the board so STOP → RESUME continues exactly where it paused.
- **CV1 (portable DB in the seed) + CV3 (constitution one-owner rule) — REVERTED.**
  The user ruled seed skeletons and pre-solving constitution rules to be test
  **scaffolding** (they hand the weak model the answer to its own failure
  classes). `seed_files` + `constitution_platform` were deleted from p4/p5 and
  are no longer consumed by `run_cases`. The deterministic **detectors** stay
  and are the honest replacement: **CV2 cascade**, **CV3 dup-owner**, plus a
  new **cross-module export-contract** detector (`from X import Y` where the
  local module never binds Y — the real residual class after de-scaffolding).
- **No-hardcode config refactor — DONE** (separate plan): all config defaults
  left *.py; floor in `tests/.test.env`, per-case override in the `workers:`
  block; `live-run.env.sh` deleted. Cases are self-contained.

### Grounding — where the wall-clock goes (latest ⏱ split)
spec review **47%** (49 ops) ≫ lifecycle **19%** (25) > integration **15%**
(8 tests) > repair **14%** (14) > human **5%** (1); decompose/implement LLM
≈ 0%. So **review is the dominant sink**, repair+integration the second front,
LLM generation is NOT the bottleneck. Levers must attack review and rework.

### Measurement harness — `p6_micro_notes` (new, this update)
Minimal case that exercises the FULL pipeline in least time: fsm + LLM-built
tree (no blueprint) + real code build + pytest at execute depth + a mid-run
HITL web_ui injection + a delivered server-rendered UI. Tiny 2-endpoint goal
⇒ small tree ⇒ few reviews; depth/width not hard-capped. Nothing seeded;
`acceptance:` is the declarative oracle. web_ui injected from
`/tmp/claude/web_ui_requirement_p6/`. This is the before/after ruler for every
lever below — compare on calls, sec_per_node, first_pass_rate,
errors_per_node, and review-op count.

### Roadmap by goal (ranked: quality → speed → cost)
Checkbox `- [ ]` = to do, `- [x]` = done.

**A. Speed — attack review (47%) + lifecycle (19%)**
- [ ] **A1 Review tiering — different DEPTH of check per node.**
  *Simple node → light check:* DETERMINISTIC only — spec-lint (format +
  traceability) + the contract/import detectors. NO LLM reviewer call, NO
  rework loop; clean ⇒ auto-pass. *Complex node → full review:* the LLM
  reviewer reads the spec, judges quality, may REJECT → rework loop (up to
  max_rework). So the saving is: most nodes skip the expensive model review and
  its rework rounds entirely.
  *Simple* = atomic leaf, `open_decisions == 0`, small `estimated_loc` (≈ <60),
  no children, at/over leaf_depth. *Complex* = a branch (has children), OR any
  open decisions, OR large LOC, OR shallow depth (architectural node whose
  mistakes cascade). *How:* complexity gate on the node's existing metrics in
  the reviewer dispatch. Target: review ops −2–3×.
- [ ] **A2 Parallel sibling review.** Review independent sibling specs
  concurrently (review is serial today). *How:* reuse `_run_child_pool`.
- [ ] **A3 Escalate-not-grind.** After N rejects, escalate the node to a
  stronger model ONCE instead of looping max_rework rounds at the same model.
- [ ] **A4 Lifecycle trim.** Batch commits per branch, drop no-op transitions
  (19%, 25 bookkeeping ops).

**B. Fewer errors — attack repair (14%) + first-pass rate**
- [ ] **B1 Pre-integrate contract gate.** Run the deterministic detectors
  (cross-module import, dup-owner, non-ASCII) BEFORE the expensive integrate,
  not after a red corpus — fail fast, cheaper repair.
- [ ] **B2 Measure the creator-ensemble first-pass lift on p6** (ensemble itself
  shipped).
- [ ] **B3 Measure topological ordering (#2)** rework reduction on p6.
- [ ] **B4 Measure diff-repair (#1)** regression reduction on p6.

**C. Role specialization — by EXECUTOR (single agent OR a team), not by model**
The executor for a node is chosen by the node's task. It can be a single
domain-specialist **agent** (db / python / web / api …), each with its own
prompt/skill/tools — OR, for a harder node, a whole **team/orchestra** of
agents (see D). Either way the choice is driven by the node domain, not by
swapping the LLM model (П13 was only the thin "role×model" version).
- [ ] **C1 Executor roster + task→executor routing.** Define specialist
  executors per domain (each = an agent, or a team); for each leaf, infer the
  domain and dispatch the matching executor. *How:* extend the П13 resolver
  from "role×model" to "role×executor" where an executor is an agent profile or
  a team spec; `resolve_specialty` already infers the domain. Measure on p6
  (web_ui leaf → the web executor).
- [ ] **C2 Specialist reviewer** — the reviewer for a node is the executor
  expert in that node's domain, not a generic one.

**D. Replace a single-agent role with an orchestra — a TEAM of heterogeneous
agents in a mini-workflow (NOT a vote of identical models)**
Today a role = one model call. Make a role = a small team of specialised
agents collaborating per node.
- [ ] **D1 Implementer orchestra.** Per leaf: architect (defines the module
  interface) → coder (writes) → tester (writes + runs tests) → fixer (repairs).
  Each is its own specialised agent. The creator ensemble becomes the "coder".
- [ ] **D2 Decomposer orchestra.** One agent drafts the tree, a second
  critiques/improves, a third reconciles → a better tree (a bad tree is the
  costliest downstream mistake).
- [ ] **D3 Declarative team + workflow config** in `workers:`: declare the
  roster AND how the agents interact — the sub-workflow (order, handoffs,
  branch/loop/parallel, what each sees from the others). e.g. architect → coder
  → tester; tester loops to fixer on red; fixer back to tester. Default = a team
  of one, one step = today's single agent (existing cases unchanged). *How:* a
  small workflow schema (nodes = agents, edges = handoffs) run per role. This is
  also the engine behind C (a node's executor may be such a team).

### Open from the old backlog
- [ ] **#11 multiple E2E scenarios** — run the engine on 3–4 different products
  (not just p4) to catch engine bugs invisible on one case.
- [ ] **#12 persistent board for STOP → RESUME** — make the claim/board ledger
  durable so a stopped run resumes exactly where it paused (see audit above).

### Done in this cycle
- [x] No-hardcode config refactor (.test.env floor + case `workers:` override,
  `live-run.env.sh` removed).
- [x] De-scaffolding: `seed_files` + `constitution_platform` removed from
  cases and no longer consumed; honest measurement restored.
- [x] Cross-module export-contract detector + CV2 cascade + CV3 dup-owner
  detectors in the verifier.
- [x] `p6_micro_notes` measurement case + web_ui injection template.

### Execution order
p6 (the ruler) → D1 → A1 → B1 → C1 → A2/A3/A4 → D2. Each = baseline p6 run,
change, p6 re-run, compare (calls, sec_per_node, first_pass_rate,
errors_per_node, review-op count). A quality regression (false accept) vetoes a
speed win.
