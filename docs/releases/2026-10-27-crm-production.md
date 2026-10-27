# ReceiptFlow: рабочая CRM после приёмки тестового контура

## Результат

27.10.2026 в 08:37:27 UTC бот переключён с `Terrasoft_test` на рабочую
`Terrasoft` на том же SQL Server. Основание — сообщение пользователя о
подтверждении заказчиком корректности интеграции и разрешение перенести её
на рабочую базу; размещение на том же сервере уточнено пользователем.

Новые обычные обработки направляют CRM-операции в `Terrasoft.dbo.tbl_Cashflow`.
Согласованный маппинг сохранён, тестовые операции не копировались и не
переигрывались. Банки остались LIVE, подпись/оплата по-прежнему за человеком.
Контрольный банковский платёж не создавался.

## Source и CI

| Объект | Подтверждённое значение |
| --- | --- |
| Baseline checkout | `cf3ea057fbc126da8c502eb1fb3ed2bdfb427360` |
| Baseline runtime execution | `3160f7d1af660e0d3aedc24970ba6bbf4566ade2` |
| Implementation commit | `cb051cce2c5635a7ae35363234ada3a568abbc65` |
| Установленный execution SHA | `0ed9f2b71373c4ffe502f5c9b55fe4bdd7a4375f` |
| CI | [40473832524](https://github.com/nwcfollowthewhiterabbit/telegram-receipt-payments/actions/runs/40473832524), completed/success |
| CI jobs | checks, package-build, foundation-instance — success |
| CI artifact | receipt-paybot-image, ID `12719888018` |
| SHA256 архива | `873d2da7a168c5446b14e3b464dfaef1831bd9a707ada378d0f8e5ee23667cb2` |
| Image ID | `sha256:1a9dee8628cf78a9337d305ee8fdfc12b07eb403903fef24f3b030f4a83cf0b7` |

74 tests PASS, включая 14 регрессий target guard/startup; architecture,
outbox и diff checks прошли. Образ из CI перенесён без сборки на сервере;
checksum и OCI revision сверены. После переключения документационные
commits не требуют нового runtime artifact; установленный execution SHA
остаётся указанным выше.

## Проверки рабочей базы

Read-only discovery подтвердил только две доступные пользовательские базы
на этом сервере: `Terrasoft_test` и `Terrasoft`, обе ONLINE/read-write.
Таблицы Account/Cashflow/Period и FK совпадают. Все семь используемых
фиксированных справочных записей присутствуют и совпадают по
идентифицирующим полям. Нужные SELECT/INSERT-права есть; период `10.2026`
существует в одном экземпляре, CFNumber пригодны для текущего алгоритма.

Конфигурация, database в URL и фактический `DB_NAME()` проверяются перед
включением bot/monitor и на соединении каждой CRM-транзакции. Проверка
неразрешённой цели прекращает запуск/запись. Default в source остаётся тестовым.

## Backup и восстановление

Runtime checkout: `/home/ubuntu/receipt-paybot`, сервер `howbot`.
Fresh backup: `/home/ubuntu/backups/receipt-paybot-20261027T082622Z`, каталог
0700, вне заменяемого runtime. Проверен 27.10.2026 в 08:36:16 UTC.

Сохранены PostgreSQL dump, receipt-файлы, `.env`, основной Compose и текущий
release override, локальный smoke script, source archive и прежний image.
Bot был остановлен для согласованного снимка; исходный DB-контейнер работал.
Dump восстановлен в `receipt-paybot-restore-20261027T082622Z` с network none и
новым tmpfs data 512 MiB. Эти настройки подтверждены inspect.

Совпали row counts/fingerprints всех четырёх таблиц локального бота:
receipts, payment_drafts, audit_logs, authorized_users. Восстановленные
конфигурация, override и каждый receipt-файл совпали по SHA256. Проверены
checksum всех backup-артефактов. Доказательства: `verified.json` и
`sha256sums.txt` в backup. Restore-контейнер и временные восстановленные файлы
удалены; backup сохранён. `testd` не использовался.

Это локальный проверенный backup бота. Метаданные внешнего SQL Server
отдельно показывают full backup `Terrasoft` 27.10.2026 в 02:35:51 по времени
SQL Server; его restore в этой задаче не выполнялся. Существующие данные и
схема внешней CRM не мигрировались, off-site/DR readiness не заявляется.

## Изменение runtime и контроль

В защищённой конфигурации изменены ровно `TERRASOFT_DATABASE` и database
часть `TERRASOFT_MSSQL_URL` на `Terrasoft`. Программное сравнение подтвердило
сохранение server/credentials и всех остальных значений. Новый release:
`/home/ubuntu/receipt-paybot-releases/0ed9f2b71373c4ffe502f5c9b55fe4bdd7a4375f`.

В release сохранены production.env (0600), compose.release.yml,
compose.candidate.yml и выполненные ops-скрипты. Candidate использовал exact
image, read-only rootfs и команду проверки вместо старта bot. Его подключение
к `Terrasoft` подтверждено до изменения действующего `.env`.

После backup выполнен SQL smoke с synthetic `HOWERP-E2E-…` marker:
реальный INSERT коннектора, чтение и сверка суммы/назначения/счёта/статуса,
обязательный ROLLBACK. Отдельное соединение подтвердило отсутствие UUID
в Cashflow, CashflowRight, CashflowLog и DatabaseLog. Банковские методы не
вызывались, новый период не создавался. Прямой `sync_receipt()` не вызывался,
поскольку он завершает транзакцию COMMIT.

Перед smoke проверены триггеры и их зависимости: действия локальные и
транзакционные. Для runtime login нет AdminUnit ни в принятой тестовой, ни в
рабочей базе; поэтому ожидается 0 автоматически добавленных строк прав.
Smoke подтвердил то же поведение. Доступ конкретного пользователя через CRM
UI этим не доказан; его сверяют на первой обычной рабочей операции.

Затем `.env` заменён атомарно, release override переключён на новый image,
пересоздан только bot (`--no-build --no-deps --pull never`). Схема не менялась,
отдельная миграция не требовалась. Фактический runtime после старта:

- exact image ID совпадает; running, restart count 0;
- Telegram polling начался, ошибок в проверенном интервале нет;
- исходный DB-контейнер сохранил ID и healthy;
- фактический `DB_NAME()` = `Terrasoft`, CRM_DRY_RUN=false;
- monobank и Приват24 остались LIVE;
- default Compose закрепляет exact image, временный restore-контейнер удалён.

Runtime evidence: `deployment.json`, `crm-rollback-smoke.json` в release.
Чужие untracked `backups/` и smoke script сохранены. Release override остаётся
runtime-ссылкой; секреты в Git не добавлялись.

## Подготовленный rollback

Прежний image сохранён как `receipt-paybot:rollback-20261027T082622Z`, ID
`sha256:a36e0b76265868a57d3d5817515091ad40759e519ae99f496a259fb405438704`.
В backup имеются `previous.env`, `compose.rollback.yml` и image export.

При разрешённом откате сначала остановить bot, проверить backup checksums,
вернуть **одновременно** прежний `.env` и override/image, затем запустить
только bot. Старый image не допускает production target. Пример из checkout:

```sh
docker compose -p receipt-paybot stop -t 90 bot
install -m 600 /home/ubuntu/backups/receipt-paybot-20261027T082622Z/previous.env .env.rollback-tmp
mv .env.rollback-tmp .env
ln -sfn /home/ubuntu/backups/receipt-paybot-20261027T082622Z/compose.rollback.yml docker-compose.override.yml
docker compose -p receipt-paybot up -d --no-build --no-deps --pull never bot
```

После отката подтвердить `Terrasoft_test`, старый image, polling и health.
Не восстанавливать БД поверх новых операций и не копировать записи между
базами: созданные после переключения рабочие операции остаются рабочими.
Полный rollback после успешного переключения не запускался; recovery-ветка
в ops-скрипте и согласованные старые артефакты сохранены.

## Следующая обычная проверка

При первой реальной обработке бухгалтер сверяет новую операцию в рабочей
CRM с сообщением бота и банковским черновиком. Не загружать прежний счёт
повторно ради переноса истории. Согласованные ограничения маппинга и
однократной записи сохранены в [самари](../crm-accountant-review.md).
