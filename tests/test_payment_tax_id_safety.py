from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from src.clients.monobank import MonobankClient
from src.clients.privat24 import Privat24Client
from src.services.payment_draft_validation import PaymentDraftValidationService
from src.services.payment_preflight import (
    extract_company_tax_ids,
    extract_iban_candidates,
    repair_iban_from_raw_text,
    run_preflight,
)
from src.services.receipt_pipeline import ReceiptPipeline
from src.services.schemas import ReceiptValidationResult


VALID_IBAN = "UA413220010000026002350010497"


def receipt(tax_id: str | None, raw_text: str | None, *, name: str = "Фізична особа-підприємець Момот Марина Сергіївна", iban: str = VALID_IBAN) -> ReceiptValidationResult:
    # Identifier-focused tests also need explicit evidence of the supplier role.
    if raw_text and "Постачальник" not in raw_text:
        raw_text = f"Постачальник: {name}\n{raw_text}\nIBAN: {iban}"
    return ReceiptValidationResult(
        readable=True,
        summary="ok",
        supplier_name=name,
        supplier_tax_id=tax_id,
        supplier_iban=iban,
        supplier_bank_name='АТ "УНІВЕРСАЛ БАНК"',
        amount=Decimal("18980.00"),
        currency="UAH",
        raw_text=raw_text,
    )


