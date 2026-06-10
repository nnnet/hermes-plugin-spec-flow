# Time-series storage

- **Node:** `storage`  ·  **Level:** L1  ·  **Decision:** `branch`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** modules 2 > 1; tasks 6 > 5; estimated_loc 240 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves
- **Project acceptance target:** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

## Size estimate (leaf_check input)

| metric | value |
|---|---|
| modules | 2 |
| tasks | 6 |
| interfaces | 1 |
| estimated_loc | 240 |
| open_decisions | 0 |
| single_concern | False |
| testable_criteria | True |

## Plan
- child: Rollup schema + migrations
- child: Retention + purge job

## Children (next level)
- `schema` — Rollup schema + migrations
- `retention` — Retention + purge job
