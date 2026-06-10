# spec-flow — анализ источника и решения

> Источник: чат «Реализация ecom-agent в Hermes»
> (https://claude.ai/share/966e2936-6c68-4e4d-8ed5-56d740c46138).
> Полная стенограмма — в `conversation.md`.

## О чём чат

Длинный исследовательский диалог, который прошёл путь от вопроса «как
ecom-agent тюнит Hermes» к проектированию переиспользуемого набора для
**Spec-Driven Development (SDD)** поверх Hermes. Ключевые остановки:

1. **ecom-agent как «тюнинг, а не сборка».** Hermes владеет циклом агента;
   обвязка лишь изолирует тулсеты (`config.template.yaml`), даёт единственный
   MCP-канал, собирает промпт и роутит модель регуляркой (`_route_model`).
2. **Внутренние механизмы Hermes:** `delegate_task` (суб-агенты, изоляция
   контекста, ширина `max_concurrent_children`=3, глубина `max_spawn_depth`
   1–3, watchdog по тишине `child_timeout_seconds`), `clarify`, перехват
   agent-loop-тулзов до реестра, дерево `/agents`.
3. **Kanban vs delegation.** delegation — короткое рассуждение в контексте
   родителя; Kanban — durable DAG задач в SQLite, воркеры = отдельные процессы.
   Глубокие деревья строят на Kanban, а не углублением делегирования.
4. **SDD в Hermes — это скиллы, не MCP-серверы.** `writing-plans`,
   `subagent-driven-development`, `test-driven-development`,
   `requesting-code-review`, `verification-before-completion`.
5. **Сборка набора `spec-flow`** — рекурсивная top-down декомпозиция на доске,
   гейты обратной связи, контракт L2, авто-детект дрейфа, инверсия
   «спека-первой», research-линии (spike + непрерывная ревизия).

## Что собрали в чате (финал — `spec-flow-suite.zip`, 25 файлов)

- Плагин: тулзы `leaf_check`, `contract_check`, `research_trigger_check`;
  команды `specflow init/start/status/revise`; таблица `CONTRACT_VALIDATORS`;
  конфиг `RESEARCH_LANE`.
- Скиллы: `spec-flow-decompose`, `spec-requirements`, `spec-reviewer`,
  `spec-contract`, `spec-implement`, `spec-integrate`, `spec-research`,
  `respec-gate`, `drift-gate`.
- Профили (6): `spec-decomposer`, `spec-contract`, `spec-reviewer`,
  `implementer`, `verifier`, `researcher` + `setup-profiles.sh`.

## Как легло на этот репозиторий

Артефакты из чата (сам код) через публичный share **не выгружаются** — в
снимке страницы только подробные описания. Поэтому набор **реконструирован** по
описаниям и адаптирован под реальный контракт плагинов этого репозитория:

| В чате | В этом репо |
|---|---|
| `ctx.register_tool` / `ctx.register_command` | `tools.registry.register(name, toolset, schema, handler, check_fn, emoji)` — как в `chief-tools` |
| CLI-команды `specflow …` | тулзы `specflow_init/start/status` (репо не регистрирует CLI-сабкоманды) |
| скиллы в `~/.hermes/skills` | `skills/` рядом с плагином + установка через `setup-profiles.sh` в `$HERMES_HOME/shared-skills` |
| абсолютные пути | только `HERMES_HOME` / `~`, по правилам проекта |

Тулзы зарегистрированы в тулсете `kanban` (там же живут оркестрация и
`contract_check`/`leaf_check`). Проверено: плагин грузится с поддельным
реестром, все 6 тулзов регистрируются и попадают в `TOOLSETS['kanban']`,
хендлеры отрабатывают (`leaf_check` валит coupled-узел в branch,
`contract_check` репортит недоступный валидатор, `research_trigger_check`
срабатывает на `on_level_return`, seed-команды не падают без бинарника
`hermes`).

## Что доделать под реальный прогон

- Установить валидаторы контрактов (`redocly`/`specmatic`, `tsc`, `buf`) и
  выверить шаблоны в `CONTRACT_VALIDATORS`.
- Сверить флаги `hermes kanban create … --board/--skill/--workspace` со своей
  сборкой Hermes (в seed-командах) и платформенный ключ `cli` в профилях.
- Подстроить пороги `leaf_check` (1/5/2/100) под проект.

## Важная оговорка (последний ход чата)

В самом конце пользователь попросил «прогнать» набор на двух проектах:
(1) безнадзорный агент с реальными деньгами, массовыми рассылками и
автозакупкой рекламы «без human-in-the-loop»; (2) добыча чужих LLM-кредитов у
спонсоров «для собственного использования». Claude **отказался** это
операционализировать (спам/неконтролируемые траты и обманное получение
ресурсов) и предложил легитимную версию: рыночный OSINT-ресёрч на публичных
данных с гейтом человека на траты и рассылки, consent-based маркетинг. Этот
набор намеренно содержит предохранители (`clarify`, `kanban_block`, ручное
подтверждение при большом радиусе) — снимать их под такие сценарии не следует.
