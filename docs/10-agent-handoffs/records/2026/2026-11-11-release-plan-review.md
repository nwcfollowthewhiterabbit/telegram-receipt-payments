# Handoff: проверка update gates и восстановления прежнего бота

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T18:43:05Z`
- Статус: `completed` — локальный review и tests, без runtime выполнения.
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`
- Scope: review backup/deploy scripts, recovery helper и regression tests.

## Цель

Независимо проверить подготовленный основным агентом воспроизводимый update
ReceiptFlow перед source commit и выполнением. Исключить успешный статус
rollback без проверки фактической цели и запуск прежнего бота на изменённой цели.

## Исходное состояние

`scripts/backup_bot.py` и `scripts/deploy_bot.py` создавались основным агентом
на основе предыдущих approved backup/deploy процедур. Они не выполнялись этим
агентом. Чужие изменения services, adapters, моделей и runtime не затронуты.

## Изменения и причины

При review переданы основному агенту и исправлены им:

- сравнение текущего release override с сохранённым backup перед swap;
- проверка exact прежнего image, running/restart count и identity/health DB
  после rollback, с отдельным результатом rollback;
- digest и точная связь rehearsal с backup record, clone, source SHA, CI run,
  успешным завершением и rollback всех fixture writes;
- отклонение symlink-аргументов до resolve;
- маркер остановки перед командой stop, чтобы частичный отказ команды
  не обходил восстановление прежнего бота.

По последующему поручению основного агента этим агентом изменены только
`recover_baseline_if_unchanged` и её вызов из `main` в `scripts/deploy_bot.py`:
ранний отказ `--execute` может запустить прежний остановленный контейнер,
только если свежий backup (не старше 30 минут) подтверждает остановку, совпадают
source/CI, checksums, конфигурация, override, image и здоровье DB. Запуск идёт
по прочитанному container ID. Затем повторно проверяются id/image/running,
restart count и DB identity/health. Изменённая или неопределённая цель
не запускается; read-only вызов не пытается восстанавливать runtime.

`tests/test_bot_release_plan.py` добавляет 16 tests: default read-only,
identity/collision/health gates, stale backups, изменённая конфигурация,
неверный artifact, symlink/tampered evidence, точная связь rehearsal,
только bot update, rollback success/failure и осторожный baseline recovery.

## Проверки

- `python -m unittest discover -s tests -p 'test_bot_release_plan.py' -v`
  — **16 PASS**. Все subprocess/Docker calls заменены проверяющими mocks;
  файловые объекты только в temporary directory, без network/runtime calls.
- `git diff --check` — **PASS**.
- Реальные Docker/SQL операции и exact-SHA CI этим suite не подменяются.
  Общий source suite выполняет интегрирующий агент.

## Runtime, backup и rollback

Runtime не изменялся. Ни backup, ни deploy, ни restart/rollback на сервере
не выполнялись. Commit/push не выполнялись. Результаты tests доказывают
локальные защитные ветки, а не фактическое восстановление production.

## Риски и незавершённое

Неизменность source SHA/CI и целевого состояния должна быть подтверждена
в штатной runtime-фазе. Если backup устарел или identity не совпадает,
recovery намеренно требует review; это не подтверждение работоспособности
прежнего бота. Persistent данные не восстанавливаются поверх новых записей.

## Следующий шаг

Основному агенту включить helper/tests в общий review и exact-SHA CI;
после разрешённых backup/restore и rehearsal выполнить update и сохранить
фактическое evidence image, health, source/target и пользовательского сценария.
