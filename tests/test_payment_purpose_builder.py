from __future__ import annotations

from decimal import Decimal
import unittest

from src.services.purpose_builder import PaymentPurposeBuilder
from src.services.schemas import ReceiptValidationResult


class PaymentPurposeBuilderTest(unittest.TestCase):
    def test_invoice_reference_is_not_used_as_whole_payment_purpose(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary='Документ є рахунком на оплату з ПДВ. Постачальник: ТОВ "Авер-ТЕХ".',
            supplier_name='Товариство з обмеженою відповідальністю "Авер-ТЕХ"',
            supplier_tax_id="21665318",
            supplier_iban="UA393348510000000002600710364",
            invoice_number="АТ-00035974",
            invoice_date="2026-06-24",
            amount=Decimal("1050.98"),
            currency="UAH",
            procurement_category="плівка",
            payment_purpose="АТ-00035974 від 24 червня 2026 р.",
            raw_text=(
                "Рахунок на оплату за замовленням № АТ-00035974 від 24 червня 2026\n"
                "Сума ПДВ: 175,16\n"
                "В призначенні платежу обов`язково вказати номер данного рахунку у форматі: "
                "АТ-00035974 від 24 червня 2026 р."
            ),
        )

        self.assertEqual(
            PaymentPurposeBuilder.build(validation),
            "Оплата за плівку згідно рахунку АТ-00035974 від 24.06.2026. у т.ч. ПДВ 175,16 грн.",
        )

    def test_vat_amount_with_thousands_separator_is_preserved(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary='Документ є рахунком на оплату будівельних матеріалів з ПДВ.',
            supplier_name='ТОВ "Постачальник"',
            supplier_tax_id='12345678',
            supplier_iban='UA123456789012345678901234567',
            invoice_number='222',
            invoice_date='2026-07-01',
            amount=Decimal('8627.22'),
            currency='UAH',
            procurement_category='будівельні матеріали',
            raw_text='Рахунок №222 від 01.07.2026. Сума ПДВ: 1,437.87. Всього: 8,627.22',
        )

        purpose = PaymentPurposeBuilder.build(validation)

        self.assertIn('у т.ч. ПДВ 1437,87 грн.', purpose)
        self.assertNotIn('ПДВ 1,44 грн', purpose)

    def test_table_header_noise_falls_back_to_category(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary='Документ є рахунком на оплату з ПДВ.',
            supplier_name='ТОВ "Постачальник"',
            supplier_tax_id='12345678',
            supplier_iban='UA123456789012345678901234567',
            invoice_number='226',
            invoice_date='2026-07-02',
            amount=Decimal('19872.00'),
            currency='UAH',
            procurement_category='витратні матеріали',
            raw_text=(
                'Товари (роботи, послуги) | Кількість | Од. вим | Ціна | Сума\n'
                'Разом з ПДВ. Сума ПДВ: 3312,00'
            ),
        )

        purpose = PaymentPurposeBuilder.build(validation)

        self.assertTrue(purpose.startswith('Оплата за витратні матеріали згідно рахунку №226'))
        self.assertIn('у т.ч. ПДВ 3312,00 грн.', purpose)
        self.assertNotIn('Кількість', purpose)
        self.assertNotIn('послуги)', purpose)
        self.assertNotIn('|', purpose)

    def test_inkos_summary_supplier_buyer_phrase_is_not_used_as_subject(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary=(
                "Документ є рахунком на оплату за послуги, надані ТОВ 'ІНКОС ГРУП' "
                "для ТОВ 'Рентолл Сервіс'."
            ),
            supplier_name='ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "ІНКОС ГРУП"',
            supplier_tax_id="43654081",
            supplier_iban="UA613052990000026000000114342",
            invoice_number="120",
            invoice_date="2026-07-07",
            amount=Decimal("174215.66"),
            currency="UAH",
            procurement_category="послуги",
            raw_text=(
                "№ Товари (роботи, послуги) Кіл-сть Од. Ціна з ПДВ Сума з ПДВ\n"
                "1 Послуги на виставці, вивантаження, завантаження, зберігання ящиків за\n"
                "межами України\n"
                "1 Послуга 174 215,66 174 215,66\n"
                "У тому числі ПДВ: 0,00"
            ),
        )

        purpose = PaymentPurposeBuilder.build(validation)

        self.assertEqual(
            purpose,
            "Оплата за послуги на виставці, вивантаження, завантаження, зберігання ящиків за межами України згідно рахунку №120 від 07.07.2026.",
        )
        self.assertNotIn("надані ТОВ", purpose)

    def test_text_month_and_iso_dates_are_canonicalized(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary="Документ є рахунком на оплату з ПДВ на постачання зарядних пристроїв.",
            supplier_name='Товариство з обмеженою відповідальністю "АЙ ОН"',
            supplier_tax_id="41258527",
            supplier_iban="UA443052990000026008040106215",
            invoice_number="11024",
            invoice_date="2026-07-13",
            amount=Decimal("155000.00"),
            currency="UAH",
            procurement_category="електротехнічне обладнання",
            payment_purpose="згідно рах №11024 , від 13 липня 2026",
            raw_text=(
                "В призначенні платежу, потрібно дослівно вказати \"згідно рах №11024 , від 13 липня 2026\".\n"
                "1.0 | P2001E PLUS | Зарядний пристрій Oukitel P2001 PLUS / 2400 Вт / 2048 Вт⋅ч / LiFePO4 | 5.0 | шт | 31000.0 | 155000.0\n"
                "У тому числі ПДВ: | 25833.33"
            ),
        )

        purpose = PaymentPurposeBuilder.build(validation)

        self.assertIn("від 13.07.2026", purpose)
        self.assertNotIn("2026-07-13", purpose)
        self.assertNotIn("13 липня 2026", purpose)
        self.assertIn("зарядний пристрій Oukitel P2001 PLUS", purpose)
        self.assertIn("у т.ч. ПДВ 25833,33 грн.", purpose)

    def test_existing_without_vat_suffix_is_not_duplicated(self) -> None:
        validation = ReceiptValidationResult(
            readable=True,
            summary="Документ є рахунком на оплату послуг без ПДВ.",
            supplier_name='ТОВ "Постачальник"',
            supplier_tax_id="12345678",
            supplier_iban="UA123456789012345678901234567",
            invoice_number="114",
            invoice_date="2026-07-01",
            amount=Decimal("6000.00"),
            currency="UAH",
            procurement_category="послуги",
            payment_purpose="Оплата за налаштування програмного забезпечення та консультації згідно рахунку №114 від 2026-07-01. без.",
            raw_text="Рахунок №114 від 01.07.2026. Разом без ПДВ",
        )

        purpose = PaymentPurposeBuilder.build(validation)

        self.assertEqual(
            purpose,
            "Оплата за налаштування програмного забезпечення та консультації згідно рахунку №114 від 01.07.2026.",
        )
        self.assertNotIn("без пдв", purpose.lower())


if __name__ == "__main__":
    unittest.main()
