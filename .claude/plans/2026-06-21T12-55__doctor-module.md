# Модуль-доктор: диагноз причины ошибки и лечение с учётом места

## Контекст

Разбор провала `delete_note` (прогон v041) показал: движок реагирует на ошибку
**по месту** — каждый гейт (`spec_review`, `spec_scope`/scope-lint, `leaf_check`,
`contract_check`, `integrate/verify`) имеет свою захардкоженную политику, вердикт
бинарный (`PASS/FAIL/REJECT/ERROR`), причина — свободный текст. Таксономии причин,
истории причин, детекта колебаний и лестницы лечения нет. Итог: система знает
*что* упало, не *почему*; одно позднее требование с дублем доходит до реализации
и ломает лист.

Цель: «**детект причины × контекст → диспетчер лечения**», где место не
игнорируется (географический/временной/по связанности контекст важен), таблица
причин — конфиг, оценщик причины — настраиваемый и не выдумывает, контур лечения
гарантированно завершается и деградирует в «записать долг», а не в крах.

База причин: `.claude/plans/2026-06-21T12-25__base-error-causes.md` (15 причин →
чем лечить → действие в коде).

## Решения (зафиксировано)

- **Порядок: сперва профилактика**, потом доктор (профилактика дешевле убирает
  большинство ошибок).
- **Скоуп MVP — только кейс `p6_micro_notes`**, тестируем всё **с чекпоинта, не с 0**.
- **Сильный тир = `xiaomimimo/mimo-v2.5-pro`** (самая сильная из бесплатных, Bifrost,
  1000/сутки) — её доктор зовёт на трудные узлы и на диагноз.
- **Голоса оценщика = 1** на старте; **обязательно логировать** разброс/уверенность,
  чтобы по данным решить, нужно ли больше.
- **Ступень `web_search` ВВОДИМ** — поиск в интернете/доках как ступень лестницы
  перед вызовом человека (требует отдельной реализации инструмента).
- **Человек в headless-p6 — реальный** (через существующий `_human_ask`).

## Профилактика — в рабочих модулях, НЕ в докторе

Доктор только **диагностирует и лечит** уже возникшую ошибку. Чтобы ошибок было
меньше, профилактику встраиваем в сами рабочие модули — каждый работает так, чтобы
его класс ошибок не возникал. Ниже: причина → профилактическая мера → куда добавить.

