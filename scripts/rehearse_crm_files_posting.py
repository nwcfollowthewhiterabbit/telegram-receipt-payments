"""Exercise CRM files/posting only in the owned, restricted verifier clone.

Default is read-only preflight. After operator authorization and exact-SHA CI:
  python scripts/rehearse_crm_files_posting.py --verified-backup VERIFIED.json \
    --execute --execution-sha SHA --ci-run-id ID --evidence-dir PRIVATE_DIR

Every synthetic write is in one transaction which is ALWAYS rolled back.
No bank HTTP, application configuration change, production allowlist change,
database deletion or backup deletion is performed. Final clone cleanup is the
separate verify_terrasoft_backup.py --cleanup operation. Evidence contains only
technical counts/booleans and fixed errors, never credentials or business rows.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from scripts import verify_terrasoft_backup as backup
from src.connectors.crm.attachments import CrmAttachment, CrmConflict, ensure_attachment
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector
from src.connectors.crm.terrasoft_posting import (
    CLOSED_CONTRACT_ID, DONE_ID, INCOME_ID, TO_PAY_ID, PostingService,
    ensure_owner_rights, native_acl_exclusions, owner_adminunit,
)
from src.connectors.payments.settlement import SettlementEvidence
from src.db.models import ReceiptStatus


require = backup.require
RehearsalError = backup.VerificationError
MUTATED_TABLES = (
    "tbl_Account", "tbl_Contract", "tbl_Offering", "tbl_OfferingType",
    "tbl_OfferingInContract", "tbl_Cashflow", "tbl_CashflowRight",
    "tbl_Period", "tbl_Files", "tbl_FilesRight", "tbl_FileInCashflow", "tbl_CashflowInCashflow",
)


def validate_evidence(evidence: dict, execution_sha: str | None, ci_run_id: str | None,
                      *, execute: bool) -> str:
    run_id = backup.validate_run_id(evidence.get("run_id", ""))
    name = backup.CLONE_PREFIX + run_id
    require(evidence.get("restore_database") == name, "rehearsal_clone_name_mismatch")
    require(evidence.get("verified") is True and evidence.get("stage") == "verified"
            and evidence.get("source_schema_matches") is True
            and evidence.get("backup_checksum_verified") is True
            and evidence.get("dbcc_physical_only_completed") is True,
            "rehearsal_verified_backup_required")
    if execute:
        require(bool(re.fullmatch(r"[0-9a-f]{40}", execution_sha or "")), "exact_execution_sha_required")
        require(bool(re.fullmatch(r"[0-9]+", ci_run_id or "")), "successful_ci_run_reference_required")
        require(evidence.get("execution_sha") == execution_sha and evidence.get("ci_run_id") == ci_run_id,
                "rehearsal_source_differs_from_backup_evidence")
    return name


def verify_owned_clone(admin: backup.SqlAdmin, evidence: dict) -> str:
    name = validate_evidence(evidence, None, None, execute=False)
    metadata = admin.metadata()
    require(metadata["is_sysadmin"] == 1
            and backup.fingerprint(metadata["server_identity"]) == evidence["server_fingerprint"]
            and metadata["source_guid"] == evidence["source_database_guid"],
            "rehearsal_source_identity_mismatch")
    identity = admin.clone_identity(name)
    require(identity["owner"] == evidence["run_id"]
            and identity["database_guid"] == evidence["clone_database_guid"]
            and identity["database_id"] == evidence["clone_database_id"]
            and str(identity["create_date"]) == evidence["clone_create_date"]
            and identity["restore_file_paths_sha256"] == evidence["restore_file_paths_sha256"],
            "rehearsal_clone_ownership_mismatch")
    require(identity["state_desc"] == "ONLINE" and identity["user_access_desc"] == "RESTRICTED_USER"
            and identity["public_connect_denied"] and not identity["is_broker_enabled"]
            and not identity["is_trustworthy_on"] and not identity["is_db_chaining_on"],
            "rehearsal_clone_isolation_changed")
    require(admin.schema_digest(name) == evidence["schema"], "rehearsal_clone_schema_changed")
    return name


def verify_connection_identity(connection, evidence: dict) -> None:
    """Repeat identity/isolation checks on the actual connection doing the work."""
    name = backup.CLONE_PREFIX + backup.validate_run_id(evidence["run_id"])
    row = connection.execute(text("""
        SELECT DB_NAME() AS name,DB_ID() AS database_id,
          CONVERT(nvarchar(256),SERVERPROPERTY('ServerName')) AS server_identity,
          d.create_date,d.state_desc,d.user_access_desc,d.is_broker_enabled,
          d.is_trustworthy_on,d.is_db_chaining_on,
          CONVERT(nvarchar(36),r.database_guid) AS database_guid
        FROM sys.databases d JOIN sys.database_recovery_status r ON r.database_id=d.database_id
        WHERE d.database_id=DB_ID()
    """)).mappings().one()
    require(row["name"] == name and row["database_id"] == evidence["clone_database_id"]
            and row["database_guid"] == evidence["clone_database_guid"]
            and str(row["create_date"]) == evidence["clone_create_date"]
            and backup.fingerprint(row["server_identity"]) == evidence["server_fingerprint"],
            "rehearsal_connection_identity_mismatch")
    owner = connection.execute(text("""
        SELECT CONVERT(nvarchar(128),value) FROM sys.extended_properties
        WHERE class=0 AND name=:property
    """), {"property": backup.OWNER_PROPERTY}).scalars().all()
    denied = connection.execute(text("""
        SELECT COUNT(*) FROM sys.database_permissions
        WHERE class=0 AND permission_name='CONNECT' AND state='D' AND grantee_principal_id=0
    """)).scalar_one()
    require(owner == [evidence["run_id"]] and denied == 1
            and row["state_desc"] == "ONLINE" and row["user_access_desc"] == "RESTRICTED_USER"
            and not row["is_broker_enabled"] and not row["is_trustworthy_on"]
            and not row["is_db_chaining_on"], "rehearsal_connection_isolation_changed")


def verify_local_trigger_graph(connection) -> int:
    """Inspect only trigger events reachable through actual write dependencies.

    The privileged restored login could reach production through an explicit
    three-part SQL target. Reject cross-database, dynamic and CLR effects.
    Merely reading a lookup table does not execute that table's triggers.
    """
    pending = []
    for name in MUTATED_TABLES:
        oid = connection.execute(text("SELECT OBJECT_ID(:name)"), {"name": "dbo." + name}).scalar_one()
        require(oid is not None, "rehearsal_schema_object_missing")
        pending.append((oid, frozenset({"INSERT", "UPDATE"})))
    seen = set()
    while pending:
        oid, events = pending.pop()
        if (oid, events) in seen:
            continue
        seen.add((oid, events))
        require(len(seen) <= 1024, "rehearsal_dependency_graph_unbounded")
        obj = connection.execute(text("""
            SELECT type,OBJECT_DEFINITION(object_id) AS definition,
              QUOTENAME(OBJECT_SCHEMA_NAME(object_id))+'.'+QUOTENAME(name) AS qualified_name
            FROM sys.all_objects WHERE object_id=:id
        """), {"id": oid}).mappings().one()
        kind = obj["type"].strip()
        require(kind not in {"SN", "PC", "FS", "FT", "AF", "TA"}, "rehearsal_external_module_unsupported")
        if kind in {"U", "V"} and events:
            require(events <= {"INSERT", "UPDATE", "DELETE"}, "rehearsal_invalid_trigger_event")
            event_sql = ",".join("'"+event+"'" for event in sorted(events))
            triggers = connection.execute(text(f"""
                SELECT DISTINCT t.object_id FROM sys.triggers t
                JOIN sys.trigger_events e ON e.object_id=t.object_id
                WHERE t.parent_id=:id AND t.is_disabled=0 AND e.type_desc IN ({event_sql})
            """), {"id": oid}).scalars().all()
            pending.extend((trigger, frozenset()) for trigger in triggers)
        writes = frozenset()
        if kind in {"TR", "P", "V", "FN", "IF", "TF"}:
            definition = obj["definition"]
            require(isinstance(definition, str) and bool(definition), "rehearsal_module_unreadable")
            code = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'", " ", definition, flags=re.S)
            require(not re.search(r"\b(?:EXEC|EXECUTE|OPENQUERY|OPENROWSET|OPENDATASOURCE|"
                                  r"BACKUP|RESTORE|USE|SEND|RECEIVE|WAITFOR)\b|\b(?:xp_|sp_OA)\w*",
                                  code, re.I), "rehearsal_external_or_dynamic_sql")
            writes = frozenset(re.findall(r"\b(?:INSERT|UPDATE|DELETE)\b", code.upper()))
            dependencies = connection.execute(text("""
                SELECT referenced_id,referenced_database_name,referenced_server_name,
                  is_ambiguous,referenced_class,referenced_entity_name,is_updated
                FROM sys.dm_sql_referenced_entities(:name,'OBJECT') WHERE referenced_minor_id=0
            """), {"name": obj["qualified_name"]}).mappings().all()
        else:
            dependencies = connection.execute(text("""
                SELECT referenced_id,referenced_database_name,referenced_server_name,is_ambiguous,
                       referenced_class,referenced_entity_name,CAST(0 AS bit) AS is_updated
                FROM sys.sql_expression_dependencies WHERE referencing_id=:id
            """), {"id": oid}).mappings().all()
        for dep in dependencies:
            require(not dep["referenced_database_name"] and not dep["referenced_server_name"]
                    and not dep["is_ambiguous"], "rehearsal_cross_database_dependency")
            if dep["referenced_class"] == 1:
                if dep["referenced_id"] is None:
                    require(kind == "TR" and not dep["is_updated"]
                            and str(dep["referenced_entity_name"]).upper() in {"INSERTED", "DELETED"},
                            "rehearsal_unresolved_dependency")
                    continue
                require(not dep["is_updated"] or bool(writes), "rehearsal_write_dependency_unknown")
                pending.append((dep["referenced_id"], writes if dep["is_updated"] else frozenset()))
    return len({oid for oid, _ in seen})


@contextmanager
def open_owned_clone(evidence: dict):
    # open_admin calls the unmodified production connector target guard first.
    with backup.open_admin(120) as admin:
        name = verify_owned_clone(admin, evidence)
        connector = TerrasoftMssqlConnector()
        require(connector.settings.terrasoft_database == backup.SOURCE
                and connector.verify_connection_target() == backup.SOURCE,
                "rehearsal_production_guard_required")
        url = connector._validate_live_target()
        engine = create_engine(url.set(database=name), echo=False, hide_parameters=True,
                               connect_args={"login_timeout": 10, "timeout": 120})
        try:
            with engine.connect() as connection:
                verify_connection_identity(connection, evidence)
                graph_size = verify_local_trigger_graph(connection)
                connector._attachment_policy(connection)
                connection.rollback()  # Only read-only preflight so far.
                yield connection, connector, graph_size
        finally:
            engine.dispose()


def synthetic_iban(number: int) -> str:
    bban = "300001" + f"{number:019d}"
    return "UA" + f"{98-int(bban+'301000') % 97:02d}" + bban


class Scenario:
    def __init__(self, connection, connector, run_id: str):
        self.connection, self.connector = connection, connector
        self.marker = "HOWERP-E2E-" + backup.validate_run_id(run_id)
        self.ids: dict[str, list[str]] = {}
        self.checks: list[str] = []

    def new_id(self, table: str) -> str:
        value = str(uuid4())
        self.ids.setdefault(table, []).append(value)
        return value

    def execute(self, sql: str, values: dict | None = None):
        return self.connection.execute(text(sql), values or {})

    def fixture(self) -> tuple[dict, SettlementEvidence, str]:
        recipient = self.new_id("tbl_Account")
        self.execute("""INSERT INTO dbo.tbl_Account (ID,Name,TaxRegistrationCode,Code)
            VALUES (CONVERT(uniqueidentifier,:id),:name,'12345678','12345678')""",
                     {"id": recipient, "name": self.marker})
        receipt = SimpleNamespace(id=1, status=ReceiptStatus.bank_created,
            extracted_invoice_date="2026-11-01", extracted_invoice_number=self.marker,
            extracted_supplier_name=self.marker, extracted_supplier_tax_id="12345678",
            extracted_supplier_iban=synthetic_iban(2), extracted_supplier_bank_name="Synthetic",
            extracted_amount=Decimal("100"), validation_payload={}, original_filename=self.marker+".pdf")
        draft = SimpleNamespace(amount=Decimal("100"), purpose=self.marker, provider_name="monobank",
                                provider_payment_id=self.marker)
        payload = self.connector._build_payload(receipt, draft)
        operation_id = self.new_id("tbl_Cashflow")
        payload.update(id=operation_id, recipient_id=recipient, external_key="receipt-paybot:cashflow:"+operation_id,
                       cf_number=self.marker, comments_payer=self.marker, status_id=TO_PAY_ID,
                       provider="monobank", bank_payment_id=self.marker, source_account=synthetic_iban(1),
                       beneficiary_iban=synthetic_iban(2), beneficiary_tax_id="12345678", currency="UAH",
                       purpose=self.marker, document_number=self.marker)
        # Reuse a native period or create it through the production helper.
        period_exists = self.execute("SELECT COUNT(*) FROM dbo.tbl_Period WHERE Name=:name",
                                     {"name":payload["estimated_date"].strftime("%m.%Y")}).scalar_one()
        payload["period_id"] = self.connector._ensure_period_id(self.connection, payload["estimated_date"])
        if not period_exists:
            self.ids.setdefault("tbl_Period", []).append(payload["period_id"])
        self.connection.execute(*self.connector._build_cashflow_insert(payload))
        ensure_owner_rights(self.connection, operation_id, payload["owner_id"])
        evidence = SettlementEvidence(provider="monobank", bank_payment_id=self.marker,
            transaction_id=self.marker+"-bank", source_account=synthetic_iban(1),
            beneficiary_iban=synthetic_iban(2), beneficiary_tax_id="12345678", amount=Decimal("100"),
            currency="UAH", paid_at=dt.datetime(2026,11,11,23,30,tzinfo=dt.timezone.utc),
            purpose=self.marker, document_number=self.marker)
        return payload, evidence, self.contract(payload)

    def contract(self, payload: dict) -> str:
        contract_id = self.new_id("tbl_Contract")
        status = self.execute("""SELECT TOP (1) ID FROM dbo.tbl_ContractStatus
            WHERE ID<>CONVERT(uniqueidentifier,:closed) ORDER BY ID""", {"closed": CLOSED_CONTRACT_ID}).scalar_one()
        self.execute("""INSERT INTO dbo.tbl_Contract
            (ID,Title,ContractStatusID,RentPeriodDays,IsWithTax,PrePayment,Debt)
            VALUES (CONVERT(uniqueidentifier,:id),:title,:status,3,1,0,0)""",
                     {"id":contract_id,"title":self.marker,"status":status})
        for service, amount in ((0, Decimal("100")), (1, Decimal("20"))):
            type_id = self.new_id("tbl_OfferingType")
            offering_id = self.new_id("tbl_Offering")
            line_id = self.new_id("tbl_OfferingInContract")
            self.execute("""INSERT INTO dbo.tbl_OfferingType (ID,Name,IsService,IsNotCalcByDays)
                VALUES (CONVERT(uniqueidentifier,:id),:name,:service,:service)""",
                         {"id":type_id,"name":self.marker,"service":service})
            self.execute("""INSERT INTO dbo.tbl_Offering (ID,Name,OfferingTypeID)
                VALUES (CONVERT(uniqueidentifier,:id),:name,CONVERT(uniqueidentifier,:type))""",
                         {"id":offering_id,"name":self.marker,"type":type_id})
            self.execute("""INSERT INTO dbo.tbl_OfferingInContract
                (ID,OfferingID,ContractID,BasicTotalAmount,TotalAmount)
                VALUES (CONVERT(uniqueidentifier,:id),CONVERT(uniqueidentifier,:offering),
                        CONVERT(uniqueidentifier,:contract),:amount,:amount)""",
                         {"id":line_id,"offering":offering_id,"contract":contract_id,"amount":amount})
        income = dict(payload, id=self.new_id("tbl_Cashflow"), type_id=INCOME_ID, status_id=DONE_ID,
                      amount=Decimal("50"),basic_amount=Decimal("50"),cf_number=self.marker+"-income")
        income["external_key"] = "receipt-paybot:cashflow:"+income["id"]
        self.connection.execute(*self.connector._build_cashflow_insert(income))
        for operation_id in (payload["id"], income["id"]):
            self.execute("""UPDATE dbo.tbl_Cashflow SET ContractID=CONVERT(uniqueidentifier,:contract)
                WHERE ID=CONVERT(uniqueidentifier,:operation)""", {"contract":contract_id,"operation":operation_id})
        return contract_id

    def state(self, operation_id: str) -> dict:
        return dict(self.execute("""SELECT StatusID,Amount,BasicAmount,ActualDate,PeriodID
            FROM dbo.tbl_Cashflow WHERE ID=CONVERT(uniqueidentifier,:id)""",
                                {"id":operation_id}).mappings().one())

    def transfer_link_fixture(self, operation_id: str, child_id: str) -> str:
        # Native Cashflow.LinkID references the relationship row, not another
        # Cashflow. Use a valid relationship to exercise the application guard
        # without dooming the SQL transaction through a foreign-key failure.
        link_id = self.new_id("tbl_CashflowInCashflow")
        self.execute("""INSERT INTO dbo.tbl_CashflowInCashflow (ID,ParentID,ChildID)
            VALUES (CONVERT(uniqueidentifier,:id),CONVERT(uniqueidentifier,:parent),
                    CONVERT(uniqueidentifier,:child))""",
                     {"id":link_id,"parent":operation_id,"child":child_id})
        self.execute("""UPDATE dbo.tbl_Cashflow SET LinkID=CONVERT(uniqueidentifier,:linked)
            WHERE ID=CONVERT(uniqueidentifier,:id)""", {"id":operation_id,"linked":link_id})
        return link_id

    def expect_conflict(self, action, code: str) -> None:
        try:
            action()
        except CrmConflict as exc:
            require(str(exc) == code, "rehearsal_unexpected_conflict")
        else:
            raise RehearsalError("rehearsal_expected_conflict_missing")

    def file_check(self, operation_id: str, attachment: CrmAttachment) -> None:
        file_id = attachment.file_id(operation_id)
        row = self.execute("""SELECT FileSize,CONVERT(varbinary(max),FileData) AS body,
            (SELECT COUNT(*) FROM dbo.tbl_FileInCashflow l WHERE l.FileID=f.ID
             AND l.CashflowID=CONVERT(uniqueidentifier,:operation)) AS links
            FROM dbo.tbl_Files f WHERE f.ID=CONVERT(uniqueidentifier,:file)""",
                           {"file":file_id,"operation":operation_id}).mappings().one()
        require(row["FileSize"] == len(attachment.content) and row["links"] == 1
                and hashlib.sha256(row["body"]).hexdigest() == attachment.sha256,
                "rehearsal_file_bytes_or_link_mismatch")
        missing = self.execute("""SELECT COUNT(*) FROM dbo.tbl_CashflowRight c
            WHERE c.RecordID=CONVERT(uniqueidentifier,:operation) AND c.CanRead=1 AND NOT EXISTS (
              SELECT 1 FROM dbo.tbl_FilesRight f WHERE f.RecordID=CONVERT(uniqueidentifier,:file)
              AND f.AdminUnitID=c.AdminUnitID AND f.CanRead=1)""",
                               {"file":file_id,"operation":operation_id}).scalar_one()
        require(missing == 0, "rehearsal_file_acl_missing")

    def run(self) -> dict:
        payload, evidence, contract_id = self.fixture()
        operation_id = payload["id"]
        original = CrmAttachment("invoice", self.marker+".pdf", b"%PDF-1.4\n% synthetic invoice\n%%EOF", "application/pdf")
        bank = CrmAttachment("bank-statement", self.marker+".json", b'{"synthetic":true}', "application/json")
        file_type, limit = self.connector._attachment_policy(self.connection)
        for attachment in (original, bank):
            self.ids.setdefault("tbl_Files", []).append(attachment.file_id(operation_id))
        attach = lambda value: ensure_attachment(self.connection,operation_id,value,file_type=file_type,max_bytes=limit)
        attach(original)
        attach(original)
        self.file_check(operation_id, original)
        self.checks.append("source_bytes_link_acl_repeat")
        before = self.state(operation_id)
        service = PostingService(self.connection)
        # A failure after native posting and creation of a bank file must undo
        # all of those writes, not leave a paid status with missing proof.
        nested = self.connection.begin_nested()
        try:
            service.post(operation_id,payload,evidence)
            attach(bank)
            self.expect_conflict(lambda: attach(replace(bank,content=b"different")), "crm_attachment_conflict")
        finally:
            nested.rollback()
        require(self.state(operation_id) == before, "rehearsal_failed_transaction_changed_cashflow")
        absent = self.execute("SELECT COUNT(*) FROM dbo.tbl_Files WHERE ID=CONVERT(uniqueidentifier,:id)",
                              {"id":bank.file_id(operation_id)}).scalar_one()
        require(absent == 0, "rehearsal_failed_transaction_left_file")
        rolled_back_contract = self.execute("SELECT PrePayment,Debt FROM dbo.tbl_Contract WHERE ID=CONVERT(uniqueidentifier,:id)",
                                            {"id":contract_id}).mappings().one()
        require(rolled_back_contract["PrePayment"] == 0 and rolled_back_contract["Debt"] == 0,
                "rehearsal_failed_transaction_changed_contract")
        self.checks.append("post_and_attachment_failure_rolled_back")
        self.expect_conflict(lambda: service.post(operation_id,payload,replace(evidence,amount=Decimal("101"))),
                             "crm_settlement_snapshot_mismatch")
        nested = self.connection.begin_nested()
        try:
            self.transfer_link_fixture(operation_id,self.ids["tbl_Cashflow"][1])
            self.expect_conflict(lambda: service.post(operation_id,payload,evidence), "crm_posting_link_requires_review")
        finally:
            nested.rollback()
        self.checks.append("wrong_bank_amount_and_transfer_link_refused")
        result = service.post(operation_id,payload,evidence)
        require(result["already_posted"] is False and result["contract_recalculated"] is True,
                "rehearsal_native_post_not_performed")
        attach(bank)
        saved = self.state(operation_id)
        require(str(saved["StatusID"]).lower() == DONE_ID and saved["Amount"] == Decimal("-100")
                and saved["BasicAmount"] == Decimal("-100")
                and saved["ActualDate"] == dt.datetime(2026,11,12)
                and saved["PeriodID"] == before["PeriodID"], "rehearsal_post_readback_mismatch")
        contract = self.execute("SELECT PrePayment,Debt FROM dbo.tbl_Contract WHERE ID=CONVERT(uniqueidentifier,:id)",
                                {"id":contract_id}).mappings().one()
        tax = Decimal(str(self.execute("SELECT FloatValue FROM dbo.tbl_SystemSetting WHERE Code='NonCashTax'").scalar_one()))
        debt = (Decimal("320")*(1+tax/100)-50).quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP)
        require(contract["PrePayment"] == Decimal("50") and contract["Debt"] == debt,
                "rehearsal_native_contract_mismatch")
        owner = owner_adminunit(self.connection,payload["owner_id"])
        rights = self.execute("""SELECT CanRead FROM dbo.tbl_CashflowRight
            WHERE RecordID=CONVERT(uniqueidentifier,:operation) AND AdminUnitID=CONVERT(uniqueidentifier,:owner)""",
                              {"operation":operation_id,"owner":owner}).scalars().all()
        require(rights == [1], "rehearsal_owner_visibility_missing")
        group, exception = native_acl_exclusions(self.connection)
        unsafe = self.execute("""SELECT COUNT(*) FROM dbo.tbl_CashflowRight
            WHERE RecordID=CONVERT(uniqueidentifier,:operation)
              AND AdminUnitID<>CONVERT(uniqueidentifier,:exception)
              AND AdminUnitID NOT IN (SELECT UserID FROM dbo.tbl_UserInGroup WHERE GroupID=CONVERT(uniqueidentifier,:group))
              AND (CanWrite<>0 OR CanDelete<>0 OR CanChangeAccess<>0)""",
                              {"operation":operation_id,"exception":exception,"group":group}).scalar_one()
        require(unsafe == 0, "rehearsal_native_acl_not_narrowed")
        self.file_check(operation_id, original)
        self.file_check(operation_id, bank)
        self.checks.append("native_post_date_sign_period_contract_acl")
        require(service.post(operation_id,payload,evidence)["already_posted"] is True,
                "rehearsal_post_repeat_not_idempotent")
        attach(bank)
        require(self.state(operation_id) == saved, "rehearsal_repeat_changed_cashflow")
        self.checks.append("posted_and_bank_file_repeat")
        nested = self.connection.begin_nested()
        try:
            self.execute("UPDATE dbo.tbl_Cashflow SET Amount=-101 WHERE ID=CONVERT(uniqueidentifier,:id)",
                         {"id":operation_id})
            self.expect_conflict(lambda: service.post(operation_id,payload,evidence),
                                 "crm_posting_financial_fields_changed")
        finally:
            nested.rollback()
        self.checks.append("manually_changed_posted_amount_refused")
        return {"checks":self.checks,"synthetic_records":sum(map(len,self.ids.values())),
                "bank_http_calls":0,"production_mutations":False}

    def assert_rolled_back(self) -> None:
        for table, values in self.ids.items():
            require(table in MUTATED_TABLES, "rehearsal_unknown_fixture_table")
            for value in values:
                count = self.execute(f"SELECT COUNT(*) FROM dbo.{table} WHERE ID=CONVERT(uniqueidentifier,:id)",
                                     {"id":value}).scalar_one()
                require(count == 0, "rehearsal_fixture_rollback_incomplete")
        for table, target in (("tbl_CashflowLog","tbl_Cashflow"),("tbl_DatabaseLog","tbl_Cashflow"),
                              ("tbl_ContractLog","tbl_Contract"),("tbl_CashflowRight","tbl_Cashflow"),
                              ("tbl_FilesRight","tbl_Files")):
            for value in self.ids.get(target, []):
                count = self.execute(f"SELECT COUNT(*) FROM dbo.{table} WHERE RecordID=CONVERT(uniqueidentifier,:id)",
                                     {"id":value}).scalar_one()
                require(count == 0, "rehearsal_audit_rollback_incomplete")
        for value in self.ids.get("tbl_Files", []):
            count = self.execute("SELECT COUNT(*) FROM dbo.tbl_FileInCashflow WHERE FileID=CONVERT(uniqueidentifier,:id)",
                                 {"id":value}).scalar_one()
            require(count == 0, "rehearsal_file_link_rollback_incomplete")


def execute_rehearsal(connection, connector, evidence: dict, run_id: str) -> dict:
    scenario = Scenario(connection,connector,run_id)
    transaction = connection.begin()
    try:
        verify_connection_identity(connection,evidence)
        connection.execute(text("SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000"))
        result = scenario.run()
    finally:
        transaction.rollback()
    scenario.assert_rolled_back()
    connection.rollback()
    return {**result,"all_fixture_writes_rolled_back":True,"clone_retained":True,
            "final_clone_cleanup":"verify_terrasoft_backup.py --cleanup VERIFIED_JSON"}


def main(argv: list[str] | None = None) -> int:
    import json
    parser = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verified-backup",type=Path,required=True)
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--execution-sha")
    parser.add_argument("--ci-run-id")
    parser.add_argument("--evidence-dir",type=Path)
    args = parser.parse_args(argv)
    journal = None
    try:
        evidence = backup.read_verified_evidence(args.verified_backup)
        validate_evidence(evidence,args.execution_sha,args.ci_run_id,execute=args.execute)
        run_id = uuid4().hex
        if args.execute:
            require(args.evidence_dir is not None,"evidence_directory_required")
            journal = backup.Journal(args.evidence_dir,{
                "run_id":run_id,"execution_sha":args.execution_sha,"ci_run_id":args.ci_run_id,
                "backup_record_sha256":evidence["record_sha256"],"restore_database":evidence["restore_database"]})
        with open_owned_clone(evidence) as (connection,connector,graph_size):
            if not args.execute:
                print(json.dumps({"mode":"read_only_rehearsal_preflight","owned_restricted_clone":True,
                                  "local_dependency_objects":graph_size,"mutations":False}))
                return 0
            journal.write("rehearsal_started",verified=False)
            result = execute_rehearsal(connection,connector,evidence,run_id)
            journal.write("rehearsal_passed",verified=True,passed=True,local_dependency_objects=graph_size,**result)
        return 0
    except BaseException as exc:
        code = str(exc) if isinstance(exc,(RehearsalError,CrmConflict)) else "rehearsal_failed"
        if not re.fullmatch(r"[a-z0-9_]+",code):
            code = "rehearsal_failed"
        if journal:
            journal.write("rehearsal_failed",verified=False,passed=False,error_code=code,clone_retained=True)
        else:
            print(json.dumps({"status":"failed","error_code":code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
