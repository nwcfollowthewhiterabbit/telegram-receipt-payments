from __future__ import annotations

import datetime as dt
import io
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfWriter

from src.clients.monobank import MonobankClient
from src.clients.privat24 import Privat24Client
from src.connectors.payments.monobank_settlement import MonobankSettlementConnector
from src.connectors.payments.privat24_settlement import Privat24SettlementConnector
from src.connectors.payments.settlement import SettlementError, build_settlement_connector


def synthetic_iban(number):
    bban = f"300001{number:019d}"
    check = 98 - int(bban + "301000") % 97
    return f"UA{check:02d}{bban}"


SOURCE, BENEFICIARY, OTHER = [synthetic_iban(number) for number in (1, 2, 3)]
NOW = dt.datetime(2026, 11, 11, 12, tzinfo=dt.timezone.utc)
PAID = NOW - dt.timedelta(hours=1)
DOCUMENT = "RCPT0001"
PURPOSE = "Оплата за синтетичним тестовим рахунком № TEST-1."


def settings(dry_run=False):
    return SimpleNamespace(
        payment_dry_run_for=lambda provider: dry_run,
        privat24_dry_run=False,
        monobank_dry_run=False,
        privat24_api_token="synthetic-token",
        privat24_api_base_url="https://bank.invalid",
        privat24_source_account=SOURCE,
        privat24_payment_date="",
        privat24_payment_accept_date="",
        monobank_api_token="synthetic-token",
        monobank_api_base_url="https://bank.invalid",
        monobank_source_iban=SOURCE,
    )


def draft(provider):
    return SimpleNamespace(
        id=1, receipt_id=1, provider_name=provider, provider_payment_id="synthetic-payment-id",
        provider_pack_id="SYNTHETIC-PACKED-REF", source_account=SOURCE,
        beneficiary_name='ТОВ "Синтетичний постач"', beneficiary_tax_id="12345678",
        beneficiary_iban=BENEFICIARY, amount=Decimal("12.34"), currency="UAH", purpose=PURPOSE,
        created_at=(NOW - dt.timedelta(days=2)).replace(tzinfo=None),
        provider_payload={"creation_context": {"version": 1, "source_account": SOURCE, "document_number": DOCUMENT}},
    )


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def privat_payment():
    item = draft("privat24")
    return {
        "payment_ref": item.provider_payment_id, "id": item.provider_payment_id,
        "payment_pack_ref": item.provider_pack_id, "document_number": DOCUMENT,
        "payer_account": SOURCE, "recipient_account": BENEFICIARY,
        "recipient_nceo": item.beneficiary_tax_id, "payment_naming": item.beneficiary_name,
        "payment_amount": "12.34", "payment_ccy": "UAH", "payment_destination": PURPOSE,
        "payment_status": "sent",
    }


def privat_statement():
    item = draft("privat24")
    local = PAID.astimezone(ZoneInfo("Europe/Kyiv"))
    return {
        "DLR": item.provider_pack_id, "REF": "SYNTHETIC-TRANSACTION", "REFN": "1",
        "PR_PR": "r", "FL_REAL": "r", "TRANTYPE": "D", "NUM_DOC": DOCUMENT,
        "AUT_MY_ACC": SOURCE, "AUT_CNTR_ACC": BENEFICIARY, "AUT_CNTR_CRF": item.beneficiary_tax_id,
        "AUT_CNTR_NAM": item.beneficiary_name, "SUM": "12.34", "CCY": "UAH", "OSND": PURPOSE,
        "DAT_OD": local.strftime("%d.%m.%Y"), "TIM_P": local.strftime("%H:%M"),
        "DATE_TIME_DAT_OD_TIM_P": local.strftime("%d.%m.%Y %H:%M:%S"),
    }


def mono_statement():
    item = draft("monobank")
    return {
        "id": item.provider_payment_id, "externalReference": DOCUMENT,
        "time": int((NOW - dt.timedelta(days=1)).timestamp()), "completedTime": int(PAID.timestamp()),
        "amount": -1234, "currencyCode": "980", "status": "DONE", "reverse": False,
        "counterIban": BENEFICIARY, "counterEdrpou": item.beneficiary_tax_id,
        "counterName": item.beneficiary_name, "description": PURPOSE, "receiptId": "SYNTHETIC-CHECK-CODE",
    }


