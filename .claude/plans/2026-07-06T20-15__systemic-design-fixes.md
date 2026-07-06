# Системные ошибки проектирования spec-flow — каталог, корень, готовые решения

## Context
Разбор прогона v167 вскрыл не баги, а СИСТЕМНЫЕ ошибки проектирования. 4
параллельных архитектурных среза (источники↔потребители, глобальная связность,
fail-open, внешние либы) сошлись к ОДНОЙ мета-ошибке. Ниже: кто/что её допустил,
полный каталог, готовые библиотеки-решения, и корневые узлы-фиксы (не ещё
точечные unit-правила — их избыток и есть часть проблемы).

## МЕТА-ОШИБКА (корень всего)

**Незавершённая миграция «проза-везде → машинный IR-первичный», поверх
fail-open-каркаса.** Два параллельных источника истины на одну сущность, и
разные потребители читают РАЗНЫЕ; там где данных/инструмента нет — считается
успехом.

**Кто/что допустил (методология, не персона):**
- Эволюция v149→v164 «ЛЛМ+проза везде» → начали переводить на IR (A1–B3, K2–N7),
  но **исполнителя ЛЛМ на IR не перевели** — он до сих пор читает прозу
  `specs/*.md`, пока router/tests/conformance уже ездят на IR.
- Храповик поощрял ЛОКАЛЬНЫЕ узловые правила (592 зелёных) — никто не держал
  СКВОЗНОЙ инвариант «источник → потребитель» и «граф замкнут».
- Fail-open заложен изначально «чтобы не рвать поток»: verdict по умолчанию
  PASS/True, оракулы optional (нет либы → `[]`), hollow-spec синтаксически валиден.
- Связность проверяется в 3 местах (policy / accept_decomposer / final-plan),
  ни одно не является единой точкой замыкания → всё всплывает поздно.

## Каталог A — ДВА ИСТОЧНИКА ИСТИНЫ / потребитель читает производное

