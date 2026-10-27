# Handoff: ReceiptFlow подключён к рабочей CRM

- Агент: `codex /root`.
- Время UTC: `2026-10-27T08:38:17Z`.
- Статус: `completed` для переключения; первая обычная операция проверяется бухгалтером.
- Начальный commit source checkout: `cf3ea057fbc126da8c502eb1fb3ed2bdfb427360`.
- Начальный локальный commit этой evidence-записи: `0ed9f2b71373c4ffe502f5c9b55fe4bdd7a4375f`.
- Scope: `ReceiptFlow runtime config/image, production CRM target, release evidence`.

## Основание и результат

По подтверждению заказчика через пользователя интеграция принятого тестового
контура переключена на рабочую CRM того же сервера. Runtime target — Terrasoft,
запись включена. Маппинг, банковские настройки и тестовая история не менялись.

## Изменения

- Runtime bot переведён на exact CI image `0ed9f2b…`; `.env` изменён только в
  имени базы и database части URL. Credentials и остальные settings сохранены.
- Default Compose override закрепляет release image. Локальная БД и receipt
  files сохранены; схемы не мигрировались.
- Добавлен [release protocol](../../../releases/2026-10-27-crm-production.md).
  Обновлены README, CHANGELOG, user manual uk, architecture roadmap,
  accountant summary и cutover contract по фактически установленному состоянию.
- Временные restore container/files удалены, защищённые backup и ops evidence
  оставлены на сервере. Чужие изменения HOWERP и untracked runtime files сохранены.

## Проверки

- Source: 74 tests, architecture, outbox и diff PASS; exact execution CI
  40473832524 completed/success с package-build и foundation-instance.
- Загружен CI artifact; checksum, OCI revision и actual image ID совпали.
- Fresh backup восстановлен изолированно: 4/4 таблиц и все конфигурационные/
  пользовательские файлы совпали по fingerprints/SHA256.
- 7/7 fixed references и структура рабочей CRM совпали с принятой тестовой.
- Контрольный INSERT с реальными SQL-конструкциями коннектора прочитан обратно,
  ROLLBACK прошёл; отдельным соединением 0 остаточных строк во всех 4 таблицах.
  Период использован существующий, банковские методы не вызывались.
- Post-switch: DB_NAME Terrasoft, CRM пишет; оба банка LIVE; bot running,
  restart count 0, polling, нет ошибок; исходная PostgreSQL healthy.
- Pure unit suite не повторяется после документационных изменений: source,
  tests и зависимости неизменны. Финальный docs commit отдельно проходит CI.

## Backup и rollback

Backup и точные команды приведены в release protocol. Сохранены старый image,
.env и Compose override; rollback обязан вернуть их вместе. Проверен restore
локальных данных бота, не всей внешней CRM. SQL smoke не оставил операций;
production schema/history не менялись. testd не использовался.

## Ограничения и следующий шаг

UI конкретной роли в рабочей CRM не прогонялся. У runtime SQL-login нет
AdminUnit в обеих базах, ожидаемые 0 строк прав совпадают с принятым тестовым
поведением. При первой обычной рабочей операции бухгалтер проверяет видимость
и поля; создавать отдельный банковский платёж для проверки не требуется.
Существующие тестовые операции не переносить повторной загрузкой счета.
