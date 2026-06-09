# Receipt Pay Bot

Docker-проект для Telegram-бота, который:

- принимает от сотрудников фото счетов на оплату;
- проверяет, достаточно ли читаются реквизиты для создания платежки;
- если все ок, создает черновик платежки в выбранном банке (`Приват24 для бизнеса` или monobank corp API);
- не подписывает платеж;
- пишет аудит всех действий в собственную PostgreSQL-базу.

## Что уже реализовано

- `aiogram`-бот с приемом фото.
- PostgreSQL + SQLAlchemy модели `receipts`, `payment_drafts`, `audit_logs`.
- Сохранение изображений в volume.
- Vision/OCR-проверка счета на оплату через OpenAI Responses API.
- Контрольная проверка финальной платежки перед отправкой в банк: сверка распознанных реквизитов, суммы, валюты, назначения и получателя.
- Выбор банка в Telegram после распознавания счета: `Приват24` или monobank.
- Dry-run режим для банковских адаптеров, чтобы можно было поднять проект до подключения реального API банка.
- Connector-слой для банков, CRM и Telegram-границы бота.
- Белый список `ALLOWED_USER_IDS`.

## Что именно проверяет текущий MVP

Из изображения счета бот пытается извлечь:

- наименование поставщика;
- ЕДРПОУ/ИНН;
- IBAN;
- банк поставщика;
- МФО;
- номер счета;
- дату счета;
- сумму;
- валюту;
- назначение платежа.

Черновик платежки создается только если удалось уверенно распознать минимум:

- `supplier_name`
- `supplier_iban`
- `amount`
- `currency`
- `payment_purpose`

Перед вызовом банковского API бот дополнительно проверяет финальную платежку. Если сумма, IBAN, валюта, получатель или назначение не совпадают с распознанным счетом, платеж не отправляется в банк и документ переводится в ручную проверку. Назначение платежа нормализуется с датой в формате `DD.MM.YYYY`, обязательным префиксом из счета и ПДВ/без ПДВ, если это указано в документе.

## Ограничение текущего MVP

Интеграция с банками вынесена в отдельные адаптеры. После успешной preflight-проверки бот спрашивает в Telegram, откуда платить: `Приват24` или monobank. `PAYMENT_PROVIDER` остается провайдером по умолчанию для служебных режимов и обратной совместимости.

Для `Privat24` используется официальный `Автоклієнт API`: `POST https://acp.privatbank.ua/api/proxy/payment/create`, а подпись идет отдельным шагом через `GET /api/proxy/payment/get` и `POST /api/proxy/payment/add-sign`.

Для `monobank` используется корпоративный API: `POST https://corp-api.monobank.ua/ext/v1/payment/prepare`. Запрос создает черновик платежа без подписи и возвращает `id`.

Официальные источники:

- https://privatbank.ua/business/intehratsiya
- https://api.privatbank.ua/index.html
- https://docs.google.com/document/d/e/2PACX-1vTtKvGa3P4E-lDqLg3bHRF6Wi9S7GIjSMFEFxII5qQZBGxuTXs25hQNiUU1hMZQhOyx6BNvIZ1bVKSr/pub

Поэтому в проекте банковская логика вынесена отдельно в [src/clients/privat24.py](/home/wroot/receipt-paybot/src/clients/privat24.py:1) и [src/clients/monobank.py](/home/wroot/receipt-paybot/src/clients/monobank.py:1). По умолчанию для безопасности оставлены dry-run режимы.

CRM-интеграция выбирается через `CRM_PROVIDER`. Сейчас доступны:

- `none` — CRM отключена.
- `terrasoft_mssql` — запись распознанного счета и банковского черновика в Terrasoft/XRM v3 через MS SQL.

Для Terrasoft live-режима нужны `TERRASOFT_MSSQL_URL` и `TERRASOFT_INVOICE_TABLE`. До согласования точной таблицы и колонок держите `CRM_DRY_RUN=true`: бот сохранит в аудит payload, который ушел бы в CRM.

## Быстрый старт

1. Скопируйте env:

```bash
cp .env.example .env
```

2. Заполните минимум:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `ALLOWED_USER_IDS`
- `PAYMENT_PROVIDER` (`privat24` или `monobank`)
- `COMMUNICATION_PROVIDER` (`telegram`)
- `PRIVAT24_API_BASE_URL`
- `PRIVAT24_API_TOKEN`
- `PRIVAT24_SOURCE_ACCOUNT`

3. Поднимите проект:

```bash
docker compose up --build -d
```

4. Логи:

