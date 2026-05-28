from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from src.services.schemas import PaymentDraftResult


class PaymentConnector(Protocol):
    provider_name: str

    def source_account(self) -> str:
        raise NotImplementedError

    def create_payment_draft(
        self,
        document_number: str,
        beneficiary_name: str,
        beneficiary_tax_id: str | None,
        beneficiary_iban: str | None,
        beneficiary_bank_name: str | None,
        beneficiary_mfo: str | None,
        amount: Decimal,
        currency: str,
        purpose: str,
    ) -> PaymentDraftResult:
        raise NotImplementedError
