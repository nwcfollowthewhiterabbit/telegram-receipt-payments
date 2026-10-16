# Наблюдение о возможности

Создайте `capabilities/outbox/<observation_id>.json` после проверки реализации и
source commit. Значения ниже — пример структуры, а не готовое observation.
Замените дату, SHA и пути фактическими; сначала прочитайте
[правила outbox](../../capabilities/README.md).

```json
{
  "contract_version": "1.0.0",
  "dependencies": {
    "apps": [],
    "doctypes": [],
    "erpnext_versions": [],
    "external_services": [],
    "frappe_versions": []
  },
  "differences": [],
  "event": "implemented",
  "evidence": {
    "adr_paths": [],
    "repository_paths": ["docs/ARCHITECTURE_ROADMAP.md"],
    "test_paths": ["tests/test_feature.py"]
  },
  "feature": {
    "business_problem": "Проверяемая проблема одного процесса.",
    "category": "integration",
    "description": "Фактическое поведение проверенной реализации.",
    "local_id": "example-feature",
    "name": "Одна логическая возможность"
  },
  "implementation": {
    "apps": ["receipt-paybot"],
    "doctypes": [],
    "kind": "original",
    "modules": ["src.services.example"],
    "repository_paths": ["src/services/example.py"]
  },
  "observation_id": "OBS-example-feature-20261016",
  "observed_at": "2026-10-16T00:00:00Z",
  "privacy_review": {
    "contains_personal_data": false,
    "contains_raw_feedback": false,
    "contains_runtime_urls": false,
    "contains_secrets": false,
    "reviewed": true
  },
  "project_key": "nwcfollowthewhiterabbit/telegram-receipt-payments",
  "relationships": {
    "capability_ids": [],
    "observation_ids": []
  },
  "schema_version": 1,
  "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Для `reused` укажите известный `CAP-ID` либо исходный `OBS-ID`; для `varied`
дополнительно заполните `differences`. Не добавляйте архитектурные рекомендации
или число reuse: это ответственность центрального registry. Запускайте
`python3 scripts/validate_capability_observations.py` после каждого нового JSON.
