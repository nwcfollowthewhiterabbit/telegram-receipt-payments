# Handoff: CI и автономный контракт capability outbox

- Агент: `codex/release_checks`
- Время UTC: `2026-10-16T05:57:09Z`
- Статус: `completed`
- Начальный commit: `0727d97e3517a8d49a777184d07d9f9eae08af8e`
- Scope: `CI, локальный capability contract и документация`

## Цель

Обеспечить воспроизводимую проверку исправления определения поставщика и
локальную запись проверенной возможности с точным source SHA. Основание —
поручение основного агента в рамках пользовательского запроса об исправлении
банковского бота и обязательного процесса repository/CI/outbox.

## Исходное состояние

В исходном checkout нет CI workflow, capability outbox и `project.toml`.
Существуют unit-тесты и `python -m scripts.check_architecture`. Рабочее дерево
при первом чтении было чистым. Параллельные изменения основных services и
бухгалтерского summary принадлежат другим агентам и не редактировались.

Read-only просмотр центрального `generated/catalog.md` подтвердил 51 capability;
аналога разделения поставщика и получателя услуг при формировании банковского
черновика в каталоге не выявлено. Центральный реестр не изменялся.

## Изменения и причины

- `.github/workflows/ci.yml` — CI для main, PR и ручного запуска: Python 3.12,
  pinned project dependencies, unit, architecture и outbox. Полная Git-история
  нужна для проверки evidence в exact source commit. Workflow не содержит
  credentials и не выполняет deploy.
- `capabilities/config.json` — identity этого репозитория и SHA-256 transport
  schema; отсутствие `project.toml` поддерживается автономным контрактом.
- `capabilities/schemas/observation.schema.json`,
  `scripts/_capability_contract.py`,
  `scripts/validate_capability_observations.py` — byte-for-byte reuse контракта
  версии 1.0.0 из HOWERP. Validator offline и использует только stdlib/Git.
- `capabilities/outbox/.gitkeep` — сохраняет пустой каталог до source commit.
- `capabilities/README.md`, `docs/templates/capability-observation.md` — правила
  observations, privacy, provenance и неизменяемости, адаптированные к этому
  Python-проекту без ERP runtime-зависимостей.
- `docs/10-agent-handoffs/template.md` — шаблон записи из HOWERP, ранее отсутствовал.
- Эта отдельная handoff-запись — область ответственности данного агента.

## Проверки

- `python3 scripts/validate_capability_observations.py` — exit 0 для config,
  схемы и пустого outbox на исходном commit с перечисленными новыми файлами.
- `PYTHONPYCACHEPREFIX=/private/tmp/receipt-paybot-pycache python3 -m py_compile
  scripts/_capability_contract.py scripts/validate_capability_observations.py`
  — exit 0. Первый запуск без override cache завершился отказом macOS sandbox
  на запись bytecode вне workspace; исходники не менялись, повтор с временным
  cache успешен.
- Ruby YAML parser — CI workflow прочитан, шесть ожидаемых steps подтверждены.
- Сравнение байтов контрактных scripts/schema с HOWERP и SHA-256 schema с config
  — совпадение подтверждено.
- `git diff --check` — exit 0 для текущего tracked diff.
- Unit/architecture и remote exact-SHA CI будут проверены основным агентом для
  полного готового набора исходников. Эта запись не заявляет их успех заранее.

## Runtime, backup и rollback

Runtime не изменялся. Запросы к центральному GitHub registry были read-only.
Банк, CRM, PostgreSQL и данные пользователей не изменялись. Для отмены этого
набора удалить добавленные данным агентом файлы до commit либо сделать revert
их source commit; изменения других агентов сохраняются.

## Риски и незавершённое

Observation ещё не создан: ему нужен точный SHA проверенной реализации.
Полный CI результат и готовность runtime не входят в выполненный этим агентом
scope. Отдельные ERP repository/readiness validators в Python-бот не переносились.

## Следующий шаг

Основному агенту проверить полный набор, зафиксировать source commit, создать
immutable observation на его SHA и подтвердить CI для итогового отправленного
revision.
