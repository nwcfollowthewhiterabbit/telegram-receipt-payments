from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from src.db.models import Receipt, ReceiptStatus
from src.services.document_text import DocumentTextExtractor
from src.services.payment_draft_validation import PaymentDraftValidationService
from src.services.receipt_pipeline import ReceiptPipeline
from src.services.schemas import PaymentDraftValidationResult, ReceiptValidationResult
from src.services.vision import ReceiptVisionService
from src.services.supplier_identity import supplier_identity_errors


NAME = 'ТОВ "Тестовий перевізник"'
BUYER = 'ТОВ "Тестовий замовник"'
IBAN = "UA413220010000026002350010497"
SOURCE = (f'Постачальник: {NAME}\nЄДРПОУ: 12345678\nIBAN: {IBAN}\n'
          f'Одержувач послуг: {BUYER}\nЄДРПОУ: 87654321\n')


def validation(name=NAME):
    return ReceiptValidationResult(readable=True, summary="ok", supplier_name=name,
        supplier_tax_id="12345678", supplier_iban=IBAN, amount=Decimal("120.00"),
        currency="UAH", raw_text=SOURCE)


def payload(name=NAME):
    return dict(beneficiary_name=name, beneficiary_tax_id="12345678",
        beneficiary_iban=IBAN, amount="120.00", currency="UAH",
        purpose="Оплата за послуги згідно рахунку №TEST від 01.01.2026.")


