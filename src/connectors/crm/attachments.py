from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid5
import xml.etree.ElementTree as ET

from sqlalchemy import text


class CrmConflict(RuntimeError):
    """Stable non-sensitive reason requiring an operator review."""


def safe_filename(value: str) -> str:
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        raise CrmConflict("attachment_filename_missing")
    stem, suffix = os.path.splitext(name)
    # SQL nvarchar(250) measures UTF-16 code units; retain the extension.
    while len((stem + suffix).encode("utf-16-le")) > 500:
        if not stem:
            raise CrmConflict("attachment_filename_invalid")
        stem = stem[:-1]
    return stem + suffix


@dataclass(frozen=True)
class CrmAttachment:
    role: str
    filename: str
    content: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if self.role not in {"invoice", "bank-receipt", "bank-statement"}:
            raise CrmConflict("attachment_role_invalid")
        if not isinstance(self.content, bytes) or not self.content:
            raise CrmConflict("attachment_empty")
        object.__setattr__(self, "filename", safe_filename(self.filename))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def file_id(self, operation_id: str) -> str:
        return str(uuid5(UUID(operation_id), "file:" + self.role))

    def marker(self, operation_id: str) -> str:
        return f"receipt-paybot:file:{operation_id}:{self.role}:{self.sha256}"


