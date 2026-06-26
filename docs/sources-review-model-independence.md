# Обзор источников: как сделать spec-flow надёжным независимо от качества модели

> Канонический свод. Заменяет разрозненные заметки по теме (см. также
> `analysis.md` — разбор исходного чата про SDD в Hermes).
> Обновлено: 2026-06-26, по итогам стабильность-цепочки p6 (v086–v088).

---

## 0. Контекст — наши текущие траблы и уже накопленный опыт

Движок: `spec → decompose → implement → integrate → boot-verify`, собирает
крошечный WSGI-сервис из листьев-модулей. Параллельные листья в worktree,
doctor-петля починки, boot-gate как оракул.

**Что УЖЕ закрыто (опыт разработки плагина):**
- git-сиротение кода листьев и split продукта — закрыты 7 фиксами
  (merge-авторитет worktree + инвариант потери кода + консолидация ривала).
  Подтверждено на 3/3 прогонах: `merge>0 / merge_failed=0`, инвариант молчит.
- Контракт продукта выводится **детерминированно из человеческого текста**
  (`_product_contract` — чистый regex/AST, не из config).
- Карта публичного API листьев (`_existing_src_api`) и детектор второго
  WSGI-entry (`_rival_wsgi_entries`) — **чистый AST**, без исполнения.
- boot-gate (`_ROOT_BOOT_PROBE`) гоняет реальный продукт: 200 на health,
  HTML-страница, round-trip персистентности, **кривое тело → 4xx, не 5xx**.

**Что ОСТАЛОСЬ хрупким — единственный генеративный шов:** саму точку входа
(WSGI-роутер) пишет LLM по текстовой директиве. Под слабой/деградировавшей
моделью именно здесь три повторяющихся мода:

| Мод | Симптом | Пример |
|-----|---------|--------|
| **M1** | `json.loads` без try/except → **500** вместо 4xx | v086 |
| **M2** | поздний маршрут (`/ui`, `/about`) не вшит в entry → **404** | v087 |
| **M3** | декомпозер пере-расщепил, ни один лист не даёт WSGI-callable → продукта без точки входа | v088 |

Наблюдение из v088: все 6 листьев единообразно пишут бизнес-логику как
`def handler(payload, query) -> (status, body)` — не хватило **только**
роутера-диспетчера. Логика готова, шов пуст.

**Главный вывод из всей литературы ниже (совпадает у всех источников):**
надёжность даёт **структура и заземление, а не качество модели**. Порядок
приоритета: **(1) снять ответственность с модели → (2) компенсировать сэмплингом
→ (3) и только в конце — итеративная починка**. Self-repair при слабой модели —
последний рубеж, а не фундамент (слабая модель в петле часто делает хуже).

---

## 1. Прямо присланные источники

### 1.1 reharness / «reasoning compiler» (bes-dev) — Apache-2.0, TypeScript
Ссылки: <https://bes-dev.com/posts/reasoning-compiler/> ·
<https://github.com/bes-dev/reharness> ·
разбор на русском: <https://telegra.ph/Obzor-stati-From-App-Factories-to-a-Reasoning-Compiler-06-15>

**Суть.** «Компилятор рассуждений»: один раз потратить reasoning, чтобы
скомпилировать задачу в **детерминированный конечный автомат**, и потом гонять
его без повторной траты токенов. Девиз — *«control flow живёт в коде, модель
отвечает только за суждение в листьях»*. Состояния `code` детерминированы,
стохастика только в `agent`-листьях. Перед запуском — **статический анализ
плана-графа**: достижимость (BFS), **definite-assignment dataflow**
(use-before-def, «не читать то, что не записано»), отсутствие тупиков,
обязательная конечная граница циклов. Wiring данных выводится из топологии, не
объявляется руками.

**Релевантность нам.** Это **наш же тезис**, подтверждённый и формализованный.
Но у нас **уже есть** иерархический FSM (`node_engine="fsm"`, composites
parallel/loop/call) — рантайм reharness дублирует то, что есть.

**Берём (как идею, переписываем на Python — методы общеизвестны):**
- **Статический анализ плана-графа ДО кодогенерации**: достижимость +
  definite-assignment (каждый объявленный маршрут имеет лист-производитель;
  ровно один лист даёт entry-callable; нет несвязанных маршрутов). Это ловит
  **M3 на этапе плана**, до траты токенов. ← усиливает наш контракт-гейт.
