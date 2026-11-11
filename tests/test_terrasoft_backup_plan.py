from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import copy
import io
import json
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import verify_terrasoft_backup as backup


RUN = "12345678123442348234123456789abc"
SOURCE_GUID = "11111111-2222-3333-4444-555555555555"
CLONE_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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
            return [dict(DatabaseName="Terrasoft", BackupType=1, IsCopyOnly=True, HasBackupChecksums=True, IsDamaged=False)]
        if "FILELISTONLY" in sql:
            return [dict(LogicalName="source_data", FileID=1, Type="D", Size=1000), dict(LogicalName="source_log", FileID=2, Type="L", Size=100)]
        if "SUSER_SNAME" in sql:
            return [{"current_admin": "PRIVATE_ADMIN_SENTINEL"}]
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


if __name__ == "__main__":
    unittest.main()
