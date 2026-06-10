# spec-flow — отчёт полного прогона проекта

**Проект:** privacy-analytics — _Self-hosted, consent-based web analytics (no PII, GDPR-compliant)_
**Цель (измеримая):** ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.

> Все скиллы задействованы: ✅ · все профили задействованы: ✅ · проект завершён: ✅ (L0 integrate done)

Источник отчёта — событийный поток прогона (`RunResult.events`), сгенерированный из `tests/runs/privacy_analytics.yaml` и возвратов **настоящих** тулзов плагина в точках решений. Сырой поток целиком выгружается в `docs/full-run-trace.jsonl`.

**Детализация:** показаны события уровня ≤ 2 (63 из 74). Уровни: 1=вехи (вердикты гейтов, циклы), 2=шаги, 3=детали (TDD, правила конституции). Управление: `SPEC_FLOW_RUN_VERBOSITY` (рендер) и `SPEC_FLOW_RUN_LOG` / `SPEC_FLOW_RUN_LOG_LEVEL` / `SPEC_FLOW_RUN_LOG_FORMAT` (лог на диск).

## Дерево задач (с версиями и повторными прогонами ↻)
```
Privacy analytics service
├─ Consent & anonymisation (L1 policy) v2 ↻2 ⟨clarify⟩
│  ├─ Consent banner widget
│  └─ Event anonymiser (daily salt)
├─ Event ingestion ⟨spike⟩
├─ Time-series storage
│  ├─ Rollup schema + migrations
│  └─ Retention + purge job
├─ Query API ⟨contract⟩
│  ├─ GET /query ⟨drift→respec⟩ ⟨review↻⟩
│  └─ GET /export
└─ Operator dashboard
```

