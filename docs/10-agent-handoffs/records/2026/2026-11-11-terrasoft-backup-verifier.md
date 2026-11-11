# Handoff: воспроизводимая проверка backup и восстановления Terrasoft

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T18:32:40Z`
- Статус: `partial` — реализация проверена локально; backup/restore не выполнялись.
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`
- Scope: эксплуатационный script, изолированные unit tests, read-only feasibility.

## Цель

Перед изменением синхронизации файлов и оплаты обеспечить отдельный проверяемый
backup внешней CRM с фактическим восстановлением в новую базу. Ранее выполненное
восстановление PostgreSQL бота не подтверждает восстановимость внешней CRM.

## Исходное состояние

Исходные 74 теста, architecture и capability outbox проверки прошли в отдельном
Python 3.12 venv с dummy CI environment без сетевых бизнес-операций. Локальный
outbox и центральный registry проверены до реализации: точного аналога
Terrasoft backup/restore verifier не найдено. Новый observation добавляет
интегрирующий агент после получения точного implementation SHA.

Read-only metadata подтвердили SQL Server 2019 Enterprise на Windows, целевую
production CRM, обычные ROWS/LOG файлы, административные права, существующие
локальные каталоги и достаточное свободное место. Эти факты сами по себе не
доказывают, что SQL service сможет записать новый файл и восстановить базу.
Проверка не обращалась к запрещённому backup-хранилищу `testd`.

Параллельные изменения CRM, банковских adapters, моделей и конфигурации
принадлежат другим агентам; этим агентом не изменялись. Репозиторий HOWERP
не изменялся; его ERP gates не заменяют проверки ReceiptFlow.

## Изменения и причины

- `scripts/verify_terrasoft_backup.py` — default read-only plan; production
  target guard выполняется до подключения к master. Уникальные UUID4 имена
  базы/файлов, существующие локальные каталоги, запрет коллизий и свежая проверка
  свободного места не менее 2× выделенного размера базы.
- Explicit `--execute` требует ссылку на exact execution SHA и CI run, а также
  существующий закрытый evidence directory. Проверку успеха CI проводит
  оператор до запуска; script не обращается к GitHub.
- Новый `COPY_ONLY, COMPRESSION, CHECKSUM` backup, HEADERONLY/FILELISTONLY,
  VERIFYONLY и фактическое восстановление без REPLACE в новые пути WITH MOVE,
  RESTRICTED_USER и CHECKSUM. Изменение состава/размера исходных файлов или
  недостаток места прерывает выполнение. Существующие базы не перезаписываются.
- Клон получает DENY CONNECT для public и текущего sysadmin владельцем;
  Broker, TRUSTWORTHY и DB_CHAINING выключаются. Проверяются фактические
  свойства, ownership marker и хеш путей. Это административная изоляция,
  которая не запрещает доступ sysadmin.
- После восстановления: DBCC CHECKDB PHYSICAL_ONLY, сравнение digest каталогов
  объектов/колонок/индексов/ограничений с источником, native blob totals,
  чтение SHA256 максимум трёх детерминированно выбранных тел, totals связей
  и прав файлов. Идентификаторы и хеши отдельных бизнес-документов не выводятся.
- JSON evidence создаётся только через exclusive create, права 0600,
  SHA256 каждого record и цепочка предыдущих records. Секреты, координаты,
  SQL errors/params и исходные строки данных в evidence не включаются.
- Backup и клон сохраняются по умолчанию. Отдельный `--cleanup VERIFIED_JSON`
  сначала показывает read-only план; mutation требует `--execute` и повторно
  проверяет exact ownership/GUID/create time/пути и читаемость backup.
  Удаляется только этот проверенный клон; backup не удаляется.
- `tests/test_terrasoft_backup_plan.py` — 19 mock tests границ target guard,
  default read-only, коллизий, fresh space/source metadata, порядка операций,
  отказов restore/CHECKDB/schema, redaction, immutable evidence и cleanup.

## Проверки

- `python -m unittest discover -s tests -p 'test_terrasoft_backup_plan.py' -v`
  — **19 PASS**, Python 3.12 isolated venv, dummy environment, без live SQL.
- `python -m scripts.check_architecture` — **PASS** для текущего дерева.
- `git diff --check` — **PASS** для текущего дерева.
- Полный integration suite и exact-SHA CI выполняет интегрирующий агент после
  объединения всех изменений; эти проверки не заменены focused suite.

SQL semantics сверены с первичными Microsoft references:
[BACKUP](https://learn.microsoft.com/en-us/sql/t-sql/statements/backup-transact-sql),
[RESTORE](https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-transact-sql),
[VERIFYONLY](https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-verifyonly-transact-sql),
[CHECKDB](https://learn.microsoft.com/en-us/sql/t-sql/database-console-commands/dbcc-checkdb-transact-sql),
[DENY](https://learn.microsoft.com/en-us/sql/t-sql/statements/deny-transact-sql).

## Runtime, backup и rollback

Runtime не изменялся. SQL обращения ограничены read-only metadata, правами,
файловыми проверками и историей backup. Новый backup, CREATE/RESTORE/ALTER/DROP
не выполнялись. Коммиты и push этим агентом не создавались.

При ошибке verifier оставляет новый backup и возможный клон, записывает stage
и sanitized error. Автоматического удаления или отката production CRM нет.
Cleanup клона не является rollback production данных.

## Риски и незавершённое

- Нужен фактический запуск из exact CI artifact до rollout и review JSON evidence.
- PHYSICAL_ONLY не является полной логической проверкой всех бизнес-значений.
  SQL backup checksum не обозначается SHA256 всего backup-файла.
- Исторические NULL blob/link-only files и orphan totals фиксируются, но не
  исправляются этим script. UI пригодность CRM этим verifier не подтверждается.
- Free-space план поддерживает проверенную конфигурацию обычных локальных
  Windows дисков; отдельная схема mount points/FILESTREAM не принята.
- Backup остаётся на том же сервере; off-site/DR readiness не подтверждается.

## Следующий шаг

Интегрирующему агенту выполнить review, source commit/push и exact-SHA CI,
затем запустить read-only plan и разрешённый `--execute`, сохранить evidence;
использовать оставленный restricted клон для проверки новой CRM логики до
отдельного cleanup.
