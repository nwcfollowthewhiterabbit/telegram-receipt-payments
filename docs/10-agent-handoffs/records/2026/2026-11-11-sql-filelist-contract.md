# Handoff: точный контракт RESTORE FILELISTONLY

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T19:03:25Z`
- Статус: `completed` — причина подтверждена read-only, исправление и tests.
- Начальный commit: `e68519fc3b49007b439b5dd898ee7693e33fc426`
- Scope: backup verifier, metadata fixture и regression.

## Цель

Установить причину безопасного отказа verifier после создания COPY_ONLY backup,
исправить подтверждённое расхождение API-контракта без повторных SQL-мутаций
в ходе диагностики.

## Исходное состояние

Основной агент сообщил об успешном создании новой копии и последующем отказе
на этапе `backup_header`. Фактическое восстановление не выполнялось, поэтому
эта копия не является verified restore evidence.

В точном CI image начального commit прочитаны только metadata источника,
`RESTORE HEADERONLY` и `RESTORE FILELISTONLY`. Эффективное окружение Compose
передано через stdin без вывода или сохранения. Диагностика вернула только
количество строк, названия/типы колонок и boolean соответствия header flags.
SQL-ошибок и stderr не было; credentials, пути и бизнес-строки не выводились.

## Изменения и причины

- `scripts/verify_terrasoft_backup.py` — оба обращения к ключу `FileID`
  исправлены на реальное имя колонки `FileId`. SQL Server 2019 отдаёт именно
  такой регистр, а Python mapping case-sensitive.
- `tests/test_terrasoft_backup_plan.py` — FILELISTONLY fixture использует
  наблюдавшиеся названия/типы metadata, исключительно синтетические значения.
  Положительный restore sequence проходит на реальном контракте; дополнительная
  regression отклоняет ошибочный старый ключ до вызова RESTORE DATABASE.

Header содержал одну строку и соответствующие ожидаемые COPY_ONLY/CHECKSUM
flags без повреждения; FILELISTONLY содержал две строки. Предыдущий fixture
повторял ошибочное имя поля, поэтому начальные unit tests не обнаруживали
расхождение с фактическим сервером.

## Проверки

- `python -m unittest discover -s tests -p 'test_terrasoft_backup_plan.py' -v`
  — **20 PASS**, изолированный Python 3.12 venv, все mutation branches mocked.
- `git diff --check` — **PASS**.
- Подтверждение первопричины: read-only HEADERONLY/FILELISTONLY, exit 0,
  только безопасные metadata outputs.

## Runtime, backup и rollback

Этим агентом SQL-мутации не выполнялись. Повторный BACKUP, RESTORE DATABASE,
CREATE/ALTER/DROP не запускались. Первая копия остаётся без доказанного
восстановления; она не удаляется и не перезаписывается.

По решению основного агента resume не добавляется: после source push и
exact-SHA CI будет создана отдельная свежая копия штатным verifier. Нового
непроверенного пути продолжения старой операции нет. Commit/push этим агентом
не выполнялись.

## Риски и незавершённое

Исправление устраняет установленный mismatch имени колонки; фактическое
восстановление и последующие проверки ещё требуют успешного runtime evidence.
CI success предыдущего artifact не выдаётся за успешный restore.

## Следующий шаг

Выполнить source commit/push, exact-SHA CI и штатный новый backup/restore
с новыми уникальными именами, сохранив обе копии и раздельное evidence.
