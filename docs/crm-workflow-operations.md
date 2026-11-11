# Эксплуатация файлов и подтверждённой оплаты

## Подготовка и установка

Все команды выполняются из чистого checkout отправленного execution SHA
после successful exact-SHA CI: checks, package-build, foundation-instance.
Образ берётся из artifact этого CI, проверяются SHA256 архива, image ID и
OCI revision. На сервере исходники не собираются заново.
Проверочные контейнеры получают effective environment через
`scripts.backup_bot.compose_environment()` → Docker `--env-file /dev/stdin`.
Это важно: Compose снимает кавычки dotenv, обычный Docker `--env-file .env`
сохраняет их в значениях. Environment не печатается и не копируется в файл;
helper отклоняет multiline/NUL значения. Startup/actual DB_NAME guard
остаётся обязательным независимо от способа передачи конфигурации.

1. `scripts/verify_terrasoft_backup.py` без `--execute`: read-only plan.
   После проверки свободного места, точной цели и отсутствия новых путей
   запустить `--execute --execution-sha SHA --ci-run-id ID --evidence-dir DIR`.
   DIR — отдельный существующий каталог 0700. COPY_ONLY/CHECKSUM backup,
   VERIFYONLY и реальный restore создают новые файлы и restricted clone.
   Production/test не заменяются. Проверка CHECKDB PHYSICAL_ONLY не является
   полным логическим CHECKDB. Это локальная копия, а не off-site DR.
2. Из того же exact image выполнить `scripts/rehearse_crm_files_posting.py`
   с `--verified-backup VERIFIED_JSON --execute --execution-sha SHA
   --ci-run-id ID --evidence-dir DIR`. Проверяется ownership/isolation на
   соединении; ни одного банковского HTTP запроса. Все synthetic записи и
   триггерные эффекты откатываются outer transaction. Успешный journal
   содержит `passed=true`, hash backup journal и подтверждение rollback.
3. На host выполнить `python3 -m scripts.backup_bot --run-id RUN_ID
   --execution-sha SHA --expected-image OLD_IMAGE_ID --ci-run-id ID --execute`.
   Временный restore PostgreSQL — network none/tmpfs. Равенство всех таблиц
   и файлов проверяется до установки. При успехе бот остаётся остановлен
   для согласованного update; при ошибке выполняется restart старого бота.
4. В течение 30 минут выполнить `python3 -m scripts.deploy_bot --execution-sha SHA
   --ci-run-id ID --backup BACKUP_DIR --crm-evidence VERIFIED_JSON
   --rehearsal-evidence REHEARSAL_JSON --execute`. Script проверяет journals,
   checksums, target, native policy/ACL, неизменность env/override и старого
   image. Меняется только bot service и release override. Новая таблица
   `crm_operations` создаётся штатным idempotent `init_db`; старые таблицы
   и CRM schema не мигрируются. Source flag default остаётся false;
   release override включает новую возможность только установленному боту.
5. Проверить polling, exact runtime image/target, неизменный healthy DB,
   нулевой restart count и отсутствие новых исторических задач. Не
   создавать банковский платёж только ради smoke. Фактический ordinary
   workflow бухгалтер проверяет по номеру RCPT/CF в боте, CRM и банке.
6. После успешного deployment — отдельный `verify_terrasoft_backup.py
   --cleanup VERIFIED_JSON --execute ...`. Удаляется только собственный
   проверенный clone после повторной проверки backup; сам backup остаётся.
   Очистить локальный disposable PostgreSQL тестов. Финальное evidence
   отправить документационным commit, повторно проверить remote SHA/CI.

## Ошибка и восстановление

Monitor делает одну фазу на сохранённой задаче. Сеть/CRM недоступны —
повтор с backoff до 15 минут; иной target, несовпадение реквизитов, изменённый
файл, чужая связь, отмена и неопределённые состояния не обходятся повтором.
В `/crm_status RCPT…` видны исходный файл и сохранённая стадия. Не следует
загружать тот же счёт или создавать второй draft ради повтора CRM.

Если контрагент отсутствовал, бухгалтер выбирает запись с точным кодом
получателя; monitor перепроверяет код перед проведением. Финансовые поля,
связанный резерв/перенос и неизвестный статус автоматически не исправляются.
Прочие исправления и будущие банковские возвраты — штатные действия CRM.

При исключении во время переключения deploy script возвращает предыдущий
release override/image и проверяет его Running/restarts + DB identity.
При раннем отказе preflight он может запустить только доказанно неизменный
старый контейнер; изменённую цель не трогает и сообщает отказ recovery.

Для планового отката сначала остановить bot, проверить backup checksums и
identity, вернуть прежний release override и запустить только bot без build
и pull. `.env` текущий update не меняет. Сверить baseline image/CRM target,
polling/DB health. Новая `crm_operations` остаётся; штатного DROP нет.
Оригиналы/подтверждения и проведённые операции в CRM сохраняются. Ни bot DB,
ни CRM backup не восстанавливаются поверх новых рабочих операций для
обычного отката приложения. Такой restore — отдельное решение владельца.

## Ограничения наблюдаемости

SQL и native metadata подтверждают bytes, links, ACL и эквивалент записи.
Это не доказательство открытия файла в Windows UI под конкретной ролью.
Первая обычная оплата: открыть оба вложения в детали «Файлы», проверить
статус, знак/сумму, дату банка, счёт и договор. Ранее принятый fixed CashAccount
mapping остаётся предметом бухгалтерской проверки для второго банка.
