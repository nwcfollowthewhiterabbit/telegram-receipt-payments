from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from src.connectors.crm.attachments import CrmConflict
from src.connectors.crm.terrasoft_posting import (
    CLOSED_CONTRACT_ID, DONE_ID, EXPENSE_ID, TO_PAY_ID, UAH_ID, PostingService,
    contract_amounts, ensure_owner_rights, native_acl_exclusions, owner_adminunit,
)
from src.connectors.payments.settlement import SettlementEvidence


MODULE = "src.connectors.crm.terrasoft_posting"
OPERATION_ID = "00000000-0000-0000-0000-000000000001"
PAYER_ID = "00000000-0000-0000-0000-000000000002"
RECIPIENT_ID = "00000000-0000-0000-0000-000000000003"
CASH_ACCOUNT_ID = "00000000-0000-0000-0000-000000000004"
OWNER_ID = "00000000-0000-0000-0000-000000000005"
ADMIN_ID = "00000000-0000-0000-0000-000000000006"
GROUP_ID = "00000000-0000-0000-0000-000000000007"
EXCEPTION_ID = "00000000-0000-0000-0000-000000000008"
CONTRACT_ID = "00000000-0000-0000-0000-000000000009"


def make_iban(number: int) -> str:
    bban = "300001" + f"{number:019d}"
    return "UA" + f"{98 - int(bban + '301000') % 97:02d}" + bban


def evidence() -> SettlementEvidence:
    return SettlementEvidence(
        provider="monobank", bank_payment_id="bank-id", transaction_id="transaction-id",
        source_account=make_iban(1), beneficiary_iban=make_iban(2), beneficiary_tax_id="12345678",
        amount=Decimal("100.00"), currency="UAH", purpose="Оплата за послуги",
        document_number="RCPT9001", paid_at=dt.datetime(2026, 11, 11, 23, 30, tzinfo=dt.timezone.utc),
    )


def expected() -> dict:
    return {
        "external_key": "receipt-paybot:cashflow:" + OPERATION_ID,
        "type_id": EXPENSE_ID, "payer_id": PAYER_ID, "recipient_id": RECIPIENT_ID,
        "cash_account_id": CASH_ACCOUNT_ID, "currency_id": UAH_ID,
        "currency_rate": "1", "amount": "100.00", "basic_amount": "100.00",
        "subject": "Оплата за послуги", "owner_id": OWNER_ID,
        "use_as_cashflow": 1, "use_as_pandl": 1, "autocalc_amount": 0,
        "provider": "monobank", "bank_payment_id": "bank-id", "source_account": make_iban(1),
        "beneficiary_iban": make_iban(2), "beneficiary_tax_id": "12345678", "currency": "UAH",
        "purpose": "Оплата за послуги", "document_number": "RCPT9001",
    }


def operation() -> dict:
    return {
        "ID": OPERATION_ID, "CodPrivat": expected()["external_key"], "StatusID": TO_PAY_ID,
        "TypeID": EXPENSE_ID, "PayerID": PAYER_ID, "RecipientID": RECIPIENT_ID,
        "CashAccountID": CASH_ACCOUNT_ID, "CurrencyID": UAH_ID, "CurrencyRate": Decimal("1"),
        "Amount": Decimal("100.00"), "BasicAmount": Decimal("100.00"),
        "Subject": "Оплата за послуги", "OwnerID": OWNER_ID, "ContractID": None,
        "ReservedID": None, "LinkID": None, "ActualDate": dt.datetime(2026, 11, 1),
        "UseAsCashflow": 1, "UseAsPandL": 1, "AutocalcAmount": 0,
    }


