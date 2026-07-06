# Спека обязана быть в машинном СТАНДАРТЕ — текстовая проза запрещена

Дата: 2026-07-06. Контекст: прогон v166 показал спеку `db_layer` чистой
прозой (контракт трёх функций в EARS-тексте, машинных `symbols` нет →
`spec_lint FAIL #20`). Юзер (жёстко): **не может быть спеки без стандарта;
текстовая спека = бесконтрольность и галлюцинации; проверять это ОТДЕЛЬНЫМ
гейтом внутри движка; парсеры — только ГОТОВЫЕ ЛИБЫ, свой код не писать.**

## Принцип (нерушимый)

Каждый узел несёт машинную спеку в СТАНДАРТЕ, распарсенную и провалидированную
готовой библиотекой-оракулом. Проза `.md` — только ПРОИЗВОДНАЯ проекция для
человека, НИКОГДА не носитель. Узел без стандартного машинного носителя не
проходит гейт (FAIL, не READY).

## ЕДИНСТВЕННЫЙ КРИТЕРИЙ приёмки спеки (главный)

Юзер 2026-07-06: формат можно ЛЮБОЙ из реестра стандартов, можно СМЕШИВАТЬ,
ВСТРАИВАТЬ один в другой, РАСШИРЯТЬ. Критерий ровно один:

> Спека узла должна быть настолько СТРУКТУРИРОВАННОЙ и ПОЛНОЙ, чтобы ЛЮБАЯ
> СЛАБАЯ ЛЛМ выполнила задачу узла ПОЛНО и ТОЧНО — без догадок, без
> галлюцинаций, без обращения к внешнему контексту.

Из этого следует: (1) носитель машинный (парсится либой-оракулом), проза —
производна; (2) гейт движка проверяет НЕ «конкретный формат», а «спека в
машинном стандарте из реестра И полна для слабой модели» (все входы/выходы/
типы/ошибки/примеры заданы данными, а не намёком).

## Реестр стандартов (класс узла → носитель → готовая либа-оракул)

Форматы КОМБИНИРУЕМЫ и ВСТРАИВАЕМЫ (напр. OpenAPI-операция + Gherkin-сценарии
приёмки на неё + JSON Schema тел). Внедряются по мере появления класса узла;
сейчас в p-кейсах реальны REST+поведение+данные — их либы уже есть/ставятся.

| Класс узла | Стандарт-носитель | Готовая либа-оракул | Статус |
|---|---|---|---|
| HTTP REST (маршруты) | **OpenAPI 3.1** | `openapi-spec-validator`, `openapi-schema-validator` | внедрено (K1/K2/L1) |
| Поведение/функции/storage/lib | **Gherkin** (Given/When/Then) | `gherkin-official` (AST); опц. `behave`/`pytest-bdd` | внедрено (N1) |
| Формы данных/конфиг | **JSON Schema 2020-12** | `jsonschema` | внедрено (H7) |
| Событийные (Kafka/MQTT/WS) | **AsyncAPI** | `asyncapi`-parser (spectral/py) | по появлению узла |
| gRPC/бинарный контракт | **Protocol Buffers** | `protobuf`/`grpcio-tools` | по появлению узла |
| Графовые запросы | **GraphQL SDL** | `graphql-core` | по появлению узла |
| Протокол-независимая модель | **Smithy** / **TypeSpec** | `smithy`/`@typespec` (компиляция→OpenAPI) | по появлению узла |
| Архитектурное решение/фича | **Markdown RFC** / **User Story** структурно | линтер-схема поверх frontmatter | вспомогательно, не заменяет носитель |

Реестр РАСШИРЯЕМ: движок держит таблицу `format → validator` (адаптеры), новый
стандарт = новый адаптер, не переписывание гейта. AsyncAPI/Protobuf/GraphQL/
Smithy/TypeSpec — заведены как адаптеры-заглушки, активируются когда декомпозер
породит узел такого класса. Референс методологии — BDD + executable-spec
(OpenAI harness-engineering): спека = исполняемый контракт, не описание.

Story Mapping/User Stories и Markdown RFC — НЕ носители (это проза), но их
СТРУКТУРА (роль/действие/ценность; frontmatter RFC) может обрамлять машинный
носитель как человекочитаемая производная. Носителем остаётся стандарт из
реестра.

## Модель содержания спеки ноды (что делает полноту ПОЛНОТОЙ)

