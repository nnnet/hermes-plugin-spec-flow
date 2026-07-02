# HANDOFF — аудит-гейтед храповик, sid babc3939

## Сделано (коммиты)
- 137e7e5 #131 честное И: продукт NOT READY при красном корне.
- a781f28 #132 метод-осознанный детектор дублей (пара метод+путь).
- 500a70f #133 карточка листа acceptance/examples + гейт полноты (lenient).
- bedf6bb АУДИТ цикл 1: tests/audit/ + фикс amend-in-place дыры v145.
- fba270a АУДИТ Уровень Б + гейт в run-detached.sh.

## Механизм храповика (готов)
tests/audit/ — дешёвый офлайн-гейт. run-detached.sh НЕ стартует пока
`pytest tests/audit` красный (SPEC_FLOW_SKIP_AUDIT=1 для диагностики).
README.md — конвенция +Z (провал прогона → новое правило аудита).
Уровень А: tests/audit/test_requirement_class_paths.py (матрица классов).
Уровень Б: tests/audit/test_honesty_invariants.py (роутер без заглушек+ветки
400/404/405, READY=И, card-гейт). 8 зелёных, 0.04с.

## Дыра v145 закрыта (amend-in-place)
`красивый_вид` (правка src/web_ui.py) роутился в amend (code_target), но анти-форк
гейты судили его как ВЛАДЕЛЬЦА /ui → handler-gate «missing» + scope-lint «duplicate»
→ empty_delta reject → RED. Фикс: узел с code_target владеет НИЧЕМ —
`_leaf_owned_routes`/`_leaf_exposed_symbols`/`_late_req_scope_findings` → [] для
аменда. Amend и анти-форк — два разных пути.

## СЕЙЧАС: прогон p6 через гейт (оракул)
Лог: tests/runs-out/_detached_ratchet_p6.log; run dir 2026-07-02T...__v146.
Гейт прошёл (audit green). Ждём: красивый_вид роутится в amend и НЕ отклоняется;
честный READY при логическом И. Проверять: meta.json/PRODUCT-RESULTS.md/trace.jsonl.

## Дальше по храповику
- Прогон зелёный → DONE p6; затем p7_word_stats_lib (медиум-независимость).
- Прогон красный → distill НОВУЮ причину в tests/audit/ (+Z) → фикс → аудит зелёный
  → снова прогон. Кандидаты: ping_text card lenient (слабая модель не дала
  acceptance); amend «no verifying test» (качество слабой модели vs дыра).
- #139 Уровень В dry-plan (decompose+spec без сборки) — опционально.
- #136 устойчивость в самом конце (p6 ×2-3 рандомные инъекции + p7).

## Правила
Аудит честный — не подгонять под зелёное. Фиксы — настоящие возможности движка,
не костыли под кейс. Не облегчать тесты. Прогон только run-detached (setsid).
claude=подписка Meridian. Параллельные агенты — worktree.