class SupplierPipelineSafetyTest(unittest.TestCase):
    def test_semantic_success_cannot_override_mixed_party_tuple(self):
        service = PaymentDraftValidationService()
        service.client = Mock()
        service._validate_semantic = Mock(return_value=PaymentDraftValidationResult(ok=True, summary="ok"))
        result = service.validate(validation(BUYER), payload(BUYER), "monobank")
        self.assertFalse(result.ok)
        self.assertIn("supplier_name_mismatch_source", result.errors)
        service._validate_semantic.assert_not_called()

    def test_final_payload_name_changed_after_preflight_is_rejected(self):
        service = PaymentDraftValidationService()
        result = service._validate_deterministic(validation(), payload(BUYER), "monobank")
        self.assertFalse(result.ok)
        self.assertIn("beneficiary_name_mismatch", result.errors)

    def test_correct_tuple_reaches_additional_control(self):
        service = PaymentDraftValidationService()
        service.client = Mock()
        service._validate_semantic = Mock(return_value=PaymentDraftValidationResult(ok=True, summary="ok"))
        self.assertTrue(service.validate(validation(), payload(), "monobank").ok)
        service._validate_semantic.assert_called_once()

    def test_no_semantic_provider_does_not_authorize_bank_call(self):
        service = PaymentDraftValidationService()
        service.client = None
        result = service.validate(validation(), payload(), "monobank")
        self.assertFalse(result.ok)
        self.assertIn("semantic_validation_unavailable", result.errors)

    def test_semantic_errors_override_inconsistent_ok_flag(self):
        service = PaymentDraftValidationService()
        service.client = Mock()
        service._validate_semantic = Mock(return_value=PaymentDraftValidationResult(
            ok=True, summary="inconsistent", errors=["supplier_mismatch"]))
        self.assertFalse(service.validate(validation(), payload(), "monobank").ok)

    def test_incomplete_transcription_is_not_source_evidence(self):
        vision = ReceiptVisionService()
        vision.client = Mock()
        vision.client.responses.create.return_value = SimpleNamespace(status="incomplete", output_text=SOURCE)
        vision._image_payload = Mock(return_value={"type": "input_image", "image_url": "synthetic"})
        with self.assertRaisesRegex(RuntimeError, "source_transcription_incomplete"):
            vision.transcribe_receipt_images(["scan.png"])

    def _pending(self, name=BUYER):
        return Receipt(id=1, telegram_user_id=1, storage_path="original.pdf", mime_type="application/pdf",
            status=ReceiptStatus.validated, extracted_supplier_name=name,
            extracted_supplier_tax_id="12345678", extracted_supplier_iban=IBAN,
            extracted_amount=Decimal("120"), extracted_currency="UAH", validation_summary="ok",
            validation_payload={"payment_ready": True, "raw_text": "old model-generated text",
                                "payment_purpose_final": payload()["purpose"]})

    def test_legacy_pending_receipt_is_rechecked_before_connector(self):
        pipeline = ReceiptPipeline()
        pipeline._read_payment_source = Mock(return_value=SOURCE)
        receipt = self._pending()
        with patch("src.services.receipt_pipeline.build_payment_connector") as bank, \
             patch("src.services.receipt_pipeline.write_audit_log"):
            pipeline.create_payment_draft_for_receipt(Mock(), receipt, "monobank")
        bank.assert_not_called()
        self.assertEqual(receipt.status, ReceiptStatus.requires_manual_review)
        self.assertFalse(receipt.validation_payload["payment_ready"])
        self.assertEqual(receipt.validation_payload["raw_text"], SOURCE)

    def test_missing_original_blocks_even_previously_ready_receipt(self):
        pipeline = ReceiptPipeline()
        pipeline._read_payment_source = Mock(side_effect=FileNotFoundError())
        receipt = self._pending(NAME)
        with patch("src.services.receipt_pipeline.build_payment_connector") as bank, \
             patch("src.services.receipt_pipeline.write_audit_log"):
            pipeline.create_payment_draft_for_receipt(Mock(), receipt, "privat24")
        bank.assert_not_called()
        self.assertEqual(receipt.status, ReceiptStatus.requires_manual_review)

    def test_existing_draft_is_never_recreated(self):
        pipeline = ReceiptPipeline()
        pipeline._read_payment_source = Mock()
        receipt = self._pending()
        receipt.status = ReceiptStatus.bank_created
        with patch("src.services.receipt_pipeline.build_payment_connector") as bank:
            self.assertIs(pipeline.create_payment_draft_for_receipt(Mock(), receipt, "monobank"), receipt)
        bank.assert_not_called()
        pipeline._read_payment_source.assert_not_called()

    def test_scan_extraction_cannot_replace_independent_transcript(self):
        vision = ReceiptVisionService()
        vision.transcribe_receipt_images = Mock(return_value=SOURCE)
        vision.validate_text_document = Mock(return_value=validation(BUYER).model_copy(update={"raw_text": "invented"}))
        result = vision.validate_receipt_images(["scan.png"])
        self.assertEqual(result.raw_text, SOURCE)
        service = PaymentDraftValidationService()
        self.assertFalse(service._validate_deterministic(result, payload(BUYER), "monobank").ok)

    def test_pdf_uses_visual_reading_order(self):
        # Drawing order deliberately differs from the visual rows, as in
        # generated invoices. Plain extraction interleaves the two parties.
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})})})
        stream = DecodedStreamObject()
        draws = [(40,700,"Supplier:"), (180,500,"LLC Test Buyer"),
                 (180,700,"LLC Test Carrier"), (40,500,"Customer:"),
                 (180,680,"Tax ID: 12345678"), (180,660,f"IBAN: {IBAN}"),
                 (180,480,"Tax ID: 87654321")]
        stream.set_data("\n".join(f"BT /F1 10 Tf {x} {y} Td ({v}) Tj ET" for x,y,v in draws).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-invoice.pdf"
            writer.write(str(path))
            source = DocumentTextExtractor().extract(str(path))
        self.assertEqual(supplier_identity_errors("LLC Test Carrier", "12345678", IBAN, source), [])
        self.assertIn("supplier_name_mismatch_source",
            supplier_identity_errors("LLC Test Buyer", "12345678", IBAN, source))

    def test_oversized_scan_is_rejected_without_partial_rendering(self):
        document = Mock()
        document.__len__ = Mock(return_value=11)
        with patch("src.services.document_text.pdfium.PdfDocument", return_value=document):
            with self.assertRaisesRegex(ValueError, "source_document_too_many_pages"):
                DocumentTextExtractor().render_pdf_pages("source.pdf", "/unused")


if __name__ == "__main__":
    unittest.main()
