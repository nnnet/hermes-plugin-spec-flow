---
name: drift-gate
description: >
  Use when contract_check reports code↔contract drift during implementation.
  Forbids silently editing code to mask drift. Classifies: (A) code is wrong →
  fix code to the contract; (B) contract is wrong → escalate to respec-gate
  (fix the contract/spec first, re-gate, then restart implementation).
---

# drift-gate

A drift between code and the frozen L2 contract is never resolved by quietly
changing code. You **classify and route**.

## On drift (from `contract_check`)

1. **Read the structured drift** — field type, signature, endpoint path.
2. **Classify:**
   - **(A) Code is wrong** (contract is correct): fix the code to match the
     contract, re-run `contract_check`, continue only when clean.
   - **(B) Contract is wrong** (code reflects a real, better decision): do NOT
     edit code to fit a wrong contract. `kanban_block(reason="DRIFT: <field X
     String≠Int>")`, create an `update-contract` task whose parent is the L2
     contract node, and hand to `respec-gate`. Change `openapi.yaml` / `spec.md`
     first, re-run spec-gate, and only after that `done` does implementation
     unblock and **restart** against the corrected contract.

The repair direction is the SDD invariant: **spec-first** — never "code under a
wrong spec".
