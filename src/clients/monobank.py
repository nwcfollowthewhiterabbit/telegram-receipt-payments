from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from src.config import Settings, get_settings
from src.services.payment_preflight import normalize_iban, normalize_text
from src.services.schemas import PaymentDraftResult


class MonobankClient:
    provider_name = "monobank"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._resolved_source_iban: str | None = None

    def source_account(self) -> str:
        return self._resolved_source_iban or self.settings.monobank_source_iban

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

    def _request(self, method: str, path: str, headers: dict[str, str] | None = None, *, allow_not_found: bool = False, **kwargs: Any) -> httpx.Response:
        response = httpx.request(
            method,
            f"{self.settings.monobank_api_base_url.rstrip('/')}{path}",
            headers={**self._headers(), **(headers or {})},
            timeout=httpx.Timeout(30.0, connect=10.0),
            **kwargs,
        )
        if allow_not_found and response.status_code == 404:
            return response
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

    def get_payment_state(self, payment_id: str) -> dict[str, Any] | None:
        return self._optional_object(f"/ext/v1/payment/{quote(payment_id, safe='')}/state")

    def get_statement_item(self, payment_id: str) -> dict[str, Any] | None:
        return self._optional_object("/ext/v1/statement", params={"id": payment_id})

    def _optional_object(self, path: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            response = self._request("GET", path, allow_not_found=True, **kwargs)
            if response.status_code == 404:
                return None
            data = response.json()
        except Exception:
            raise RuntimeError("monobank_settlement_request_failed") from None
        if not isinstance(data, dict):
            raise RuntimeError("monobank_invalid_settlement_response")
        return data

    def get_statement(self, account: str, start: int, end: int, limit: int = 500) -> list[dict[str, Any]]:
        if not account or not 0 <= start < end or end - start > 86400 or not 1 <= limit <= 500:
            raise RuntimeError("monobank_invalid_statement_request")
        try:
            data = self._request("GET", f"/ext/v1/statement/{quote(account, safe='')}/{start}/{end}", params={"limit": limit}).json()
        except Exception:
            raise RuntimeError("monobank_statement_request_failed") from None
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise RuntimeError("monobank_invalid_statement_response")
        # This endpoint documents a limit, not a cursor. A full window cannot
        # prove completeness; never invent a pagination protocol.
        if len(data) >= limit:
            raise RuntimeError("monobank_statement_window_limit")
        return data

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
        self._resolved_source_iban = payload["senderIban"]
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
            payload={**data, "create_request": dict(payload), "creation_context": {
                "version": 1, "document_number": document_number,
                "source_account": payload["senderIban"],
            }},
        )
