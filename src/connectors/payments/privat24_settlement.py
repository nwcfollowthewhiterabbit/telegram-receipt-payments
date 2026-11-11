"""Privat24 settlement by exact packed reference and posted debit statement."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.clients.privat24 import Privat24Client
from src.config import Settings
from src.connectors.payments.settlement import (
    BankAttachment, ConfirmedPayment, SettlementError, SettlementEvidence,
    bank_text, check_paid_at, created_at, currency, iban, identifier, money,
    name_key, tax_id, validate_draft,
)


class Privat24SettlementConnector:
    def __init__(self, settings: Settings, client: Privat24Client | None = None, *, now: Callable[[], dt.datetime] | None = None) -> None:
        self.settings = settings
        self.client = client or Privat24Client(settings)
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def confirm_payment(self, draft: Any, document_number: str) -> ConfirmedPayment | None:
        if self.settings.payment_dry_run_for("privat24") or not self.settings.privat24_api_token:
            return None
        payment_id = getattr(draft, "provider_payment_id", None)
        if not payment_id or str(payment_id).startswith("dry-run-"):
            return None
        validate_draft(draft, "privat24", document_number)
        packed_ref = identifier(getattr(draft, "provider_pack_id", None))
        payment = self.client.get_payment(payment_id)
        self._validate_payment(payment, draft, document_number)

        now = self.now()
        first_date = created_at(draft).astimezone(ZoneInfo("Europe/Kyiv")).date() - dt.timedelta(days=1)
        today = now.astimezone(ZoneInfo("Europe/Kyiv")).date()
        # Bounded rolling search; exact references remain mandatory. Older
        # settlements outside the window require explicit reconciliation.
        first_date = max(first_date, today - dt.timedelta(days=31))
        transactions = self.client.get_transactions(
            account=iban(draft.source_account), start_date=first_date, end_date=today,
            limit=100, max_pages=100,
        )
        candidates = [item for item in transactions if item.get("DLR") == packed_ref]
        if not candidates:
            return None
        # Never choose the first of several matching payments, including a
        # duplicated statement row; the provider response must be unambiguous.
        if len(candidates) != 1:
            raise SettlementError("privat24_ambiguous_settlement")
        transaction = candidates[0]
        if transaction.get("PR_PR") != "r" or transaction.get("FL_REAL") != "r":
            return None
        self._validate_transaction(transaction, draft, document_number)
        reference = identifier(transaction.get("REF"))
        refn = identifier(transaction.get("REFN"))
        if not refn.isascii() or not refn.isdigit():
            raise SettlementError("privat24_invalid_statement_reference")
        paid_at = check_paid_at(self._paid_at(transaction), draft, now)
        _, content = self.client.print_receipt(iban(draft.source_account), reference, refn, per_page=1)
        Privat24Client.validate_receipt_pdf(content)
        return ConfirmedPayment(
            evidence=SettlementEvidence(
                provider="privat24", bank_payment_id=payment_id,
                transaction_id=f"{reference}:{refn}", source_account=iban(draft.source_account),
                beneficiary_iban=iban(draft.beneficiary_iban), beneficiary_tax_id=tax_id(draft.beneficiary_tax_id),
                amount=money(draft.amount), currency=currency(draft.currency), paid_at=paid_at,
                purpose=bank_text(transaction["OSND"]), document_number=document_number,
            ),
            receipt=BankAttachment("privat24-payment-receipt.pdf", content, "application/pdf"),
            bank_payload={"evidence_version": 1, "payment": payment, "statement": transaction},
        )

    @staticmethod
    def _validate_payment(payment: dict[str, Any], draft: Any, document_number: str) -> None:
        if not isinstance(payment, dict) or payment.get("payment_ref") != draft.provider_payment_id:
            raise SettlementError("privat24_payment_reference_mismatch")
        if "id" in payment and payment["id"] != draft.provider_payment_id:
            raise SettlementError("privat24_payment_reference_mismatch")
        if "payment_pack_ref" in payment and payment["payment_pack_ref"] != draft.provider_pack_id:
            raise SettlementError("privat24_packed_reference_mismatch")
        if not (
            payment.get("document_number") == document_number
            and iban(payment.get("payer_account")) == iban(draft.source_account)
            and iban(payment.get("recipient_account")) == iban(draft.beneficiary_iban)
            and tax_id(payment.get("recipient_nceo")) == tax_id(draft.beneficiary_tax_id)
            and name_key(payment.get("payment_naming")) == name_key(draft.beneficiary_name)
            and money(payment.get("payment_amount")) == money(draft.amount)
            and currency(payment.get("payment_ccy")) == currency(draft.currency)
            and bank_text(payment.get("payment_destination")) == bank_text(draft.purpose)
        ):
            raise SettlementError("privat24_payment_details_changed")

    @staticmethod
    def _validate_transaction(transaction: dict[str, Any], draft: Any, document_number: str) -> None:
        if not (
            transaction.get("TRANTYPE") == "D"
            and transaction.get("NUM_DOC") == document_number
            and iban(transaction.get("AUT_MY_ACC")) == iban(draft.source_account)
            and iban(transaction.get("AUT_CNTR_ACC")) == iban(draft.beneficiary_iban)
            and tax_id(transaction.get("AUT_CNTR_CRF")) == tax_id(draft.beneficiary_tax_id)
            and name_key(transaction.get("AUT_CNTR_NAM")) == name_key(draft.beneficiary_name)
            and money(transaction.get("SUM")) == money(draft.amount)
            and currency(transaction.get("CCY")) == currency(draft.currency)
            and bank_text(transaction.get("OSND")) == bank_text(draft.purpose)
        ):
            raise SettlementError("privat24_statement_details_mismatch")

    @staticmethod
    def _paid_at(transaction: dict[str, Any]) -> dt.datetime:
        date = transaction.get("DAT_OD")
        time = transaction.get("TIM_P")
        timestamp = transaction.get("DATE_TIME_DAT_OD_TIM_P")
        parsed = None
        try:
            if timestamp:
                parsed = dt.datetime.strptime(timestamp, "%d.%m.%Y %H:%M:%S")
            elif isinstance(date, str) and isinstance(time, str):
                for format in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
                    try:
                        parsed = dt.datetime.strptime(f"{date} {time}", format)
                        break
                    except ValueError:
                        pass
            if parsed is None:
                raise ValueError("missing timestamp")
            if date and parsed.strftime("%d.%m.%Y") != date:
                raise ValueError("conflicting date")
            if time and time not in {parsed.strftime("%H:%M"), parsed.strftime("%H:%M:%S")}:
                raise ValueError("conflicting time")
        except (TypeError, ValueError):
            raise SettlementError("privat24_invalid_settlement_time") from None
        return parsed.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
