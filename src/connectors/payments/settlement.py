"""Verified bank settlement contracts, independent of CRM and Telegram delivery."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from src.config import Settings
from src.services.payment_preflight import is_valid_iban


class SettlementError(RuntimeError):
    """Fail-closed evidence error; messages contain codes, never bank data."""


@dataclass(frozen=True)
class BankAttachment:
    filename: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class SettlementEvidence:
    provider: str
    bank_payment_id: str
    transaction_id: str
    source_account: str
    beneficiary_iban: str
    beneficiary_tax_id: str
    amount: Decimal
    currency: str
    paid_at: dt.datetime
    purpose: str
    document_number: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount <= 0:
            raise SettlementError("settlement_invalid_positive_amount")
        if not isinstance(self.paid_at, dt.datetime) or self.paid_at.utcoffset() is None:
            raise SettlementError("settlement_payment_time_requires_timezone")


@dataclass(frozen=True)
class ConfirmedPayment:
    evidence: SettlementEvidence
    receipt: BankAttachment | None
    bank_payload: dict[str, Any]


class SettlementConnector(Protocol):
    def confirm_payment(self, draft: Any, document_number: str) -> ConfirmedPayment | None:
        """Return confirmed, correlated debit evidence or no confirmation.

        Missing/contradictory evidence raises SettlementError. A caller must
        separately claim the transaction uniquely before applying CRM effects.
        """
        ...


def build_settlement_connector(settings: Settings, provider: str) -> SettlementConnector:
    if provider == "privat24":
        from src.connectors.payments.privat24_settlement import Privat24SettlementConnector

        return Privat24SettlementConnector(settings)
    if provider == "monobank":
        from src.connectors.payments.monobank_settlement import MonobankSettlementConnector

        return MonobankSettlementConnector(settings)
    raise SettlementError("settlement_unsupported_provider")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise SettlementError("settlement_invalid_identifier")
    if any(ord(char) < 32 for char in value):
        raise SettlementError("settlement_invalid_identifier")
    return value


def iban(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9\s]+", value):
        raise SettlementError("settlement_invalid_iban")
    normalized = re.sub(r"\s+", "", value).upper()
    if not is_valid_iban(normalized):
        raise SettlementError("settlement_invalid_iban")
    return normalized


def tax_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SettlementError("settlement_invalid_tax_id")
    normalized = re.sub(r"\s+", "", str(value))
    if not re.fullmatch(r"[0-9]{8}|[0-9]{10}", normalized):
        raise SettlementError("settlement_invalid_tax_id")
    return normalized


def money(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SettlementError("settlement_invalid_amount")
    try:
        amount = Decimal(str(value).replace(" ", "").replace(",", "."))
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            raise SettlementError("settlement_invalid_amount")
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise SettlementError("settlement_invalid_amount") from None


def currency(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SettlementError("settlement_invalid_currency")
    result = str(value).upper().strip()
    result = {"980": "UAH", "840": "USD", "978": "EUR", "826": "GBP"}.get(result, result)
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise SettlementError("settlement_invalid_currency")
    return result


def bank_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SettlementError("settlement_missing_text")
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "’": "'"}))
    return " ".join(value.split())


def name_key(value: Any) -> str:
    return bank_text(value).casefold()


def created_at(draft: Any) -> dt.datetime:
    value = getattr(draft, "created_at", None)
    if not isinstance(value, dt.datetime):
        raise SettlementError("settlement_missing_creation_time")
    # The existing ORM stores UTC as a naive datetime.
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def check_paid_at(value: dt.datetime, draft: Any, now: dt.datetime) -> dt.datetime:
    if value.utcoffset() is None or now.utcoffset() is None:
        raise SettlementError("settlement_payment_time_requires_timezone")
    if value < created_at(draft) - dt.timedelta(minutes=5) or value > now + dt.timedelta(minutes=5):
        raise SettlementError("settlement_payment_time_outside_expected_range")
    return value


def validate_draft(draft: Any, provider: str, document_number: str) -> None:
    if getattr(draft, "provider_name", None) != provider:
        raise SettlementError("settlement_provider_mismatch")
    identifier(getattr(draft, "provider_payment_id", None))
    identifier(document_number)
    iban(draft.source_account)
    iban(draft.beneficiary_iban)
    tax_id(draft.beneficiary_tax_id)
    if money(draft.amount) <= 0:
        raise SettlementError("settlement_invalid_positive_amount")
    currency(draft.currency)
    bank_text(draft.purpose)
    name_key(draft.beneficiary_name)
    created_at(draft)
    payload = getattr(draft, "provider_payload", None) or {}
    context = payload.get("creation_context")
    if context is not None:
        if not isinstance(context, dict) or context.get("version") != 1:
            raise SettlementError("settlement_invalid_creation_context")
        if context.get("document_number") != document_number or iban(context.get("source_account")) != iban(draft.source_account):
            raise SettlementError("settlement_creation_context_mismatch")
