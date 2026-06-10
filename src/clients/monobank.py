from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

import httpx

from src.config import get_settings
from src.services.payment_preflight import normalize_iban, normalize_text
from src.services.schemas import PaymentDraftResult


class MonobankClient:
    provider_name = "monobank"

    def __init__(self) -> None:
        self.settings = get_settings()

    def source_account(self) -> str:
        return self.settings.monobank_source_iban

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Token": self.settings.monobank_api_token,
        }

    @staticmethod
    def normalize_edrpou(value: str | None) -> int | None:
        if not value:
            return None
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) > 10:
            digits = digits[:8]
        if len(digits) in {8, 10}:
            return int(digits)
        return None

    @staticmethod
    def _currency_code(value: str | None) -> str:
        normalized = (value or "").strip().upper()
        if normalized in {"", "UAH", "ГРН", "980"}:
            return "980"
        raise RuntimeError(f"Monobank supports only UAH payments, got {value}")

    @staticmethod
    def _minor_units(amount: Decimal) -> int:
        return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _request(self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        response = httpx.request(
            method,
            f"{self.settings.monobank_api_base_url.rstrip('/')}{path}",
            headers={**self._headers(), **(headers or {})},
            timeout=30.0,
            **kwargs,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text.strip()
            message = f"Monobank request failed with status {exc.response.status_code}"
            if response_text:
                message = f"{message}: {response_text}"
            raise RuntimeError(message) from exc
        return response

    def get_accounts(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/ext/v1/accounts")
        data = response.json()
        return data if isinstance(data, list) else []

    def _resolve_sender_iban(self) -> str:
        configured = normalize_iban(self.settings.monobank_source_iban)
        if configured:
            return configured

        accounts = self.get_accounts()
        uah_accounts = [item for item in accounts if str(item.get("currency")) == "980" and item.get("iban")]
        if not uah_accounts:
            raise RuntimeError("MONOBANK_SOURCE_IBAN is not configured and no UAH account was returned by Monobank")
        if len(uah_accounts) > 1:
            raise RuntimeError("MONOBANK_SOURCE_IBAN is required because Monobank returned multiple UAH accounts")
        return normalize_iban(str(uah_accounts[0]["iban"])) or str(uah_accounts[0]["iban"])

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
        del beneficiary_bank_name, beneficiary_mfo

        sender_iban = normalize_iban(self.settings.monobank_source_iban) or "<auto-from-monobank-accounts>"
        beneficiary_name = normalize_text(beneficiary_name) or ""
        beneficiary_iban = normalize_iban(beneficiary_iban)
        purpose = normalize_text(purpose) or ""
        receiver_edrpou = self.normalize_edrpou(beneficiary_tax_id)
        currency_code = self._currency_code(currency)

        if not beneficiary_iban:
            raise RuntimeError("Missing beneficiary IBAN")
        if not receiver_edrpou:
            raise RuntimeError("Monobank requires beneficiary EDRPOU/IPN")

        payload = {
            "senderIban": sender_iban,
            "receiver": {
                "iban": beneficiary_iban,
                "edrpou": receiver_edrpou,
                "name": beneficiary_name,
            },
            "destination": purpose,
            "amount": self._minor_units(amount),
            "currency": currency_code,
            "externalReference": document_number,
        }

        if self.settings.payment_dry_run_for("monobank") or not self.settings.monobank_api_token:
            return PaymentDraftResult(
                created=True,
                provider_payment_id=f"dry-run-{uuid4()}",
                provider_pack_id=None,
                status="draft_created_dry_run",
                payload=payload,
            )

        payload["senderIban"] = self._resolve_sender_iban()
        data = self._request(
            "POST",
            "/ext/v1/payment/prepare",
            headers={"idempotency-key": f"receipt-paybot-{document_number}"},
            json=payload,
        ).json()
        return PaymentDraftResult(
            created=True,
            provider_payment_id=data.get("id"),
            provider_pack_id=None,
            status="draft_created_pending_signature",
            payload=data,
        )