def load_original(receipt, storage_root: str, max_bytes: int, expected_hash: str | None = None) -> CrmAttachment:
    root = Path(storage_root).resolve(strict=True)
    candidate = Path(receipt.storage_path)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or candidate.is_symlink():
        raise CrmConflict("source_file_outside_storage")
    fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > max_bytes:
            raise CrmConflict("source_file_size_invalid")
        # Re-check the resolved name after opening. Storage is bot-owned and
        # writers use atomic files; symlinks are never accepted as originals.
        if candidate.resolve(strict=True) != resolved:
            raise CrmConflict("source_file_changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(max_bytes + 1)
        if len(content) != info.st_size or len(content) > max_bytes:
            raise CrmConflict("source_file_changed")
    finally:
        os.close(fd)
    attachment = CrmAttachment("invoice", receipt.original_filename, content, receipt.mime_type)
    if expected_hash and attachment.sha256 != expected_hash:
        raise CrmConflict("source_file_changed")
    return attachment


def native_xml(connection, code: str) -> ET.Element:
    raw = connection.execute(
        text("SELECT XMLData FROM dbo.tbl_Service WHERE Code=:code"), {"code": code}
    ).scalar_one()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            xml = stream.read(2_000_001)
        if len(xml) > 2_000_000:
            raise ValueError()
        return ET.fromstring(xml)
    except (OSError, ValueError, ET.ParseError, TypeError):
        raise CrmConflict("crm_native_metadata_invalid") from None


def inherit_file_rights(connection, operation_id: str, file_id: str) -> None:
    # Equivalent to native isq_GiveRightsByParentItem: only missing subjects.
    # Never broaden an existing file ACL or grant public/general access.
    connection.execute(text("""
        INSERT INTO dbo.tbl_FilesRight
            (ID,RecordID,AdminUnitID,CanRead,CanWrite,CanDelete,CanChangeAccess)
        SELECT NEWID(),CONVERT(uniqueidentifier,:file_id),r.AdminUnitID,
               MAX(r.CanRead),MAX(r.CanWrite),MAX(r.CanDelete),MAX(r.CanChangeAccess)
        FROM dbo.tbl_CashflowRight r
        WHERE r.RecordID=CONVERT(uniqueidentifier,:operation_id)
          AND NOT EXISTS (
            SELECT 1 FROM dbo.tbl_FilesRight f WITH (UPDLOCK,HOLDLOCK)
            WHERE f.RecordID=CONVERT(uniqueidentifier,:file_id) AND f.AdminUnitID=r.AdminUnitID
          )
        GROUP BY r.AdminUnitID
    """), {"file_id": file_id, "operation_id": operation_id})


def ensure_attachment(connection, operation_id: str, attachment: CrmAttachment, *, file_type: str,
                      max_bytes: int) -> dict:
    """Called inside the parent's locked transaction; all writes roll back together."""
    if len(attachment.content) > max_bytes:
        raise CrmConflict("attachment_too_large")
    file_id = attachment.file_id(operation_id)
    marker = attachment.marker(operation_id)
    params = {"file_id": file_id, "operation_id": operation_id}
    existing = connection.execute(text("""
        SELECT ItemTypeID,Link,Description,FileSize,CONVERT(varbinary(max),FileData) AS FileData
        FROM dbo.tbl_Files WITH (UPDLOCK,HOLDLOCK)
        WHERE ID=CONVERT(uniqueidentifier,:file_id)
    """), params).mappings().first()
    if existing:
        if (str(existing["ItemTypeID"]).casefold() != file_type.casefold()
                or existing["Description"] != marker
                or existing["Link"] != attachment.filename
                or existing["FileSize"] != len(attachment.content)
                or hashlib.sha256(existing["FileData"] or b"").hexdigest() != attachment.sha256):
            raise CrmConflict("crm_attachment_conflict")
    else:
        connection.execute(text("""
            INSERT INTO dbo.tbl_Files
                (ID,CreatedOn,ModifiedOn,ItemTypeID,Link,Description,Revision,FileData,FileSize)
            VALUES (CONVERT(uniqueidentifier,:file_id),GETUTCDATE(),GETUTCDATE(),
                    :file_type,:filename,:marker,1,:content,:size)
        """), {**params, "file_type": file_type, "filename": attachment.filename,
               "marker": marker, "content": attachment.content, "size": len(attachment.content)})
    links = connection.execute(text("""
        SELECT CONVERT(nvarchar(36),CashflowID) AS CashflowID
        FROM dbo.tbl_FileInCashflow WITH (UPDLOCK,HOLDLOCK) WHERE FileID=CONVERT(uniqueidentifier,:file_id)
    """), params).scalars().all()
    if links and (len(links) != 1 or str(links[0]).lower() != operation_id.lower()):
        raise CrmConflict("crm_attachment_parent_conflict")
    if not links:
        connection.execute(text("""
            INSERT INTO dbo.tbl_FileInCashflow (ID,CreatedOn,ModifiedOn,FileID,CashflowID)
            VALUES (CONVERT(uniqueidentifier,:link_id),GETUTCDATE(),GETUTCDATE(),
                    CONVERT(uniqueidentifier,:file_id),CONVERT(uniqueidentifier,:operation_id))
        """), {**params, "link_id": str(uuid5(UUID(operation_id), "link:" + attachment.role))})
    inherit_file_rights(connection, operation_id, file_id)
    stored = connection.execute(text("""
        SELECT f.ItemTypeID,f.Link,f.Description,f.FileSize,CONVERT(varbinary(max),f.FileData) AS FileData,
               (SELECT COUNT(*) FROM dbo.tbl_FileInCashflow a WHERE a.FileID=f.ID) AS AllParentLinks,
               (SELECT COUNT(*) FROM dbo.tbl_FileInCashflow l
                WHERE l.FileID=f.ID AND l.CashflowID=CONVERT(uniqueidentifier,:operation_id)) AS ParentLinks
        FROM dbo.tbl_Files f WHERE f.ID=CONVERT(uniqueidentifier,:file_id)
    """), params).mappings().first()
    if (not stored or stored["ParentLinks"] != 1 or stored["AllParentLinks"] != 1 or stored["Description"] != marker
            or stored["Link"] != attachment.filename or stored["FileSize"] != len(attachment.content)
            or str(stored["ItemTypeID"]).casefold() != file_type.casefold()
            or hashlib.sha256(stored["FileData"] or b"").hexdigest() != attachment.sha256):
        raise CrmConflict("crm_attachment_readback_failed")
    return {"id": file_id, "sha256": attachment.sha256, "size": len(attachment.content), "role": attachment.role}
