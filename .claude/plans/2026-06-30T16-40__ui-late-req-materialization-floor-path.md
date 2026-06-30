# /ui сирота: материализация поздних требований в product+floor пути

> Свежий контекст. Перед стартом: `.claude/HANDOFF.md` + этот файл.
> Ветка `feat/roadmap-phase-1`, HEAD `0790815`+. Офлайн 338 зелёных.
> Прогон: `bash tests/run-detached.sh --case p6_micro_notes --depth product
> --workers real --doctor-enabled`, смотреть логи отвязанно.

## Где мы (сделано и проверено этой сессией)
3 движковых фикса, все зелёные + запушены, БАЗА продукта надёжно корректна:
- **#82 автофикс stdlib-теста** (`6205999`) — свой `src/*.py` не клеймится сторонним.
- **#83 coder-empty-retry** (`fe573b9`) — пустой ответ кодера (таймаут) → ретрай по
  цепочке до 3, даже ансамбль=1. Тела листьев ложатся.
- **Герметичный PYTHONPATH** (`4a27225`) — продукт судится ТОЛЬКО по своему `src/`
  (`hermetic_env`); чужой `db` из editable-установки `ideaharvest_backend` (.pth в
  user-site) не затеняет и не даёт ложного зелёного.

**Проверено прямыми WSGI-вызовами по финальному продукту (v131 И v132):**
`GET /health 200`, `POST /notes 200{id}`, `GET /notes 200{items}`,
**`POST` пустое → 400** (баг пустого тела доктор закрыл САМ). База — ПОЛНОСТЬЮ ОК.

## Единственный блокер к READY: /ui 404 (поздняя просьба web_ui не материализуется)
**Корень (точно локализован):** движок читает standing requirements ОДИН раз — в
блоке размещения детей корня (`spec_flow_runner.py:5510-5524`, depth==0). p6 yaml
прямо это документирует: «a requirement injected after that phase is orphaned — the
engine reads standing requirements once». web_ui впрыскивается диспетчером
(`tests/lib/hitl_dispatcher.py`, отдельный поток) по триггеру `when:{event:"minimal
impl"}`.
- В ОБЫЧНОМ продукте (много листьев) web_ui успевает прийти до окна размещения.
- В **product + small-product-floor** (#122/#123: один `core`-лист) окно размещения
  проходит слишком рано/мимо → web_ui НЕ становится узлом (узлы v131/v132: только
  `core` + `product_entry`-asm; 5 «requirements» все базовые). `/ui` остаётся в
  контракте (integrate ловит «GET /ui -> 404»), но листа-владельца нет → сирота.
- Офлайн `tests/coverage/test_requirement_injection.py` (depth=spec) ПРОХОДИТ —
  значит баг специфичен для product/floor/live, не общий.

## Шаги
### Шаг 1 — офлайн-репро (ПЕРВЫМ, детерминированно)
Расширить `test_requirement_injection.py` (или новый) на **product-depth + floor**:
контракт ≤5 маршрутов (floor срабатывает) + `standing_requirements` отдаёт web_ui
с поздним приходом → проверить, материализуется ли web_ui как root-child. Ожидаемо
КРАСНЫЙ (повторяет /ui сироту). Это фиксирует баг до правки.

### Шаг 2 — надёжный фикс: материализация не one-shot
Перечитывать standing requirements на **root-integrate** (перед вердиктом): любое
непокрытое требование (не в `self.tasks`, его acceptance краснит integrate) →
материализовать листом + построить + переоткрыть integrate (переиспользовать
машинерию `_requirement_nodes` + reconcile #90). Закрывает гонку диспетчера И
floor-fast-path, модель-независимо. НЕ ослаблять boot-gate/контракт.
- Альтернатива/дополнение: floor-корень держать «открытым» для late-req до integrate.

### Шаг 3 — отвязанный p6 до честного READY
База уже зелёная; нужен только /ui (web_ui → `src/web_ui.py`+`get_ui` → маршрут в
синтезе app.py). Ждать спокойный провайдер (v131/v132 съели 12 и 20 таймаутов по 75с
— турбулентность, ось #83; fast-fail mimo работает, но claude/sonnet тоже таймаутит).

## Правила (не нарушать)
- НЕ ослаблять honest-гейты ради зелёного. /ui должен РЕАЛЬНО служить HTML.
- Реальные баги чинит система; моя правка — движок/материализация, не код продукта.
- Прогоны: free OpenRouter через Bifrost + claude/sonnet подписочный. Отвязанно.
- После правки: офлайн `pytest tests/coverage tests/harness` зелёный → коммит → пуш.

## Память
`mem:specflow_v124_silent_death_and_decomp_root` (обновлена: v130/v131/v132),
`mem:feedback_never_scaffold_tests_to_pass`, `mem:specflow_weak_model_code_errors_multiagent_creator`.

## Трекер
#82✅ #83(coder-retry)✅ герметичный-PYTHONPATH✅ (эта сессия). Открыто: /ui материализация
(этот план), #83 ведущая модель/таймауты, #113 атомарная карточка листа.
