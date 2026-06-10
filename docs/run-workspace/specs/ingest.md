# Event ingestion

- **Level:** L1  ·  **Decision:** `leaf`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** within all thresholds

## Plan
- bottom-up plan: DB → logic → API → tests
- TDD: test (RED) → impl → test (GREEN)
- two-stage review (spec-conformance, then quality)
- verification-before-completion + commit
