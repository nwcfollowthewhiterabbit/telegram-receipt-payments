"""Opt-in local PostgreSQL integration; bank and CRM network calls stay mocked.

Set RECEIPTFLOW_TEST_DATABASE_URL to a disposable loopback PostgreSQL database
named receiptflow_test_<suffix>. Every test creates/drops only its own random
schema. Unset URL skips integration; an unsafe supplied URL fails before connect.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.connectors.payments.settlement import ConfirmedPayment, SettlementEvidence
from src.db.base import Base
from src.db.models import CrmOperation, PaymentDraft, Receipt
from src.services.crm_sync import CrmSyncService, enroll, transaction_key


def guarded_test_url(value):
    try:
        url = make_url(value)
        safe = (url.drivername == "postgresql+psycopg"
                and url.host in {"localhost", "127.0.0.1", "::1"}
                and url.database and len(url.database) <= 63
                and re.fullmatch(r"receiptflow_test_[a-z0-9_]+", url.database)
                and not url.query)
    except Exception:
        safe = False
    if not safe:
        raise RuntimeError("local_disposable_postgres_test_target_required") from None
    return url


class PostgresTestTargetGuardTests(unittest.TestCase):
    def test_only_explicit_local_disposable_targets_are_accepted(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            url = guarded_test_url(f"postgresql+psycopg://test:synthetic@{host}:55432/receiptflow_test_synthetic")
            self.assertEqual(url.database, "receiptflow_test_synthetic")

    def test_unsafe_targets_and_url_overrides_fail_without_credentials(self):
        for value in (
            "postgresql+psycopg://test:synthetic-secret@example.invalid/receiptflow_test_synthetic",
            "postgresql+psycopg://test:synthetic-secret@localhost/production",
            "postgresql+psycopg://test:synthetic-secret@localhost/postgres",
            "postgresql+psycopg://test:synthetic-secret@localhost/receiptflow_test_",
            "postgresql+psycopg://test:synthetic-secret@localhost/receiptflow_test_synthetic?host=elsewhere",
            "postgresql+psycopg://test:synthetic-secret@localhost/receiptflow_test_synthetic?options=-csearch_path=public",
            "postgresql+psycopg:///receiptflow_test_synthetic",
            "postgresql://test:synthetic-secret@localhost/receiptflow_test_synthetic",
            "synthetic-secret-not-a-url",
        ):
            with self.subTest(index=value.find("@")), self.assertRaisesRegex(
                RuntimeError, "^local_disposable_postgres_test_target_required$"
            ):
                guarded_test_url(value)


@unittest.skipUnless(os.environ.get("RECEIPTFLOW_TEST_DATABASE_URL"), "local disposable PostgreSQL not configured")
class PostgresCrmWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.url = guarded_test_url(os.environ["RECEIPTFLOW_TEST_DATABASE_URL"])
        self.raw_engine = create_engine(self.url, pool_pre_ping=True, hide_parameters=True,
            connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000 -c lock_timeout=3000"})
        self.addCleanup(self.raw_engine.dispose)
        with self.raw_engine.connect() as connection:
            actual = connection.execute(text("SELECT current_database()" )).scalar_one()
            self.assertEqual(actual, self.url.database, "actual test database mismatch")
        self.schema = "receiptflow_test_" + uuid4().hex
        with self.raw_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.drop_owned_schema)
        self.engine = self.raw_engine.execution_options(schema_translate_map={None: self.schema})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "invoice.pdf"
        self.source.write_bytes(b"synthetic-original")
        self.settings = SimpleNamespace(crm_files_and_posting_enabled=True, crm_provider="terrasoft_mssql",
            crm_dry_run=False, payment_dry_run_for=Mock(return_value=False), terrasoft_database="Terrasoft_test",
            receipt_storage_dir=self.tmp.name, crm_attachment_max_bytes=1024 * 1024, crm_sync_poll_seconds=30,
            privat24_receipt_notify_phone=None)
        self.connector = Mock()
        self.connector.prepare_operation.side_effect = lambda op_id, *_: {"id": op_id, "amount": "10.01"}
        self.connector.ensure_operation.return_value = {
            "cf_number": "CF-SYNTHETIC", "source_attachment": {"sha256": "synthetic"}, "recipient_found": True}
        self.connector.post_operation.return_value = {"posted": True}
        self.bank = Mock()
        self.bank.confirm_payment.return_value = None
        self.service = CrmSyncService(settings=self.settings, session_factory=self.sessions,
                                      connector=self.connector, bank_factory=Mock(return_value=self.bank))
        self.op_id = self.create_operation()

    def drop_owned_schema(self):
        # Schema is generated by this test, never supplied through configuration.
        assert re.fullmatch(r"receiptflow_test_[0-9a-f]{32}", self.schema)
        with self.raw_engine.begin() as connection:
            actual = connection.execute(text("SELECT current_database()")).scalar_one()
            if actual != self.url.database:
                raise RuntimeError("test_cleanup_database_mismatch")
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def add_draft(self, db):
        receipt = Receipt(telegram_user_id=100, telegram_chat_id=100, telegram_file_id=str(uuid4()),
            original_filename="invoice.pdf", storage_path=str(self.source), mime_type="application/pdf",
            validation_payload={})
        db.add(receipt)
        db.flush()
        draft = PaymentDraft(receipt_id=receipt.id, provider_name="monobank", provider_payment_id="synthetic-bank-" + str(receipt.id),
            source_account="synthetic-source", beneficiary_name="Synthetic Supplier", beneficiary_tax_id="12345678",
            beneficiary_iban="synthetic-beneficiary", amount=Decimal("10.01"), currency="UAH", purpose="Synthetic purpose")
        db.add(draft)
        return receipt, draft

    def create_operation(self):
        with self.sessions() as db:
            receipt, draft = self.add_draft(db)
            op = enroll(db, receipt, draft, self.settings)
            db.commit()
            return op.id

    def operation(self, operation_id=None):
        with self.sessions() as db:
            op = db.get(CrmOperation, operation_id or self.op_id)
            _ = op.payment_draft.receipt
            return op

    def confirmation(self, operation_id=None):
        op = self.operation(operation_id)
        draft = op.payment_draft
        evidence = SettlementEvidence(provider="monobank", bank_payment_id=draft.provider_payment_id,
            transaction_id="synthetic-transaction", source_account=draft.source_account,
            beneficiary_iban=draft.beneficiary_iban, beneficiary_tax_id=draft.beneficiary_tax_id,
            amount=draft.amount, currency="UAH", paid_at=dt.datetime(2026, 11, 11, 10, tzinfo=dt.timezone.utc),
            purpose=draft.purpose, document_number=f"RCPT{draft.receipt_id:04d}")
        return ConfirmedPayment(evidence, None, {"id": "synthetic-transaction", "amount": -1001})

    def test_jsonb_roundtrip_atomic_enrollment_and_unique_draft_foreign_key(self):
        with self.raw_engine.connect() as connection:
            kind = connection.execute(text("SELECT data_type FROM information_schema.columns "
                "WHERE table_schema=:schema AND table_name='crm_operations' AND column_name='payload'"),
                {"schema": self.schema}).scalar_one()
        self.assertEqual(kind, "jsonb")
        with self.sessions() as db:
            receipt, draft = self.add_draft(db)
            op = enroll(db, receipt, draft, self.settings)
            db.flush()
            rolled_back_id = op.id
            db.rollback()
        with self.sessions() as db:
            self.assertIsNone(db.get(CrmOperation, rolled_back_id))
            self.assertEqual(db.query(PaymentDraft).count(), 1)
            original = db.get(CrmOperation, self.op_id)
            enroll(db, original.payment_draft.receipt, original.payment_draft, self.settings)
            with self.assertRaises(IntegrityError) as duplicate:
                db.commit()
            self.assertEqual(duplicate.exception.orig.diag.constraint_name, "crm_operations_payment_draft_id_key")
        with self.sessions() as db:
            op = db.get(CrmOperation, self.op_id)
            db.delete(op.payment_draft)
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_racing_transaction_claims_have_one_winner_at_database_constraint(self):
        second_id = self.create_operation()
        key = transaction_key(self.confirmation().evidence)
        contender_started = Event()

        def contender():
            with self.sessions() as db:
                db.get(CrmOperation, second_id).bank_transaction_key = key
                contender_started.set()
                try:
                    db.commit()
                except IntegrityError as exc:
                    return exc.orig.diag.constraint_name
                return "unexpected-second-winner"

        with self.sessions() as winner:
            winner.get(CrmOperation, self.op_id).bank_transaction_key = key
            winner.flush()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(contender)
                self.assertTrue(contender_started.wait(2))
                winner.commit()
                self.assertEqual(pending.result(timeout=5), "crm_operations_bank_transaction_key_key")
        self.assertEqual(self.operation().bank_transaction_key, key)
        self.assertIsNone(self.operation(second_id).bank_transaction_key)

    def test_locked_operation_is_skipped_before_external_calls(self):
        with self.sessions() as lock_owner:
            lock_owner.query(CrmOperation).filter_by(id=self.op_id).with_for_update().one()
            self.assertFalse(self.service.step(self.op_id))
            self.connector.prepare_operation.assert_not_called()
            self.assertEqual(self.operation().attempts, 0)
            lock_owner.rollback()
        self.assertTrue(self.service.step(self.op_id))
        self.connector.prepare_operation.assert_called_once()

    def test_parallel_monitors_do_not_execute_same_phase_twice(self):
        entered, release = Event(), Event()

        def prepare(op_id, *_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic-concurrency-timeout")
            return {"id": op_id, "amount": "10.01"}

        self.connector.prepare_operation.side_effect = prepare
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.service.step, self.op_id)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.service.step(self.op_id))
                self.assertEqual(self.connector.prepare_operation.call_count, 1)
            finally:
                release.set()
            self.assertTrue(pending.result(timeout=5))
        self.assertEqual(self.operation().state, "prepared")

    def test_full_outbox_retry_and_paid_confirmation_survive_real_commits(self):
        self.assertTrue(self.service.step(self.op_id))
        self.connector.ensure_operation.side_effect = OSError("synthetic-external-unavailable")
        self.assertFalse(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual((op.state, op.attempts, op.last_error), ("prepared", 1, "crm_dependency_error"))
        self.connector.ensure_operation.side_effect = None
        self.assertTrue(self.service.step(self.op_id))
        self.assertFalse(self.service.step(self.op_id))
        self.connector.post_operation.assert_not_called()
        self.bank.confirm_payment.return_value = self.confirmation()
        self.assertTrue(self.service.step(self.op_id))
        op = self.operation()
        self.assertEqual(op.state, "confirmed")
        self.assertEqual(op.payload["evidence"]["amount"], "10.01")
        self.assertEqual(op.bank_transaction_key, transaction_key(self.bank.confirm_payment.return_value.evidence))
        self.assertTrue(Path(op.payload["confirmation_file"]["path"]).is_file())
        self.assertTrue(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "posted")
        self.assertFalse(self.service.step(self.op_id))
        self.connector.post_operation.assert_called_once()

    def test_claim_conflict_is_saved_and_other_operation_stays_unposted(self):
        self.service.prepare_and_create(self.op_id)
        self.bank.confirm_payment.return_value = self.confirmation()
        self.assertTrue(self.service.step(self.op_id))
        second_id = self.create_operation()
        self.service.prepare_and_create(second_id)
        self.bank.confirm_payment.return_value = self.confirmation(second_id)
        self.assertFalse(self.service.step(second_id))
        second = self.operation(second_id)
        self.assertEqual((second.state, second.last_error), ("waiting_payment", "crm_bank_transaction_already_claimed"))
        self.assertIsNone(second.bank_transaction_key)
        self.connector.post_operation.assert_not_called()

    def test_lost_commit_after_post_preserves_claim_and_retries_fixed_operation(self):
        self.service.prepare_and_create(self.op_id)
        self.bank.confirm_payment.return_value = self.confirmation()
        self.service.step(self.op_id)
        external_posted = set()
        fault = {"armed": False, "enabled": True}

        def post(operation_id, *_):
            external_posted.add(operation_id)
            fault["armed"] = fault["enabled"]
            return {"posted": True}

        @contextmanager
        def factory():
            with self.sessions() as db:
                original_commit = db.commit

                def commit():
                    if fault["armed"]:
                        fault["armed"] = False
                        raise OSError("synthetic-local-commit-loss")
                    original_commit()

                db.commit = commit
                yield db

        self.connector.post_operation.side_effect = post
        self.service.session_factory = factory
        key = self.operation().bank_transaction_key
        self.assertFalse(self.service.step(self.op_id))
        self.assertEqual((self.operation().state, self.operation().bank_transaction_key), ("confirmed", key))
        fault["enabled"] = False
        self.assertTrue(self.service.step(self.op_id))
        self.assertEqual(self.operation().state, "posted")
        self.assertEqual(external_posted, {self.op_id})
        self.assertEqual(self.connector.post_operation.call_count, 2)


if __name__ == "__main__":
    unittest.main()
