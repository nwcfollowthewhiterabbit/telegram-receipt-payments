from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from src.config import get_settings
from src.services.payment_preflight import normalize_iban, normalize_text
from src.services.schemas import PaymentDraftResult


class Privat24Client:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "receipt-paybot/1.0",
            "token": self.settings.privat24_api_token,
            "Content-Type": "application/json;charset=utf-8",
        }

    @staticmethod
    def normalize_nceo(value: str | None) -> str:
        if not value:
            return "0000000000"
        digits = "".join(ch for ch in value if ch.isdigit())
        if 8 <= len(digits) <= 10:
            return digits
        if len(digits) > 10:
            return digits[:8]
        return digits or "0000000000"

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
        beneficiary_name = normalize_text(beneficiary_name) or ""
        beneficiary_iban = normalize_iban(beneficiary_iban)
        beneficiary_bank_name = normalize_text(beneficiary_bank_name)
        purpose = normalize_text(purpose) or ""
        payload = {
            "document_number": document_number,
            "payer_account": self.settings.privat24_source_account,
            "recipient_account": beneficiary_iban,
            "recipient_nceo": self.normalize_nceo(beneficiary_tax_id),
            "payment_naming": beneficiary_name,
            "payment_amount": str(amount),
            "payment_destination": purpose,
            "payment_ccy": currency,
            "document_type": "cr",
        }
        if self.settings.privat24_payment_date:
            payload["payment_date"] = self.settings.privat24_payment_date
        if self.settings.privat24_payment_accept_date:
            payload["payment_accept_date"] = self.settings.privat24_payment_accept_date
        if beneficiary_mfo:
            payload["recipient_ifi"] = beneficiary_mfo
        if beneficiary_bank_name:
            payload["recipient_ifi_text"] = beneficiary_bank_name

        if self.settings.privat24_dry_run or not self.settings.privat24_api_base_url or not self.settings.privat24_api_token:
            return PaymentDraftResult(
                created=True,
                provider_payment_id=f"dry-run-{uuid4()}",
                provider_pack_id=f"dry-pack-{uuid4()}",
                status="draft_created_dry_run",
                payload=payload,
            )

        response = httpx.post(
            f"{self.settings.privat24_api_base_url.rstrip('/')}/api/proxy/payment/create",
            json=payload,
            headers=self._headers(),
            timeout=30.0,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text.strip()
            message = f"Privat24 create failed with status {exc.response.status_code}"
            if response_text:
                message = f"{message}: {response_text}"
            raise RuntimeError(message) from exc
        data = response.json()
        return PaymentDraftResult(
            created=True,
            provider_payment_id=data.get("payment_ref") or data.get("payment_data", {}).get("payment_ref"),
            provider_pack_id=data.get("payment_pack_ref") or data.get("payment_data", {}).get("payment_pack_ref"),
            status="draft_created_pending_signature",
            payload=data,
        )

    def get_payment(self, payment_ref: str) -> dict:
        response = httpx.get(
            f"{self.settings.privat24_api_base_url.rstrip('/')}/api/proxy/payment/get",
            params={"ref": payment_ref},
            headers=self._headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def get_transactions(
        self,
        account: str,
        start_date: dt.date,
        end_date: dt.date | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "acc": account,
            "startDate": start_date.strftime("%d-%m-%Y"),
            "limit": min(limit, 100),
        }
        if end_date:
            params["endDate"] = end_date.strftime("%d-%m-%Y")

        items: list[dict[str, Any]] = []
        follow_id: str | None = None
        while True:
            current_params = dict(params)
            if follow_id:
                current_params["followId"] = follow_id
            response = httpx.get(
                f"{self.settings.privat24_api_base_url.rstrip('/')}/api/statements/transactions",
                params=current_params,
                headers=self._headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            chunk = data.get("transactions") or data.get("list") or data.get("data") or []
            if isinstance(chunk, list):
                items.extend(chunk)
            follow_id = data.get("followId") or data.get("follow_id")
            if not follow_id or not chunk:
                break
        return items

    def print_receipt(self, account: str, reference: str, refn: str, per_page: int = 4) -> tuple[str, bytes]:
        response = httpx.post(
            f"{self.settings.privat24_api_base_url.rstrip('/')}/api/paysheets/print_receipt",
            json={
                "transactions": [
                    {
                        "account": account,
                        "reference": reference,
                        "refn": refn,
                    }
                ],
                "perPage": per_page,
            },
            headers={**self._headers(), "Accept": "application/octet-stream"},
            timeout=60.0,
        )
        response.raise_for_status()
        filename = f"receipt_{account}_{reference}.pdf"
        content_disposition = response.headers.get("content-disposition", "")
        if "filename=" in content_disposition:
            filename = content_disposition.split("filename=", 1)[1].strip().strip('"')
        return filename, response.content
