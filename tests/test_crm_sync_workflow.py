from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import xml.etree.ElementTree as ET

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from src.connectors.crm.attachments import CrmAttachment, CrmConflict
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector
from src.connectors.payments.settlement import BankAttachment, ConfirmedPayment, SettlementEvidence
from src.db.base import Base
from src.db.models import AuditLog, AuthorizedUser, CrmOperation, PaymentDraft, Receipt, ReceiptStatus
from src.services.receipt_pipeline import ReceiptPipeline
from src.services.schemas import PaymentDraftResult, PaymentDraftValidationResult
from src.services.crm_sync import CrmSyncService, encode_evidence, enroll, error_code, transaction_key


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kwargs):
    # Exercise real local commits, rollback, unique indexes and JSON roundtrips.
    # PostgreSQL locking and MSSQL effects require the restore-clone gate.
    return "JSON"


def test_iban(number):
    bban = "300001" + f"{number:019d}"
    return f"UA{98 - int(bban + '301000') % 97:02d}{bban}"


class CrmSyncWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "invoice.pdf"
        self.source.write_bytes(b"synthetic-invoice")
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.settings = SimpleNamespace(
            crm_files_and_posting_enabled=True, crm_provider="terrasoft_mssql", crm_dry_run=False,
            payment_dry_run_for=Mock(return_value=False), terrasoft_database="Terrasoft_test",
            receipt_storage_dir=str(self.root), crm_attachment_max_bytes=1024 * 1024,
            crm_sync_poll_seconds=30, privat24_receipt_notify_phone=None,
        )
        self.connector = Mock()
        self.connector.prepare_operation.side_effect = lambda operation_id, receipt, draft: {
            "id": operation_id, "external_id": "receipt-paybot:" + operation_id,
            "provider": draft.provider_name, "amount": str(draft.amount),
        }
        self.connector.ensure_operation.return_value = {
            "cf_number": "CF-SYNTHETIC", "source_attachment": {"id": "source-file-id"}, "recipient_found": True,
        }
        self.connector.post_operation.return_value = {"status": "posted"}
        self.bank = Mock()
        self.bank.confirm_payment.return_value = None
        self.bank_factory = Mock(return_value=self.bank)
        self.service = CrmSyncService(settings=self.settings, session_factory=self.sessions,
                                      connector=self.connector, bank_factory=self.bank_factory)
        self.op_id = self.create_operation()

    def create_operation(self, *, provider="monobank", bank_id=None):
        with self.sessions() as db:
            receipt = Receipt(telegram_user_id=123, telegram_chat_id=456, telegram_file_id=str(uuid4()),
                              original_filename="invoice.pdf", storage_path=str(self.source), mime_type="application/pdf",
                              validation_payload={})
            db.add(receipt)
            db.flush()
            draft = PaymentDraft(receipt_id=receipt.id, provider_name=provider,
                                 provider_payment_id=bank_id or "synthetic-bank-" + str(receipt.id),
                                 source_account=test_iban(1), beneficiary_name="Synthetic Supplier",
                                 beneficiary_tax_id="12345678", beneficiary_iban=test_iban(2),
                                 amount=Decimal("123.45"), currency="UAH", purpose="Synthetic services",
                                 provider_payload={})
            db.add(draft)
            op = enroll(db, receipt, draft, self.settings)
            db.commit()
            return op.id

    def operation(self, op_id=None):
        with self.sessions() as db:
            op = db.get(CrmOperation, op_id or self.op_id)
            # Load relationships before detaching for concise assertions.
            _ = op.payment_draft.receipt
            return op

    def change_operation(self, **values):
        with self.sessions() as db:
            op = db.get(CrmOperation, self.op_id)
            for field, value in values.items():
                setattr(op, field, value)
            db.commit()

    def confirmation(self, *, op_id=None, transaction="synthetic-transaction", receipt=None):
        op = self.operation(op_id)
        draft = op.payment_draft
        evidence = SettlementEvidence(provider=draft.provider_name, bank_payment_id=draft.provider_payment_id,
            transaction_id=transaction, source_account=draft.source_account, beneficiary_iban=draft.beneficiary_iban,
            beneficiary_tax_id=draft.beneficiary_tax_id, amount=draft.amount, currency=draft.currency,
            paid_at=dt.datetime(2026, 11, 11, 9, 0, tzinfo=dt.timezone.utc), purpose=draft.purpose,
            document_number=f"RCPT{draft.receipt_id:04d}")
        return ConfirmedPayment(evidence, receipt, {"id": transaction, "amount": -12345, "synthetic": True})

    def waiting(self):
        self.service.prepare_and_create(self.op_id)
        self.assertEqual(self.operation().state, "waiting_payment")

    def confirmed(self):
        self.waiting()
        self.bank.confirm_payment.return_value = self.confirmation()
        self.assertTrue(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "confirmed")

    def fail_local_commit_once_after(self, callback):
        armed = {"fail": False}
        original_factory = self.sessions

        @contextmanager
        def factory():
            with original_factory() as db:
                real_commit = db.commit

                def commit():
                    if armed["fail"]:
                        armed["fail"] = False
                        raise OSError("synthetic lost local acknowledgement")
                    return real_commit()

                db.commit = commit
                yield db

        self.service.session_factory = factory

        def external_call(*args):
            result = callback(*args)
            armed["fail"] = True
            return result

        return external_call

    def test_enrollment_is_atomic_with_caller_and_unique_per_draft(self):
        original = self.operation()
        with self.sessions() as db:
            receipt = db.get(Receipt, original.payment_draft.receipt_id)
            draft = db.get(PaymentDraft, original.payment_draft_id)
            duplicate = enroll(db, receipt, draft, self.settings)
            with self.assertRaises(IntegrityError):
                db.commit()
            duplicate_id = duplicate.id
        with self.sessions() as db:
            self.assertIsNone(db.get(CrmOperation, duplicate_id))
            self.assertEqual(db.query(CrmOperation).count(), 1)
        with self.sessions() as db:
            receipt = Receipt(telegram_user_id=999, telegram_chat_id=999, telegram_file_id="rolled-back",
                              original_filename="invoice.pdf", storage_path=str(self.source), validation_payload={})
            db.add(receipt)
            db.flush()
            draft = PaymentDraft(receipt_id=receipt.id, provider_name="monobank", provider_payment_id="new-live",
                                 beneficiary_name="Synthetic", amount=Decimal("1"), purpose="Test")
            db.add(draft)
            pending = enroll(db, receipt, draft, self.settings)
            pending_id = pending.id
            db.rollback()
        with self.sessions() as db:
            self.assertIsNone(db.get(CrmOperation, pending_id))
            self.assertIsNone(db.query(Receipt).filter_by(telegram_file_id="rolled-back").first())

    def test_enrollment_refuses_missing_and_dry_run_bank_ids_even_in_live_settings(self):
        with self.sessions() as db:
            op = db.get(CrmOperation, self.op_id)
            for value in (None, "", "dry-run-123"):
                with self.subTest(bank_id=value):
                    op.payment_draft.provider_payment_id = value
                    with self.assertRaisesRegex(CrmConflict, "crm_live_bank_draft_required"):
                        enroll(db, op.payment_draft.receipt, op.payment_draft, self.settings)
        self.connector.assert_not_called()
        self.bank_factory.assert_not_called()

    def test_each_external_phase_follows_a_durable_local_state(self):
        self.assertTrue(self.service.step(self.op_id))
        prepared = self.operation()
        self.assertEqual(prepared.state, "prepared")
        self.assertEqual(prepared.payload["expected"]["id"], self.op_id)
        self.assertEqual(len(prepared.source_sha256), 64)
        self.connector.ensure_operation.assert_not_called()
        self.bank_factory.assert_not_called()
        self.assertTrue(self.service.step(self.op_id))
        waiting = self.operation()
        self.assertEqual(waiting.state, "waiting_payment")
        self.assertEqual(waiting.cf_number, "CF-SYNTHETIC")
        self.assertTrue(waiting.payment_draft.receipt.validation_payload["crm_source_attached"])
        self.bank_factory.assert_not_called()
        self.connector.post_operation.assert_not_called()
        self.assertFalse(self.service.step(self.op_id, creation_only=True))
        self.bank_factory.assert_not_called()

    def test_source_changed_after_prepare_stops_creation_until_original_restored(self):
        self.service.step(self.op_id)
        self.source.write_bytes(b"changed-invoice")
        self.assertFalse(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual((op.state, op.last_error, op.attempts), ("prepared", "source_file_changed", 1))
        self.connector.ensure_operation.assert_not_called()
        self.source.write_bytes(b"synthetic-invoice")
        self.assertTrue(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual((op.state, op.last_error, op.attempts), ("waiting_payment", None, 0))

    def test_lost_local_commit_after_external_create_reuses_same_operation(self):
        self.service.step(self.op_id)
        external = {}

        def ensure(expected, original):
            external.setdefault(expected["id"], {"cf_number": "CF-EXISTING", "source_attachment": {"sha": original.sha256}})
            return external[expected["id"]]

        self.connector.ensure_operation.side_effect = self.fail_local_commit_once_after(ensure)
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "prepared")
        self.connector.ensure_operation.side_effect = ensure
        self.assertTrue(self.service.step(self.op_id))
        self.assertEqual(self.operation().cf_number, "CF-EXISTING")
        self.assertEqual(list(external), [self.op_id])
        self.assertEqual(self.connector.prepare_operation.call_count, 1)
        self.bank_factory.assert_not_called()

    def test_pending_bank_never_posts_and_future_poll_is_scheduled(self):
        self.waiting()
        before = dt.datetime.utcnow()
        self.assertFalse(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual(op.state, "waiting_payment")
        self.assertIsNone(op.bank_transaction_key)
        self.assertGreaterEqual(op.next_attempt_at, before + dt.timedelta(seconds=30))
        self.connector.post_operation.assert_not_called()

    def test_mono_statement_is_honest_json_and_claim_commits_before_post(self):
        self.confirmed()
        op = self.operation()
        descriptor = op.payload["confirmation_file"]
        self.assertEqual(descriptor["role"], "bank-statement")
        self.assertEqual(descriptor["mime_type"], "application/json")
        self.assertTrue(descriptor["filename"].endswith("-statement.json"))
        payload = json.loads(Path(descriptor["path"]).read_bytes())
        self.assertEqual(payload["document_type"], "bank_statement_data")
        self.assertEqual(payload["bank_data"], self.bank.confirm_payment.return_value.bank_payload)
        self.assertEqual(op.bank_transaction_key, transaction_key(self.bank.confirm_payment.return_value.evidence))
        self.connector.post_operation.assert_not_called()

    def test_one_bank_transaction_cannot_be_claimed_by_two_operations(self):
        self.confirmed()
        first = self.operation()
        second_id = self.create_operation()
        self.service.prepare_and_create(second_id)
        self.bank.confirm_payment.return_value = self.confirmation(op_id=second_id)
        self.assertFalse(self.service.step(second_id))
        second = self.operation(second_id)
        self.assertEqual(second.state, "waiting_payment")
        self.assertEqual(second.last_error, "crm_bank_transaction_already_claimed")
        self.assertIsNone(second.bank_transaction_key)
        self.connector.post_operation.assert_not_called()
        with self.sessions() as db:
            db.get(CrmOperation, second_id).bank_transaction_key = first.bank_transaction_key
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_bank_claim_key_scopes_provider_account_and_transaction(self):
        evidence = self.confirmation().evidence
        for change in ({"provider": "privat24"}, {"source_account": test_iban(5)}, {"transaction_id": "another"}):
            self.assertNotEqual(transaction_key(evidence), transaction_key(replace(evidence, **change)))
        self.assertEqual(transaction_key(evidence), transaction_key(replace(evidence, amount=Decimal("99.99"))))

    def test_saved_evidence_is_reconfirmed_and_reversal_blocks_post(self):
        self.confirmed()
        self.bank.confirm_payment.return_value = None
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().last_error, "crm_confirmation_no_longer_final")
        self.assertEqual(self.operation().state, "confirmed")
        self.connector.post_operation.assert_not_called()

    def test_changed_fresh_bank_evidence_blocks_post(self):
        self.confirmed()
        confirmed = self.bank.confirm_payment.return_value
        for change in ({"transaction_id": "changed"}, {"amount": Decimal("1.00")}, {"purpose": "changed"},
                       {"paid_at": confirmed.evidence.paid_at + dt.timedelta(seconds=1)}):
            self.bank.confirm_payment.return_value = replace(confirmed, evidence=replace(confirmed.evidence, **change))
            with self.subTest(change=change):
                self.assertFalse(self.service.step(self.op_id))
                self.assertEqual(self.operation().last_error, "crm_confirmation_changed")
                self.connector.post_operation.assert_not_called()

    def test_changed_claim_or_confirmation_file_blocks_post(self):
        self.confirmed()
        key = self.operation().bank_transaction_key
        self.change_operation(bank_transaction_key="synthetic-wrong-claim")
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().last_error, "crm_bank_claim_mismatch")
        self.change_operation(bank_transaction_key=key)
        Path(self.operation().payload["confirmation_file"]["path"]).write_bytes(b"changed")
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().last_error, "source_file_changed")
        self.connector.post_operation.assert_not_called()

    def test_successful_post_is_terminal_and_publishes_same_operation(self):
        self.confirmed()
        self.assertTrue(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual(op.state, "posted")
        self.assertEqual(op.payment_draft.receipt.validation_payload["crm_external_id"], self.op_id)
        self.assertEqual(self.connector.post_operation.call_args.args[0], self.op_id)
        self.assertEqual(self.bank.confirm_payment.call_count, 2)
        self.assertFalse(self.service.step(self.op_id))
        self.service.run_cycle()
        self.assertEqual(self.bank.confirm_payment.call_count, 2)
        self.assertEqual(self.connector.post_operation.call_count, 1)

    def test_post_commit_loss_retries_same_id_and_claim_without_duplicate_external_effect(self):
        self.confirmed()
        external = {}

        def post(operation_id, expected, evidence, attachment):
            external.setdefault(operation_id, {"status": "posted", "transaction": evidence.transaction_id})
            return external[operation_id]

        self.connector.post_operation.side_effect = self.fail_local_commit_once_after(post)
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "confirmed")
        self.connector.post_operation.side_effect = post
        self.assertTrue(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "posted")
        self.assertEqual(list(external), [self.op_id])
        self.assertEqual(self.bank.confirm_payment.call_count, 3)

    def test_disabled_dry_and_other_provider_never_reach_bank_or_crm(self):
        configurations = ({"crm_files_and_posting_enabled": False}, {"crm_dry_run": True},
                          {"crm_provider": "none"}, {"payment_dry_run_for": Mock(return_value=True)})
        for values in configurations:
            originals = {key: getattr(self.settings, key) for key in values}
            for key, value in values.items():
                setattr(self.settings, key, value)
            with self.subTest(values=list(values)):
                self.assertFalse(self.service.step(self.op_id))
                self.connector.prepare_operation.assert_not_called()
                self.bank_factory.assert_not_called()
                self.assertEqual(self.operation().attempts, 0)
            for key, value in originals.items():
                setattr(self.settings, key, value)

    def test_wrong_target_version_and_dry_id_fail_before_any_external_work(self):
        cases = (("target_database", "Terrasoft", "crm_outbox_target_mismatch"),
                 ("payload", {"version": 1}, "crm_outbox_version_mismatch"))
        for field, value, reason in cases:
            original = getattr(self.operation(), field)
            self.change_operation(**{field: value})
            with self.subTest(field=field):
                self.assertFalse(self.service.step(self.op_id))
                self.assertEqual(self.operation().last_error, reason)
                self.connector.prepare_operation.assert_not_called()
                self.bank_factory.assert_not_called()
            self.change_operation(**{field: original})
        with self.sessions() as db:
            db.get(CrmOperation, self.op_id).payment_draft.provider_payment_id = "dry-run-synthetic"
            db.commit()
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual(self.operation().last_error, "crm_live_bank_draft_required")
        self.connector.prepare_operation.assert_not_called()

    def test_failure_is_sanitized_backed_off_and_same_reason_not_reaudited(self):
        sentinel = "synthetic-password-secret"
        self.connector.prepare_operation.side_effect = RuntimeError("connection failed: " + sentinel)
        before = dt.datetime.utcnow()
        with self.assertLogs("src.services.crm_sync", level="WARNING") as logs:
            self.assertFalse(self.service.step(self.op_id))
            self.assertFalse(self.service.step(self.op_id))
        self.assertNotIn(sentinel, " ".join(logs.output))
        op = self.operation()
        self.assertEqual((op.last_error, op.attempts), ("crm_dependency_error", 2))
        self.assertGreaterEqual(op.next_attempt_at, before + dt.timedelta(seconds=120))
        with self.sessions() as db:
            audits = db.query(AuditLog).all()
            self.assertEqual(len(audits), 1)
            self.assertNotIn(sentinel, json.dumps(audits[0].payload))

    def test_failure_to_record_retry_does_not_escape_or_expose_error(self):
        self.connector.prepare_operation.side_effect = OSError("synthetic-source-failure")
        with patch.object(self.service, "_record_failure", side_effect=OSError("synthetic-secret")):
            with self.assertLogs("src.services.crm_sync", level="WARNING") as logs:
                self.assertFalse(self.service.step(self.op_id))
        self.assertNotIn("synthetic-secret", " ".join(logs.output))

    def test_receipt_delivery_is_independent_of_posting_and_deduplicated_by_chat(self):
        self.op_id = self.create_operation(provider="privat24")
        self.waiting()
        receipt = BankAttachment("receipt.pdf", b"%PDF-1.4\nsynthetic", "application/pdf")
        self.bank.confirm_payment.return_value = self.confirmation(receipt=receipt)
        self.assertTrue(self.service.step(self.op_id))
        self.settings.privat24_receipt_notify_phone = "+000000000"
        with self.sessions() as db:
            db.add(AuthorizedUser(telegram_user_id=321, telegram_chat_id=456, phone_number="+000000000"))
            db.commit()
        jobs = self.service.pending_receipt_deliveries()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["chats"], [456])
        self.assertEqual(jobs[0]["attachment"].content, receipt.content)
        self.service.record_receipt_delivery(self.op_id, 456)
        self.assertEqual(self.service.pending_receipt_deliveries(), [])
        self.assertEqual(self.operation().state, "confirmed")
        self.assertEqual(self.bank.confirm_payment.call_count, 1)
        self.connector.post_operation.assert_not_called()
        draft = self.operation().payment_draft
        self.assertEqual(draft.status, "receipt_sent")
        self.assertIn("456", draft.provider_payload["receipt_delivery"]["sent_to"])

    def test_legacy_drafts_without_outbox_are_not_replayed(self):
        with self.sessions() as db:
            db.delete(db.get(CrmOperation, self.op_id))
            db.commit()
        self.service.run_cycle()
        self.connector.prepare_operation.assert_not_called()
        self.bank_factory.assert_not_called()

    def test_error_code_accepts_only_bounded_nonsensitive_reason_codes(self):
        self.assertEqual(error_code(CrmConflict("crm_attachment_conflict")), "crm_attachment_conflict")
        self.assertEqual(error_code(RuntimeError("monobank_invalid_statement")), "monobank_invalid_statement")
        self.assertEqual(error_code(RuntimeError("privat24_invalid_receipt")), "privat24_invalid_receipt")
        self.assertEqual(error_code(RuntimeError("crm_" + "a" * 136)), "crm_dependency_error")
        self.assertEqual(error_code(RuntimeError("crm_reason\npassword=secret")), "crm_dependency_error")
        evidence = self.confirmation().evidence
        self.assertEqual(encode_evidence(evidence)["amount"], "123.45")

    def pipeline_fixture(self, db, provider, returned_id):
        receipt = Receipt(telegram_user_id=123, telegram_chat_id=456, telegram_file_id=str(uuid4()),
            original_filename="invoice.pdf", storage_path=str(self.source), mime_type="application/pdf",
            status=ReceiptStatus.validated, extracted_supplier_name="Synthetic Supplier",
            extracted_supplier_tax_id="12345678", extracted_supplier_iban=test_iban(2),
            extracted_amount=Decimal("123.45"), extracted_currency="UAH",
            validation_payload={"payment_ready": True, "payment_purpose_final": "Synthetic purpose"})
        db.add(receipt)
        db.commit()
        pipeline = ReceiptPipeline.__new__(ReceiptPipeline)
        pipeline.settings = self.settings
        pipeline.payment_validator = Mock()
        pipeline.payment_validator.validate.return_value = PaymentDraftValidationResult(ok=True, summary="synthetic")
        pipeline._read_payment_source = Mock(return_value="synthetic independently tested source")
        pipeline._sync_crm = Mock()
        bank = Mock(provider_name=provider)
        bank.source_account.return_value = test_iban(1)
        bank.create_payment_draft.return_value = PaymentDraftResult(created=True, provider_payment_id=returned_id,
            provider_pack_id="synthetic-pack", status="draft_created_pending_signature",
            payload={"synthetic_bank_response": True, "payment_pack_ref": "synthetic-pack"})
        preflight = SimpleNamespace(ok=True, normalized_supplier_name="Synthetic Supplier",
            normalized_supplier_tax_id="12345678", normalized_supplier_iban=test_iban(2),
            normalized_supplier_bank_name="Synthetic Bank", normalized_purpose="Synthetic purpose")
        return pipeline, receipt, bank, preflight

    def test_pipeline_missing_bank_identity_persists_attempt_and_repeat_cannot_create_again(self):
        for provider, returned_id in (("privat24", None), ("monobank", ""), ("monobank", "dry-run-synthetic")):
            with self.subTest(provider=provider, returned_id=returned_id), self.sessions() as db:
                pipeline, receipt, bank, preflight = self.pipeline_fixture(db, provider, returned_id)
                with patch("src.services.receipt_pipeline.build_payment_connector", return_value=bank), \
                     patch("src.services.receipt_pipeline.run_preflight", return_value=preflight), \
                     patch("src.services.receipt_pipeline.CrmSyncService") as workflow:
                    result = pipeline.create_payment_draft_for_receipt(db, receipt, provider)
                    self.assertEqual(result.status, ReceiptStatus.bank_created)
                    self.assertEqual(result.validation_payload["crm_enhanced_state"], "manual_review")
                    self.assertEqual(result.validation_payload["crm_enhanced_error"], "crm_bank_draft_identity_missing")
                    saved = db.query(PaymentDraft).filter_by(receipt_id=receipt.id).one()
                    self.assertEqual(saved.provider_payment_id, returned_id)
                    self.assertEqual(saved.provider_payload["payment_pack_ref"], "synthetic-pack")
                    self.assertIsNone(db.query(CrmOperation).filter_by(payment_draft_id=saved.id).first())
                    pipeline.create_payment_draft_for_receipt(db, receipt, provider)
                    bank.create_payment_draft.assert_called_once()
                    workflow.assert_not_called()
                    pipeline._sync_crm.assert_not_called()

    def test_pipeline_valid_bank_draft_and_outbox_commit_before_external_crm_and_repeat_is_safe(self):
        for provider in ("privat24", "monobank"):
            with self.subTest(provider=provider), self.sessions() as db:
                pipeline, receipt, bank, preflight = self.pipeline_fixture(db, provider, "synthetic-created-bank-id")
                observed = []

                def inspect_durable_operation(op_id):
                    with self.sessions() as independent:
                        op = independent.get(CrmOperation, op_id)
                        self.assertIsNotNone(op)
                        self.assertEqual(op.state, "pending_create")
                        self.assertEqual(op.payload, {"version": 2})
                        self.assertEqual(op.payment_draft.receipt_id, receipt.id)
                        self.assertEqual(op.payment_draft.provider_payment_id, "synthetic-created-bank-id")
                        self.assertEqual(op.payment_draft.receipt.status, ReceiptStatus.bank_created)
                        self.assertEqual(op.payment_draft.receipt.validation_payload["crm_operation_id"], op_id)
                        observed.append(op_id)

                with patch("src.services.receipt_pipeline.build_payment_connector", return_value=bank), \
                     patch("src.services.receipt_pipeline.run_preflight", return_value=preflight), \
                     patch("src.services.receipt_pipeline.CrmSyncService") as workflow:
                    workflow.return_value.prepare_and_create.side_effect = inspect_durable_operation
                    pipeline.create_payment_draft_for_receipt(db, receipt, provider)
                    pipeline.create_payment_draft_for_receipt(db, receipt, provider)
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(db.query(PaymentDraft).filter_by(receipt_id=receipt.id).count(), 1)
                    bank.create_payment_draft.assert_called_once()
                    workflow.return_value.prepare_and_create.assert_called_once_with(observed[0])
                    pipeline._sync_crm.assert_not_called()


