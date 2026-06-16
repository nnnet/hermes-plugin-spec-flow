# План: полный лог ответов LLM + отчёт в «Простоях» + стоп/ран с чекпойнтами-снимками

> Финальное место (конвенция проекта): после выхода из plan mode скопировать в
> `spec-flow/.claude/plans/2026-06-16T18-10__llm-response-log-idle-report-checkpoints.md`,
> `git add`, удалить из `~/.claude/plans/`. Ветка `feat/roadmap-phase-1`.

## Context

Живой p6 показал: wall-clock доминируют LLM-вызовы на проверках (reviewer
nemotron-120b 61–78 с/вызов) + ожидания троттлинга free-пула. Но **429 сейчас
гасятся молча**: `_ask_openai` спит по `retry_after_seconds`/backoff и в лог не
пишет — в `llm-log.jsonl` попадает только агрегат (`quota_wait`, `call_ok/
call_error`, `token_usage`). Поэтому в «Простоях» не видно реальных причин
задержек (сколько времени съели 429, ретраи, медленные ответы, ошибки).

Задача (слова юзера):
1. Логировать **все** ответы/исходы LLM-вызовов (не только 429), чтобы было
   видно все причины задержек и ответов; отдельный отчёт во вкладке «Простои».
2. Добавить в раннер команды **стоп** и **ран**.
3. «стоп» сохраняет состояние прогона снимками на разных хешах спеки;
   воспроизводить можно **с любого** снимка (`run --from <id>`).

Что уже есть (переиспользуем, не плодим): `--stop`/`--resume` в `run_cases.py`,
кнопки `/api/run/stop`/`/api/run/start` на дашборде, журнал `spec_hash` по узлам
(`journal_mark`/`journal_index`), cache-hit на resume по спек-хешу, durable
`claims.db`, STOP-сентинел + кооперативная остановка на границе узла.

## Жёсткие правила
- Пути — от корня репо; комментарии/строки в коде — только English; абсолютных
  путей в коде нет. Секреты не трогаем.
- Дефолты не ломают p4/p5: новые логи — аддитивны; чекпойнты — по требованию
  (флаг/команда), без них поведение прежнее.
- Тесты зелёные (`pytest tests/ -q`, сейчас 901) + юниты на каждый слой.
- Сравнение/проверка — через прогон движка и юниты, не ручной curl к LLM.

---

## Часть 1 — Полный лог исходов LLM-вызовов

**Цель:** каждая HTTP-попытка и терминальный исход вызова видны в `llm-log.jsonl`
с причиной задержки.

Главное (уточнение юзера): нужно ВИДЕТЬ **ненормальные ответы** — когда модель
не ответила как надо (429 / 5xx / timeout / пустой-битый ответ), к чему это
привело (**фоллбек** на следующую модель / `quota_wait` / **терминальная
ошибка**) и **сколько задержки** на это ушло. Каждый исход — в лог.

**Где:** `tests/harness/llm_backend.py`
- `_ask_openai` (цикл попыток ~640–666): на КАЖДОЙ попытке писать `llm_attempt`:
  `{event, backend:"openai", model, provider, attempt, status, latency_s,
  retry_after_s?, slept_s?, error?, abnormal:bool}`.
  - 200 (и непустой ответ) → `status:200, latency_s, abnormal:false`.
  - 429 (654-657) → `status:429, retry_after_s, slept_s, abnormal:true` (молчит).
  - 5xx / прочий не-2xx → `status, slept_s, abnormal:true`.
  - timeout/connect-исключение → `status:0, error, abnormal:true`.
  - пустой/битый ответ (reasoning-only/JSON-fail) → `status:200, abnormal:true,
    error:"empty_or_malformed"`.
  - `provider` — из префикса модели (`openrouter`/`xiaomimimo`/`claude`) или
    `last_call`.
- `_ask_claude` (CLI-путь): `llm_attempt {backend:"claude", model, latency_s,
  abnormal, error?}`.
- **Переход-фоллбек** (главная для отчёта): в `ask()` на точках смены модели
  (483, 507-520) и при `QuotaExhausted`/cooldown (304/312-314) писать
  `llm_fallback`: `{event, from_model, to_model|null, reason
  ("429"/"5xx"/"timeout"/"empty"/"quota_exhausted"), wait_s (сумма slept до
  перехода), terminal:bool}`. `terminal:true` + `to_model:null` = цепочка
  исчерпана → ошибка вызова.
- `ask()` уже пишет `quota_wait`/`error_round`/`call_ok`/`call_error`/
  `token_usage` — оставить; `llm_attempt`+`llm_fallback` дают попыточную картину
  и связь «сбой → к чему привёл».
