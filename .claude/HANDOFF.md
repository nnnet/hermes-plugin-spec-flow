# HANDOFF — 2026-06-30 — spec-flow live measurement (v124/v125)

## Что сделано этой сессией
- Подтверждено: код run-time плана (5 рычагов + fast-fail-mimo) уже внедрён
  (дефолты `tiering:True`, `simple_max_loc:120`, ensemble по сложности,
  breaker timeout→retire-after-1). Задачи #91-96, #102-103 закрыты.
- **Фикс молчаливой смерти прогона (`ca94d8e`, запушен):** v124 исчез на ~6 мин
  внутри `run_project` без следа (C-краш в потоке, faulthandler был выкл).
  Добавлен `faulthandler.enable()` + register SIGTERM/SIGUSR1 в
  `tests/lib/run_cases.py`, `PYTHONFAULTHANDLER=1` в `tests/run-detached.sh`.
- **Честный замер v125:** `product NOT READY` → `meta.status=FAILED`, 343 вызова,
  ~35 мин. Рычаги времени фаерят, ложного зелёного нет.

## Корень (задача #122, ось atomic-specs #82)
Декомпозер раздробил микро-«notes» на 8 листьев; канон-хендлеры
`post_notes/get_notes/get_health` определены в ДВУХ модулях (`db_layer.py` +
`wsgi_app.py`), хранилище `NOTES` только в `db_layer` → синтез точки входа
привязывает чужой `get_notes` → e2e roundtrip RED, `/health` не служит. Доктор
не сходится (resolved_ratio 0.11) — дефект структурный, по-листно не лечится.

## Точный следующий шаг
Детерминированный гейт «один канон-хендлер = ровно один лист-модуль».
НЕ патчить эвристику `_plan_ownership_report` (`spec_flow_runner.py:3154`,
diagnostic-only, матч по упоминанию пути). Чинить по факту AST-определения
хендлера (переиспользовать Фазу 1 `_resolve_route_handlers` / Фазу 2
`_leaf_handler_gate`): при дубле определения — консолидация к владельцу,
со-расположенному с хранилищем маршрута, либо отказ плана с принудительным
слиянием листьев у декомпозера. Свой план + свежий контекст.

## Состояние
- Ветка `feat/roadmap-phase-1`, HEAD `ca94d8e`, всё запушено.
- Офлайн-набор 316 зелёных.
- Прогон v125: `tests/runs-out/2026-06-30T09-09-55__v125__p6-micro-notes/`.
