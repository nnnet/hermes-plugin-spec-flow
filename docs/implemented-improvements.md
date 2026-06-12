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