- `llm_log` импортируется лениво (как в 459-482) — переиспользовать; записи несут
  `t`+`wall` (llm_log.log 28-45) — корреляция с трассой бесплатна.

**Тесты** (`tests/workers/` рядом с `test_worker_config.py`, мок-транспорт, без
сети): модель A отдаёт 429×N→исчерпание → есть `llm_attempt status:429
slept_s>0` и `llm_fallback {from:A, to:B, reason:"429", wait_s>0}`; цепочка
исчерпана → `llm_fallback terminal:true, to_model:null`; пустой ответ →
`abnormal:true, error:"empty_or_malformed"`.

---

## Часть 2 — Отчёт «Сбои моделей → фоллбек/ошибка» в «Простоях»

**НЕ** общая таблица всех ответов. Секция отвечает на: **какая модель сколько раз
не ответила нормально, к чему это привело (фоллбек / quota_wait / ошибка) и
сколько задержки это стоило.**

**Где:** `tests/lib/live_dashboard.py`
- `_idle_analysis` (1182-1475): рядом с разбором `quota_wait` (1244-1248) распарсить
  новые `llm_attempt` (где `abnormal:true`) и `llm_fallback` → агрегаты:
  - `by_model`: модель → {ненормальных ответов, из них 429 / 5xx / timeout /
    пустой, **фоллбеков с этой модели**, **терминальных ошибок**, сумм. задержка
    (slept+ожидание до перехода)}.
  - `transitions`: список `from_model → to_model (reason, wait_s)` — куда уходил
    фоллбек и почему (видно цепочку, например nemotron-120b →429→ haiku).
  - `totals`: всего сбоев, всего фоллбеков, всего терминальных ошибок, суммарная
    потерянная на этом задержка.
  - В возврат (1463-1475) добавить `"failures": {by_model, transitions, totals}`.
- Причина 429 уже мапится как «ожидание квоты» в общей таблице «по причинам» —
  оставить; новый блок — это адресная разбивка СБОЕВ, не дубль.
- Фронт `idleHTML()` (2190-2254): после таблицы «По причинам» (2220) добавить
  секцию **«Сбои моделей → фоллбек/ошибка»**:
  - таблица по модели: `модель · сбоев · 429 · 5xx · timeout · пусто · фоллбеков ·
    ошибок · задержка(с)` из `IDLE.failures.by_model` (сорт по задержке).
  - под ней — компактный список переходов `A →(reason, Ns)→ B` из
    `transitions`; терминальные (to=∅) выделить красным.
  - переиспользовать паттерн `cmp`-таблицы + `causeColor` (2200-2202; добавить
    цвет для «сбой»/«терминальная ошибка»).
  - если сбоев 0 — секцию не рисовать (чисто).
- `/api/idle` (1688-1693) уже отдаёт весь `_idle_analysis` → новое поле само
  попадёт на фронт.

**Тесты** (`tests/dashboard/`): синтетический `llm-log.jsonl` с `llm_attempt`
(429×2 по модели A) + `llm_fallback` (A→B reason 429) + terminal (B→∅) →
`_idle_analysis(...)["failures"]`: A.fallbacks==1, totals.terminal==1, задержка
== сумме slept; прогон без сбоев → `failures.totals` нули и фронт секцию прячет.

---

## Часть 3 — Раннер: стоп/ран + чекпойнты-снимки (replay с любого)

**Решение юзера:** несколько снимков; каждый = копия `workspace/` в
`RUN_DIR/checkpoints/<NNN>__<roothash>/`; `run --from <id>` восстанавливает снимок
и продолжает с него.

### 3.1 Снимок/восстановление (новые хелперы)
**Где:** `spec_flow_runner.py` (рядом с `Workspace`/журналом 880-929) или
`tests/lib/run_cases.py`.
- `snapshot_checkpoint(run_dir) -> path`: копирует `workspace/` (specs/, src/,
  tests/, contracts/, `.spec-flow/journal.jsonl`, `claims.db`, MANIFEST/COMMITS)
  в `run_dir/checkpoints/<seq>__<roothash>/`, где `roothash` = дайджест всех
  текущих спек-хешей (стабильный id «где дерево»), `seq` — инкремент. Пишет
  `checkpoint.json` (seq, roothash, node-count, last tick, спек-хеши).
- `restore_checkpoint(checkpoint_dir, dest_run_dir)`: копирует снимок workspace
  в `dest_run_dir/workspace/`, чтобы движок в режиме resume увидел журнал из
  снимка и закэшировал готовые узлы (cache-hit 2924-2942 — переиспользуем).
- Снимок берём на ГРАНИЦЕ узла (workspace в покое) — консистентно.

