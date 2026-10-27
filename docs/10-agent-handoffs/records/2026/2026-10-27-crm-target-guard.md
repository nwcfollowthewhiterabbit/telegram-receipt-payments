# Handoff: проверка выбранной базы Terrasoft перед запуском и записью

- Агент: `Codex /root/supplier_guard_design`
- Время UTC: `2026-10-27T08:27:36Z`
- Статус: `completed`
- Начальный commit: `cf3ea057fbc126da8c502eb1fb3ed2bdfb427360`
- Scope: `src/connectors/crm/terrasoft_mssql.py`, `src/main.py`,
  `tests/test_terrasoft_target_safety.py`

## Цель

После подтверждённой владельцем приёмки разрешить явный выбор рабочей базы
Terrasoft, сохранив тестовую цель по умолчанию, и исключить запись в базу,
отличающуюся от настроенной. Реестр до реализации проверил основной агент;
прямой аналог проверки CRM target не найден.

## Исходное состояние

Рабочее дерево перед изменениями было чистым. Старый коннектор проверял только
декларативное имя тестовой базы, не сверяя путь SQLAlchemy URL и фактический
`DB_NAME()`. Отдельная проверка CRM до запуска polling отсутствовала. Baseline
60 unit tests и architecture check успешно выполнил основной агент до правок.

## Изменения и причины

- `src/connectors/crm/terrasoft_mssql.py` — точный allowlist двух баз: тестовой
  и рабочей. Настройка обязана совпасть с разобранным SQLAlchemy URL; пустая
  база, неподдержанный драйвер, отсутствующий host, query overrides и ошибочный
  URL отклоняются. Перед бизнес-SQL фактическая база проверяется через
  `SELECT DB_NAME()` на том же transaction connection. Созданный engine
  освобождается через `dispose()` в `finally`.
- Там же — публичный метод `verify_connection_target()` для предварительной
  read-only проверки. В CRM dry-run он не создаёт engine и не подключается.
  Ошибки разбора URL и первоначального подключения не раскрывают credentials.
- `src/main.py` — проверка enabled live CRM до `init_db`, создания Telegram
  клиента, банковского монитора и polling. Для CRM `none` или dry-run проверка
  соединения пропускается.
- `tests/test_terrasoft_target_safety.py` — проверки обоих разрешённых target,
  точного allowlist, URL/config/actual mismatch, отказа до поиска контрагента,
  номера, периода и INSERT, очистки engine, отсутствия сети при dry-run/none и
  порядка startup.
- Эта запись — evidence отдельной агентской подзадачи. Mapping и поведение
  создания новых CRM операций не менялись; старые операции не переигрывались.

## Проверки

- Профильная команда из корня checkout с test env: `TELEGRAM_BOT_TOKEN=test-no-network
  OPENAI_API_KEY= CRM_DRY_RUN=true PAYMENT_DRY_RUN=true
  PYTHONPYCACHEPREFIX=/private/tmp/receipt-paybot-pycache-20261027
  /private/tmp/receipt-paybot-venv-20261027/bin/python -m unittest discover
  -s tests -p 'test_terrasoft_target_safety.py' -v` — exit `0`, 14 tests PASS
  на итоговом наборе исходников подзадачи. Все внешние соединения заменены mock.
- `git diff --check` — exit `0`.
- Независимое read-only review агента `/root/release_checks` не выявило
  блокирующего дефекта проверки target, порядка SQL и startup.
- Полный unit/architecture/outbox набор после объединения изменений выполняет
  основной агент. Наблюдаемое предупреждение о `datetime.utcnow()` относится
  к существующему mapping; в этой подзадаче он не изменялся.

## Runtime, backup и rollback

Runtime не изменялся. Реальные банковские и CRM соединения не открывались;
данные не создавались. Commit, push и deploy не выполнялись. До применения
rollback состоит в исключении перечисленных изменений из исходного набора.

## Риски и незавершённое

Коннектор намеренно поддерживает прямой URL `mssql+pymssql` без query. Другие
драйверы/DSN/URL-параметры требуют отдельного изменения контракта. Фактическое
переключение runtime, backup, endpoint smoke и release evidence остаются
ответственностью основного агента. Проверка target не меняет CRM mapping.

## Следующий шаг

Основному агенту выполнить объединённый набор проверок, зафиксировать release
и применить разрешённое переключение с обязательной read-only target проверкой.
