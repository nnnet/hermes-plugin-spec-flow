# Time-series storage

- **Level:** L1  ·  **Decision:** `branch`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** modules 2 > 1; tasks 6 > 5; estimated_loc 240 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves

## Plan
- child: Rollup schema + migrations
- child: Retention + purge job
