# Handoff: исправление FK синтетической связи rehearsal

- Агент: `Codex /root/crm_accountant_summary`
- Время UTC: `2026-11-11T20:10:00Z`
- Статус: `partial`
- Начальный commit: `661dd7e212b0b12d395801cf41e235344e40f5ad`
- Scope: только rehearsal script, его regression test и эта запись.

## Цель

Устранить фактический сбой native rehearsal, не меняя продуктовую логику
проведения и её отказ для связанных переносов. Основной агент разрешил
диагностику и полный candidate Scenario на уже проверенном owned clone
с обязательным outer rollback, без банковских запросов и production writes.

## Исходное состояние

Exact execution прошёл CI и revalidation внешнего backup. Rehearsal завершился
ошибкой после проверки оригинала и rollback частичного проведения; ошибка
отката savepoint скрывала исходную ошибку SQL при включённом XACT_ABORT.
Основной агент подтвердил отсутствие всех исходных 12 fixture UUID после
outer rollback. Изменений других агентов в рабочем дереве не было.

Read-only проверка native FK установила причину: `tbl_Cashflow.LinkID`
ссылается на `tbl_CashflowInCashflow.ID`. Harness передавал UUID другой
Cashflow вместо UUID relation. У relation обязательный только ID;
ParentID/ChildID ссылаются на Cashflow. INSERT/UPDATE triggers отсутствуют.

## Изменения и причины

- `scripts/rehearse_crm_files_posting.py`: `transfer_link_fixture` сначала
  создаёт синтетическую relation с ParentID/ChildID, затем устанавливает
  её UUID в LinkID. Это достигает продуктовой проверки неподдерживаемой
  связи без нарушения FK. Relation включена в dependency guard и registry
  UUID для проверки cleanup.
- `tests/test_crm_files_posting_rehearsal.py`: regression проверяет native
  relation target, оба FK, отличие LinkID от UUID дочерней Cashflow и
  регистрацию для cleanup.
- Продуктовый PostingService и его отказ `crm_posting_link_requires_review`
  не менялись. Эта запись дополняет
  [rehearsal handoff](2026-11-11-crm-rehearsal-harness.md).

## Проверки

- `python -m unittest discover -s tests -p 'test_crm_files_posting_rehearsal.py'`
  из baseline virtualenv — **16 PASS**.
- `git diff --check` — PASS.
- Полный candidate Scenario запущен на том же verified owned restricted
  clone. Только кандидатный harness монтировался read-only в exact image;
  все продуктовые модули оставались из начального execution SHA.
  Harness SHA256: `1c724c3048a9b5a8fd9119d43f8d1b9cfe1330e5e97840cef9c01edc27bc8073`.
- Dependency guard — **52 local objects PASS**. Все шесть групп завершены:
  `source_bytes_link_acl_repeat`,
  `post_and_attachment_failure_rolled_back`,
  `wrong_bank_amount_and_transfer_link_refused`,
  `native_post_date_sign_period_contract_acl`,
  `posted_and_bank_file_repeat`,
  `manually_changed_posted_amount_refused`.
- Outer rollback завершён; `assert_rolled_back` подтвердил отсутствие всех
  **13** tracked fixture UUID, включая relation, а также проверяемых
  Cashflow/Contract audit, ACL и file links. Банковских HTTP вызовов — 0.

## Runtime, backup и rollback

Рабочий bot, рабочие CRM-данные и конфигурация не менялись. В owned restored
clone выполнены синтетические INSERT/UPDATE внутри одной транзакции и
savepoints; все изменения откатились. Новый backup/restore не выполнялся.
Диагностический контейнер использовал read-only rootfs и временный read-only
mount harness; после завершения контейнер и временный host-файл удалены.
Environment передавался штатным compose_environment без печати/сохранения.

## Риски и незавершённое

Candidate PASS не является exact-source release evidence. После commit/push
и нового exact-SHA CI основной агент должен повторить guarded revalidation
и rehearsal из конечного image до deploy. Самим агентом commit/push/deploy
не выполнялись; существующий owned clone оставлен для этих gates.

## Следующий шаг

Основному агенту включить исправление fixture в source commit и выполнить
точные CI/revalidation/rehearsal gates конечного артефакта.
