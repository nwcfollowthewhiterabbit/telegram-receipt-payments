from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.config import Settings
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector
from src.db.models import Receipt, ReceiptStatus
from src import main as application


MODULE = "src.connectors.crm.terrasoft_mssql"
SECRET = "synthetic-password-sentinel"


def configuration(database="Terrasoft_test", *, dry_run=False, url=None):
    return SimpleNamespace(
        crm_provider="terrasoft_mssql",
        crm_dry_run=dry_run,
        terrasoft_database=database,
        terrasoft_mssql_url=url if url is not None else f"mssql+pymssql://test:{SECRET}@example.invalid/{database}",
        terrasoft_invoice_table="dbo.tbl_Cashflow",
        telegram_bot_token="test-no-network",
    )


def connector(settings):
    with patch(f"{MODULE}.get_settings", return_value=settings):
        return TerrasoftMssqlConnector()


def receipt():
    return Receipt(
        id=1,
        status=ReceiptStatus.validated,
        extracted_supplier_name="Synthetic Supplier",
        extracted_supplier_tax_id="12345678",
        extracted_amount=Decimal("12.34"),
        extracted_invoice_date="01.01.2026",
        extracted_invoice_number="TEST-1",
        original_filename="synthetic.pdf",
        validation_payload={},
    )


def fake_engine(actual_database="Terrasoft_test"):
    engine = Mock()
    connection = Mock()
    connection.execute.return_value.scalar_one.return_value = actual_database
    engine.connect.return_value.__enter__ = Mock(return_value=connection)
    engine.connect.return_value.__exit__ = Mock(return_value=False)
    engine.begin.return_value.__enter__ = Mock(return_value=connection)
    engine.begin.return_value.__exit__ = Mock(return_value=False)
    return engine, connection


