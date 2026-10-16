# Локальный outbox реестра возможностей

Каталог хранит проверенные наблюдения о существенных возможностях Receipt Pay
Bot. Он не заменяет требования, инструкции и release evidence. Центральный
реестр — `nwcfollowthewhiterabbit/rabbit-erp-customization-registry`.

Перед реализацией проверьте локальный outbox и доступный центральный каталог.
После проверки реализации создайте отдельный immutable JSON для одной логической
возможности в `capabilities/outbox/`. Для новой возможности
`relationships.capability_ids` остаётся пустым; известный аналог указывается по
`CAP-ID` или исходному `OBS-ID`. Архитектурное размещение и reuse оценивает
центральный reviewed registry, а не проект.

Контракт версии `1.0.0` использует схему
[observation.schema.json](schemas/observation.schema.json), SHA-256 которой
закреплён в [config.json](config.json). Клиент схемы и validator переиспользованы
из проекта HOWERP без изменения transport contract. Шаблон записи находится в
[capability-observation.md](../docs/templates/capability-observation.md).

Для этого репозитория без `project.toml` identity берётся из `project_key` в
config. После rename прежний key добавляется в `project_aliases`, старые записи
не переписываются. Если появится `project.toml`, его `source.repository` обязан
совпадать с текущим key.

`source_revision` содержит полный SHA commit с проверенной реализацией.
`implementation.repository_paths` и все evidence paths должны указывать на
обычные файлы именно в этом commit. Поэтому CI получает полную Git-историю.
Новая запись после source commit может отправляться одним push с ним.

В outbox запрещены исходные сообщения, персональные данные, коммерческие
документы, source data, секреты, URL, hostname, IP и runtime-координаты.
Используйте только очищенное описание поведения и repository-relative paths.
После reviewed import запись не переписывается; исправления и изменения
получают новый `OBS-ID`.

Проверка работает offline и не обращается к центральному реестру:

```bash
python3 scripts/validate_capability_observations.py
```

Недоступность реестра не блокирует CI и работу бота; наблюдение остаётся локально
до reviewed import. Центральный importer копирует исходный JSON byte-for-byte,
проверяет provenance и сопоставляет repository identity.
