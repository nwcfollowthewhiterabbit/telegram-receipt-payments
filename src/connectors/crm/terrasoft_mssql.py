from __future__ import annotations

import datetime as dt
from uuid import uuid4

from sqlalchemy import create_engine, text

from src.config import get_settings
from src.db.models import PaymentDraft, Receipt
from src.services.schemas import CrmSyncResult


DEFAULT_COLUMN_MAP = {
    "id": "Id",
    "created_on": "CreatedOn",
    "modified_on": "ModifiedOn",
    "receipt_id": "UsrReceiptId",
    "telegram_user_id": "UsrTelegramUserId",
    "supplier_name": "UsrSupplierName",
    "supplier_tax_id": "UsrSupplierTaxId",
    "supplier_iban": "UsrSupplierIban",
    "invoice_number": "UsrInvoiceNumber",
    "invoice_date": "UsrInvoiceDate",
    "amount": "UsrAmount",
    "currency": "UsrCurrency",
    "payment_purpose": "UsrPaymentPurpose",
    "payment_provider": "UsrPaymentProvider",
    "payment_draft_id": "UsrPaymentDraftId",
    "payment_status": "UsrPaymentStatus",
    "provider_payment_id": "UsrProviderPaymentId",
}


class TerrasoftMssqlConnector:
    provider_name = "terrasoft_mssql"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.column_map = {**DEFAULT_COLUMN_MAP, **self.settings.terrasoft_column_map}

    def sync_receipt(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> CrmSyncResult:
        payload = self._build_payload(receipt, payment_draft)
        if self.settings.crm_dry_run:
            return CrmSyncResult(
                synced=True,
                provider_name=self.provider_name,
                external_id=str(payload["id"]),
                status="crm_dry_run",
                payload=self._mapped_payload(payload),
            )

        if not self.settings.terrasoft_mssql_url:
            raise RuntimeError("TERRASOFT_MSSQL_URL is required for Terrasoft CRM sync")
        if not self.settings.terrasoft_invoice_table:
            raise RuntimeError("TERRASOFT_INVOICE_TABLE is required for Terrasoft CRM sync")

        mapped_payload = self._mapped_payload(payload)
        columns = list(mapped_payload.keys())
        column_sql = ", ".join(f"[{column}]" for column in columns)
        value_sql = ", ".join(f":{column}" for column in columns)
        query = text(f"INSERT INTO {self.settings.terrasoft_invoice_table} ({column_sql}) VALUES ({value_sql})")

        engine = create_engine(self.settings.terrasoft_mssql_url, future=True, pool_pre_ping=True)
        with engine.begin() as connection:
            connection.execute(query, mapped_payload)

        return CrmSyncResult(
            synced=True,
            provider_name=self.provider_name,
            external_id=str(payload["id"]),
            status="crm_synced",
            payload={"table": self.settings.terrasoft_invoice_table, "id": str(payload["id"])},
        )

    def _build_payload(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> dict:
        now = dt.datetime.utcnow()
        provider_payload = payment_draft.provider_payload if payment_draft else {}
        purpose = payment_draft.purpose if payment_draft else (receipt.validation_payload or {}).get("payment_purpose_final")
        return {
            "id": uuid4(),
            "created_on": now,
            "modified_on": now,
            "receipt_id": receipt.id,
            "telegram_user_id": receipt.telegram_user_id,
            "supplier_name": receipt.extracted_supplier_name,
            "supplier_tax_id": receipt.extracted_supplier_tax_id,
            "supplier_iban": receipt.extracted_supplier_iban,
            "invoice_number": receipt.extracted_invoice_number,
            "invoice_date": receipt.extracted_invoice_date,
            "amount": receipt.extracted_amount,
            "currency": receipt.extracted_currency,
            "payment_purpose": purpose,
            "payment_provider": payment_draft.provider_name if payment_draft else None,
            "payment_draft_id": payment_draft.id if payment_draft else None,
            "payment_status": payment_draft.status if payment_draft else receipt.status.value,
            "provider_payment_id": payment_draft.provider_payment_id if payment_draft else None,
            "provider_payload": provider_payload,
        }

    def _mapped_payload(self, payload: dict) -> dict:
        return {
            column_name: payload[field_name]
            for field_name, column_name in self.column_map.items()
            if field_name in payload and column_name
        }