class PaymentTaxIdSafetyTest(unittest.TestCase):
    def test_extracts_full_ten_digit_ipn_after_edrpou_label(self) -> None:
        self.assertEqual(
            extract_company_tax_ids("Код за ЄДРПОУ 3042309844, ІПН 3042309844"),
            ["3042309844"],
        )

    def test_preflight_keeps_full_ten_digit_tax_id(self) -> None:
        result = run_preflight(
            receipt("3042309844", "Код за ЄДРПОУ 3042309844, ІПН 3042309844"),
            "Оплата за спортивні товари зг-но рахунку №51 від 24.06.2026",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized_supplier_tax_id, "3042309844")
        self.assertNotIn("invalid_supplier_tax_id", result.errors)

    def test_preflight_repairs_truncated_tax_id_from_raw_text(self) -> None:
        result = run_preflight(
            receipt("30423098", "Код за ЄДРПОУ 3042309844, ІПН 3042309844"),
            "Оплата за спортивні товари зг-но рахунку №51 від 24.06.2026",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized_supplier_tax_id, "3042309844")

    def test_preflight_blocks_tax_id_conflict_with_raw_text(self) -> None:
        result = run_preflight(
            receipt("11111111", "Код за ЄДРПОУ 3042309844, ІПН 3042309844"),
            "Оплата за спортивні товари зг-но рахунку №51 від 24.06.2026",
        )

        self.assertFalse(result.ok)
        self.assertIn("supplier_tax_id_mismatch_raw_text", result.errors)

    def test_preflight_prefers_edrpou_when_ocr_returns_vat_number(self) -> None:
        result = run_preflight(
            receipt("340774225138", "код за ЄДРПОУ 34077427, ІПН 340774225138"),
            "Оплата за будівельні матеріали згідно рахунку №609 від 24.06.2026. у т.ч. ПДВ 8994,18 грн.",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized_supplier_tax_id, "34077427")

    def test_preflight_accepts_unlabeled_supplier_code_if_present_in_raw_text(self) -> None:
        raw_text = (
            'Постачальник ТОВ "ГЕКСТЕК"\n'
            "45790487\n"
            "UA773220010000026007700005176\n"
            'Одержувач ТОВ "РЕНТОЛЛ СЕРВІС" ЄДРПОУ 43105412'
        )
        result = run_preflight(
            receipt("45790487", raw_text, name='ТОВ "ГЕКСТЕК"', iban="UA773220010000026007700005176"),
            "Оплата за електронні компоненти згідно рахунку №632458 від 25.06.2026. у т.ч. ПДВ 380,00 грн.",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized_supplier_tax_id, "45790487")

    def test_preflight_repairs_short_iban_from_full_raw_text(self) -> None:
        raw_text = (
            'Платник ТОВ "РЕНТОЛЛ СЕРВIС"\n'
            "UA70 305299 00000 26000046803093\n"
            'Постачальник: Товариство з обмеженою відповідальністю "Авер-ТЕХ"\n'
            "21665318\n"
            "UA39 334851 00000 00002600710364"
        )
        result = run_preflight(
            ReceiptValidationResult(
                readable=True,
                summary="ok",
                supplier_name='Товариство з обмеженою відповідальністю "Авер-ТЕХ"',
                supplier_tax_id="21665318",
                supplier_iban="UA3933485100000002600710364",
                amount=Decimal("1050.98"),
                currency="UAH",
                raw_text=raw_text,
            ),
            "Оплата за плівку згідно рахунку АТ-00035974 від 24.06.2026. у т.ч. ПДВ 175,16 грн.",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized_supplier_iban, "UA393348510000000002600710364")

    def test_extract_iban_candidates_keeps_only_valid_ukrainian_ibans(self) -> None:
        self.assertEqual(
            extract_iban_candidates(
                "UA40 305299 00000 26006010218618\n"
                "UA403052990000026006010218\n"
                "UA233052990000026004302055651"
            ),
            ["UA403052990000026006010218618"],
        )

    def test_repairs_ocr_swapped_ukrainian_iban_check_digits(self) -> None:
        self.assertEqual(
            repair_iban_from_raw_text("UA393005280000026001455039188", "UA393005280000026001455039188"),
            "UA933005280000026001455039188",
        )

    def test_repairs_single_missing_zero_in_ukrainian_iban_account(self) -> None:
        self.assertEqual(
            repair_iban_from_raw_text("UA88305299000002602001029371", "UA88305299000002602001029371"),
            "UA883052990000026002001029371",
        )

    def test_does_not_repair_multiple_missing_iban_digits_by_guessing(self) -> None:
        self.assertEqual(
            repair_iban_from_raw_text("UA1335100500000260158052300", "UA1335100500000260158052300"),
            "UA1335100500000260158052300",
        )

    def test_text_pdf_validation_keeps_full_extracted_text_for_preflight(self) -> None:
        class TextExtractor:
            def extract(self, file_path: str) -> str:
                return (
                    "код за ЄДРПОУ 43654081, ІПН 436540826500\n"
                    "UA613052990000026000000114342\n"
                    "Сума ПДВ: 8 616,67"
                )

        class Vision:
            def validate_text_document(self, document_text: str, source_name: str) -> ReceiptValidationResult:
                return receipt("436540826500", "Сума ПДВ: 8 616,67")

        pipeline = ReceiptPipeline()
        pipeline.text_extractor = TextExtractor()
        pipeline.vision = Vision()

        result = pipeline._validate_saved_file(Path("invoice.pdf"), "application/pdf", "invoice.pdf")

        self.assertIn("код за ЄДРПОУ 43654081", result.raw_text or "")

    def test_deterministic_validation_blocks_final_tax_id_mismatch(self) -> None:
        service = PaymentDraftValidationService()
        result = service._validate_deterministic(
            receipt("3042309844", "Код за ЄДРПОУ 3042309844, ІПН 3042309844"),
            {
                "source_account": "UA703052990000026000046803093",
                "beneficiary_name": "Фізична особа-підприємець Момот Марина Сергіївна",
                "beneficiary_tax_id": "30423098",
                "beneficiary_iban": VALID_IBAN,
                "amount": "18980.00",
                "currency": "UAH",
                "purpose": "Оплата за спортивні товари зг-но рахунку №51 від 24.06.2026",
            },
            "privat24",
        )

        self.assertFalse(result.ok)
        self.assertIn("beneficiary_tax_id_mismatch", result.errors)
        self.assertIn("beneficiary_tax_id_mismatch_raw_text", result.errors)

    def test_deterministic_validation_allows_unlabeled_supplier_code_in_raw_text(self) -> None:
        service = PaymentDraftValidationService()
        raw_text = (
            'Постачальник ТОВ "ГЕКСТЕК"\n'
            "45790487\n"
            f"{VALID_IBAN}\n"
            'Одержувач ТОВ "РЕНТОЛЛ СЕРВІС" ЄДРПОУ 43105412'
        )
        result = service._validate_deterministic(
            receipt("45790487", raw_text, name='ТОВ "ГЕКСТЕК"'),
            {
                "source_account": "UA703052990000026000046803093",
                "beneficiary_name": 'ТОВ "ГЕКСТЕК"',
                "beneficiary_tax_id": "45790487",
                "beneficiary_iban": VALID_IBAN,
                "amount": "18980.00",
                "currency": "UAH",
                "purpose": "Оплата за електронні компоненти згідно рахунку №632458 від 25.06.2026.",
            },
            "privat24",
        )

        self.assertNotIn("beneficiary_tax_id_mismatch_raw_text", result.errors)

    def test_bank_clients_do_not_truncate_long_tax_ids(self) -> None:
        with self.assertRaises(RuntimeError):
            Privat24Client.normalize_nceo("304230984455")

        self.assertIsNone(MonobankClient.normalize_edrpou("304230984455"))

    def test_retry_file_id_fits_database_column(self) -> None:
        retry_file_id = ReceiptPipeline._retry_file_id("x" * 255)

        self.assertLessEqual(len(retry_file_id), 255)
        self.assertIn(":retry:", retry_file_id)


if __name__ == "__main__":
    unittest.main()
