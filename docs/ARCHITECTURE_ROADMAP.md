# Receipt Pay Bot Architecture And Roadmap

## Purpose

Receipt Pay Bot receives supplier invoices from Telegram, extracts payment details, validates them, creates an unsigned bank payment draft, and records invoice/payment context for accounting and CRM workflows.

The project should stay connector-driven:

- Payment connectors: Privat24, Monobank, future banks.
- CRM connectors: Terrasoft/XRM v3 over MS SQL, future CRMs.
- Communication connectors: Telegram now, future channels if needed.
- Business logic layer: invoice parsing, validation, payment preflight, orchestration, audit.

## Current Architecture

The active entrypoint is `src/main.py`.

Main layers:

- `src/bot/handlers.py` is the Telegram adapter.
- `src/services/receipt_pipeline.py` is the orchestration/use-case layer.
- `src/services/vision.py` extracts invoice fields with OpenAI.
- `src/services/payment_preflight.py` normalizes and validates payment requisites.
- `src/connectors/payments/registry.py` selects the active payment connector.
- `src/connectors/crm/registry.py` selects the active CRM connector.
- `src/connectors/communication/registry.py` selects the active communication adapter.
- `src/connectors/communication/telegram.py` stores Telegram files and converts Telegram messages into transport-neutral incoming files.
- `src/clients/privat24.py` implements Privat24 payment draft creation and receipt APIs.
- `src/clients/monobank.py` implements Monobank corporate payment draft creation.
- `src/connectors/payments/privat24_receipt_monitor.py` handles Privat24-specific receipt polling and Telegram delivery.
- `src/connectors/crm/terrasoft_mssql.py` implements Terrasoft/XRM v3 MS SQL sync.
- `src/db/models.py` stores local receipts, payment drafts, authorized users, and audit log.

Runtime selection is controlled by environment variables:

- `PAYMENT_PROVIDER=privat24|monobank`
- `PAYMENT_DRY_RUN=` optional override for all payment connectors
- `CRM_PROVIDER=none|terrasoft_mssql`
- `CRM_DRY_RUN=true|false`
- `COMMUNICATION_PROVIDER=telegram`

Current production-like configuration on the server:

- Payment provider: `monobank`
- Payment mode: live
- CRM provider: `terrasoft_mssql`
- CRM mode: dry-run

## Architectural Rules

1. Business logic must not instantiate concrete external providers directly.

   Use connector registries. `ReceiptPipeline` should depend on connector interfaces and use-case data, not on concrete bank or CRM APIs.

2. Provider-specific behavior stays in provider-specific modules.

   Privat24 transaction polling and receipt printing should remain Privat24-specific. Monobank payment draft logic should stay in the Monobank connector.

3. The local database is the system of record for bot processing.

   External systems can fail. Every invoice must keep local status, parsed fields, provider payloads, and audit events.

4. External writes must be idempotent before live CRM sync is enabled.

   Terrasoft sync uses a stable `external_key` based on local `receipt_id`; live MS SQL sync must upsert by that key.

5. Dry-run modes are mandatory for new connectors.

   Any new bank, CRM, or notification connector must support safe validation without writing to external systems.

6. Secrets stay in `.env` only.

   Tokens, SQL credentials, IBANs, and Telegram tokens must not be committed or printed in logs.

## Known Gaps

- `MONOBANK_SOURCE_IBAN` is not configured yet. Monobank returned multiple UAH accounts, so the connector intentionally refuses automatic account selection.
- Terrasoft test mapping targets `Terrasoft_test.dbo.tbl_Cashflow`.
- Production Terrasoft writes are intentionally blocked in code. The live connector currently allows only `Terrasoft_test`.
- There is only one communication adapter today: Telegram.

## Terrasoft/XRM v3 Integration Plan

Phase 1: Schema Discovery

- Confirm whether Terrasoft should store invoices in an existing object/table or a new custom object.
- Identify table name, primary key strategy, required system columns, lookup fields, and owner/account/contact relations.
- Confirm field types for amount, dates, status, provider id, supplier data, and raw payload/audit reference.
- Confirm whether direct MS SQL writes are acceptable for this Terrasoft installation or whether application/API-level writes are required by operations policy.

