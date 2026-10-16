from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.services.schemas import ReceiptValidationResult
from src.services.supplier_identity import supplier_identity_errors, supplier_source_text

UKRAINIAN_IBAN_LENGTH = 29
UKRAINIAN_IBAN_ACCOUNT_PREFIX = "000002600"


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


def _ukrainian_iban_check_digits(bban: str) -> str:
    provisional = f"UA00{bban}"
    rearranged = provisional[4:] + provisional[:4]
    converted = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    return f"{98 - (int(converted) % 97):02d}"


def _repair_ukrainian_iban_checksum(iban: str | None) -> str | None:
    normalized = normalize_iban(iban)
    if not normalized or len(normalized) != UKRAINIAN_IBAN_LENGTH or not normalized.startswith("UA"):
        return None
    bban = normalized[4:]
    if not bban.isdigit():
        return None
    candidate = f"UA{_ukrainian_iban_check_digits(bban)}{bban}"
    return candidate if is_valid_iban(candidate) else None


def _repair_ukrainian_iban_missing_zeroes(iban: str | None) -> str | None:
    normalized = normalize_iban(iban)
    if (
        not normalized
        or not normalized.startswith("UA")
        or len(normalized) >= UKRAINIAN_IBAN_LENGTH
        or not normalized[2:].isdigit()
    ):
        return None

    missing_count = UKRAINIAN_IBAN_LENGTH - len(normalized)
    if missing_count != 1:
        return None
    candidates = {normalized}
    for _ in range(missing_count):
        expanded: set[str] = set()
        for value in candidates:
            for index in range(10, len(value) + 1):
                expanded.add(f"{value[:index]}0{value[index:]}")
        candidates = expanded

    valid_candidates = sorted(
        candidate
        for candidate in candidates
        if len(candidate) == UKRAINIAN_IBAN_LENGTH
        and candidate[10:].startswith(UKRAINIAN_IBAN_ACCOUNT_PREFIX)
        and is_valid_iban(candidate)
    )
    return valid_candidates[0] if len(valid_candidates) == 1 else None


def extract_iban_candidates(raw_text: str | None) -> list[str]:
    if not raw_text:
        return []

    candidates: list[str] = []
    for match in re.finditer(r"\bUA\s*\d{2}(?:[\s-]*\d){20,35}\b", raw_text, flags=re.IGNORECASE):
        candidate = normalize_iban(match.group(0))
        if (
            candidate
            and len(candidate) == UKRAINIAN_IBAN_LENGTH
            and is_valid_iban(candidate)
            and candidate not in candidates
        ):
            candidates.append(candidate)
    return candidates


def repair_iban_from_raw_text(value: str | None, raw_text: str | None) -> str | None:
    iban = normalize_iban(value)
    if is_valid_iban(iban):
        return iban

    candidates = extract_iban_candidates(raw_text)
    if iban:
        matching_prefix = [candidate for candidate in candidates if candidate[:10] == iban[:10]]
        if len(matching_prefix) == 1:
            return matching_prefix[0]
    if not iban and len(candidates) == 1:
        return candidates[0]
    checksum_repair = _repair_ukrainian_iban_checksum(iban)
    if checksum_repair:
        return checksum_repair
    missing_zeroes_repair = _repair_ukrainian_iban_missing_zeroes(iban)
    if missing_zeroes_repair:
        return missing_zeroes_repair
    return iban


def normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("“", '"').replace("”", '"').replace("’", "'").replace("`", "'")
    return value or None


def normalize_tax_id(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) in {8, 10}:
        return digits
    return None