Реестр отвечает «в каком ФОРМАТЕ», модель содержания — «ЧТО должно быть
описано», чтобы слабая ЛЛМ выполнила узел без догадок. Полнота = ВСЕ
ПРИМЕНИМЫЕ аспекты заданы ДАННЫМИ; неприменимый аспект явно помечен `n/a`
(не пропущен молча).

| Обязательный аспект узла | Формат-носитель | Кто заполняет |
|---|---|---|
| **ЧТО ДЕЛАЕТ** — поведение When/Then, крайние случаи | Gherkin (Given/When/Then + Scenario Outline/Examples) | декомпозер, spec-стадия |
| **ЧЕМУ СООТВЕТСТВУЕТ** — HTTP-интерфейс | OpenAPI 3.1 (routes/status/media/schemas/errors) | декомпозер |
| **ЧЕМУ СООТВЕТСТВУЕТ** — данные / БД / формы | JSON Schema 2020-12 (таблицы, поля, тела) | декомпозер |
| **ПУБЛИЧНЫЙ API кода** — функции/классы | `symbols.exposes` (типизир. сигнатуры + returns + raises) | декомпозер |
| **АРХИТЕКТУРА** — связи, зависимости, эффекты, границы, файлы | машинные `dependencies`/`consumes`/`effects`/`files` (IR) | декомпозер (декомпозиция) |
| **ТРЕБОВАНИЯ** — нормативные, трассируемые | EARS, привязанные к сценарию/маршруту (машинно) | декомпозер |
| **ОШИБКИ / ПРИМЕРЫ** — крайние случаи | Examples (Gherkin) + error responses (OpenAPI) | декомпозер |

## Процесс: кто / где / как разрабатывает архитектуру и дизайн ноды

- **Архитектура** (разбивка на узлы, связи, границы, порядок волн) — ДЕКОМПОЗЕР
  при построении дерева + существующие план-гейты (waves/spikes/leaf_check).
- **Дизайн ноды** (контракт: интерфейс + поведение + данные + сигнатуры) —
  SPEC-стадия узла: декомпозер эмитит машинный носитель (OpenAPI — K2,
  Gherkin+symbols — N3), комбинируя форматы по аспектам.
- **Полнота** — ГЕЙТ N2 по модели содержания (N6): каждый применимый аспект
  задан данными; неполнота = ИМЕНОВАННЫЙ отказ, узел не READY.
- **Доработка неполного** — ДОКТОР/респек добавляет недостающий аспект,
  перепроверка гейтом.
- **ЛЛМ ровно в двух местах** (как в перевороте): описание → машинный носитель
  (валидируется либой + гейтом); тела функций в неприкосновенном каркасе.

## Отвергнутое ПЕРЕОСМЫСЛЕНО

Прежде (A2 / EXTERNAL-TECH-GUIDE) полный Cucumber/pytest-bdd был отвергнут в
пользу самодельного мини-раннера «Gherkin как нотация без парсер-поверхности».
Юзер это ОТМЕНЯЕТ: парсер сценариев — готовая либа (`gherkin-official`), не
свой код. Гайд обновить: строку из «отвергнуто» → «принято», с причиной.

## Библиотеки (готовые, не писать своё)

- `gherkin-official` — парсинг `.feature`/сценариев в AST; оракул стандарта Gherkin.
- `behave` или `pytest-bdd` — опциональное ИСПОЛНЕНИЕ сценариев (шаги → проверки),
  если существующего раннера `spec_scenarios` поверх AST либы недостаточно.
- OpenAPI-стек уже внедрён (K1/K2/L1).
Выбор конкретной делает узел N1 после проверки pip-доступности; приоритет —
чистый парсер `gherkin-official` (без runner-навязывания).

## Граф работ

```yaml
graph:
  - {id: N1, needs: [],        parallel: "",     status: "[x]", files: [spec_scenarios.py, spec_ir.py, tests/requirements-dev.txt, docs/EXTERNAL-TECH-GUIDE.md, tests/audit/]}
  - {id: N6, needs: [N1],      parallel: "",      status: "[x]", files: [spec_ir.py, tests/audit/]}
  - {id: N2, needs: [N1, N6],  parallel: "",      status: "[x]", files: [spec_flow_runner.py, spec_registry.py, tests/audit/]}
  - {id: N3, needs: [N1],      parallel: "wave2", status: "[x]", files: [tests/harness/llm_decomposer.py, spec_flow_runner.py, spec_gherkin.py, spec_ir.py, tests/audit/]}
  - {id: N5, needs: [N1],      parallel: "wave2", status: "[x]", files: [tests/lib/live_dashboard.py, tests/dashboard/]}
  - {id: N4, needs: [N2, N3],  parallel: "",      status: "[ ]", files: [spec_flow_runner.py, tests/audit/]}
```

