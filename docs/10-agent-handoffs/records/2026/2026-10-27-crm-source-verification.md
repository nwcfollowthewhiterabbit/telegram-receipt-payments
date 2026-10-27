# Handoff: source verification CRM target guard

- Агент: `codex /root`.
- Время UTC: `2026-10-27T08:30:00Z`.
- Статус: `partial`, runtime не изменялся.
- Начальный commit: `cb051cce2c5635a7ae35363234ada3a568abbc65`.
- Scope: `immutable capability observation, source verification evidence`.

## Цель и изменения

Добавлено новое observation explicit-crm-database-target с exact source SHA.
CAP-ID не назначался; центральный реестр не изменялся. Этот commit добавляет
только observation/evidence, функции и тесты остаются из source commit.

## Проверки

Source implementation: 74 unit tests PASS, architecture PASS, outbox PASS,
diff --check PASS. Python 3.12.11 с зависимостями requirements и dummy CI env.
Новый outbox проходит повторную валидацию перед commit. Pure suite повторно
не нужен только из-за observation; exact execution CI обязан подтвердить
финальный revision, image build и изолированные image tests.
HOWERP проверки и сохранённые чужие изменения описаны в предыдущей записи.

## Runtime, backup, rollback

Runtime не изменялся, бизнес-данные не изменялись. Source откатывается новым
Git commit. Runtime update требует отдельного проверенного backup и exact
CI; контракт — docs/crm-production-cutover.md.

## Риски и следующий шаг

Проверить remote SHA/CI и установить образ с согласованной конфигурацией,
выполнить SQL rollback smoke без сохранённой операции и подтвердить health.
