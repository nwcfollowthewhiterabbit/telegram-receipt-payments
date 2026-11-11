# Handoff: передача эффективного окружения Compose

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T18:50:35Z`
- Статус: `completed` — локальные regression tests.
- Начальный commit: `e0d4b50d33ec3affdd4493bf587dece306d72118`
- Scope: tests и отдельная delta-запись к release-plan review.

## Цель

Закрепить исправление способа передачи окружения в проверочный контейнер,
не раскрывая значения настроек в аргументах команд, stdout или stderr.

## Исходное состояние

Exact-SHA CI `43080646674` для начального commit завершился успешно во всех
трёх jobs; image artifact был загружен и проверен по source revision label.
Позже основной агент сообщил, что runtime preflight безопасно отказал:
Docker `--env-file` сохранил кавычки из dotenv, а target guard отклонил
полученное имя базы. SQL-мутации не выполнялись. Это runtime-конфигурационный
отказ, не ошибка завершившегося CI.

Основной агент исправил `compose_environment()` в `scripts/backup_bot.py`
и вызов preflight в `scripts/deploy_bot.py`. Эти исходники этой подзадачей
не изменялись. Запись дополняет
[предыдущий review](2026-11-11-release-plan-review.md).

## Изменения и причины

- `tests/test_bot_release_plan.py` — mocks поддерживают read-only
  `docker compose config --format json`; проверяют эффективное значение
  имени базы без dotenv-кавычек, сохранность допустимых буквальных кавычек,
  символов `$`, `#`, `=` и пустых строк.
- Проверяются отказ при NUL/CR/LF, недопустимых ключах и нестроковых значениях;
  безопасная ошибка при неуспешной команде Compose.
- Deployment preflight получает bytes исключительно как subprocess input
  для `--env-file /dev/stdin`. Значения отсутствуют в argv/stdout/stderr,
  исходный dotenv не используется непосредственно как Docker env-file.
- Локальная метаинформация первого artifact дополнена примечанием об этом
  runtime preflight отказе; CI success и checksum самого artifact не изменены.

## Проверки

- `python -m unittest discover -s tests -p 'test_bot_release_plan.py' -v`
  — **20 PASS** (16 прежних + 4 новых regression tests), Python 3.12.
  Все Docker/subprocess вызовы mocked; только synthetic values и temp files.
- `git diff --check` — **PASS**.
- Новый exact-SHA CI и фактический preflight остаются у основного агента.

## Runtime, backup и rollback

Runtime этой подзадачей не изменялся. SQL/банковские операции не выполнялись.
Новые backup/restore/deploy не запускались; commit/push не выполнялись.

## Риски и незавершённое

Mock tests проверяют транспорт окружения и redaction, но не заменяют
проверку нового CI artifact на фактической конфигурации. Эффективные значения
окружения нельзя печатать или сохранять; helper хранит их только в памяти.

## Следующий шаг

Отправить исправление и tests, подтвердить exact-SHA CI и повторить безопасный
preflight с эффективным окружением Compose перед SQL backup/restore.
