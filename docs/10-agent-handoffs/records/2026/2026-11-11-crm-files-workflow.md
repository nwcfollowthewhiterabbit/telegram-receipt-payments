# Handoff: файлы и проведение подтверждённых оплат

- Агент: `/root` (Codex)
- Время UTC: `2026-11-11T18:39:38Z`
- Статус: `partial`
- Начальный commit: `2b576c98cfbd31581f5ec7401d409ca091321b84`
- Scope: код ReceiptFlow, требования, проверки, подготовка update/backup

## Цель

По поручению пользователя изучить штатные «Файлы» и проведение Terrasoft,
добавлять оригинал и подтверждение банка, проводить операцию после оплаты.
Для monobank пользователь явно выбрал JSON банковской выписки и ручное
добавление PDF. Банковская подпись/оплата человеком не меняется.

## Исходное состояние

Рабочая база уже включена после приёмки тестовой интеграции. Baseline:
74 tests PASS, architecture/outbox PASS. Изучены native metadata файлов,
Cashflow, прав и договоров read-only. В ReceiptFlow checkout чужих изменений
не было; незавершённые файлы исходного HOWERP workspace не изменялись.

Центральный capability registry проверен на revision
`8cbe87181bb231599cefa0040bad96ca2ec43d49`: аналоги приватных вложений
CAP-SEC-0004/CAP-SEC-0006 и CAP-UI-0002 относятся к другой платформе.
Прямого аналога Terrasoft settlement не найдено; общий код не заимствован.

## Изменения и причины

- `src/db/models.py`, `src/services/crm_sync.py` — новая долговременная задача
  на один банковский черновик, постоянный UUID, уникальное закрепление
  банковской транзакции, независимые фазы создания/подтверждения/проведения.
- `src/connectors/crm/attachments.py`, `terrasoft_mssql.py` — нативные bytes,
  связь с операцией, SHA256, повтор без дубля, чтение результата после
  триггеров; ограничения размера сервиса и native MaxFileSize, права владельца.
- `src/services/receipt_pipeline.py` — задача сохраняется одной локальной
  транзакцией с черновиком, исходный счёт и операция одной CRM-транзакцией.
- `src/connectors/communication/crm_monitor.py`, `src/main.py` — возобновление
  задач без повторного банковского создания, сохранение доставки Приват24.
- Старый receipt monitor исключает новые задачи; checkpoint доставки
  продублирован в прежнем формате для совместимости отката приложения.
- `src/config.py` — новая возможность выключена по умолчанию, poll/size bounds.
- Bank adapters, нативное проведение, UI и профильные проверки описаны в
  отдельных записках участников; root проверил границы транзакций и повторов.
- `.github/workflows/ci.yml` — изолированный PostgreSQL для реальных проверок
  outbox/constraints; image regression сохраняет режим без сети.
- `scripts/backup_bot.py`, `scripts/deploy_bot.py` — проверяемый local backup
  с реальным restore, exact image gate и атомарное переключение только бота.
  DB restore не применяется поверх новых данных при откате приложения.
- `docs/crm-files-and-payment-posting.md` — принятие требования, инварианты,
  native mapping, backup и сценарий отказа. Руководство пока помечено candidate.

## Проверки

На готовом наборе изменений: `python -m unittest discover -s tests -p
"test_*.py"` — 249 tests PASS, включая реальные PostgreSQL scenarios в
одноразовой local DB; `python -m scripts.check_architecture`,
`python3 scripts/validate_capability_observations.py`, `git diff --check`
— PASS. Проверка credential patterns в 45 изменённых файлах — 0 hits.
Exact-SHA CI и restored-CRM rehearsal будут подтверждены отдельным release
evidence; эта записка их не утверждает. Остальные runtime gates не пройдены.
Найденные при review случаи исправлены: повторная проверка банка перед
проведением; changed source/confirmation; symlink containment; actual file
readback; финансовый snapshot проверяется даже для уже проведённой операции.

## Runtime, backup и rollback

Runtime приложения и рабочие данные не изменялись. Исследование CRM — SELECT.
Локально создан одноразовый PostgreSQL в Docker/tmpfs для тестов; перед
завершением его контейнер удаляется. Внешнее запрещённое backup-хранилище
не использовалось. Scripts backup/deploy пока не исполнялись с --execute.

## Риски и незавершённое

Обязательны exact-SHA CI, full SQL backup/actual restore, нативный integration
rehearsal, fresh bot backup/restore, установка и smoke. Проверка штатного
Windows UI под бухгалтерской ролью не подменяется SQL-проверками ACL.
Сохраняется ранее принятый fixed CashAccount mapping; в его справочнике
нет IBAN, безопасно вывести иной счёт для второго банка невозможно.
Автоматического перепроведения истории и отмены после будущего банковского
возврата нет; такие случаи обрабатываются бухгалтером штатно.

## Следующий шаг

Завершить профильные проверки и отправить source, затем выполнить exact-SHA
CI и восстановление CRM для безопасного интеграционного rehearsal.
