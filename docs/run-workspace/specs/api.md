# Query API

- **Level:** L1  ·  **Decision:** `branch`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** modules 2 > 1; tasks 7 > 5; estimated_loc 320 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves

## Plan
- child: GET /query
- child: GET /export
