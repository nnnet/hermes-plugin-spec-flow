# Assemble product entry (src/app.py) — Bottom-Up Plan

## Design
Собрать WSGI приложение, которое:
- маршрутизирует POST /notes, GET /notes, GET /health к готовым обработчикам из notes_api.py
- инициализирует БД на первый запрос через init_db()
- возвращает WSGI iterable (список байтов)
- только стандартная библиотека

Готовые модули для импорта:
- `src.notes_api`: init_db(), handle_post_notes, handle_get_notes, handle_health, handle_not_found
- `src.db_schema_init`: connect()

## Bottom-Up Tasks

### Task 1: Тесты для wsgi_app
**File**: tests/test_app.py (создать)

Покрытие:
- TDD: создать tests, ожидать FAIL
- test_wsgi_app_init_db: проверить, что wsgi_app инициализирует БД
- test_post_note: POST /notes → {text: "hello"} → {id: 1}
- test_get_notes: GET /notes → {items: [...]}
- test_health: GET /health → 200
- test_not_found: GET /unknown → 404

Время: 5 мин

### Task 2: Реализация wsgi_app
**File**: src/app.py (создать)

Минимальная реализация:
- import from notes_api, db_schema_init
- def wsgi_app(environ, start_response)
- парсинг REQUEST_METHOD и PATH_INFO
- вызов handle_* функций
- кодирование JSON ответа в bytes
- возврат [response_body]

Время: 5 мин

### Task 3: Запуск тестов до GREEN
Команда: `cd tests && python test_app.py`
Ожидаемый результат: все тесты зелёные

Время: 2 мин

### Task 4: Двухэтапный review
- **Этап 1**: соответствие спеку (API контракт, обработчики, инициализация БД)
- **Этап 2**: качество кода (нет моков, импорты корректны, читаемость)

### Task 5: Contract check
Проверить дрифт относительно frozen API:
- POST /notes: {text} → {id} ✓
- GET /notes: → {items} ✓
- GET /health: 200 ✓

### Task 6: Verify before completion
```bash
cd /mnt/9/aimanager/sources/hermes-plugins-collection/spec-flow
python -m pytest tests/test_app.py -v
```

Все тесты green, нет ошибок импорта.

## Files Modified
- `src/app.py` (new)
- `tests/test_app.py` (new)
