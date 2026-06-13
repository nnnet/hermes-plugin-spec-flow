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
