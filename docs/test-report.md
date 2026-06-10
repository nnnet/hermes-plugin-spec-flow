# spec-flow — отчёт боевого тестирования

> Тулзов зарегистрировано: 6 (contract_check, leaf_check, research_trigger_check, specflow_init, specflow_start, specflow_status)
> Проектов проверено: 4 · итог: ✅ всё совпало с ожиданием


Каждый узел проекта классифицирован **настоящим** `leaf_check`. Дерево ниже — фактический отклик плагина; ✓ = совпало с ожиданием.


## cli-todo — ✅ OK
_A local command-line todo app (storage + CLI)._

```
CLI todo app [🌿 branch] ✓
├─ JSON storage layer [🍃 leaf] ✓
└─ argparse CLI commands [🍃 leaf] ✓
```
Узлов: 3 · листьев(impl): 2 · веток(integrate): 1 · контрактов: 0 · ревью: 2 · глубина: 1
Сверка с ожидаемым DAG: leaves 2/2 ✓ · integrates 1/1 ✓ · contracts 0/0 ✓ · max_depth 1/1 ✓

## data-pipeline — ✅ OK
_ETL pipeline: ingest -> transform -> load._

```
ETL pipeline [🌿 branch] ✓
├─ Source ingestion [🍃 leaf] ✓
├─ Transform stage (schema undecided) [🌿 branch] ✓
│  ├─ Parse + validate [🍃 leaf] ✓
│  └─ Enrich + normalize [🍃 leaf] ✓
└─ Sink load [🍃 leaf] ✓
```
Узлов: 6 · листьев(impl): 4 · веток(integrate): 2 · контрактов: 0 · ревью: 4 · глубина: 2
Сверка с ожидаемым DAG: leaves 4/4 ✓ · integrates 2/2 ✓ · contracts 0/0 ✓ · max_depth 2/2 ✓

## ecommerce-checkout — ✅ OK
_Checkout subsystem: cart + payment + a coupled checkout form._

```
Checkout subsystem [🌿 branch] ✓
├─ Cart service [🌿 branch ⟨contract⟩] ✓
│  ├─ POST /cart/items [🍃 leaf] ✓
│  └─ DELETE /cart/items/{id} [🍃 leaf] ✓
├─ Payment service [🌿 branch ⟨contract⟩] ✓
│  ├─ POST /payments/charge [🍃 leaf] ✓
│  └─ POST /payments/refund [🍃 leaf] ✓
└─ Checkout form (coupled UI+API) [🌿 branch] ✓
   ├─ Checkout form UI [🍃 leaf] ✓
   └─ Checkout submit endpoint [🍃 leaf] ✓
```
Узлов: 10 · листьев(impl): 6 · веток(integrate): 4 · контрактов: 2 · ревью: 6 · глубина: 2
Сверка с ожидаемым DAG: leaves 6/6 ✓ · integrates 4/4 ✓ · contracts 2/2 ✓ · max_depth 2/2 ✓

## url-shortener — ✅ OK
_A URL shortener service (DB + REST API + web UI)._

```
URL shortener [🌿 branch] ✓
├─ Persistence (codes table) [🍃 leaf] ✓
├─ REST API [🌿 branch ⟨contract⟩] ✓
│  ├─ POST /shorten [🍃 leaf] ✓
│  └─ GET /{code} [🍃 leaf] ✓
└─ Minimal web form [🍃 leaf] ✓
```
Узлов: 6 · листьев(impl): 4 · веток(integrate): 2 · контрактов: 1 · ревью: 4 · глубина: 2
Сверка с ожидаемым DAG: leaves 4/4 ✓ · integrates 2/2 ✓ · contracts 1/1 ✓ · max_depth 2/2 ✓


## Contract drift (real openapi_diff validator)

| Scenario | status | drift |
|---|---|---|
| clean implementation | ✅ ok | 0 |
| type mismatch (id: int->str) | ⚠️ drift | 1 |
| missing endpoint | ⚠️ drift | 1 |
| subtree parallel (2 contracts) | ⚠️ drift | 1 |

## Research lane timeline (data-pipeline)

| event | completed | errors | fired? | by |
|---|---|---|---|---|
| tick | 3 | 0 | — | - |
| on_level_return | 4 | 0 | 🔬 yes | on_level_return |
| tick | 6 | 0 | — | - |
| tick | 10 | 12 | 🔬 yes | m_test_errors>=10 |
| tick | 12 | 12 | — | - |
| tick | 31 | 12 | 🔬 yes | every_n_tasks>=20 |
| cron | 31 | 12 | — | - |
