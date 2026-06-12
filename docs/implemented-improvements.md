# Implemented improvements — what changed and why

Status: shipped (tests green, `tests/` 402 passed). Each section: the
mechanism, why it exists, where it lives. Newest first.

## 1. Late requirements materialize deterministically (mid-run HITL)

**Problem.** An operator adds a requirement ("the product needs a web UI")
*after* the tree is half-built. Plain notes died 4 times in a row: atomic
nodes consumed them and nothing changed. Then the binding prompt block was
added — and live runs proved **both** branch decomposers that received it
ignored it (diffusion of responsibility: every branch assumes another one
covers it). Lesson recorded: **a prompt is not a gate.**

**Mechanism (4 layers, each deterministic except the first):**

1. *Requirement as artifact.* `hitl/requirements/<name>/` holds
   `REQUIREMENT.md` + real acceptance tests. The verifier syncs the tests
   into `workspace/tests/smoke/acceptance_<name>_*.py` at every integrate
   and marks them platform-protected — the ROOT physically cannot pass
   until the requirement is satisfied (`harness/hitl.py
   sync_requirements`, `harness/pytest_verifier.py`).
2. *Standing requirements in branch prompts.* Every branch decomposer gets
   the full list as a binding block (`harness/role_worker.py`). Advisory —
   kept because it sometimes works and costs nothing.
3. *Deterministic attach.* After a branch decomposer replies, every
   requirement not yet covered by any tree node is attached as a child
   leaf by the ENGINE — idempotent via the tree registry, allowed to
   exceed `MAX_CHILDREN`. The attached node's own decompose pass receives
   the FULL requirement statement, so its spec is authored from the source
   text, not a truncated title. Logged as `requirement_attached`.
4. *Addressed notes.* A note prefixed `@branch` is only consumed by a
   branch-capable node — atomic leaves can no longer eat instructions
   meant for decomposition (`harness/hitl.py poll_note`).

**Why this shape.** Generation is unreliable; discipline must come from
deterministic gates. Layer 1 guarantees the *outcome* (root gate), layer 3
guarantees the *work item exists* (tree node), layers 2/4 merely improve
the odds the LLM does it gracefully.

## 2. Two-tier leaf bar — the culpable leaf pays, not the branch

**Problem.** A leaf lands green on its own tests while silently breaking a
sibling module; the damage surfaces ticks later at branch integrate, where
blame is diffuse and repair is expensive.

**Mechanism.** Before a leaf is accepted, two checks
(`harness/role_worker.py _leaf_bar`): its own tests pass AND the whole
non-smoke suite is not worse than the baseline snapshot taken before the
leaf wrote anything. Degradation → a repair round with the failing output
quoted; still degraded → the leaf is rejected red.

**Why.** Shifts detection from the integrate gate (wrong place, late) to
the exact leaf that caused it (right place, immediate).

## 3. Real pytest as the integrate verdict + bounded self-repair

**Mechanism.** `harness/pytest_verifier.py`: the integrate gate runs the
actual suite; FAIL starts up to `SPEC_FLOW_INTEGRATE_MAX_REPAIR` repair
rounds. Each round snapshots the workspace, computes a badness score
(collection break = +100), and ROLLS BACK any repair that made things
worse. Protected files (platform + synced acceptance) are read-only
context with the hint "no route = author a new module"; repair may create
files, never rewrite the platform.

**Why.** An LLM opinion never substitutes for a green run; and an
unsupervised "fixer" that can make things worse needs an undo.

## 4. Repository map in every prompt that plans or writes code

**Mechanism.** `harness/repo_map.py` builds an AST digest (routes,
schemas, signatures) of `src/`; decomposers, implementers and repair all
receive it.

**Why.** Dedup ("never plan a node that duplicates an existing route") and
contract-awareness are only enforceable if the worker can SEE the existing
surface.

## 5. One door to the LLM + budget discipline

**Mechanism.** `harness/llm_backend.py ask()` is the single entry point.
Free-gate: models without `:free` are rejected unless
`SPEC_FLOW_ALLOW_PAID=1`. Daily OpenRouter free quota exhausted →
automatic fallback to haiku (direct, bypassing the gateway) with a 600 s
cooldown before retrying free. `last_call` records who actually answered.

**Why.** Test runs must never burn paid tokens silently; the fallback
keeps long runs alive overnight without human attention.

## 6. Platform seed + constitution merge for ambiguous cases (p4)