## Журнал исполнения
```
TICK │ ACTOR · SKILL · [TASK] action → result   (verbosity=2)
── requirements ──
t 1 │ 🧩 spec-decomposer · spec-requirements · [L0:req] policy_gate on the goal → pass  «ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.»
t 5 │ 🧩 spec-decomposer · spec-requirements · [L0:req] EARS requirements frozen  «ingest >= 1000 events/s; p95 query < 200ms; 0 PII fields in storage.»
── decompose ──
t 6 │ 🧩 spec-decomposer · spec-flow-decompose · [L0] read parent handoff, write level spec (Traces-to)  «Privacy analytics service»
t 7 │ 🧩 spec-decomposer · spec-flow-decompose · [L0] leaf_check → branch  «modules 5 > 1; tasks 20 > 5; interfaces 3 > 2; estimated_loc 3000 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves»
t 8 │ 🧩 spec-decomposer · spec-flow-decompose · [consent] read parent handoff, write level spec (Traces-to)  «Consent & anonymisation (L1 policy)»
t 9 │ 🧩 spec-decomposer · spec-flow-decompose · [consent] kanban_block — open decision  «Cookieless hashing vs signed opt-in token?»
t10 │ ⚖️ spec-reviewer · spec-reviewer · [consent] clarify answered → unblock  «Cookieless rotating daily salt»
t11 │ 🧩 spec-decomposer · spec-flow-decompose · [consent] leaf_check → branch  «modules 2 > 1; estimated_loc 150 > 100 (not one commit); 1 open decision(s) — resolve before leafing; coupled step (multiple concerns) — split into single-concern leaves»
t12 │ 🧩 spec-decomposer · spec-flow-decompose · [consent_banner] read parent handoff, write level spec (Traces-to)  «Consent banner widget»
t13 │ 🧩 spec-decomposer · spec-flow-decompose · [consent_banner] leaf_check → leaf  «within all thresholds»
── implement ──
t14 │ 🛠️ implementer · spec-implement · [consent_banner:impl] design → bottom-up plan (DB→logic→API→tests)  «Consent banner widget»
── review ──
t16 │ ⚖️ spec-reviewer · spec-reviewer · [consent_banner:review] impl-review → quality gate → PASS
── decompose ──
t17 │ 🧩 spec-decomposer · spec-flow-decompose · [anonymiser] read parent handoff, write level spec (Traces-to)  «Event anonymiser (daily salt)»
t18 │ 🧩 spec-decomposer · spec-flow-decompose · [anonymiser] leaf_check → leaf  «within all thresholds»
── implement ──
t19 │ 🛠️ implementer · spec-implement · [anonymiser:impl] design → bottom-up plan (DB→logic→API→tests)  «Event anonymiser (daily salt)»
── review ──
t21 │ ⚖️ spec-reviewer · spec-reviewer · [anonymiser:review] impl-review → quality gate → PASS
── integrate ──
t22 │ ✅ verifier · spec-integrate · [consent:integrate] end-to-end acceptance criteria → PASS  «verification-before-completion»
── decompose ──
t23 │ 🧩 spec-decomposer · spec-flow-decompose · [ingest] read parent handoff, write level spec (Traces-to)  «Event ingestion»
── research ──
t24 │ 🔬 researcher · spec-research · [ingest:spike] SPIKE before freeze  «Batch vs streaming ingestion for 1k ev/s?»
t25 │ 🔬 researcher · spec-research · [ingest:spike] recommendation folded into spec (above the gate, no rework)  «Streaming with bounded backpressure queue»
── decompose ──
t26 │ 🧩 spec-decomposer · spec-flow-decompose · [ingest] leaf_check → leaf  «within all thresholds»
── implement ──
t27 │ 🛠️ implementer · spec-implement · [ingest:impl] design → bottom-up plan (DB→logic→API→tests)  «Event ingestion»
── review ──
t29 │ ⚖️ spec-reviewer · spec-reviewer · [ingest:review] impl-review → quality gate → PASS
── decompose ──
t30 │ 🧩 spec-decomposer · spec-flow-decompose · [storage] read parent handoff, write level spec (Traces-to)  «Time-series storage»
t31 │ 🧩 spec-decomposer · spec-flow-decompose · [storage] leaf_check → branch  «modules 2 > 1; tasks 6 > 5; estimated_loc 240 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves»
t32 │ 🧩 spec-decomposer · spec-flow-decompose · [schema] read parent handoff, write level spec (Traces-to)  «Rollup schema + migrations»
t33 │ 🧩 spec-decomposer · spec-flow-decompose · [schema] leaf_check → leaf  «within all thresholds»
── implement ──
t34 │ 🛠️ implementer · spec-implement · [schema:impl] design → bottom-up plan (DB→logic→API→tests)  «Rollup schema + migrations»
── review ──
t36 │ ⚖️ spec-reviewer · spec-reviewer · [schema:review] impl-review → quality gate → PASS
── decompose ──
t37 │ 🧩 spec-decomposer · spec-flow-decompose · [retention] read parent handoff, write level spec (Traces-to)  «Retention + purge job»
t38 │ 🧩 spec-decomposer · spec-flow-decompose · [retention] leaf_check → leaf  «within all thresholds»
── implement ──
t39 │ 🛠️ implementer · spec-implement · [retention:impl] design → bottom-up plan (DB→logic→API→tests)  «Retention + purge job»
── review ──
t41 │ ⚖️ spec-reviewer · spec-reviewer · [retention:review] impl-review → quality gate → PASS
── integrate ──
t42 │ ✅ verifier · spec-integrate · [storage:integrate] end-to-end acceptance criteria → PASS  «verification-before-completion»
── decompose ──
t43 │ 🧩 spec-decomposer · spec-flow-decompose · [api] read parent handoff, write level spec (Traces-to)  «Query API»
t44 │ 🧩 spec-decomposer · spec-flow-decompose · [api] leaf_check → branch  «modules 2 > 1; tasks 7 > 5; estimated_loc 320 > 100 (not one commit); coupled step (multiple concerns) — split into single-concern leaves»
── contract ──
t45 │ 📐 spec-contract · spec-contract · [api:contract] freeze OpenAPI contract (x-traces-to)  «query.openapi.yaml»
t46 │ ⚖️ spec-reviewer · spec-reviewer · [api:contract] spec-gate on contract → PASS  «trace + constitution OK»
── decompose ──
t47 │ 🧩 spec-decomposer · spec-flow-decompose · [ep_query] read parent handoff, write level spec (Traces-to)  «GET /query»
t48 │ 🧩 spec-decomposer · spec-flow-decompose · [ep_query] leaf_check → leaf  «within all thresholds»
── implement ──
t49 │ 🛠️ implementer · spec-implement · [ep_query:impl] design → bottom-up plan (DB→logic→API→tests)  «GET /query»
t51 │ 🛠️ implementer · spec-implement · [ep_query:impl] contract_check vs frozen L2 → drift  «[{"kind": "type_mismatch", "endpoint": "GET /query", "field": "count", "contract": "integer", "code": "string"}]»
── drift ──
t52 │ 🛠️ implementer · drift-gate · [ep_query:impl] drift-gate classify  «contract_wrong»
── respec ──
t53 │ ⚖️ spec-reviewer · respec-gate · [query.openapi.yaml] spec-first: update contract node, version-bump, re-gate, restart impl  «query.openapi.yaml → query_fixed.openapi.yaml»
── implement ──
t54 │ 🛠️ implementer · spec-implement · [ep_query:impl] contract_check after respec → ok  «matches corrected contract»
── review ──
t55 │ ⚖️ spec-reviewer · spec-reviewer · [ep_query:review] impl-review (spec-conformance) → FAIL  «FAIL: missing edge-case handling on error path»
t56 │ 🛠️ implementer · spec-implement · [ep_query:impl] fix per critique → unblock → re-run
t57 │ ⚖️ spec-reviewer · spec-reviewer · [ep_query:review] impl-review → quality gate → PASS
── decompose ──
t58 │ 🧩 spec-decomposer · spec-flow-decompose · [ep_export] read parent handoff, write level spec (Traces-to)  «GET /export»
t59 │ 🧩 spec-decomposer · spec-flow-decompose · [ep_export] leaf_check → leaf  «within all thresholds»
── implement ──
t60 │ 🛠️ implementer · spec-implement · [ep_export:impl] design → bottom-up plan (DB→logic→API→tests)  «GET /export»
── review ──
t62 │ ⚖️ spec-reviewer · spec-reviewer · [ep_export:review] impl-review → quality gate → PASS
── integrate ──
t63 │ ✅ verifier · spec-integrate · [api:integrate] parallel contract_check across subtree → ok  «query_fixed.openapi.yaml»
t64 │ ✅ verifier · spec-integrate · [api:integrate] end-to-end acceptance criteria → PASS  «verification-before-completion»
── decompose ──
t65 │ 🧩 spec-decomposer · spec-flow-decompose · [dashboard] read parent handoff, write level spec (Traces-to)  «Operator dashboard»
t66 │ 🧩 spec-decomposer · spec-flow-decompose · [dashboard] leaf_check → leaf  «within all thresholds»
── implement ──
t67 │ 🛠️ implementer · spec-implement · [dashboard:impl] design → bottom-up plan (DB→logic→API→tests)  «Operator dashboard»
── review ──
t69 │ ⚖️ spec-reviewer · spec-reviewer · [dashboard:review] impl-review → quality gate → PASS
── integrate ──
t70 │ ✅ verifier · spec-integrate · [L0:integrate] end-to-end acceptance criteria → PASS  «verification-before-completion»
── revision ──
t71 │ 🔬 researcher · spec-research · [revision] research_trigger_check → trigger  «fired_by=['on_level_return', 'every_n_tasks>=20']»
t72 │ 🔬 researcher · spec-research · [revision] REVISION finding (upstream impact)  «New ePrivacy guidance: implied consent insufficient, explicit per-purpose opt-in required.»
── respec ──
t73 │ ⚖️ spec-reviewer · respec-gate · [consent] respec-gate: change the cause first, version-bump, re-derive only affected subtree  «Version-bump consent spec (supersedes v1), reopen its subtree, re-derive only affected leaves.»
── integrate ──
t74 │ ✅ verifier · spec-integrate · [L0:integrate] L0 integrate done = project COMPLETE  «all subtrees merged & verified»
```

## Покрытие
- **Скиллы (9/9):** drift-gate, respec-gate, spec-contract, spec-flow-decompose, spec-implement, spec-integrate, spec-requirements, spec-research, spec-reviewer
- **Профили (6/6):** implementer, researcher, spec-contract, spec-decomposer, spec-reviewer, verifier
- **Задач на доске:** 35
- **Вызовы тулзов плагина:** policy_gate×1, leaf_check×12, contract_check×3, research_trigger_check×1

## Циклы / уточнения / критика (где «крутилось»)
| Тип | Где | Что |
|---|---|---|
| 🟡 clarify/block | `consent` | Cookieless hashing vs signed opt-in token? |
| 📐 дрейф контракта → drift-gate → respec | `ep_query:impl` | [{"kind": "type_mismatch", "endpoint": "GET /query", "field": "count", "contract": "integer", "code": "string"}] |
| ⚖️ критика ревью (FAIL→fix→re-run) | `ep_query:review` | spec-conformance critique |
| 🔬 ревизия research → respec-gate | `consent` | New ePrivacy guidance: implied consent insufficient, explicit per-purpose opt-in required. |

