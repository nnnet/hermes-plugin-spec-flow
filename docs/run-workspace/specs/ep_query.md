# GET /query

- **Node:** `ep_query`  ·  **Level:** L2  ·  **Decision:** `leaf`
- **Traces-to:** Query API
- **leaf_check reason:** within all thresholds
- **Project acceptance target:** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

## Size estimate (leaf_check input)

| metric | value |
|---|---|
| modules | 1 |
| tasks | 4 |
| interfaces | 1 |
| estimated_loc | 90 |
| open_decisions | 0 |
| single_concern | True |
| testable_criteria | True |

## Contract-drift episode
- classification: **contract_wrong** (contract_wrong → respec the L2 first; code_wrong → fix the code)

## Review history
- impl-review failed 1× before PASS (critique loop)

## Plan
- bottom-up plan: DB → logic → API → tests
- TDD: test (RED) → impl → test (GREEN)
- two-stage review (spec-conformance, then quality)
- verification-before-completion + commit