class TerrasoftTargetSafetyTest(unittest.TestCase):
    def test_default_database_remains_test(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings().terrasoft_database, "Terrasoft_test")

    def test_verifies_test_and_production_with_only_read_only_sql(self):
        for database in ("Terrasoft_test", "Terrasoft"):
            with self.subTest(database=database):
                service = connector(configuration(database))
                engine, connection = fake_engine(database)
                with patch(f"{MODULE}.create_engine", return_value=engine) as create:
                    self.assertEqual(service.verify_connection_target(), database)
                self.assertEqual(create.call_args.args[0].database, database)
                self.assertEqual([str(call.args[0]) for call in connection.execute.call_args_list], ["SELECT DB_NAME()"])
                engine.begin.assert_not_called()
                engine.dispose.assert_called_once()

    def test_configured_database_allowlist_is_exact(self):
        for database in ("terrasoft", "TERRASOFT", "Terrasoft_test ", "master", "", "Terrasoft_other"):
            with self.subTest(database=database), patch(f"{MODULE}.create_engine") as create:
                with self.assertRaisesRegex(RuntimeError, "TERRASOFT_DATABASE"):
                    connector(configuration(database)).verify_connection_target()
                create.assert_not_called()

    def test_url_database_must_match_before_engine_or_business_work(self):
        for database, url_database in (("Terrasoft_test", "Terrasoft"), ("Terrasoft", "Terrasoft_test"), ("Terrasoft", "")):
            with self.subTest(database=database, url_database=url_database):
                service = connector(configuration(database, url=f"mssql+pymssql://test:{SECRET}@example.invalid/{url_database}"))
                with patch(f"{MODULE}.create_engine") as create:
                    with self.assertRaisesRegex(RuntimeError, "must match"):
                        service.sync_receipt(receipt(), None)
                    create.assert_not_called()

    def test_malformed_or_ambiguous_urls_fail_without_exposing_credentials(self):
        urls = (
            f"malformed-url-{SECRET}",
            f"mssql+pymssql://test:{SECRET}@example.invalid:bad/Terrasoft_test",
            f"mssql+pymssql://test:{SECRET}@example.invalid/Terrasoft_test?database=Terrasoft",
            f"mssql+pymssql://test:{SECRET}@example.invalid/Terrasoft_test?database=Terrasoft_test&database=Terrasoft",
            f"mssql+pymssql://test:{SECRET}@example.invalid/Terrasoft_test?odbc_connect=ambiguous",
            f"mssql+pymssql://test:{SECRET}@example.invalid/Terrasoft_test?charset=utf8",
            f"mssql+pyodbc://test:{SECRET}@example.invalid/Terrasoft_test",
            "mssql+pymssql:///Terrasoft_test",
            "",
        )
        for url in urls:
            with self.subTest(url_kind=urls.index(url)), patch(f"{MODULE}.create_engine") as create:
                with self.assertRaises(RuntimeError) as raised:
                    connector(configuration(url=url)).verify_connection_target()
                self.assertNotIn(SECRET, str(raised.exception))
                create.assert_not_called()

    def test_wrong_actual_database_stops_sync_before_business_queries(self):
        for actual_database in ("Terrasoft", "master", "terrasoft_test", None):
            with self.subTest(actual_database=actual_database):
                service = connector(configuration())
                service._find_account_by_supplier_code = Mock()
                service._next_cf_number = Mock()
                service._ensure_period_id = Mock()
                service._build_cashflow_insert = Mock()
                engine, connection = fake_engine(actual_database)
                with patch(f"{MODULE}.create_engine", return_value=engine):
                    with self.assertRaisesRegex(RuntimeError, "does not match"):
                        service.sync_receipt(receipt(), None)
                self.assertEqual([str(call.args[0]) for call in connection.execute.call_args_list], ["SELECT DB_NAME()"])
                service._find_account_by_supplier_code.assert_not_called()
                service._next_cf_number.assert_not_called()
                service._ensure_period_id.assert_not_called()
                service._build_cashflow_insert.assert_not_called()
                engine.dispose.assert_called_once()

    def test_matching_target_is_checked_before_account_number_period_and_insert(self):
        for database in ("Terrasoft_test", "Terrasoft"):
            with self.subTest(database=database):
                events = []
                service = connector(configuration(database))
                engine, connection = fake_engine(database)

                def execute(statement, *args):
                    events.append("verify" if str(statement) == "SELECT DB_NAME()" else "insert")
                    result = Mock()
                    result.scalar_one.return_value = database
                    return result

                connection.execute.side_effect = execute
                service._find_account_by_supplier_code = Mock(side_effect=lambda *args: events.append("account"))
                service._next_cf_number = Mock(side_effect=lambda *args: events.append("number") or "CF1")
                service._ensure_period_id = Mock(side_effect=lambda *args: events.append("period") or "synthetic-period")
                with patch(f"{MODULE}.create_engine", return_value=engine):
                    result = service.sync_receipt(receipt(), None)
                self.assertEqual(events, ["verify", "account", "number", "period", "insert"])
                self.assertEqual(result.status, "crm_synced")
                self.assertEqual(result.payload["database"], database)
                engine.dispose.assert_called_once()

    def test_database_verification_error_is_sanitized_and_disposes_engine(self):
        service = connector(configuration())
        engine, connection = fake_engine()
        connection.execute.side_effect = RuntimeError(f"driver failure with {SECRET}")
        with patch(f"{MODULE}.create_engine", return_value=engine):
            with self.assertRaises(RuntimeError) as raised:
                service.sync_receipt(receipt(), None)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertEqual(connection.execute.call_count, 1)
        engine.dispose.assert_called_once()

    def test_startup_connection_error_is_sanitized_and_disposes_engine(self):
        service = connector(configuration())
        engine, _ = fake_engine()
        engine.connect.side_effect = RuntimeError(f"connection failure with {SECRET}")
        with patch(f"{MODULE}.create_engine", return_value=engine):
            with self.assertRaises(RuntimeError) as raised:
                service.verify_connection_target()
        self.assertNotIn(SECRET, str(raised.exception))
        engine.dispose.assert_called_once()

    def test_engine_is_disposed_on_later_business_failure(self):
        service = connector(configuration())
        service._find_account_by_supplier_code = Mock(side_effect=RuntimeError("synthetic business failure"))
        engine, _ = fake_engine()
        with patch(f"{MODULE}.create_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "synthetic business failure"):
                service.sync_receipt(receipt(), None)
        engine.dispose.assert_called_once()

    def test_dry_run_skips_url_validation_and_external_connections(self):
        service = connector(configuration(dry_run=True, url="unconfigured"))
        with patch(f"{MODULE}.create_engine") as create, patch(f"{MODULE}.make_url") as parse:
            self.assertIsNone(service.verify_connection_target())
            self.assertEqual(service.sync_receipt(receipt(), None).status, "crm_dry_run")
        create.assert_not_called()
        parse.assert_not_called()


class TerrasoftStartupSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_target_failure_prevents_database_init_and_bank_polling(self):
        with patch.object(application, "get_settings", return_value=configuration()), \
             patch.object(application, "build_crm_connector") as build, \
             patch.object(application, "init_db") as init_db, \
             patch.object(application, "Bot") as bot, \
             patch.object(application, "Dispatcher") as dispatcher, \
             patch.object(application, "PaymentReceiptMonitor") as monitor:
            build.return_value.verify_connection_target.side_effect = RuntimeError("target mismatch")
            with self.assertRaisesRegex(RuntimeError, "target mismatch"):
                await application.main()
            init_db.assert_not_called()
            bot.assert_not_called()
            dispatcher.assert_not_called()
            monitor.assert_not_called()

    async def test_target_is_verified_before_every_startup_activity(self):
        events = []

        async def monitor_run():
            await asyncio.Event().wait()

        async def poll(*args):
            events.append("poll")

        with patch.object(application, "get_settings", return_value=configuration()), \
             patch.object(application, "build_crm_connector") as build, \
             patch.object(application, "init_db", side_effect=lambda: events.append("init_db")), \
             patch.object(application, "Bot", side_effect=lambda **kwargs: events.append("bot") or Mock()), \
             patch.object(application, "Dispatcher") as dispatcher, \
             patch.object(application, "register_handlers", side_effect=lambda *args: events.append("handlers")), \
             patch.object(application, "PaymentReceiptMonitor") as monitor:
            build.return_value.verify_connection_target.side_effect = lambda: events.append("verify")
            dispatcher.return_value.start_polling = AsyncMock(side_effect=poll)
            monitor.side_effect = lambda *args: events.append("monitor") or SimpleNamespace(run=monitor_run)
            await application.main()
        self.assertEqual(events, ["verify", "init_db", "bot", "handlers", "monitor", "poll"])

    async def test_disabled_or_dry_run_crm_never_builds_target_connection(self):
        for provider, dry_run in (("none", False), ("none", True), ("terrasoft_mssql", True)):
            with self.subTest(provider=provider, dry_run=dry_run):
                settings = configuration(dry_run=dry_run)
                settings.crm_provider = provider
                with patch.object(application, "get_settings", return_value=settings), \
                     patch.object(application, "build_crm_connector") as build, \
                     patch.object(application, "init_db"), \
                     patch.object(application, "Bot"), \
                     patch.object(application, "Dispatcher") as dispatcher, \
                     patch.object(application, "register_handlers"), \
                     patch.object(application, "PaymentReceiptMonitor") as monitor:
                    dispatcher.return_value.start_polling = AsyncMock()
                    monitor.return_value.run = AsyncMock()
                    await application.main()
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