class EnhancedConnectorBoundaryTests(unittest.TestCase):
    MODULE = "src.connectors.crm.terrasoft_mssql"

    def setUp(self):
        self.op_id = str(uuid4())
        self.settings = SimpleNamespace(
            crm_files_and_posting_enabled=True, crm_dry_run=False,
            payment_dry_run_for=Mock(return_value=False), terrasoft_database="Terrasoft_test",
            terrasoft_mssql_url="mssql+pymssql://synthetic:synthetic@example.invalid/Terrasoft_test",
            terrasoft_invoice_table="dbo.tbl_Cashflow", crm_attachment_max_bytes=20 * 1024 * 1024,
        )
        with patch(f"{self.MODULE}.get_settings", return_value=self.settings):
            self.connector = TerrasoftMssqlConnector()
        self.expected = {"id": self.op_id, "external_key": "receipt-paybot:cashflow:" + self.op_id,
                         "amount": "123.45", "basic_amount": "123.45", "currency_rate": "1.0000",
                         "recipient_id": None, "owner_id": str(uuid4()), "estimated_date": "2026-11-11T00:00:00"}
        self.file = CrmAttachment("invoice", "invoice.pdf", b"synthetic", "application/pdf")
        self.receipt = Receipt(id=1, original_filename="invoice.pdf", validation_payload={})
        self.draft = PaymentDraft(provider_name="monobank", provider_payment_id="synthetic-bank", currency="UAH",
                                  amount=Decimal("123.45"), purpose="Synthetic", beneficiary_name="Synthetic")
        self.engine, self.connection = Mock(), Mock()
        self.engine.begin.return_value.__enter__ = Mock(return_value=self.connection)
        self.engine.begin.return_value.__exit__ = Mock(return_value=False)
        self.engine.connect.return_value.__enter__ = Mock(return_value=self.connection)
        self.engine.connect.return_value.__exit__ = Mock(return_value=False)
        self.connection.execute.return_value.scalar_one.return_value = "Terrasoft_test"
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "CodPrivat": self.expected["external_key"], "CFNumber": "CF-EXISTING"}

    def invoke(self, phase):
        if phase == "prepare":
            return self.connector.prepare_operation(self.op_id, self.receipt, self.draft)
        if phase == "ensure":
            return self.connector.ensure_operation(self.expected, self.file)
        return self.connector.post_operation(self.op_id, self.expected, Mock(), self.file)

    def test_actual_target_verified_before_business_sql_in_every_enhanced_phase(self):
        self.connection.execute.return_value.scalar_one.return_value = "Terrasoft"
        for phase in ("prepare", "ensure", "post"):
            self.connection.execute.reset_mock()
            self.engine.dispose.reset_mock()
            with self.subTest(phase=phase), patch(f"{self.MODULE}.create_engine", return_value=self.engine), \
                 patch.object(self.connector, "_attachment_policy") as policy, \
                 patch(f"{self.MODULE}.ensure_attachment") as attach, \
                 patch("src.connectors.crm.terrasoft_posting.PostingService") as posting:
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    self.invoke(phase)
                self.assertEqual([str(c.args[0]) for c in self.connection.execute.call_args_list], ["SELECT DB_NAME()"])
                policy.assert_not_called()
                attach.assert_not_called()
                posting.assert_not_called()
                self.engine.dispose.assert_called_once()

    def test_dry_disabled_or_url_mismatch_never_opens_enhanced_connection(self):
        for field, value in (("crm_dry_run", True), ("crm_files_and_posting_enabled", False),
                             ("terrasoft_database", "Terrasoft")):
            original = getattr(self.settings, field)
            setattr(self.settings, field, value)
            for phase in ("prepare", "ensure", "post"):
                with self.subTest(field=field, phase=phase), patch(f"{self.MODULE}.create_engine") as create:
                    with self.assertRaises(RuntimeError):
                        self.invoke(phase)
                    create.assert_not_called()
            setattr(self.settings, field, original)

    def test_existing_operation_reuses_number_and_source_on_same_transaction(self):
        with patch(f"{self.MODULE}.create_engine", return_value=self.engine), \
             patch.object(self.connector, "_attachment_policy", return_value=(str(uuid4()), 100)), \
             patch.object(self.connector, "_next_cf_number") as number, \
             patch.object(self.connector, "_ensure_period_id") as period, \
             patch(f"{self.MODULE}.ensure_attachment", return_value={"id": "same-file"}) as attach:
            result = self.invoke("ensure")
        self.assertEqual(result["cf_number"], "CF-EXISTING")
        number.assert_not_called()
        period.assert_not_called()
        self.assertIs(attach.call_args.args[0], self.connection)
        self.assertEqual(attach.call_args.args[1], self.op_id)
        self.engine.dispose.assert_called_once()

    def test_operation_owned_by_another_source_never_attaches(self):
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "CodPrivat": "manual-operation", "CFNumber": "CF-MANUAL"}
        with patch(f"{self.MODULE}.create_engine", return_value=self.engine), \
             patch.object(self.connector, "_attachment_policy", return_value=(str(uuid4()), 100)), \
             patch(f"{self.MODULE}.ensure_attachment") as attach:
            with self.assertRaisesRegex(CrmConflict, "crm_operation_owned_by_another_source"):
                self.invoke("ensure")
            attach.assert_not_called()
        self.assertIs(self.engine.begin.return_value.__exit__.call_args.args[0], CrmConflict)
        self.engine.dispose.assert_called_once()

    def test_attachment_failure_exits_posting_transaction_with_exception(self):
        with patch(f"{self.MODULE}.create_engine", return_value=self.engine), \
             patch.object(self.connector, "_attachment_policy", return_value=(str(uuid4()), 100)), \
             patch("src.connectors.crm.terrasoft_posting.PostingService") as posting, \
             patch(f"{self.MODULE}.ensure_attachment", side_effect=CrmConflict("crm_attachment_conflict")) as attach:
            posting.return_value.post.return_value = {"status": "posted"}
            with self.assertRaisesRegex(CrmConflict, "crm_attachment_conflict"):
                self.invoke("post")
            posting.assert_called_once_with(self.connection)
            self.assertIs(attach.call_args.args[0], self.connection)
        self.assertIs(self.engine.begin.return_value.__exit__.call_args.args[0], CrmConflict)
        self.engine.dispose.assert_called_once()

    def test_native_attachment_limit_is_stricter_of_local_and_native_policies(self):
        file_type = str(uuid4())
        metadata = ET.fromstring(f'<Items><Item Code="ft_File" ID="{file_type}"/></Items>')
        with patch(f"{self.MODULE}.native_xml", return_value=metadata):
            for native, expected in ((25, 20), (1, 1)):
                self.connection.execute.return_value.scalars.return_value.all.return_value = [native]
                self.assertEqual(self.connector._attachment_policy(self.connection), (file_type, expected * 1024 * 1024))
            for values in ([], [1, 2], [None], [0], [-1], ["20"]):
                self.connection.execute.return_value.scalars.return_value.all.return_value = values
                with self.subTest(values=values), self.assertRaisesRegex(CrmConflict, "crm_native_attachment_limit_invalid"):
                    self.connector._attachment_policy(self.connection)


if __name__ == "__main__":
    unittest.main()