| # | Дефект | Файл:функция | Severity |
|---|---|---|---|
| A1 | contract продукта из ПРОЗЫ vs IR; router из прозы, tests из IR, **ЛЛМ из прозы** | `spec_flow_runner.py:4016 _product_contract` vs `spec_ir build_ir` | CRITICAL |
| A2 | specs/*.md — РЕЗУЛЬТАT (compiled from OpenAPI), но ЛЛМ читает его как ИСТОЧНИК | `spec_flow_runner.py:1402` (write) vs `:10106/10370` (read `ictx.spec`) | HIGH |
| A3 | сигнатуры handler-ов: `_canonical_handler_symbol` на лету vs skeleton из IR | `spec_flow_runner.py:1383` vs `:11297 _ir_skeleton_for` | HIGH |
| A4 | route-media из прозы vs IR (`_route_media_map`) — дрейф v164 `/about` | `spec_flow_runner.py:4096` vs IR | MEDIUM (I2 частично) |
| A5 | scenarios из IR vs ЛЛМ-тестер угадывает статусы (v149/150) | `spec_scenarios` vs LLM-tester | закрыт B3 |

## Каталог B — НЕЗАМКНУТАЯ СВЯЗНОСТЬ (падает поздно, на integrate)

| # | Дефект | Когда падает | Severity |
|---|---|---|---|
| B1 | consumed-символ без exposes-владельца; mid-growth ошибки НАМЕРЕННО фильтруются | поздно (`_write_ir`) | CRITICAL |
| B2 | orphan-маршрут: контракт есть, handler не синтезирован (gate добавлен задним ходом v159) | финальный план | HIGH |
| B3 | pinned/entry (`src/app.py`,`src/db.py`) без узла-владельца — КОРЕНЬ краснца v167 | integrate_verify | CRITICAL |
| B4 | human-requirement без узла — трассируемость не проверяется | не проверяется | HIGH |
| — | главное: связность в 3 местах, НЕТ единой точки замыкания графа | — | — |

## Каталог C — FAIL-OPEN / ОТСУТСТВИЕ = УСПЕХ

| # | Дефект | Файл:строка | Severity |
|---|---|---|---|
| C1 | `validate_carrier`: формат неактивен/отсутствует → `[]` = «валидно» (fail-open by design) | `spec_registry.py:130` | CRITICAL |
| C2 | LLM-вердикт дефолт **PASS/True** при молчании модели | `spec_flow_runner.py:8220, 3249, 8569` | CRITICAL |
| C3 | hollow-spec (0 requirements/errors) проходит все format-оракулы — синтаксис есть, содержания нет | `spec_ir.py:556 spec_completeness_gaps` (лишь фиксирует, не блокирует) | HIGH |
| C4 | оракулы degrade в `[]` при отсутствии либы (не «not-checked») | `spec_ir.py:391 gherkin_errors`, `:272 jsonschema_errors`, `spec_gherkin.py:57` | HIGH |
| C5 | `standardized_spec` при 0 проверенных узлов — молчание = успех | `spec_flow_runner.py:8101` | MEDIUM |
| C6 | LLM-судья вместо детерминизма в семантических гейтах (недетерминизм) | `spec_flow_diagnosers.py:197` | HIGH |
| C7 | дашборд: пустой узел → «✓ validated»; общий бейдж не агрегирует под-ошибки | `tests/lib/live_dashboard.py:1239, 3620` | HIGH |

## Готовые библиотеки (не изобретать — брать)

| Класс | Либа | Что даёт | Вердикт |
|---|---|---|---|
| контракт→промпт (A1/A2) | **DSPy** | Signature из IR → детерминированный промпт; ЛЛМ получает машинный носитель, не прозу | ВЗЯТЬ |
| fail-closed (C1–C5) | **Pydantic v2** (`extra='forbid'`, required, strict) + jsonschema 2020-12 (`additionalProperties:false`) | пусто/лишнее = ошибка на уровне модели | ВЗЯТЬ |
| conformance код↔контракт | **Schemathesis** (property из OpenAPI) + **pytest-bdd/behave** (исполнить Gherkin из IR против кода) | код проверяется против машинного контракта, не прозы | ВЗЯТЬ |
| замыкание графа (B) | правило в IR + `datamodel-code-generator` (каркас-владелец из OpenAPI), Pact (cross-service) | — | частично |

ТОП-3 по эффекту на цель «слабая ЛЛМ реализует по машинной спеке точно»:
**DSPy, Pydantic v2, Schemathesis.**

## Решения из экосистем spec-first / OpenAPI / BDD (2-й раунд research)

Проверены spec-kit, Specmatic, OpenSpec, Kiro, SpecLoom + OpenAPI/BDD-стек
против классов A/B/C. Вердикты:

| Инструмент | Класс | Вердикт | Как встроить |
|---|---|---|---|
| **Specmatic** (`--strict`) | A+B+conformance | **ВЗЯТЬ** (уже частично B2) | OpenAPI = единый источник контракта; contract-tests обязательный гейт перед integrate; negative-paths (undeclared route/status = fail рано) |
| **SpecLoom** (идея, JS-экосистема) | A+B | **ВЗЯТЬ идею** (Python) | requirement=anchor, код stamped IR-хэшем (`# IR-V:<hash>`); `verify`-gate → **STALE** (спека сменилась, код старый) + **ORPHAN** (requirement без узла). Замыкает A+B по построению |
| **OpenSpec** (подход) | B | **ВЗЯТЬ подход** (уже частично B1) | delta-спека → сценарий(RED) → узел; явная трассировка requirement→scenario→node (закрывает B4) |
| **Redocly CLI** (`--extends recommended-strict`) | C | **ВЗЯТЬ** | заменить openapi-spec-validator в `CONTRACT_VALIDATORS`; strict = fail-closed на hollow OpenAPI |
| **Spectral** (custom oas rules) | C | **ВЗЯТЬ** | адаптер в `spec_registry`: `paths-non-empty`, `required-non-empty`, `no-empty-schema` |
| **openapi-core** | conformance | **ВЗЯТЬ** | в `spec_conformance._invoke()`: runtime-валидация ответа против OpenAPI-схемы (точный код↔контракт) |
| **jsonschema 2020-12 strict + Pydantic v2** (`extra='forbid'`) | C | **ВЗЯТЬ** | пусто/лишнее = ошибка на уровне модели IR |
| **GitHub Spec Kit, AWS Kiro** | — | **НЕ встраивать** | LLM-workflow, не детерминированный компилятор; только EARS/структура как reference |
| **Cucumber/behave/pytest-bdd** | conformance | частично | только исполнение Gherkin как executable-spec, не машинный оракул полноты |
| **Dredd** | conformance | **не брать** | вытеснен Schemathesis/Specmatic |

ГЛАВНОЕ: **hollow-spec = FAIL** достигается не новым кодом, а поднятием
`spec_completeness_gaps` (N6) из «incomplete» в БЛОКИРУЮЩУЮ ошибку + Redocly/
Spectral strict. Замыкание A+B — идея SpecLoom (stamping+verify), уже ~80% в
наших полях IR (owners/requirements). Conformance — Specmatic `--strict` +
openapi-core, уже частично в `CONTRACT_VALIDATORS`.

## Граф работ (КОРНЕВЫЕ фиксы)

```yaml
graph:
  - {id: Q1, needs: [], parallel: "",   status: "[ ]", files: [tests/harness/role_worker.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q2, needs: [], parallel: "q",   status: "[ ]", files: [spec_ir.py, spec_registry.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q3, needs: [], parallel: "q",   status: "[ ]", files: [spec_ir.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q4, needs: [Q1], parallel: "",  status: "[ ]", files: [spec_conformance.py, tests/harness/, tests/requirements-dev.txt, tests/audit/]}
  - {id: Q5, needs: [Q1], parallel: "",  status: "[ ]", files: [tests/harness/role_worker.py, tests/requirements-dev.txt, tests/audit/]}
  - {id: Q6, needs: [],   parallel: "",  status: "[ ]", files: [tests/harness/role_worker.py, tests/harness/llm_backend.py, spec_flow_runner.py, tests/audit/]}
```
Зоны Q1/Q2/Q3 толкаются в `spec_flow_runner.py` → worktree, порядок влития
Q3 → Q2 → Q1. Q4/Q5 после Q1.

### Q1 `single-source-to-consumer` — ЛЛМ-исполнитель получает машинный носитель, не прозу
- выход: промпт исполнителя строится ИЗ IR-фрагмента узла (openapi/gherkin/
  symbols/skeleton/env/schema) как ПЕРВИЧНОГО; проза `specs/*.md` — вторичный/
  сжатый блок или убрана. Router/media/contract читают IR, не `_product_contract`
  из прозы (добить K2-миграцию на исполнителя)
- приёмка: RED — промпт листа несёт прозу как главный носитель / contract из
  прозы; GREEN — машинный носитель первичен, один источник; Stage

### Q2 `fail-closed` — убрать «отсутствие=успех» системно
- выход: verdict по умолчанию **не PASS** (молчание модели = FAIL/retry, не зелёный);
  оракул без либы/носителя = статус **not-checked** (не `[]`), блокирует, не молчит;
  hollow-spec = FAIL (completeness блокирует); `validate_carrier` отсутствующий/
  неактивный носитель = FAIL. IR-схема на Pydantic v2 (`extra='forbid'`)
- приёмка: RED — пустое/молчание даёт PASS в перечисленных местах; GREEN —
  fail-closed везде; Stage
- готовое: Redocly `recommended-strict` + Spectral (`no-empty-schema`,
  `required-non-empty`, `paths-non-empty`) + jsonschema 2020-12 `strict` +
  Pydantic v2 `extra='forbid'`; поднять `spec_completeness_gaps` (N6) из
  «incomplete» в блокирующую ошибку (hollow = FAIL)

### Q3 `contract-closure-one-point` — единая точка замыкания графа ДО реализации
- выход: одна проверка при декомпозиции: каждый pinned/entry/route/consumed-symbol/
  human-requirement имеет узел-владельца, иначе ИМЕНОВАННЫЙ отказ РАНО (не на
  integrate). Снять «намеренную фильтрацию» mid-growth, заменить на «владелец
  обязан появиться до закрытия декомпозиции»
- приёмка: RED — дерево без владельца pinned/route/symbol/req проходит молча,
  падает на integrate; GREEN — ранний именованный отказ; Stage
- готовое: идея SpecLoom — requirement=anchor, код stamped IR-хэшем, verify-gate
  ловит ORPHAN (req без узла) + STALE (спека сменилась, код старый); трассировка
  requirement→scenario→node (подход OpenSpec, ~80% уже в полях owners/requirements)

### Q4 `conformance-from-contract` — Schemathesis + pytest-bdd против кода
- выход: conformance листа проверяет код против МАШИННОГО контракта: Schemathesis
  (property из node.openapi) + pytest-bdd/behave (исполнить node Gherkin) —
  готовыми либами, не рукописно; ЛЛМ-угадывание исключено
- приёмка: RED — код, нарушающий OpenAPI/Gherkin но совпадающий с прозой,
  проходит; GREEN — краснеет от либы-оракула; Stage
- готовое: Specmatic `--strict` (contract-tests, negative-paths) + openapi-core
  (runtime-валидация ответа против схемы) + Schemathesis (property-фаззинг) +
  pytest-bdd/behave (исполнить node Gherkin против кода) — всё уже частично в
  `CONTRACT_VALIDATORS`, финализировать как обязательный гейт

### Q5 `dspy-contract-prompt` (пилот) — промпт через DSPy-сигнатуру из IR
- выход: пилот `dspy.Signature` из IR-фрагмента узла → промпт/исполнение;
  детерминизм и переносимость на слабые модели. Сравнить с Q1-подходом на p6
- приёмка: пилот-прогон p6 depth=spec/execute, метрики реализации листа vs
  текущий; решение расширять/нет; Stage

### Q6 `prompt-capture` — каждый вызов ЛЛМ сохраняется в файл, связан с логом
- выход: КАЖДЫЙ вызов модели (implementer/reviewer/decomposer/diagnoser/tester —
  через `llm_backend.ask`) пишет ПОЛНЫЙ промпт (system+user+контекст) в файл
  `<run_dir>/prompts/<seq>__<node>__<role>__<call-id>.md`; тот же `call-id`
  пишется в событие `trace.jsonl` → промпт однозначно связывается с шагом лога.
  Плюс ответ модели рядом (`.response`) для полной пары. Наблюдаемость: видно,
  что РЕАЛЬНО ушло каждой модели (проверка Q1 — машинный носитель vs проза)
- приёмка: RED — вызов ЛЛМ не оставляет файла промпта / нет call-id связи с
  trace; GREEN — на каждый вызов есть файл + связь с логом по call-id; Stage
- заметка: единая точка — обёртка в `llm_backend.ask`; call-id генерируется
  детерминированно (seq+node+role), не Date/random (правило скриптов)

## Verification
- `.venv/bin/python -m pytest tests/audit/ tests/dashboard/ -q` зелёное.
- Живой `tests/run.sh p6 spec` → v168: ЛЛМ получает машинный носитель; ни один
  fail-open не даёт ложный PASS; отсутствие владельца pinned/route падает РАНО,
  не на integrate; conformance краснеет на дрейфе кода от контракта.
- Дашборд :8092 — пустой узел = «нет спеки», без ложного «✓ validated».
