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

---

## PROGRESS + DECISIONS (2026-06-16, до компакта)

### Сделано и запушено (ветка feat/roadmap-phase-1)
- `f091907` **Фаза 1**: call_start/ok/error внутри `ask()` (+ параметр `meta`);
  `llm_log.timed_ask` УДАЛЁН; ручной call_start верификатора убран; decomposer/
  implementer/judge/`_call_model`/verifier зовут дверь напрямую. Инвариант
  `tests/coverage/test_single_llm_door.py` (без monkeypatch).
- Тест-фреймворк БЕЗ подмен кода (решение пользователя «а+в»):
  - `tests/harness_fakeapi.py` + фикстура `fake_openai(script)` — реальный
    локальный HTTP-сервер OpenAI-протокола, отдаёт по заказу 429/500/200.
  - `real_bifrost` — живой bifrost :8080, модель `openrouter/xiaomi/mimo-v2-flash`
    (skip если недоступен).
  - `dead_endpoint` — недоступный адрес → реальная ошибка сети.
  - все три в `tests/conftest.py`.
- Переведены БЕЗ monkeypatch: `tests/llm/test_llm_backend.py` (5/5),
  `tests/nodes/test_cycle_fallbacks.py` (7/7).
- `9915e85` узел сборки `code_target=src/app.py` + AST-API (v026 собрал app.py).
- `9f561a0` дашборд namespace-устойчивый `_run_active`.
- live_dashboard «Сбои» теперь читает живые `call_error`/`provider_fallback`
  (закоммичено отдельно).

### ЖЁСТКОЕ правило тестов (память feedback_specflow_no_monkeypatch_real_seams)
НЕТ monkeypatch/stub/smoke на ЛЛМ-пути. claude-путь — реальный
`headroom wrap claude` + haiku (НЕ фейковый claude-скрипт; синтаксис: claude
args после headroom-опций, `-p` конфликтует с `--port` headroom — разобраться).

### Осталось (пофайлово, набор зелёный после каждого)
Снос ЛЛМ-monkeypatch в 10 файлах: `test_specialist_config`, `test_worker_config`
(claude-кроссовер → headroom-haiku или bifrost claude-haiku-4-5 по HTTP),
`test_orchestra`, `test_orchestra_remote`, `test_role_workers` (патчит
`_run_claude`), `test_memory_learning`, `test_memory_modes`,
`test_verification/test_pytest_verifier`, `test_review_artifacts`,
`test_parallel_children`, `test_spec_lint`. ОТДЕЛЬНЫЙ слой: снос НЕ-ЛЛМ заглушек
в оркестр-тестах (`_stub_orchestra_machinery`: run_suite/_leaf_bar/
_write_reply_files/git) → реальный pytest+git.

Двери: Фаза 2 (провайдер `_remote_call` ЗВЕНОМ в `ask(fallbacks=)`, убрать
`provider_fallback`), Фаза 3 (`llm_attempt` на claude/provider), Фаза 4
(`_run_claude` в llm_backend), Фаза 5 (дашборд + значок `⚙️` техн-инъекций
checkpoint/verify/product_entry, источник НЕ hitl/requirements, вместо `📌`).

Шаблон перевода: заменить `monkeypatch.setattr(lb,"_http_post"/"ask"/...)` на
`fake_openai([(200, ok(json.dumps(reply)))])` (контролируемый ответ) ИЛИ
`real_bifrost` (живой) ИЛИ `dead_endpoint` (сбой); ассертить по `srv.requests`.

---

## PROGRESS 2026-06-16 (сессия после компакта)

### Снос ЛЛМ-monkeypatch — СДЕЛАНО 7 файлов (+2 ранее)
test_specialist_config, test_spec_lint, test_memory_learning,
test_parallel_children, test_pytest_verifier, test_memory_modes,
test_orchestra_remote — переведены на реальные семы (`fake_openai`/
`real_bifrost`/`dead_endpoint`). Ранее: test_llm_backend, test_cycle_fallbacks.
Коммиты: 653b48c, 8beb3fb, 9fdcc54, 755db6f.

`harness_fakeapi.FakeOpenAI` доработан: `ThreadingHTTPServer` + lock + `delay`
+ `peak_concurrency` (реальный замер одновременных вызовов клиента).
Модель роли в тестах резолвится на `openrouter/x:free` через конфиг-сем
`_model_for`/`chain_for` (не подмена вызова) — обход free-only гейта.

### Решение юзера по порядку: СНАЧАЛА Фаза 4, потом весь снос claude-плеча разом
~Половина стабов worker_config(21)+role_workers(20) — claude-плечо
(_ask_claude/_run_claude/subprocess). Детерминированно «по-настоящему»
тестируется только после унификации claude-бэкенда (Фаза 4).

### Фаза 2 (#76) — ЗАКРЫТА (727d3a5)
Провайдер сложен в дверь: `ask(provider=..., provider_call=...)` — адаптер
первое звено цепочки; при отказе обычный `llm_fallback` (с полем provider) и
проваливание в локальную цепочку. `_call_model` больше не делает свой
try/except+`provider_fallback` на chat-пути. Весь набор зелёный (883).

