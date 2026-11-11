# Handoff: изолированный rehearsal файлов и проведения CRM

- Агент: `Codex /root/crm_accountant_summary`
- Время UTC: `2026-11-11T18:43:18Z`
- Статус: `partial`
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`
- Scope: rehearsal artifact, unit tests, дополнительная защита already-posted.

## Цель

Подготовить исполняемую интеграционную проверку новой синхронизации только на
отдельной базе, восстановленной `verify_terrasoft_backup.py`. SQL-мутации
rehearsal разрешаются основным агентом после exact-SHA CI и backup gates.

## Исходное состояние

Прочитаны API/evidence backup verifier, существующие connector/helpers и
handoff template. В checkout находились параллельные изменения других агентов;
они не переписывались. Это продолжение
[native posting handoff](2026-11-11-native-cashflow-posting.md).

Read-only schema подтвердил обязательные fixture-поля: Account ID/Name,
Contract ID/Title, Offering и OfferingType ID/Name, OfferingInContract ID.
Fixture заполняет эти поля и нужные связи/числа. CFNumber — nvarchar(250),
поэтому синтетические HOWERP-E2E номера помещаются. RecordID в CashflowLog и
DatabaseLog подтверждён. Закрытая база, credentials и business rows не выводились.

## Изменения и причины

- `scripts/rehearse_crm_files_posting.py` — default read-only preflight;
  execute требует verified backup JSON, совпадающие execution SHA/CI reference
  и приватный каталог evidence. Production connector guard не меняется;
  clone URL строится только после проверки source/server fingerprint,
  ownership/GUID/id/create_date/file identity, schema, restricted access,
  public CONNECT denial и выключенных broker/trust/chaining. Идентичность
  повторно проверяется на соединении, выполняющем транзакцию.
- Dependency guard проверяет реально достижимые native триггеры по
  `sys.dm_sql_referenced_entities.is_updated` и trigger events. Отвергаются
  cross-database/server references, dynamic EXEC, внешние SQL механизмы,
  CLR, synonyms, unresolved dependencies. Чтение lookup не приводит к
  предположению, что его INSERT/DELETE триггеры исполняются.
- В одной outer transaction создаются только синтетические Account,
  Cashflow, Contract, Offering/OfferingType/OfferingInContract и файлы.
  Используются существующие lookup/owner values без их вывода. Проверяются
  bytes/hash/link/ACL оригинала и банковских данных, повторы вложений,
  проведение/дата/знак/PeriodID, налоговый договорный пересчёт с доходом,
  native ACL, повтор проведения, amount/LinkID и manual-done mismatch.
  Savepoint проверяет rollback после уже выполненного проведения и добавления
  подтверждения, когда следующий шаг обнаруживает конфликт файла.
- Outer transaction всегда rollback; после успеха проверяется отсутствие
  fixtures, связей, прав и scoped audit rows. База и backup не удаляются;
  окончательная очистка clone остаётся отдельным verifier `--cleanup`.
  Bank HTTP не вызывается. Итоговый JSON содержит `execution_sha`,
  `passed:true`, `verified:true`, `stage:rehearsal_passed` для deploy gate.
- `tests/test_crm_files_posting_rehearsal.py` — 15 проверок provenance,
  isolation, untouched production guard, dependency graph, rollback,
  default read-only CLI, release references и sanitized evidence/errors.
- `src/connectors/crm/terrasoft_posting.py` и
  `tests/test_terrasoft_posting.py` — по замечанию review `_verify_snapshot`
  выполняется также для уже проведённой операции, до ответа already_posted.
  Изменённая сумма/получатель не позволяют прикрепить подтверждение исходного
  платежа к вручную изменённой операции. Добавлен regression test.

## Проверки

- `python -m unittest discover -s tests -p 'test_crm_files_posting_rehearsal.py'`
  из согласованного virtualenv — **15 PASS** для итогового harness.
- `python -m unittest discover -s tests -p 'test_terrasoft_posting.py'` —
  **21 PASS** после already-posted snapshot correction.
- Точная функция dependency guard выполнена read-only на actual CRM metadata:
  **51 reachable local objects, PASS**. Никакие сценарии fixture SQL не запускались.
- `git diff --check` — PASS. Полные repository/unit checks и exact-SHA CI
  выполняет основной агент для собранного набора source.

## Runtime, backup и rollback

Runtime не изменялся. Были только SELECT схемы/metadata на существующем
подключении. Backup, restore, fixture INSERT/UPDATE, deploy, commit и push
этим агентом не выполнялись. Скрипт ещё не имеет integration PASS evidence.

## Риски и незавершённое

Нативные SQL inserts/triggers и фактическая длина/сохранение bytes должны
пройти реальный rehearsal на owned restored clone. Присутствие тестов и
schema preflight не заменяет этого. Harness проверяет ACL-строки и native
условия видимости; интерактивный вход владельца в UI не выполняется.
Конкурентные отдельные SQL-сессии этот rollback-only harness не моделирует.
Доказательство exact-SHA успешного CI проверяется deploy orchestration;
сам harness сверяет предоставленные ссылки с verifier evidence.

## Следующий шаг

После source push и успешного exact-SHA CI основному агенту восстановить
изолированный clone через verifier и запустить harness сначала без execute,
затем разрешённый execute; сохранить итоговый JSON для deploy gate.