**Mechanism.** A case YAML may ship `seed_files` (deterministic skeleton:
registry, sqlite plumbing, WSGI dispatcher that passes HTML strings
through as `text/html`) and `constitution_platform` (platform rules merged
into the case constitution at load).

**Why.** Mechanical context beats wishful prompting: workers build INTO a
frozen skeleton instead of each inventing their own app shape — the
single biggest source of integrate-time chaos in early runs.

## 7. Shared project state flows into EVERY node (dedup safeguards)

**Mechanism (predates this doc; pinned here so later work never drops
it).** Each decomposer call receives the project's shared state:
the full node registry (`existing_nodes`: id + title of every node
already created anywhere in the tree), the ancestor chain, the
repository map (AST digest of `src/`), and the frozen contract context.
On top of that the ENGINE runs `_dedup_children`: proposed children that
re-create existing work (near-identical titles/scope) are pruned before
they become tasks.

**Why.** Without the registry every branch reinvents its siblings'
endpoints; without pruning the tree grows duplicate leaves that later
collide at integrate. (The parallel-children tests tripped this
safeguard with look-alike titles — proof it bites.)

**Parallel-mode caveat (deliberate).** Parallel siblings see the
registry as of fork time and register their nodes under a lock as they
land; the dedup gate between concurrent siblings is therefore weaker —
one reason parallelism is opt-in and bounded (see 13).

## 8. Late requirements: ENGINE-owned placement (root / scoped / fallback)

Branch prompts proved obedient in the wrong way: a branch swallowed the
cross-cutting web_ui requirement into its own subtree. Placement now
belongs to the engine: unscoped requirement → direct ROOT child (its
acceptance runs on the assembled product); `@scope: <branch>` → child of
that branch while it is still open; missed scope → root fallback; a name
already covered → no injection. Workers get awareness text only.
Tests: tests/test_requirement_injection.py (levels × arrival stages).

## 9. Deterministic spec lint + minimal-edit rework

The reviewer repeated 'AC-5 lacks REQ-5' through a whole rework budget
while rework re-authored the spec from scratch each round. Now: (a) the
mechanical traceability rule (every AC↔REQ pair) is checked by CODE
before any reviewer round (`spec_lint` gate); (b) every rework carries
the PREVIOUS spec verbatim with a minimal-edit instruction. LLM rounds
are spent on meaning, never on what code can state precisely.

## 10. Integrate-fail policy (record | rework | halt)

What a FAILed branch integrate does next is the case's choice:
`on_integrate_fail: record` (book the debt, carry on — the default),
`rework` (re-invoke the verifier with a fresh repair budget,
`integrate_max_rework` rounds), `halt` (stop the run). p4 runs `rework`
since v13 to measure the efficiency effect.

## 11. Providers/models as case parameters; failures walk the chain

The case YAML `workers:` block is the single source of truth: a
`providers` registry (lists with parameters: prefix, required `:free`
suffix, daily quota) and per-role ordered model chains. ANY provider
failure — daily quota, the per-minute 429 ceiling (it killed a live run
once), a dead endpoint — walks to the next chain entry; only config
errors abort. Budget: `workers.budget` caps total calls per run.

## 12. Run journal (waves) + memory providers — landed, not yet wired

`spec_flow_journal.RunJournal`: append-only JSONL, ONE writer (OS lock),
atomic fsynced waves, torn-tail cut on recovery. The substrate for
parallelization stages 2+. `harness/memory.py`: role/project banks,
Hindsight adapter (Hermes stack, 127.0.0.1:8888) verified live;
failures never kill a run. Both wait for engine wiring behind flags.

## 13. Parallel children (stage 1) — opt-in, bounded, criteria as parameters

A branch may run children's subtrees in a thread pool. Smart-enable
criteria are CASE PARAMETERS, not hardcode:
`parallel: {children: N, min_siblings: M, depth_limit: D,
max_workers: W}` — pool size, the don't-bother floor, where forking is
allowed (nested forks multiply concurrency), and a global subtree
ceiling. `workers.concurrency` caps in-flight LLM calls (the free pool
is ~8 req/min — unbounded parallel calls trade speed for a 429 storm).

**Safeguards pinned by tests/test_parallel_children.py — MUST survive
all future work:** sequential default without the flag; single-writer
trace (strictly increasing unique ticks); branch integrate joins ALL
children; the budget counter stays exact under threads; one pytest at a
time per workspace; an agent crash in one child spares the others;
dedup pruning still bites.
