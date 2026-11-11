from __future__ import annotations

import copy
import gzip
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from src.connectors.crm.attachments import (
    CrmAttachment, CrmConflict, ensure_attachment, load_original, native_xml, safe_filename,
)
from src.services.crm_sync import read_confirmation, save_confirmation


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "storage"
        self.root.mkdir()
        self.source = self.root / "original.pdf"
        self.source.write_bytes(b"synthetic-original-bytes")
        self.receipt = SimpleNamespace(storage_path=str(self.source), original_filename="invoice.pdf",
                                       mime_type="application/pdf")
        self.op_id = str(uuid4())
        self.confirmation = CrmAttachment("bank-receipt", "receipt.pdf", b"%PDF-1.4\nsynthetic", "application/pdf")

    def test_filename_is_basename_and_retains_extension_with_utf16_limit(self):
        self.assertEqual(safe_filename("C:\\folder\\invoice?.pdf"), "invoice_.pdf")
        self.assertEqual(safe_filename("../../invoice\x00.pdf"), "invoice_.pdf")
        name = safe_filename("😀" * 300 + ".pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertLessEqual(len(name.encode("utf-16-le")), 500)
        with self.assertRaisesRegex(CrmConflict, "attachment_filename_missing"):
            safe_filename("../ . ")

    def test_attachment_identity_is_stable_by_operation_and_role_not_bytes(self):
        original = CrmAttachment("invoice", "invoice.pdf", b"one", "application/pdf")
        changed = CrmAttachment("invoice", "invoice.pdf", b"two", "application/pdf")
        self.assertEqual(original.file_id(self.op_id), changed.file_id(self.op_id))
        self.assertNotEqual(original.marker(self.op_id), changed.marker(self.op_id))
        self.assertNotEqual(original.file_id(self.op_id), self.confirmation.file_id(self.op_id))
        self.assertNotEqual(original.file_id(self.op_id), original.file_id(str(uuid4())))
        for role, content in (("other", b"one"), ("invoice", b""), ("invoice", "not-bytes")):
            with self.subTest(role=role, content=content), self.assertRaises(CrmConflict):
                CrmAttachment(role, "invoice.pdf", content, "application/pdf")

    def test_load_original_preserves_exact_bytes_and_pins_hash(self):
        value = load_original(self.receipt, str(self.root), 100)
        self.assertEqual(value.content, self.source.read_bytes())
        self.assertEqual(value.sha256, hashlib.sha256(value.content).hexdigest())
        self.source.write_bytes(b"changed-by-another-writer")
        with self.assertRaisesRegex(CrmConflict, "source_file_changed"):
            load_original(self.receipt, str(self.root), 100, value.sha256)

    def test_original_outside_and_symlink_escape_are_refused(self):
        outside = Path(self.tmp.name) / "outside.pdf"
        outside.write_bytes(b"outside")
        symlink = self.root / "link.pdf"
        symlink.symlink_to(self.source)
        parent = self.root / "outside-directory"
        parent.symlink_to(Path(self.tmp.name), target_is_directory=True)
        for path in (outside, symlink, parent / outside.name):
            with self.subTest(path=str(path)), self.assertRaisesRegex(CrmConflict, "source_file_outside_storage"):
                load_original(SimpleNamespace(**{**vars(self.receipt), "storage_path": str(path)}), str(self.root), 100)

    def test_empty_oversize_and_nonregular_originals_are_refused(self):
        for content, maximum in ((b"", 100), (b"12345", 4)):
            self.source.write_bytes(content)
            with self.subTest(content=content), self.assertRaisesRegex(CrmConflict, "source_file_size_invalid"):
                load_original(self.receipt, str(self.root), maximum)
        self.receipt.storage_path = str(self.root)
        with self.assertRaisesRegex(CrmConflict, "source_file_size_invalid"):
            load_original(self.receipt, str(self.root), 100)

    def test_confirmation_is_durable_idempotent_and_detects_manual_change(self):
        first = save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        second = save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(first, second)
        self.assertEqual(read_confirmation(str(self.root), first, 100), self.confirmation)
        path = Path(first["path"])
        path.write_bytes(b"manual-edit")
        with self.assertRaisesRegex(CrmConflict, "crm_confirmation_file_conflict"):
            save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        with self.assertRaisesRegex(CrmConflict, "source_file_changed"):
            read_confirmation(str(self.root), first, 100)
        self.assertEqual(path.read_bytes(), b"manual-edit")

    def test_confirmation_symlink_parent_is_refused_before_outside_directory_creation(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "_bank_confirmations").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(CrmConflict, "source_file_outside_storage"):
            save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(list(outside.iterdir()), [])

    def test_confirmation_operation_directory_and_file_symlinks_are_refused(self):
        parent = self.root / "_bank_confirmations"
        parent.mkdir()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        folder = parent / self.op_id
        folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(CrmConflict, "source_file_outside_storage"):
            save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(list(outside.iterdir()), [])
        folder.unlink()
        folder.mkdir()
        (folder / self.confirmation.sha256).symlink_to(self.source)
        with self.assertRaisesRegex(CrmConflict, "crm_confirmation_file_conflict"):
            save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(self.source.read_bytes(), b"synthetic-original-bytes")

    def test_failed_durable_write_removes_partial_file_and_retry_succeeds(self):
        with patch("src.services.crm_sync.os.fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(list((self.root / "_bank_confirmations" / self.op_id).iterdir()), [])
        saved = save_confirmation(str(self.root), self.op_id, self.confirmation, 100)
        self.assertEqual(Path(saved["path"]).read_bytes(), self.confirmation.content)

    def test_oversized_confirmation_never_creates_storage_directory(self):
        with self.assertRaisesRegex(CrmConflict, "attachment_too_large"):
            save_confirmation(str(self.root), self.op_id, self.confirmation, 2)
        self.assertFalse((self.root / "_bank_confirmations").exists())


class FileStoreConnection:
    """Transactional SQL boundary double, including persisted byte readback.

    This verifies connector decisions; SQL Server semantics are covered by the
    separate restore-clone integration gate, not claimed by these unit tests.
    """
    def __init__(self):
        self.file = None
        self.links = []
        self.writes = []
        self.readback_change = None

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        result = Mock()
        if sql.startswith("SELECT") and "ParentLinks" in sql:
            row = copy.deepcopy(self.file)
            if row:
                row["ParentLinks"] = self.links.count(params["operation_id"])
                row["AllParentLinks"] = len(self.links)
                if self.readback_change:
                    row.update(self.readback_change)
            result.mappings.return_value.first.return_value = row
        elif sql.startswith("SELECT") and "FROM dbo.tbl_Files " in sql:
            result.mappings.return_value.first.return_value = copy.deepcopy(self.file)
        elif sql.startswith("SELECT") and "FROM dbo.tbl_FileInCashflow " in sql:
            result.scalars.return_value.all.return_value = list(self.links)
        elif sql.startswith("INSERT INTO dbo.tbl_Files ("):
            self.writes.append("file")
            self.file = {"ItemTypeID": params["file_type"], "Link": params["filename"],
                         "Description": params["marker"], "FileData": params["content"], "FileSize": params["size"]}
        elif sql.startswith("INSERT INTO dbo.tbl_FileInCashflow "):
            self.writes.append("link")
            self.links.append(params["operation_id"])
        elif sql.startswith("INSERT INTO dbo.tbl_FilesRight "):
            self.writes.append("rights")
            self.rights_sql = sql
        else:
            raise AssertionError(f"Unexpected SQL shape: {sql[:120]}")
        return result


class NativeAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.op_id = str(uuid4())
        self.file_type = str(uuid4())
        self.file = CrmAttachment("invoice", "invoice.pdf", b"synthetic-file", "application/pdf")
        self.connection = FileStoreConnection()

    def attach(self):
        return ensure_attachment(self.connection, self.op_id, self.file, file_type=self.file_type, max_bytes=100)

    def test_retries_reuse_file_and_link_and_do_not_broaden_existing_acl(self):
        first = self.attach()
        second = self.attach()
        self.assertEqual(first, second)
        self.assertEqual(self.connection.writes.count("file"), 1)
        self.assertEqual(self.connection.writes.count("link"), 1)
        self.assertEqual(self.connection.file["FileData"], self.file.content)
        self.assertIn("NOT EXISTS", self.connection.rights_sql)
        self.assertIn("f.AdminUnitID=r.AdminUnitID", self.connection.rights_sql)
        self.assertNotIn("UPDATE", self.connection.rights_sql)

    def test_changed_existing_bytes_or_metadata_never_overwrites_file(self):
        for field, value in (("FileData", b"manual"), ("FileSize", 999), ("Description", "manual"),
                             ("Link", "renamed.pdf"), ("ItemTypeID", str(uuid4()))):
            with self.subTest(field=field):
                self.connection = FileStoreConnection()
                self.attach()
                self.connection.file[field] = value
                before = copy.deepcopy(self.connection.file)
                self.connection.writes.clear()
                with self.assertRaisesRegex(CrmConflict, "crm_attachment_conflict"):
                    self.attach()
                self.assertEqual(self.connection.file, before)
                self.assertEqual(self.connection.writes, [])

    def test_other_parent_or_duplicate_link_refused(self):
        self.attach()
        for links in ([str(uuid4())], [self.op_id, self.op_id], [self.op_id, str(uuid4())]):
            with self.subTest(links=links):
                self.connection.links = links
                self.connection.writes.clear()
                with self.assertRaisesRegex(CrmConflict, "crm_attachment_parent_conflict"):
                    self.attach()
                self.assertEqual(self.connection.writes, [])

    def test_persisted_readback_must_match_actual_bytes_metadata_and_single_parent(self):
        for change in ({"FileData": b"truncated"}, {"FileSize": 1}, {"Description": "rewritten"},
                       {"Link": "changed"}, {"ItemTypeID": str(uuid4())}, {"ParentLinks": 0}, {"ParentLinks": 2},
                       {"AllParentLinks": 2}):
            with self.subTest(change=change):
                self.connection = FileStoreConnection()
                self.connection.readback_change = change
                with self.assertRaisesRegex(CrmConflict, "crm_attachment_readback_failed"):
                    self.attach()

    def test_oversized_attachment_never_reaches_sql(self):
        connection = Mock()
        with self.assertRaisesRegex(CrmConflict, "attachment_too_large"):
            ensure_attachment(connection, self.op_id, self.file, file_type=self.file_type, max_bytes=1)
        connection.execute.assert_not_called()

    def test_native_metadata_malformed_and_decompression_limit_fail_closed(self):
        conn = Mock()
        conn.execute.return_value.scalar_one.return_value = gzip.compress(b"<schema><type/></schema>")
        self.assertEqual(native_xml(conn, "synthetic").tag, "schema")
        for raw in (b"not-gzip", gzip.compress(b"not-xml"), gzip.compress(b"x" * 2_000_001)):
            conn.execute.return_value.scalar_one.return_value = raw
            with self.subTest(length=len(raw)), self.assertRaisesRegex(CrmConflict, "crm_native_metadata_invalid"):
                native_xml(conn, "synthetic")


if __name__ == "__main__":
    unittest.main()
