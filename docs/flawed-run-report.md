# flawed-run (пример с ошибками) — отчёт по логам (footprints + методологический аудит)

> Источник: трейс из 8 событий. Скиллы: 3 · профили: 3 · задач: 6 · завершён: ✅.
> Ревизия: spike (уровень 1) — · непрерывная ревизия (уровень 2) —.
> **Методологический вердикт: ❌ есть ошибки** (6 error, 2 прочих).

## Методологический аудит (для ревизионера)

> Аудит проверяет **этот прогон** (его лог) на соответствие методологии spec-flow — это **не баги кода плагина**. «Зелено» = прогон шёл по методу; находки = где метод нарушен *в этом прогоне*, и как починить **процесс** (добавить пропущенный гейт, провести дрейф через drift-gate и т.п.).

| Уровень | Правило | Где (задача) | Что не так | Как починить прогон |
|---|---|---|---|---|
| ❌ error | `R1-policy-gate` | `L0` | no passing policy_gate — constitution / measurable-target check missing | run policy_gate on the goal before decomposing |
| ❌ error | `R2-leaf-before-impl` | `subsys_leaf:impl` | implemented 'subsys_leaf' without a leaf_check=leaf verdict | gate every node with leaf_check; only leaves get an impl task |
| ❌ error | `R3-silent-drift` | `svc:impl` | contract_check reported drift but no drift-gate followed | route every drift through drift-gate; never edit code silently |
| ❌ error | `R4-impl-not-reviewed` | `subsys_leaf:impl` | 'subsys_leaf' implemented without a passing spec-reviewer impl-review | add a review node downstream of every impl; require PASS |
| ❌ error | `R4-impl-not-reviewed` | `svc:impl` | 'svc' implemented without a passing spec-reviewer impl-review | add a review node downstream of every impl; require PASS |
| ❌ error | `R5-branch-no-integrate` | `subsys` | branch node has no Integrate & verify node | create an integrate node whose parents are the branch's children |
| 🟡 warn | `R6-expanded-past-open-decision` | `subsys` | level expanded while a decision was still open (feedback loop skipped) | kanban_block on the open decision; expand only after it is resolved |
| 🟡 warn | `R9-impl-before-research` | `L1` | implementation started before any research (analogs / build-vs-reuse / architecture & NFRs) | put a research/ADR node (analogs, differentiation, architecture, DB, load, security) before feature subtrees |

## Footprint — что делалось по шагам (детализация ≤ 2)

> Колонка **«Простыми словами»** — самым простым языком: что реально получилось (создан план, написан код, прогнан тест, сделан коммит, пройдено ревью, собрана сборка) и каково последствие.

| # | Кто | Что делал (технически) | 👶 Простыми словами: что вышло | Тип |
|--:|---|---|---|---|
| 1 | 🧩 spec-decomposer | leaf_check | задача большая → разбили на подзадачи | 🧩 Разбили |
| 2 | 🧩 spec-decomposer | leaf_check | задача маленькая → можно писать код | 🍃 К работе |
| 3 | 🛠️ implementer | design then code (NO TDD, NO review) | расписали порядок: БД→логика→API→тесты | 📝 План кода |
| 4 | 🛠️ implementer | contract_check vs frozen L2 | код разошёлся с контрактом — поймали | ⚠️ Расхождение |
| 5 | 🛠️ implementer | edited code to match (silent) | edited code to match (silent) | · |
| 6 | 🧩 spec-decomposer | leaf_check | задача большая → разбили на подзадачи | 🧩 Разбили |
| 7 | 🛠️ implementer | implement child directly | implement child directly | · |
| 8 | ✅ verifier | L0 integrate done = project COMPLETE | всё собрано и проверено — ПРОЕКТ ГОТОВ | 🏁 Готово |

## Покрытие — посчитано кодом из лога

Скиллы: **3/9** · Профили: **3/6** (события каждого посчитаны по полям `skill`/`profile` трейса)

| Скилл | Событий | · | Профиль | Событий |
|---|--:|---|---|--:|
| `drift-gate` | — не использован | · | 🛠️ `implementer` | 4 |
| `respec-gate` | — не использован | · | 🔬 `researcher` | — не использован |
| `spec-contract` | — не использован | · | 📐 `spec-contract` | — не использован |
| `spec-flow-decompose` | 3 | · | 🧩 `spec-decomposer` | 3 |
| `spec-implement` | 4 | · | ⚖️ `spec-reviewer` | — не использован |
| `spec-integrate` | 1 | · | ✅ `verifier` | 1 |
| `spec-requirements` | — не использован | · |  |  |
| `spec-research` | — не использован | · |  |  |
| `spec-reviewer` | — не использован | · |  |  |

- Вызовы тулзов: leaf_check×3, contract_check×1
- ⚠️ Не использованы: `drift-gate`, `respec-gate`, `spec-contract`, `spec-requirements`, `spec-research`, `spec-reviewer`, `researcher`, `spec-contract`, `spec-reviewer`
