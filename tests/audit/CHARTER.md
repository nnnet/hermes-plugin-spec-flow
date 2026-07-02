# Audit Charter — the principles the engine is revised against

This charter is LAYER 3 of the audit: a periodic REVISION layer above the
per-stage checks in `TAXONOMY.md`. Layers 1–2 (static hygiene, honesty
invariants, routing matrices, termination caps, dynamic simulation, capability
liveness, assembly-seam honesty) each guard ONE concrete failure shape. This
document states the DESIGN PRINCIPLES those shapes are instances of, so that:

- `revision.py` can review the engine source against the principles with an
  LLM (map-reduce over `spec_flow_runner.py`) and surface violations no
  existing stage was written for yet;
- `test_property_scenarios.py` can probe the principles with seeded,
  generated scenarios (metamorphic properties) instead of hand-picked cases;
- every NEW live failure is first classified under a principle here, then
  codified as a stage rule in `TAXONOMY.md` (the ratchet: discover once,
  catch cheaply forever).

A principle entry has four parts: the statement, a real historical violation,
the audit stage that guards it today, and — where the guard is partial — an
explicit GAP note. A GAP is a debt, not a footnote: it names the next check
to write.

---

## P1 — Single declared source

**Statement.** Every value that TWO independent artifacts must agree on
(status codes, symbol names, module owners, route sets, entry callables) must
exist as ONE engine-declared datum that both sides read. Re-deriving a shared
value in two places is a collision deferred to assembly time.

**Violation (v149).** The leaf card pinned no success status for
`POST /notes`: the coder guessed 201, the tester guessed 200. Both guesses
were locally plausible; they collided only when the assembled suite ran —
hours after either agent could have been corrected.

**Guarded by.** S10.1 (`_route_success_status` is the single function; the
binding prints it for the coder, the leaf test-status gate enforces it on the
tester) and the machine interface contract `contracts/interface.json`
(S10 engine note) dumped from the same functions every consumer reads.

---

## P2 — Liveness proof for every declared capability

**Statement.** A capability the engine declares (parallelism, resume, doctor
remedies, memory tiers) must be PROVEN reachable in its representative
scenario by a dynamic offline test. "The code exists" is not evidence: the
dangerous bug is a trigger condition that can never be met in exactly the
runs that need the capability.

**Violation (v148).** Parallel development was configured
(`parallel: {children: N}`) yet unreachable three ways at once: the fork
decision was taken ONCE against the initial tree shape (small products
collapse to one leaf — nothing to fork), late-injected requirements dripped
through a re-poll window with no parallel path, and a static `depth_limit`
starved forks when a leaf recomposed into a branch online. Every node built
serially while the config claimed parallelism.

**Guarded by.** STAGE 9 (S9.1 base fan-out, S9.2 late-injection accumulation,
S9.3 online recomposition) with a structural thread-overlap witness.

**GAP.** S9.4 (cross-level ordering under wave partitioning) and S9.5
(liveness proofs for `--resume`, doctor remedies, worktree isolation, memory
tiers) are open — declared capabilities currently without a proof.

---

## P3 — Complete replacement (structure + prose + data)

**Statement.** When the engine replaces one plan with another (a collapse, a
recomposition, an amend), the replacement must be COMPLETE across all three
carriers: the STRUCTURE (task tree), the PROSE (specs/cards), and the DATA
(contracts, bindings, acceptance). A replacement that rewrites the tree but
inherits the old prose or stale data smuggles the replaced design back in
through a channel the gates were not reading.

**Violation (v150).** The small-product floor collapsed the tree to ONE
atomic leaf, but the leaf's card (`core.md`) still ORDERED the multi-module
architecture — `src/db.py` plus `src/app.py` — inherited from the
pre-collapse plan. A mini-architecture smuggled past the graph inside prose;
the v149 phantom import was ordered by exactly that inherited text.

**Guarded by.** S10.5 (card gate reds on a leaf spec that plans src files no
node owns — atomicity enforced on the TEXT, not only on metrics). The DATA
carrier is probed by the collapse-invariance property in
`test_property_scenarios.py` (a collapsed and an uncollapsed run must declare
the same interface contract).

---

## P4 — Attributable failure

**Statement.** Every failure must be attributable: an exit code, a cause, and
an owner (which node / which gate / which external force). A death that
leaves no record is an observability hole the ratchet cannot learn from —
the run cost is paid but the lesson is lost.

**Violation (v148).** The detached run was killed by an external SIGKILL and
left ZERO evidence: no exit code recorded, no "killed by signal" line, no
attributable record at all. The postmortem had to be reconstructed from
absence.

**Guarded by.** S6.8 (the detached launcher records the python exit code and
decodes rc>=128 as "killed by signal S"). Cause→remedy attribution inside a
run is STAGE 5 (every doctor CAUSE maps to a dispatchable REMEDY; every
lifecycle state has an outgoing transition).