class ContractCalculationTests(unittest.TestCase):
    def test_native_basic_amount_service_and_rental_formula(self):
        contract = {"RentPeriodDays": 3, "IsWithTax": 1}
        lines = [{"BasicTotalAmount": Decimal("100"), "IsService": 0},
                 {"BasicTotalAmount": Decimal("20"), "IsService": 1}]
        prepayment, debt = contract_amounts(contract, lines, [{"BasicAmount": Decimal("50")}], Decimal("5"))
        self.assertEqual(prepayment, Decimal("50.00"))
        self.assertEqual(debt, Decimal("286.0000"))

    def test_non_tax_contract_ignores_missing_tax_setting(self):
        result = contract_amounts({"RentPeriodDays": 2, "IsWithTax": 0},
                                  [{"BasicTotalAmount": Decimal("9.99"), "IsService": 0}], [], None)
        self.assertEqual(result, (Decimal("0.00"), Decimal("19.9800")))

    def test_empty_contract_reproduces_zero_total_and_signed_debt(self):
        result = contract_amounts({"RentPeriodDays": 1, "IsWithTax": 0}, [],
                                  [{"BasicAmount": Decimal("20")}], None)
        self.assertEqual(result, (Decimal("20.00"), Decimal("-20.0000")))

    def test_missing_type_and_rent_days_fail_closed(self):
        for contract, lines in [
            ({"RentPeriodDays": None, "IsWithTax": 0}, []),
            ({"RentPeriodDays": 2, "IsWithTax": 0}, [{"BasicTotalAmount": 1, "IsService": None}]),
        ]:
            with self.subTest(contract=contract, lines=lines), self.assertRaises(CrmConflict):
                contract_amounts(contract, lines, [], None)

    def test_amount_overflow_and_missing_tax_fail_closed(self):
        with self.assertRaisesRegex(CrmConflict, "tax_setting"):
            contract_amounts({"RentPeriodDays": 1, "IsWithTax": 1}, [], [], None)
        with self.assertRaisesRegex(CrmConflict, "overflow"):
            contract_amounts({"RentPeriodDays": 1, "IsWithTax": 0},
                             [{"BasicTotalAmount": Decimal("1e40"), "IsService": 1}], [], None)


class PostingTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.service = PostingService(self.connection)
        self.owner = patch(MODULE + ".owner_adminunit", return_value=ADMIN_ID).start()
        self.acl = patch(MODULE + ".native_acl_exclusions", return_value=(GROUP_ID, EXCEPTION_ID)).start()
        self.ensure = patch(MODULE + ".ensure_owner_rights", return_value=ADMIN_ID).start()
        self.addCleanup(patch.stopall)

    def prepare_rows(self, row: dict):
        saved = {**row, "StatusID": DONE_ID, "Amount": Decimal("-100.00"),
                 "BasicAmount": Decimal("-100.00"), "ActualDate": dt.datetime(2026, 11, 12)}
        self.service._load_operation = MagicMock(side_effect=[row, saved])
        self.service._verify_recipient = MagicMock()

    def test_confirmed_payment_updates_same_operation_with_kyiv_bank_date_and_negative_amount(self):
        self.prepare_rows(operation())
        result = self.service.post(OPERATION_ID, expected(), evidence())
        self.assertEqual(result, {"already_posted": False, "bank_date": "2026-11-12", "contract_recalculated": False})
        first_sql, params = self.connection.execute.call_args_list[0].args
        self.assertIn("UPDATE dbo.tbl_Cashflow", str(first_sql))
        self.assertEqual(params["operation_id"], OPERATION_ID)
        self.assertEqual(params["actual_date"], dt.datetime(2026, 11, 12))
        self.assertEqual(params["amount"], Decimal("-100"))
        self.assertNotIn("PeriodID=", str(first_sql))
        self.assertNotIn("OwnerID=", str(first_sql))
        self.assertFalse(self.connection.commit.called)
        self.assertFalse(self.connection.rollback.called)

    def test_ui_normalized_negative_amount_is_accepted(self):
        self.prepare_rows({**operation(), "Amount": Decimal("-100"), "BasicAmount": Decimal("-100")})
        self.assertFalse(self.service.post(OPERATION_ID, expected(), evidence())["already_posted"])

    def test_manual_posting_is_not_overwritten(self):
        self.service._load_operation = MagicMock(return_value={**operation(), "StatusID": DONE_ID})
        self.service._verify_recipient = MagicMock()
        result = self.service.post(OPERATION_ID, expected(), evidence())
        self.assertTrue(result["already_posted"])
        self.connection.execute.assert_not_called()
        self.ensure.assert_not_called()

    def test_manual_posting_financial_mismatch_does_not_authorize_proof(self):
        self.service._load_operation = MagicMock(return_value={**operation(), "StatusID": DONE_ID,
                                                              "Amount": Decimal("-99")})
        with self.assertRaisesRegex(CrmConflict, "financial_fields_changed"):
            self.service.post(OPERATION_ID, expected(), evidence())
        self.connection.execute.assert_not_called()

    def test_wrong_owner_marker_and_manual_status_are_blocked_without_mutation(self):
        for row in [{**operation(), "CodPrivat": "foreign"},
                    {**operation(), "StatusID": "00000000-0000-0000-0000-000000000099"}]:
            self.service._load_operation = MagicMock(return_value=row)
            with self.subTest(row=row), self.assertRaises(CrmConflict):
                self.service.post(OPERATION_ID, expected(), evidence())
        self.connection.execute.assert_not_called()

    def test_financial_edit_and_reserved_or_transfer_link_fail_closed(self):
        edits = [{"Amount": Decimal("101")}, {"BasicAmount": Decimal("101")},
                 {"CashAccountID": OWNER_ID}, {"Subject": "manually changed"},
                 {"ReservedID": CONTRACT_ID}, {"LinkID": CONTRACT_ID}, {"UseAsPandL": 0}]
        for edit in edits:
            self.service._load_operation = MagicMock(return_value={**operation(), **edit})
            with self.subTest(edit=edit), self.assertRaises(CrmConflict):
                self.service.post(OPERATION_ID, expected(), evidence())
        self.connection.execute.assert_not_called()

    def test_bank_requisites_and_payment_identity_must_match_snapshot(self):
        edits = [{"bank_payment_id": "other"}, {"source_account": make_iban(3)},
                 {"beneficiary_tax_id": "87654321"}, {"amount": Decimal("99")},
                 {"currency": "USD"}, {"purpose": "different"}, {"document_number": "RCPT9002"}]
        self.service._load_operation = MagicMock()
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaises(CrmConflict):
                self.service.post(OPERATION_ID, expected(), replace(evidence(), **edit))
        self.service._load_operation.assert_not_called()
        self.connection.execute.assert_not_called()

    def test_new_owner_and_open_contract_are_preserved_and_recalculated(self):
        row = {**operation(), "OwnerID": ADMIN_ID, "ContractID": CONTRACT_ID}
        self.prepare_rows(row)
        self.service._prepare_contract = MagicMock(return_value=(CONTRACT_ID, Decimal("50"), Decimal("286")))
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "PrePayment": Decimal("50"), "Debt": Decimal("286")}
        result = self.service.post(OPERATION_ID, expected(), evidence())
        self.assertTrue(result["contract_recalculated"])
        self.ensure.assert_called_once_with(self.connection, OPERATION_ID, ADMIN_ID)
        sql, values = self.connection.execute.call_args_list[-2].args
        self.assertIn("UPDATE dbo.tbl_Contract", str(sql))
        self.assertEqual(values["debt"], Decimal("286"))
        self.assertNotIn("TotalAmount", str(sql))

    def test_contract_trigger_readback_mismatch_is_rejected(self):
        self.prepare_rows({**operation(), "ContractID": CONTRACT_ID})
        self.service._prepare_contract = MagicMock(return_value=(CONTRACT_ID, Decimal("50"), Decimal("286")))
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "PrePayment": Decimal("50"), "Debt": Decimal("999")}
        with self.assertRaisesRegex(CrmConflict, "contract_readback"):
            self.service.post(OPERATION_ID, expected(), evidence())

    def test_closed_contract_is_rejected_before_writes(self):
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "ContractStatusID": CLOSED_CONTRACT_ID, "RentPeriodDays": 1, "IsWithTax": 0}
        with self.assertRaisesRegex(CrmConflict, "contract_closed"):
            self.service._prepare_contract(CONTRACT_ID)
        self.assertEqual(self.connection.execute.call_count, 1)
        self.assertIn("WITH (UPDLOCK,HOLDLOCK)", str(self.connection.execute.call_args.args[0]))

    def test_trigger_readback_mismatch_is_rejected(self):
        row = operation()
        self.service._load_operation = MagicMock(side_effect=[row, row])
        self.service._verify_recipient = MagicMock()
        with self.assertRaisesRegex(CrmConflict, "readback"):
            self.service.post(OPERATION_ID, expected(), evidence())

    def test_corrected_initially_empty_recipient_requires_exact_code(self):
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "TaxRegistrationCode": "12 345-678", "Code": ""}
        self.service._verify_recipient(operation(), {**expected(), "recipient_id": None})
        self.connection.execute.return_value.mappings.return_value.first.return_value = {
            "TaxRegistrationCode": "87654321", "Code": ""}
        with self.assertRaisesRegex(CrmConflict, "code_mismatch"):
            self.service._verify_recipient(operation(), {**expected(), "recipient_id": None})

    def test_missing_or_replaced_recipient_is_rejected(self):
        for row in [{**operation(), "RecipientID": None}, {**operation(), "RecipientID": OWNER_ID}]:
            with self.subTest(row=row), self.assertRaises(CrmConflict):
                self.service._verify_recipient(row, expected())
        self.connection.execute.assert_not_called()


