# Handoff: backup закрытых файлов подтверждения оплаты

- Агент: `Codex /root/release_checks`
- Время UTC: `2026-11-11T19:12:49Z`
- Статус: `completed` — исходники и focused tests; Docker privilege rehearsal ожидается.
- Начальный commit: `d098cf6612b231edaa6bea55088cc0884c20a5fa`
- Scope: `scripts/backup_bot.py`, профильные release tests.

## Цель

Устранить обнаруженную основным агентом несовместимость будущего backup с
закрытыми файлами подтверждения оплаты: бот создаёт каталоги 0700 и файлы 0600
от имени container root, поэтому host-пользователь может не прочитать их.
Права файлов и основной пользовательский процесс не ослабляются.

## Исходное состояние

Source и CI предыдущего commit проверены. Старый backup использовал host tar
и `Path.rglob`; недоступная вложенная директория могла не попасть в hash inventory.
Core services, режимы файлов и runtime не меняются этой подзадачей.

## Изменения и причины

- `tree_hash` использует `os.walk` с обязательной ошибкой на недоступном
  каталоге. Симлинки и специальные файлы отклоняются через lstat; частичный
  inventory не считается успешной проверкой.
- Исходный receipt tree читается внутри exact baseline image с root uid,
  `--network none`, `--read-only`, `--user 0:0`, явным Python entrypoint и
  единственным read-only bind. Выполняется fixed hash implementation текущего
  проверенного checkout; baseline image не обязан содержать новый helper.
  Relative names и hashes используются только в памяти, не печатаются.
- Runtime archive создаётся явным tar entrypoint в таком же изолированном
  контейнере. ROOT монтируется read-only, а внешний resolved override — отдельным
  exact file bind, тоже read-only. Host-файл открывается exclusive с mode 0600;
  архив идёт в него через stdout, stderr перехватывается и не публикуется.
- После восстановления на хосте source hash из readonly helper сравнивается
  с host hash восстановленных файлов. Исходные права 0700/0600 не меняются.
- `tests/test_bot_release_plan.py` — пять новых focused tests и обновлённые
  command mocks: private tree coverage, отказ при недоступности/симлинке/FIFO,
  exact mounts/entrypoints, streamed archive и safe failure/no overwrite.

## Проверки

- `python -m unittest discover -s tests -p 'test_bot_release_plan.py' -v`
  — **25 PASS**. Fixed helper Python реально прочитал synthetic 0700/0600 tree;
  локальный tar реально создал архив private fixture и external override.
  Docker invocations заменены проверяющими mocks, содержимое fixtures synthetic.
- `git diff --check` — **PASS**.
- Проверка наличия предложенного локального Docker fixture image вернула
  `No such image`; image не скачивался и не создавался. Поэтому реальное чтение
  root-owned host files через Docker этой подзадачей не заявляется.

## Runtime, backup и rollback

Runtime не изменялся. Контейнеры не запускались, production файлы не читались
и не менялись; SQL/банковские действия не выполнялись. Новые backup/deploy
не выполнялись. Commit/push выполняет основной агент после review.

## Риски и незавершённое

Нужны exact-SHA CI и фактическая temporary Docker fixture проверка с root-owned
0700/0600 перед backup. Поведение сохраняет отказ при недоступности, неизвестном
типе файла или неподдерживаемой цепочке override symlinks; таких случаев оно
не пропускает молча. Backup остаётся локальным, off-site readiness не меняется.

## Следующий шаг

Основному агенту проверить diff, выполнить source/CI, затем controlled fixture
с закрытыми synthetic файлами и штатный backup/restore до включения функции.
