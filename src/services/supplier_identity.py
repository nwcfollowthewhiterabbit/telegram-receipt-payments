"""Conservative, source-grounded supplier identity checks.

Only an explicitly labelled, unique supplier/payee block is evidence.  This
module neither repairs account identifiers nor infers roles from their order.
Unsupported or ambiguous layouts require manual review.
"""

from __future__ import annotations

import re
import unicodedata


_SUPPLIER_LABELS = (
    "постачальник", "поставщик", "продавець", "продавец", "виконавець",
    "исполнитель", "supplier", "seller", "vendor", "service provider",
    "payment recipient", "payee", "beneficiary", "отримувач коштів",
    "одержувач коштів", "отримувач платежу", "одержувач платежу",
    "получатель платежа", "получатель средств",
)
_OTHER_PARTY_LABELS = (
    "одержувач послуг", "отримувач послуг", "одержувач товарів",
    "отримувач товарів", "получатель услуг", "получатель товаров",
    "service recipient", "recipient of services", "recipient of goods",
    "покупець", "покупатель", "замовник", "заказчик", "платник",
    "плательщик", "клієнт", "клиент", "споживач", "потребитель",
    "вантажоодержувач", "грузополучатель", "buyer", "customer", "payer",
    "client", "consumer", "consignee", "bill to", "ship to", "bill-to", "ship-to",
    # A bare recipient is ambiguous: it delimits a party but does not prove
    # that the party receives money rather than the purchased goods/services.
    "одержувач", "отримувач", "получатель", "recipient",
)


def _label_pattern(label: str) -> str:
    return r"\s+".join(re.escape(word) for word in label.split())


_PARTY_LABEL = re.compile(
    r"(?<!\w)(?P<label>" + "|".join(
        _label_pattern(label)
        for label in sorted((*_SUPPLIER_LABELS, *_OTHER_PARTY_LABELS), key=len, reverse=True)
    ) + r")(?!\w)",
    re.IGNORECASE,
)
_SUPPLIER_LABEL_SET = set(_SUPPLIER_LABELS)
_TAX_LABEL = r"(?:код\s+за\s+(?:єдрпоу|едрпоу|дрфо)|єдрпоу|едрпоу|дрфо|рнокпп|іпн|инн|інн|tax\s*id|код)"
_TAX_ID = re.compile(r"(?<!\w)" + _TAX_LABEL + r"\s*[:№#]?\s*(\d{10}|\d{8})(?!\d|[.,]\d)", re.IGNORECASE)
_NAME_END = re.compile(
    r"(?<!\w)(?:" + _TAX_LABEL
    + r"|iban|мфо|банк|bank|рахунок|р\s*/\s*р|адреса|адрес|address|телефон|тел\.|phone)\b"
    + r"|(?<!\w)[A-Z]{2}\d{2}(?:[\s-]*[A-Z0-9]){11,30}"
    + r"|(?<![\d.,])\d{8,12}(?![\d.,])",
    re.IGNORECASE,
)
_LEGAL_FORMS = (
    (r"товариство\s+з\s+обмеженою\s+відповідальністю|общество\s+с\s+ограниченной\s+ответственностью|limited\s+liability\s+company|тов|ооо|llc", "llc"),
    (r"фізична\s+особа\s*[-–—]?\s*підприємець|физическое\s+лицо\s*[-–—]?\s*предприниматель|фоп|флп", "soleproprietor"),
    (r"приватне\s+підприємство|частное\s+предприятие|пп|чп", "privateenterprise"),
    (r"акціонерне\s+товариство|акционерное\s+общество|ат|ао", "jointstockcompany"),
)


def _clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return value.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "`": "'"}))


def _supplier_blocks(source_text: str | None) -> list[str]:
    if not source_text:
        return []
    source_text = _clean_text(source_text)
    labels = list(_PARTY_LABEL.finditer(source_text))
    blocks: list[str] = []
    for index, match in enumerate(labels):
        label = re.sub(r"\s+", " ", match.group("label")).casefold()
        if label in _SUPPLIER_LABEL_SET:
            end = labels[index + 1].start() if index + 1 < len(labels) else len(source_text)
            blocks.append(source_text[match.end():end].strip(" \t\r\n:|;–—-"))
    return blocks


