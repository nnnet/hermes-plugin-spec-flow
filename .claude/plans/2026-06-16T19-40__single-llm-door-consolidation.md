# План: СТРОГО ОДНА точка обращения к ЛЛМ + фоллбеки параметром + всё логируется внутри

> Финальное место (конвенция проекта): после выхода из plan mode скопировать в
> `spec-flow/.claude/plans/2026-06-16T19-40__single-llm-door-consolidation.md`,
> `git add`, удалить из `~/.claude/plans/`. Ветка `feat/roadmap-phase-1`.

## Context

Требование пользователя (дословно): **строго одна точка обращения к ЛЛМ**; внутри
неё логируется КАЖДЫЙ вызов, КАЖДЫЙ фоллбек, ЛЮБОЙ ответ — всё; **фоллбеки
(провайдер + модель) переносятся в эту точку параметром.**

Сейчас (карта по коду) точка размазана на 3 слоя и есть прямой обход:

1. `llm_backend.ask()` (`tests/harness/llm_backend.py:378`) — гоняет МОДЕЛЬНУЮ
   цепочку `[model, *fallbacks]`, роутит по префиксу через `_ask_one` →
   `_ask_openai`/`_ask_claude`. Логирует `llm_fallback`, `quota_wait`,
   `error_round`, `token_usage`. `_ask_openai` логирует `llm_attempt`.
2. `llm_log.timed_ask()` (`llm_log.py:83`) — ВНЕШНЯЯ обёртка, пишет
   `call_start`/`call_ok`/`call_error`. Оборачивает decomposer/implementer/
   judge/`role_worker._call_model`. То есть логи границы вызова — НЕ внутри ask.
3. `role_worker._call_model._do` (`role_worker.py:236`) — **обходит ask**: при
   `provider != local` зовёт `_remote_call()` (адаптеры Hermes/MC/A2A) напрямую,
   свой `provider_fallback` (line 277); при не-chat-only — свой `_run_claude()`
   (второй CLI-путь, line 313). Только chat-local идёт в `ask()`.
4. `pytest_verifier.py:785` зовёт `ask()` БЕЗ `timed_ask` → нет call_start/ok/error.

Симптомы, которые это уже дало:
- На дашборде во вкладке «Простои» секция «Сбои моделей» пустая — она ждёт
  `llm_attempt`/`llm_fallback`, а живой оркестр шёл мимо `ask` и писал
  `call_error`/`provider_fallback`.
- `llm_attempt` (Часть 1) на живом пути не пишется вообще.

## Цель (инвариант, проверяемый автоматически)

**Каждое обращение к модели/провайдеру/CLI проходит через ОДНУ функцию
`llm_backend.ask()`. Внутри неё — ВСЁ логирование:**
- `call_start` (запрос), `call_ok` (ответ, с длиной/латентностью) или
  `call_error` (исключение) — на КАЖДЫЙ вызов;
- `llm_attempt` — на КАЖДУЮ попытку любого бэкенда (openai-HTTP / claude-CLI /
  provider-адаптер), со статусом/латентностью/abnormal;
- `llm_fallback` — на КАЖДЫЙ переход по цепочке (провайдер ИЛИ модель), включая
  терминальный;
- `token_usage` — на каждый успех.
**Вне `ask()` не остаётся ни одного места, которое зовёт модель или пишет
call_*/llm_*/provider_fallback.** `provider_fallback` исчезает как отдельное
событие — провал провайдера становится обычным `llm_fallback`-переходом.

**Фоллбеки — параметр `ask()`:** единая цепочка `[primary, *fallbacks]`, где
звено может быть провайдером ИЛИ моделью. Деградация «remote-провайдер недоступен
→ локальная модель» = просто следующее звено той же цепочки.

## Жёсткие правила
- Комментарии/строки в коде — English; пути от корня репо; без абсолютных путей.
- Free-only политика моделей не ослабляется; paid-гейт остаётся.
- Тесты зелёные (`pytest tests/ -q`, сейчас 916) после КАЖДОЙ фазы; стабы
  обновляются под новую единую дверь.