- Принцип «модель только в листьях, glue — код» ← фундамент детерминированного
  роутера (M1/M2/M3 структурно).

**Отвергаем:** сам инструмент/рантайм reharness (TS, отдельный FSM, XML-IR,
мультиязычный фронтенд) — внедрение = переписать наш движок. Методы анализа
общие, копировать Apache-2.0-код не нужно.

### 1.2 GRACE / grace-marketplace (osovv) — MIT, TypeScript/Bun
Ссылки: <https://github.com/osovv/grace-marketplace> ·
разбор на русском (фреймворк создания кода LLM в больших контекстах):
<https://m.vk.com/@turboplanner-grace-freimvork-sozdaniya-koda-llm-v-bolshih-kontekstah-s-uc>

**Суть.** «Graph-RAG Anchored Code Engineering» — **contract-first**,
*process-first, not prompt-first*. Стадии: `grace-init → requirements.xml /
technology.xml → grace-plan → grace-verification → lint/status gates →
grace-execute`. Артефакты: `development-plan.xml` (модули, контракты, порядок),
`verification-plan.xml`, `knowledge-graph.xml`. Файл-локальная разметка
`MODULE_CONTRACT`/`MODULE_MAP`. Автономия = «governed mode, проходящий явный
readiness-gate». `operational-packets.xml` фиксирует stop-conditions, retry-budget,
checkpoint-поля.

**Берём (идеи):**
- **Контракт как явный артефакт между decompose и implement** (карта
  «маршрут→хендлер» с обязательными полями) ← прямо под наш план.
- **Verification planned, named, reused** — наш boot-gate уже такой; закрепить,
  что он один на всех и переиспользуется (не импровизируется на узел).
- **Readiness-gate как дешёвый pre-flight** ← наш статический анализ плана.
- Нормализованные ID/якоря для grep-точного роутинга (на будущее, для крупных
  проектов p4/p5).

**Отвергаем:** GRACE как инструмент (TS/Bun, своя skill-система, XML-артефакты) —
у нас Python-движок и свой контракт-вывод из человеческого текста. Граф-RAG
навигация — избыточна для нашего масштаба продуктов.

### 1.3 GitHub Spec Kit + экосистема Spec-Driven Development
Ссылки: <https://github.com/github/spec-kit>,
<https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/>,
Spec Kit Agents (arXiv 2604.05278)

**Суть.** Спека → план → задачи → реализация, со стадиями `analyze`/`clarify`,
context-grounding на каждой фазе. «Вынеси структуру наружу, оставь модели
заполнение слотов».

**Берём:** подтверждение «каркас + наполнение слотов»; идея фазы `clarify`/
`analyze` как гейта перед кодом (у нас частично есть desire-to-goal + контракт).

**Отвергаем:** Spec Kit как тулчейн (markdown-команды для Copilot/Cursor) — мы не
интерактивная IDE-обвязка, а автономный движок.

---

## 2. Источники из веб-исследования (приоритет по ROI)

### 2.1 Template-based assembly / контракт→glue — **берём, ядро решения**
fastify-openapi-glue <https://github.com/seriousme/fastify-openapi-glue>,
OpenAPI Generator <https://github.com/OpenAPITools/openapi-generator>,
template-based FastAPI кейс (arXiv 2603.21439).

**Суть.** Routing, сериализация, обработка ошибок генерируются **из контракта**
(список маршрутов + схемы), а не доверяются модели; модель пишет только тела
бизнес-логики. Наивная генерация эндпоинтов рассинхронит пути/методы/поля —
митигатор именно template-based.

**Берём:** движок **сам** детерминированно генерирует WSGI-роутер из контракта.
Закрывает **M2 и M3 структурно** (entry и роутер не пишет модель → не может их
«забыть»), а guard тела закрывает **M1**. Это пункт №1 плана.

### 2.2 Детерминированный AST/libcst пост-процессор — **берём, страховочная сетка**
APR survey (arXiv 2506.23749), AST-to-AST repair templates.

