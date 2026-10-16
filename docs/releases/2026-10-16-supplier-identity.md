# ReceiptFlow: устранение смешения поставщика и получателя услуг

## Результат и границы

Обновление выполнено 16.10.2026 в 06:22:39 UTC по явному разрешению владельца
«Да, установить с backup и проверкой». Изменён только сервис `bot` проекта
ReceiptFlow. На исходном PDF установлено: ранее выбранное имя относится к
получателю услуг; код и IBAN — к поставщику. Новый backend отклоняет это
смешение как `supplier_name_mismatch_source` до обращения к банку.

Данные существующего банковского черновика и CRM-операции не исправлялись.
При проверках новый банковский черновик/CRM-операция не создавались. Текущий
статус прежнего черновика в банке этим релизом не подтверждён.

## Source, CI и артефакт

- Предыдущий source: `0727d97e3517a8d49a777184d07d9f9eae08af8e`.
- Implementation: `d8cbb9c22910a4a32431eb7f7b372983f8d8df51`.
- Execution SHA: `3160f7d1af660e0d3aedc24970ba6bbf4566ade2`.
- [CI execution SHA](https://github.com/nwcfollowthewhiterabbit/telegram-receipt-payments/actions/runs/38859541223):
  `checks`, `package-build`, `foundation-instance` — completed/success.
- CI artifact `receipt-paybot-image`, ID `12028955997`; загружен именно образ CI,
  локальная сборка на сервере не выполнялась.
- SHA256 архива: `a0cdcee8f3bfe6fbb4d7205a89d4d6fc560eb5827ef0fcdecb92a5fafbb0672b`;
  совпадение после переноса подтверждено.
- Image ID: `sha256:a36e0b76265868a57d3d5817515091ad40759e519ae99f496a259fb405438704`;
  OCI revision равен execution SHA.
- 60 unit tests, architecture и capability validation успешны. Проверки в CI
  выполнены в собранном образе с `--network none --read-only` и dummy settings.

## Backup и проверка восстановления

Target: checkout `/home/ubuntu/receipt-paybot` на разрешённом сервере `howbot`.
До изменения tracked files чистые; существовавшие `backups/` и
`scripts/smoke_receipt_history.py` сохранены. Контейнер БД был healthy.

Защищённая копия с правами каталога 0700:
`/home/ubuntu/backups/receipt-paybot-20261016T061218Z`.

Сохранены PostgreSQL custom dump, `.env`, Compose, все исходные файлы счетов,
локальный smoke-скрипт, архив исходного commit и прежний Docker image.
Для согласованного снимка bot остановлен; БД продолжала работать.

Dump восстановлен с `pg_restore --exit-on-error` в отдельный контейнер
`receipt-paybot-restore-20261016T061218Z`. Его `network=none` и tmpfs data
512 MiB подтверждены inspect. Для всех четырёх таблиц (`receipts`,
`payment_drafts`, `audit_logs`, `authorized_users`) совпали количество и
агрегированные fingerprints всех строк. Конфигурация и файлы восстановлены в
отдельный каталог; SHA256 каждого файла совпал с источником. Контрольные суммы
всех backup-артефактов проверены. Evidence хранится в `verified.json` и
`sha256sums.txt` внутри backup; секретные значения в Git не переносились.

Временные контейнер и каталог восстановления удалены после проверки.
Backup сохранён. Это копия на том же сервере, не off-site/DR readiness.
`testd` не использовался.

## Установка и эксплуатация

Checkout fast-forward обновлён до execution SHA. Создан неизменяемый release
каталог `/home/ubuntu/receipt-paybot-releases/3160f7d1af660e0d3aedc24970ba6bbf4566ade2`.
В `compose.release.yml` закреплены:

```yaml
services:
  bot:
    image: receipt-paybot:3160f7d1af660e0d3aedc24970ba6bbf4566ade2
    pull_policy: never
```

Ранее отсутствовавший `docker-compose.override.yml` в runtime checkout —
ссылка на этот файл. Обычный Compose разрешает тот же exact image, что
проверено через `docker compose config`. Установка из checkout:

```sh
docker compose -p receipt-paybot -f docker-compose.yml \
  -f /home/ubuntu/receipt-paybot-releases/3160f7d1af660e0d3aedc24970ba6bbf4566ade2/compose.release.yml \
  up -d --no-build --no-deps --pull never bot
```

Runtime image совпал с CI image ID; bot running, restart count 0,
`Run polling for bot` подтверждено, ошибок старта нет. ID контейнера БД
сохранился, health healthy. SQL-схема и настройки интеграций не менялись;
отдельная миграция не требовалась. Банковский LIVE-режим сохранён.

## Проверка оригинала после установки

Проверка выполнена внутри установленного image с пустым `OPENAI_API_KEY`,
read-only SQL-транзакцией и без вызовов банковских/CRM методов. Использованы
`DocumentTextExtractor`, supplier guard, preflight и финальная техническая
проверка. Название правильной стороны получено из явного блока поставщика
самого PDF; остальные проверяемые реквизиты — из сохранённой операции.

Все условия выполнены:

- оригинал читается и блок поставщика однозначен;
- правильный набор проходит preflight и финальный технический контроль;
- старый смешанный набор отклоняется обоими уровнями;
- ошибки правильного набора отсутствуют;
- внешнее AI-распознавание и создание платежа не выполнялись.

Автоматическая проверка разрешений отклонила повторную отправку исходного
счёта внешнему OpenAI. Использован безопасный локальный сценарий выше.
Поэтому на реальном счёте подтверждена детерминированная защита, но не новый
полный ответ внешней модели. Модельные ошибки, противоречивые/неполные ответы
и порядок pipeline проверены регрессионными тестами. Telegram UI с новым
платежом не прогонялся, чтобы не создавать банковскую запись.

После установки `SELECT DB_NAME()` повторно подтвердил `Terrasoft_test`;
`CRM_DRY_RUN=false`, `MONOBANK_DRY_RUN=false`, `PRIVAT24_DRY_RUN=false`.
Полная карта полей и вопросы приёмки: [самари для бухгалтера](../crm-accountant-review.md).

## Rollback

Прежний image сохранён как `receipt-paybot:rollback-20261016T061218Z` и в
backup-архиве. Его ID:
`sha256:13e5b5e5723ed211f188e45b71bdc7ef469530578f55b0e08198a71631169d10`.
В backup есть `compose.rollback.yml`, закрепляющий этот image.

Для разрешённого отката переключить только bot и указатель default Compose:

```sh
cd /home/ubuntu/receipt-paybot
ln -sfn /home/ubuntu/backups/receipt-paybot-20261016T061218Z/compose.rollback.yml docker-compose.override.yml
docker compose -p receipt-paybot -f docker-compose.yml \
  -f /home/ubuntu/backups/receipt-paybot-20261016T061218Z/compose.rollback.yml \
  up -d --no-build --no-deps --pull never bot
```

После отката проверить image ID, running/restart count, polling и DB health.
Восстановление БД при откате этого изменения не требуется: оно могло бы
потерять новые пользовательские операции. Данный rollback не выполнялся;
путь возврата подготовлен, старый образ сохранён.

## Оставшаяся приёмка

Неподдерживаемые/неоднозначные счета требуют ручной проверки. Защита не даёт
гарантии отсутствия всех OCR-ошибок. Бухгалтерская приёмка нужна для выбора
счёта CRM по банку, фактической даты/статуса оплаты, обязательных аналитик,
контрагентов и защиты от повторного хозяйственного счёта. Эти правила этим
релизом не менялись.
