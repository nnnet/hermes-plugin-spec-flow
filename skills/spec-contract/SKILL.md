---
name: spec-contract
description: >
  Use on an L2 (technical design) node. Freeze the interface as a real,
  machine-checkable contract artifact BEFORE any implementation task exists:
  OpenAPI by default, or Zod / Protobuf / DB schema, or several in parallel for
  assurance. Every operation carries x-traces-to:[REQ-n]. The contract is the
  rails the code is later validated against.
---

# spec-contract

You turn an interface decision into a **frozen artifact** — the "rails" of SDD.
Code is later checked against this, not against prose.

## Steps

1. **Pick the contract type.** Default **OpenAPI** (`openapi.yaml`). Switch to
   Zod (TypeScript) or Protobuf (`.proto`) when the domain demands, or produce
   **several in parallel** for extra assurance.
2. **Write the contract** into the `dir:` workspace. Every operation / schema
   field carries `x-traces-to: [REQ-n]` for machine traceability back to L1.
3. **Pin the artifact** in `metadata.contract_path` (and `contract_types`) so
   `contract_check` and `spec-integrate` find it.
4. **Freeze through the gate.** Run `spec-reviewer` (spec-gate) on the contract
   **before** a single implementation task is created. The contract is frozen
   only after PASS.

## Validation

`contract_check(contract_artifacts=[...], types=[...])` runs the validators
(default OpenAPI; parallel when several types). Any drift, or in strict mode an
unavailable validator, fails — never a silent pass. Configure validator
binaries in `CONTRACT_VALIDATORS` at the top of the plugin's
`spec_flow_tools.py` (redocly/specmatic for OpenAPI, tsc for Zod, buf for
Protobuf).

A leaf under this contract may only be created after this node is `done`.