- Поведение для дефолт-кейсов (p4/p5/p6) эквивалентно: тот же набор моделей, тот
  же порядок, та же деградация — меняется только ГДЕ это происходит и логируется.

---

## Дизайн единой двери

`ask(prompt, *, model, role, step, system=None, fallbacks=(), params=None,
     provider_ctx=None) -> str`

- Цепочка = `[model, *fallbacks]`. Звено — строка-модель (`openrouter/...:free`,
  `claude/haiku`) ИЛИ провайдерное звено (dict `{"provider": "...", "model": ...}`
  или строка `"provider:model"`).
- `_ask_one(entry, ...)` роутит:
  - провайдерное звено → `_remote_call(...)` (строит `RoleTask` из `provider_ctx`:
    node/title/spec/specialty/cfg, зовёт `get_provider(p).execute(task)`), рендерит
    ответ как раньше;
  - `claude/...` или `BACKEND!=openai` → claude-CLI (см. ниже);
  - иначе → `_ask_openai` (HTTP).
- **`ask()` сам оборачивает весь прогон цепочки** в `call_start` (до) /
  `call_ok` (на возврате, с `reply_chars`+`latency_s`) / `call_error` (на
  терминальном провале) — логика `timed_ask` ВЪЕЗЖАЕТ внутрь `ask`.
- **Каждая попытка** (в т.ч. claude-CLI и provider) логирует `llm_attempt`
  (сейчас только openai). Добавить `llm_attempt` в claude-путь и provider-путь:
  `{event, backend, model/provider, attempt, status, latency_s, abnormal, error?}`.
- claude-tool-policy путь (`allowed`/`disallowed`, cwd) сохранить: `_run_claude`
  переезжает внутрь llm_backend как claude-бэкенд `_ask_one` с прокидыванием
  policy через `provider_ctx`/params (а не отдельной функцией в role_worker).

После этого:
- `role_worker._call_model` схлопывается до: собрать цепочку (provider-звено +
  модельные fallbacks через `chain_for`) и `provider_ctx`, вызвать `ask()` ОДИН
  раз. Удаляются `_do`-ветки, прямой `_remote_call`, событие `provider_fallback`,
  внешний `timed_ask`-врап.
- decomposer/implementer/judge: убрать `timed_ask`-обёртку, звать `ask()` прямо
  (он самологируется).
- `pytest_verifier`: уже зовёт `ask()` прямо — получает полный лог даром.
- `llm_log.timed_ask` остаётся тонким deprecated-passthrough (или удаляется, если
  не останется внешних вызовов) — решить по факту нулевых вызовов.

---

## Фазы (каждая — отдельный коммит, тесты зелёные)

**Фаза 1 — логи границы вызова ВНУТРЬ `ask()`.** Внести call_start/ok/error в
тело `ask` (вокруг прогонки цепочки). Снять `timed_ask`-обёртку с decomposer/
implementer/judge/`_call_model`/verifier. Обновить стабы (`tests/live/
test_live_caps.py` патчит `timed_ask`; перенаправить на `ask`). Инвариант-тест:
один `ask()` даёт ровно один call_start + один call_ok/call_error.

**Фаза 2 — провайдер в цепочку, убрать обход.** `_ask_one` умеет провайдерное
звено (через `_remote_call`+`provider_ctx`). `_call_model._do` удаляется; провайдер
становится head-звеном цепочки `ask`, локальная модель — следующим звеном. Событие
`provider_fallback` заменяется на `llm_fallback`. Обновить
`tests/workers/test_orchestra_remote.py`, `tests/providers/test_external_team.py`
(ассертить llm_fallback + что adapter дернулся через ask).

**Фаза 3 — `llm_attempt` для ВСЕХ бэкендов + ответы.** Добавить `llm_attempt` в
claude-CLI и provider пути (сейчас только openai). На `call_ok` фиксировать ответ
(reply_chars + латентность — переносится из timed_ask). Тест: claude- и
provider-вызов оба пишут `llm_attempt`.

