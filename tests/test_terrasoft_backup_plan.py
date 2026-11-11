from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import copy
from decimal import Decimal
import io
import json
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

from scripts import verify_terrasoft_backup as backup


RUN = "12345678123442348234123456789abc"
SOURCE_GUID = "11111111-2222-3333-4444-555555555555"
CLONE_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BACKUP_GUID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class FakeAdmin:
    def __init__(self):
        self.commands = []
        self.fail_on = None
        self.extra_files = set()
        self.existing_db = False
        self.clone = False
        self.backup_created = False
        self.owner = RUN
        self.guid = CLONE_GUID
        self.schema_mismatch = False
        self.private_identity = "PRIVATE_SERVER_SENTINEL"
        self.metadata_result = dict(
            version="15.0.2170.1", platform="Windows", server_identity=self.private_identity,
            source_guid=SOURCE_GUID, is_sysadmin=1, state_desc="ONLINE", is_read_only=False,
            backup_dir="D:\\private-backup", data_dir="D:\\private-data", log_dir="D:\\private-log",
            files=[dict(file_id=1, logical_name="source_data", type_desc="ROWS", physical_name="D:\\private-data\\existing.mdf", size_bytes=1000),
                   dict(file_id=2, logical_name="source_log", type_desc="LOG", physical_name="D:\\private-log\\existing.ldf", size_bytes=100)],
            free={"D:": 10000},
        )

    def metadata(self):
        return copy.deepcopy(self.metadata_result)

    def database_exists(self, name):
        return self.existing_db or self.clone

    def file_exists(self, path):
        directories = {self.metadata_result[k] for k in ("backup_dir", "data_dir", "log_dir")}
        existing = path in self.extra_files or (self.backup_created and path.endswith(".bak"))
        return {"file": existing, "directory": path in directories, "parent": True}

    def rows(self, sql, params=None):
        if "HEADERONLY" in sql:
            return [dict(DatabaseName="Terrasoft", BackupType=1, IsCopyOnly=True, HasBackupChecksums=True, IsDamaged=False,
                         BackupSetGUID=UUID(BACKUP_GUID), BindingID=UUID(SOURCE_GUID))]
        if "FILELISTONLY" in sql:
            # Column names/casing/types observed read-only on SQL Server 2019;
            # every value below is synthetic. In particular the field is FileId.
            return [dict(LogicalName=name, FileId=file_id, Type=kind, Size=size,
                         PhysicalName=f"D:\\synthetic\\source_{file_id}.bin",
                         UniqueId=UUID(int=file_id), CreateLSN=Decimal("0"),
                         FileGroupId=1 if kind == "D" else 0, IsPresent=True,
                         IsReadOnly=False, TDEThumbprint=None, SnapshotUrl=None)
                    for name, file_id, kind, size in
                    (("source_data", 1, "D", 1000), ("source_log", 2, "L", 100))]
        if "SUSER_SNAME" in sql:
            return [{"current_admin": "PRIVATE_ADMIN_SENTINEL"}]
        if "FROM sys.master_files" in sql:
            return copy.deepcopy(self.metadata_result["files"])
        if "FROM msdb.dbo.restorehistory" in sql:
            return [dict(backup_set_uuid=BACKUP_GUID, source_guid=SOURCE_GUID, restore_type="D",
                         used_replace=False, recovery=True, database_name="Terrasoft", backup_type="D")]
        raise AssertionError("unexpected fake query")

    def command(self, sql):
        self.commands.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("SQL_ERROR_PRIVATE_SECRET_AND_PATH")
        if sql.startswith("BACKUP DATABASE"):
            self.backup_created = True
        if sql.startswith("RESTORE DATABASE"):
            self.clone = True
        if sql.startswith("DROP DATABASE"):
            self.clone = False

    def clone_identity(self, name):
        files = [str(PureWindowsPath(self.metadata_result["data_dir"]) / f"{backup.CLONE_PREFIX}{RUN}_1.mdf"),
                 str(PureWindowsPath(self.metadata_result["log_dir"]) / f"{backup.CLONE_PREFIX}{RUN}_2.ldf")]
        return dict(owner=self.owner, database_guid=self.guid, database_id=9, create_date="2026-11-11 12:00:00",
                    state_desc="ONLINE", user_access_desc="RESTRICTED_USER", public_connect_denied=True,
                    is_broker_enabled=False, is_trustworthy_on=False, is_db_chaining_on=False,
                    restore_file_paths_sha256=backup.fingerprint(sorted(f.casefold() for f in files)))

    def schema_digest(self, name):
        return dict(catalog_rows=30, schema_sha256="b" * 64 if self.schema_mismatch and name != backup.SOURCE else "a" * 64)

    def attachment_checks(self, name):
        if getattr(self, "attachment_failure", False):
            raise RuntimeError("PRIVATE_DATABASE_IMAGE_AGGREGATE_ERROR")
        return dict(file_rows=2, blob_rows=2, blob_bytes=100, sampled_bodies=2, sample_hash_reads_valid=True)