def supplier_source_text(source_text: str | None) -> str | None:
    """Return the unique explicit supplier block, or None if unproven.

    Callers may use this scope for existing identifier normalization/repair;
    they must still check the resulting complete tuple with the guard below.
    """
    blocks = _supplier_blocks(source_text)
    if len(blocks) != 1 or not blocks[0] or len(_source_tax_ids(blocks[0])) > 1:
        return None
    return blocks[0]


def _name_key(value: str) -> tuple[str, ...]:
    normalized = _clean_text(value).casefold()
    for pattern, replacement in _LEGAL_FORMS:
        normalized = re.sub(r"(?<!\w)(?:" + pattern + r")(?!\w)", replacement, normalized)
    return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _source_name(block: str) -> str:
    block = re.sub(r"^(?:найменування|назва|название|name)\s*:\s*", "", block, flags=re.IGNORECASE)
    for match in _NAME_END.finditer(block):
        # Words like 'Код' can be part of a quoted legal company name.
        if block[:match.start()].count('"') % 2 == 0:
            block = block[:match.start()]
            break
    lines = [line.strip(" \t|;:") for line in block.splitlines() if line.strip(" \t|;:")]
    if not lines:
        return ""
    name = lines[0]
    index = 1
    legal_only = {("llc",), ("soleproprietor",), ("privateenterprise",), ("jointstockcompany",)}
    while index < len(lines) and (name.count('"') % 2 or _name_key(name) in legal_only):
        name += " " + lines[index]
        index += 1
    return name.strip(" \t|;:")


def _source_tax_ids(block: str) -> set[str]:
    tax_ids = {match.group(1) for match in _TAX_ID.finditer(block)}
    # Some invoice templates put the unlabelled registration code on its own
    # line. Do not accept an arbitrary invoice number, amount or substring.
    tax_ids.update(re.findall(r"(?m)^\s*(\d{10}|\d{8})\s*$", block))
    return tax_ids


def _source_has_iban(block: str, iban: str | None) -> bool:
    normalized = re.sub(r"[\s-]+", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", normalized):
        return False
    pattern = r"(?<![A-Z0-9])" + r"[\s-]*".join(re.escape(char) for char in normalized) + r"(?![A-Z0-9])"
    return re.search(pattern, block, re.IGNORECASE) is not None


def supplier_identity_errors(
    name: str | None,
    tax_id: str | None,
    iban: str | None,
    source_text: str | None,
) -> list[str]:
    """Check a complete beneficiary tuple against one source supplier block.

    Empty/unlabelled source and multiple supplier blocks fail closed.  Neither
    an unrelated party's identifier nor a model's confidence is evidence.
    """
    if not source_text or not source_text.strip():
        return ["supplier_source_evidence_missing"]
    blocks = _supplier_blocks(source_text)
    if not blocks or not any(blocks):
        return ["supplier_role_evidence_missing"]
    if len(blocks) != 1:
        return ["supplier_role_evidence_ambiguous"]
    block = blocks[0]
    source_tax_ids = _source_tax_ids(block)
    if len(source_tax_ids) > 1:
        return ["supplier_role_evidence_ambiguous"]
    errors: list[str] = []
    source_name = _source_name(block)
    if not name or not source_name or _name_key(name) != _name_key(source_name):
        errors.append("supplier_name_mismatch_source")
    normalized_tax_id = re.sub(r"\s+", "", tax_id or "")
    if not re.fullmatch(r"\d{8}|\d{10}", normalized_tax_id) or normalized_tax_id not in source_tax_ids:
        errors.append("supplier_tax_id_mismatch_source")
    if not _source_has_iban(block, iban):
        errors.append("supplier_iban_mismatch_source")
    return errors
