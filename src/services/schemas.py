from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class ReceiptValidationResult(BaseModel):
    readable: bool
    summary: str
    supplier_name: str | None = None
    supplier_tax_id: str | None = None
    supplier_iban: str | None = None
    supplier_bank_name: str | None = None
    supplier_mfo: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    procurement_category: str | None = None
    payment_purpose: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    raw_text: str | None = None


class PaymentDraftResult(BaseModel):
    created: bool
    provider_payment_id: str | None = None
    provider_pack_id: str | None = None
    status: str
    payload: dict = Field(default_factory=dict)