### Фаза 4 (#78) — ЧАСТЬ 1 ЗАКРЫТА (133d14e): claude через HTTP-шлюз
`workers.claude_gateway {base_url, api_key?, model_map?}` (или env
`SPEC_FLOW_CLAUDE_GATEWAY`) → `_ask_one` гонит `claude/<m>` по тому же
реальному HTTP-пути (`_ask_openai` с per-call base_url/api_key) на шлюз
(bifrost отдаёт `anthropic/claude-haiku-4-5`). По умолчанию выкл → CLI
(prod не тронут). Это РАЗБЛОКИРУЕТ детерминированный тест claude-плеча
(реальный 429/200). Тест: `llm/test_claude_gateway.py` (3 шт, вкл. реальный
chain-fallback). Набор зелёный (511).

ЧАСТЬ 2 Фазы 4 (ОСТАЛОСЬ): перенос АГЕНТСКОГО `_run_claude` (CLI с tools/MCP) в
llm_backend как claude-бэкенд под дверью. Это CLI-only (агентские tools нельзя
по plain-HTTP), prod-путь дефолтного non-chat режима — РИСКОВО, нужен реальный
прогон headroom-haiku для верификации. Отдельный аккуратный шаг.

### Дальше (порядок)
1. #80 разом: worker_config + role_workers claude-плечо — через
   `configure_workers({"claude_gateway": {"base_url": srv.base_url}})` +
   `fake_openai`/`real_bifrost`; openai-плечо — `fake_openai` сразу.
   role_workers `_run_claude` агентские — после ЧАСТИ 2 Фазы 4 или real headroom.