N6 (модель содержания) — фундамент для гейта N2: формализует ОБЯЗАТЕЛЬНЫЕ
аспекты (см. раздел «Модель содержания») как машинную проверку. Зона N6
(`spec_ir.py`) с идущими не толкается — можно вести сразу после N1.

Зоны: N2 и N3 толкаются в `spec_flow_runner.py` → worktree с порядком влития
**N3 → N2** (N3 эмитит носитель, N2 гейтит его наличие). N5 (дашборд) с N2/N3
не пересекается → параллельно. N4 (проза-derived) после N2+N3.

Статусы: `[ ]` ожидает | `[~]` в работе | `[!]` человек | `[x]` готов | `[-]` отменён.

### N1 `gherkin-lib-oracle` — готовый Gherkin-парсер как оракул стандарта
- выход: `gherkin-official` (или behave/pytest-bdd) в `tests/requirements-dev.txt`;
  `spec_scenarios.py` парсит/валидирует Gherkin ЛИБОЙ (AST), самодельный парсинг
  сценариев снят; гайд обновлён (отвергнутое→принятое)
- приёмка: RED — сценарий валидируется библиотекой, не рукописным разбором;
  битый Gherkin ловится либой; весь `tests/audit` зелёный; Stage в TAXONOMY
- заметки:
  - Либа: **`gherkin-official==41.0.0`** — официальный Cucumber-парсер, чистый
    AST, без раннер-навязывания. Проверена pip-доступность (41.0.0), ставится и
    импортируется в venv worktree. `behave`/`pytest-bdd` НЕ взяты: их ценность —
    step-execution, а сценарии уже исполняет `spec_scenarios` против WSGI; нужен
    только оракул грамматики.
  - Что заменил: рукописный `_check_scenario` был grammar-blind (проверял только
    ключи/типы closed-схемы, docstring гордо заявлял «not Gherkin, no parser
    surface»). Добавлен `spec_ir.gherkin_errors(ir)`: каждый closed-сценарий
    проецируется в канонический `.feature` (`_scenario_to_gherkin`) и парсится
    либой; расхождение AST↔closed-структуры (не ровно 1 Feature+Scenario, или
    keyword-последовательность шагов ≠ ожидаемой) = именованная ошибка.
    `validate_ir` теперь вливает вердикт оракула (паттерн S13.8/S14.7 — либа
    рядом с ручными cross-rules, не вместо них). Degrade: нет либы → `[]`,
    ручные правила стоят (проверено).
  - Носитель Gherkin стал источником грамматики; closed `{when{method,path,body},
    then{status,media,body_check}}` осталась ЦЕЛЕВОЙ структурой движка.
  - RED краснел на: (1) нет `spec_ir.gherkin_errors`; (2) нет `import gherkin`
    в пути валидации; (3) closed-valid сценарий с `requirement="core\n  Scenario:
    hijack"` (валидный route /health, ручной разбор молчит) → либа видит 2
    сценария в AST → ошибка; (4) `validate_ir` не вливал вердикт оракула.
  - Stage: **S31** (`tests/audit/test_gherkin_lib_oracle.py`, 5 тестов).
  - Зелёные: полный `tests/audit` = **550 passed, 11 skipped** (было 545+5);
    scenario/closed-world/L1 наборы зелёные.
  - Коммит: см. лог ветки (feat commit S31).

### N2 `node-spec-standard-required` — гейт: спека узла ОБЯЗАНА быть в стандарте

- выход: (а) РЕЕСТР адаптеров `format → validator` (OpenAPI/Gherkin/JSON Schema
  активны; AsyncAPI/Protobuf/GraphQL/Smithy/TypeSpec — заглушки-адаптеры,
  расширяемо); (б) гейт `standardized_spec` в движке (`_product_check`/шов узла):
  КАЖДЫЙ узел имеет валидный машинный носитель из реестра, распарсенный либой,
  проза-only = FAIL; (в) проверка ПОЛНОТЫ-для-слабой-модели — все входы/выходы/
  типы/ошибки/примеры заданы ДАННЫМИ, не намёком (неполный носитель = FAIL)
