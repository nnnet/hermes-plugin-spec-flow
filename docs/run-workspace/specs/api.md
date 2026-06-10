# Query API

- **Node:** `api`  ·  **Level:** L1  ·  **Decision:** `branch`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** modules 2 > 1; tasks 7 > 5; estimated_loc 320 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves
- **Project acceptance target:** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

## Size estimate (leaf_check input)

| metric | value |
|---|---|
| modules | 2 |
| tasks | 7 |
| interfaces | 2 |
| estimated_loc | 320 |
| open_decisions | 0 |
| single_concern | False |
| testable_criteria | True |

## Frozen L2 contract
- `contracts/query.openapi.yaml` (x-traces-to: `api`)

## Plan
- child: GET /query
- child: GET /export

## Children (next level)
- `ep_query` — GET /query
- `ep_export` — GET /export
