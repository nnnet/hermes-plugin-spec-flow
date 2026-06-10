# flawed-run (пример с ошибками) — отчёт по логам (footprints + методологический аудит)

> Источник: трейс из 8 событий. Скиллы: 3 · профили: 3 · задач: 6 · завершён: ✅.
> Ревизия: spike (уровень 1) — · непрерывная ревизия (уровень 2) —.
> **Методологический вердикт: ❌ есть ошибки** (6 error, 1 прочих).

## Методологический аудит (для ревизионера)

> Аудит проверяет **этот прогон** (его лог) на соответствие методологии spec-flow — это **не баги кода плагина**. «Зелено» = прогон шёл по методу; находки = где метод нарушен *в этом прогоне*, и как починить **процесс** (добавить пропущенный гейт, провести дрейф через drift-gate и т.п.).

| Уровень | Правило | Где (задача) | Что не так | Как починить прогон |
|---|---|---|---|---|
| ❌ error | `R1-policy-gate` | `L0` | no passing policy_gate — constitution / measurable-target check missing | run policy_gate on the goal before decomposing |
| ❌ error | `R2-leaf-before-impl` | `subsys_leaf:impl` | implemented 'subsys_leaf' without a leaf_check=leaf verdict | gate every node with leaf_check; only leaves get an impl task |
| ❌ error | `R3-silent-drift` | `svc:impl` | contract_check reported drift but no drift-gate followed | route every drift through drift-gate; never edit code silently |
| ❌ error | `R4-impl-not-reviewed` | `svc:impl` | 'svc' implemented without a passing spec-reviewer impl-review | add a review node downstream of every impl; require PASS |
| ❌ error | `R4-impl-not-reviewed` | `subsys_leaf:impl` | 'subsys_leaf' implemented without a passing spec-reviewer impl-review | add a review node downstream of every impl; require PASS |
| ❌ error | `R5-branch-no-integrate` | `subsys` | branch node has no Integrate & verify node | create an integrate node whose parents are the branch's children |
| 🟡 warn | `R6-expanded-past-open-decision` | `subsys` | level expanded while a decision was still open (feedback loop skipped) | kanban_block on the open decision; expand only after it is resolved |

## Шаги на снегу (что плагин делал по шагам, детализация ≤ 2)
| # | Фаза | Кто (профиль · скилл) | Задача | Действие | Итог |
|--:|---|---|---|---|---|
| 1 | Декомпозиция | 🧩 spec-decomposer · `spec-flow-decompose` | `L0` | leaf_check | 🌿 branch — modules 3 > 1 |
| 2 | Декомпозиция | 🧩 spec-decomposer · `spec-flow-decompose` | `svc` | leaf_check | 🍃 leaf — within all thresholds |
| 3 | Реализация | 🛠️ implementer · `spec-implement` | `svc:impl` | design then code (NO TDD, NO review) |  |
| 4 | Реализация | 🛠️ implementer · `spec-implement` | `svc:impl` | contract_check vs frozen L2 | ⚠️ drift — type_mismatch count integer vs string |
| 5 | Реализация | 🛠️ implementer · `spec-implement` | `svc:impl` | edited code to match (silent) | no drift-gate, no respec |
| 6 | Декомпозиция | 🧩 spec-decomposer · `spec-flow-decompose` | `subsys` | leaf_check | 🌿 branch — modules 2 > 1; 1 open decision(s) — resolve before leafing |
| 7 | Реализация | 🛠️ implementer · `spec-implement` | `subsys_leaf:impl` | implement child directly | parent never leaf-gated this child |
| 8 | Интеграция | ✅ verifier · `spec-integrate` | `L0:integrate` | L0 integrate done = project COMPLETE | declared done |

## Сводка
- Скиллы: spec-flow-decompose, spec-implement, spec-integrate
- Профили: implementer, spec-decomposer, verifier
- Вызовы тулзов: leaf_check×3, contract_check×1
