# Handoff: подготовка переключения CRM после приёмки

- Агент: `codex /root`.
- Время UTC: `2026-10-27T08:26:22Z`.
- Статус: `partial`, source готовится к CI; runtime пока не изменён.
- Начальный commit: `cf3ea057fbc126da8c502eb1fb3ed2bdfb427360`.
- Scope: `CRM target guard, startup, tests, требования/операционный контракт`.

## Цель и основание

Пользователь подтвердил приёмку заказчиком бота с тестовой базой и разрешил
перенос интеграции на рабочую базу на том же сервере. Изменение ограничено
целевым подключением, без переноса тестовой истории и изменения маппинга.

## Baseline и изменения

- Серверный source совпадает с baseline, tracked tree чистый; существующие
  backups, локальный smoke script и release Compose link сохранены.
- Работающий image соответствует execution SHA `3160f7d…`, bot running,
  restart count 0, PostgreSQL healthy. Все три имени CRM-базы — тестовая цель.
- Агент supplier_guard_design изменил CRM guard/startup и добавил 14 регрессий;
  его отдельная запись описывает точные файлы. Root обновил README,
  `.env.example`, CHANGELOG, architecture roadmap, статус приёмки в
  accountant summary и добавил `docs/crm-production-cutover.md`.
- Локальный outbox и центральный registry проверены перед реализацией.
  CAP-CRM-0001 не является аналогом этой возможности; новой capability не
  назначается CAP-ID. Exact-SHA observation будет добавлен после source commit.

## Проверки

- Текущий source baseline: 60 tests и architecture PASS в Python 3.12.
  Сначала обнаружены неполный старый временный venv и отсутствие dummy token;
  создан новый venv по requirements, команда повторена с CI-compatible env.
- SQL discovery: доступны только test/production цели на том же сервере;
  таблицы и FK совпадают, нужные права есть; 7 фиксированных lookup references
  совпадают по идентифицирующим полям; текущий период существует; CFNumber
  пригодны для существующего алгоритма. SQL Server не имеет TRY_CONVERT,
  проверка числовых суффиксов адаптирована без изменения данных.
- Read-only review INSERT-триггеров: только локальные транзакционные записи,
  без внешних/необратимых side effects; допустим bounded rollback smoke.
- Независимый review нового guard: блокирующих замечаний нет, 14 focused tests
  PASS; suite/architecture/outbox проверяются для объединённого набора.
- HOWERP `87a1f279…` + сохранённое чужое dirty tree: repository 0;
  unittest 0 (1012 tests); capability 0; readiness ожидаемый 1/scaffold.
  До/после status одинаковый. HOWERP Git-файлы не менялись.

## Runtime, backup и rollback

Runtime не изменялся. Выполнены только SELECT metadata/lookup checks без
печати credentials и business records. Нет новых банковских/CRM операций.
Контракт backup, переключения и отката: `docs/crm-production-cutover.md`.

## Риски и следующий шаг

Нужны source+observation push, exact-SHA CI, проверенный свежий backup бота,
установка точного image с согласованной парой settings, rollback SQL smoke,
health и отдельное release evidence. Внешняя CRM не мигрируется. При откате
возвращаются одновременно image и обе настройки базы. Согласованный маппинг
с его ограничениями сохраняется.
