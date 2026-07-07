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
  - {id: Q1, needs: [], parallel: "",   status: "[x]", files: [tests/harness/role_worker.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q2, needs: [], parallel: "q",   status: "[x]", files: [spec_ir.py, spec_registry.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q3, needs: [], parallel: "q",   status: "[x]", files: [spec_ir.py, spec_flow_runner.py, tests/audit/]}
  - {id: Q4, needs: [Q1], parallel: "",  status: "[x]", files: [spec_conformance.py, tests/harness/, tests/requirements-dev.txt, tests/audit/]}
  - {id: Q5, needs: [Q1], parallel: "",  status: "[~]", files: [tests/harness/role_worker.py, tests/requirements-dev.txt, tests/audit/]}
  - {id: Q6, needs: [],   parallel: "",  status: "[x]", files: [tests/harness/llm_backend.py, tests/audit/]}
  - {id: Q7, needs: [Q2], parallel: "",  status: "[x]", files: [spec_flow_runner.py, tests/audit/]}
  - {id: Q8, needs: [Q2], parallel: "",  status: "[x]", files: [spec_ir.py, tests/audit/]}
  - {id: Q9, needs: [Q8], parallel: "",  status: "[~]", files: [spec_flow_runner.py, tests/audit/]}
```

### Q8 `entry-not-hollow` — сборочный вход экспонирует product-callable (влито, S47)
- заметки: v170 ложный hollow на product_entry — узел-вход владеет src/app.py и
  сводит маршруты в один callable (wsgi_app), но exposes не записывался.
  `_entry_exposes` из product-контракта → code-узел, не hollow. 657 audit green.
  Подтверждено v171 (L0+product_entry ушли из ошибок).

### Q9 `injection-triage` — анализ HITL-инъекции → маршрут (attach / carrier / drop-noise)
- контекст: инъекция может быть ЧЕМ УГОДНО — правкой, новой фичей, сменой сути,
  ИЛИ шумом (стихи/анекдот не по теме). Движок обязан проанализировать и свернуть
  в план так, чтобы выполнить исходное задание с учётом ЗНАЧИМЫХ инъекций.
- найдено: механизм привязки УЖЕ есть — `_amend_target`/`_amend_find_owner`
  (детекторы владельца) + `SPEC_FLOW_REQ_AMEND` (вкл для кейсов с injections) +
  `SPEC_FLOW_AMEND_LLM` (сильная модель-роутер для уточнений, что не поймали
  детерминированные сигналы). НО: (а) LLM-роутер армится только при
  `injections.llm_route: true` в кейсе (p6 не задал) → чистый детерминизм →
  промах на «сделай красиво»; (б) у роутера НЕТ ветки «это шум → выбросить» и
  нет ветки «новая фича без владельца → узел С носителем» — он возвращает
  владельца-или-None, а None форкает пустой лист.
- выход: расширить роутер до 3-way ТРИАЖА (по умолчанию для любой инъекции):
  1. attach — уточнение существующей поверхности → в узел-владелец (уже есть);
  2. new-feature — новая фича с выводимым интерфейсом → узел С носителем
     (маршрут+сценарии), НЕ пустой лист;
  3. drop-noise — не относится к сути софта (стихи/анекдот) → зафиксировать и
     НЕ создавать узел, план не менять.
  Семантику решает ЛЛМ (анализ инъекции), но ИСХОД проверяет детерминированный
  гейт: после обработки инъекций НИ ОДНА не осталась листом-без-носителя
  (attach ∨ carrier ∨ recorded-noise).
- приёмка: RED — инъекция-шум/уточнение форкает пустой лист (hollow в финале);
  GREEN — шум отброшен с причиной, уточнение привязано, фича с носителем; финал
  без hollow из инъекций; Stage
- заметки: НЕ глушить HITL-фичу, НЕ подгонять кейс. Использовать существующие
  `_amend_find_owner` + LLM-router-сем, добавить noise/feature-классификацию и
  no-hollow-from-injection гейт. Это следующая итерация цикла.

### Q7 `tree-visible-incremental` — реализованное дерево видно IR-сборке ВО ВРЕМЯ декомпозиции
- выход: `project["tree"] = root` публикуется ДО `_visit`, чтобы инкрементальные
  IR-записи (идущие внутри `_visit`) видели растущее дерево; ветка не
  сериализуется без детей
- приёмка: RED — publish стоит ПОСЛЕ `_visit` → ветка без детей → Q2 ложный
  hollow; GREEN — publish до visit, ветка с детьми не hollow; Stage S46
- заметки: вскрыто прогоном v168 — L0 (корневая ветка) сериализовалась пустой
  (`children=None`), Q2 верно бил hollow по пустому узлу, но корень — stale tree
  datum: дерево публиковалось после обхода, а инкрементальная запись —
  внутри. Фикс: одна перестановка (`root` мутируется на месте, publish до visit
  даёт живой вид). 650 audit green. НЕ регресс gate — gate верный, ему давали
  устаревшее дерево. Открыто отдельно: дашбордный false-green (C7) — «✓
  validated» рядом с hollow-FAIL на одном узле; L0-кейс закрыт этим фиксом,
  общий агрегат бейджей — отдельный узел при надобности
Зоны Q1/Q2/Q3 толкаются в `spec_flow_runner.py` → worktree, порядок влития
Q3 → Q2 → Q1. Q4/Q5 после Q1.

### Q1 `single-source-to-consumer` — ЛЛМ-исполнитель получает машинный носитель, не прозу
- выход: промпт исполнителя строится ИЗ IR-фрагмента узла (openapi/gherkin/
  symbols/skeleton/env/schema) как ПЕРВИЧНОГО; проза `specs/*.md` — вторичный/
  сжатый блок или убрана. Router/media/contract читают IR, не `_product_contract`
  из прозы (добить K2-миграцию на исполнителя)
- приёмка: RED — промпт листа несёт прозу как главный носитель / contract из
  прозы; GREEN — машинный носитель первичен, один источник; Stage
- заметки: S41 (625 audit green). Движок: `machine_carrier_of(node, frag)` +
  `Engine._leaf_machine_carrier` → `ictx["carrier"]` (openapi/behavior/
  scenarios/symbols/env/schema, union узел+IR-фрагмент, node wins). Воркер:
  `_machine_carrier_block` рендерит «MACHINE CONTRACT (authoritative … prose
  SECONDARY)» ПЕРВЫМ; `_coder_chat_prompt` — общий head для chat+orchestra;
  добавлено во все 3 сайта (orchestra/chat/claude). Носитель предшествует
  прозе. Тест: tests/audit/test_machine_carrier_primary.py. Остаток (не в этом
  атоме, отдельный узел при надобности): router/media на `_product_contract`
  из прозы (A4/I2 частично закрыты)

### [x] Q2 `fail-closed` — убрать «отсутствие=успех» системно
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
- РЕЗУЛЬТАТ (Stage S38): реализовано в spec_ir.py + spec_registry.py +
  spec_flow_runner.py; тест `tests/audit/test_fail_closed_absence.py` (12).
  RED-кейсы (все краснели на pre-Q2): hollow проходил `validate_ir`; inactive/
  unknown carrier → `[]`==valid; отсутствие либы у оракула → `[]`==ok; reply
  без поля verdict → PASS / без `approved` → True. Что сделано fail-closed:
  (1) **hollow** — новый `_hollow_node_reason` (исполняемый узел без поведения
  И без интерфейса/контракта) блокирует в `validate_ir["errors"]`; на раннем
  decomposer-seam hollow ИСКЛЮЧЁН из фильтра, чтобы гейт `standardized_spec`
  сохранил атрибуцию отказа. (2) **carrier** — `validate_carrier` на
  unknown/inactive-stub возвращает NOT-CHECKED-маркер, не `[]`. (3) **oracle
  degrade** — `gherkin_errors`/`jsonschema_errors` при отсутствии либы отдают
  NOT-CHECKED (`spec_ir.not_checked`/`is_not_checked`), а не `[]`; `validate_ir`
  выносит их в ОТДЕЛЬНЫЙ ключ `not_checked` (гейт полноты краснит, но
  hermetic-subprocess без dev-либы не получает ложный refused). jsonschema-схема
  IR уже strict 2020-12 (`additionalProperties:False`) + добавлен FormatChecker;
  import обёрнут в NOT-CHECKED (нет крэша при отсутствии либы). (4) **silent
  verdict** — `_review_verdict_from_reply` (reply без `verdict` → REJECT) и
  `_approved_from_reply` (approved только при явном truthy) применены в трёх
  местах (reviewer 8250, `_hitl` approver 3279, approver-on-reject 8599);
  автономный default кладёт явный verdict → genuine sign-off не блокируется.
  Дополнено фикстур: 1 (общий контракт `_errors` в `test_ir_closed_world.py` —
  добавлен ключ `not_checked`, 15 тестов файла позеленели одной правкой; это
  расширение контракта, не ослабление — errors/incomplete по-прежнему не
  смешиваются). Прогон `tests/audit/`: 597 passed, 11 skipped; `tests/dashboard/`
  109 passed. Коммит `feat(S38): fail-closed ...`. НЕ push. Предупреждение:
  worktree был создан от древнего cccbf0a — сделан `git merge --ff-only 57d6f98`
  ПЕРЕД работой (HEAD=57d6f98).

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
- заметки: Stage S39. Готово. Единая функция `spec_ir.contract_closure_gaps(ir,
  constitution)` — чисто по IR, 5 owner-инвариантов (pinned/entry/route/consumed-
  symbol/human-requirement). Вызов в `spec_flow_runner._run` СРАЗУ после
  `_write_ir()` (закрытие декомпозиции, до сборки/integrate) → веха
  `decomposition_closure`/FAIL + loop + doctor_advise, если граф не замкнут.
  Mid-growth фильтр «is exposed by no node» в `_accept_decomposer_ir` НЕ трогал
  (там pending легитимен); closure — точка ПОСЛЕ роста, где pending уже не
  оправдание. Храповик доказан: 6/6 RED-тестов падают против no-op заглушки
  closure, 11/11 зелёные с логикой. tests/audit/test_contract_closure_gaps.py.
  Прогон: audit 596 passed / 11 skipped, nodes+decomposition 150 passed,
  dashboard 109 passed. Существующие фикстуры не покраснели (их деревья несут
  владельцев либо не имеют pinned/req — легально). НЕ трогал completeness/carrier/
  verdict (зона Q2). Commit S39.

### Q4 `conformance-from-contract` — Schemathesis + pytest-bdd против кода
- выход: conformance листа проверяет код против МАШИННОГО контракта: Schemathesis
  (property из node.openapi) + pytest-bdd/behave (исполнить node Gherkin) —
  готовыми либами, не рукописно; ЛЛМ-угадывание исключено
- приёмка: RED — код, нарушающий OpenAPI/Gherkin но совпадающий с прозой,
  проходит; GREEN — краснеет от либы-оракула; Stage
- заметки: S42 (629 audit green). `spec_conformance`: скомпилированный тест
  листа конформит тело против ПОЛНОЙ OpenAPI-схемы готовым
  `openapi-schema-validator` (`_HELPER_CONFORM` + `_conform(schema, body,
  label)` рядом с рукописным `_shape_body_asserts`). Ловит enum/nested/format,
  что верхнеуровневый пин пропускает. Импорт НЕ обёрнут (fail-closed, как Q2):
  нет либы = жёсткий фейл, не skip. Новых зависимостей нет —
  openapi-schema-validator уже в requirements-dev.txt. Тест:
  tests/audit/test_conformance_oracle.py.
- ОТЛОЖЕННОЕ ДОДЕЛАНО: S44 (640 audit green) — конформанс СОБРАННОГО приложения
  на integrate. `_assembled_response_schemas` (схемы ответов из IR) +
  `_response_probe_rows` (детерминированные replay-строки) + probe печатает
  `CONFORM_DUMP` (изолированный python3 не может импортить оракул — печатает
  тела) + `_conform_assembled_responses` валидирует их движком (.venv,
  openapi-schema-validator), RED с именем маршрута+владельца при дрейфе.
  Ловит то, что листовой оракул не видит: роутер завёл не тот хендлер, entry
  переформатировал тело. Тест: tests/audit/test_assembled_conformance.py.
  schemathesis property-fuzz ДОБАВЛЕН: S45 (646 audit green) — opt-in
  (`SPEC_FLOW_SCHEMATHESIS_FUZZ`, бюджет `SPEC_FLOW_SCHEMATHESIS_MAX`),
  derandomize (фиксированный seed → воспроизводимо). schemathesis только
  генерит входы (его CheckContext API version-fragile), вердикт — тем же
  оракулом openapi-schema-validator: фаззенный запрос не должен 5xx, 2xx-тело
  конформит схему. Прогон под sys.executable (.venv, где есть schemathesis+
  оракул — изолированный python3 probe их не имеет). `_assembled_openapi_doc`
  (union IR) + `_SCHEMATHESIS_FUZZ` + `_assembled_fuzz`, хук в
  `_assembled_product_boots` после детерминированного conform. FUZZ_ERROR =
  fail-closed. По умолчанию ВЫКЛ (S44 — дефолтный гейт). Тест:
  tests/audit/test_schemathesis_fuzz.py. Ничего не отложено
- готовое: Specmatic `--strict` (contract-tests, negative-paths) + openapi-core
  (runtime-валидация ответа против схемы) + Schemathesis (property-фаззинг) +
  pytest-bdd/behave (исполнить node Gherkin против кода) — всё уже частично в
  `CONTRACT_VALIDATORS`, финализировать как обязательный гейт

### Q5 `dspy-contract-prompt` (пилот) — промпт через DSPy-сигнатуру из IR
- выход: пилот `dspy.Signature` из IR-фрагмента узла → промпт/исполнение;
  детерминизм и переносимость на слабые модели. Сравнить с Q1-подходом на p6
- приёмка: пилот-прогон p6 depth=spec/execute, метрики реализации листа vs
  текущий; решение расширять/нет; Stage
- заметки: S43 (633 audit green). Выбор пользователя — ЛЁГКИЙ СРЕЗ без dspy.
  `_signature_block(ctx, fn)` в role_worker: детерминированный типизированный
  INPUT→OUTPUT (contract/data_schema/dependencies/acceptance/env → code/tests/
  exposes) из IR-носителя. Env-гейт `SPEC_FLOW_SIGNATURE_PROMPT`, ПУСТ по
  умолчанию (пилот, ноль влияния на живой промпт), при включении предшествует
  носителю. Ноль новых зависимостей (dspy НЕ импортируется). Тест:
  tests/audit/test_signature_prompt.py. Полный DSPy-фреймворк (LLM-оптимизатор)
  — отдельный follow-up по явному запросу пользователя
- СТАТУС `[~]` частично: сдан детерминированный срез (signature-блок из IR без
  зависимостей). НЕ сдан полный DSPy-путь (реальный `dspy.Signature` +
  оптимизатор + сравнение на живом прогоне). Полный план ниже.

  #### Альтернативный ПОЛНЫЙ план Q5 с DSPy (не начат — нужен явный опт-ин)
  Цель: заменить рукописную сборку промпта исполнителя на `dspy.Signature`/
  `dspy.Module`, где контракт узла = типизированные поля, а сам промпт
  компилируется/оптимизируется DSPy под конкретную (слабую) модель.

  Граф под-узлов (все needs: Q1 закрыт):
  ```yaml
  graph:
    - {id: Q5D1, needs: [],     status: "[ ]", files: [tests/requirements-dev.txt]}
    - {id: Q5D2, needs: [Q5D1], status: "[ ]", files: [tests/harness/dspy_signature.py]}
    - {id: Q5D3, needs: [Q5D2], status: "[ ]", files: [tests/harness/dspy_signature.py, tests/harness/llm_backend.py]}
    - {id: Q5D4, needs: [Q5D2], status: "[ ]", files: [tests/harness/role_worker.py]}
    - {id: Q5D5, needs: [Q5D3, Q5D4], status: "[ ]", files: [tests/audit/, tests/eval/]}
    - {id: Q5D6, needs: [Q5D5], status: "[ ]", files: [.claude/plans/]}
  ```
  - Q5D1 `dep` — добавить `dspy-ai` (тянет litellm) в requirements-dev.txt;
    пиннинг версии; проверить оффлайн-импорт в аудит-гейте (не рвёт CI без сети).
  - Q5D2 `signature` — `class ImplementLeaf(dspy.Signature)`: входы
    `contract: str` (машинный носитель), `dependencies: str`, `acceptance: str`,
    `env: str`; выходы `code: str`, `tests: str`, `exposes: str`. Билдер
    `signature_from_ir(node, frag)` заполняет поля из IR-носителя (переиспользует
    `machine_carrier_of`). Приёмка: детерминированное заполнение из известного IR.
  - Q5D3 `LM-адаптер` — обернуть `llm_backend.ask` как `dspy.LM`, чтобы DSPy
    ходил через ЕДИНЫЙ бэкенд (free OpenRouter/headers), без прямых вызовов
    провайдера (правило: тесты только через общий бэкенд). Приёмка: dummy-LM
    прогон возвращает структуру полей.
  - Q5D4 `интеграция` — в `role_worker`: при `SPEC_FLOW_DSPY_IMPL=1` исполнитель
    идёт через `dspy.Predict(ImplementLeaf)` вместо ручного промпта; иначе
    сегодняшний путь. Взаимоисключимо с `SPEC_FLOW_SIGNATURE_PROMPT`.
  - Q5D5 `оценка` — offline eval-набор: N листов p6, метрика реализации
    (проходят ли скомпилированные тесты + конформанс-оракул S42) для трёх
    режимов: baseline (проза), Q5-lite (signature-блок), Q5-DSPy. Тот же
    llm_backend, та же слабая free-модель. Приёмка: таблица метрик, решение
    расширять/откатить по данным, не по мнению.
  - Q5D6 `решение` — записать вердикт в план: DSPy даёт прирост на слабой
    модели → мигрировать основной путь; нет → оставить Q5-lite, DSPy убрать
    из deps. Приёмка: зафиксированное решение + числа.

  Риски: dspy-ai + litellm — большой транзитивный хвост (версионные конфликты
  с текущими pinned либами); DSPy-оптимизатор недетерминирован (нужен кэш
  скомпилированного промпта, иначе прогон невоспроизводим — конфликт с
  правилом «прогон не зависит от сессии»); LLM-зависимая оценка требует квоты
  free-пула. Поэтому по умолчанию — Q5-lite, полный путь только по опт-ину.

### [x] Q6 `prompt-capture` — каждый вызов ЛЛМ сохраняется в файл, связан с логом
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
- РЕЗУЛЬТАТ (Stage S40): реализовано ТОЛЬКО в `tests/harness/llm_backend.py` —
  единая дверь `ask` покрыла все роли, `role_worker.py`/`spec_flow_runner.py`
  трогать не потребовалось (все ходят через `ask`, run_dir из env). Два helper:
  `_capture_prompt` (пишет `<run_dir>/prompts/<seq>__<node>__<role>__<call-id>.md`
  с полным system+user+мета, возвращает детерминированный call_id `<seq>-<node>-<role>`)
  и `_capture_response` (кладёт `<...>.response.md` рядом в единой точке выхода `_ret`).
  run_dir = родитель `SPEC_FLOW_LLM_LOG`; лог выключен → capture no-op (как и логирование).
  call_id проброшен в `_bctx` ДО `call_start` и в `_bident` (call_ok) → событие
  лога однозначно связано с файлом промпта. seq — per-process монотонный счётчик
  под lock (детерминированный порядок). RED: `tests/audit/test_prompt_capture.py`
  краснел «two calls -> two prompt files: 0==2» (файлов нет, call_id не связан) →
  GREEN 4/4. Инвариант single-door (`test_single_llm_door`) цел: call_id живёт
  внутри двери, событий call_* наружу не добавлено. Полный `tests/audit/` +
  single-door: 587 passed / 11 skipped. Коммит `feat(S40): ...`.

## Verification
- `.venv/bin/python -m pytest tests/audit/ tests/dashboard/ -q` зелёное.
- Живой `tests/run.sh p6 spec` → v168: ЛЛМ получает машинный носитель; ни один
  fail-open не даёт ложный PASS; отсутствие владельца pinned/route падает РАНО,
  не на integrate; conformance краснеет на дрейфе кода от контракта.
- Дашборд :8092 — пустой узел = «нет спеки», без ложного «✓ validated».
