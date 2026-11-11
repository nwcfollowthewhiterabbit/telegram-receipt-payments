# ReceiptFlow: файлы и проведение подтверждённой оплаты

11.11.2026 в **20:27 UTC** установлен и включён процесс для новых обработок
в рабочей `Terrasoft`: оригинал счёта в детали «Файлы», подтверждение исполнения
банком, подтверждающий файл и проведение той же операции Cashflow.
Privat24 прикладывает PDF-квитанцию; monobank — банковский JSON выписки.
PDF monobank при необходимости добавляет бухгалтер вручную.

Подпись и отправка платежа на исполнение остаются за человеком. Контрольных
реальных платежей при установке не создавали. История автоматически не
включалась; после запуска очередь новых CRM-задач пуста, исторических задач — 0.

## Source и артефакт

| Проверка | Результат |
| --- | --- |
| Execution SHA | `4b9d5bbe4f4c4572730abf4c331cff932644995f` |
| Exact-SHA CI | [43094014777](https://github.com/nwcfollowthewhiterabbit/telegram-receipt-payments/actions/runs/43094014777), completed/success |
| Jobs | `checks`, `package-build`, `foundation-instance` — success |
| Unit/domain checks с PostgreSQL | 270 tests, OK, без пропусков |
| Изолированная проверка image | 270 tests, OK; 7 PG tests skipped без сети, выполнены в предыдущем job |
| Architecture / capability outbox | PASS |
| CI artifact | `13888719985`, gzip image archive 81 047 204 bytes |
| Archive SHA256 | `fe0116cd3bf0726458bdd95ad1e1835aec42827cb6a729b3c7af59fdeaa001e5` |
| Image ID | `sha256:a873afe7ddb130e45b140aee174fef06050f6001494aceef95e843cd22945a31` |

Архив получен из CI. На сервере перед использованием проверены checksum,
image ID и OCI revision; установлен именно этот image, без локальной сборки.
Исходное дерево перед изменением runtime было чистым, local/remote `main`
совпадали с execution SHA. Базовый source задачи — `2b576c98cfbd31581f5ec7401d409ca091321b84`.

## Backup CRM и native rehearsal

Создан COPY_ONLY/COMPRESSION/CHECKSUM backup и фактически восстановлен в
отдельную новую базу с ownership marker. Копия ограничена RESTRICTED_USER,
public CONNECT запрещён, broker/trust/chaining выключены. Рабочая и прежняя
тестовая базы не перезаписывались. Backup и clone идентифицируются run
`9171ddaf67f345f8ad19fd3c824727bf`.

После исправления проверочного скрипта сохранённая копия повторно проверена
финальным образом: исходная цепочка журналов, сервер/источник, точная
принадлежность clone, пути файлов и связь backup set с фактическим restore
через HEADERONLY и `msdb.restorehistory`. Повторены VERIFYONLY CHECKSUM,
DBCC CHECKDB PHYSICAL_ONLY, сравнение схемы и чтение вложений.

Финальная проверка завершена **20:24:32 UTC**. Private journal в каталоге
release `crm-evidence/9171ddaf67f345f8ad19fd3c824727bf-003-verified.json`,
record SHA256 `d75eb18699cca44a1fcd1e44909ad59d95d9ba404821a59e35694955cd0c7acb`.
Все обязательные признаки verified/checksum/physical/schema — true.

Проверены 9 305 строк файлов, 9 291 непустое тело, 6 319 связей, без orphan
links. Исторические **69 расхождений FileSize** с длиной тела зафиксированы,
не исправлялись. Хэши тел прочитаны для трёх детерминированных образцов;
проверка всех тел по SHA256 не заявляется. SQL backup CHECKSUM не называется
SHA256 всего backup-файла. PHYSICAL_ONLY не заменяет полный логический CHECKDB.

Финальный rehearsal из того же image завершён **20:25:20 UTC**, journal
`crm-evidence/6b989cd7ae1f4439bdf726562ab68ceb-001-rehearsal_passed.json`,
record SHA256 `05f2d19837f797d0526a57f840a32316f1711571e5a3334ff12c42f0efd7cfce`.
Проверены 52 объекта графа зависимостей и шесть групп сценариев:

1. Bytes/hash, связь, ACL и повторное добавление оригинала.
2. Полный откат проведения/договора/банковского файла при конфликте вложения.
3. Отказ при другой сумме банка и при корректной native связи операций.
4. Знак расхода, дата банка, сохранение PeriodID, договорный пересчёт и ACL.
5. Повтор проведения и добавления банковского файла без дублей/изменений.
6. Отказ при ручном изменении суммы уже проведённой операции.

Все 13 synthetic IDs, связанные права, файловые связи и scoped audit
проверены после outer rollback: отсутствуют. Bank HTTP calls — 0,
production mutations rehearsal — false. Отдельные параллельные SQL Server
сессии этот rollback-only harness не моделирует; PostgreSQL concurrency
проверяется отдельными интеграционными тестами CI.

При подготовке устранены ошибки эксплуатационных проверок: native имя
`FileId` в FILELISTONLY, недопустимый COUNT над `image`, связь LinkID с
`tbl_CashflowInCashflow`. Они обнаружены до переключения бота. Исходные
неуспешные журналы сохранены; они не выдаются за успешный release evidence.
Подробности — в immutable agent handoffs этой даты.

## Backup бота и установка

До остановки подтверждены прежний image, running/restarts=0, здоровая БД,
чистый checkout и отсутствие недавних пользовательских операций.
Локальный backup: `/home/ubuntu/backups/receipt-paybot-20261111T202518Z-c751e016`,
режим каталога 0700. Проверен **20:26:45 UTC**, до переключения.

PostgreSQL dump фактически восстановлен в отдельном контейнере без сети
и с новым tmpfs. Списки таблиц, количество и fingerprints всех строк четырёх
таблиц совпали. Файлы счетов, `.env`, Compose и сохранённый служебный script
восстановлены и совпали. SHA256 архивов проверены. Private файлы 0700/0600
архивируются через прежний image с read-only mounts без расширения прав.
Временные restore-контейнер и каталог проверки удалены.

- `verified.json` SHA256: `3574664210233da3e8565215fd2b669c6e323207103ad08919c003674cf79214`.
- `sha256sums.txt` SHA256: `11b19f6c8061a7a51ae0aaf3fb085a6c5e169913e02b0ffad04d5d451a9780ed`.

Изменён только bot service и его release override. В override явно включён
`CRM_FILES_AND_POSTING_ENABLED=true`; source default остаётся false.
`.env` не изменён, прежний контейнер PostgreSQL сохранён. Создана только новая
локальная таблица `crm_operations`; внешняя CRM schema не мигрировалась.

Deployment journal `deployment.json` в каталоге exact release:
SHA256 `306e995c047822fb058c078e7732b9729dc0d389d786097a39fc90ac9c6f89c5`.
Он подтверждает обе проверенные резервные копии, native rehearsal,
actual target, exact image, running/restarts=0 и сохранённый DB container.

Первый runtime smoke в **20:27:24 UTC**: actual `Terrasoft`, live CRM,
оба банка LIVE, flag true, polling started, PostgreSQL healthy, restart count 0,
ошибок/traceback/CRM cycle deferrals — 0, новые и исторические CRM-задачи — 0.
Poll interval — 60 секунд. `runtime-smoke.json` SHA256:
`66ad72e167aaa9122c9faf006c71cd7abfaed0cc6e6a7cf7afa87e498ea75992`.

Повторный `runtime-stability.json` в **20:29:09 UTC** подтвердил те же
результаты за интервал с 20:27:07: рестартов, error/traceback/CRM cycle
deferrals нет; очередь остаётся пустой, PostgreSQL healthy. Его SHA256:
`236d48c16030fa409ba51014852cb03abe80343688fc08385196afdac8cce24f`.
Временных CRM/PG helper-контейнеров после cleanup не осталось.

## Очистка, хранение и откат

Owned clone удалён в **20:29:03 UTC** после повторной проверки ownership и
VERIFYONLY сохранённого backup. Cleanup journal:
`crm-evidence/7b7b4dd745cd487d92566d37cc1fb56d-001-cleaned.json`, record SHA256
`465c577d6f8d5efc3a9203ea6cebcc540d923d240e185abb0423675a8271a73a`.
`restore_removed=true`, `backup_retained=true`. Backup сохраняется. Все копии локальные на соответствующих серверах;
`testd` не использовался. Это не off-site backup и не подтверждение DR readiness.

Сохранены прежний release override, конфигурация и image
`sha256:1a9dee8628cf78a9337d305ee8fdfc12b07eb403903fef24f3b030f4a83cf0b7`
(execution `0ed9f2b71373c4ffe502f5c9b55fe4bdd7a4375f`). Проверенный архив
старого image входит в backup. Откат приложения возвращает этот image/override,
сохраняя новые локальные задачи, CRM-операции и файлы. Восстановление backup
поверх новых рабочих операций не является штатным rollback. Фактический
rollback работающей новой версии не выполнялся: установка прошла успешно.

## Первая обычная оплата и ограничения

Бухгалтеру проверить первую обычную новую обработку по RCPT/CF: получателя,
код, сумму и назначение, открытие оригинала и подтверждения в «Файлах» под
своей ролью, проведение той же операции и фактическую дату банка. При наличии
договора — предоплату и долг. SQL-проверка ACL не заменяет UI-проверку под
конкретным пользователем; live bank→CRM E2E и такой UI-вход при установке
не выполнялись. Старые счета ради проверки повторно не загружать.

Ранее принятый fixed CashAccount mapping для обоих банков сохранён. Его
соответствие фактическому счёту списания бухгалтер проверяет отдельно.
Автоматическое сторнирование будущих возвратов не добавлено. Несовпадение
банковских реквизитов/назначения, ручные финансовые изменения, неизвестные
состояния, закрытые договоры и связанные операции требуют ручной проверки.

[Украинская инструкция](../user-manual.uk.md#файли-та-підтвердження-оплати)
содержит основной сценарий и проверку бухгалтером.

## Фиксация документации

Последующая документационная фиксация не меняет установленный execution SHA.
Её clean tree, равенство local/remote main и successful exact-SHA CI сохраняются
отдельно в `final-documentation-verification.json` каталога этого release,
чтобы не создавать самоссылочный commit. Runtime image остаётся указанным выше.
