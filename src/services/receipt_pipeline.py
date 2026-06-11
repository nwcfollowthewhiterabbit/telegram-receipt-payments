from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from src.config import get_settings
from src.connectors.communication.base import StoredIncomingFile
from src.connectors.crm.registry import build_crm_connector
from src.connectors.payments.registry import build_payment_connector
from src.db.models import ActionType, PaymentDraft, Receipt, ReceiptStatus
from .audit import write_audit_log
from .document_text import DocumentTextExtractor
from .payment_draft_validation import PaymentDraftValidationService
from .payment_preflight import run_preflight
from .purpose_builder import PaymentPurposeBuilder
from .schemas import PaymentDraftValidationResult, ReceiptValidationResult
from .vision import ReceiptVisionService


class ReceiptPipeline:
    PAYMENT_PROVIDERS = ("privat24", "monobank")

    def __init__(self) -> None:
        self.settings = get_settings()
        self.vision = ReceiptVisionService()
        self.text_extractor = DocumentTextExtractor()
        self.payment_validator = PaymentDraftValidationService()
        self.crm_connector = build_crm_connector(self.settings)

    @staticmethod
    def _has_required_invoice_fields(validation: ReceiptValidationResult) -> bool:
        return bool(
            validation.readable
            and validation.supplier_name
            and validation.supplier_iban
            and validation.amount
            and validation.currency
        )

    @staticmethod
    def _document_number(receipt_id: int) -> str:
        return f"RCPT{receipt_id:04d}"

    def process_incoming_file(self, db: Session, incoming_file: StoredIncomingFile) -> Receipt:
        return self._process_saved_file(
            db=db,
            telegram_user_id=incoming_file.telegram_user_id,
            telegram_chat_id=incoming_file.telegram_chat_id,
            telegram_file_id=incoming_file.telegram_file_id,
            original_filename=incoming_file.original_filename,
            storage_path=incoming_file.storage_path,
            mime_type=incoming_file.mime_type,
        )

    def process_local_file(self, db: Session, file_path: str) -> Receipt:
        source = Path(file_path)
        extension = source.suffix.lower()
        if extension in {".jpg", ".jpeg", ".png", ".webp"}:
            mime_type = "image/jpeg"
        elif extension == ".pdf":
            mime_type = "application/pdf"
        elif extension == ".xls":
            mime_type = "application/vnd.ms-excel"
        elif extension == ".xlsx":
            mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise ValueError("unsupported_local_file_type")
        return self._process_saved_file(
            db=db,
            telegram_user_id=0,
            telegram_chat_id=0,
            telegram_file_id=source.name,
            original_filename=source.name,
            storage_path=source,
            mime_type=mime_type,
        )

    def _process_saved_file(
        self,
        db: Session,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_file_id: str,
        original_filename: str,
        storage_path: Path,
        mime_type: str,
    ) -> Receipt:
        existing = db.query(Receipt).filter(Receipt.telegram_file_id == telegram_file_id).first()
        if existing:
            return existing

        receipt = Receipt(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_file_id=telegram_file_id,
            original_filename=original_filename,
            storage_path=str(storage_path),
            mime_type=mime_type,
        )
        db.add(receipt)
        db.commit()
        db.refresh(receipt)

        write_audit_log(
            db,
            action_type=ActionType.receipt_uploaded,
            telegram_user_id=telegram_user_id,
            receipt_id=receipt.id,
            message="Source document uploaded",
            payload={"path": str(storage_path), "mime_type": mime_type},
        )

        validation = self._validate_saved_file(storage_path, mime_type, original_filename)
        receipt.validation_summary = validation.summary
        receipt.validation_payload = validation.model_dump(mode="json")
        receipt.extracted_supplier_name = validation.supplier_name
        receipt.extracted_supplier_tax_id = validation.supplier_tax_id
        receipt.extracted_supplier_iban = validation.supplier_iban
        receipt.extracted_supplier_bank_name = validation.supplier_bank_name
        receipt.extracted_supplier_mfo = validation.supplier_mfo
        receipt.extracted_invoice_number = validation.invoice_number
        receipt.extracted_invoice_date = validation.invoice_date
        receipt.extracted_amount = validation.amount
        receipt.extracted_currency = validation.currency or self.settings.default_currency
        receipt.status = ReceiptStatus.validated if validation.readable else ReceiptStatus.unreadable
        db.add(receipt)
        db.commit()
        db.refresh(receipt)

        write_audit_log(
            db,
            action_type=ActionType.validation_completed,
            telegram_user_id=telegram_user_id,
            receipt_id=receipt.id,
            message="Document validation completed",
            payload=validation.model_dump(mode="json"),
        )

        if not self._has_required_invoice_fields(validation):
            return receipt

        purpose = PaymentPurposeBuilder.build(validation)
        preflight = run_preflight(validation, purpose)
        receipt.extracted_supplier_name = preflight.normalized_supplier_name or receipt.extracted_supplier_name
        receipt.extracted_supplier_tax_id = preflight.normalized_supplier_tax_id or receipt.extracted_supplier_tax_id
        receipt.extracted_supplier_iban = preflight.normalized_supplier_iban or receipt.extracted_supplier_iban
        receipt.extracted_supplier_bank_name = preflight.normalized_supplier_bank_name or receipt.extracted_supplier_bank_name
        receipt.validation_payload = {
            **receipt.validation_payload,
            "payment_purpose_final": preflight.normalized_purpose,
            "preflight_errors": preflight.errors,
            "payment_ready": preflight.ok,
            "available_payment_providers": list(self.PAYMENT_PROVIDERS),
        }
        db.add(receipt)
        db.commit()
        db.refresh(receipt)

        if not preflight.ok:
            receipt.status = ReceiptStatus.requires_manual_review
            db.add(receipt)
            db.commit()
            write_audit_log(
                db,
                action_type=ActionType.preflight_failed,
                telegram_user_id=telegram_user_id,
                receipt_id=receipt.id,
                message="Payment preflight validation failed",
                payload={"errors": preflight.errors},
            )
            return receipt

        write_audit_log(
            db,
            action_type=ActionType.payment_draft_requested,
            telegram_user_id=telegram_user_id,
            receipt_id=receipt.id,
            message="Payment provider selection requested",
            payload={
                "purpose": preflight.normalized_purpose,
                "procurement_category": PaymentPurposeBuilder.infer_category(validation),
                "available_payment_providers": list(self.PAYMENT_PROVIDERS),
            },
        )
        return receipt

    def create_payment_draft_for_receipt(self, db: Session, receipt: Receipt, provider: str) -> Receipt:
        provider_name = provider.lower()
        if provider_name not in self.PAYMENT_PROVIDERS:
            raise ValueError("unsupported_payment_provider")
        if receipt.status in {ReceiptStatus.dry_run_created, ReceiptStatus.bank_created}:
            return receipt
        if not (receipt.validation_payload or {}).get("payment_ready"):
            raise ValueError("receipt_is_not_ready_for_payment")

        validation = ReceiptValidationResult(
            readable=True,
            summary=receipt.validation_summary or "",
            supplier_name=receipt.extracted_supplier_name,
            supplier_tax_id=receipt.extracted_supplier_tax_id,
            supplier_iban=receipt.extracted_supplier_iban,
            supplier_bank_name=receipt.extracted_supplier_bank_name,
            supplier_mfo=receipt.extracted_supplier_mfo,
            invoice_number=receipt.extracted_invoice_number,
            invoice_date=receipt.extracted_invoice_date,
            amount=receipt.extracted_amount,
            currency=receipt.extracted_currency,
            procurement_category=receipt.validation_payload.get("procurement_category"),
            payment_purpose=receipt.validation_payload.get("payment_purpose"),
            missing_fields=receipt.validation_payload.get("missing_fields") or [],
            raw_text=receipt.validation_payload.get("raw_text"),
        )
        purpose = receipt.validation_payload.get("payment_purpose_final") or PaymentPurposeBuilder.build(validation)
        preflight = run_preflight(validation, purpose)
        if not preflight.ok:
            receipt.status = ReceiptStatus.requires_manual_review
            receipt.validation_payload = {
                **(receipt.validation_payload or {}),
                "preflight_errors": preflight.errors,
                "payment_ready": False,
            }
            db.add(receipt)
            db.commit()
            write_audit_log(
                db,
                action_type=ActionType.preflight_failed,
                telegram_user_id=receipt.telegram_user_id,
                receipt_id=receipt.id,
                message="Payment preflight validation failed before provider draft",
                payload={"errors": preflight.errors, "payment_provider": provider_name},
            )
            return receipt

        payment_connector = build_payment_connector(self.settings, provider=provider_name)
        payment_dry_run = self.settings.payment_dry_run_for(provider_name)
        payment_payload = {
            "provider": provider_name,
            "document_number": self._document_number(receipt.id),
            "source_account": payment_connector.source_account(),
            "beneficiary_name": preflight.normalized_supplier_name,
            "beneficiary_tax_id": preflight.normalized_supplier_tax_id,
            "beneficiary_iban": preflight.normalized_supplier_iban,
            "beneficiary_bank_name": preflight.normalized_supplier_bank_name,
            "beneficiary_mfo": validation.supplier_mfo,
            "amount": str(validation.amount),
            "currency": validation.currency or self.settings.default_currency,
            "purpose": preflight.normalized_purpose,
        }
        try:
            payment_validation = self.payment_validator.validate(validation, payment_payload, provider_name)
        except Exception as exc:
            payment_validation = PaymentDraftValidationResult(
                ok=False,
                summary="Не вдалося виконати контрольну перевірку платежу.",
                errors=["payment_validation_error"],
                warnings=[str(exc)],
            )

        receipt.validation_payload = {
            **(receipt.validation_payload or {}),
            "payment_validation": payment_validation.model_dump(mode="json"),
        }
        db.add(receipt)
        db.commit()
        if not payment_validation.ok:
            receipt.status = ReceiptStatus.requires_manual_review
            receipt.validation_payload = {
                **(receipt.validation_payload or {}),
                "payment_ready": False,
            }
            db.add(receipt)
            db.commit()
            write_audit_log(
                db,
                action_type=ActionType.preflight_failed,
                telegram_user_id=receipt.telegram_user_id,
                receipt_id=receipt.id,
                message="Payment draft validation failed before bank request",
                payload={
                    "payment_provider": provider_name,
                    "validation": payment_validation.model_dump(mode="json"),
                },
            )
            return receipt

        write_audit_log(
            db,
            action_type=ActionType.validation_completed,
            telegram_user_id=receipt.telegram_user_id,
            receipt_id=receipt.id,
            message="Payment draft validation completed",
            payload={
                "payment_provider": provider_name,
                "validation": payment_validation.model_dump(mode="json"),
            },
        )
        write_audit_log(
            db,
            action_type=ActionType.payment_draft_requested,
            telegram_user_id=receipt.telegram_user_id,
            receipt_id=receipt.id,
            message="Payment draft creation requested",
            payload={
                "purpose": preflight.normalized_purpose,
                "procurement_category": PaymentPurposeBuilder.infer_category(validation),
                "payment_provider": provider_name,
                "execution_mode": "dry_run" if payment_dry_run else "live",
            },
        )
        try:
            draft_result = payment_connector.create_payment_draft(
                document_number=self._document_number(receipt.id),
                beneficiary_name=preflight.normalized_supplier_name,
                beneficiary_tax_id=preflight.normalized_supplier_tax_id,
                beneficiary_iban=preflight.normalized_supplier_iban,
                beneficiary_bank_name=preflight.normalized_supplier_bank_name,
                beneficiary_mfo=validation.supplier_mfo,
                amount=validation.amount,
                currency=validation.currency or self.settings.default_currency,
                purpose=preflight.normalized_purpose,
            )
        except Exception as exc:
            receipt.status = ReceiptStatus.payment_draft_failed
            receipt.validation_payload = {
                **(receipt.validation_payload or {}),
                "payment_create_error": str(exc),
                "payment_provider": provider_name,
                "execution_mode": "dry_run" if payment_dry_run else "live",
            }
            db.add(receipt)
            db.commit()
            write_audit_log(
                db,
                action_type=ActionType.payment_draft_failed,
                telegram_user_id=receipt.telegram_user_id,
                receipt_id=receipt.id,
                message="Payment draft creation failed",
                payload={
                    "error": str(exc),
                    "document_number": self._document_number(receipt.id),
                    "beneficiary_tax_id": preflight.normalized_supplier_tax_id,
                    "beneficiary_iban": preflight.normalized_supplier_iban,
                    "payment_provider": provider_name,
                },
            )
            return receipt

        payment_draft = PaymentDraft(
            receipt_id=receipt.id,
            provider_name=payment_connector.provider_name,
            provider_payment_id=draft_result.provider_payment_id,
            provider_pack_id=draft_result.provider_pack_id,
            source_account=payment_connector.source_account(),
            beneficiary_name=preflight.normalized_supplier_name,
            beneficiary_tax_id=preflight.normalized_supplier_tax_id,
            beneficiary_iban=preflight.normalized_supplier_iban,
            beneficiary_bank_name=preflight.normalized_supplier_bank_name,
            beneficiary_mfo=validation.supplier_mfo,
            amount=validation.amount,
            currency=validation.currency or self.settings.default_currency,
            purpose=preflight.normalized_purpose,
            status=draft_result.status,
            provider_payload=draft_result.payload,
        )
        db.add(payment_draft)
        receipt.validation_payload = {
            **(receipt.validation_payload or {}),
            "payment_provider": provider_name,
            "execution_mode": "dry_run" if payment_dry_run else "live",
        }
        if draft_result.created:
            receipt.status = ReceiptStatus.dry_run_created if payment_dry_run else ReceiptStatus.bank_created
        else:
            receipt.status = ReceiptStatus.payment_draft_failed
        db.add(receipt)
        db.commit()
        db.refresh(receipt)

        write_audit_log(
            db,
            action_type=ActionType.payment_draft_created if draft_result.created else ActionType.payment_draft_failed,
            telegram_user_id=receipt.telegram_user_id,
            receipt_id=receipt.id,
            message="Payment draft processed",
            payload=draft_result.model_dump(mode="json"),
        )
        self._sync_crm(db, receipt.telegram_user_id, receipt, payment_draft)
        return receipt

    def _sync_crm(
        self,
        db: Session,
        telegram_user_id: int,
        receipt: Receipt,
        payment_draft: PaymentDraft | None,
    ) -> None:
        try:
            crm_result = self.crm_connector.sync_receipt(receipt, payment_draft)
        except Exception as exc:
            receipt.validation_payload = {
                **(receipt.validation_payload or {}),
                "crm_sync_error": str(exc),
            }
            db.add(receipt)
            db.commit()
            write_audit_log(
                db,
                action_type=ActionType.payment_draft_failed,
                telegram_user_id=telegram_user_id,
                receipt_id=receipt.id,
                message="CRM sync failed",
                payload={"error": str(exc), "crm_provider": self.settings.crm_provider},
            )
            return

        receipt.validation_payload = {
            **(receipt.validation_payload or {}),
            "crm_provider": crm_result.provider_name,
            "crm_sync_status": crm_result.status,
            "crm_external_id": crm_result.external_id,
            "crm_recipient_found": crm_result.payload.get("recipient_found"),
            "crm_recipient_match": crm_result.payload.get("recipient_match"),
            "crm_cf_number": crm_result.payload.get("cf_number"),
        }
        db.add(receipt)
        db.commit()
        write_audit_log(
            db,
            action_type=ActionType.payment_draft_created,
            telegram_user_id=telegram_user_id,
            receipt_id=receipt.id,
            message="CRM sync processed",
            payload=crm_result.model_dump(mode="json"),
        )

    def _validate_saved_file(self, storage_path: Path, mime_type: str, original_filename: str) -> ReceiptValidationResult:
        extension = storage_path.suffix.lower()
        if mime_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp"}:
            return self.vision.validate_receipt(str(storage_path))

        extracted_text = self.text_extractor.extract(str(storage_path)).strip()
        if not extracted_text:
            return ReceiptValidationResult(
                readable=False,
                summary="Не вдалося витягти текст із документа.",
                missing_fields=["document_text"],
                raw_text="",
            )
        return self.vision.validate_text_document(extracted_text[:50000], original_filename)
