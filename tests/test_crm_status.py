from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Dispatcher
from sqlalchemy import Integer, JSON, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from src.bot import handlers
from src.db.models import Receipt, ReceiptStatus


class StatusBase(DeclarativeBase):
    pass


class StatusReceipt(StatusBase):
    __tablename__ = "status_receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer)
    telegram_chat_id: Mapped[int] = mapped_column(Integer)
    validation_payload: Mapped[dict] = mapped_column(JSON)


def receipt(state="waiting_payment", **payload):
    return Receipt(id=1, telegram_user_id=10, telegram_chat_id=100,
                   status=ReceiptStatus.bank_created,
                   extracted_supplier_name="Синтетичний постачальник",
                   extracted_supplier_tax_id="12345678",
                   extracted_amount=Decimal("12.34"), extracted_currency="UAH",
                   validation_payload={"crm_enhanced_state": state, "crm_cf_number": "CF123",
                                       "payment_provider": "monobank", "payment_purpose": "Синтетичний рахунок",
                                       "execution_mode": "live", **payload})


class CrmStatusRenderingTest(unittest.TestCase):
    def test_progress_never_labels_a_bank_draft_as_paid(self):
        for state in ("pending_create", "prepared", "waiting_payment"):
            with self.subTest(state=state):
                result = "\n".join(handlers._crm_status_lines(receipt(state)))
                self.assertNotIn("Операцію в CRM проведено", result)
                self.assertNotIn("Банк підтвердив виконання", result)
                self.assertNotIn("pending_create", result)
                self.assertNotIn("prepared", result)
        self.assertIn("Очікуємо підтвердження", "\n".join(handlers._crm_status_lines(receipt())))

    def test_source_attachment_is_claimed_only_when_recorded(self):
        for flag in (None, False, "true", True):
            with self.subTest(flag=flag):
                result = "\n".join(handlers._crm_status_lines(receipt(crm_source_attached=flag)))
                self.assertEqual("Оригінал рахунку додано" in result, flag is True)

    def test_confirmed_posting_error_and_posted_are_distinct(self):
        confirmed = "\n".join(handlers._crm_status_lines(receipt("confirmed")))
        deferred = "\n".join(handlers._crm_status_lines(receipt("confirmed", crm_enhanced_error="SQL_PASSWORD_PRIVATE")))
        posted = "\n".join(handlers._crm_status_lines(receipt("posted")))
        self.assertIn("Банк підтвердив виконання", confirmed)
        self.assertNotIn("Операцію в CRM проведено", confirmed)
        self.assertIn("завершення проведення в CRM ще не підтверджено", deferred)
        self.assertNotIn("SQL_PASSWORD_PRIVATE", deferred)
        self.assertIn("Операцію в CRM проведено", posted)
        self.assertIn("підтвердження оплати додано", posted)

    def test_mono_json_and_manual_pdf_are_explicit_without_fabricating_receipt(self):
        for state in ("waiting_payment", "confirmed", "posted"):
            result = "\n".join(handlers._crm_status_lines(receipt(state)))
            self.assertIn("JSON", result)
            self.assertIn("PDF-квитанцію за потреби додайте вручну", result)
        privat = "\n".join(handlers._crm_status_lines(receipt("posted", payment_provider="privat24")))
        self.assertNotIn("monobank", privat)
        self.assertNotIn("JSON", privat)

    def test_missing_legacy_or_unknown_state_does_not_claim_posting(self):
        for state in (None, "unexpected_private_value"):
            result = "\n".join(handlers._crm_status_lines(receipt(state)))
            self.assertNotIn("Операцію в CRM проведено", result)
            self.assertNotIn("unexpected_private_value", result)

    def test_result_contains_own_reference_and_status_command_only_when_enrolled(self):
        result = handlers._render_receipt_result(receipt(crm_source_attached=True))
        self.assertIn("Рахунок у боті: RCPT0001", result)
        self.assertIn("/crm_status RCPT0001", result)
        self.assertIn("Оригінал рахунку додано до CRM", result)
        self.assertNotIn("/crm_status", handlers._render_receipt_result(receipt(None)))

    def test_unsafe_external_number_and_raw_errors_are_never_rendered(self):
        result = "\n".join(handlers._crm_status_lines(receipt(
            "waiting_payment", crm_cf_number="PRIVATE_SERVER/path", crm_enhanced_error="PRIVATE_SECRET")))
        self.assertNotIn("PRIVATE", result)


class CrmStatusCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        StatusBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, autoflush=False)
        with self.sessions() as db:
            db.add_all([
                StatusReceipt(id=1, telegram_user_id=10, telegram_chat_id=100,
                              validation_payload=receipt().validation_payload),
                StatusReceipt(id=2, telegram_user_id=20, telegram_chat_id=100,
                              validation_payload=receipt("posted", crm_cf_number="CF222").validation_payload),
                StatusReceipt(id=3, telegram_user_id=10, telegram_chat_id=300,
                              validation_payload=receipt("posted", crm_cf_number="CF333").validation_payload),
            ])
            db.commit()
        self.statements = []
        event.listen(self.engine, "before_cursor_execute", self._record_sql)
        self.patches = [patch.object(handlers, "Receipt", StatusReceipt),
                        patch.object(handlers, "SessionLocal", self.sessions),
                        patch.object(handlers, "_is_allowed", return_value=True),
                        patch.object(handlers, "pipeline", Mock())]
        self.mocks = [value.start() for value in self.patches]

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.engine.dispose()

    def _record_sql(self, connection, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    async def run_command(self, text, user=10, chat=100):
        message = SimpleNamespace(text=text, from_user=SimpleNamespace(id=user) if user else None,
                                  chat=SimpleNamespace(id=chat), answer=AsyncMock())
        await handlers._handle_crm_status(message)
        return message.answer.call_args.args[0]

    async def test_owner_in_original_chat_reads_saved_state_without_business_actions(self):
        output = await self.run_command("/crm_status RCPT0001")
        self.assertIn("Збережений стан CRM", output)
        self.assertIn("RCPT0001", output)
        self.assertIn("CF123", output)
        self.assertTrue(self.statements)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in self.statements))
        self.assertEqual(self.mocks[-1].mock_calls, [])

    async def test_other_owner_other_chat_and_absent_id_have_same_response(self):
        outputs = [await self.run_command(f"/crm_status RCPT{id:04d}") for id in (2, 3, 999)]
        self.assertEqual(len(set(outputs)), 1)
        for output in outputs:
            self.assertNotIn("CF222", output)
            self.assertNotIn("CF333", output)
            self.assertNotIn("проведено", output)
        self.assertEqual(await self.run_command("/crm_status RCPT0001", chat=500), outputs[0])

    async def test_unauthorized_or_anonymous_sender_cannot_read_receipts(self):
        self.mocks[2].return_value = False
        self.assertIn("Доступ заборонено", await self.run_command("/crm_status RCPT0001"))
        self.mocks[2].return_value = True
        self.assertIn("Доступ заборонено", await self.run_command("/crm_status RCPT0001", user=None))
        self.assertFalse(self.statements)

    async def test_malformed_command_is_rejected_before_receipt_query(self):
        for command in ("/crm_status", "/crm_status 1", "/crm_status RCPT0", "/crm_status RCPT-1",
                        "/crm_status RCPT2147483648", "/crm_status RCPT0001 extra", "/crm_status RCPT1;DROP"):
            with self.subTest(command=command):
                self.assertIn("Вкажіть номер", await self.run_command(command))
        self.assertFalse(self.statements)

    async def test_database_error_does_not_expose_details(self):
        with patch.object(handlers, "SessionLocal", side_effect=RuntimeError("PRIVATE_PASSWORD_HOST")):
            output = await self.run_command("/crm_status RCPT0001")
        self.assertIn("Спробуйте пізніше", output)
        self.assertNotIn("PRIVATE", output)

    def test_command_is_registered_before_the_fallback(self):
        dispatcher = Dispatcher()
        handlers.register_handlers(dispatcher)
        callbacks = [handler.callback for handler in dispatcher.message.handlers]
        self.assertIn(handlers._handle_crm_status, callbacks)
        self.assertLess(callbacks.index(handlers._handle_crm_status), len(callbacks) - 1)


if __name__ == "__main__":
    unittest.main()