@contextmanager
def fake_connection(admin):
    yield admin


class BackupPlanTest(unittest.TestCase):
    def test_default_cli_is_read_only_and_does_not_create_evidence(self):
        admin = FakeAdmin()
        stream = io.StringIO()
        with patch.object(backup, "open_admin", return_value=fake_connection(admin)), \
             patch.object(backup, "Journal") as journal, redirect_stdout(stream):
            self.assertEqual(backup.main(["--run-id", RUN]), 0)
        self.assertFalse(admin.commands)
        journal.assert_not_called()
        result = json.loads(stream.getvalue())
        self.assertEqual(result["mode"], "read_only_plan")
        self.assertFalse(result["verified"])
        self.assertNotIn("private-", stream.getvalue())
        self.assertNotIn(admin.private_identity, stream.getvalue())

    def test_production_guard_precedes_master_connection(self):
        connector = Mock()
        connector.settings.crm_provider = "terrasoft_mssql"
        connector.settings.crm_dry_run = False
        connector.settings.terrasoft_database = "Terrasoft"
        connector.verify_connection_target.side_effect = RuntimeError("private driver error")
        with patch.object(backup, "TerrasoftMssqlConnector", return_value=connector), patch.object(backup, "create_engine") as create:
            with self.assertRaises(RuntimeError):
                with backup.open_admin():
                    pass
        create.assert_not_called()

    def test_actual_target_mismatch_never_opens_master(self):
        connector = Mock()
        connector.settings.crm_provider = "terrasoft_mssql"
        connector.settings.crm_dry_run = False
        connector.settings.terrasoft_database = "Terrasoft"
        connector.verify_connection_target.return_value = "Terrasoft_test"
        with patch.object(backup, "TerrasoftMssqlConnector", return_value=connector), patch.object(backup, "create_engine") as create:
            with self.assertRaises(backup.VerificationError):
                with backup.open_admin():
                    pass
        create.assert_not_called()

    def test_all_collisions_fail_before_any_admin_mutation(self):
        for kind in ("database", "backup", "restore"):
            admin = FakeAdmin()
            if kind == "database":
                admin.existing_db = True
            elif kind == "backup":
                admin.extra_files.add(f"D:\\private-backup\\{backup.BACKUP_PREFIX}{RUN}.bak")
            else:
                admin.extra_files.add(f"D:\\private-data\\{backup.CLONE_PREFIX}{RUN}_1.mdf")
            with self.subTest(kind=kind), self.assertRaises(backup.VerificationError):
                backup.build_plan(admin, RUN)
            self.assertFalse(admin.commands)

    def test_low_space_and_unsupported_types_fail_closed(self):
        for change in ("space", "filestream", "sysadmin", "directory"):
            admin = FakeAdmin()
            if change == "space": admin.metadata_result["free"]["D:"] = 2199
            if change == "filestream": admin.metadata_result["files"][0]["type_desc"] = "FILESTREAM"
            if change == "sysadmin": admin.metadata_result["is_sysadmin"] = 0
            if change == "directory": admin.metadata_result["backup_dir"] = "\\\\testd\\backup"
            with self.subTest(change=change), self.assertRaises(backup.VerificationError):
                backup.build_plan(admin, RUN)
            self.assertFalse(admin.commands)

    def test_headroom_cannot_be_weakened_and_names_cannot_target_existing_databases(self):
        for value in (0, 1.9, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(backup.VerificationError):
                backup.build_plan(FakeAdmin(), RUN, value)
        for value in ("Terrasoft", "Terrasoft_test", "x" * 32, "0" * 32):
            with self.subTest(value=value), self.assertRaises(backup.VerificationError):
                backup.build_plan(FakeAdmin(), value)

    def test_execute_requires_ci_reference_before_any_connection(self):
        with patch.object(backup, "open_admin") as connect, redirect_stdout(io.StringIO()):
            self.assertEqual(backup.main(["--execute"]), 1)
        connect.assert_not_called()

    def test_cli_driver_errors_are_sanitized(self):
        output = io.StringIO()
        with patch.object(backup, "open_admin", side_effect=RuntimeError("PRIVATE_PASSWORD")), redirect_stdout(output):
            self.assertEqual(backup.main([]), 1)
        self.assertNotIn("PRIVATE_PASSWORD", output.getvalue())


class BackupExecutionTest(unittest.TestCase):
    def run_verification(self, admin, directory):
        plan = backup.build_plan(admin, RUN)
        journal = backup.Journal(directory, plan.public())
        with redirect_stdout(io.StringIO()):
            result = backup.execute_verification(admin, plan, journal)
        return plan, result

    def test_restore_sequence_keeps_backup_and_restricted_clone(self):
        admin = FakeAdmin()
        with tempfile.TemporaryDirectory() as directory:
            plan, result = self.run_verification(admin, Path(directory))
            records = list(Path(directory).glob("*.json"))
            verified = next(p for p in records if p.name.endswith("-verified.json"))
            self.assertEqual(backup.read_verified_evidence(verified)["run_id"], RUN)
            serialized = "".join(p.read_text() for p in records)
        self.assertTrue(result["verified"])
        self.assertTrue(admin.clone and admin.backup_created)
        sql = "\n".join(admin.commands)
        self.assertLess(sql.index("BACKUP DATABASE"), sql.index("RESTORE VERIFYONLY"))
        self.assertLess(sql.index("RESTORE VERIFYONLY"), sql.index("RESTORE DATABASE"))
        self.assertLess(sql.index("RESTRICTED_USER"), sql.index("DENY CONNECT TO public"))
        self.assertIn("COPY_ONLY, COMPRESSION, CHECKSUM", sql)
        self.assertNotIn("REPLACE", sql)
        self.assertNotIn("DROP DATABASE", sql)
        self.assertNotIn("ALTER DATABASE [Terrasoft]", sql)
        self.assertIn("MOVE N'source_data'", sql)
        self.assertIn("MOVE N'source_log'", sql)
        self.assertNotIn("PRIVATE_", serialized)
        self.assertNotIn("private-", serialized)

    def test_changed_free_space_is_rechecked_but_does_not_invalidate_identity(self):
        admin = FakeAdmin()
        plan = backup.build_plan(admin, RUN)
        admin.metadata_result["free"]["D:"] = 9999
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            result = backup.execute_verification(admin, plan, backup.Journal(Path(directory), plan.public()))
        self.assertTrue(result["verified"])

    def test_restore_or_checkdb_failure_never_auto_drops_clone_or_removes_backup(self):
        for failing_stage in ("RESTORE DATABASE", "DBCC CHECKDB"):
            admin = FakeAdmin()
            admin.fail_on = failing_stage
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                with self.assertRaises(backup.VerificationError):
                    self.run_verification(admin, Path(directory))
                output = "".join(p.read_text() for p in Path(directory).glob("*.json"))
            self.assertTrue(admin.backup_created)
            self.assertFalse(any("DROP DATABASE" in s for s in admin.commands))
            self.assertNotIn("SQL_ERROR_PRIVATE", output)
            self.assertIn('"stage":"failed"', output)

    def test_schema_mismatch_does_not_claim_verified_restore(self):
        admin = FakeAdmin()
        admin.schema_mismatch = True
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(backup.VerificationError, "schema_mismatch"):
            self.run_verification(admin, Path(directory))

    def test_fresh_source_file_change_stops_before_backup(self):
        admin = FakeAdmin()
        plan = backup.build_plan(admin, RUN)
        admin.metadata_result["files"][0]["logical_name"] = "new_logical_name"
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(backup.VerificationError, "source_files_changed"):
                backup.execute_verification(admin, plan, backup.Journal(Path(directory), plan.public()))
        self.assertFalse(admin.commands)

    def test_fresh_space_after_backup_blocks_restore_and_preserves_backup(self):
        admin = FakeAdmin()
        original = admin.command
        def command(sql):
            original(sql)
            if sql.startswith("BACKUP DATABASE"):
                admin.metadata_result["free"]["D:"] = 1
        admin.command = command
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(backup.VerificationError, "insufficient_restore_headroom"):
            self.run_verification(admin, Path(directory))
        self.assertTrue(admin.backup_created)
        self.assertFalse(any(s.startswith("RESTORE DATABASE") for s in admin.commands))

    def test_changed_backup_filelist_or_allocation_never_restores(self):
        for mutation in ("missing", "growth"):
            admin = FakeAdmin()
            original = admin.rows
            def rows(sql, params=None):
                result = original(sql, params)
                if "FILELISTONLY" in sql:
                    if mutation == "missing":
                        return result[:1]
                    result[0]["Size"] = 1001
                return result
            admin.rows = rows
            with tempfile.TemporaryDirectory() as directory, self.subTest(mutation=mutation), self.assertRaises(backup.VerificationError):
                self.run_verification(admin, Path(directory))
            self.assertTrue(admin.backup_created)
            self.assertFalse(any(s.startswith("RESTORE DATABASE") for s in admin.commands))

    def test_incorrect_file_id_column_case_is_not_an_accepted_contract(self):
        admin = FakeAdmin()
        original = admin.rows
        def rows(sql, params=None):
            result = original(sql, params)
            if "FILELISTONLY" in sql:
                for row in result:
                    row["FileID"] = row.pop("FileId")
            return result
        admin.rows = rows
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(backup.VerificationError):
            self.run_verification(admin, Path(directory))
        self.assertTrue(admin.backup_created)
        self.assertFalse(any(s.startswith("RESTORE DATABASE") for s in admin.commands))

    def test_cleanup_requires_marker_guid_paths_and_existing_backup(self):
        for mutation in ("owner", "guid", "filehash", "backup"):
            admin = FakeAdmin()
            with tempfile.TemporaryDirectory() as directory:
                _, evidence = self.run_verification(admin, Path(directory))
                if mutation == "owner": admin.owner = "someone_else"
                if mutation == "guid": admin.guid = SOURCE_GUID
                if mutation == "filehash": evidence["restore_file_paths_sha256"] = "0" * 64
                if mutation == "backup": admin.backup_created = False
                before = list(admin.commands)
                with self.subTest(mutation=mutation), self.assertRaises(backup.VerificationError):
                    backup.cleanup_verified_clone(admin, evidence, None, execute=False)
                self.assertEqual(admin.commands, before)

    def test_cleanup_plan_is_read_only_then_explicit_cleanup_drops_only_owned_clone(self):
        admin = FakeAdmin()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            _, evidence = self.run_verification(admin, Path(directory))
            before = list(admin.commands)
            preview = backup.cleanup_verified_clone(admin, evidence, None, execute=False)
            self.assertTrue(preview["owned_verified_clone"])
            self.assertEqual(admin.commands, before)
            journal = backup.Journal(Path(directory), {**evidence, "run_id": "f" * 32})
            backup.cleanup_verified_clone(admin, evidence, journal, execute=True)
        self.assertFalse(admin.clone)
        self.assertTrue(admin.backup_created)
        self.assertEqual(admin.commands[-1], f"DROP DATABASE [{backup.CLONE_PREFIX}{RUN}]")

    def test_journal_does_not_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            journal = backup.Journal(Path(directory), {"run_id": RUN})
            journal.write("started")
            with self.assertRaises(FileExistsError):
                backup.Journal(Path(directory), {"run_id": RUN}).write("started")

    def test_cleanup_backup_validation_failure_never_drops_clone(self):
        admin = FakeAdmin()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            _, evidence = self.run_verification(admin, Path(directory))
            admin.fail_on = "RESTORE VERIFYONLY"
            journal = backup.Journal(Path(directory), {"run_id": "f" * 32})
            with self.assertRaisesRegex(backup.VerificationError, "cleanup_operation_failed"):
                backup.cleanup_verified_clone(admin, evidence, journal, execute=True)
            self.assertTrue(list(Path(directory).glob("*-cleanup_failed.json")))
        self.assertTrue(admin.clone and admin.backup_created)
        self.assertFalse(any(s.startswith("DROP DATABASE") for s in admin.commands))


class ExistingRestoreRevalidationTest(unittest.TestCase):
    ORIGINAL_SHA = "a" * 40
    NEW_SHA = "b" * 40

    def fixture(self, root):
        old = root / "old"
        new = root / "new"
        old.mkdir(mode=0o700)
        new.mkdir(mode=0o700)
        admin = FakeAdmin()
        plan = backup.build_plan(admin, RUN)
        context = {**plan.public(), "execution_sha": self.ORIGINAL_SHA, "ci_run_id": "123"}
        admin.attachment_failure = True
        with redirect_stdout(io.StringIO()), self.assertRaises(backup.VerificationError):
            backup.execute_verification(admin, plan, backup.Journal(old, context))
        admin.attachment_failure = False
        admin.commands.clear()
        return admin, old / f"{RUN}-003-restore_restricted.json", new

    def execute(self, admin, old, new):
        with patch.object(backup, "open_admin", return_value=fake_connection(admin)), redirect_stdout(io.StringIO()):
            return backup.main(["--revalidate-existing", str(old), "--execute",
                                "--execution-sha", self.NEW_SHA, "--ci-run-id", "456", "--evidence-dir", str(new)])

    def test_default_revalidation_only_reads_identity_and_never_writes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            admin, old, new = self.fixture(Path(directory))
            output = io.StringIO()
            with patch.object(backup, "open_admin", return_value=fake_connection(admin)), redirect_stdout(output):
                self.assertEqual(backup.main(["--revalidate-existing", str(old)]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "read_only_revalidation_plan")
            self.assertFalse(admin.commands)
            self.assertFalse(list(new.iterdir()))

    def test_revalidation_rechecks_every_stage_without_backup_restore_or_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            admin, old, new = self.fixture(Path(directory))
            old_content = {p.name: p.read_bytes() for p in old.parent.iterdir()}
            prior = backup.read_existing_restore(old)
            self.assertEqual(self.execute(admin, old, new), 0)
            verified = backup.read_verified_evidence(new / f"{RUN}-003-verified.json")
            self.assertEqual(verified["execution_sha"], self.NEW_SHA)
            self.assertEqual(verified["ci_run_id"], "456")
            self.assertEqual(verified["original_execution_sha"], self.ORIGINAL_SHA)
            self.assertEqual(verified["original_ci_run_id"], "123")
            self.assertEqual(verified["original_restore_record_sha256"], prior["record_sha256"])
            self.assertTrue(verified["revalidated_existing_restore"])
            self.assertTrue(verified["backup_restore_lineage_verified"])
            self.assertTrue(verified["backup_checksum_verified"] and verified["dbcc_physical_only_completed"])
            self.assertEqual(old_content, {p.name: p.read_bytes() for p in old.parent.iterdir()})
        self.assertEqual(len(admin.commands), 2)
        self.assertTrue(admin.commands[0].startswith("RESTORE VERIFYONLY"))
        self.assertTrue(admin.commands[1].startswith("DBCC CHECKDB"))
        self.assertTrue(admin.clone and admin.backup_created)

    def test_original_chain_missing_tampered_or_relinked_is_rejected(self):
        for mutation in ("missing", "tampered", "relinked", "context", "wrong_stage"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(mutation=mutation):
                admin, old, new = self.fixture(Path(directory))
                selected = old.parent / f"{RUN}-001-backup_complete.json"
                if mutation == "missing":
                    selected.unlink()
                else:
                    data = json.loads(selected.read_text())
                    if mutation == "tampered": data["backup_retained"] = False
                    if mutation == "relinked": data["previous_record_sha256"] = "0" * 64
                    if mutation == "context": data["execution_sha"] = "c" * 40
                    if mutation == "wrong_stage": data["stage"] = "verified"
                    if mutation != "tampered":
                        data.pop("record_sha256")
                        data["record_sha256"] = backup.fingerprint(data)
                    selected.write_text(json.dumps(data))
                self.assertEqual(self.execute(admin, old, new), 1)
                self.assertFalse(admin.commands)
                self.assertFalse(list(new.iterdir()))

    def test_changed_clone_source_privileges_or_backup_blocks_all_physical_checks(self):
        for mutation in ("owner", "guid", "paths", "source", "server", "privileges", "backup"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(mutation=mutation):
                admin, old, new = self.fixture(Path(directory))
                if mutation == "owner": admin.owner = "different_owner"
                if mutation == "guid": admin.guid = SOURCE_GUID
                if mutation == "paths": admin.metadata_result["data_dir"] = "D:\\different"
                if mutation == "source": admin.metadata_result["source_guid"] = CLONE_GUID
                if mutation == "server": admin.metadata_result["server_identity"] = "different_server"
                if mutation == "privileges": admin.metadata_result["is_sysadmin"] = 0
                if mutation == "backup": admin.backup_created = False
                self.assertEqual(self.execute(admin, old, new), 1)
                self.assertFalse(admin.commands)
                self.assertFalse(list(new.iterdir()))

    def test_revalidation_cannot_overwrite_or_append_to_prior_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            admin, old, new = self.fixture(Path(directory))
            self.assertEqual(self.execute(admin, old, old.parent), 1)
            self.assertFalse(admin.commands)
            self.assertEqual(self.execute(admin, old, new), 0)
            admin.commands.clear()
            self.assertEqual(self.execute(admin, old, new), 1)
            self.assertFalse(admin.commands)

    def test_revalidation_rejects_backup_set_or_source_that_did_not_restore_this_clone(self):
        for mutation in ("backup_set_uuid", "source_guid", "BindingID", "used_replace", "restore_type", "recovery"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(mutation=mutation):
                admin, old, new = self.fixture(Path(directory))
                original = admin.rows
                def rows(sql, params=None):
                    result = original(sql, params)
                    if "FROM msdb.dbo.restorehistory" in sql:
                        if mutation in {"backup_set_uuid", "source_guid"}: result[0][mutation] = CLONE_GUID
                        if mutation == "used_replace": result[0][mutation] = True
                        if mutation == "restore_type": result[0][mutation] = "L"
                        if mutation == "recovery": result[0][mutation] = False
                    if "HEADERONLY" in sql and mutation == "BindingID": result[0][mutation] = UUID(CLONE_GUID)
                    return result
                admin.rows = rows
                self.assertEqual(self.execute(admin, old, new), 1)
                self.assertFalse(admin.commands)
                self.assertFalse(list(new.glob("*-verified.json")))

    def test_revalidation_requires_one_unambiguous_database_restore_history(self):
        for count in (0, 2):
            with tempfile.TemporaryDirectory() as directory, self.subTest(count=count):
                admin, old, new = self.fixture(Path(directory))
                original = admin.rows
                def rows(sql, params=None):
                    result = original(sql, params)
                    return result * count if "FROM msdb.dbo.restorehistory" in sql else result
                admin.rows = rows
                self.assertEqual(self.execute(admin, old, new), 1)
                self.assertFalse(admin.commands)
                self.assertFalse(list(new.glob("*-verified.json")))

    def test_failed_checksum_physical_or_attachment_reads_never_claim_success(self):
        for failure in ("RESTORE VERIFYONLY", "DBCC CHECKDB", "attachments"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(failure=failure):
                admin, old, new = self.fixture(Path(directory))
                if failure == "attachments": admin.attachment_failure = True
                else: admin.fail_on = failure
                self.assertEqual(self.execute(admin, old, new), 1)
                self.assertFalse(list(new.glob("*-verified.json")))
                failed = next(new.glob("*-failed.json"))
                output = failed.read_text()
                self.assertIn('"verified":false', output)
                self.assertNotIn("PRIVATE_", output)
                self.assertTrue(admin.clone and admin.backup_created)
                self.assertTrue(all(command.startswith(("RESTORE VERIFYONLY", "DBCC CHECKDB")) for command in admin.commands))

    def test_schema_change_prevents_attachment_reads_and_final_success(self):
        with tempfile.TemporaryDirectory() as directory:
            admin, old, new = self.fixture(Path(directory))
            admin.schema_mismatch = True
            self.assertEqual(self.execute(admin, old, new), 1)
            self.assertFalse(list(new.glob("*-verified.json")))
            self.assertEqual(len(admin.commands), 2)

    def test_native_image_blob_count_uses_nullable_integer_expression(self):
        admin = backup.SqlAdmin(Mock())
        def rows(sql, params=None):
            if "COUNT_BIG(FileData)" in sql:
                raise RuntimeError("synthetic_sql_8117_image_aggregate_unsupported")
            if "AS blob_rows" in sql:
                self.assertIn("COUNT_BIG(CASE WHEN FileData IS NOT NULL THEN 1 END)", sql)
                return [dict(file_rows=3, blob_rows=2, blob_bytes=20, size_metadata_mismatches=1)]
            if "TOP (3)" in sql:
                return [dict(bytes=10, digest="a" * 64)]
            if "AS link_rows" in sql:
                return [dict(link_rows=2, orphan_links=0)]
            return [dict(attachment_right_rows=2)]
        admin.rows = rows
        result = admin.attachment_checks(backup.CLONE_PREFIX + RUN)
        self.assertEqual(result["blob_rows"], 2)
        self.assertEqual(result["size_metadata_mismatches"], 1)
        self.assertTrue(result["sample_hash_reads_valid"])
        self.assertNotIn("digest", result)


if __name__ == "__main__":
    unittest.main()
