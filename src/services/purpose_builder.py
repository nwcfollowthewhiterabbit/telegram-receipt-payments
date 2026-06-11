from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

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
        if re.search(r"\bбез\s+ПДВ\b", normalized, re.IGNORECASE) or re.search(
            r"\bне\s+платник\b.{0,30}\bПДВ\b|\bПДВ\b.{0,30}\bне\s+платник\b",
            normalized,
            re.IGNORECASE,
        ) or re.search(
            r"\bПДВ\)?\s*[:\-]?\s*0+(?:[,.]0+)?\b",
            normalized,
            re.IGNORECASE,
        ):
            return "без ПДВ"

        patterns = [
            r"(?:у\s+тому\s+числі|в\s*т\.?\s*ч\.?)\s*ПДВ(?:\s*\([^)]*\))?\s*[:\-]?\s*([0-9][0-9\s]*[,.][0-9]{2,4})",
            r"\bПДВ(?:\s*\([^)]*\))?\s*[:\-]?\s*([0-9][0-9\s]*[,.][0-9]{2,4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                raw_amount = re.sub(r"\s+", "", match.group(1)).replace(",", ".")
                try:
                    decimal_amount = Decimal(raw_amount).quantize(Decimal("0.01"))
                except InvalidOperation:
                    continue
                if decimal_amount == 0:
                    return "без ПДВ"
                amount = f"{decimal_amount:.2f}"
                return f"ПДВ - 20 % {amount} грн"
        return None

    @staticmethod
    def _format_amount(value: Decimal | None) -> str | None:
        if value is None:
            return None
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
        return f"{amount:.2f}"

    @staticmethod
    def _clean_item_text(value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip(" ,.;:-")
        value = re.sub(r"^\d+\s+", "", value)
        value = re.sub(r"\s+\d+$", "", value)
        noisy_markers = [
            "до договору",
            "постачальник",
            "рахунок",
            "платник",
            "найменування",
            "мфо",
            "послуги)",
            "кіл-сть",
            "од. вим",
            "ціна",
        ]
        lower = value.lower()
        for marker in noisy_markers:
            pos = lower.find(marker)
            if pos > 0:
                value = value[:pos].strip(" ,.;:-")
                lower = value.lower()
        value = re.sub(r",?\s*[^,]{0,30}послуги\)?$", "", value, flags=re.IGNORECASE).strip(" ,.;:-")
        return value

    @classmethod
    def _extract_item_description(cls, validation: ReceiptValidationResult) -> str | None:
        summary = validation.summary or ""
        summary_patterns = [
            r"рахунком на оплату за (.+?)(?:\.| Постачальник| Вказан)",
            r"рахунком-фактурою на оплату (.+?)(?:\.| від )",
            r"за надані (.+?)(?: від | для | на суму|\.)",
        ]
        for pattern in summary_patterns:
            match = re.search(pattern, summary, flags=re.IGNORECASE)
            if match:
                candidate = cls._clean_item_text(match.group(1))
                if (
                    candidate
                    and candidate.lower() not in {"товари", "послуги", "обладнання"}
                    and not candidate.lower().startswith(("від ", "от "))
                ):
                    return candidate

        raw_text = validation.raw_text or ""
        keyword_starts = [
            "Чилер",
            "Витяжка",
            "Рукавички",
            "Пакет",
            "Послуги",
            "Комплекс",
            "ВИСТАВКОВИЙ",
            "ПВХ",
            "Передплата",
        ]
        direct_items: list[str] = []
        for keyword in keyword_starts:
            pos = raw_text.lower().find(keyword.lower())
            if pos < 0:
                continue
            chunk = raw_text[pos : pos + 140]
            unit_match = re.search(r"\s+(?:шт|пар|послуг|послуга|кг|м2|м²)\s+\d+", chunk, re.IGNORECASE)
            if unit_match:
                chunk = chunk[: unit_match.start()]
            candidate = cls._clean_item_text(chunk)
            if candidate and not any(candidate in item or item in candidate for item in direct_items):
                direct_items.append(candidate)
            if len(direct_items) == 2:
                return ", ".join(direct_items)
        if direct_items:
            return ", ".join(direct_items)

        keyword_item_pattern = re.compile(
            r"((?:Чилер|Витяжка|Рукавички|Пакет|Послуги|Комплекс|ВИСТАВКОВИЙ|ПВХ|Передплата|ОПЛАТА)"
            r"[А-ЯІЇЄҐа-яіїєґA-Za-z0-9№&+.,()\"\\-\\s]{5,120}?)"
            r"\s+(?:шт|пар|послуг|послуга|кг|м2|м²)\s+\d+",
            re.IGNORECASE,
        )
        keyword_items: list[str] = []
        for match in keyword_item_pattern.finditer(raw_text):
            candidate = cls._clean_item_text(match.group(1))
            if candidate and candidate not in keyword_items:
                keyword_items.append(candidate)
            if len(keyword_items) == 2:
                return ", ".join(keyword_items)
        if keyword_items:
            return ", ".join(keyword_items)

        item_pattern = re.compile(
            r"([А-ЯІЇЄҐA-Z][А-ЯІЇЄҐа-яіїєґA-Za-z0-9№&+.,()\"\\-\\s]{10,120}?)"
            r"\s+(?:шт|пар|послуг|послуга|кг|м2|м²)?\s*\d+(?:[,.]\d+)?\s+[0-9][0-9\s]*[,.]\d{2}",
            re.IGNORECASE,
        )
        items: list[str] = []
        for match in item_pattern.finditer(raw_text):
            candidate = cls._clean_item_text(match.group(1))
            if not candidate or candidate.lower() in {"всього", "ціна сума"}:
                continue
            if len(candidate) < 8 or candidate in items:
                continue
            items.append(candidate)
            if len(items) == 2:
                break
        if items:
            return ", ".join(items)
        return None

    @classmethod
    def _finalize(cls, purpose: str, validation: ReceiptValidationResult) -> str:
        result = re.sub(r"\s+", " ", purpose).strip()
        prefix = cls._extract_required_prefix(validation.raw_text)
        if prefix and not result.startswith(prefix):
            result = f"{prefix} {result}"

        vat_suffix = cls._extract_vat_suffix(validation.raw_text)
        if vat_suffix and "пдв" not in result.lower():
            amount = cls._format_amount(validation.amount)
            amount_suffix = f"У сумi {amount} грн., " if amount else ""
            result = f"{result}. {amount_suffix}{vat_suffix}"

        return result

    @classmethod
    def build(cls, validation: ReceiptValidationResult) -> str:
        explicit_purpose = (validation.payment_purpose or "").strip()
        if explicit_purpose:
            return cls._finalize(explicit_purpose, validation)

        category = cls.infer_category(validation)
        detail = cls._extract_item_description(validation)
        subject = detail or category
        number = validation.invoice_number or "б/н"
        date = cls._format_invoice_date(validation.invoice_date)
        return cls._finalize(f"Оплата за {subject} зг-но рахунку №{number} від {date}", validation)