**Фаза 4 — claude-CLI как бэкенд `_ask_one`.** Перенести `_run_claude` (tool-policy
flags) в llm_backend; role_worker больше не держит CLI-путь. Обновить
`tests/workers/test_role_workers.py` (патчат `_run_claude`).

**Фаза 5 — дашборд на унифицированные события + 2 мелких UI-долга.**
- «Простои»/«Сбои» теперь наполняется из `llm_attempt`(abnormal)+`llm_fallback`
  (единый источник). Текущую правку (потребление `call_error`/`provider_fallback`)
  оставить как робастный запас, первичный источник — унифицированные события.
- **Эмодзи/тултип технических инъекций:** `📌`+«позднее требование» (live_dashboard
  ~2112 и SVG ~2531) сейчас на ЛЮБОМ `attached`. Технические узлы (`checkpoint`,
  `verify`, engine-сборка `product_entry`) пометить флагом `technical` (источник —
  НЕ `hitl/requirements/<id>/`) и рисовать иным значком (напр. `⚙️`) + тултип
  «техническая вставка движка (не из HITL)». Правки: `_attach_orphan_nodes`/
  `_node_view` (проброс `technical`), JS-рендер дерева и SVG.

---

## Files to modify
- `tests/harness/llm_backend.py` — `ask()` (логи границы + провайдер-роутинг +
  фоллбеки-параметр), `_ask_one` (провайдерное звено + claude-policy бэкенд),
  `_ask_openai`/`_ask_claude` (`llm_attempt` на всех путях), перенос `_run_claude`.
- `tests/harness/role_worker.py` — `_call_model` схлопнуть в один `ask()`; удалить
  `_do`-ветки, прямой `_remote_call`-вызов, `provider_fallback`, `_run_claude`.
- `tests/harness/llm_log.py` — `timed_ask` → deprecated passthrough/удаление.
- `tests/harness/llm_decomposer.py`, `llm_implementer.py`, `llm_judge.py`,
  `pytest_verifier.py` — убрать внешний `timed_ask`, звать `ask()` напрямую.
- `tests/lib/live_dashboard.py` — отчёт на унифицированные события (уже частично
  правлено) + `technical`-флаг и значок/тултип.
- Тесты (blast radius): `tests/workers/test_orchestra*.py`,
  `test_role_workers.py`, `test_specialist_config.py`, `test_worker_config.py`,
  `tests/providers/test_external_team.py`, `tests/live/test_live_caps.py`,
  `tests/llm/test_llm_backend.py`, `tests/nodes/test_cycle_fallbacks.py`,
  `tests/dashboard/test_dashboard_idle.py`.

## Verification
- `python3 -c "import ast; ast.parse(open(f).read())"` на каждый правленый .py.
- `pytest tests/ -q` зелёный (916 + новые) после каждой фазы.
- **Инвариант одной двери (grep-тест в CI):** в `tests/harness/` НЕТ вызовов
  модели/CLI/адаптера вне `llm_backend` (`subprocess ... claude`,
  `get_provider(...).execute`, `urlopen`/`_http_post` для ЛЛМ — только в
  llm_backend); НЕТ `provider_fallback`; `call_start/call_ok/call_error/
  llm_attempt/llm_fallback` эмитятся только из `llm_backend`.
- **Живой p6** (`run_cases.py run --case p6 --workers real --depth execute`): по
  `llm-log.jsonl` каждый вызов имеет пару call_start→call_ok|call_error; есть
  `llm_attempt` на openai- И claude-путях; провайдерная деградация (если есть)
  идёт как `llm_fallback`, а не `provider_fallback`; на дашборде «Простои» секция
  «Сбои» наполнена; технические узлы показаны иным значком.

## Риск и порядок
Горячий путь + ~12 тест-файлов со стабами. Снимается фазами: 1 (логи внутрь, без
смены маршрутизации) → 2 (провайдер в цепочку) → 3 (attempt на всех) → 4 (CLI
переезд) → 5 (дашборд+UI). После каждой фазы — полный pytest + ast-parse; смену
поведения нигде не вносим, только КОНСОЛИДАЦИЮ места и логов.
