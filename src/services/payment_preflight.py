from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.services.schemas import ReceiptValidationResult


def normalize_iban(value: str | None) -> str | None:
    if not value:
        return None
    normalized = "".join(ch for ch in value.upper() if ch.isalnum())
    return normalized or None


def is_valid_iban(value: str | None) -> bool:
    iban = normalize_iban(value)
    if not iban or len(iban) < 15 or len(iban) > 34:
        return False
    if not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    converted = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    return int(converted) % 97 == 1


def normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("“", '"').replace("”", '"').replace("’", "'").replace("`", "'")
    return value or None


def extract_company_tax_id(raw_text: str | None) -> str | None:
    if not raw_text:
        return None

    patterns = [
        r"код\s+за\s+єдрпоу\s*[:№]?\s*(\d{8})",
        r"\bєдрпоу\s*[:№]?\s*(\d{8})",
        r"\bкод\s*[:№]?\s*(\d{8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    normalized_supplier_name: str | None = None
    normalized_supplier_tax_id: str | None = None
    normalized_supplier_iban: str | None = None
    normalized_supplier_bank_name: str | None = None
    normalized_purpose: str | None = None
    errors: list[str] = field(default_factory=list)


def run_preflight(validation: ReceiptValidationResult, purpose: str) -> PreflightResult:
    supplier_name = normalize_text(validation.supplier_name)
    supplier_tax_id = "".join(ch for ch in (validation.supplier_tax_id or "") if ch.isdigit()) or None
    supplier_iban = normalize_iban(validation.supplier_iban)
    supplier_bank_name = normalize_text(validation.supplier_bank_name)
    normalized_purpose = normalize_text(purpose)
    company_tax_id = extract_company_tax_id(validation.raw_text)

    if company_tax_id:
        supplier_tax_id = company_tax_id

    errors: list[str] = []

    if not supplier_name:
        errors.append("missing_supplier_name")
    if not supplier_iban:
        errors.append("missing_supplier_iban")
    elif not is_valid_iban(supplier_iban):
        errors.append("invalid_supplier_iban")

    if not validation.amount or validation.amount <= 0:
        errors.append("invalid_amount")
    if not validation.currency:
        errors.append("missing_currency")
    if not normalized_purpose:
        errors.append("missing_payment_purpose")
    if supplier_tax_id and len(supplier_tax_id) not in {8, 10}:
        if len(supplier_tax_id) > 10:
            supplier_tax_id = supplier_tax_id[:8]
        else:
            errors.append("invalid_supplier_tax_id")

    return PreflightResult(
        ok=not errors,
        normalized_supplier_name=supplier_name,
        normalized_supplier_tax_id=supplier_tax_id,
        normalized_supplier_iban=supplier_iban,
        normalized_supplier_bank_name=supplier_bank_name,
        normalized_purpose=normalized_purpose,
        errors=errors,
    )