def privat_connector():
    client = Mock(spec=Privat24Client)
    client.get_payment.return_value = privat_payment()
    client.get_transactions.return_value = [privat_statement()]
    client.print_receipt.return_value = ("ignored-bank-name.pdf", pdf())
    return Privat24SettlementConnector(settings(), client, now=lambda: NOW), client


def mono_connector():
    client = Mock(spec=MonobankClient)
    client.get_payment_state.return_value = {"id": draft("monobank").provider_payment_id, "state": "IN_STATEMENT"}
    client.get_statement_item.return_value = mono_statement()
    client.get_statement.return_value = [mono_statement()]
    return MonobankSettlementConnector(settings(), client, now=lambda: NOW), client


class Privat24SettlementTest(unittest.TestCase):
    def test_exact_posted_debit_returns_valid_receipt_and_aware_evidence(self):
        connector, client = privat_connector()
        result = connector.confirm_payment(draft("privat24"), DOCUMENT)
        self.assertEqual(result.evidence.transaction_id, "SYNTHETIC-TRANSACTION:1")
        self.assertEqual(result.evidence.amount, Decimal("12.34"))
        self.assertEqual(result.evidence.paid_at, PAID)
        self.assertEqual(result.evidence.source_account, SOURCE)
        self.assertEqual(result.receipt.mime_type, "application/pdf")
        self.assertEqual(client.get_transactions.call_args.kwargs["account"], SOURCE)
        client.print_receipt.assert_called_once_with(SOURCE, "SYNTHETIC-TRANSACTION", "1", per_page=1)
        with self.assertRaises(FrozenInstanceError):
            result.evidence.amount = Decimal("99.00")

    def test_pending_reversed_rejected_or_non_real_statement_is_not_paid(self):
        for field, value in [("PR_PR", "p"), ("PR_PR", "t"), ("PR_PR", "n"), ("PR_PR", "success"), ("FL_REAL", "i"), ("FL_REAL", None)]:
            with self.subTest(field=field, value=value):
                connector, client = privat_connector()
                client.get_transactions.return_value[0][field] = value
                self.assertIsNone(connector.confirm_payment(draft("privat24"), DOCUMENT))
                client.print_receipt.assert_not_called()

    def test_similar_amount_and_payee_without_exact_packed_ref_are_not_matched(self):
        connector, client = privat_connector()
        client.get_transactions.return_value[0]["DLR"] = "OTHER-PAYMENT"
        self.assertIsNone(connector.confirm_payment(draft("privat24"), DOCUMENT))
        client.print_receipt.assert_not_called()

    def test_multiple_or_duplicate_candidates_are_never_first_matched(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                connector, client = privat_connector()
                extra = privat_statement()
                if not duplicate:
                    extra["REF"] = "ANOTHER-TRANSACTION"
                client.get_transactions.return_value.append(extra)
                with self.assertRaisesRegex(SettlementError, "ambiguous"):
                    connector.confirm_payment(draft("privat24"), DOCUMENT)
                client.print_receipt.assert_not_called()

    def test_every_business_field_is_required_and_must_match(self):
        changes = {
            "NUM_DOC": "RCPT9999", "AUT_MY_ACC": OTHER, "AUT_CNTR_ACC": OTHER,
            "AUT_CNTR_CRF": "87654321", "AUT_CNTR_NAM": "Different supplier",
            "SUM": "12.35", "CCY": "USD", "OSND": "Another payment purpose",
            "TRANTYPE": "C", "REF": "", "REFN": "not-a-number",
        }
        for field, value in changes.items():
            for replacement in (value, None):
                with self.subTest(field=field, missing=replacement is None):
                    connector, client = privat_connector()
                    client.get_transactions.return_value[0][field] = replacement
                    with self.assertRaises(SettlementError):
                        connector.confirm_payment(draft("privat24"), DOCUMENT)
                    client.print_receipt.assert_not_called()

    def test_changed_bank_draft_is_rejected_before_statement_lookup(self):
        changes = {
            "payment_ref": "other-id", "id": "other-id", "payment_pack_ref": "other-pack",
            "document_number": "RCPT9999", "payer_account": OTHER, "recipient_account": OTHER,
            "recipient_nceo": "87654321", "payment_naming": "Another supplier",
            "payment_amount": "12.35", "payment_ccy": "USD", "payment_destination": "Other purpose",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                connector, client = privat_connector()
                client.get_payment.return_value[field] = value
                with self.assertRaises(SettlementError):
                    connector.confirm_payment(draft("privat24"), DOCUMENT)
                client.get_transactions.assert_not_called()
                client.print_receipt.assert_not_called()

    def test_payment_status_success_does_not_replace_statement_evidence(self):
        connector, client = privat_connector()
        client.get_payment.return_value["payment_status"] = "success"
        client.get_transactions.return_value = []
        self.assertIsNone(connector.confirm_payment(draft("privat24"), DOCUMENT))
        client.print_receipt.assert_not_called()

    def test_invalid_or_conflicting_bank_time_cannot_be_posted(self):
        for value in ("invalid", "01.01.2030 10:00:00", "01.01.2020 10:00:00"):
            with self.subTest(value=value):
                connector, client = privat_connector()
                client.get_transactions.return_value[0]["DATE_TIME_DAT_OD_TIM_P"] = value
                with self.assertRaises(SettlementError):
                    connector.confirm_payment(draft("privat24"), DOCUMENT)
                client.print_receipt.assert_not_called()

    def test_bank_error_disguised_as_pdf_does_not_return_confirmation(self):
        connector, client = privat_connector()
        client.print_receipt.return_value = ("receipt.pdf", b'{"status":"ERROR"}')
        with self.assertRaisesRegex(RuntimeError, "not_pdf"):
            connector.confirm_payment(draft("privat24"), DOCUMENT)


class MonobankSettlementTest(unittest.TestCase):
    def test_done_exact_debit_on_expected_account_returns_statement_without_fake_pdf(self):
        connector, client = mono_connector()
        result = connector.confirm_payment(draft("monobank"), DOCUMENT)
        self.assertEqual(result.evidence.transaction_id, draft("monobank").provider_payment_id)
        self.assertEqual(result.evidence.amount, Decimal("12.34"))
        self.assertEqual(result.evidence.paid_at, PAID)
        self.assertIsNone(result.receipt)
        self.assertEqual(result.bank_payload["statement"], mono_statement())
        self.assertEqual(result.bank_payload["account_statement"]["account"], SOURCE)
        timestamp = mono_statement()["time"]
        client.get_statement.assert_called_once_with(SOURCE, timestamp - 1, timestamp + 1, limit=500)

    def test_draft_declined_and_unknown_state_do_not_read_statements(self):
        for value in ("DRAFT", "DECLINED", "DONE", "SUCCESS", None):
            with self.subTest(value=value):
                connector, client = mono_connector()
                client.get_payment_state.return_value["state"] = value
                self.assertIsNone(connector.confirm_payment(draft("monobank"), DOCUMENT))
                client.get_statement_item.assert_not_called()
                client.get_statement.assert_not_called()

    def test_in_statement_pending_declined_or_reverse_is_not_paid(self):
        for field, value in [("status", "PENDING"), ("status", "DECLINED"), ("status", "success"), ("status", None), ("reverse", True), ("reverse", None), ("reverse", 0)]:
            with self.subTest(field=field, value=value):
                connector, client = mono_connector()
                client.get_statement_item.return_value[field] = value
                self.assertIsNone(connector.confirm_payment(draft("monobank"), DOCUMENT))
                client.get_statement.assert_not_called()

    def test_missing_bank_record_is_not_confirmation(self):
        for method in ("get_payment_state", "get_statement_item"):
            with self.subTest(method=method):
                connector, client = mono_connector()
                getattr(client, method).return_value = None
                self.assertIsNone(connector.confirm_payment(draft("monobank"), DOCUMENT))
                client.get_statement.assert_not_called()

    def test_wrong_state_id_or_nested_success_does_not_prove_identity(self):
        for value in ({"id": "wrong", "state": "IN_STATEMENT"}, {"status": "SUCCESS", "data": {"id": draft("monobank").provider_payment_id, "state": "IN_STATEMENT"}}):
            connector, client = mono_connector()
            client.get_payment_state.return_value = value
            with self.assertRaisesRegex(SettlementError, "reference_mismatch"):
                connector.confirm_payment(draft("monobank"), DOCUMENT)
            client.get_statement_item.assert_not_called()

    def test_point_statement_fields_must_all_match(self):
        changes = {
            "id": "other-id", "externalReference": "RCPT9999", "amount": -1235,
            "currencyCode": "840", "counterIban": OTHER, "counterEdrpou": "87654321",
            "counterName": "Another supplier", "description": "Another purpose",
            "completedTime": "not-a-time", "time": "not-a-time",
        }
        for field, value in changes.items():
            for replacement in (value, None):
                with self.subTest(field=field, missing=replacement is None):
                    connector, client = mono_connector()
                    client.get_statement_item.return_value[field] = replacement
                    with self.assertRaises(SettlementError):
                        connector.confirm_payment(draft("monobank"), DOCUMENT)
                    client.get_statement.assert_not_called()

    def test_credit_float_bool_and_unscaled_amount_are_not_debits(self):
        for value in (1234, -12.34, True, "-1234", -123400):
            with self.subTest(value=value):
                connector, client = mono_connector()
                client.get_statement_item.return_value["amount"] = value
                with self.assertRaises(SettlementError):
                    connector.confirm_payment(draft("monobank"), DOCUMENT)
                client.get_statement.assert_not_called()

    def test_completion_cannot_precede_creation_or_lie_in_future(self):
        for field, value in (("time", int(NOW.timestamp())), ("completedTime", int((NOW + dt.timedelta(days=1)).timestamp())), ("time", 1)):
            with self.subTest(field=field, value=value):
                connector, client = mono_connector()
                client.get_statement_item.return_value[field] = value
                with self.assertRaises(SettlementError):
                    connector.confirm_payment(draft("monobank"), DOCUMENT)
                client.get_statement.assert_not_called()

    def test_exact_payment_must_also_be_present_on_source_account(self):
        connector, client = mono_connector()
        client.get_statement.return_value = []
        self.assertIsNone(connector.confirm_payment(draft("monobank"), DOCUMENT))

    def test_duplicate_id_or_external_reference_on_account_is_ambiguous(self):
        for changed_id in (False, True):
            connector, client = mono_connector()
            extra = mono_statement()
            if changed_id:
                extra["id"] = "another-id"
            client.get_statement.return_value.append(extra)
            with self.assertRaisesRegex(SettlementError, "ambiguous"):
                connector.confirm_payment(draft("monobank"), DOCUMENT)

    def test_account_scoped_snapshot_cannot_conflict_with_point_lookup(self):
        for field, value in {"id": "another-id", "status": "PENDING", "reverse": True, "amount": -1, "completedTime": int(PAID.timestamp()) - 30, "time": mono_statement()["time"] + 30}.items():
            with self.subTest(field=field):
                connector, client = mono_connector()
                client.get_statement.return_value[0][field] = value
                with self.assertRaises(SettlementError):
                    connector.confirm_payment(draft("monobank"), DOCUMENT)

    def test_full_account_window_is_not_assumed_complete(self):
        connector, client = mono_connector()
        client.get_statement.return_value = [mono_statement()] * 500
        with self.assertRaisesRegex(SettlementError, "window_limit"):
            connector.confirm_payment(draft("monobank"), DOCUMENT)


class SettlementModeAndContextTest(unittest.TestCase):
    def test_global_dry_run_override_prevents_all_bank_reads_for_both_providers(self):
        for provider, factory in (("privat24", privat_connector), ("monobank", mono_connector)):
            connector, client = factory()
            connector.settings = settings(dry_run=True)
            self.assertIsNone(connector.confirm_payment(draft(provider), DOCUMENT))
            self.assertEqual(client.mock_calls, [])

    def test_missing_token_and_persisted_dry_run_drafts_never_contact_bank(self):
        for provider, factory in (("privat24", privat_connector), ("monobank", mono_connector)):
            for reason in ("token", "draft"):
                with self.subTest(provider=provider, reason=reason):
                    connector, client = factory()
                    item = draft(provider)
                    if reason == "token":
                        setattr(connector.settings, f"{provider}_api_token", "")
                    else:
                        item.provider_payment_id = "dry-run-synthetic"
                    self.assertIsNone(connector.confirm_payment(item, DOCUMENT))
                    self.assertEqual(client.mock_calls, [])

    def test_missing_source_or_changed_creation_context_blocks_before_bank_reads(self):
        for provider, factory in (("privat24", privat_connector), ("monobank", mono_connector)):
            for mutation in ("source", "context"):
                with self.subTest(provider=provider, mutation=mutation):
                    connector, client = factory()
                    item = draft(provider)
                    if mutation == "source":
                        item.source_account = ""
                    else:
                        item.provider_payload["creation_context"]["source_account"] = OTHER
                    with self.assertRaises(SettlementError):
                        connector.confirm_payment(item, DOCUMENT)
                    self.assertEqual(client.mock_calls, [])

    def test_registry_routes_only_supported_providers(self):
        self.assertIsInstance(build_settlement_connector(settings(), "privat24"), Privat24SettlementConnector)
        self.assertIsInstance(build_settlement_connector(settings(), "monobank"), MonobankSettlementConnector)
        with self.assertRaises(SettlementError):
            build_settlement_connector(settings(), "unsupported")


def response(data, status=200):
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://bank.invalid"))


class BankReadClientTest(unittest.TestCase):
    def test_privat_uses_documented_pagination_fields_and_collects_every_page(self):
        client = Privat24Client(settings())
        pages = [response({"status": "SUCCESS", "transactions": [{"REF": "1"}], "exist_next_page": True, "next_page_id": "NEXT"}),
                 response({"status": "SUCCESS", "transactions": [{"REF": "2"}], "exist_next_page": False})]
        with patch("src.clients.privat24.httpx.get", side_effect=pages) as get:
            items = client.get_transactions(SOURCE, NOW.date())
        self.assertEqual(items, [{"REF": "1"}, {"REF": "2"}])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["followId"], "NEXT")
        self.assertEqual(get.call_args_list[0].kwargs["timeout"].connect, 10.0)

    def test_privat_error_envelope_missing_cursor_or_empty_more_page_fails_closed(self):
        pages = [
            {"status": "ERROR", "transactions": []},
            {"status": "SUCCESS", "transactions": []},
            {"status": "SUCCESS", "transactions": [{}], "exist_next_page": True},
            {"status": "SUCCESS", "transactions": [], "exist_next_page": True, "next_page_id": "NEXT"},
        ]
        for page in pages:
            with self.subTest(page=page), patch("src.clients.privat24.httpx.get", return_value=response(page)):
                with self.assertRaises(RuntimeError):
                    Privat24Client(settings()).get_transactions(SOURCE, NOW.date())

    def test_privat_repeated_cursor_and_page_limit_never_return_partial_evidence(self):
        page = {"status": "SUCCESS", "transactions": [{}], "exist_next_page": True, "next_page_id": "NEXT"}
        for maximum in (1, 2):
            with self.subTest(maximum=maximum), patch("src.clients.privat24.httpx.get", return_value=response(page)) as get:
                with self.assertRaises(RuntimeError):
                    Privat24Client(settings()).get_transactions(SOURCE, NOW.date(), max_pages=maximum)
                self.assertEqual(get.call_count, maximum)

    def test_mono_reads_exact_id_and_404_is_not_a_confirmation(self):
        client = MonobankClient(settings())
        with patch.object(client, "_request", return_value=response({}, 404)) as request:
            self.assertIsNone(client.get_payment_state("id/with/slashes"))
            self.assertEqual(request.call_args.args[:2], ("GET", "/ext/v1/payment/id%2Fwith%2Fslashes/state"))
            self.assertIsNone(client.get_statement_item("exact-id"))
            self.assertEqual(request.call_args.kwargs["params"], {"id": "exact-id"})

    def test_mono_full_window_or_invalid_response_is_never_complete_evidence(self):
        client = MonobankClient(settings())
        for data in ([{}] * 500, {}, ["invalid"]):
            with patch.object(client, "_request", return_value=response(data)):
                with self.assertRaises(RuntimeError):
                    client.get_statement(SOURCE, 100, 102)

    def test_mono_read_errors_do_not_copy_bank_response_or_credentials(self):
        client = MonobankClient(settings())
        with patch.object(client, "_request", side_effect=RuntimeError("synthetic-private-token-detail")):
            with self.assertRaises(RuntimeError) as raised:
                client.get_statement_item("exact-id")
        self.assertNotIn("synthetic-private-token-detail", str(raised.exception))

    def test_receipt_download_enforces_pdf_type_content_size_and_safe_name(self):
        content = pdf()
        bank_response = httpx.Response(200, content=content, headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="../../escape.pdf"'}, request=httpx.Request("POST", "https://bank.invalid"))
        context = Mock()
        context.__enter__ = Mock(return_value=bank_response)
        context.__exit__ = Mock(return_value=False)
        with patch("src.clients.privat24.httpx.stream", return_value=context):
            filename, result = Privat24Client(settings()).print_receipt(SOURCE, "REF", "1")
        self.assertEqual(filename, "privat24-payment-receipt.pdf")
        self.assertEqual(result, content)

    def test_receipt_rejects_json_html_fake_pdf_and_oversized_data(self):
        for content_type, content, maximum in (("application/json", b'{}', 1024), ("application/octet-stream", b'<html>error</html>', 1024), ("application/pdf", b'%PDF-1.7 fake %%EOF', 1024), ("application/pdf", pdf(), 20)):
            with self.subTest(content_type=content_type, size_limit=maximum):
                bank_response = httpx.Response(200, content=content, headers={"content-type": content_type}, request=httpx.Request("POST", "https://bank.invalid"))
                context = Mock()
                context.__enter__ = Mock(return_value=bank_response)
                context.__exit__ = Mock(return_value=False)
                with patch("src.clients.privat24.httpx.stream", return_value=context), patch.object(Privat24Client, "MAX_RECEIPT_BYTES", maximum):
                    with self.assertRaises(RuntimeError):
                        Privat24Client(settings()).print_receipt(SOURCE, "REF", "1")

    def test_monobank_create_keeps_actual_resolved_source_and_original_reply(self):
        config = settings()
        config.monobank_source_iban = ""
        client = MonobankClient(config)
        with patch.object(client, "get_accounts", return_value=[{"iban": SOURCE, "currency": "980"}]), \
             patch.object(client, "_request", return_value=response({"id": "created-id"})):
            result = client.create_payment_draft(DOCUMENT, "Synthetic Supplier", "12345678", BENEFICIARY, None, None, Decimal("12.34"), "UAH", PURPOSE)
        self.assertEqual(result.provider_payment_id, "created-id")
        self.assertEqual(result.payload["id"], "created-id")
        self.assertEqual(result.payload["create_request"]["senderIban"], SOURCE)
        self.assertEqual(result.payload["create_request"]["externalReference"], DOCUMENT)
        self.assertEqual(result.payload["creation_context"]["source_account"], SOURCE)
        self.assertEqual(client.source_account(), SOURCE)

    def test_privat_create_keeps_both_refs_and_sent_request(self):
        client = Privat24Client(settings())
        with patch("src.clients.privat24.httpx.post", return_value=response({"payment_ref": "created-id", "payment_pack_ref": "packed-id"}, 201)):
            result = client.create_payment_draft(DOCUMENT, "Synthetic Supplier", "12345678", BENEFICIARY, None, None, Decimal("12.34"), "UAH", PURPOSE)
        self.assertEqual(result.provider_payment_id, "created-id")
        self.assertEqual(result.provider_pack_id, "packed-id")
        self.assertEqual(result.payload["payment_ref"], "created-id")
        self.assertEqual(result.payload["create_request"]["payer_account"], SOURCE)
        self.assertEqual(result.payload["creation_context"]["document_number"], DOCUMENT)


if __name__ == "__main__":
    unittest.main()
