from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from pypdf import PdfReader

from src.config import Settings, get_settings
from src.services.payment_preflight import normalize_iban, normalize_text
from src.services.schemas import PaymentDraftResult


class Privat24Client:
    provider_name = "privat24"
    MAX_RECEIPT_BYTES = 20 * 1024 * 1024
    MAX_STATEMENT_PAGES = 100

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def source_account(self) -> str:
        return self.settings.privat24_source_account

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "receipt-paybot/1.0",
            "token": self.settings.privat24_api_token,
            "Content-Type": "application/json;charset=utf-8",
        }

    @staticmethod
    def normalize_nceo(value: str | None) -> str:
        if not value:
            raise RuntimeError("Privat24 requires recipient EDRPOU/IPN")
        digits = "".join(ch for ch in value if ch.isdigit())
        if 8 <= len(digits) <= 10:
            return digits
        raise RuntimeError(f"Invalid recipient EDRPOU/IPN length: {len(digits)}")

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

        if self.settings.payment_dry_run_for("privat24") or not self.settings.privat24_api_base_url or not self.settings.privat24_api_token:
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
            payload={**data, "create_request": dict(payload), "creation_context": {
                "version": 1, "document_number": document_number,
                "source_account": normalize_iban(payload["payer_account"]),
            }},
        )

    def get_payment(self, payment_ref: str) -> dict:
        response = httpx.get(
            f"{self.settings.privat24_api_base_url.rstrip('/')}/api/proxy/payment/get",
            params={"ref": payment_ref},
            headers=self._headers(),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("privat24_invalid_payment_response")
        return data

    def get_transactions(
        self,
        account: str,
        start_date: dt.date,
        end_date: dt.date | None = None,
        limit: int = 100,
        max_pages: int = MAX_STATEMENT_PAGES,
    ) -> list[dict[str, Any]]:
        if not account or not 1 <= limit <= 100 or not 1 <= max_pages <= self.MAX_STATEMENT_PAGES:
            raise RuntimeError("privat24_invalid_statement_request")
        if end_date is not None and end_date < start_date:
            raise RuntimeError("privat24_invalid_statement_dates")
        params: dict[str, Any] = {
            "acc": account,
            "startDate": start_date.strftime("%d-%m-%Y"),
            "limit": limit,
        }
        if end_date:
            params["endDate"] = end_date.strftime("%d-%m-%Y")

        items: list[dict[str, Any]] = []
        follow_id: str | None = None
        visited: set[str] = set()
        for _ in range(max_pages):
            current_params = dict(params)
            if follow_id:
                current_params["followId"] = follow_id
            response = httpx.get(
                f"{self.settings.privat24_api_base_url.rstrip('/')}/api/statements/transactions",
                params=current_params,
                headers=self._headers(),
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("status") != "SUCCESS":
                raise RuntimeError("privat24_invalid_statement_response")
            chunk = data.get("transactions")
            if not isinstance(chunk, list) or len(chunk) > limit or any(not isinstance(item, dict) for item in chunk):
                raise RuntimeError("privat24_invalid_statement_response")
            items.extend(chunk)
            more = data.get("exist_next_page")
            if more is False:
                return items
            if more is not True:
                raise RuntimeError("privat24_missing_pagination_evidence")
            follow_id = data.get("next_page_id")
            if not isinstance(follow_id, str) or not follow_id or follow_id in visited or not chunk:
                raise RuntimeError("privat24_invalid_pagination_cursor")
            visited.add(follow_id)
        raise RuntimeError("privat24_statement_page_limit")

    def print_receipt(self, account: str, reference: str, refn: str, per_page: int = 4) -> tuple[str, bytes]:
        if not 1 <= per_page <= 4:
            raise RuntimeError("privat24_invalid_receipt_request")
        with httpx.stream(
            "POST",
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
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/pdf", "application/octet-stream"}:
                raise RuntimeError("privat24_receipt_not_pdf")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > self.MAX_RECEIPT_BYTES:
                    raise RuntimeError("privat24_receipt_too_large")
        result = bytes(content)
        self.validate_receipt_pdf(result)
        # A server-supplied filename must never become a local path.
        return "privat24-payment-receipt.pdf", result

    @classmethod
    def validate_receipt_pdf(cls, content: bytes) -> None:
        if not isinstance(content, bytes) or not content or len(content) > cls.MAX_RECEIPT_BYTES:
            raise RuntimeError("privat24_invalid_receipt_size")
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
            raise RuntimeError("privat24_receipt_not_pdf")
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted or not len(reader.pages):
                raise ValueError("invalid receipt")
        except Exception:
            raise RuntimeError("privat24_invalid_receipt_pdf") from None
