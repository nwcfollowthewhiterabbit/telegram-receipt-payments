from __future__ import annotations

import datetime as dt
import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from src.config import get_settings

from .schemas import ReceiptValidationResult


class PaymentPurposeBuilder:
    MONTHS_UA: dict[str, int] = {
        "січня": 1,
        "сiчня": 1,
        "лютого": 2,
        "березня": 3,
        "квітня": 4,
        "квiтня": 4,
        "травня": 5,
        "червня": 6,
        "липня": 7,
        "серпня": 8,
        "вересня": 9,
        "жовтня": 10,
        "листопада": 11,
        "грудня": 12,
    }
    CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
        (("коврол", "плінтус", "клей", "підлог", "пвх", "листовий", "буд"), "будівельні матеріали"),
        (("олива", "мастил", "масло", "mobil", "nuto"), "мастильні матеріали"),
        (("комбінезон", "bodyguard", "colad", "спецодяг", "захисн"), "спецодяг"),
        (("ноутбук", "монітор", "принтер", "оргтех", "комп'ютер", "компьютер", "картридж"), "оргтехніку"),
        (("канц", "папір", "папiр", "офісн", "офисн", "ручк"), "канцелярські товари"),
        (("послуг", "монтаж", "доставка", "оренда", "сервіс", "обслуговув"), "послуги"),
        (("авто", "запчаст", "шин", "акумулятор", "фільтр"), "автотовари"),
    ]
    SUBJECT_FOR_PURPOSE: dict[str, str] = {
        "плівка": "плівку",
    }
    SUBJECT_PREFIX_REPLACEMENTS: tuple[tuple[str, str], ...] = (
        ("організація ", "організацію "),
        ("розробка ", "розробку "),
    )
    FORBIDDEN_PURPOSE_PATTERNS: tuple[str, ...] = (
        r"\bкод\b",
        r"\bкільк(?:ість|\.?-?ть)\b",
        r"\bод\.?\s*(?:вим)?\b",
        r"\bціна\b",
        r"\bсума\b",
        r"\bартикул\b",
        r"товари\s*\(\s*роботи\s*,\s*послуги\s*\)",
        r"послуги\)",
        r"режим\s+полімер",
        r"тип\s+нанесення",
        r"\|",
    )

    @classmethod
    def infer_category(cls, validation: ReceiptValidationResult) -> str:
        if validation.procurement_category:
            category = validation.procurement_category.strip(" ,.;:-")
            if category:
                return category

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
        parsed = PaymentPurposeBuilder._parse_date(raw)
        if parsed:
            return parsed.strftime("%d.%m.%Y")
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(raw, fmt).strftime("%d.%m.%Y")
            except ValueError:
                pass
        return raw

    @classmethod
    def _parse_date(cls, value: str | None) -> dt.date | None:
        if not value:
            return None
        raw = re.sub(r"\s+", " ", value.strip().lower()).strip(" ,.;")
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return dt.datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
        match = re.search(r"\b(\d{1,2})\s+([а-яіїєґi]+)\s+(\d{4})\s*(?:р\.?)?\b", raw, re.IGNORECASE)
        if not match:
            return None
        month = cls.MONTHS_UA.get(match.group(2).lower())
        if not month:
            return None
        try:
            return dt.date(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            return None

    @classmethod
    def normalize_purpose_dates(cls, purpose: str) -> str:
        def replace_iso(match: re.Match[str]) -> str:
            parsed = cls._parse_date(match.group(0))
            return parsed.strftime("%d.%m.%Y") if parsed else match.group(0)

        def replace_month_name(match: re.Match[str]) -> str:
            parsed = cls._parse_date(match.group(0))
            return parsed.strftime("%d.%m.%Y") if parsed else match.group(0)

        purpose = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", replace_iso, purpose)
        purpose = re.sub(
            r"\b\d{1,2}\s+[А-ЯІЇЄҐа-яіїєґi]+\s+20\d{2}\s*(?:р\.?)?\b",
            replace_month_name,
            purpose,
            flags=re.IGNORECASE,
        )
        return purpose

    @staticmethod
    def _parse_money(value: str | None) -> Decimal | None:
        if value is None:
            return None
        text = re.sub(r"\s+", "", str(value).strip())
        if not text:
            return None
        text = re.sub(r"[^0-9,.-]", "", text).strip(".,")
        if not text or text in {".", ",", "-"}:
            return None

        if "," in text and "." in text:
            decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
            thousands_separator = "." if decimal_separator == "," else ","
            text = text.replace(thousands_separator, "").replace(decimal_separator, ".")
        elif "," in text:
            parts = text.split(",")
            if len(parts[-1]) in {1, 2, 3, 4}:
                text = "".join(parts[:-1]) + "." + parts[-1]
            else:
                text = "".join(parts)
        elif text.count(".") > 1:
            parts = text.split(".")
            text = "".join(parts[:-1]) + "." + parts[-1]

        try:
            return Decimal(text).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _format_money(value: Decimal | None) -> str | None:
        if value is None:
            return None
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
        return f"{amount:.2f}".replace(".", ",")

    @staticmethod
    def _extract_required_prefix(raw_text: str | None) -> str | None:
        if not raw_text:
            return None
        match = re.search(r"вказати\s+\*([^*]{1,40})\*\s+перед\s+текстом\s+призначення", raw_text, re.IGNORECASE)
        if match:
            return f"*{match.group(1).strip()}*"
        return None

    @classmethod
    def _extract_vat_amount(cls, raw_text: str | None) -> Decimal | None:
        if not raw_text:
            return None
        amount = r"([0-9][0-9\s'’.,]*(?:[,.]\d{1,4})?)"
        patterns = [
            rf"(?:сума\s+ПДВ|ПДВ\s+до\s+сплати)\s*[:\-\|\s]*{amount}",
            rf"(?:у\s+тому\s+числі|в\s*т\.?\s*ч\.?)\s*ПДВ(?:\s*\([^)]*\))?\s*[:\-\|\s]*{amount}",
            rf"(?<!без\s)\bПДВ(?:\s*\([^)]*\))?\s*[:\-\|\s]+{amount}",
            rf"(?<!без\s)\bПДВ\s+\d{{1,2}}\s*%\s*[:\-\|\s]*{amount}",
        ]
        for line in raw_text.splitlines():
            normalized = re.sub(r"\s+", " ", line).strip()
            if not normalized or "пдв" not in normalized.lower():
                continue
            if re.search(r"\bбез\s+ПДВ\b", normalized, re.IGNORECASE):
                continue
            for pattern in patterns:
                match = re.search(pattern, normalized, re.IGNORECASE)
                if not match:
                    continue
                value = cls._parse_money(match.group(1))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _is_without_vat(raw_text: str | None) -> bool:
        if not raw_text:
            return False
        normalized = re.sub(r"\s+", " ", raw_text)
        return bool(
            re.search(
                r"\bне\s+платник\b.{0,30}\bПДВ\b|\bПДВ\b.{0,30}\bне\s+платник\b",
                normalized,
                re.IGNORECASE,
            )
            or re.search(r"\bПДВ\)?\s*[:\-]?\s*0+(?:[,.]0+)?\b", normalized, re.IGNORECASE)
            or re.search(
                r"(?:сума|усього|всього|разом|до\s+сплати|в\s+т\.?\s*ч\.?)\s+без\s+ПДВ\b",
                normalized,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _extract_vat_suffix(cls, raw_text: str | None) -> str | None:
        vat_amount = cls._extract_vat_amount(raw_text)
        if vat_amount is not None:
            if vat_amount == 0:
                return None
            return f"у т.ч. ПДВ {cls._format_money(vat_amount)} грн"
        return None

    @classmethod
    def _format_amount(cls, value: Decimal | None) -> str | None:
        formatted = cls._format_money(value)
        return formatted.replace(",", ".") if formatted else None

    @staticmethod
    def _has_forbidden_purpose_noise(value: str | None) -> bool:
        if not value:
            return False
        normalized = re.sub(r"\s+", " ", value).lower()
        return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in PaymentPurposeBuilder.FORBIDDEN_PURPOSE_PATTERNS)

    @staticmethod
    def _looks_like_generic_supplier_buyer_subject(value: str | None) -> bool:
        if not value:
            return False
        normalized = re.sub(r"\s+", " ", value).lower()
        return bool(
            re.search(r"\bнадан[іи]\b.{0,120}\bдля\b", normalized)
            or re.search(r"\bпостачальник\w*\b.{0,120}\bпокупц", normalized)
            or re.search(r"\bтов\b.{0,80}\bдля\s+тов\b", normalized)
        )

    @classmethod
    def _clean_item_text(cls, value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip(" ,.;:-|")
        value = re.sub(r"^\d+(?:[,.]\d+)?\s*\|\s*", "", value)
        if "|" in value:
            parts = [part.strip(" ,.;:-") for part in value.split("|")]
            text_parts = [
                part
                for part in parts
                if len(part) >= 8
                and not re.fullmatch(r"[A-ZА-ЯІЇЄҐ0-9№./\-\s]+", part)
                and not re.search(r"^(?:шт|послуга|послуг|пар|кг|м2|м²)$", part, re.IGNORECASE)
                and not re.fullmatch(r"[0-9\s.,]+", part)
                and not cls._has_forbidden_purpose_noise(part)
            ]
            if text_parts:
                value = max(text_parts, key=len)
        value = re.sub(r"^\d+\s+", "", value)
        value = re.sub(r"^\d+(?:[,.]\d+)?\s+", "", value)
        value = re.sub(r"\s+\d+$", "", value)
        noisy_markers = [
            "товари (роботи, послуги)",
            "до договору",
            "постачальник",
            "рахунок",
            "платник",
            "найменування",
            "мфо",
            "послуги)",
            "кількість",
            "кіл-сть",
            "од. вим",
            "ціна",
            "сума",
            "режим полімер",
            "тип нанесення",
        ]
        value = re.sub(r"^\d+(?:[,.]\d+)?\.?\s+", "", value)
        value = re.sub(
            r"\s+(?:шт|пар|послуг|послуга|год\.?|кг|м2|м²)?\s*[0-9][0-9\s'.,]*"
            r"(?:\s+[0-9][0-9\s'.,]*){1,4}$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip(" ,.;:-|")
        lower = value.lower()
        for marker in noisy_markers:
            pos = lower.find(marker)
            if pos >= 0:
                if pos == 0:
                    value = ""
                else:
                    value = value[:pos].strip(" ,.;:-|")
                lower = value.lower()
        value = re.sub(r",?\s*[^,]{0,30}послуги\)?$", "", value, flags=re.IGNORECASE).strip(" ,.;:-|")
        if cls._has_forbidden_purpose_noise(value) or cls._looks_like_generic_supplier_buyer_subject(value):
            return ""
        return value

    @classmethod
    def _line_may_contain_item(cls, line: str) -> bool:
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized:
            return False
        if re.search(
            r"^(?:увага|зразок|отримувач|банк|ат\s+кб|постачальник|покупець|договір|разом|всього|усього|"
            r"у\s+тому\s+числі|сто\s|виписав|п/р|код\s+за|рахунок\s+на\s+оплату|шт\b|послуга\b|послуг\b)\b",
            normalized,
            re.IGNORECASE,
        ):
            return False
        if "|" in normalized:
            parts = [part.strip() for part in normalized.split("|")]
            if len(parts) < 4:
                return False
            return bool(
                re.search(r"\b(?:шт|послуга|послуг|пар|год\.?|кг|м2|м²)\b", normalized, re.IGNORECASE)
                or sum(bool(re.fullmatch(r"[0-9\s'.,]+", part)) for part in parts) >= 2
            )
        if re.match(r"^\d+(?:[,.]\d+)?\s+\S+", normalized):
            return True
        if re.search(r"\b(?:шт|послуга|послуг|пар|кг|м2|м²)\b", normalized, re.IGNORECASE) and re.search(
            r"\d+[,.]\d{2}", normalized
        ):
            return True
        return False

    @classmethod
    def _line_is_not_item_context(cls, line: str) -> bool:
        return bool(
            re.search(
                r"^(?:увага|зразок|отримувач|банк|ат\s+кб|постачальник|покупець|договір|разом|всього|усього|"
                r"у\s+тому\s+числі|сто\s|виписав|п/р|код\s+за|рахунок\s+на\s+оплату|рахунок-фактура|"
                r"підстава|валюта|гривня|курс|на\s+дату|умова|одержувач|платник|єдрпоу|іпн|адреса|тел\.)\b",
                line,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _extract_line_item_description(cls, raw_text: str) -> str | None:
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
        for index, line in enumerate(lines):
            if (
                line
                and not cls._line_may_contain_item(line)
                and not cls._line_is_not_item_context(line)
                and re.search(r"[А-ЯІЇЄҐа-яіїєґA-Za-z]{3,}", line)
            ):
                following = " ".join(lines[index + 1 : index + 3])
                if re.search(r"\b(?:шт|послуга|послуг|пар|год\.?|кг|м2|м²)\b\s+[0-9]", following, re.IGNORECASE):
                    continuation = []
                    for next_line in lines[index + 1 : index + 3]:
                        if re.search(r"\b(?:шт|послуга|послуг|пар|год\.?|кг|м2|м²)\b\s+[0-9]", next_line, re.IGNORECASE):
                            break
                        if next_line and not cls._line_is_not_item_context(next_line):
                            continuation.append(next_line)
                    candidate = cls._clean_item_text(" ".join([line, *continuation]))
                    if candidate and len(candidate) >= 8:
                        return candidate
            if not cls._line_may_contain_item(line):
                continue
            chunk = line
            if "|" not in chunk:
                for next_line in lines[index + 1 : index + 3]:
                    if not next_line or cls._line_may_contain_item(next_line) or re.search(
                        r"^(?:разом|всього|усього|у\s+тому\s+числі|сто\s|виписав)\b",
                        next_line,
                        re.IGNORECASE,
                    ):
                        break
                    chunk = f"{chunk} {next_line}"
            candidate = cls._clean_item_text(chunk)
            if candidate and len(candidate) >= 10:
                return candidate
        return None

    @classmethod
    def _subject_for_purpose(cls, subject: str) -> str:
        normalized = re.sub(r"\s+", " ", subject).strip(" ,.;:-|")
        mapped = cls.SUBJECT_FOR_PURPOSE.get(normalized.lower(), normalized)
        lowered = mapped.lower()
        for source, replacement in cls.SUBJECT_PREFIX_REPLACEMENTS:
            if lowered.startswith(source):
                mapped = replacement + mapped[len(source) :]
                break
        first_word = mapped.split(" ", 1)[0] if mapped else ""
        if (
            mapped
            and mapped[:1].isupper()
            and not mapped.isupper()
            and not (len(first_word) <= 4 and first_word.isupper())
        ):
            mapped = mapped[:1].lower() + mapped[1:]
        return mapped

    @staticmethod
    def _looks_like_invoice_reference(value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value).strip().lower()
        if not normalized:
            return False
        has_payment_action = re.search(r"\b(оплат|сплат|плат[еі]ж|передплат)\w*", normalized, re.IGNORECASE)
        has_invoice_marker = re.search(r"\b(рахунок|рахунку|рах\.?|№)\b", normalized, re.IGNORECASE)
        has_date_marker = re.search(r"\bвід\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b20\d{2}\b", normalized)
        starts_with_invoice_number = bool(re.match(r"^[a-zа-яіїєґ]{1,6}[-\s]*\d", normalized, re.IGNORECASE))
        return not has_payment_action and (has_invoice_marker or (starts_with_invoice_number and has_date_marker))

    @classmethod
    def _extract_item_description(cls, validation: ReceiptValidationResult) -> str | None:
        raw_text = validation.raw_text or ""
        line_item = cls._extract_line_item_description(raw_text)
        if line_item:
            return line_item

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
            r"[А-ЯІЇЄҐа-яіїєґA-Za-z0-9№&+.,()\"\-\s]{5,120}?)"
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
            r"([А-ЯІЇЄҐA-Z][А-ЯІЇЄҐа-яіїєґA-Za-z0-9№&+.,()\"\-\s]{10,120}?)"
            r"\s+(?:шт|пар|послуг|послуга|кг|м2|м²)?\s*\d+(?:[,.]\d+)?\s+[0-9][0-9\s.,]*[,.]\d{2}",
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
        return None

    @classmethod
    def _strip_existing_vat_suffix(cls, purpose: str) -> str:
        purpose = re.sub(
            r"\s*[.;,]?\s*(?:у\s+т\.\s*ч\.|в\s+т\.\s*ч\.|у\s+тому\s+числі)?\s*ПДВ\s*[0-9\s.,]+\s*(?:грн\.?)?\s*\.?$",
            "",
            purpose,
            flags=re.IGNORECASE,
        )
        purpose = re.sub(r"\s*[.;,]?\s*без\s+ПДВ\s*\.?$", "", purpose, flags=re.IGNORECASE)
        purpose = re.sub(r"\s*[.;,]?\s*без\s*\.?$", "", purpose, flags=re.IGNORECASE)
        return purpose.strip(" ,.;")

    @classmethod
    def _strip_without_vat_suffix(cls, purpose: str) -> str:
        purpose = re.sub(r"\s*[.;,]?\s*без\s+ПДВ\s*\.?$", "", purpose, flags=re.IGNORECASE)
        purpose = re.sub(r"\s*[.;,]?\s*без\s*\.?$", "", purpose, flags=re.IGNORECASE)
        return purpose.strip(" ,.;")

    @classmethod
    def _finalize(cls, purpose: str, validation: ReceiptValidationResult) -> str:
        result = re.sub(r"\s+", " ", purpose).strip(" ,;")
        result = re.sub(r"\s+([.,;:])", r"\1", result)
        result = re.sub(r"№\s*", "№", result)
        result = re.sub(r"\s*,\s*від\b", " від", result, flags=re.IGNORECASE)
        result = cls.normalize_purpose_dates(result)
        if cls._is_without_vat(validation.raw_text):
            result = cls._strip_without_vat_suffix(result)
        prefix = cls._extract_required_prefix(validation.raw_text)
        if prefix and not result.startswith(prefix):
            result = f"{prefix} {result}"

        vat_suffix = cls._extract_vat_suffix(validation.raw_text)
        if vat_suffix:
            result = cls._strip_existing_vat_suffix(result)
            separator = "" if result.endswith(".") else "."
            result = f"{result}{separator} {vat_suffix}."
        elif result and not result.endswith("."):
            result = f"{result}."
        return result

    @classmethod
    def _safe_subject(cls, validation: ReceiptValidationResult) -> str:
        category = cls.infer_category(validation)
        detail = cls._extract_item_description(validation)
        subject = cls._subject_for_purpose(detail or category)
        if not subject or cls._has_forbidden_purpose_noise(subject) or cls._looks_like_generic_supplier_buyer_subject(subject):
            subject = cls._subject_for_purpose(category)
        return subject or "товари"

    @classmethod
    def _deterministic_build(cls, validation: ReceiptValidationResult) -> str:
        explicit_purpose = (validation.payment_purpose or "").strip()
        if explicit_purpose:
            if cls._looks_like_invoice_reference(explicit_purpose):
                subject = cls._safe_subject(validation)
                if re.search(r"\bзгідно\s+рах", explicit_purpose, re.IGNORECASE):
                    return cls._finalize(f"Оплата за {subject} {explicit_purpose}", validation)
                return cls._finalize(f"Оплата за {subject} згідно рахунку {explicit_purpose}", validation)
            if not cls._has_forbidden_purpose_noise(explicit_purpose):
                return cls._finalize(explicit_purpose, validation)

        subject = cls._safe_subject(validation)
        number = validation.invoice_number or "б/н"
        date = cls._format_invoice_date(validation.invoice_date)
        return cls._finalize(f"Оплата за {subject} згідно рахунку №{number} від {date}", validation)

    @classmethod
    def _purpose_has_expected_vat(cls, purpose: str, validation: ReceiptValidationResult) -> bool:
        expected = cls._extract_vat_amount(validation.raw_text)
        if expected is None:
            return True
        match = re.search(r"пдв\s*([0-9][0-9\s.,]*[,.][0-9]{1,4})", purpose, re.IGNORECASE)
        if not match:
            return False
        actual = cls._parse_money(match.group(1))
        return actual == expected

    @classmethod
    def _is_acceptable_purpose(cls, purpose: str | None, validation: ReceiptValidationResult) -> bool:
        if not purpose:
            return False
        purpose = purpose.strip()
        if len(purpose) < 20 or len(purpose) > 160:
            return False
        if not purpose.lower().startswith(("оплата", "*")):
            return False
        if cls._has_forbidden_purpose_noise(purpose):
            return False
        if cls._looks_like_generic_supplier_buyer_subject(purpose):
            return False
        if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", purpose):
            return False
        if validation.invoice_number and validation.invoice_number not in purpose:
            return False
        if not cls._purpose_has_expected_vat(purpose, validation):
            return False
        return True

    @classmethod
    def _build_ai_prompt(cls, validation: ReceiptValidationResult, fallback: str) -> str:
        payload: dict[str, Any] = {
            "invoice_number": validation.invoice_number,
            "invoice_date": validation.invoice_date,
            "supplier_name": validation.supplier_name,
            "amount": str(validation.amount) if validation.amount is not None else None,
            "currency": validation.currency,
            "procurement_category": cls.infer_category(validation),
            "existing_payment_purpose": validation.payment_purpose,
            "fallback_purpose": fallback,
            "vat_suffix_required": cls._extract_vat_suffix(validation.raw_text),
            "summary": validation.summary,
            "raw_text_excerpt": (validation.raw_text or "")[:6000],
        }
        return (
            "Сформуй коротке призначення платежу українською для банківського платежу. "
            "Поверни тільки JSON з ключем payment_purpose. Формат: "
            "Оплата за {короткий предмет} згідно рахунку №{номер} від {дата_DD.MM.YYYY}. {ПДВ}. "
            "Дата у призначенні завжди тільки DD.MM.YYYY; ніколи не використовуй YYYY-MM-DD або текстовий місяць. "
            "Не копіюй табличні заголовки або службові поля: Код, Кількість, Кіл-ть, Од, Ціна, Сума, "
            "Артикул, Товари (роботи, послуги), послуги), Режим полімерізації, Тип нанесення. "
            "Не використовуй загальні фрази з summary на кшталт 'послуги, надані {постачальник} для {покупець}'. "
            "Якщо в таблиці є конкретний предмет, використовуй його: "
            "'Послуги на виставці, вивантаження, завантаження, зберігання ящиків' -> "
            "'Оплата за послуги на виставці, вивантаження, завантаження, зберігання ящиків згідно рахунку №120 від 07.07.2026.'; "
            "'Зарядний пристрій Oukitel P2001 PLUS' -> "
            "'Оплата за зарядний пристрій Oukitel P2001 PLUS згідно рахунку №11024 від 13.07.2026. у т.ч. ПДВ 25833,33 грн.'. "
            "Не вигадуй номер рахунку, дату, ПДВ або суму ПДВ. Якщо є ПДВ, сума ПДВ має точно збігатися з документом. "
            "Предмет має бути коротким: наприклад будівельні матеріали, комплектуючі, витратні матеріали.\n\n"
            f"Дані документа:\n{json.dumps(payload, ensure_ascii=False)}"
        )

    @classmethod
    def _build_with_ai(cls, validation: ReceiptValidationResult, fallback: str) -> str | None:
        settings = get_settings()
        if not settings.openai_api_key:
            return None
        if os.getenv("RECEIPT_PURPOSE_AI", "true").strip().lower() in {"0", "false", "no", "off"}:
            return None

        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {
                    "role": "system",
                    "content": "Ти бухгалтерський помічник. Генеруєш тільки безпечне призначення платежу, без табличного сміття.",
                },
                {"role": "user", "content": cls._build_ai_prompt(validation, fallback)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "payment_purpose_response",
                    "schema": {
                        "type": "object",
                        "properties": {"payment_purpose": {"type": "string"}},
                        "required": ["payment_purpose"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
            temperature=0,
        )
        content = getattr(response, "output_text", "") or ""
        if not content:
            return None
        parsed = json.loads(content)
        purpose = str(parsed.get("payment_purpose") or "").strip()
        finalized = cls._finalize(purpose, validation)
        if cls._is_acceptable_purpose(finalized, validation):
            return finalized
        return None

    @classmethod
    def build(cls, validation: ReceiptValidationResult) -> str:
        return cls._deterministic_build(validation)

    @classmethod
    def build_for_payment(cls, validation: ReceiptValidationResult) -> str:
        fallback = cls._deterministic_build(validation)
        try:
            ai_purpose = cls._build_with_ai(validation, fallback)
        except Exception:
            ai_purpose = None
        return ai_purpose or fallback