### 3.2 Когда создаются снимки (несколько за прогон)
- `stop`: STOP-сентинел (как сейчас, `request_stop`/`_stop_run` 44-63) → движок
  встаёт на границе узла → `run_cases` делает `snapshot_checkpoint`.
- `checkpoint`: новый CHECKPOINT-сентинел — движок на следующей границе узла
  делает `snapshot_checkpoint` и **продолжает** (не завершается). Так за один
  прогон накапливается несколько точек.
- (Авто-снимок каждые N узлов юзер не выбрал — не делаем.)

### 3.3 Команды раннера (verb поверх существующих флагов, back-compat)
**Где:** `run_cases.py` main — пре-парс `argv[1]`:
- `run_cases.py run --case p6 ...` → старт (= текущий путь).
- `run_cases.py run --from <RUN_DIR>/checkpoints/<id> [--case p6]` → restore +
  resume (новый run-dir или in-place).
- `run_cases.py stop <RUN_DIR>` → стоп + финальный снимок (= `--stop` + snapshot).
- `run_cases.py checkpoint <RUN_DIR>` → снимок на следующей границе, без останова.
- `run_cases.py checkpoints <RUN_DIR>` → список снимков (seq, roothash, узлов,
  tick) из `checkpoints/*/checkpoint.json`.
- Если `argv[1]` не verb → текущий argparse (`--case`/`--resume`/`--stop`) — чтобы
  дашборд (`/api/run/start` 1792-1828) и тесты не сломались.

### 3.4 Дашборд (минимум; replay-UI — опционально)
- На вкладке состояния — кнопка **«Чекпойнт»** (`POST /api/run/checkpoint` →
  CHECKPOINT-сентинел) рядом со Stop/Run (1770-1828).
- Список снимков прогона + «Воспроизвести отсюда» (`POST /api/run/start` с
  `from=<checkpoint>`). Можно вынести в follow-up, ядро — в раннере.

**Тесты** (`tests/coverage/` или `tests/lib/`): snapshot→restore round-trip
(копия workspace идентична; `checkpoint.json` корректен); `run --from` поверх
снимка с неизменёнными спеками → все узлы cache-hit (движок не зовёт воркеров);
verb-диспетчер: неизвестный `argv[1]` падает в legacy-флаги (back-compat).

### Ответ на вопрос «можно воспроизвести с этих точек?»
Да. Движок уже умеет resume по журналу спек-хешей (cache-hit пропускает
неизменённые узлы). Снимок сохраняет ИМЕННО то состояние workspace+журнал на
границе узла; `run --from <id>` восстанавливает его и продолжает — то есть
replay с любой сохранённой точки, а не только с последней.

---

## Files to modify
- `tests/harness/llm_backend.py` — `llm_attempt` логирование в `_ask_openai`/
  `_ask_claude`.
- `tests/lib/live_dashboard.py` — `_idle_analysis` (поле `responses`) + `idleHTML`
  секция «Ответы LLM».
- `spec_flow_runner.py` — `snapshot_checkpoint`/`restore_checkpoint` + CHECKPOINT
  сентинел на границе узла (рядом со STOP 189/`request_stop`).
- `tests/lib/run_cases.py` — verb-диспетчер `run/stop/checkpoint/checkpoints` +
  `--from`; снимок на stop.
- (опц.) `tests/lib/live_dashboard.py` — `/api/run/checkpoint` + список снимков.
- Новые тесты: `tests/workers/` (llm_attempt), `tests/dashboard/` (responses-
  отчёт), `tests/coverage|lib/` (snapshot/restore + verb-диспетчер).

## Verification
- `python3 -c "import ast; ast.parse(open(f).read())"` на каждый правленый .py.
- `python3 -m pytest tests/ -q` зелёный (901→+новые) после каждой части.
- Живой p6: `run_cases.py run --case p6 --workers real --depth execute`; затем
  открыть «Простои» → секция «Ответы LLM» показывает 429/латентности/ожидания;
  `run_cases.py checkpoint <RUN_DIR>` во время прогона → появляется
  `checkpoints/001__*`; `run_cases.py run --from <RUN_DIR>/checkpoints/001` →
  по трассе: узлы со снимка = cache-hit, дальше продолжает.
- commit + push после каждой части (ветка `feat/roadmap-phase-1`).

## Риск и порядок
Порядок: **Часть 1 (лог) → Часть 2 (отчёт) → Часть 3 (чекпойнты)**. 1+2 дёшевы и
аддитивны. Часть 3 рискованнее (copy-снимки, CHECKPOINT-сентинел в горячем пути,
verb-диспетчер) — снимается: снимок только на границе узла (консистентность),
verb-диспетчер с fallback в legacy-флаги (back-compat), юниты snapshot/restore +
обязательный живой replay-прогон p6.