def digits_only(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


def raw_text_contains_tax_id(raw_text: str | None, tax_id: str | None) -> bool:
    if not raw_text or not tax_id:
        return False
    pattern = rf"(?<!\d){re.escape(tax_id)}(?!\d)"
    return bool(re.search(pattern, raw_text))


def extract_company_tax_ids(raw_text: str | None) -> list[str]:
    if not raw_text:
        return []

    patterns = [
        r"код\s+за\s+єдрпоу\s*[:№]?\s*(\d{8}|\d{10})(?!\d)",
        r"код\s+за\s+дрфо\s*[:№]?\s*(\d{10})(?!\d)",
        r"\bєдрпоу\s*[:№]?\s*(\d{8}|\d{10})(?!\d)",
        r"\bдрфо\s*[:№]?\s*(\d{10})(?!\d)",
        r"\bіпн\s*[:№]?\s*(\d{10})(?!\d)",
        r"\bінн\s*[:№]?\s*(\d{10})(?!\d)",
        r"\bрнокпп\s*[:№]?\s*(\d{10})(?!\d)",
        r"\bкод\s*[:№]?\s*(\d{8}|\d{10})(?!\d)",
    ]
    tax_ids: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE):
            tax_id = normalize_tax_id(match.group(1))
            if tax_id and tax_id not in tax_ids:
                tax_ids.append(tax_id)
    return tax_ids


def extract_company_tax_id(raw_text: str | None) -> str | None:
    tax_ids = extract_company_tax_ids(raw_text)
    return tax_ids[0] if tax_ids else None


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
    # Fallbacks may only use the supplier's block, never the buyer's requisites.
    supplier_text = supplier_source_text(validation.raw_text) or ""
    supplier_name = normalize_text(validation.supplier_name)
    supplier_tax_id = normalize_tax_id(validation.supplier_tax_id)
    raw_supplier_tax_id = digits_only(validation.supplier_tax_id)
    supplier_iban = repair_iban_from_raw_text(validation.supplier_iban, supplier_text)
    supplier_bank_name = normalize_text(validation.supplier_bank_name)
    normalized_purpose = normalize_text(purpose)
    company_tax_ids = extract_company_tax_ids(supplier_text)

    tax_id_conflicts_raw_text = False
    if company_tax_ids:
        if supplier_tax_id and raw_text_contains_tax_id(supplier_text, supplier_tax_id):
            pass
        elif supplier_tax_id and supplier_tax_id not in company_tax_ids:
            company_tax_ids_by_length = sorted(company_tax_ids, key=len, reverse=True)
            if supplier_tax_id in {tax_id[: len(supplier_tax_id)] for tax_id in company_tax_ids_by_length}:
                supplier_tax_id = company_tax_ids_by_length[0]
            else:
                tax_id_conflicts_raw_text = True
                supplier_tax_id = None
        elif not supplier_tax_id:
            supplier_tax_id = company_tax_ids[0]
    elif supplier_tax_id and supplier_text and not raw_text_contains_tax_id(supplier_text, supplier_tax_id):
        tax_id_conflicts_raw_text = True
        supplier_tax_id = None

    if (
        not supplier_tax_id
        and raw_supplier_tax_id
        and company_tax_ids
        and (
            len(raw_supplier_tax_id) not in {8, 10}
            or any(tax_id.startswith(raw_supplier_tax_id) for tax_id in company_tax_ids)
        )
    ):
        supplier_tax_id = company_tax_ids[0]
        tax_id_conflicts_raw_text = False

    errors = supplier_identity_errors(supplier_name, supplier_tax_id, supplier_iban, validation.raw_text)

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
    if not supplier_tax_id:
        errors.append("missing_supplier_tax_id")
    elif len(supplier_tax_id) not in {8, 10}:
        errors.append("invalid_supplier_tax_id")
    if tax_id_conflicts_raw_text:
        errors.append("supplier_tax_id_mismatch_raw_text")

    return PreflightResult(
        ok=not errors,
        normalized_supplier_name=supplier_name,
        normalized_supplier_tax_id=supplier_tax_id,
        normalized_supplier_iban=supplier_iban,
        normalized_supplier_bank_name=supplier_bank_name,
        normalized_purpose=normalized_purpose,
        errors=errors,
    )
