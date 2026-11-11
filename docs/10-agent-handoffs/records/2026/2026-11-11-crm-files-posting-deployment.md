# Handoff: установленное сопровождение файлов и оплаты в CRM

- Агент: `Codex /root`
- Время UTC: `2026-11-11T20:30:08Z`
- Статус: `completed` — реализация и установка; UI/обычная оплата при приёмке.
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`.
- Scope: ReceiptFlow source/tests/docs, SQL backup/owned clone, bot release.

## Цель

По поручению пользователя исследовать native «Файлы» и проведение Terrasoft,
прикреплять оригинал и банковское подтверждение, проводить ту же CRM-операцию
только после исполнения банка. Для monobank согласованы данные выписки и
ручное добавление PDF. Автоматические реальные тестовые платежи не входили
в scope и не создавались.

## Исходное состояние

Продуктовый checkout — отдельный telegram-receipt-payments. Рабочий HOWERP
и его существующие чужие изменения не редактировались. Baseline ReceiptFlow
74 tests PASS; runtime execution `0ed9f2b71373c4ffe502f5c9b55fe4bdd7a4375f`,
CRM `Terrasoft`, оба банка LIVE, PostgreSQL healthy. Тестовая CRM не переносилась.

## Изменения и причины

- Durable `CrmOperation` связывает один новый банковский черновик с постоянным
  CRM UUID, целевой базой, snapshot и уникальным подтверждением банка.
- Native files/link/ACL и posting service реализованы с атомарностью, readback,
  проверкой ручных изменений, банковской даты, знака суммы и договора.
- Строгие settlement adapters подтверждают исполнение и принадлежность оплаты;
  исторические records не включаются. Пользователь видит украинские статусы
  и авторизованную read-only команду `/crm_status`.
- Эксплуатационные scripts обеспечивают exact-image update, backup/restore,
  owned restricted SQL clone, native rehearsal и guarded recovery.
- Продуктовые source/tests изменения и независимые reviews записаны в отдельных
  handoffs этой даты. При native проверках исправлены FileId metadata,
  aggregate image и fixture LinkID; рабочий бот до прохождения gates не менялся.
- `docs/releases/2026-11-11-crm-files-posting.md` фиксирует фактический release.
  README, changelog, процесс и украинская инструкция обновлены отдельным
  агентом; его handoff сохранён. В Git добавлен handoff read-only bank probe.
- В README исправлены старые абсолютные ссылки на несуществующий checkout:
  заменены переносимыми repository-relative ссылками; фактические targets проверены.
- Временные диагностические scripts вне Git использовали только bounded
  metadata/fixtures, скрывали credentials/business rows и проверяли rollback.
  Существенные исправления перенесены в versioned scripts/tests до установки.

## Проверки

- Финальный source `4b9d5bbe4f4c4572730abf4c331cff932644995f`:
  local full suite **270 PASS, 7 PG skipped**; architecture/outbox/diff PASS.
- Exact CI [43094014777](https://github.com/nwcfollowthewhiterabbit/telegram-receipt-payments/actions/runs/43094014777):
  checks/package-build/foundation-instance success. PG job — 270 tests без
  skipped; foundation — 270 с ожидаемыми 7 PG skips. Проверки source повторены
  после последней правки; docs-only фиксация не требует повторного native run.
- Фактические SQL backup/restore выполнены ранее. Финальным image повторно
  пройдены lineage, VERIFYONLY, CHECKDB PHYSICAL_ONLY, schema, attachment
  metadata и три bounded body hash reads — PASS.
- Native rehearsal: 6 групп PASS, 52 dependency objects, 13 synthetic IDs;
  исходный файл/права/повторы, атомарность/отказы, дата/знак/период/договор,
  уже проведённая операция и ручная правка. Fixtures/ACL/links/scoped audit
  отсутствуют после outer rollback; bank HTTP calls 0.
- Backup бота восстановлен: все четыре PG tables/fingerprints, receipts и
  config совпали. Private file fixture предварительно прошёл на прежнем image;
  входы backup helper/baseline image не менялись, actual backup также PASS.
- Runtime с 20:27:07 до 20:29:09: exact image, actual target, flag true,
  оба банка LIVE, polling, healthy DB, restarts/error/traceback/deferrals=0,
  outbox/history replay=0. `.env` сохранился.
- Подробные hashes/UTC/journals приведены в release evidence, без секретов.

## Runtime, backup и rollback

Установлен только bot service в 20:27 UTC из CI artifact; PostgreSQL container
сохранён. Новая локальная таблица создана штатным init; внешняя CRM schema
не мигрировалась. Конфигурация feature находится в новом release override.

Проверенный backup бота `receipt-paybot-20261111T202518Z-c751e016` содержит
данные, файлы, config и прежний image. SQL COPY_ONLY backup локален на SQL
server; `testd` не использовался. Это не off-site/DR готовность.

Owned SQL clone удалён штатным verifier в 20:29:03 UTC, backup сохранён.
Временные PG restore/container/files удалены. Старый release/image/backup
сохранены. Rollback приложения возвращает прежний image/override, не затирая
новые рабочие данные; фактический rollback успешной новой установки не выполнялся.

Документационный commit/remote equality/CI фиксируются после push в отдельном
`final-documentation-verification.json` release directory; runtime остаётся
на exact execution image. Эта запись не заявляет CI ещё не созданного commit.

## Риски и незавершённое

Первая обычная новая оплата и открытие файлов под ролью бухгалтера остаются
приёмочным шагом. Реальный банк→CRM E2E, UI role session и параллельные native
SQL sessions при установке не выполнялись. Исторические 69 несоответствий
FileSize не исправлялись. Fixed CashAccount mapping для двух банков сохранён;
его бухгалтер сверяет отдельно. Возврат после проведения не сторнируется
автоматически. Эти ограничения не скрыты за успешными техническими проверками.

## Следующий шаг

Бухгалтер проверяет одну обычную новую оплату по украинской инструкции:
RCPT/CF, реквизиты, оригинал/подтверждение, счёт средств, состояние и дата банка.
