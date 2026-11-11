"""Monobank corporate settlement; IN_STATEMENT alone is never payment proof."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable

from src.clients.monobank import MonobankClient
from src.config import Settings
from src.connectors.payments.settlement import (
    ConfirmedPayment, SettlementError, SettlementEvidence, bank_text, check_paid_at,
    currency, iban, money, name_key, tax_id, validate_draft,
)


class MonobankSettlementConnector:
    def __init__(self, settings: Settings, client: MonobankClient | None = None, *, now: Callable[[], dt.datetime] | None = None) -> None:
        self.settings = settings
        self.client = client or MonobankClient(settings)
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def confirm_payment(self, draft: Any, document_number: str) -> ConfirmedPayment | None:
        if self.settings.payment_dry_run_for("monobank") or not self.settings.monobank_api_token:
            return None
        payment_id = getattr(draft, "provider_payment_id", None)
        if not payment_id or str(payment_id).startswith("dry-run-"):
            return None
        validate_draft(draft, "monobank", document_number)
        state = self.client.get_payment_state(payment_id)
        if state is None:
            return None
        if not isinstance(state, dict) or state.get("id") != payment_id:
            raise SettlementError("monobank_payment_reference_mismatch")
        if state.get("state") != "IN_STATEMENT":
            return None
        transaction = self.client.get_statement_item(payment_id)
        if transaction is None:
            return None
        if not isinstance(transaction, dict) or transaction.get("id") != payment_id:
            raise SettlementError("monobank_statement_reference_mismatch")
        if transaction.get("status") != "DONE" or transaction.get("reverse") is not False:
            return None
        paid_at = self._validate_transaction(transaction, draft, document_number)
        timestamp = self._timestamp(transaction.get("time"))
        # The point lookup has no sender IBAN. Confirm the identical payment
        # on the expected account, without a heuristic amount/name search.
        start, end = timestamp - 1, timestamp + 1
        account_items = self.client.get_statement(iban(draft.source_account), start, end, limit=500)
        if len(account_items) >= 500:
            raise SettlementError("monobank_statement_window_limit")
        related = [item for item in account_items if item.get("id") == payment_id or item.get("externalReference") == document_number]
        if not related:
            return None
        if len(related) != 1:
            raise SettlementError("monobank_ambiguous_settlement")
        scoped = related[0]
        scoped_paid_at = self._validate_transaction(scoped, draft, document_number)
        if self._timestamp(scoped.get("time")) != timestamp or scoped_paid_at != paid_at:
            raise SettlementError("monobank_inconsistent_statement_evidence")
        return ConfirmedPayment(
            evidence=SettlementEvidence(
                provider="monobank", bank_payment_id=payment_id, transaction_id=payment_id,
                source_account=iban(draft.source_account), beneficiary_iban=iban(draft.beneficiary_iban),
                beneficiary_tax_id=tax_id(draft.beneficiary_tax_id), amount=money(draft.amount),
                currency=currency(draft.currency), paid_at=paid_at,
                purpose=bank_text(transaction["description"]), document_number=document_number,
            ),
            receipt=None,
            bank_payload={
                "evidence_version": 1, "payment_state": state, "statement": transaction,
                "account_statement": {"account": iban(draft.source_account), "from": start, "to": end, "transaction": scoped},
            },
        )

    def _validate_transaction(self, transaction: dict[str, Any], draft: Any, document_number: str) -> dt.datetime:
        minor_amount = transaction.get("amount")
        if isinstance(minor_amount, bool) or not isinstance(minor_amount, int):
            raise SettlementError("monobank_invalid_minor_amount")
        if not (
            transaction.get("id") == draft.provider_payment_id
            and transaction.get("externalReference") == document_number
            and transaction.get("status") == "DONE"
            and transaction.get("reverse") is False
            and minor_amount < 0
            and Decimal(-minor_amount) / Decimal(100) == money(draft.amount)
            and currency(transaction.get("currencyCode")) == currency(draft.currency)
            and iban(transaction.get("counterIban")) == iban(draft.beneficiary_iban)
            and tax_id(transaction.get("counterEdrpou")) == tax_id(draft.beneficiary_tax_id)
            and name_key(transaction.get("counterName")) == name_key(draft.beneficiary_name)
            and bank_text(transaction.get("description")) == bank_text(draft.purpose)
        ):
            raise SettlementError("monobank_statement_details_mismatch")
        try:
            paid_at = dt.datetime.fromtimestamp(self._timestamp(transaction.get("completedTime")), tz=dt.timezone.utc)
            transaction_time = dt.datetime.fromtimestamp(self._timestamp(transaction.get("time")), tz=dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise SettlementError("monobank_invalid_settlement_time") from None
        check_paid_at(transaction_time, draft, self.now())
        if transaction_time > paid_at:
            raise SettlementError("monobank_inconsistent_statement_time")
        return check_paid_at(paid_at, draft, self.now())

    @staticmethod
    def _timestamp(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SettlementError("monobank_invalid_statement_time")
        return value