- приёмка: RED — (1) узел с прозой без носителя краснеет ИМЕНОВАННЫМ отказом;
  (2) машинный, но НЕПОЛНЫЙ носитель (нет типов/ошибок/примеров) тоже краснеет;
  GREEN — полный носитель из реестра зелёный; узел не READY пока не выполнен
  единственный критерий. Отдельная проверка «внутри движка» как требует юзер; Stage
- заметки: [x] Готов. Stage **S35** (`tests/audit/test_standardized_spec_gate.py`,
  5 тестов).
  - **Реестр** — новый модуль `spec_registry.py`, таблица-ДАННЫЕ
    `FORMAT_VALIDATORS: format → {standard, validator, active}`. Активные адаптеры
    (openapi/gherkin/jsonschema) резолвятся в ГОТОВЫЕ оракулы, уже живущие в коде
    (`spec_openapi.node_openapi_library_errors`, `spec_ir.gherkin_errors`,
    `spec_ir.jsonschema_errors`) — реестр их СОБИРАЕТ, не переписывает. Заглушки
    (asyncapi/protobuf/graphql/smithy/typespec) объявлены `active=False` + no-op
    валидатор; активируются строкой, не веткой гейта. `node_carrier_format(node)`
    по классу узла (тот же `spec_ir._node_class`/`spec_gherkin.is_code_leaf`)
    отвечает какой АКТИВНЫЙ стандарт несёт узел или None (проза-only).
  - **Где встроил гейт** — шов `_accept_decomposer_ir` в `spec_flow_runner.py`,
    новый блок `if not errors:` СРАЗУ после блока Gherkin (`decomposer_gherkin`
    PASS) и ПЕРЕД `reg.update(nodes)` — точный аналог decomposer_openapi/gherkin.
    Для КАЖДОГО не-branch узла: (а) `spec_registry.node_carrier_format` → None =
    проза-only = ошибка; иначе `validate_carrier` (валидность носителя); (б)
    `spec_ir.spec_completeness_gaps(node)` (N6) — любой gap = ошибка. Есть ошибки
    → веха `standardized_spec` FAIL (узел не входит в IR-реестр); чисто → веха
    `standardized_spec` PASS (симметрия с M3). НЕ дублирую: валидность формата —
    оракулы реестра, полнота — N6-детектор; гейт СОБИРАЕТ вердикт.
  - RED краснел (5 тестов): (1) нет модуля `spec_registry`; (2) проза-only узел
    класса `other` шёл через шов с ZERO ошибок и без вехи (ни openapi- ни
    gherkin-гейт его не трогают); (3) http-узел с валидным OpenAPI, но только
    2xx-ответом (неполный error surface по N6) не краснел ни на одном гейте;
    (4) полный узел не эмитил PASS-веху `standardized_spec`.
  - **Фикстуры дополнены до полноты (8 тестов, 4 файла)** — не подгонка, а
    соответствие новому инварианту: их http/code-узлы предшествовали модели
    полноты (не было `scenarios`/`effects`/error-response). Дополнены до N6
    (`test_decomposer_emits_gherkin` +effects; `test_decomposer_emits_ir` — helper
    `_http_leaf` + error_status; `test_decomposer_emits_openapi`,
    `test_spec_validation_milestones` — error-response в media маршрута +
    scenarios/effects; `test_ir_compiled_tests` — error_op + effects + health
    scenarios). Гейт НЕ ослаблен.
  - Зелёные: `tests/audit` = **569 passed, 14 skipped, 1 FAIL** —
    `test_gherkin_lib_oracle::test_validate_ir_folds`, PRE-EXISTING на чистой базе
    8ffc2c9 (нет либы `gherkin-official` в окружении), доказан `git stash` — не
    мой регресс. nodes+decomposition+gates = **219 passed**;
    spec+coverage+contracts = **436 passed**.
  - Коммит: `feat(S35): standardized_spec gate — every node spec must be
    machine-standard AND complete, else not READY (N2)`.

### N3 `decomposer-emits-gherkin` — декомпозер эмитит машинный носитель не-HTTP узла
- выход: декомпозер выдаёт машинный Gherkin feature (+ при необходимости JSON
  Schema тел, комбинируемо) как ПЕРВИЧНЫЙ носитель поведения/функций (storage/
  lib-узлы, как `db_layer`); `symbols` (exposes с сигнатурами/типами/ошибками)
  выводятся из шагов; каждый сценарий ПОЛОН для слабой модели (конкретные вход,
  выход, тип, крайние случаи). Проза производна. Класс «db_layer прозой без
  symbols» невозможен — контракт машинный и полный с первого шага
