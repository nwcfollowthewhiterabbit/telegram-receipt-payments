from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, text

from src.config import get_settings
from src.db.models import PaymentDraft, Receipt, ReceiptStatus
from src.services.schemas import CrmSyncResult


TERRASOFT_TEST_DATABASE = "Terrasoft_test"
TERRASOFT_CASHFLOW_TABLE = "dbo.tbl_Cashflow"

CASHFLOW_TYPE_EXPENSE_ID = "484C8429-DABF-482A-BC7B-4C75D1436A1B"
CASHFLOW_STATUS_PLANNED_ID = "D7141996-2996-4BB2-BCCD-E422A54AA02E"
CASHFLOW_STATUS_TO_PAY_ID = "A0EE28EC-0074-414A-ABE9-0526F923A84A"
CURRENCY_UAH_ID = "D18AAED6-14F9-435C-9606-0E90CAE816F7"
PAYER_RENTALL_ACCOUNT_ID = "E308B781-3C5B-4ECB-89EF-5C1ED4DA488E"
MONOBANK_UAH_CASH_ACCOUNT_ID = "FF8B6D7F-50F8-4F38-8BC8-6AAD2E345263"


class TerrasoftMssqlConnector:
    provider_name = "terrasoft_mssql"

    def __init__(self) -> None:
        self.settings = get_settings()

    def sync_receipt(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> CrmSyncResult:
        payload = self._build_payload(receipt, payment_draft)
        if self.settings.crm_dry_run:
            return CrmSyncResult(
                synced=True,
                provider_name=self.provider_name,
                external_id=payload["id"],
                status="crm_dry_run",
                payload=payload,
            )

        self._validate_live_target()
        engine = create_engine(self.settings.terrasoft_mssql_url, future=True, pool_pre_ping=True)
        with engine.begin() as connection:
            recipient = self._find_account_by_supplier_code(connection, receipt.extracted_supplier_tax_id)
            payload["recipient_id"] = recipient["id"] if recipient else None
            payload["recipient_match"] = recipient
            payload["cf_number"] = self._next_cf_number(connection)
            connection.execute(*self._build_cashflow_insert(payload))

        return CrmSyncResult(
            synced=True,
            provider_name=self.provider_name,
            external_id=payload["id"],
            status="crm_synced",
            payload={
                "database": self.settings.terrasoft_database,
                "table": TERRASOFT_CASHFLOW_TABLE,
                "id": payload["id"],
                "external_key": payload["external_key"],
                "recipient_match": payload["recipient_match"],
            },
        )

    def _validate_live_target(self) -> None:
        if not self.settings.terrasoft_mssql_url:
            raise RuntimeError("TERRASOFT_MSSQL_URL is required for Terrasoft CRM sync")
        if self.settings.terrasoft_database != TERRASOFT_TEST_DATABASE:
            raise RuntimeError("Terrasoft live sync is currently allowed only for Terrasoft_test")
        table = self.settings.terrasoft_invoice_table or TERRASOFT_CASHFLOW_TABLE
        if table != TERRASOFT_CASHFLOW_TABLE:
            raise RuntimeError("Terrasoft cashflow sync requires TERRASOFT_INVOICE_TABLE=dbo.tbl_Cashflow")

    def _build_payload(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> dict:
        now = dt.datetime.utcnow()
        cashflow_id = str(uuid4())
        external_key = f"receipt-paybot:cashflow:{cashflow_id}"
        amount = Decimal(str(payment_draft.amount if payment_draft else receipt.extracted_amount or 0)).quantize(
            Decimal("0.01")
        )
        estimated_date = self._estimated_date(receipt)
        return {
            "id": cashflow_id,
            "external_key": external_key,
            "created_on": now,
            "modified_on": now,
            "cash_account_id": MONOBANK_UAH_CASH_ACCOUNT_ID,
            "type_id": CASHFLOW_TYPE_EXPENSE_ID,
            "payer_id": PAYER_RENTALL_ACCOUNT_ID,
            "recipient_id": None,
            "status_id": self._status_id(receipt, payment_draft),
            "period_id": self._period_id_for_date(estimated_date),
            "currency_id": CURRENCY_UAH_ID,
            "estimated_date": estimated_date,
            "actual_date": None,
            "subject": self._subject(receipt, payment_draft),
            "currency_rate": Decimal("1.0000"),
            "amount": amount,
            "basic_amount": amount,
            "owner_id": "D8D923D0-1A2E-44E1-9072-3D82451A7A7E",
            "use_as_cashflow": 1,
            "use_as_pandl": 1,
            "autocalc_amount": 0,
            "cf_number": None,
            "comments_payer": self._comments_payer(receipt, payment_draft, external_key),
            "recipient_match": None,
        }

    @staticmethod
    def _status_id(receipt: Receipt, payment_draft: PaymentDraft | None) -> str:
        if payment_draft and receipt.status in {ReceiptStatus.bank_created, ReceiptStatus.dry_run_created}:
            return CASHFLOW_STATUS_TO_PAY_ID
        return CASHFLOW_STATUS_PLANNED_ID

    @staticmethod
    def _estimated_date(receipt: Receipt) -> dt.datetime:
        raw = receipt.extracted_invoice_date
        if raw:
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return dt.datetime.combine(dt.datetime.strptime(raw, fmt).date(), dt.time.min)
                except ValueError:
                    pass
        return dt.datetime.combine(dt.date.today(), dt.time.min)

    @staticmethod
    def _period_id_for_date(value: dt.datetime) -> str | None:
        # Existing periods are monthly records named MM.YYYY. The lookup is resolved by SQL during insert.
        return value.strftime("%m.%Y")

    @staticmethod
    def _subject(receipt: Receipt, payment_draft: PaymentDraft | None) -> str:
        purpose = payment_draft.purpose if payment_draft else (receipt.validation_payload or {}).get("payment_purpose_final")
        if purpose:
            return str(purpose)[:500]
        invoice = receipt.extracted_invoice_number or receipt.id
        supplier = receipt.extracted_supplier_name or "поставщик"
        return f"Оплата счета {invoice} - {supplier}"[:500]

    @staticmethod
    def _comments_payer(receipt: Receipt, payment_draft: PaymentDraft | None, external_key: str) -> str:
        lines = [
            "ReceiptPayBot",
            f"Receipt ID: {receipt.id}",
            "Mode: create new Terrasoft operation for each processing attempt",
            f"External key: {external_key}",
            f"Payment provider: {payment_draft.provider_name if payment_draft else ''}",
            f"Provider payment ID: {payment_draft.provider_payment_id if payment_draft else ''}",
            f"Invoice: {receipt.extracted_invoice_number or ''} {receipt.extracted_invoice_date or ''}",
            f"Supplier: {receipt.extracted_supplier_name or ''}",
            f"Supplier code: {receipt.extracted_supplier_tax_id or ''}",
            f"Supplier IBAN: {receipt.extracted_supplier_iban or ''}",
            f"Supplier bank: {receipt.extracted_supplier_bank_name or ''}",
            f"Purpose: {payment_draft.purpose if payment_draft else (receipt.validation_payload or {}).get('payment_purpose_final', '')}",
            f"Source file: {receipt.original_filename}",
        ]
        return "\n".join(lines)[:5000]

    @staticmethod
    def _normalize_supplier_code(value: str | None) -> str | None:
        digits = re.sub(r"\D+", "", value or "")
        return digits or None

    def _find_account_by_supplier_code(self, connection, supplier_code: str | None) -> dict | None:
        code = self._normalize_supplier_code(supplier_code)
        if not code:
            return None
        row = (
            connection.execute(
                text(
                    """
                    SELECT TOP 1 CONVERT(nvarchar(36), ID) AS id, Name AS name, TaxRegistrationCode AS tax_code, Code AS code
                    FROM dbo.tbl_Account
                    WHERE REPLACE(REPLACE(REPLACE(ISNULL(TaxRegistrationCode, ''), ' ', ''), '-', ''), '.', '') = :code
                       OR REPLACE(REPLACE(REPLACE(ISNULL(Code, ''), ' ', ''), '-', ''), '.', '') = :code
                    ORDER BY ModifiedOn DESC
                    """
                ),
                {"code": code},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def _next_cf_number(self, connection) -> str:
        row = connection.execute(
            text(
                """
                SELECT ISNULL(MAX(CAST(SUBSTRING(CFNumber, 3, 32) AS int)), 0) + 1 AS next_number
                FROM dbo.tbl_Cashflow WITH (UPDLOCK, HOLDLOCK)
                WHERE CFNumber LIKE 'CF%' AND ISNUMERIC(SUBSTRING(CFNumber, 3, 32)) = 1
                """
            )
        ).scalar_one()
        return f"CF{int(row)}"

    def _build_cashflow_insert(self, payload: dict) -> tuple:
        query = text(
            """
            DECLARE @PeriodID uniqueidentifier;
            SELECT TOP 1 @PeriodID = ID FROM dbo.tbl_Period WHERE Name = :period_name ORDER BY StartDate DESC;

            INSERT INTO dbo.tbl_Cashflow (
                ID, CreatedOn, ModifiedOn, CashAccountID, TypeID, RecipientID, PayerID, StatusID, PeriodID,
                CurrencyID, EstimatedDate, ActualDate, Subject, CurrencyRate, Amount, BasicAmount, OwnerID,
                UseAsCashflow, UseAsPandL, AutocalcAmount, CFNumber, CommentsPayer, CodPrivat
            )
            VALUES (
                CONVERT(uniqueidentifier, :id), :created_on, :modified_on, CONVERT(uniqueidentifier, :cash_account_id),
                CONVERT(uniqueidentifier, :type_id),
                CASE WHEN :recipient_id IS NULL THEN NULL ELSE CONVERT(uniqueidentifier, :recipient_id) END,
                CONVERT(uniqueidentifier, :payer_id), CONVERT(uniqueidentifier, :status_id), @PeriodID,
                CONVERT(uniqueidentifier, :currency_id), :estimated_date, :actual_date, :subject, :currency_rate,
                :amount, :basic_amount, CONVERT(uniqueidentifier, :owner_id), :use_as_cashflow, :use_as_pandl,
                :autocalc_amount, :cf_number, :comments_payer, :external_key
            );
            """
        )
        return query, {**payload, "period_name": payload["period_id"]}