| Причина | Профилактическая мера | Куда добавить (модуль / шов) |
| --- | --- | --- |
| Слабый исполнитель | Назначать модель по сложности узла | Маршрутизация воркера при спавне узла читает метрики `leaf_check` → тир в `chain_for` (`harness/llm_backend.py:~260`, `workers:`-конфиг) |
| Слишком крупная задача | Предел «одна проверяемая дельта на лист» | Декомпозер + пороги `_handle_leaf_check` (`spec_flow_tools.py:~74/213`) — ужесточить атомарность на разбиении |
| Нечёткая постановка | Контракт узла (зона/контекст/критерии) до старта | Схема и промпт декомпозера (`harness/role_worker.py make_decomposer`) + `spec_lint` требует контракт |
| Лишние данные на входе | Давать только свою зону, соседей ссылкой | Сборка контекста воркера `_decomposer_ctx` / implement-ctx (`spec_flow_runner.py`) — обрезка до своей поверхности |
| Проверка без права вернуть | Ревью имеет право вернуть узел | `review_policy` / обвязка ревьюера (`runner:~584/2701`) — гарантировать возврат |
| Исправление переписыванием | Дифф-правка по умолчанию | Путь починки реализатора (уже есть точечный дифф #41) — сделать дефолтом |
| Пустой результат | Дельта как условие приёмки | `verify`/приёмочный гейт + `testable_criteria` декомпозера |
| Несогласованные проверки | Один вердикт по единому ключу | Агрегация вердиктов на гейте/`engine.loops` (`runner:~1621`) |
| Расхождение задачи и проверки | Проверка выводится из требования | Авто-вывод контракта (#49) + промпт ревьюера/верификатора |
| Случайность ответа | Низкая температура + N голосов по умолчанию | Дефолты `ask()` для всех воркеров (`harness/llm_backend.py:~455`, `workers:`-конфиг) |
| Нет связи с целью | Привязать цель + критерий покрытия к узлу | Декомпозер протягивает корневую цель (`Traces-to` в шаблоне спеки) |
| Скрытые зависимости | Граф зависимостей + топо-порядок заранее | Декомпозер объявляет `depends_on`; планировщик топо-порядка (`runner:~2813`) |
| Молчаливое усечение | Запрет тихой обрезки, всё логировать | `ask()` + чанкование файлов (`INLINE_FILE_LIMIT`) — лог отброшенного |
| Накопление мусора | Контроль качества на каждом шаге | Тированное ревью (#58 A1) + ранний integrate (#44) — проверять узел до передачи вверх |
| Потеря памяти/контекста | Всегда подавать прежние решения/ограничения | `MemoryProvider` (#26/#29) + сборка контекста воркера всегда инжектит решения |

Эти меры — **отдельные задачи в существующих модулях**, ставятся независимо от
доктора и до него; доктор остаётся «нижней сеткой» для того, что профилактика
не покрыла.

## Архитектурное решение (где живёт код)

Доктор должен работать и в реальном прогоне плагина, где `tests/` может быть не на
`sys.path` (`spec_flow_runner.py:1963`). Поэтому **весь доктор — в корне плагина**,
рядом с движком:

- `spec_flow_doctor.py` — фасад `Doctor` (`diagnose()`, `treat()`), модель данных,
  чистая математика контура (лестница/кольцо/ранжирование/потолок) — юнит-тестится
  без движка.
- `spec_flow_diagnosers.py` — реестр детерминированных детекторов + промпт-классификатор.
- `spec_flow_remedies.py` — таблица `причина×контекст→лечение`, дефолтные словари
  `DEFAULT_CAUSES`/`DEFAULT_EVALUATOR` + загрузчики (мердж как `review_policy`).

`escalation_model` из `tests/harness/cycle_control.py` переиспользуем через уже
существующую ссылку движка `_cycle` (передаётся в `Doctor`), а не дублируем и не
тащим логику в harness. ЛЛМ-вызов диагноза идёт через существующий резолвер
`_llm_router_handles()` (`spec_flow_runner.py:~1929`) → `ask`/`chain_for`. Доктор
сам не пишет события и не зовёт воркеров — возвращает решение (`Action`), движок
его исполняет (единый писатель `emit`, вся работа воркеров — в движке).

## Модель данных (`spec_flow_doctor.py`)

- `Context` — **место**: `node, module, gate, depth`; **время**: `attempt,
  version, changed_since_last`; **связанность**: `depends_on, surface_neighbors`.
- `Finding` — заземление: `cause, detector, evidence(цитата), confidence, source`
  (`deterministic|semantic|config`). Любая причина обязана сослаться на находку.
- `Diagnosis` — `context, ranked(tuple[Finding] корень→симптом), abstained`.
- `Remedy` — `name, params`; `RemedyLadder(cause, rungs)`.
- `TreatmentRecord` — пишется в `engine.loops` как dict, совместимо со схемой
  (`type/task/detail` + `cause/remedy/rung/level/outcome`).
- `Action` — `kind(rework|split|escalate|halt|record|ask_human|noop), remedy,
  feedback, record`. Движок свитчит по `kind`.

`Cause` — не питон-enum, а множество ключей из конфига `causes:`, валидируется при
загрузке (таблица = конфиг).

## Диагност (`spec_flow_diagnosers.py`)

Слоисто (правило «код, не промпт-онли + проверка»):

1. **Детерминированные детекторы** (реестр, грубо→точно). Переиспользуют готовое:
   - дубль-поверхность → `_dup_surface_findings` (`runner:~950`) как есть;
   - крупная задача → пороги `_handle_leaf_check` (`spec_flow_tools.py:~223`);
   - пустая дельта → сравнение с `previous_spec`/хэшем спеки (`runner:~2609/3399`);
   - правка-перепиской → один и тот же текст причины ≥2 заходов;
   - несогласованные проверки → разброс вердиктов по одному ключу в `loops`;
   - скрытые зависимости → граф по `depends_on` vs порядок (топо-помощник `~2813`);
   - молчаливое усечение → маркеры отброшенного в журнале;
   - накопление мусора → провал предка по `depends_on`;
   - потеря памяти → прежние решения отсутствуют в контексте воркера.
2. **Промпт-классификатор** (семантика: слабый исполнитель, нечёткая постановка,
   расхождение задача/проверка, нет связи с целью). Срабатывает только если
   детерминированных находок нет и гейт в `evaluator.semantic_gates`. Строится как
   `make_reviewer`/`_amend_llm_router`. Выход — **одна причина из enum + цитата**;
   ответ без причины из списка или без цитаты отбрасывается.
3. **Чисто конфиг** — «проверка без права остановить»: читается флаг `can_block`
   гейта.

Порядок: детерминированные (доверие 1.0) → семантика по остатку → конфиг.

## Диспетчер и лестница (`spec_flow_remedies.py`)

Таблица по причине + модификаторы по контексту:

```yaml
doctor:
  enabled: false            # по умолчанию — старое поведение байт-в-байт
  default_ladder: [rework, escalate_model, split, record]
  causes:
    task_too_large:  { detector: leaf_metrics, ladder: [split, record],
                       by_gate: { spec_review: { ladder: [split, record] } } }
    weak_implementer:{ detector: prompt, ladder: [rework, escalate_model, record] }
    empty_delta:     { detector: empty_delta, ladder: [reject_empty, rework, stop_branch] }
    hidden_deps:     { detector: hidden_deps, ladder: [order_by_deps, record] }
    # ... все 15
  causes_order: [hidden_deps, garbage_accumulation, empty_delta, ...]  # корень→симптом
evaluator:
  role: reviewer
  model: ""                 # тир; chain_for(reviewer, "diagnose")
  temperature: 0.0
  votes: 3
  majority: 2
  require_cite: true
  escalate_model: ""        # сильнее модель при abstain
  semantic_gates: [spec_review, integrate_verify]
```

**Место модулирует лечение** (место не игнорируем):
- `by_gate[gate]` подменяет лестницу (на замороженном дереве `split` → `record`);
- глубина: корень (`depth==0`) не делает тихий `stop_branch`/`halt` → `ask_human`;
- связанность: при непустых `surface_neighbors` предпочесть `order_by_deps`/`trim_input`
  обычному `rework` (дубль-поверхность — проблема охвата, не качества);
- время/заход: номер ступени = число прошлых `TreatmentRecord` по ключу
  `(cause, place)`, `place = f"{nid}:{gate}"`.

## Конфигурация: где хранится и как настраивается

### Где лежит (5 слоёв, мердж в порядке возрастания приоритета)

1. **Дефолты в коде** — `spec_flow_remedies.py`: `DEFAULT_CAUSES`, `DEFAULT_EVALUATOR`,
   `DEFAULT_TIERS`, `DEFAULT_DOCTOR`. Это «заводская» полная таблица, чтобы запуск
   работал без единой настройки.
2. **Флоры окружения** — `tests/.test.env`: числовые пороги `SPEC_FLOW_DOCTOR_*`
   (минимально допустимые значения, читаются через `config.env()`).
3. **Env-override (JSON)** — переменные `SPEC_FLOW_DOCTOR_CAUSES` /
   `SPEC_FLOW_DOCTOR_EVALUATOR` / `SPEC_FLOW_TIERS` для разовой подмены без правки YAML.
4. **Case-YAML** — `tests/scenarios/p6*.yaml`, блоки `doctor:`, `causes:`,
   `evaluator:`, `workers.tiers:`. Мердж — глубокий, как у `review_policy`
   (`_policy_config`, `tools:~141`).
5. **Запуск ранера / плагина** — флаги CLI и `run()`-аргументы при старте, **высший
   приоритет**: оператор крутит параметры на конкретный прогон, не трогая кейс.

Загрузчики: `causes_config(project, overrides)`, `evaluator_config(...)`,
`tiers_config(...)` в `spec_flow_remedies.py` — собирают слои 1→5, где `overrides`
приходят из аргументов запуска.

### Передача при запуске ранера / плагина (слой 5)

Ранер уже принимает флаги (`--from-run`, `--checkpoint-every`, `--dashboard`…) —
добавить группу доктора и прокинуть в `run()` как `doctor_overrides`:

```
python tests/run.py p6_micro_notes \
  --doctor-enabled \
  --doctor-tier evaluator=strong \
  --doctor-set evaluator.votes=5 \
  --doctor-set causes.task_too_large.ladder='[split,record]' \
  --doctor-floor LEVELS=3
```

- `--doctor-set k=v` — точечный оверрайд любого ключа `doctor:/causes:/evaluator:`
  (dot-путь), парсится в dict и мерджится последним.
- `--doctor-tier role=tier`, `--doctor-floor NAME=val` — частые случаи короче.
- Программный вызов плагина: тот же dict передаётся в `Engine.run(..., doctor_overrides=...)`
  рядом с тем, как сейчас принимается `project`/`gates`.
- Эхо применённого конфига в `trace.jsonl` (one событие `doctor-config`), чтобы в
  сравнении прогонов было видно, с какими параметрами шёл каждый `vNNN`.

### Что настраивается (полный пример)

```yaml
# --- иерархия мощности ЛЛМ и их параметры (workers.tiers) ---
workers:
  tiers:                      # лестница мощности: слабее → сильнее (только free/без квоты)
    weak:   { models: ["lmstudio/gpt-oss-20b", "claude/haiku"], temperature: 0.2, votes: 1, timeout: 300 }  # локальная, без квоты/429
    medium: { models: ["xiaomimimo/mimo-v2.5", "claude/haiku"], temperature: 0.1, votes: 1 }
    strong: { models: ["xiaomimimo/mimo-v2.5-pro"],      temperature: 0.0, votes: 1 }
  complexity_to_tier:         # профилактика «модель по сложности узла»
    leaf_small:  weak         # метрики leaf_check ниже порога
    leaf_big:    medium
    branch:      strong
  defaults:   { tier: medium }   # дефолтный тир для роли, если не задан
  decomposer: { tier: strong }
  implementer:{ tier: medium }
  reviewer:   { tier: strong }
  verifier:   { tier: medium }

# --- список причин и лечение (таблица доктора) ---
doctor:
  enabled: false
  default_ladder: [rework, escalate_tier, web_search, split, ask_human, record]
  causes_order: [hidden_deps, garbage_accumulation, empty_delta, vague_spec, ...]
causes:
  task_too_large:  { detector: leaf_metrics, ladder: [split, record],
                     by_gate: { spec_review: { ladder: [split, record] } } }
  weak_implementer:{ detector: prompt, ladder: [rework, escalate_tier, ask_human] }
  garbage_accumulation: { detector: ancestry,
                          ladder: [find_root, escalate_tier, web_search, ask_human, rollback_redo] }
  # ... все 15, см. base-error-causes.md

# --- оценщик причины (диагност) ---
evaluator:
  tier: strong               # ссылается на workers.tiers (не дублирует модель) = mimo-v2.5-pro
  temperature: 0.0
  votes: 1                    # старт с 1; разброс/уверенность логируем для решения «нужно ли больше»
  majority: 1
  require_cite: true
  abstain_escalate_tier: true
  log_vote_spread: true       # обязательный лог уверенности диагноза
  semantic_gates: [spec_review, integrate_verify]

# --- когда звать человека (явный триггер вызова) ---
human:
  enable: true
  after_resources: [escalate_tier, web_search]  # звать только если эти ступени не помогли
  at_levels: [respec, human]      # с какого уровня глубины разрешён вызов
  on_abstain: true                # диагноз не уверен (нет большинства) → человек
  on_root_failure: true           # провал корня (depth==0) не останавливаем тихо
  blast_radius_min: 3             # большой радиус правки (как RESPEC_HITL_BLAST)
  timeout_s: 300                  # ожидание ответа (флор SPEC_FLOW_HITL_ASK_TIMEOUT)
  on_timeout: record              # нет ответа → записать долг и вверх, не крах
```

Где живёт вызов человека: блок `doctor.human` (выше) + уже существующие HITL-ручки
движка — `hitl:` кейса (`approve_required`, `worker_may_ask_human`,
`human_may_intervene`), `gates.*.exhausted=ask`, `RESPEC_HITL_BLAST` (`runner:~610`),
хук `_human_ask` (`runner:~2760`), флоры `SPEC_FLOW_HITL_TIMEOUT`/`HITL_ASK_TIMEOUT`.
Доктор не вводит новый канал к человеку — он только решает **когда** дёрнуть
существующий, по параметрам `doctor.human`. Ступень `ask_human` в `ladder` причины
срабатывает лишь если выполнены условия `human` (иначе пропускается на `record`).

### Числовые флоры контура (`tests/.test.env`)

```
SPEC_FLOW_DOCTOR_RING=6              # размер кольца истории причин
SPEC_FLOW_DOCTOR_NODE_ATTEMPTS=6    # общий потолок попыток на узел
SPEC_FLOW_DOCTOR_NONSHRINK=2        # стоп, если набор причин не сжимается N заходов
SPEC_FLOW_DOCTOR_LEVELS=4           # глубина лестницы (лист→родитель→переспек→человек)
SPEC_FLOW_DOCTOR_ATTEMPTS_PER_LEVEL=2
```

### Принципы

- **Мощность ЛЛМ — через тиры, не через хардкод модели.** Роли, оценщик и лестница
  лечения (`escalate_tier`) ссылаются на `workers.tiers`; смена модели = одна правка
  в одном месте. Параметры (температура/голоса/таймаут) живут в тире.
- **Список причин — данные, не код.** `causes:` правится в YAML; код только исполняет.
- **Лестница ресурсов** (сильнее ИИ → веб/доки → человек) — это ступени `ladder`
  причины: `escalate_tier`, `web_search`, `ask_human`, затем `rollback_redo`.
- **Один источник на параметр.** Нет дублей: тир задаёт модель+параметры, причина
  задаёт лестницу, флор задаёт предел. Дефолты в коде — единственное место с
  «заводскими» значениями.

### Тюнинг и сравнение через кейс `p6_micro_notes.yaml`

Кейс — высший слой мерджа, поэтому все параметры доктора крутятся прямо в нём, без
правки кода. Блок для вставки в `tests/scenarios/p6_micro_notes.yaml`:

```yaml
doctor:
  enabled: true
  default_ladder: [rework, escalate_tier, web_search, split, ask_human, record]
  causes_order: [hidden_deps, garbage_accumulation, empty_delta, vague_spec]
causes:
  task_too_large:  { detector: leaf_metrics, ladder: [split, record] }
  empty_delta:     { detector: empty_delta, ladder: [reject_empty, rework, escalate_tier] }
  # переопредели любую из 15 — остальное берётся из DEFAULT_CAUSES
evaluator:
  tier: strong               # = mimo-v2.5-pro
  temperature: 0.0
  votes: 1                   # старт; log_vote_spread даст данные для увеличения
  majority: 1
  require_cite: true
  log_vote_spread: true
workers:
  tiers:
    weak:   { models: ["lmstudio/gpt-oss-20b", "claude/haiku"], temperature: 0.2, votes: 1 }
    strong: { models: ["xiaomimimo/mimo-v2.5-pro"], temperature: 0.0, votes: 1 }
  complexity_to_tier: { leaf_small: weak, branch: strong }
```

MVP-прогон: всё на `p6_micro_notes`, **с чекпоинта** (`--from-run N --from-checkpoint M`),
не с нуля; человек — реальный.

Реализация оверрайда (часть Ф0): загрузчики `causes_config/evaluator_config/tiers_config`
читают `project.get("doctor"|"causes"|"evaluator")` и `project["workers"]["tiers"]`
и глубоко мерджат поверх `DEFAULT_*` (тем же путём, что `gates:` парсится в
`run()` ~2074). Числовые флоры из `tests/.test.env` остаются нижней границей.

Сравнение прогонов: запускать p6 с разными `doctor:`-блоками и сводить инструментом
сравнения прогонов (#31) — метрики (число лечений, доля закрытых причин, READY/нет)
рядом по версиям `vNNN`. Дашборд показывает причину/лечение/исход построчно.

## Контур лечения (чистая математика в `spec_flow_doctor.py`)

Состояние на узле: `node["_doctor"] = {ladder{}, ring[], node_attempts, level,
level_attempts, last_causeset, nonshrink_rounds, open{}}`.

- **Та же причина повторяется** → не повторять лечение: шаг вниз по лестнице
  `(cause, place)`; лестница кончилась → подъём уровня. Счётчик только растёт.
- **Разные причины** → прогресс, если прошлая `(cause,place)` закрылась (зелёная);
  иначе общий потолок на узел `node_attempts` + проверка «нет сокращения красных за
  2 захода» = «болтанка» → подъём уровня.
- **Цикл причин (A→B→A)** → кольцо последних N (`ring`, floor 6), предикат периода
  ≤ N/2; при колебании лечить не лист, а **уровнем выше** (пересборка родителя →
  переспек → человек), кольцо чистится при смене уровня.
- **Несколько причин сразу** → ранжированный набор по `causes_order` (корень→симптом),
  лечим **по одной**, после каждого — **передиагноз** (симптомы исчезают). Набор не
  сокращается 2 захода подряд → подъём уровня.
- **Глубина**: `0 лист → 1 родитель → 2 переспек → 3 человек`; жёсткий потолок =
  `levels × attempts_per_level`. На потолке — **долг + наверх**, без краха (как
  политика record `runner:~2779/2061`). Жёсткий стоп — только если кейс задал
  `gates.*.exhausted=halt` (переиспользуем `ReviewExhaustedHalt`/`IntegrateFailHalt`).

Завершаемость: каждый `treat` увеличивает хотя бы один ограниченный монотонный
счётчик, счётчики не падают (кроме смены уровня, тоже ограниченной) ⇒ цикл всегда
завершается в PASS или DEBT.

## Надёжность оценщика (против выдумок)

В блоке `evaluator`: тир модели (через `chain_for`), температура ~0, N голосов и
выбор большинством (`majority`), право `abstain` → подъём/сильнее модель,
обязательная цитата конкретной находки (`require_cite`), enum-выход. Голос с
цитатой, которой нет в реальном выводе гейта, отбрасывается до подсчёта.

## Наблюдаемость

Каждый диагноз/лечение → `self.emit(...)` (`runner:~1643`) с
`gate=f"doctor:{cause}"`, и запись в `self.loops`. На дашборде красный→зелёный
работает сам: бейдж по «последнему вердикту на гейт» (`live_dashboard.py:~1104`) —
закрытие той же `cause+place` поздним PASS делает строку зелёной. Схема дашборда не
меняется (колонки `gate/verdict/action/detail` уже есть).

## Обратная совместимость

При `doctor.enabled=false` гейты идут мимо доктора — поведение p4/p5 байт-в-байт
(как флаги `tiering`/`SPEC_FLOW_PRE_GATE`). Дефолтная таблица при `enabled=true` без
переопределений воспроизводит текущие политики: `spec_review` REJECT →
`[rework, escalate_model, record]` с длиной = `review.max_rework`; `integrate_verify`
→ `[rework, record]`; scope-lint дубль → `[rework]` (2 ступени). Шим в `run()`:
если заданы legacy `gates.review`/`on_integrate_fail`, но нет `doctor:` — синтезируем
эквивалентный `doctor`-конфиг.

## Точки врезки (за флагом `doctor.enabled`)

| Шов | Файл:строка | После |
| --- | --- | --- |
| spec_review REJECT | `spec_flow_runner.py:~2701` | `diagnose`→`treat`→`Action` вместо `on_reject`-свитча |
| scope-lint дубль | `spec_flow_runner.py:~2637` | находки → `Finding`, тот же rework как `Action` |
| integrate/verify FAIL | `spec_flow_runner.py:~3345` | `diagnose(gate=integrate_verify)`→ступень лестницы |
| leaf_check BRANCH | `spec_flow_tools.py:~238` | выдать `Finding` `task_too_large` для `split` |
| конфиг `doctor:` | `spec_flow_runner.py:~2074` | мердж рядом с `gates:` |

Переиспользовать: `_dup_surface_findings` (`~950`), `_handle_leaf_check`
(`tools:~223`), `_llm_router_handles` (`~1929`), `ask`/`chain_for`
(`harness/llm_backend.py`), `make_reviewer`/`_amend_llm_router`, `DEFAULT_REVIEW_POLICY`
мердж (`~1547`), `_policy_config`/`_research_lane_config` (`tools:~141`),
`engine.loops` (`~1621`), `escalation_model` (`harness/cycle_control.py:21`).

## Порядок сборки (каждая фаза за флагом, отдельно поставляема)

1. Модель данных + конфиг-загрузчики (`spec_flow_doctor.py`, `spec_flow_remedies.py`)
   — чисто, юнит-тесты.
2. Детерминированный диагност (`spec_flow_diagnosers.py`), обёртка `_dup_surface_findings`
   и порогов `leaf_check` в `Finding`. Тесты на готовых фикстурах.
3. Диспетчер `treat()` (причина×контекст→`Action`) + дефолтные лестницы;
   golden-тест: `Action` совпадает с сегодняшними решениями политик.
4. Врезка одного шва (`spec_review` REJECT) за `doctor.enabled`; проверить p4/p5 при
   флаге off — без изменений.
5. Промпт-классификатор (семантика) + N-голосов/majority/abstain/цитата.
6. Остальные швы (integrate, scope-lint, leaf_check) + контур: лестница, кольцо,
   ранжирование, потолок, передиагноз.

## Проверка

- Юнит: чистая математика контура (лестница/колебание/ранжирование/потолок/сжатие
  набора) и загрузка/мердж конфига `doctor:`.
- Golden: при `enabled=true` без переопределений решения = текущим политикам
  (p4/p5 прогоны идентичны при `enabled=false`).
- E2E: p6 с рандомными поздними инъекциями (как v041) — кейс `delete_note` должен
  получить причину `route-redeclare/empty_delta`, пройти лестницу до `split`/`record`
  и не уйти в реализацию с пустой дельтой; на дашборде строка из красной стать
  зелёной при закрытии той же `cause+place`.
- Тесты только через harness spec-flow на free OpenRouter / живой Bifrost, как
  принято; никаких прямых curl мимо ask().

##  

Ссылки на код — пути от корня плагина `spec-flow/`. Свежие файлы доктора Serena
ещё не проиндексировала (LSP-кэш), номера строк взяты по живому коду (grep).

Профилактика (в рабочих модулях, до доктора):
- [ ] Модель по сложности узла: маршрутизация `chain_for` по метрикам `leaf_check`
  → строит.блоки готовы: `complexity_to_tier()` `spec_flow_remedies.py:215`,
    `chain_for_tier()` `tests/harness/llm_backend.py:306`; маршрутизация на спавне — НЕТ (pending)
- [ ] Атомарность: предел «одна дельта на лист» в декомпозере + пороги `leaf_check`
  → пороги `MAX_MODULES/TASKS/INTERFACES/LOC` `spec_flow_tools.py:74-77`,
    `_handle_leaf_check` `spec_flow_tools.py:213`; ужесточение на разбиении — pending
- [x] Контракт узла (зона/контекст/критерии) в схеме декомпозера + `spec_lint`
  → `_decomposer_ctx` late_req_contract `spec_flow_runner.py:2594`;
    `make_decomposer` `tests/harness/role_worker.py:545-560`
- [ ] Обрезка входа воркера до своей зоны, соседи — ссылкой
  → НЕТ (pending); место: `_decomposer_ctx` `spec_flow_runner.py`
- [x] Дифф-правка реализатора по умолчанию
  → `_apply_diff_repair` `tests/harness/role_worker.py:1389` (вызов :1106)
- [ ] Дельта как условие приёмки (`verify` + `testable_criteria`)
  → частично: детектор `empty_delta` `spec_flow_diagnosers.py` `_scope_findings`;
    отдельного приёмочного гейта пустой дельты нет (pending)
- [x] Проверка выводится из требования (авто-контракт + промпт ревью)
  → авто-контракт `tests/harness/contract_from_smoke.py:1`; узел несёт требование
    в спеку (`Covers human requirement`/`Acceptance` `spec_flow_runner.py:1211`);
    `_REVIEW_TASK` `tests/harness/role_worker.py` требует проверку, выводимую ИЗ
    требования (REJECT если её нет); тест `test_spec_coverage_criterion.py`
- [x] Цель + критерий покрытия привязаны к узлу (`Traces-to`)
  → `Workspace.spec` `spec_flow_runner.py:1211`: `Covers human requirement:` +
    `Acceptance (coverage criterion)` из `node["requirement"]`; модель-независимо
    (эхо человеческого текста, без web/app-литералов). Тест coverage_criterion
- [x] `depends_on` от декомпозера + топо-порядок запуска
  → `_topo_order` `spec_flow_runner.py:2949`
- [x] Низкая температура + N голосов по умолчанию для всех воркеров
  → `_with_default_params()` `tests/harness/llm_backend.py:317` + вызов в `ask()` :505;
    флор `SPEC_FLOW_WORKER_TEMPERATURE` `tests/.test.env`; голоса оценщика — `DEFAULT_EVALUATOR`
- [x] Запрет тихой обрезки: лог отброшенного, чанкование файлов
  → `_inline_file` лог+пометка отброшенного `tests/harness/role_worker.py:314-323`
- [x] Контроль на каждом шаге: тированное ревью + ранний integrate
  → review tiering `spec_flow_runner.py:598,603`; incremental integrate `spec_flow_runner.py:1613`
- [ ] Всегда инжектить прежние решения/ограничения (память)
  → `MemoryProvider.recall_block` `tests/harness/memory.py:43,57`; «всегда инжектить» — частично (pending)

Пререквизиты доктора (отдельные задачи):
- [x] Тиры мощности: `workers.tiers` + `complexity_to_tier` + резолвер в `chain_for` (strong = mimo-v2.5-pro)
  → `chain_for_tier` `tests/harness/llm_backend.py:306`, tier-fallback :278/286/289;
    `complexity_to_tier` `spec_flow_remedies.py:215`; `DEFAULT_TIERS` `spec_flow_remedies.py:26`
- [x] Инструмент `web_search` (поиск в интернете/доках) как ступень лестницы перед человеком
  → `search()` `spec_flow_websearch.py:42`, `summarize()` :61
- [~] Источники данных детекторов: обход предков по `loops` (garbage_accumulation) + история причин (rewrite_loops) ПОДКЛЮЧЕНЫ; маркеры отброшенного (silent_truncation) и снимок памяти (memory_loss) — ещё нет
  → `_doctor_advise` биндит живые `self.loops` в helpers + строит `reason_history`
    из прошлых reject-записей узла `spec_flow_runner.py:1998-2012`; тест
    `tests/coverage/test_diagnosers_data.py` (4 зелёных: ancestry fires/silent, rewrite fires/quiet);
    `_truncation`/`_memory_absent` ждут плумбинга dropped/missing_decisions из движка (pending)
- [x] Алгоритм `find_root` (бисекция/трассировка корневого источника)
  → `find_root()` `spec_flow_doctor.py:190`
- [x] Метрики доктора в `RunResult` + сравнение прогонов (#31): число лечений, доля закрытых причин, READY, разброс голосов
  → `doctor_metrics()` `spec_flow_doctor.py:202`; эмит в `_result` `spec_flow_runner.py:2550`

Доктор (диагноз + лечение, за флагом `doctor.enabled`):
- [x] Ф0: модель данных + конфиг-загрузчики (`spec_flow_doctor.py`, `_remedies.py`), юнит-тесты
  → dataclasses `spec_flow_doctor.py:22-97`; загрузчики `spec_flow_remedies.py:164-219`;
    тесты `tests/coverage/test_doctor_core.py`
- [x] Ф1: детерминированный диагност (`_diagnosers.py`) поверх `_dup_surface_findings` и `leaf_check`
  → `Diagnosers` `spec_flow_diagnosers.py:241`, `DETECTORS` :132
- [x] Ф2: диспетчер `treat()` + дефолтные лестницы; golden = текущим политикам
  → `Doctor.treat` `spec_flow_doctor.py:262`, `ladder_step` :118; golden — pending (нужен прогон)
- [x] Ф3: врезка шва `spec_review` REJECT за флагом; p4/p5 при off — без изменений
  → `_doctor_advise` `spec_flow_runner.py:1941`; spec_review шов :2686
- [x] Ф4: промпт-классификатор семантики (N голосов / majority / abstain / цитата)
  → `Classifier` `spec_flow_diagnosers.py:173`, `SEMANTIC_CAUSES` :155
- [x] Ф5: швы integrate + scope-lint + leaf_check (`Finding`)
  → scope `spec_flow_runner.py:2795`, integrate :2494, resolve :2689/:2832
- [x] Ф6: контур — лестница, кольцо колебаний, ранжирование, потолок, передиагноз
  → `is_oscillation` `spec_flow_doctor.py:139`, `rank_causes` :164, `ceiling_reached` :186,
    `treat` :262, `find_root` :190

Параметры и конфиги (case-YAML + флоры в `tests/.test.env`, мердж как `review_policy`):
- [x] Блок `doctor:` — `enabled`(false), `default_ladder`, `causes_order`
  → `DEFAULT_DOCTOR` `spec_flow_remedies.py:108`; кейс `tests/scenarios/p6_micro_notes.yaml`
- [x] Блок `causes:` — на каждую из 15 причин: `detector`, `ladder`, `by_gate`
  → `DEFAULT_CAUSES` (15) `spec_flow_remedies.py:62`; override в p6 (2 причины)
- [x] Блок `evaluator:` — `role`, `model`, `temperature`(0.0), `votes`(1), `majority`, `require_cite`, `semantic_gates`
  → `DEFAULT_EVALUATOR` `spec_flow_remedies.py:36`; блок в p6
- [x] Флоры контура: `SPEC_FLOW_DOCTOR_RING/NODE_ATTEMPTS/NONSHRINK/LEVELS/ATTEMPTS_PER_LEVEL`
  → `doctor_config` env-чтение `spec_flow_remedies.py:164`; `tests/.test.env`
- [x] Профилактика-конфиг: дефолт `temperature`/`votes` для всех воркеров; пороги `leaf_check` в конфиг; карта «сложность→тир»
  → пороги `_leaf_env` `spec_flow_tools.py:76` (+ floors `.test.env`); дефолт temp
    `_with_default_params` `tests/harness/llm_backend.py:317`; карта `complexity_to_tier`
    `spec_flow_remedies.py:215` + p6 `workers.complexity_to_tier`
- [x] Загрузчики `causes_config()/evaluator_config()`; env-override JSON `SPEC_FLOW_DOCTOR_*`
  → `*_config` `spec_flow_remedies.py:164-219`, `_env_json`/`_env_int` :150-/:155-;
    CLI слой-5 `set_overrides` :126, `_apply_doctor_overrides` `tests/lib/run_cases.py:551`
- [x] Шим обратной совместимости: legacy `gates.*` не тронуты
  → `enabled:false` по умолчанию `DEFAULT_DOCTOR` `spec_flow_remedies.py:108`
    (off → весь старый путь без изменений; синтез из gates.* не нужен)

Проверка:
- [x] Юнит: чистая математика контура + мердж конфига `doctor:`
  → `tests/coverage/test_doctor_core.py`, `test_tiers.py`, `test_websearch.py` (89 зелёных)
- [x] Golden/характеризация: `enabled=false` по умолчанию (обратная совместимость) + карта «причина→первое лечение» 15 причин + завершаемость каждой лестницы
  → `tests/coverage/test_doctor_golden.py` (4 зелёных); контур/`treat` уже в `test_doctor_core.py`
- [x] Исполняемый `reconcile_check` (Ф6): доктор не только диагностирует, но ЛЕЧИТ
  → `_remedy_reconcile_check` `spec_flow_runner.py`: пере-собирает заявленную точку
    входа через реального воркера (`_invoke_implementer` на спеке `_assembly_node`) +
    boot-gate; зелёный boot → снять корневой fail + `_doctor_resolve` (красная→зелёная).
    `_doctor_advise` возвращает Action, пред-гейт исполняет. Commit `3bab846`.
- [x] Контракт продукта ВЫВОДИТСЯ из человеческого текста (не хардкод, не конфиг-хинт)
  → `_product_contract()` парсит маршруты/энтри/boot из constitution+goal+инъекций;
    движок не знает «web/app.py/wsgi/health/notes/ui», ключ `product:` не читается;
    нет HTTP-описания → нет сборки/boot/reconcile (библиотека/CLI). Boot-probe
    параметризован выведенным контрактом. Тесты assembly_node на выводе. Commit `3bab846`.
- [x] Шов integrate ИСПОЛНЯЕТ вердикт доктора (не advisory) — критичный фикс из v044
  → пред-гейт «entry not built» пишет корневой `integrate-fail`; `_doctor_open_causes()`
    `spec_flow_runner.py` ORится в `root_red` → открытая причина блокирует зелёный COMPLETE.
    v044: COMPLETE/зелёный над битой сборкой (500 no-such-table) → v045 (тот же чекпоинт):
    NOT complete / RED. Commit `2e0f70e`.
- [~] E2E p6 (как v041): `delete_note` получает причину, проходит лестницу; красная→зелёная
  → ЧАСТИЧНО: негативная половина доказана (v045 — битая сборка теперь RED, не COMPLETE).
    Позитивная (полный product-прогон до зелёного рабочего продукта) — v046 идёт (отвязанно,
    depth=product, doctor on)
- [ ] Обновить указатель сабмодуля spec-flow в родителе
  → pending (только после зелёного E2E v046)