- приёмка: RED — не-HTTP лист без машинного носителя ИЛИ с неполным (нет типов/
  крайних случаев) = отказ на шве декомпозера; GREEN — полный Gherkin, spec_lint
  не FAIL, гейт N2 зелёный; Stage
- заметки:
  - Stage: **S32** (`tests/audit/test_decomposer_emits_gherkin.py`, 5 тестов).
  - Носитель не-HTTP листа = машинный Gherkin feature (поле узла `behavior`,
    Feature + Scenario на каждую публичную функцию) + полный `symbols.exposes`
    (каждая запись: name, типизированные `args` вида `name: type`, `returns`,
    `raises`). Проза производна.
  - Новый адаптер-оракул `spec_gherkin.py` (аналог `spec_openapi.py`):
    `is_code_leaf` (files+нет openapi+нет children), `feature_library_errors`
    (грамматика через `gherkin-official`, degrade при отсутствии либы),
    `node_behavior_carrier_errors` (полнота: feature обязателен и грамматичен,
    exposes непусты и полны — иначе именованные ошибки). Полнота exposes —
    детерминированна, работает и без либы.
  - Шов `_accept_decomposer_ir`: gherkin-блок после openapi-блока — веха
    `decomposer_gherkin` FAIL/PASS (точный аналог `decomposer_openapi`);
    отказ = fragment не входит в реестр, не молчаливый проход.
  - `spec_ir`: closed-мир расширен — `behavior` в `_NODE_KEYS`, `returns`/
    `raises`/`signature` в `_EXPOSE_KEYS` (иначе полный носитель отвергался).
    Требование ПРИСУТСТВИЯ живёт в `spec_gherkin`, не в closed-схеме.
  - Декомпозер-промпт: не-HTTP лист обязан эмитить `behavior` (Gherkin) +
    типизированный exposes с returns/raises.
  - RED краснел на: (1) прозовый db_layer (пустые symbols, нет behavior) шёл
    через шов с ZERO ошибок — v166 молчаливый проход; (2) неполный носитель
    (untyped exposes, нет returns/raises) не краснел; (3) полный носитель с
    `behavior`/`returns`/`raises` отвергался closed-миром до расширения ключей.
  - Зелёные: `tests/audit` = **555 passed, 11 skipped** (было 550+11);
    decomposition+nodes+zone-trim = **152 passed**. K2/S22, N1/S31 целы.
  - Коммит: `feat(S32): decomposer emits machine Gherkin+symbols for non-HTTP
    nodes, complete-for-weak-LLM (N3)`.

### N4 `prose-derived-from-standard` — проза .md строго ПРОИЗВОДНА от стандарта
- выход: `specs/*.md` компилируются ИЗ машинного носителя (OpenAPI + Gherkin
  AST) для ВСЕХ классов узлов, не только HTTP (K3 расширяется на Gherkin);
  правка .md на сборку не влияет
- приёмка: RED — .md не-HTTP узла не выводится из Gherkin-AST / проза влияет на
  сборку; GREEN — детерминированный рендер из носителя; Stage
- заметки:

### N5 `dashboard-standard-spec` — дашборд: стандарт спеки + валидация, без текста
- выход: вкладка «Спека» показывает СТАНДАРТ (OpenAPI-операции / Gherkin-сценарии
  из AST) + бейдж «validated by <lib>»; фикс лживой заглушки IR-вкладки
  (`live_dashboard.py:3356` «пишется после реализации дерева» → живой инкремент
  под локом); текстового-only вида спеки нет
- приёмка: RED — spec-панель рендерит прозу без стандарта/бейджа; заглушка IR
  описывает старое поведение; GREEN — стандарт+бейдж, заглушка исправлена; Stage
