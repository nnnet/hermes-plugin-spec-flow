# Consent & anonymisation (L1 policy)

- **Node:** `consent`  ·  **Level:** L1  ·  **Decision:** `branch`
- **Traces-to:** Privacy analytics service
- **leaf_check reason:** modules 2 > 1; estimated_loc 150 > 100 (not one commit); 1 open decision(s) — resolve before leafing; coupled step (multiple concerns) — split into single-concern leaves
- **Project acceptance target:** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

## Size estimate (leaf_check input)

| metric | value |
|---|---|
| modules | 2 |
| tasks | 4 |
| interfaces | 1 |
| estimated_loc | 150 |
| open_decisions | 1 |
| single_concern | False |
| testable_criteria | True |

## Resolved open decision (clarify loop)
- **Question:** Cookieless hashing vs signed opt-in token?
- **Resolution:** Cookieless rotating daily salt

## Plan
- child: Consent banner widget
- child: Event anonymiser (daily salt)

## Children (next level)
- `consent_banner` — Consent banner widget
- `anonymiser` — Event anonymiser (daily salt)