**Суть.** После генерации — детерминированный проход по AST, чинит типовые
дефекты: обернуть `json.loads` в try/except→400, гарантировать экспорт
entry-callable (`application = wsgi_app`), дособрать роутер из найденных
хендлеров. ~11–13% LLM-дефектов — мелкие локализованные правки, идеальные под
кодемод.

**Берём:** как **гарантированную сетку поверх** любого entry (LLM-написанного или
синтезированного) — лечит **M1** вообще без модели. Пункт №2 плана.
**Риск (учитываем):** пост-процессор может замаскировать глубокую поломку
декомпозиции — поэтому только ПОВЕРХ каркаса, не вместо гейта.

### 2.3 Контракт-валидация декомпозиции + structured output на план — **берём**
Spec Kit analyze/clarify; constrained-generation (ниже).

**Берём:** декомпозер обязан вернуть план в строгой JSON-схеме; гейт проверяет
инвариант «ровно один лист даёт entry-callable, все маршруты покрыты» (тот же
definite-assignment из reharness). Ловит **M3 в корне**, до кода. Пункт №3 плана.

### 2.4 Constrained / structured / type-constrained decoding — **берём частично**
arXiv 2508.15866, 2504.09246; SynCode.

**Суть.** Маскирование токенов по грамматике даёт синтаксис «by construction».
**Берём только** structured-output (JSON-схема на ОТВЕТ модели) для плана
декомпозиции — это единственное применимое к нам (грамматика тел кода требует
доступа к decoding, которого у нас нет: чужой API, free-пул).
**Отвергаем:** grammar-constrained decoding для тел функций — нет доступа к
инференсу; и синтаксис ≠ семантика, наши M1/M2/M3 — семантические.

### 2.5 Best-of-N с executable-селектором по boot-verify — **берём как компенсатор**
AlphaCodium (arXiv 2401.08500), ограничения self-consistency.

**Суть.** Сгенерировать N кандидатов, **выбрать первого прошедшего boot-verify**
(селектор = реальный тест, НЕ majority-vote, НЕ самооценка). Слабые модели
выигрывают от best-of-N сильнее сильных.

**Берём:** для остаточной вариативности листьев (после каркаса) — наш
`creator_ensemble` уже даёт N кандидатов; добавить селектор по boot-gate. Пункт №4
(ниже приоритетом — каркас может сделать его ненужным для entry).
**Нюанс:** классический self-consistency (голосование) к open-ended коду неприменим.

### 2.6 Flow-engineering / AlphaCodium — **берём идеи тестов, осторожно по цене**
arXiv 2401.08500, Qodo blog.

**Суть.** Не один промпт, а поток: само-рефлексия над спекой → генерация доп.
тестов → run-fix. GPT-4 pass@5 19%→44%, работает и на open-source.

**Берём:** AI-генерённые тесты на наши три мода (кривое тело, поздний маршрут,
нет entry) как ранний сигнал. **Цена:** больше вызовов — на free-пуле терпимо.

### 2.7 Self-repair / self-debug — **берём как ПОСЛЕДНИЙ рубеж, с жёстким лимитом**
Olausson «не серебряная пуля» (2306.09896), iterative self-repair (2604.10508),
RepairAgent ICSE'25, Revisit Self-Debugging (2501.12793).

**Суть.** Запустить → собрать ошибки → дать модели починить → повтор. Наш
boot-verify + doctor уже это.
**Предел (важно для слабой модели):** diminishing returns после ~3–5 итераций;
**слабая модель вносит НОВЫЕ баги при починке**; oracle-тесты завышают
реальную способность.
**Берём:** держим как внешнюю петлю поверх каркаса, лимит ~3–5 (у нас
`integrate_max_rework`), богатый сигнал (точная трасса). **НЕ фундамент.**

### 2.8 OpenHands / Software Agent SDK — **к сведению, не внедряем**
arXiv 2511.03690.

Зрелый агент-SDK; подтверждает «структура + грунтование». Своего рантайма не
заимствуем — у нас свой движок.

---

## 3. Сводка: берём / отвергаем