class NativeAclTests(unittest.TestCase):
    def test_owner_is_resolved_by_user_contact_not_sql_login(self):
        connection = MagicMock()
        connection.execute.return_value.scalars.return_value.all.return_value = [ADMIN_ID]
        self.assertEqual(owner_adminunit(connection, OWNER_ID), ADMIN_ID)
        sql, params = connection.execute.call_args.args
        self.assertIn("UserContactID", str(sql))
        self.assertIn("UserIsEnabled=1", str(sql))
        self.assertEqual(params["owner_id"], OWNER_ID)
        connection.execute.return_value.scalars.return_value.all.return_value = [ADMIN_ID, EXCEPTION_ID]
        with self.assertRaisesRegex(CrmConflict, "not_unique"):
            owner_adminunit(connection, OWNER_ID)

    def test_owner_defaults_insert_only_and_existing_rights_are_not_widened(self):
        connection = MagicMock()
        with patch(MODULE + ".owner_adminunit", return_value=ADMIN_ID):
            connection.execute.return_value.scalar_one.return_value = 1
            ensure_owner_rights(connection, OPERATION_ID, OWNER_ID)
            self.assertEqual(connection.execute.call_count, 1)
            connection.reset_mock()
            connection.execute.return_value.scalar_one.return_value = 0
            ensure_owner_rights(connection, OPERATION_ID, OWNER_ID)
            self.assertEqual(connection.execute.call_count, 2)
            self.assertIn(",1,1,0,0)", str(connection.execute.call_args.args[0]))

    def test_exclusions_follow_native_query_parameters_without_private_constants(self):
        root = ET.fromstring(f"""<Service><Parameters>
          <Item Name="GroupID" Value="{{{GROUP_ID}}}"/>
          <Item Name="InstallationException" Value="{{{EXCEPTION_ID}}}"/>
        </Parameters><Selects><Item><Filters>
          <Item Type="IncludeFilter" IsNot="True"><TestExpression FieldSQLName="AdminUnitID"/>
            <ValuesExpressions><Item><ExpressionSelectQuery><Selects><Item FromTableUSI="tbl_UserInGroup">
              <Columns><Item ColumnAlias="UserID"/></Columns><Filters><Item>
                <TestExpression FieldSQLName="GroupID"/><ValueExpression ParameterName="GroupID"/>
              </Item></Filters>
            </Item></Selects></ExpressionSelectQuery></Item></ValuesExpressions>
          </Item><Item Type="CompareFilter" IsNot="True"><TestExpression FieldSQLName="AdminUnitID"/>
            <ValueExpression ParameterName="InstallationException"/>
          </Item></Filters></Item></Selects></Service>""")
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 2
        with patch(MODULE + ".native_xml", return_value=root):
            self.assertEqual(native_acl_exclusions(connection), (GROUP_ID, EXCEPTION_ID))
        root.find("./Selects/Item/Filters").remove(root.find("./Selects/Item/Filters/Item"))
        with patch(MODULE + ".native_xml", return_value=root), self.assertRaisesRegex(CrmConflict, "metadata_changed"):
            native_acl_exclusions(connection)


if __name__ == "__main__":
    unittest.main()