```bash
docker compose logs -f bot

## Как тестировать парсинг до банкового API

Через Telegram уже можно отправлять:

- обычное фото;
- изображение как файл (`document` с mime `image/*`);
- `PDF`;
- `XLS`;
- `XLSX`.

Бот вернет распарсенные поля и покажет, чего не хватает для создания платежки.

Для пакетной локальной проверки по папке с инвойсами:

```bash
cd /home/wroot/receipt-paybot
python3 -m scripts.test_invoice_parsing /path/to/invoices
```

Скрипт выведет JSON по каждому файлу отдельно. Это удобно для сравнения качества парсинга на разных счетах до подключения `Privat24`.

Для `PDF` текущий пайплайн сначала извлекает текст. Если PDF состоит только из сканов без текстового слоя, может понадобиться отдельный OCR-рендеринг страниц.
```

## Основные переменные

- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота.
- `OPENAI_API_KEY` — ключ для OCR/vision-проверки счета.
- `OPENAI_MODEL` — модель vision-проверки.
- `ALLOWED_USER_IDS` — список Telegram user id через запятую.
- `DATABASE_URL` — строка подключения к PostgreSQL.
- `RECEIPT_STORAGE_DIR` — директория хранения изображений.
- `PAYMENT_PROVIDER` — активный банковский адаптер: `privat24` или `monobank`.
- `PAYMENT_DRY_RUN` — общий override dry-run режима; если пустой, используется настройка выбранного провайдера.
- `PRIVAT24_API_BASE_URL` — базовый URL API банка.
- `PRIVAT24_API_TOKEN` — токен интеграции.
- `PRIVAT24_SOURCE_ACCOUNT` — счет списания.
- `PRIVAT24_PAYMENT_DATE` — дата списания в формате `DD.MM.YYYY`.
- `PRIVAT24_PAYMENT_ACCEPT_DATE` — дата зачисления/валютирования в формате `DD.MM.YYYY`.
- `PRIVAT24_DRY_RUN` — если `true`, вместо реального запроса создается локальный черновик-имитация.
- `MONOBANK_API_BASE_URL` — базовый URL корпоративного API monobank.
- `MONOBANK_API_TOKEN` — токен корпоративного API monobank.
- `MONOBANK_SOURCE_IBAN` — IBAN счета списания; если пустой, бот попробует получить гривневый счет через `/ext/v1/accounts`.
- `MONOBANK_DRY_RUN` — если `true`, вместо реального запроса создается локальный черновик-имитация.
- `CRM_PROVIDER` — активный CRM-коннектор: `none` или `terrasoft_mssql`.
- `CRM_DRY_RUN` — если `true`, CRM-коннектор не пишет в CRM, а возвращает payload для аудита.
- `TERRASOFT_MSSQL_URL` — SQLAlchemy URL подключения к тестовой базе, например `mssql+pymssql://DOMAIN%5Cuser:pass@192.168.112.20:1433/Terrasoft_test`.
- `TERRASOFT_DATABASE` — разрешенная база для live-записи; сейчас коннектор принимает только `Terrasoft_test`.
- `TERRASOFT_INVOICE_TABLE` — таблица операций, сейчас только `dbo.tbl_Cashflow`.
- `TERRASOFT_COLUMN_MAP` — зарезервировано для будущих CRM-коннекторов; `tbl_Cashflow` маппится явно.
- `COMMUNICATION_PROVIDER` — активный adapter входящего канала, сейчас поддерживается `telegram`.

## Структура

- [docs/ARCHITECTURE_ROADMAP.md](/home/wroot/receipt-paybot/docs/ARCHITECTURE_ROADMAP.md:1) — архитектурный регламент, roadmap и вопросы для финализации требований.
- [src/main.py](/home/wroot/receipt-paybot/src/main.py:1) — точка входа.
- [src/bot/handlers.py](/home/wroot/receipt-paybot/src/bot/handlers.py:1) — Telegram-обработчики.
- [src/services/receipt_pipeline.py](/home/wroot/receipt-paybot/src/services/receipt_pipeline.py:1) — пайплайн счета на оплату.
- [src/connectors/payments/registry.py](/home/wroot/receipt-paybot/src/connectors/payments/registry.py:1) — выбор платежного коннектора.
- [src/connectors/crm/registry.py](/home/wroot/receipt-paybot/src/connectors/crm/registry.py:1) — выбор CRM-коннектора.
- [src/connectors/communication/registry.py](/home/wroot/receipt-paybot/src/connectors/communication/registry.py:1) — выбор коммуникационного adapter.
- [src/services/vision.py](/home/wroot/receipt-paybot/src/services/vision.py:1) — OCR/vision-валидация счета.
- [src/clients/privat24.py](/home/wroot/receipt-paybot/src/clients/privat24.py:1) — адаптер Privat24.
- [src/connectors/payments/privat24_receipt_monitor.py](/home/wroot/receipt-paybot/src/connectors/payments/privat24_receipt_monitor.py:1) — Privat24-specific монитор квитанций.
- [src/clients/monobank.py](/home/wroot/receipt-paybot/src/clients/monobank.py:1) — адаптер monobank.
- [src/connectors/crm/terrasoft_mssql.py](/home/wroot/receipt-paybot/src/connectors/crm/terrasoft_mssql.py:1) — адаптер Terrasoft/XRM v3 через MS SQL.

## Проверка архитектуры

```bash
python -m scripts.check_architecture
```

Скрипт проверяет, что service layer не импортирует Telegram/aiogram напрямую, и что registry собирает активные payment/CRM connectors.
- [src/db/models.py](/home/wroot/receipt-paybot/src/db/models.py:1) — модели БД.

## Что я бы сделал следующим шагом

1. Уточнил обязательные реквизиты платежки именно под ваш тип счетов и юрлицо.
2. Подключил реальный endpoint `Privat24` по выданному токену и зафиксировал контракт запроса/ответа.
3. Добавил ручное подтверждение в Telegram перед созданием черновика платежа.
4. Добавил отдельную таблицу сотрудников и роли.
