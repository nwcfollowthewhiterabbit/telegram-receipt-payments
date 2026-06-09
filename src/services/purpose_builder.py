from __future__ import annotations

import datetime as dt
import re

from .schemas import ReceiptValidationResult


class PaymentPurposeBuilder:
    CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
        (("коврол", "плінтус", "клей", "підлог", "пвх", "листовий", "буд"), "будівельні матеріали"),
        (("олива", "мастил", "масло", "mobil", "nuto"), "мастильні матеріали"),
        (("комбінезон", "bodyguard", "colad", "спецодяг", "захисн"), "спецодяг"),
        (("ноутбук", "монітор", "принтер", "оргтех", "комп'ютер", "компьютер", "картридж"), "оргтехніку"),
        (("канц", "папір", "папiр", "офісн", "офисн", "ручк"), "канцелярські товари"),
        (("послуг", "монтаж", "доставка", "оренда", "сервіс", "обслуговув"), "послуги"),
        (("авто", "запчаст", "шин", "акумулятор", "фільтр"), "автотовари"),
    ]

    @classmethod
    def infer_category(cls, validation: ReceiptValidationResult) -> str:
        if validation.procurement_category:
            return validation.procurement_category.strip()

        haystack = " ".join(
            chunk for chunk in [validation.summary, validation.raw_text, validation.payment_purpose] if chunk
        ).lower()
        normalized = re.sub(r"\s+", " ", haystack)
        for keywords, category in cls.CATEGORY_RULES:
            if any(keyword in normalized for keyword in keywords):
                return category
        return "товари"

    @staticmethod
    def _format_invoice_date(value: str | None) -> str:
        if not value:
            return "без дати"
        raw = value.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(raw, fmt).strftime("%d.%m.%Y")
            except ValueError:
                pass
        return raw

    @staticmethod
    def _extract_required_prefix(raw_text: str | None) -> str | None:
        if not raw_text:
            return None
        match = re.search(r"вказати\s+\*([^*]{1,40})\*\s+перед\s+текстом\s+призначення", raw_text, re.IGNORECASE)
        if match:
            return f"*{match.group(1).strip()}*"
        return None

    @staticmethod
    def _extract_vat_suffix(raw_text: str | None) -> str | None:
        if not raw_text:
            return None
        normalized = re.sub(r"\s+", " ", raw_text)
        if re.search(r"\bбез\s+ПДВ\b", normalized, re.IGNORECASE):
            return "без ПДВ"

        patterns = [
            r"(?:у\s+тому\s+числі|в\s*т\.?\s*ч\.?)\s*ПДВ\s*[:\-]?\s*([0-9][0-9\s]*[,.][0-9]{2})",
            r"\bПДВ\s*[:\-]?\s*([0-9][0-9\s]*[,.][0-9]{2})",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                amount = re.sub(r"\s+", "", match.group(1)).replace(".", ",")
                return f"у т.ч. ПДВ {amount} грн"
        return None

    @classmethod
    def _finalize(cls, purpose: str, validation: ReceiptValidationResult) -> str:
        result = re.sub(r"\s+", " ", purpose).strip()
        prefix = cls._extract_required_prefix(validation.raw_text)
        if prefix and not result.startswith(prefix):
            result = f"{prefix} {result}"

        vat_suffix = cls._extract_vat_suffix(validation.raw_text)
        if vat_suffix and "пдв" not in result.lower():
            result = f"{result}, {vat_suffix}"

        return result

    @classmethod
    def build(cls, validation: ReceiptValidationResult) -> str:
        explicit_purpose = (validation.payment_purpose or "").strip()
        if explicit_purpose:
            return cls._finalize(explicit_purpose, validation)

        category = cls.infer_category(validation)
        number = validation.invoice_number or "б/н"
        date = cls._format_invoice_date(validation.invoice_date)
        return cls._finalize(f"Оплата за {category} згідно рахунку №{number} від {date}", validation)
