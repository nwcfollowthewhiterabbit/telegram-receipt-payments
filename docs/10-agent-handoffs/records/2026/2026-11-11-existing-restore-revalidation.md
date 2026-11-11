# Handoff: проверка уже восстановленного клона после исправления image aggregate

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T19:46:25Z`
- Статус: `completed` — source и focused tests; actual revalidation не запускалась.
- Начальный commit: `05b8085b3dbb3b686824d3eee255569c2f514a56`
- Scope: эксплуатационный verifier и его tests, без изменения основного процесса.

## Цель

Исправить установленную ошибку подсчёта legacy image blobs и повторно проверить
уже восстановленный owned restricted clone, сохранив backup и предыдущие
immutable records. Избежать повторного создания и восстановления той же копии.

## Исходное состояние

Основной агент подтвердил, что COPY_ONLY backup, фактическое восстановление,
DBCC и сравнение схемы завершились, после чего проверка отказала на
`attachment_reads`. Фактический SQL Server вернул error 8117 для
`COUNT_BIG(FileData)`: native `FileData` имеет тип `image`.

Основной агент отдельно выполнил исправленные read-only запросы: aggregate,
три bounded hash reads, связи и права файлов прошли. Исторические расхождения
размера файлов остаются показателем в отчёте; их исправление не входит в scope.
Production строки, файлы и права не менялись в ходе этой диагностики.

## Изменения и причины

- `scripts/verify_terrasoft_backup.py` — используется
  `COUNT_BIG(CASE WHEN FileData IS NOT NULL THEN 1 END)`: считается integer
  expression, сохраняя число ненулевых blobs без aggregate над image.
- Новый `--revalidate-existing OLD_DIR/RUN-003-restore_restricted.json`
  проверяет digest каждого исходного record 000..003, previous-hash цепочку,
  совпадение run/source/CI/context и доказанные этапы backup/checksum/restore.
  Вход `failed` или произвольное имя базы не принимается.
- Свежие проверки сопоставляют source/server identity, права sysadmin,
  clone marker/GUID/id/create time, restricted/public DENY и защитные настройки,
  hash физических путей, exact backup basename и hash backup path.
- HEADERONLY `BackupSetGUID` и `BindingID` сопоставляются с единственной
  database restore записью `msdb.restorehistory`/`backupset` для клона и source
  GUID. Дополнительно проверяются full restore/full backup, recovery и отсутствие
  REPLACE. Это отклоняет другой валидный backup с тем же именем и FILELIST.
- С `--execute` повторяются HEADERONLY/FILELISTONLY, VERIFYONLY CHECKSUM,
  DBCC CHECKDB PHYSICAL_ONLY, сравнение текущей source/clone схемы и чтение
  native attachment metadata/bounded bodies. BACKUP, RESTORE DATABASE,
  ALTER, DROP и исправление данных отсутствуют в этом пути.
- Требуется новый пустой private evidence directory. Старые records не
  переписываются. Ownership run_id сохраняется, новый финальный record содержит
  new execution SHA/CI, original execution SHA/CI и hash исходного restore record.
  Финальный basename — `RUN-003-verified.json` в новом каталоге.
- Без `--execute` читаются только chain и metadata/identity, journal не создаётся.
  Повтор в заполненный directory завершается отказом.
- `tests/test_terrasoft_backup_plan.py` — десять новых regression tests:
  default read-only, real image aggregate contract, целая/tampered/missing chain,
  изменённая identity/права/пути/backup, только обязательные read checks,
  fresh physical check, lineage, отказ при сбоях и immutable directories,
  другой backup set/source и отсутствующая/неоднозначная restore history.

## Проверки

- `python -m unittest discover -s tests -p 'test_terrasoft_backup_plan.py' -v`
  — **30 PASS**, Python 3.12, synthetic metadata и mocked administrative adapter.
- `git diff --check` — **PASS**.
- Independent focused source review агентом `/root/crm_accountant_summary`
  выявил gap backup-set lineage; добавлена проверка через restore history.
  Основной агент read-only подтвердил все использованные поля и совпадения
  UUID на фактическом SQL Server. Общий suite и exact-SHA CI выполняет основной
  агент после review.

## Runtime, backup и rollback

Runtime этой подзадачей не изменялся. SQL/банковские запросы этим агентом
не выполнялись. Фактическую диагностику и corrected read-only probe выполнил
основной агент. Commit/push не выполнялись.

Предыдущие backup/clone/evidence сохраняются. Новый verifier не объявляет их
готовыми, пока повторные checksum/physical/schema/attachment checks не завершены.
SQL backup checksum по-прежнему не называется SHA256 всего backup-файла.

## Риски и незавершённое

Нужны новый source commit/CI и actual revalidation. PHYSICAL_ONLY остаётся
физической проверкой, не полной бизнес-валидацией исторических данных.
После успешной revalidation native rehearsal обязан использовать новый verified
record и тот же execution SHA/CI. Старый evidence directory монтируется целиком
read-only для проверки sibling chain; новый каталог монтируется отдельно.

## Следующий шаг

После review и exact-SHA CI выполнить revalidation retained clone с новым
private evidence directory, затем native rehearsal и штатные bot backup/deploy.
