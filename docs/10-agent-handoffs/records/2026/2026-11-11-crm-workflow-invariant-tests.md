# Handoff: проверки долговременной CRM-синхронизации и вложений

- Агент: `Codex /root/supplier_guard_design`
- Время UTC: `2026-11-11T18:40:13Z`
- Статус: `completed`
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`
- Scope: независимое review CRM workflow и новые профильные тесты

## Цель

По поручению основного агента независимо проверить, что новые операции
сохраняют исходник и банковское подтверждение, переносят временные ошибки без
дублирования CRM-операции и не проводят её по неподтверждённому платежу.
Основание — принятый контракт `docs/crm-files-and-payment-posting.md`.

## Исходное состояние

Baseline до общего изменения: 74 tests PASS по evidence основного агента.
Во время этой подзадачи в общем checkout уже присутствовали изменения банков,
CRM posting, конфигурации и мониторинга других агентов. Они не переписывались.
Прочитаны изменяемый процесс, ORM, workflow, файловый адаптер и расширенные
методы Terrasoft connector. Банковская часть предыдущей подзадачи описана в
`2026-11-11-bank-settlement-evidence.md` этого каталога.

## Изменения и причины

- `tests/test_crm_attachments.py` — 16 тестов: безопасное имя, стабильные UUID
  по роли, сохранность исходных bytes/hash, запрет выхода за storage и symlink,
  повтор сохранения, очистка частичного файла, изменение существующего файла,
  неправильные/повторные родительские связи, чтение фактически сохранённых
  bytes/metadata/link counts и лимиты файлов/распаковки metadata.
- `tests/test_crm_sync_workflow.py` — 29 тестов с настоящими локальными
  SQLite-транзакциями и mock внешних границ: atomic enrollment, фиксированный
  UUID, смена исходника, потеря локального commit после внешнего create/post,
  уникальность bank claim, ожидание и повторная проверка исполнения, отказ при
  отмене/изменении подтверждения, terminal posted, target/version/dry gates,
  отсутствие исторического replay, sanitized retry и Telegram checkpoint.
  Отдельно проверены SQL boundary: DB_NAME первым, одна транзакция для posting
  и attachment, foreign ownership, сохранение номера, native MaxFileSize.
  Pipeline-regressions проверяют сохранение успешной банковской попытки с
  отсутствующим/пустым/dry-run ID и отсутствие повторного создания либо legacy
  fallback; для корректного ID проверено, что draft и outbox уже сохранены к
  первому обращению в CRM, а повторный выбор банка не создаёт второй draft.
- `tests/test_crm_sync_postgres.py` — два теста безопасного target guard и семь
  opt-in интеграционных сценариев реального PostgreSQL: JSONB, rollback,
  unique draft/FK, конкурентный bank claim, SKIP LOCKED, два параллельных
  monitor, полный долговременный workflow и потеря локального commit после
  внешнего post. Требует явно указанной локальной disposable базы, создаёт и
  удаляет только собственную случайную schema. Банк и CRM остаются mock.

При review переданы основному агенту и исправлены им: создание каталога до
проверки symlink-containment; отсутствие итоговой проверки FileData/metadata/
связей после INSERT; приём dry-run ID при live-конфигурации; необходимость
считать все FileID-связи отдельно от связи к ожидаемой операции. Тесты выше
закрепляют исправления. Production-файлы в этой подзадаче не изменялись.

Финальное review также выявило, что отказ enrollment при успешном ответе
банка без payment ID откатывал локальную банковскую попытку. Основной агент
исправил pipeline: сохраняется `bank_created` и rawresponse, включается ручная
проверка без CRM fallback. Два дополнительных теста закрепляют отсутствие
повторного bank create и порядок commit перед CRM.

## Проверки

Среда: Python 3.12 из проверенного requirements venv. Для всех команд заданы
dummy Telegram token, пустой OpenAI key и глобальные PAYMENT_DRY_RUN/CRM_DRY_RUN;
локальные тестовые settings включают live-ветку только с mock зависимостей.

- `python -m unittest discover -s tests -p 'test_crm_attachments.py'` —
  exit `0`, 16 tests PASS.
- `python -m unittest discover -s tests -p 'test_crm_sync_workflow.py'` —
  exit `0`, 29 tests PASS.
- `python -m unittest discover -s tests -p 'test_crm_sync_postgres.py'` без
  `RECEIPTFLOW_TEST_DATABASE_URL` — exit `0`, два guard tests PASS, семь
  интеграционных тестов ожидаемо skipped. После этого основной агент сообщил
  результат той же команды с явно заданным локальным disposable PostgreSQL:
  exit `0`, все девять тестов PASS за 0.824 s, без skips. PostgreSQL runtime
  запущен и обслуживается основным агентом.
- `git diff --check` — exit `0`.

Профильные результаты относятся к итоговым исходникам этой подзадачи и
интеграционному набору общего checkout; полный suite и exact-SHA release
checks выполняет основной агент после завершения общего изменения.

## Runtime, backup и rollback

Runtime не изменялся. Нет live bank/CRM вызовов, изменений существующих БД,
commit, push или deploy. SQLite и временные файлы удаляются teardown тестов.
Opt-in PostgreSQL runtime создаёт основной агент; тесты ограничены своей
случайной schema. Для rollback до commit достаточно исключить новые тестовые
файлы из общего набора; чужие изменения не затрагиваются.

## Риски и незавершённое

Mock SQL boundary не доказывает фактическую семантику SQL Server, native
triggers и ACL. PostgreSQL locking/constraints проверены opt-in прогоном по
evidence основного агента. Native CRM posting/files и UI остаются предметом отдельной проверки
на изолированной восстановленной копии до разрешённого deploy.

## Следующий шаг

Выполнить общий набор проверок и изолированную SQL Server проверку с
сохранением release evidence.