Phase 2: Dry-Run Mapping

- Set `CRM_PROVIDER=terrasoft_mssql`.
- Keep `CRM_DRY_RUN=true`.
- Configure `TERRASOFT_COLUMN_MAP` to match the real table.
- Process several real invoice examples and inspect audit payloads.
- Verify that parsed data matches accounting expectations before enabling writes.

Phase 3: Live Upsert

- Use `tbl_Cashflow.CodPrivat` as the deterministic external key: `receipt-paybot:receipt:<id>`.
- Upsert `tbl_Cashflow` by `CodPrivat`.
- Resolve supplier account by `tbl_Account.TaxRegistrationCode` or `tbl_Account.Code`; if not found, leave `RecipientID` empty and write supplier details to `CommentsPayer`.
- Add CRM sync status fields locally or persist CRM external id in a dedicated table if needed.
- Enable `CRM_DRY_RUN=false`.
- Test with one controlled invoice and verify the Terrasoft record manually.

Phase 4: Operational Hardening

- Add retry policy for transient CRM failures.
- Add a command or admin action to re-sync failed CRM records.
- Add structured metrics or reports for processed invoices, failed preflight, bank draft failures, and CRM sync failures.

## Questions To Finalize The Correct Build

### Business Process

1. What is the exact lifecycle of an invoice: received, recognized, validated, bank draft created, CRM record created, approved, signed, paid, receipt delivered?
2. Who is responsible at each step: employee, accountant, manager, director, bot?
3. Should a bank draft be created automatically, or should Telegram require a manual confirmation before bank API calls?
4. Are there approval thresholds by amount, supplier, department, or procurement category?
5. What should happen when OCR confidence is low but core fields are present?

### Banking

6. Which Monobank source IBAN should be used for payments?
7. Will the bot support both Privat24 and Monobank simultaneously per invoice, or only one active provider per deployment?
8. Are budget payments needed? Monobank requires special `payCode` and `additionalInfo` for them.
9. Which currencies must be supported? The current Monobank corporate payment endpoint is treated as UAH-only.
10. Should payment signing remain fully outside the bot?

### CRM / Terrasoft

11. What is the target Terrasoft table/object for invoices?
12. What are the exact column names and required fields?
13. Should the bot create new suppliers/accounts in Terrasoft if they do not exist?
14. How should the bot match supplier records: EDRPOU/IPN, IBAN, name, or a combination?
15. Should CRM sync happen before bank draft creation, after successful draft creation, or in both stages?
16. Should files themselves be attached to Terrasoft, or only metadata and a local path/reference?
17. Is direct MS SQL write approved, or should we use a Terrasoft API/service layer?

### Telegram / Access

18. Who can send invoices to the bot?
19. Should roles exist in Telegram: submitter, accountant, admin, approver?
20. Should the bot expose commands for status, retry, cancel, or manual CRM re-sync?
21. Should the bot send payment receipts to the sender, accountant, group chat, or all of them?

### Data And Audit

22. How long should source files be retained?
23. Which data must be included in audit for legal/accounting traceability?
24. Should raw OCR text be stored long-term, or redacted after processing?
25. Do we need export/reporting for accountants outside CRM?

### Deployment

26. Should this server become the canonical git working copy?
27. What GitHub repository should this project live in?
28. Should deployments be manual `docker compose up --build -d`, or should GitHub Actions deploy to the server?
29. Who owns production `.env` changes and secret rotation?

## Immediate Next Steps

1. Set `MONOBANK_SOURCE_IBAN` explicitly.
2. Provide Terrasoft table/column schema or read-only DB access for schema inspection.
3. Decide whether Terrasoft direct MS SQL writes are acceptable.
4. Initialize or connect the project to a GitHub repository.
5. Convert CRM dry-run payload into a confirmed Terrasoft column map.
6. Add idempotent CRM upsert before enabling `CRM_DRY_RUN=false`.