| Источник | Вердикт | Что именно |
|----------|---------|-----------|
| reharness (reasoning compiler) | **идея** | статический анализ плана-графа (reachability + definite-assignment); «модель только в листьях» |
| GRACE marketplace | **идея** | контракт как явный артефакт; verification named+reused; readiness-gate |
| Spec Kit / SDD | **идея** | каркас+слоты; фаза clarify/analyze как гейт |
| template-based / OpenAPI glue | **ВНЕДРЯЕМ** | детерминированный роутер из контракта (ядро, M2/M3) |
| AST/libcst пост-процессор | **ВНЕДРЯЕМ** | кодемод: guard `json.loads`→400, экспорт callable (M1) |
| контракт-гейт декомпозиции | **ВНЕДРЯЕМ** | инвариант «один entry + все маршруты покрыты» (M3 в корне) |
| structured-output на план | **ВНЕДРЯЕМ** | JSON-схема ответа декомпозера |
| best-of-N + boot-селектор | **ВНЕДРЯЕМ позже** | селектор кандидатов по boot-gate |
| AlphaCodium flow / AI-тесты | **частично** | доп. тесты на 3 мода |
| self-repair петля | **уже есть, лимитируем** | doctor + boot-verify, лимит 3–5 |
| grammar-constrained decoding (тела) | **ОТВЕРГАЕМ** | нет доступа к инференсу; синтаксис ≠ семантика |
| reharness/GRACE/SpecKit как инструменты | **ОТВЕРГАЕМ** | чужой стек (TS/Bun), дублируют наш Python-FSM |

---

## 4. Что это меняет в плане движка (привязка к фазам)

Порядок намеренный — сначала снять ответственность с модели:

- **Фаза 1 (ядро, закрывает M1/M2/M3 детерминированно):** движок сам генерирует
  WSGI-роутер из контракта (`_synthesize_entry_code` + `_resolve_route_handlers`),
  парсинг тела с guard→400, диспетч по таблице объявленных маршрутов, фикс
  ABI хендлера листа `(payload, query) -> (status, body)`. Детерминированно-первый,
  LLM — фолбэк. Закрыть v088-щель (assembly при entry-без-callable). ← источники 2.1, 1.1
- **Фаза 2 (сетка):** AST-пост-процессор `_harden_entry` (guard json + экспорт
  callable) поверх любого entry. ← источник 2.2
- **Фаза 3 (гейт плана):** статический анализ декомпозиции (reachability +
  definite-assignment: один entry-производитель, все маршруты покрыты) до кода. ←
  источники 1.1, 2.3, 2.4
- **Фаза 4 (компенсатор):** best-of-N листьев с селектором по boot-gate. ←
  источник 2.5

Boot-gate остаётся честным оракулом: round-trip персистентности роутер подделать
не может — реальная логика листьев обязательна. Это согласуется с правилом
«красный честный > зелёный лживый».

### Источники (полный список ссылок)
- reasoning compiler: <https://bes-dev.com/posts/reasoning-compiler/> · <https://github.com/bes-dev/reharness> · разбор: <https://telegra.ph/Obzor-stati-From-App-Factories-to-a-Reasoning-Compiler-06-15>
- GRACE: <https://github.com/osovv/grace-marketplace> · разбор: <https://m.vk.com/@turboplanner-grace-freimvork-sozdaniya-koda-llm-v-bolshih-kontekstah-s-uc>
- Spec Kit: <https://github.com/github/spec-kit> · <https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/> · Spec Kit Agents (arXiv 2604.05278)
- AlphaCodium: <https://arxiv.org/abs/2401.08500> · <https://www.qodo.ai/blog/qodoflow-state-of-the-art-code-generation-for-code-contests/>
- template-based FastAPI: <https://arxiv.org/pdf/2603.21439> · fastify-openapi-glue <https://github.com/seriousme/fastify-openapi-glue> · OpenAPI Generator <https://github.com/OpenAPITools/openapi-generator>
- constrained decoding: <https://arxiv.org/abs/2508.15866> · <https://arxiv.org/pdf/2504.09246> · SynCode <https://openreview.net/forum?id=L6CYAzpO1k>
- self-repair: <https://arxiv.org/html/2604.10508> · <https://www.emergentmind.com/papers/2306.09896> · <https://arxiv.org/pdf/2501.12793> · RepairAgent <https://software-lab.org/publications/icse2025_RepairAgent.pdf> · APR survey <https://arxiv.org/pdf/2506.23749>
- OpenHands: <https://arxiv.org/html/2511.03690v1>
