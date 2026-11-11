"""Plan, create and verify an isolated SQL Server backup of the configured CRM.

Default: read-only metadata plan; no files or databases are created. Operators
must first push the source and verify successful exact-SHA CI, then run with
--execute --execution-sha SHA --ci-run-id ID --evidence-dir EXISTING_PRIVATE_DIR.
The restored database and backup are retained. Cleanup is a separate command:
--execute --cleanup VERIFIED_JSON (plus the same execution/evidence arguments).
It drops only the owned, verified clone and NEVER removes the backup file.
To recheck a retained restricted clone after a verifier-only correction:
--revalidate-existing OLD_DIR/RUN-003-restore_restricted.json --execute
--execution-sha NEW_SHA --ci-run-id NEW_CI --evidence-dir NEW_EMPTY_PRIVATE_DIR.
Mount the entire OLD_DIR read-only so its 000..003 journal chain is available.
This repeats checksum/physical/schema/attachment checks, never backup or restore.

The supported target is the existing Windows SQL Server 2019+ Terrasoft with
ordinary ROWS/LOG files. No arbitrary target, directory, SQL or restore name is
accepted. Plan derives directories from SQL Server metadata. All SQL exceptions
are reduced to stage/error codes; SQL text, paths, credentials and rows are not
printed or placed in evidence. Evidence hashes are hashes of technical metadata,
not hashes of business documents. SQL backup CHECKSUM is not a file SHA-256.

Microsoft references (checked 2026-11-11):
https://learn.microsoft.com/en-us/sql/t-sql/statements/backup-transact-sql
https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-transact-sql
https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-verifyonly-transact-sql
https://learn.microsoft.com/en-us/sql/t-sql/database-console-commands/dbcc-checkdb-transact-sql
https://learn.microsoft.com/en-us/sql/t-sql/statements/deny-transact-sql
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from typing import Any
from uuid import UUID, uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector


SOURCE = "Terrasoft"
CLONE_PREFIX = "ReceiptFlowRestore_"
BACKUP_PREFIX = "ReceiptFlow_copyonly_"
OWNER_PROPERTY = "ReceiptFlowRestoreOwner"
SCHEMA_VERSION = 1


class VerificationError(RuntimeError):
    """Only fixed, non-sensitive codes may be surfaced to an operator."""


def require(condition: Any, code: str) -> None:
    if not condition:
        raise VerificationError(code)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value: str) -> str:
    return "[" + value.replace("]", "]]") + "]"


def literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def validate_run_id(value: str) -> str:
    require(bool(re.fullmatch(r"[0-9a-f]{32}", value)), "invalid_run_id")
    require(UUID(hex=value).version == 4, "run_id_must_be_uuid4")
    return value


def local_directory(value: str) -> PureWindowsPath:
    path = PureWindowsPath(value)
    require(bool(re.fullmatch(r"[A-Za-z]:", path.drive)) and bool(path.root), "nonlocal_directory")
    require(".." not in path.parts and "testd" not in value.casefold(), "forbidden_directory")
    return path


@dataclass(frozen=True)
class BackupPlan:
    run_id: str
    source_guid: str
    server_fingerprint: str
    backup_path: str
    files: tuple[dict, ...]
    allocated_bytes: int
    headroom_multiplier: float
    volume_free_bytes: tuple[int, ...]

    @property
    def clone_name(self) -> str:
        return CLONE_PREFIX + self.run_id

    def public(self) -> dict:
        return {
            "run_id": self.run_id,
            "restore_database": self.clone_name,
            "backup_basename": PureWindowsPath(self.backup_path).name,
            "source_database_guid": self.source_guid,
            "server_fingerprint": self.server_fingerprint,
            "source_file_count": len(self.files),
            "allocated_bytes": self.allocated_bytes,
            "headroom_multiplier": self.headroom_multiplier,
            "volume_free_bytes": list(self.volume_free_bytes),
            "restore_file_paths_sha256": fingerprint(sorted(f["destination"].casefold() for f in self.files)),
            "backup_path_sha256": fingerprint(self.backup_path.casefold()),
        }


class SqlAdmin:
    def __init__(self, connection):
        self.connection = connection

    def rows(self, sql: str, params: dict | None = None) -> list[dict]:
        result = self.connection.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()] if result.returns_rows else []

    def command(self, sql: str) -> None:
        result = self.connection.execute(text(sql))
        # With NO_INFOMSGS a successful CHECKDB has no diagnostic rows.
        if result.returns_rows:
            require(not result.fetchall(), "unexpected_admin_result")
        result.close()

    def file_exists(self, path: str) -> dict:
        result = self.connection.execute(text("EXEC master.dbo.xp_fileexist :path"), {"path": path}).first()
        require(result is not None, "file_check_unavailable")
        return {"file": bool(result[0]), "directory": bool(result[1]), "parent": bool(result[2])}

    def database_exists(self, name: str) -> bool:
        return bool(self.rows("SELECT database_id FROM sys.databases WHERE name=:name", {"name": name}))

    def metadata(self) -> dict:
        info = self.rows("""
            SELECT CONVERT(nvarchar(128), SERVERPROPERTY('ProductVersion')) AS version,
              CONVERT(nvarchar(256), SERVERPROPERTY('ServerName')) AS server_identity,
              CONVERT(nvarchar(4000), SERVERPROPERTY('InstanceDefaultBackupPath')) AS backup_dir,
              CONVERT(nvarchar(4000), SERVERPROPERTY('InstanceDefaultDataPath')) AS data_dir,
              CONVERT(nvarchar(4000), SERVERPROPERTY('InstanceDefaultLogPath')) AS log_dir,
              IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin
        """)[0]
        info.update(self.rows("""
            SELECT CONVERT(nvarchar(36), r.database_guid) AS source_guid,
              d.state_desc, d.is_read_only FROM sys.databases d
              JOIN sys.database_recovery_status r ON r.database_id=d.database_id
              WHERE d.name=:source
        """, {"source": SOURCE})[0])
        info["platform"] = self.rows("SELECT host_platform FROM sys.dm_os_host_info")[0]["host_platform"]
        info["files"] = self.rows("""
            SELECT file_id, name AS logical_name, type_desc,
              physical_name, CONVERT(bigint,size)*8192 AS size_bytes
            FROM sys.master_files WHERE database_id=DB_ID(:source) ORDER BY file_id
        """, {"source": SOURCE})
        # xp_fixeddrives is read-only; drive names stay inside the process.
        result = self.connection.execute(text("EXEC master.dbo.xp_fixeddrives"))
        info["free"] = {str(r[0]).upper() + ":": int(r[1]) * 1024 * 1024 for r in result}
        return info

    def clone_identity(self, name: str) -> dict:
        require(name.startswith(CLONE_PREFIX), "invalid_clone_prefix")
        rows = self.rows("""
            SELECT d.database_id, d.create_date, d.state_desc, d.user_access_desc,
              d.is_broker_enabled,d.is_trustworthy_on,d.is_db_chaining_on,
              CONVERT(nvarchar(36),r.database_guid) AS database_guid
            FROM sys.databases d JOIN sys.database_recovery_status r
              ON r.database_id=d.database_id WHERE d.name=:name
        """, {"name": name})
        require(len(rows) == 1, "clone_not_found")
        identity = rows[0]
        db = identifier(name)
        marker = self.rows(f"SELECT CONVERT(nvarchar(128),value) AS owner FROM {db}.sys.extended_properties WHERE class=0 AND name=:key", {"key": OWNER_PROPERTY})
        identity["owner"] = marker[0]["owner"] if len(marker) == 1 else None
        identity["public_connect_denied"] = bool(self.rows(f"SELECT 1 AS denied FROM {db}.sys.database_permissions WHERE class=0 AND permission_name='CONNECT' AND state='D' AND grantee_principal_id=0"))
        paths = self.rows("SELECT physical_name FROM sys.master_files WHERE database_id=:id ORDER BY file_id", {"id": identity["database_id"]})
        identity["restore_file_paths_sha256"] = fingerprint(sorted(r["physical_name"].casefold() for r in paths))
        return identity

    def schema_digest(self, name: str) -> dict:
        db = identifier(name)
        queries = [
            f"SELECT o.object_id,s.name AS schema_name,o.name,o.type,o.parent_object_id,CONVERT(varchar(64),HASHBYTES('SHA2_256',CONVERT(varbinary(max),m.definition)),2) AS definition_digest FROM {db}.sys.objects o JOIN {db}.sys.schemas s ON s.schema_id=o.schema_id LEFT JOIN {db}.sys.sql_modules m ON m.object_id=o.object_id WHERE o.is_ms_shipped=0 ORDER BY o.object_id",
            f"SELECT c.object_id,c.column_id,c.name,c.system_type_id,c.user_type_id,c.max_length,c.precision,c.scale,c.is_nullable,c.is_identity,c.is_computed,c.default_object_id FROM {db}.sys.columns c JOIN {db}.sys.objects o ON o.object_id=c.object_id WHERE o.is_ms_shipped=0 ORDER BY c.object_id,c.column_id",
            f"SELECT i.object_id,i.index_id,i.name,i.type,i.is_unique,i.is_primary_key,i.is_disabled,i.filter_definition,c.index_column_id,c.column_id,c.key_ordinal,c.is_included_column FROM {db}.sys.indexes i JOIN {db}.sys.objects o ON o.object_id=i.object_id LEFT JOIN {db}.sys.index_columns c ON c.object_id=i.object_id AND c.index_id=i.index_id WHERE o.is_ms_shipped=0 ORDER BY i.object_id,i.index_id,c.index_column_id",
            f"SELECT f.object_id,f.parent_object_id,f.referenced_object_id,f.delete_referential_action,f.update_referential_action,f.is_disabled,f.is_not_trusted,c.constraint_column_id,c.parent_column_id,c.referenced_column_id FROM {db}.sys.foreign_keys f JOIN {db}.sys.foreign_key_columns c ON c.constraint_object_id=f.object_id ORDER BY f.object_id,c.constraint_column_id",
            f"SELECT object_id,parent_object_id,parent_column_id,definition FROM {db}.sys.default_constraints ORDER BY object_id",
            f"SELECT object_id,parent_object_id,parent_column_id,definition,is_disabled,is_not_trusted FROM {db}.sys.check_constraints ORDER BY object_id",
            f"SELECT object_id,column_id,definition,is_persisted FROM {db}.sys.computed_columns ORDER BY object_id,column_id",
        ]
        records = [self.rows(q) for q in queries]
        return {"catalog_rows": sum(map(len, records)), "schema_sha256": fingerprint(records)}

    def attachment_checks(self, name: str) -> dict:
        db = identifier(name)
        # Read length metadata for every native blob, but hash at most three
        # deterministic bodies on the CLONE to avoid scanning all 20+ GB twice.
        totals = self.rows(f"SELECT COUNT_BIG(*) AS file_rows,COUNT_BIG(CASE WHEN FileData IS NOT NULL THEN 1 END) AS blob_rows,COALESCE(SUM(CONVERT(bigint,DATALENGTH(FileData))),0) AS blob_bytes,SUM(CASE WHEN FileData IS NOT NULL AND FileSize<>DATALENGTH(FileData) THEN CONVERT(bigint,1) ELSE 0 END) AS size_metadata_mismatches FROM {db}.dbo.tbl_Files")[0]
        samples = self.rows(f"SELECT TOP (3) DATALENGTH(FileData) AS bytes,CONVERT(varchar(64),HASHBYTES('SHA2_256',CONVERT(varbinary(max),FileData)),2) AS digest FROM {db}.dbo.tbl_Files WHERE FileData IS NOT NULL ORDER BY ID")
        require(all(re.fullmatch(r"[0-9A-Fa-f]{64}", row["digest"] or "") for row in samples), "attachment_read_failed")
        links = self.rows(f"SELECT COUNT_BIG(*) AS link_rows,SUM(CASE WHEN f.ID IS NULL OR c.ID IS NULL THEN CONVERT(bigint,1) ELSE 0 END) AS orphan_links FROM {db}.dbo.tbl_FileInCashflow l LEFT JOIN {db}.dbo.tbl_Files f ON f.ID=l.FileID LEFT JOIN {db}.dbo.tbl_Cashflow c ON c.ID=l.CashflowID")[0]
        rights = self.rows(f"SELECT COUNT_BIG(*) AS attachment_right_rows FROM {db}.dbo.tbl_FilesRight")[0]
        # Historic NULL bodies may represent native links, not lost blobs.
        return {**totals, **links, **rights, "sampled_bodies": len(samples), "sample_hash_reads_valid": True}


@contextmanager
def open_admin(timeout_seconds: int = 7200):
    connector = TerrasoftMssqlConnector()
    require(connector.settings.crm_provider == "terrasoft_mssql" and not connector.settings.crm_dry_run, "live_crm_configuration_required")
    require(connector.settings.terrasoft_database == SOURCE, "production_target_required")
    # Validate actual target BEFORE constructing any administrative connection.
    require(connector.verify_connection_target() == SOURCE, "source_target_verification_failed")
    url = connector._validate_live_target()
    engine = create_engine(url.set(database="master"), isolation_level="AUTOCOMMIT", echo=False,
                           connect_args={"login_timeout": 10, "timeout": timeout_seconds})
    try:
        with engine.connect() as connection:
            require(connection.execute(text("SELECT DB_NAME()")).scalar_one() == "master", "admin_database_mismatch")
            yield SqlAdmin(connection)
    finally:
        engine.dispose()


def build_plan(admin: SqlAdmin, run_id: str, headroom: float = 2.0) -> BackupPlan:
    validate_run_id(run_id)
    require(math.isfinite(headroom) and 2 <= headroom <= 16, "invalid_headroom")
    m = admin.metadata()
    require(m["platform"] == "Windows" and int(m["version"].split(".")[0]) >= 15, "unsupported_sql_platform")
    require(m["is_sysadmin"] == 1, "sysadmin_required_for_isolated_restore")
    require(m["state_desc"] == "ONLINE" and not m["is_read_only"], "source_database_not_online_writable")
    require(m["files"] and all(f["type_desc"] in {"ROWS", "LOG"} for f in m["files"]), "unsupported_source_file_type")
    dirs = {key: local_directory(m[key] or "") for key in ("backup_dir", "data_dir", "log_dir")}
    for path in dirs.values():
        require(admin.file_exists(str(path))["directory"], "destination_directory_missing")
    allocated = sum(int(f["size_bytes"]) for f in m["files"])
    require(allocated > 0, "invalid_database_size")
    free = tuple(m["free"].get(drive.upper(), 0) for drive in sorted({p.drive for p in dirs.values()}))
    require(all(value >= math.ceil(allocated * headroom) for value in free), "insufficient_free_headroom")
    files = []
    for f in m["files"]:
        folder = dirs["log_dir" if f["type_desc"] == "LOG" else "data_dir"]
        extension = ".ldf" if f["type_desc"] == "LOG" else ".mdf" if f["file_id"] == 1 else ".ndf"
        files.append({"logical_name": f["logical_name"], "file_id": f["file_id"], "type": "L" if f["type_desc"] == "LOG" else "D", "size_bytes": int(f["size_bytes"]), "destination": str(folder / f"{CLONE_PREFIX}{run_id}_{f['file_id']}{extension}")})
    backup_path = str(dirs["backup_dir"] / (BACKUP_PREFIX + run_id + ".bak"))
    source_paths = {f["physical_name"].casefold() for f in m["files"]}
    require(not any(f["destination"].casefold() in source_paths for f in files), "source_path_collision")
    require(len({f["destination"].casefold() for f in files}) == len(files), "restore_path_collision")
    plan = BackupPlan(run_id, m["source_guid"], fingerprint(m["server_identity"]), backup_path, tuple(files), allocated, headroom, free)
    check_collisions(admin, plan)
    return plan


def check_collisions(admin: SqlAdmin, plan: BackupPlan, *, backup_exists: bool = False) -> None:
    require(not admin.database_exists(plan.clone_name), "restore_database_exists")
    for path in [f["destination"] for f in plan.files]:
        found = admin.file_exists(path)
        require(not found["file"] and not found["directory"] and found["parent"], "restore_file_exists_or_parent_missing")
    found = admin.file_exists(plan.backup_path)
    require(found["file"] == backup_exists and not found["directory"] and found["parent"], "backup_file_collision_or_missing")


class Journal:
    def __init__(self, directory: Path, context: dict):
        require(directory.is_dir() and not directory.is_symlink(), "evidence_directory_required")
        require(directory.stat().st_mode & 0o077 == 0, "evidence_directory_must_be_private")
        self.directory, self.context, self.sequence, self.previous = directory, context, 0, None

    def write(self, stage: str, **details) -> dict:
        record = {"schema_version": SCHEMA_VERSION, **self.context,
                  "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "stage": stage, "previous_record_sha256": self.previous, **details}
        record["record_sha256"] = fingerprint(record)
        filename = f"{record['run_id']}-{self.sequence:03d}-{stage}.json"
        fd = os.open(self.directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(canonical(record) + b"\n")
            out.flush()
            os.fsync(out.fileno())
        self.previous, self.sequence = record["record_sha256"], self.sequence + 1
        print(json.dumps({"evidence_basename": filename, **record}, default=str), flush=True)
        return record


def verify_clone_identity(identity: dict, plan: BackupPlan) -> None:
    require(identity["owner"] == plan.run_id, "clone_ownership_mismatch")
    require(identity["state_desc"] == "ONLINE" and identity["user_access_desc"] == "RESTRICTED_USER", "clone_not_restricted")
    require(identity["public_connect_denied"] and not identity["is_broker_enabled"] and not identity["is_trustworthy_on"] and not identity["is_db_chaining_on"], "clone_access_isolation_failed")
    require(identity["restore_file_paths_sha256"] == plan.public()["restore_file_paths_sha256"], "clone_file_identity_mismatch")


def execute_verification(admin: SqlAdmin, plan: BackupPlan, journal: Journal) -> dict:
    stage = "pre_backup"
    try:
        # Source, ownership, space and collisions are fresh, not reused plan evidence.
        fresh = build_plan(admin, plan.run_id, plan.headroom_multiplier)
        stable_keys = set(plan.public()) - {"volume_free_bytes"}
        require(all(fresh.public()[key] == plan.public()[key] for key in stable_keys), "plan_changed_replan_required")
        require(fresh.files == plan.files, "source_files_changed_replan_required")
        source_schema = admin.schema_digest(SOURCE)
        journal.write("started", verified=False)
        stage = "backup"
        admin.command(f"BACKUP DATABASE {identifier(SOURCE)} TO DISK={literal(plan.backup_path)} WITH COPY_ONLY, COMPRESSION, CHECKSUM, STOP_ON_ERROR, NOINIT, NOSKIP")
        journal.write("backup_complete", backup_retained=True)
        stage = "backup_header"
        headers = admin.rows(f"RESTORE HEADERONLY FROM DISK={literal(plan.backup_path)}")
        require(len(headers) == 1 and headers[0]["DatabaseName"] == SOURCE and headers[0]["BackupType"] == 1 and headers[0]["IsCopyOnly"] and headers[0]["HasBackupChecksums"] and not headers[0]["IsDamaged"], "backup_header_mismatch")
        filelist = admin.rows(f"RESTORE FILELISTONLY FROM DISK={literal(plan.backup_path)} WITH FILE=1")
        require({(r["LogicalName"], int(r["FileId"]), r["Type"]) for r in filelist} == {(f["logical_name"], f["file_id"], f["type"]) for f in plan.files}, "backup_filelist_changed")
        sizes = {f["file_id"]: f["size_bytes"] for f in plan.files}
        require(all(0 < int(row["Size"]) <= sizes[int(row["FileId"])] for row in filelist), "backup_allocation_changed_replan_required")
        moves = ", ".join(f"MOVE {literal(f['logical_name'])} TO {literal(f['destination'])}" for f in plan.files)
        stage = "verifyonly"
        admin.command(f"RESTORE VERIFYONLY FROM DISK={literal(plan.backup_path)} WITH FILE=1, CHECKSUM, STOP_ON_ERROR, {moves}")
        journal.write("backup_verified", backup_checksum_verified=True)
        stage = "pre_restore"
        m = admin.metadata()
        require(m["source_guid"] == plan.source_guid and fingerprint(m["server_identity"]) == plan.server_fingerprint, "source_identity_changed")
        drives = {local_directory(str(PureWindowsPath(f["destination"]).parent)).drive.upper() for f in plan.files}
        require(all(m["free"].get(d, 0) >= math.ceil(plan.allocated_bytes * plan.headroom_multiplier) for d in drives), "insufficient_restore_headroom")
        check_collisions(admin, plan, backup_exists=True)
        stage = "restore"
        clone = identifier(plan.clone_name)
        # Never REPLACE. Every source file has a new, checked destination.
        admin.command(f"RESTORE DATABASE {clone} FROM DISK={literal(plan.backup_path)} WITH FILE=1, {moves}, RECOVERY, RESTRICTED_USER, CHECKSUM, STOP_ON_ERROR")
        stage = "restrict_clone"
        # public DENY does not block sysadmin or the database owner. This is
        # administrative isolation, not a boundary against SQL administrators.
        admin.command(f"USE {clone}; DENY CONNECT TO public; EXEC sys.sp_addextendedproperty @name={literal(OWNER_PROPERTY)}, @value={literal(plan.run_id)}; USE [master]")
        owner = admin.rows("SELECT SUSER_SNAME() AS current_admin")[0]["current_admin"]
        require(isinstance(owner, str) and bool(owner), "clone_owner_unavailable")
        admin.command(f"ALTER AUTHORIZATION ON DATABASE::{clone} TO {identifier(owner)}")
        admin.command(f"ALTER DATABASE {clone} SET DISABLE_BROKER WITH ROLLBACK IMMEDIATE")
        admin.command(f"ALTER DATABASE {clone} SET TRUSTWORTHY OFF")
        admin.command(f"ALTER DATABASE {clone} SET DB_CHAINING OFF")
        identity = admin.clone_identity(plan.clone_name)
        verify_clone_identity(identity, plan)
        journal.write("restore_restricted", clone_database_guid=identity["database_guid"], clone_database_id=identity["database_id"], clone_create_date=str(identity["create_date"]))
        stage = "checkdb"
        admin.command(f"DBCC CHECKDB ({clone}) WITH PHYSICAL_ONLY, NO_INFOMSGS, ALL_ERRORMSGS, MAXDOP=1")
        stage = "schema"
        restored_schema = admin.schema_digest(plan.clone_name)
        require(restored_schema == source_schema, "restored_schema_mismatch")
        stage = "attachment_reads"
        attachments = admin.attachment_checks(plan.clone_name)
        verify_clone_identity(admin.clone_identity(plan.clone_name), plan)
        return journal.write("verified", verified=True, backup_retained=True, restore_retained=True,
                             clone_database_guid=identity["database_guid"], clone_database_id=identity["database_id"],
                             clone_create_date=str(identity["create_date"]), dbcc_physical_only_completed=True,
                             source_schema_matches=True, schema=restored_schema, attachments=attachments,
                             backup_checksum_verified=True, backup_file_sha256_computed=False)
    except BaseException as exc:
        code = str(exc) if isinstance(exc, VerificationError) else "operation_failed"
        journal.write("failed", verified=False, failed_stage=stage, error_code=code,
                      backup_may_remain=True, restore_may_remain=True)
        raise VerificationError(code) from None


def read_journal_record(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), "verified_evidence_required")
    data = json.loads(path.read_text())
    digest = data.pop("record_sha256", None)
    require(fingerprint(data) == digest, "evidence_digest_mismatch")
    data["record_sha256"] = digest
    validate_run_id(data.get("run_id", ""))
    require(data.get("schema_version") == SCHEMA_VERSION, "evidence_schema_mismatch")
    require(data.get("restore_database") == CLONE_PREFIX + data["run_id"], "evidence_clone_mismatch")
    return data


def read_verified_evidence(path: Path) -> dict:
    data = read_journal_record(path)
    require(data.get("stage") == "verified" and data.get("verified") is True, "evidence_not_verified")
    return data


def read_existing_restore(path: Path) -> dict:
    """Require the complete original backup → restricted restore journal chain."""
    evidence = read_journal_record(path)
    require(evidence.get("stage") == "restore_restricted", "restricted_restore_evidence_required")
    require(re.fullmatch(r"[0-9a-f]{40}", evidence.get("execution_sha", ""))
            and re.fullmatch(r"[0-9]+", evidence.get("ci_run_id", "")), "original_execution_identity_required")
    previous = None
    context_keys = ("run_id", "restore_database", "backup_basename", "source_database_guid",
                    "server_fingerprint", "source_file_count", "allocated_bytes", "headroom_multiplier",
                    "restore_file_paths_sha256", "backup_path_sha256", "execution_sha", "ci_run_id")
    stages = ("started", "backup_complete", "backup_verified", "restore_restricted")
    for sequence, stage in enumerate(stages):
        sibling = path.parent / f"{evidence['run_id']}-{sequence:03d}-{stage}.json"
        record = read_journal_record(sibling)
        require(record.get("stage") == stage and record.get("previous_record_sha256") == previous,
                "original_evidence_chain_mismatch")
        require(all(record.get(key) == evidence[key] for key in context_keys), "original_evidence_context_mismatch")
        if stage == "backup_complete":
            require(record.get("backup_retained") is True, "original_backup_incomplete")
        if stage == "backup_verified":
            require(record.get("backup_checksum_verified") is True, "original_backup_not_verified")
        previous = record["record_sha256"]
    require(previous == evidence["record_sha256"], "original_restore_record_mismatch")
    return evidence


def inspect_existing_restore(admin: SqlAdmin, evidence: dict) -> tuple[str, dict, dict]:
    """Fresh identity check; never grants ownership from a name alone."""
    run_id = validate_run_id(evidence["run_id"])
    name = CLONE_PREFIX + run_id
    require(evidence["restore_database"] == name, "evidence_clone_mismatch")
    metadata = admin.metadata()
    require(metadata["is_sysadmin"] == 1 and fingerprint(metadata["server_identity"]) == evidence["server_fingerprint"]
            and metadata["source_guid"] == evidence["source_database_guid"], "revalidation_source_identity_mismatch")
    identity = admin.clone_identity(name)
    require(identity["owner"] == run_id and identity["database_guid"] == evidence["clone_database_guid"]
            and identity["database_id"] == evidence["clone_database_id"]
            and str(identity["create_date"]) == evidence["clone_create_date"], "revalidation_clone_identity_mismatch")
    require(identity["state_desc"] == "ONLINE" and identity["user_access_desc"] == "RESTRICTED_USER"
            and identity["public_connect_denied"] and not identity["is_broker_enabled"]
            and not identity["is_trustworthy_on"] and not identity["is_db_chaining_on"], "clone_access_isolation_failed")
    require(identity["restore_file_paths_sha256"] == evidence["restore_file_paths_sha256"], "clone_file_identity_mismatch")
    basename = BACKUP_PREFIX + run_id + ".bak"
    require(evidence["backup_basename"] == basename, "revalidation_backup_name_mismatch")
    backup = str(local_directory(metadata["backup_dir"] or "") / basename)
    found = admin.file_exists(backup)
    require(fingerprint(backup.casefold()) == evidence["backup_path_sha256"] and found["file"]
            and not found["directory"], "revalidation_backup_missing_or_changed")
    return backup, identity, metadata


def revalidate_existing_restore(admin: SqlAdmin, evidence: dict, journal: Journal) -> dict:
    """Recheck an owned restored copy. No BACKUP, database restore, ALTER or DROP."""
    stage = "existing_identity"
    try:
        backup, identity, _ = inspect_existing_restore(admin, evidence)
        name = evidence["restore_database"]
        journal.write("revalidation_started", verified=False)
        stage = "backup_metadata"
        headers = admin.rows(f"RESTORE HEADERONLY FROM DISK={literal(backup)}")
        require(len(headers) == 1 and headers[0]["DatabaseName"] == SOURCE and headers[0]["BackupType"] == 1
                and headers[0]["IsCopyOnly"] and headers[0]["HasBackupChecksums"] and not headers[0]["IsDamaged"], "backup_header_mismatch")
        history = admin.rows("""
            SELECT CONVERT(nvarchar(36),b.backup_set_uuid) AS backup_set_uuid,
              CONVERT(nvarchar(36),b.database_guid) AS source_guid,
              r.restore_type,r.[replace] AS used_replace,r.recovery,b.database_name,b.type AS backup_type
            FROM msdb.dbo.restorehistory r JOIN msdb.dbo.backupset b ON b.backup_set_id=r.backup_set_id
            WHERE r.destination_database_name=:name AND r.restore_type<>'V'
            ORDER BY r.restore_history_id
        """, {"name": name})
        require(len(history) == 1, "revalidation_restore_history_ambiguous")
        restored = history[0]
        require(restored["restore_type"] == "D" and restored["backup_type"] == "D"
                and restored["database_name"] == SOURCE and restored["used_replace"] == 0 and restored["recovery"] == 1
                and UUID(str(restored["backup_set_uuid"])) == UUID(str(headers[0]["BackupSetGUID"]))
                and UUID(str(restored["source_guid"])) == UUID(evidence["source_database_guid"])
                and UUID(str(headers[0]["BindingID"])) == UUID(evidence["source_database_guid"]),
                "revalidation_backup_restore_lineage_mismatch")
        filelist = admin.rows(f"RESTORE FILELISTONLY FROM DISK={literal(backup)} WITH FILE=1")
        files = admin.rows("SELECT file_id,name AS logical_name,type_desc,CONVERT(bigint,size)*8192 AS size_bytes FROM sys.master_files WHERE database_id=:id ORDER BY file_id", {"id": identity["database_id"]})
        require(len(files) == len(filelist) == evidence["source_file_count"]
                and all(f["type_desc"] in {"ROWS", "LOG"} for f in files), "revalidation_filelist_mismatch")
        require({(f["logical_name"], int(f["file_id"]), "D" if f["type_desc"] == "ROWS" else "L") for f in files}
                == {(f["LogicalName"], int(f["FileId"]), f["Type"]) for f in filelist}, "revalidation_filelist_mismatch")
        sizes = {f["file_id"]: int(f["size_bytes"]) for f in files}
        require(all(0 < int(f["Size"]) <= sizes[int(f["FileId"])] for f in filelist), "revalidation_backup_allocation_mismatch")
        stage = "verifyonly"
        admin.command(f"RESTORE VERIFYONLY FROM DISK={literal(backup)} WITH FILE=1, CHECKSUM, STOP_ON_ERROR")
        journal.write("backup_verified", backup_checksum_verified=True)
        stage = "checkdb"
        admin.command(f"DBCC CHECKDB ({identifier(name)}) WITH PHYSICAL_ONLY, NO_INFOMSGS, ALL_ERRORMSGS, MAXDOP=1")
        journal.write("physical_check_completed", dbcc_physical_only_completed=True)
        stage = "schema"
        schema = admin.schema_digest(name)
        require(schema == admin.schema_digest(SOURCE), "restored_schema_mismatch")
        stage = "attachment_reads"
        attachments = admin.attachment_checks(name)
        inspect_existing_restore(admin, evidence)
        return journal.write("verified", verified=True, backup_retained=True, restore_retained=True,
                             clone_database_guid=identity["database_guid"], clone_database_id=identity["database_id"],
                             clone_create_date=str(identity["create_date"]), dbcc_physical_only_completed=True,
                             source_schema_matches=True, schema=schema, attachments=attachments,
                             backup_checksum_verified=True, backup_file_sha256_computed=False,
                             revalidated_existing_restore=True, backup_restore_lineage_verified=True)
    except BaseException as exc:
        code = str(exc) if isinstance(exc, VerificationError) else "revalidation_failed"
        journal.write("failed", verified=False, failed_stage=stage, error_code=code,
                      backup_retained=True, restore_retained=True)
        raise VerificationError(code) from None


def cleanup_verified_clone(admin: SqlAdmin, evidence: dict, journal: Journal, *, execute: bool) -> dict:
    m = admin.metadata()
    require(m["is_sysadmin"] == 1 and fingerprint(m["server_identity"]) == evidence["server_fingerprint"] and m["source_guid"] == evidence["source_database_guid"], "cleanup_source_identity_mismatch")
    name = CLONE_PREFIX + validate_run_id(evidence["run_id"])
    identity = admin.clone_identity(name)
    require(identity["owner"] == evidence["run_id"] and identity["database_guid"] == evidence["clone_database_guid"] and identity["database_id"] == evidence["clone_database_id"] and str(identity["create_date"]) == evidence["clone_create_date"], "cleanup_ownership_mismatch")
    require(identity["restore_file_paths_sha256"] == evidence["restore_file_paths_sha256"] and identity["user_access_desc"] == "RESTRICTED_USER" and identity["public_connect_denied"], "cleanup_clone_changed")
    backup = str(local_directory(m["backup_dir"] or "") / (BACKUP_PREFIX + evidence["run_id"] + ".bak"))
    require(fingerprint(backup.casefold()) == evidence["backup_path_sha256"] and admin.file_exists(backup)["file"], "cleanup_backup_missing_or_changed")
    if not execute:
        return {"mode": "cleanup_plan", "restore_database": name, "owned_verified_clone": True, "backup_retained": True}
    journal.write("cleanup_started", cleanup_of_record_sha256=evidence["record_sha256"])
    try:
        admin.command(f"RESTORE VERIFYONLY FROM DISK={literal(backup)} WITH FILE=1, CHECKSUM, STOP_ON_ERROR")
        admin.command(f"ALTER DATABASE {identifier(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        admin.command(f"DROP DATABASE {identifier(name)}")
        require(not admin.database_exists(name), "cleanup_not_confirmed")
        return journal.write("cleaned", restore_removed=True, backup_retained=True)
    except BaseException as exc:
        code = str(exc) if isinstance(exc, VerificationError) else "cleanup_operation_failed"
        journal.write("cleanup_failed", error_code=code, restore_may_remain=True, backup_retained=True)
        raise VerificationError(code) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Mutation only after exact-SHA successful CI and operator authorization")
    parser.add_argument("--cleanup", type=Path, help="Verified JSON for owned clone; read-only preview unless --execute")
    parser.add_argument("--revalidate-existing", type=Path, help="Original restore_restricted journal; recheck existing copy without backup/restore/deletion")
    parser.add_argument("--keep-restore", action="store_true", default=True, help="Keep clone for further tests (default; cleanup is separate)")
    parser.add_argument("--run-id", default=None, help="UUID4 hex from a reviewed plan; otherwise generate a new UUID4")
    parser.add_argument("--headroom", type=float, default=2.0, help="Required free space / database allocation on each relevant volume; minimum 2")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--execution-sha")
    parser.add_argument("--ci-run-id")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        require(30 <= args.timeout_seconds <= 14400, "invalid_timeout")
        require(not (args.cleanup and args.revalidate_existing), "conflicting_operations")
        require(not (args.revalidate_existing and args.run_id), "existing_restore_run_id_is_fixed")
        if args.execute:
            require(bool(re.fullmatch(r"[0-9a-f]{40}", args.execution_sha or "")), "exact_execution_sha_required")
            require(bool(re.fullmatch(r"[0-9]+", args.ci_run_id or "")), "successful_ci_run_reference_required")
            require(args.evidence_dir is not None, "evidence_directory_required")
        with open_admin(args.timeout_seconds) as admin:
            if args.revalidate_existing:
                evidence = read_existing_restore(args.revalidate_existing)
                inspect_existing_restore(admin, evidence)
                if args.execute:
                    # Original evidence is never rewritten. Keep ownership run_id
                    # but require a fresh release evidence directory for this run.
                    require(args.evidence_dir.is_dir() and not any(args.evidence_dir.iterdir()), "fresh_revalidation_evidence_directory_required")
                    keys = ("run_id", "restore_database", "backup_basename", "source_database_guid",
                            "server_fingerprint", "source_file_count", "allocated_bytes", "headroom_multiplier",
                            "restore_file_paths_sha256", "backup_path_sha256")
                    context = {key: evidence[key] for key in keys}
                    context.update(execution_sha=args.execution_sha, ci_run_id=args.ci_run_id,
                                   original_execution_sha=evidence["execution_sha"], original_ci_run_id=evidence["ci_run_id"],
                                   original_restore_record_sha256=evidence["record_sha256"])
                    result = revalidate_existing_restore(admin, evidence, Journal(args.evidence_dir, context))
                else:
                    result = {"mode": "read_only_revalidation_plan", "restore_database": evidence["restore_database"], "verified": False}
            elif args.cleanup:
                evidence = read_verified_evidence(args.cleanup)
                context = {key: evidence[key] for key in ("run_id", "restore_database", "server_fingerprint", "source_database_guid")}
                context.update(execution_sha=args.execution_sha, ci_run_id=args.ci_run_id, cleanup_attempt=uuid4().hex)
                # A fresh run UUID for journal filenames prevents rewriting the
                # original verification records; ownership stays in evidence.
                if args.execute:
                    context["run_id"] = uuid4().hex
                journal = Journal(args.evidence_dir, context) if args.execute else None
                result = cleanup_verified_clone(admin, evidence, journal, execute=args.execute)
            else:
                plan = build_plan(admin, args.run_id or uuid4().hex, args.headroom)
                if args.execute:
                    journal = Journal(args.evidence_dir, {**plan.public(), "execution_sha": args.execution_sha, "ci_run_id": args.ci_run_id})
                    result = execute_verification(admin, plan, journal)
                else:
                    result = {"mode": "read_only_plan", "verified": False, **plan.public()}
            if not args.execute:
                print(json.dumps(result, default=str))
        return 0
    except BaseException as exc:
        code = str(exc) if isinstance(exc, VerificationError) else "verification_failed"
        print(json.dumps({"status": "failed", "error_code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
