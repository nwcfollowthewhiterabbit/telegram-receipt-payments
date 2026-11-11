"""Bounded equivalent of the native Cashflow dataset's paid transition.

The caller owns the target check and transaction. This module never commits,
creates bank payments, or replays historical CRM rows.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.connectors.crm.attachments import CrmConflict, native_xml
from src.connectors.payments.settlement import (
    SettlementEvidence, SettlementError, bank_text, currency, iban, identifier, tax_id,
)

EXPENSE_ID = "484c8429-dabf-482a-bc7b-4c75d1436a1b"
INCOME_ID = "358da0cd-9ea6-43b8-a099-cd77da3c6114"
TO_PAY_ID = "a0ee28ec-0074-414a-abe9-0526f923a84a"
DONE_ID = "fdea47be-53fe-4730-bf4f-4f44c3b5d61a"
CLOSED_CONTRACT_ID = "cd8ad8e9-2fe4-4215-aebe-380e459e94ae"
UAH_ID = "d18aaed6-14f9-435c-9606-0e90cae816f7"


def _guid(value, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise CrmConflict("crm_posting_invalid_identity") from None


def _decimal(value) -> Decimal:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise CrmConflict("crm_posting_invalid_amount") from None


def _sql_decimal(value: Decimal, scale: int) -> Decimal:
    try:
        rounded = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise CrmConflict("crm_contract_amount_overflow") from None
    if abs(rounded) >= Decimal(10) ** (15 - scale):
        raise CrmConflict("crm_contract_amount_overflow")
    return rounded


def owner_adminunit(connection, owner_id: str) -> str:
    """OwnerID references Contact; native access resolves AdminUnit.UserContactID."""
    rows = connection.execute(text("""
        SELECT CONVERT(nvarchar(36),ID) AS ID
        FROM dbo.tbl_AdminUnit WITH (HOLDLOCK)
        WHERE UserContactID=CONVERT(uniqueidentifier,:owner_id)
          AND IsGroup=0 AND UserIsEnabled=1
    """), {"owner_id": _guid(owner_id)}).scalars().all()
    if len(rows) != 1:
        raise CrmConflict("crm_owner_adminunit_not_unique")
    return _guid(rows[0])


def ensure_owner_rights(connection, operation_id: str, owner_id: str) -> str:
    """Native owner defaults, inserting only; existing grants are never widened."""
    adminunit_id = owner_adminunit(connection, owner_id)
    params = {"operation_id": _guid(operation_id), "adminunit_id": adminunit_id}
    count = connection.execute(text("""
        SELECT COUNT(*) FROM dbo.tbl_CashflowRight WITH (UPDLOCK,HOLDLOCK)
        WHERE RecordID=CONVERT(uniqueidentifier,:operation_id)
          AND AdminUnitID=CONVERT(uniqueidentifier,:adminunit_id)
    """), params).scalar_one()
    if count > 1:
        raise CrmConflict("crm_owner_rights_ambiguous")
    if not count:
        connection.execute(text("""
            INSERT INTO dbo.tbl_CashflowRight
                (ID,RecordID,AdminUnitID,CanRead,CanWrite,CanDelete,CanChangeAccess)
            VALUES (CONVERT(uniqueidentifier,:id),CONVERT(uniqueidentifier,:operation_id),
                    CONVERT(uniqueidentifier,:adminunit_id),1,1,0,0)
        """), {**params, "id": str(uuid4())})
    return adminunit_id


def native_acl_exclusions(connection) -> tuple[str, str]:
    """Read installation-specific identities from the native query, not source code."""
    root = native_xml(connection, "sq_CashflowRight")
    parameters = {node.get("Name"): node.get("Value")
                  for node in root.findall("./Parameters/Item")}
    groups = []
    individuals = []
    for node in root.findall("./Selects/Item/Filters/Item"):
        test = node.find("TestExpression")
        if node.get("IsNot") != "True" or test is None or test.get("FieldSQLName") != "AdminUnitID":
            continue
        if node.get("Type") == "IncludeFilter":
            selects = node.findall("./ValuesExpressions/Item/ExpressionSelectQuery/Selects/Item")
            if len(selects) != 1 or selects[0].get("FromTableUSI") != "tbl_UserInGroup":
                raise CrmConflict("crm_native_acl_metadata_changed")
            columns = selects[0].findall("./Columns/Item")
            filters = selects[0].findall("./Filters/Item")
            if (len(columns) != 1 or columns[0].get("ColumnAlias") != "UserID"
                    or len(filters) != 1):
                raise CrmConflict("crm_native_acl_metadata_changed")
            group_test = filters[0].find("TestExpression")
            group_value = filters[0].find("ValueExpression")
            if group_test is None or group_value is None or group_test.get("FieldSQLName") != "GroupID":
                raise CrmConflict("crm_native_acl_metadata_changed")
            groups.append(_guid(parameters.get(group_value.get("ParameterName"))))
        elif node.get("Type") == "CompareFilter":
            value = node.find("ValueExpression")
            if value is None:
                raise CrmConflict("crm_native_acl_metadata_changed")
            individuals.append(_guid(parameters.get(value.get("ParameterName"))))
    if len(groups) != 1 or len(individuals) != 1:
        raise CrmConflict("crm_native_acl_metadata_changed")
    group_id, exception_id = groups[0], individuals[0]
    matches = connection.execute(text("""
        SELECT COUNT(*) FROM dbo.tbl_AdminUnit WITH (HOLDLOCK)
        WHERE (ID=CONVERT(uniqueidentifier,:group_id) AND IsGroup=1)
           OR (ID=CONVERT(uniqueidentifier,:exception_id) AND IsGroup=0)
    """), {"group_id": group_id, "exception_id": exception_id}).scalar_one()
    if matches != 2:
        raise CrmConflict("crm_native_acl_identity_missing")
    return group_id, exception_id


def contract_amounts(contract: dict, lines: list[dict], incomes: list[dict],
                     tax_rate: Decimal | None) -> tuple[Decimal, Decimal]:
    """Persisted outputs of scr_ContractUtils.RefreshAmount(ContractID).

    TotalAmount/TotalAmountWithTax are read-only query aliases, not columns.
    The native helper uses BasicTotalAmount + IsService, unlike the separate
    tsf_GetContractAmount display function. Do not substitute that function.
    """
    days = contract["RentPeriodDays"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise CrmConflict("crm_contract_rent_days_invalid")
    if contract["IsWithTax"] not in (0, 1, False, True):
        raise CrmConflict("crm_contract_tax_flag_invalid")
    total = Decimal(0)
    for line in lines:
        if line["IsService"] not in (0, 1, False, True):
            raise CrmConflict("crm_contract_offering_type_invalid")
        total += _decimal(line["BasicTotalAmount"]) * (1 if line["IsService"] else days)
    prepayment = sum((_decimal(row["BasicAmount"]) for row in incomes), Decimal(0))
    if contract["IsWithTax"]:
        if tax_rate is None or not Decimal(0) <= tax_rate <= Decimal(100):
            raise CrmConflict("crm_contract_tax_setting_invalid")
        total *= 1 + tax_rate / 100
    return _sql_decimal(prepayment, 2), _sql_decimal(total - prepayment, 4)


class PostingService:
    def __init__(self, connection) -> None:
        self.connection = connection

    def _load_operation(self, operation_id: str) -> dict:
        row = self.connection.execute(text("""
            SELECT ID,CodPrivat,StatusID,TypeID,PayerID,RecipientID,CashAccountID,CurrencyID,
                   CurrencyRate,Amount,BasicAmount,Subject,OwnerID,ContractID,ReservedID,
                   LinkID,ActualDate,UseAsCashflow,UseAsPandL,AutocalcAmount
            FROM dbo.tbl_Cashflow WITH (UPDLOCK,HOLDLOCK)
            WHERE ID=CONVERT(uniqueidentifier,:operation_id)
        """), {"operation_id": operation_id}).mappings().first()
        if row is None:
            raise CrmConflict("crm_operation_missing")
        return dict(row)

    @staticmethod
    def _verify_evidence(expected: dict, evidence: SettlementEvidence) -> None:
        try:
            checks = (
                evidence.provider == expected["provider"] and evidence.provider in {"privat24", "monobank"},
                identifier(evidence.bank_payment_id) == identifier(expected["bank_payment_id"]),
                bool(identifier(evidence.transaction_id)),
                iban(evidence.source_account) == iban(expected["source_account"]),
                iban(evidence.beneficiary_iban) == iban(expected["beneficiary_iban"]),
                tax_id(evidence.beneficiary_tax_id) == tax_id(expected["beneficiary_tax_id"]),
                _decimal(evidence.amount) == _decimal(expected["amount"]) > 0,
                currency(evidence.currency) == currency(expected["currency"]) == "UAH",
                bank_text(evidence.purpose) == bank_text(expected["purpose"]),
                identifier(evidence.document_number) == identifier(expected["document_number"]),
                evidence.paid_at.utcoffset() is not None,
            )
        except (KeyError, TypeError, AttributeError, SettlementError):
            raise CrmConflict("crm_settlement_snapshot_incomplete") from None
        if not all(checks):
            raise CrmConflict("crm_settlement_snapshot_mismatch")

    def _verify_recipient(self, row: dict, expected: dict) -> None:
        recipient_id = _guid(row["RecipientID"], nullable=True)
        previous_id = _guid(expected.get("recipient_id"), nullable=True)
        if recipient_id is None:
            raise CrmConflict("crm_posting_recipient_missing")
        if previous_id is not None and previous_id != recipient_id:
            raise CrmConflict("crm_posting_recipient_changed")
        recipient = self.connection.execute(text("""
            SELECT TaxRegistrationCode,Code FROM dbo.tbl_Account WITH (HOLDLOCK)
            WHERE ID=CONVERT(uniqueidentifier,:recipient_id)
        """), {"recipient_id": recipient_id}).mappings().first()
        if recipient is None:
            raise CrmConflict("crm_posting_recipient_missing")
        # Same normalization as native connector lookup: spaces, dots, hyphens.
        wanted = tax_id(expected["beneficiary_tax_id"])
        codes = {str(recipient[key] or "").replace(" ", "").replace("-", "").replace(".", "")
                 for key in ("TaxRegistrationCode", "Code")}
        if wanted not in codes:
            raise CrmConflict("crm_posting_recipient_code_mismatch")

    def _verify_snapshot(self, row: dict, expected: dict) -> None:
        try:
            for column, key in (("TypeID", "type_id"), ("PayerID", "payer_id"),
                                ("CashAccountID", "cash_account_id"), ("CurrencyID", "currency_id")):
                if _guid(row[column]) != _guid(expected[key]):
                    raise CrmConflict("crm_posting_financial_fields_changed")
            if _guid(row["TypeID"]) != EXPENSE_ID or _guid(row["CurrencyID"]) != UAH_ID:
                raise CrmConflict("crm_posting_unsupported_type_or_currency")
            if (abs(_decimal(row["Amount"])) != _decimal(expected["amount"])
                    or abs(_decimal(row["BasicAmount"])) != _decimal(expected["basic_amount"])
                    or _decimal(row["CurrencyRate"]) != _decimal(expected["currency_rate"])
                    or _decimal(row["CurrencyRate"]) != 1
                    or _decimal(expected["basic_amount"]) != _decimal(expected["amount"])
                    or row["Subject"] != expected["subject"]):
                raise CrmConflict("crm_posting_financial_fields_changed")
            for column, key in (("UseAsCashflow", "use_as_cashflow"), ("UseAsPandL", "use_as_pandl"),
                                ("AutocalcAmount", "autocalc_amount")):
                if row[column] != expected[key]:
                    raise CrmConflict("crm_posting_financial_fields_changed")
        except KeyError:
            raise CrmConflict("crm_posting_snapshot_incomplete") from None
        if row["ReservedID"] is not None or row["LinkID"] is not None:
            raise CrmConflict("crm_posting_link_requires_review")
        self._verify_recipient(row, expected)

    def _prepare_contract(self, contract_id: str) -> tuple[str, Decimal, Decimal]:
        contract_id = _guid(contract_id)
        contract = self.connection.execute(text("""
            SELECT ContractStatusID,RentPeriodDays,IsWithTax
            FROM dbo.tbl_Contract WITH (UPDLOCK,HOLDLOCK)
            WHERE ID=CONVERT(uniqueidentifier,:contract_id)
        """), {"contract_id": contract_id}).mappings().first()
        if contract is None or contract["ContractStatusID"] is None:
            raise CrmConflict("crm_posting_contract_missing_or_unclassified")
        if _guid(contract["ContractStatusID"]) == CLOSED_CONTRACT_ID:
            raise CrmConflict("crm_posting_contract_closed")
        lines = self.connection.execute(text("""
            SELECT oi.BasicTotalAmount,ot.IsService
            FROM dbo.tbl_OfferingInContract oi WITH (HOLDLOCK)
            LEFT JOIN dbo.tbl_Offering o WITH (HOLDLOCK) ON o.ID=oi.OfferingID
            LEFT JOIN dbo.tbl_OfferingType ot WITH (HOLDLOCK) ON ot.ID=o.OfferingTypeID
            WHERE oi.ContractID=CONVERT(uniqueidentifier,:contract_id)
        """), {"contract_id": contract_id}).mappings().all()
        incomes = self.connection.execute(text("""
            SELECT BasicAmount FROM dbo.tbl_Cashflow WITH (HOLDLOCK)
            WHERE ContractID=CONVERT(uniqueidentifier,:contract_id)
              AND StatusID=CONVERT(uniqueidentifier,:done_id)
              AND TypeID=CONVERT(uniqueidentifier,:income_id)
        """), {"contract_id": contract_id, "done_id": DONE_ID, "income_id": INCOME_ID}).mappings().all()
        tax_rate = None
        if contract["IsWithTax"]:
            settings = self.connection.execute(text("""
                SELECT FloatValue FROM dbo.tbl_SystemSetting WITH (HOLDLOCK)
                WHERE Code='NonCashTax'
            """)).scalars().all()
            if len(settings) != 1:
                raise CrmConflict("crm_contract_tax_setting_invalid")
            tax_rate = _decimal(settings[0])
        prepayment, debt = contract_amounts(dict(contract), list(lines), list(incomes), tax_rate)
        return contract_id, prepayment, debt

    def post(self, operation_id: str, expected: dict, evidence: SettlementEvidence) -> dict:
        operation_id = _guid(operation_id)
        self._verify_evidence(expected, evidence)
        row = self._load_operation(operation_id)
        marker = expected.get("external_key")
        if (not isinstance(marker, str) or marker != f"receipt-paybot:cashflow:{operation_id}"
                or row["CodPrivat"] != marker):
            raise CrmConflict("crm_posting_ownership_mismatch")
        status = _guid(row["StatusID"])
        bank_date = evidence.paid_at.astimezone(ZoneInfo("Europe/Kyiv")).date()
        if status not in {TO_PAY_ID, DONE_ID}:
            raise CrmConflict("crm_posting_status_requires_review")
        self._verify_snapshot(row, expected)
        if status == DONE_ID:
            # Human posting is authoritative. Attachments may still be added by
            # the caller, but do not rewrite status, dates, amounts, or rights.
            return {"already_posted": True, "bank_date": bank_date.isoformat()}
        owner_adminunit(self.connection, row["OwnerID"])
        group_id, exception_id = native_acl_exclusions(self.connection)
        contract_update = self._prepare_contract(row["ContractID"]) if row["ContractID"] else None
        amount = -abs(_decimal(row["Amount"]))
        basic_amount = -abs(_decimal(row["BasicAmount"]))
        actual_date = dt.datetime.combine(bank_date, dt.time.min)
        self.connection.execute(text("""
            UPDATE dbo.tbl_Cashflow
            SET StatusID=CONVERT(uniqueidentifier,:done_id),ActualDate=:actual_date,
                Amount=:amount,BasicAmount=:basic_amount,ModifiedOn=GETUTCDATE()
            WHERE ID=CONVERT(uniqueidentifier,:operation_id)
        """), {"operation_id": operation_id, "done_id": DONE_ID,
               "actual_date": actual_date, "amount": amount, "basic_amount": basic_amount})
        self.connection.execute(text("""
            UPDATE r SET CanWrite=0,CanDelete=0,CanChangeAccess=0
            FROM dbo.tbl_CashflowRight r
            WHERE r.RecordID=CONVERT(uniqueidentifier,:operation_id)
              AND r.AdminUnitID<>CONVERT(uniqueidentifier,:exception_id)
              AND r.AdminUnitID NOT IN (
                SELECT UserID FROM dbo.tbl_UserInGroup WITH (HOLDLOCK)
                WHERE GroupID=CONVERT(uniqueidentifier,:group_id)
              )
        """), {"operation_id": operation_id, "group_id": group_id, "exception_id": exception_id})
        ensure_owner_rights(self.connection, operation_id, row["OwnerID"])
        if contract_update:
            contract_id, prepayment, debt = contract_update
            self.connection.execute(text("""
                UPDATE dbo.tbl_Contract SET PrePayment=:prepayment,Debt=:debt,ModifiedOn=GETUTCDATE()
                WHERE ID=CONVERT(uniqueidentifier,:contract_id)
            """), {"contract_id": contract_id, "prepayment": prepayment, "debt": debt})
            saved_contract = self.connection.execute(text("""
                SELECT PrePayment,Debt FROM dbo.tbl_Contract WITH (HOLDLOCK)
                WHERE ID=CONVERT(uniqueidentifier,:contract_id)
            """), {"contract_id": contract_id}).mappings().first()
            if (saved_contract is None or _decimal(saved_contract["PrePayment"]) != prepayment
                    or _decimal(saved_contract["Debt"]) != debt):
                raise CrmConflict("crm_contract_readback_mismatch")
        # INSTEAD OF triggers make rowcount unreliable; verify actual persisted values.
        saved = self._load_operation(operation_id)
        if (_guid(saved["StatusID"]) != DONE_ID or saved["ActualDate"] != actual_date
                or _decimal(saved["Amount"]) != amount or _decimal(saved["BasicAmount"]) != basic_amount):
            raise CrmConflict("crm_posting_readback_mismatch")
        return {"already_posted": False, "bank_date": bank_date.isoformat(),
                "contract_recalculated": bool(contract_update)}