**GAP.** Owner attribution for mid-run gate failures is uneven: some gates
emit a node id and gate name, others only prose. No stage yet asserts "every
FAIL event carries (node, gate, cause)".

---

## P5 — Prose is data

**Statement.** Prose artifacts (cards, specs, handoffs) are DATA subject to
gates, not commentary. Agents execute what the prose orders; therefore text
that contradicts the graph or the contract is a live defect, exactly as a
wrong constant would be. Gating metrics while trusting prose is auditing the
label and shipping the bottle.

**Violation (v149/v150).** The phantom `from db import …` was not invented by
the tester at random — it was ORDERED by the leaf's inherited prose (v150's
`core.md` planning `src/db.py`). Structure said "one module"; prose said
"two"; the agents obeyed the prose.

**Guarded by.** S10.5 (the card gate reads the TEXT: a leaf spec planning
unowned src files reds at the card, before any agent runs).

---

## P6 — Sterile oracle

**Statement.** The suite that judges the product must see ONLY the product:
imports resolve inside the workspace (host `site-packages`/`.pth` decoys stay
invisible), network is fenced to loopback unless the project opts out, and
paths outside the workspace boundary are a gate failure. A verdict produced
with help from the host environment is not a verdict about the product.

**Violation (v149).** A tester-invented `db` module was satisfied by a host
editable-install `.pth` pointing at an UNRELATED repository's code. The suite
imported someone else's `db`, went green on it, and judged the wrong product.

**Guarded by.** S10.3 (hermetic verification: engine-planted oracle-isolation
conftest; a decoy visible through PYTHONPATH/.pth must stay invisible —
dynamic subprocess proof) and the engine's single sterile-oracle boundary
(`_prepare_hermetic_suite`: hermetic imports, loopback network fence,
assembly import repair, workspace-boundary path gate, interface contract).

---

## P7 — Honest conjunction

**Statement.** The terminal verdict is a logical AND over every gate: READY
⟺ (all leaves green) ∧ (all declared addresses served) ∧ (assembled suite
green) ∧ (root integrate green). No summary, report or dashboard may present
a weaker aggregate than the conjunction — a single RED anywhere makes every
"READY" claim upstream of it a lie.

**Violation (v144).** `meta.status = READY` was shipped while the root
integrate gate had recorded FAIL — the summary read a different (weaker)
signal than the gate ledger. (Same class: v119's green narrow acceptance
over a RED corpus.)

**Violation (report layer, open).** The harness summary parser derives the
verdict by substring: `"READY" if "✅" in head and "READY" in head` — but
"NOT READY" CONTAINS "READY", so a NOT-READY report whose first 400 chars
carry any green checkmark is summarised as READY
(`tests/lib/run_cases.py`, `product_verdict`). The engine tells the truth;
the summary can still lie.

**Guarded by.** S3.1–S3.3 (READY is a conjunction; a base-contract smoke
cannot lift a red leaf; every declared address must be served), plus the
`complete`-verdict guard in the case summary (root RED can never present ✅).

**GAP.** No audit test covers the report/summary parsers themselves — the
substring verdict above is exactly the hole. The terminal-verdict property in
`test_property_scenarios.py` asserts the engine end; the report layer still
owes a check.

---

## P8 — Finite caps everywhere

**Statement.** Every loop the engine can enter has a finite, positive,
declared cap: per-node rework, decompose calls, integrate rework, leaf
deadlines, amend edits, standing-requirement re-polls, and the global
run-call budget. "It should converge" is not a cap. A run must reach a
terminal verdict — READY or an honest NOT READY — in bounded agent calls no
matter what the agents return.

**Violation (v146).** A late amend re-entered the FULL leaf lifecycle
(decompose→spec→implement) instead of a bounded targeted edit, and the
re-poll re-materialised the same requirement unboundedly: 46 minutes of
churn, no terminal, no progress — the infinite-churn shape.

**Guarded by.** STAGE 6 (S6.1–S6.7: every named cap finite; the global
run-call budget halts a runaway with an explicit FAIL milestone) and STAGE 8
(S8.1–S8.3: dynamic simulation under a hard call ceiling, including an
adversarial agent that never satisfies its gate). The generated-scenario
terminal property in `test_property_scenarios.py` extends S8 from
hand-picked shapes to seeded random ones.

---

## Convention (inherited from TAXONOMY.md, restated for layer 3)

- A check is HONEST: it reds on a real hole and is never softened to pass.
- Fixes are real engine capabilities, never per-case crutches.
- A probe that exposes a real engine defect stays in the suite as
  `xfail(strict=True)` with the defect class named — it is a finding, not a
  flaky test to delete.
- `run-detached.sh` will not launch while `pytest tests/audit` is red.
