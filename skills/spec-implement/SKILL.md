---
name: spec-implement
description: >
  Use on a spec-flow LEAF task. Runs the atomic implementation pipeline:
  design → bottom-up plan (DB → logic → API → tests) → TDD (red/green) →
  two-stage review → contract_check (drift) → verification-before-completion.
  Orchestrates the bundled Hermes skills (writing-plans, test-driven-
  development, subagent-driven-development, requesting-code-review).
---

# spec-implement

You implement **one leaf** against its approved spec and frozen L2 contract.

## Pipeline

1. **Design** the leaf from the parent handoff spec; do not exceed scope.
2. **Plan bottom-up** (`writing-plans`): order tasks **DB → logic → API →
   tests**, bite-sized (2-5 min each), with exact file paths, copy-paste code
   and verification commands.
3. **TDD per task** (`test-driven-development`): write test → run (expect FAIL)
   → minimal impl → run (expect PASS). Tests precede code.
4. **Two-stage review** (`spec-reviewer` impl-review via
   `subagent-driven-development`): spec-conformance first, then code quality.
   On divergence: fix and re-run the spec-review; continue only when it
   conforms.
5. **Drift gate (step 5b):** `contract_check(contract_artifacts=[<contract_path
   from parent metadata>], changed_files=[...], types=[...])`. On drift, do NOT
   silently edit code — route to `drift-gate` (classify A: code wrong vs B:
   contract wrong).
6. **Pre-commit gate** (`requesting-code-review`) when the task changed 2+
   files, before commit/push.
7. **Verify & complete** (`verification-before-completion`): run fresh
   verification commands; commit frequently.

Close with `kanban_complete(summary, metadata={changed_files, verification,
contract_path, residual_risk})`.

## Bug handling mid-leaf

If you hit a bug during implementation, report to the parent (block/comment) —
the parent decides: re-run the task, adjust the plan, or delegate a fix. Don't
silently expand scope.
