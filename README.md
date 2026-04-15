# Receipt Pay Bot

Docker-проект для Telegram-бота, который:

- принимает от сотрудников фото счетов на оплату;
- проверяет, достаточно ли читаются реквизиты для создания платежки;
- если все ок, создает черновик платежки в `Приват24 для бизнеса`;
- не подписывает платеж;
- пишет аудит всех действий в собственную PostgreSQL-базу.

## Что уже реализовано

- `aiogram`-бот с приемом фото.
- PostgreSQL + SQLAlchemy модели `receipts`, `payment_drafts`, `audit_logs`.
- Сохранение изображений в volume.
- Vision/OCR-проверка счета на оплату через OpenAI Responses API.
- Автоматический переход к созданию черновика платежа.
- Dry-run режим для `Privat24`, чтобы можно было поднять проект до подключения реального API банка.
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

## Ограничение текущего MVP

Интеграция с `Приват24` переведена на официальный `Автоклієнт API`. Для гривневого платежа используется `POST https://acp.privatbank.ua/api/proxy/payment/create`, а подпись идет отдельным шагом через `GET /api/proxy/payment/get` и `POST /api/proxy/payment/add-sign`.

Официальные источники:

- https://privatbank.ua/business/intehratsiya
- https://api.privatbank.ua/index.html
- https://docs.google.com/document/d/e/2PACX-1vTtKvGa3P4E-lDqLg3bHRF6Wi9S7GIjSMFEFxII5qQZBGxuTXs25hQNiUU1hMZQhOyx6BNvIZ1bVKSr/pub

Поэтому в проекте адаптер вынесен отдельно в [src/clients/privat24.py](/home/wroot/receipt-paybot/src/clients/privat24.py:1), а по умолчанию для безопасности оставлен `PRIVAT24_DRY_RUN=true`.

## Быстрый старт

1. Скопируйте env:

```bash
cp .env.example .env
```

2. Заполните минимум:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `ALLOWED_USER_IDS`
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
- `PRIVAT24_API_BASE_URL` — базовый URL API банка.
- `PRIVAT24_API_TOKEN` — токен интеграции.
- `PRIVAT24_SOURCE_ACCOUNT` — счет списания.
- `PRIVAT24_PAYMENT_DATE` — дата списания в формате `DD.MM.YYYY`.
- `PRIVAT24_PAYMENT_ACCEPT_DATE` — дата зачисления/валютирования в формате `DD.MM.YYYY`.
- `PRIVAT24_DRY_RUN` — если `true`, вместо реального запроса создается локальный черновик-имитация.

## Структура

- [src/main.py](/home/wroot/receipt-paybot/src/main.py:1) — точка входа.
- [src/bot/handlers.py](/home/wroot/receipt-paybot/src/bot/handlers.py:1) — Telegram-обработчики.
- [src/services/receipt_pipeline.py](/home/wroot/receipt-paybot/src/services/receipt_pipeline.py:1) — пайплайн счета на оплату.
- [src/services/vision.py](/home/wroot/receipt-paybot/src/services/vision.py:1) — OCR/vision-валидация счета.
- [src/clients/privat24.py](/home/wroot/receipt-paybot/src/clients/privat24.py:1) — адаптер банка.
- [src/db/models.py](/home/wroot/receipt-paybot/src/db/models.py:1) — модели БД.

## Что я бы сделал следующим шагом

1. Уточнил обязательные реквизиты платежки именно под ваш тип счетов и юрлицо.
2. Подключил реальный endpoint `Privat24` по выданному токену и зафиксировал контракт запроса/ответа.
3. Добавил ручное подтверждение в Telegram перед созданием черновика платежа.
4. Добавил отдельную таблицу сотрудников и роли.
