# Operator dashboard

- **Node:** `dashboard`  ·  **Level:** L1  ·  **Decision:** `leaf`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** within all thresholds
- **Project acceptance target:** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

## Size estimate (leaf_check input)

| metric | value |
|---|---|
| modules | 1 |
| tasks | 4 |
| interfaces | 1 |
| estimated_loc | 95 |
| open_decisions | 0 |
| single_concern | True |
| testable_criteria | True |

## Plan
- bottom-up plan: DB → logic → API → tests
- TDD: test (RED) → impl → test (GREEN)
- two-stage review (spec-conformance, then quality)
- verification-before-completion + commit
