# HANDOFF — spec-flow — feat/roadmap-phase-1

Сделано: C3-C5 интеграция вшита (`d73359d`) — не-веб продукт (библиотека/CLI)
доходит до честного READY. `_product_contract` при отсутствии HTTP-роутов выводит
форму через `_project_kind` → медиум-независимый контракт `{kind, entry, exposes}`
без routes/boot; `_try_synthesize_lib_entry` (новый) детерм. собирает не-веб вход
(реэкспорт API по AST-реестру символов); `_nonweb_capability_boots` (новый) судит
по поведению (импорт входа в подпроцессе + capability-probe); `_assembled_product_boots`
/`_assembly_node`/`_try_synthesize_entry` ветвят по kind. Веб-путь не тронут (routes
выигрывают первыми). Кейс `tests/scenarios/p7_word_stats_lib.yaml` (библиотека без
HTTP) + офлайн-тесты вшивки (`test_nonweb_pipeline_wiring.py`, 6). Офлайн 487 зелёных.

Дальше: живой отвязанный прогон не-веб кейса до READY —
`bash tests/run-detached.sh --case p7_word_stats_lib --depth product --workers real --doctor-enabled`.
ЗАБЛОКИРОВАН провайдером: claude/sonnet через Bifrost(:8080)→Meridian(:3456) отдаёт
HTTP 400 на preflight-смоуке (устойчиво, дважды). Процессы Meridian/Bifrost подняты,
но запрос к подписке отвергается (вероятно протухшая сессия/токен Meridian). По
правилу проекта Meridian — общий сервис стека Hermes, рестарт (`scripts/meridian.sh`)
ТОЛЬКО с явного согласия юзера. Спросить перед прогоном.