2. Фаза 3 (#77): `llm_attempt` на claude-CLI/provider путях + лог ответов.
3. ЧАСТЬ 2 Фазы 4: агентский `_run_claude` в дверь.
4. Фаза 5 (#79): дашборд на унифицированные события + значок `⚙️` техн-инъекций.

## PROGRESS 2026-06-16 (продолжение, claude-плечо разом)

### worker_config.py — ЗАКРЫТ ПОЛНОСТЬЮ (cac52e7 + 305870a)
Все 21 LLM-door стаба снесены: chain/budget/logging — через `fake_openai` +
`_gw(srv)` (claude через шлюз на тот же сервер); 4 ротационных — через
model-aware роутер `FakeOpenAI(callable)`, ассерт по порядку моделей на проводе;
второй свободный провайдер `_ORC` (openrouter_custom, requests_per_day) пускает
не-`:free` ротационную модель через гейт. Изоляция: `_free_down_until=0` в autouse
(реальный 429 ставит кулдаун — не должен течь между тестами). Остался ТОЛЬКО
OS-сем `subprocess.run` в тесте зависшего CLI (инъекция сбоя процесса, не фейк
ответа — задокументировано). `FakeOpenAI` теперь принимает callable-роутер
`(payload)->(status,body)`.

### role_workers.py — ОСТАЛОСЬ (последний файл, 20 стабов)
- ~15 мигрируемы СЕЙЧАС: стабят `ask`/`_ask_openai`/claude-фоллбек → `fake_openai`
  + `_gw`/модель-free через `_model_for`.
- 5× `_run_claude` (93,115,219,242,258) — АГЕНТСКИЙ CLI-путь (tools/MCP), блокирован
  Фазой 4 ЧАСТЬ 2 (перенос агентского `_run_claude` в дверь; CLI-only, рисковый
  prod-путь non-chat режима, нужен живой headroom-haiku для верификации).

### Порядок остатка
1. role_workers: снести ~15 не-агентских стабов (как worker_config).
2. Фаза 4 ЧАСТЬ 2: агентский `_run_claude` в дверь → разблокирует 5 оставшихся.
3. Фаза 3 (#77): `llm_attempt` на claude/provider + лог ответов (claude-http уже
   логирует llm_attempt через `_ask_openai`; CLI-путь — нет).
4. Фаза 5 (#79): дашборд на унифицированные события + значок `⚙️` техн-инъекций.

## PROGRESS 2026-06-16 (role_workers закрыт, #80 почти весь снят)

### role_workers.py — 13 из 20 снесено (45ed51b)
Все chat-тесты (implementer/reviewer/decomposer, prompt-capture, repair с
реальным pytest) + quota→haiku фоллбек с кулдауном — на `fake_openai`/`_gw`/
model-aware роутер + `_free_model` (резолвер роли на free id). Autouse-сброс
`claude_gateway`+`_free_down_until`. ОСТАЛОСЬ 7: 5× агентский `_run_claude` +
1 CLI-direct-bypass тест (`_ask_openai`/`_ask_claude`, проверяет direct=True
обход шлюза) — все блокированы Фазой 4 ЧАСТЬ 2 (агентский claude в дверь).

### Итог #80
Снято полностью: test_llm_backend, test_cycle_fallbacks, test_specialist_config,
test_spec_lint, test_memory_learning, test_parallel_children, test_pytest_verifier,
test_memory_modes, test_orchestra_remote, **test_worker_config (21/21)**.
Снято частично: **test_role_workers (13/20)** — 7 хвостов на Фазе 4 ч.2.
Остатки-исключения (НЕ LLM-reply фейки): OS-сем `subprocess.run` (зависший CLI),
CLI-direct-флаг. Полный набор зелёный: 886 (без live).

### Осталось всего
1. Фаза 4 ЧАСТЬ 2: агентский `_run_claude` (CLI+tools) в дверь как claude-бэкенд
   → разблокирует 6 хвостов role_workers (5 _run_claude + CLI-direct). Рисковый
   prod-путь non-chat режима — нужен живой headroom-haiku для верификации.
2. Фаза 3 (#77): `llm_attempt` на claude-CLI пути + лог ответов (claude-http уже
   логирует через `_ask_openai`).
3. Фаза 5 (#79): дашборд на унифицированные события + значок `⚙️` техн-инъекций.
4. #81: снос НЕ-ЛЛМ заглушек оркестра (реальный pytest/git) — отдельный пласт.

## PROGRESS 2026-06-16 (Фазы 3 и 5 закрыты)

### Фаза 3 (#77) — ЗАКРЫТА (bb27018)
`_ask_claude` (CLI-путь) теперь шлёт `llm_attempt` на каждую попытку
(success/timeout/spawn_error/empty) — зеркало `_ask_openai`. Дашборд видит
причины задержек и на subscription-пути. Additive, поведение не менялось. Тест
через OS-сем subprocess.

### Фаза 5 (#79) — ЗАКРЫТА (8fe3d0b)
Служебные узлы движка (`_TECHNICAL_TASKS`: checkpoint/verify/product_entry/
boot/assembly/integrate/contract) получают значок `⚙️` (своя подсказка) в
дереве И в SVG-графе; человеческий вброс требования (web_ui) остаётся `📌`.
Флаг `technical` ставится в `_attach_orphan_nodes`, проброшен через
`_tree_view`. Тест в dashboard/test_dashboard_orphan_nodes.py. Набор 63 зелёный.
ВНИМАНИЕ: живому дашборду нужен рестарт по ТОЧНОМУ PID чтобы показать ⚙️.

### Состояние: закрыты Фазы 1,2,3,4-ч.1,5 + #80 (кроме 7 хвостов)
ОСТАЛОСЬ:
- Фаза 4 ЧАСТЬ 2 (#78): агентский `_run_claude` в дверь. ПЕРЕПЛЕТЕНО:
  `_chat_only`-гейтинг + маршрутизация BACKEND + шлюз-для-агентского (строка 308
  `_ask_one` НЕ проверяет gateway — надо добавить) + tool-флаги + инвариант
  role/step. Единственный prod-вызов — role_worker.py:297. Тест-миграция 5
  агентских тестов: можно через `claude_gateway` (агентский worker отправит
  собранные system+prompt в HTTP-тело, тест прочтёт с провода) — НО надо чтобы
  `_ask_one` строка 308 уходила в gateway, и `_chat_only` был False. Рисковый
  prod-путь, нужна живая headroom-haiku верификация. Делать аккуратно, свежим
  бюджетом.
- #81: снос НЕ-ЛЛМ заглушек оркестра (`_stub_orchestra_machinery`: run_suite/
  _leaf_bar/_write_reply_files/git) → реальный pytest+git. Отдельный пласт.

## ЗАВЕРШЕНО 2026-06-16: дверь ЛЛМ консолидирована полностью (ca44ec6)

### Фаза 4 ЧАСТЬ 2 — ЗАКРЫТА: агентский claude в дверь
Агентский non-chat путь (бывший `role_worker._run_claude` — CLI-сессия с tools)
теперь через `llm_backend.ask(..., tools=, cwd=)`: `_ask_claude` получил
allowed/disallowed/cwd (агентская команда), `_ask_one` пробрасывает tools/cwd и
при `claude_gateway` шлёт агентский бэкенд по тому же HTTP, `_call_model`
non-chat зовёт `ask()` как chat (провайдер сложен, single-door логирование).
Мёртвый `_run_claude` удалён. 5 агентских тестов + question/operator —
мигрированы через `_agw(srv)` (BACKEND=claude + claude_gateway→сервер), читают
собранные system+prompt с провода. ВЕРИФИЦИРОВАНО ВЖИВУЮ на bifrost
(anthropic/claude-haiku-4-5 → 'AGENTIC').

### #80 — ЗАКРЫТО полностью
Все LLM-door monkeypatch сняты. Исключения (легитимны, НЕ LLM-reply фейки):
OS-сем subprocess.run (hung-CLI + CLI-attempt-лог тесты) + 1 CLI-direct тест
(наблюдает флаг direct через стаб _ask_claude — тест роутинга _ask_one).

### Итог: Фазы 1,2,3,4(ч1+ч2),5 + #80 — ВСЁ ЗАКРЫТО. Набор 888 зелёный (без live).
ОСТАЛОСЬ: #81 — снос НЕ-ЛЛМ заглушек оркестра (`_stub_orchestra_machinery`:
run_suite/_leaf_bar/_write_reply_files/git → реальный pytest+git). Отдельный пласт.
