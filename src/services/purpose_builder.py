from __future__ import annotations

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

    @classmethod
    def build(cls, validation: ReceiptValidationResult) -> str:
        explicit_purpose = (validation.payment_purpose or "").strip()
        if explicit_purpose:
            return explicit_purpose

        category = cls.infer_category(validation)
        number = validation.invoice_number or "б/н"
        date = validation.invoice_date or "без дати"
        return f"Оплата за {category} згідно рахунку №{number} від {date}"