- заметки: сделано (S33). Stage 33 в TAXONOMY (S32 оставлен агенту N3). Правки
  только в `tests/lib/live_dashboard.py` + новый `tests/dashboard/test_dashboard_standard_spec.py`.
  (1) Заглушка IR-вкладки исправлена: старый текст «после того как дерево
  реализовано» / «после реализации дерева» убран; теперь правдиво — живой
  инкремент по ходу под `_ir_write_lock` после каждого закрытого листа
  (`_write_ir_incremental` в `_visit`), пусто пока не закрыт первый лист.
  (2) Панель «Спека» узла показывает МАШИННЫЙ стандарт: HTTP-узел — OpenAPI
  routes-таблица (K3); не-HTTP — Gherkin Given/When/Then из `scenarios` +
  сигнатуры `symbols`; проза .md рендерится ниже, помечена производной. Новые
  функции `_node_standard_spec_html`, `_node_standard_badge_html`, `_node_is_http`,
  `_std_badge_chip`; ключ узла `spec_standard` в `_build_state`.
  (3) Бейдж по стандарту над прочитанным ir.json теми же оракулами
  (`spec_openapi.validate_openapi_library`, `spec_ir.gherkin_errors`,
  `spec_ir.jsonschema_errors`): «OpenAPI 3.1 ✓» / «Gherkin ✓» / «JSON Schema ✓»
  с числом ошибок; не-HTTP узел — Gherkin-бейдж + «no HTTP interface» (не дефект);
  оракул без либы degrade-ит в «—» (без ложного зелёного).
  (4) Ни одна вкладка не удалена (тест целостности global+node вкладок).
  RED-краснели: заглушка со старым текстом; отсутствие `_node_standard_spec_html`/
  `_node_standard_badge_html`; отсутствие `spec_standard` в state.
  Прогон: 8/8 новых зелёных; tests/dashboard+lib+audit — 656 passed, 1 failed
  (`test_gherkin_lib_oracle::test_validate_ir_folds…` — предсуществующий фейл
  среды без `gherkin-official`, зона N1/S31, доказан git stash — не регресс N5;
  M1/M3/M4 dashboard-тесты целы).

### N6 `completeness-model` — машинная модель обязательных аспектов ноды
- выход: `spec_ir.spec_completeness_gaps(node)` — по классу узла определяет
  ПРИМЕНИМЫЕ аспекты (см. «Модель содержания»: поведение/интерфейс/данные/
  symbols/архитектура/требования/ошибки) и возвращает список ПУСТЫХ обязательных
  аспектов; неприменимый помечается `n/a` явно, не пропускается. Это определение
  «полноты для слабой модели» как данные, а не суждение
- приёмка: RED — узел с валидным носителем, но БЕЗ требований / крайних случаев /
  типов у symbols НЕ краснеет; GREEN — `spec_completeness_gaps` называет каждый
  недостающий аспект; неприменимый аспект не попадает в gaps; Stage
- заметки: [x] Готов. Реализован `spec_ir.spec_completeness_gaps(node)` —
  ЧИСТЫЙ детектор (данные, не гейт-эмиссия; веху эмитит N2). Семь аспектов в
  `COMPLETENESS_ASPECTS`: behavior / http_interface / data_schema / public_api /
  architecture / requirements / errors_edges. ПРИМЕНИМОСТЬ по классу узла
  (`_node_class`): http (владеет `openapi`) / code (не-HTTP `.py`-лист через
  `spec_gherkin.is_code_leaf`) / branch (`children`) / other. Неприменимый
  аспект помечается n/a КЛАССОМ явно, НЕ попадает в gaps (http_interface n/a для
  storage-листа; public_api n/a для HTTP-листа — его контракт в OpenAPI;
  behavior/interface/public_api n/a для branch). Каждый gap = `{aspect, why}`,
  why непусто. Ошибки/крайние случаи: HTTP — не-2xx responses; код — Examples /
  второй Scenario в Gherkin (проба ПРИСУТСТВИЯ, грамматика — у gherkin/openapi
  оракулов, не дублирую). public_api переиспользует `spec_gherkin.
  _expose_incompleteness` (типы args/returns/raises). Локальный импорт
  spec_gherkin (цикла нет — spec_gherkin не тянет spec_ir). RED краснел на
  отсутствии функции + на невыявлении дыр (13 failed). Stage S34, тест
  tests/audit/test_spec_completeness_gaps.py — 13 passed. Регрессии: N1/S31
  (test_ir_scenarios_schema) + N3/S32 (test_decomposer_emits_gherkin) целы.
  Полный tests/audit/ = 564 passed, 14 skipped, 1 FAIL —
  test_gherkin_lib_oracle::test_validate_ir_folds_in_the_gherkin_oracle,
  PRE-EXISTING на чистой базе c20dd15 (нет либы gherkin-official в окружении),
  НЕ мой регресс (проверено git stash). Коммит: feat(S34).

## Порядок исполнения

1. N1 (фундамент-оракул) — solo.
2. После N1: N3 + N5 параллельно (worktree, зоны не пересекаются).
3. N2 после N3 (общий runner, worktree-порядок N3→N2).
4. N4 после N2+N3.
Каждый узел — храповик RED→GREEN, готовые либы, поиск через codebase-memory,
прогон через `tests/run.sh` (не python напрямую).
