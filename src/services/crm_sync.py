"""Durable, transaction-scoped CRM workflow for new bank drafts only."""
from __future__ import annotations

from dataclasses import asdict
import datetime as dt
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from src.config import get_settings
from src.connectors.crm.attachments import CrmAttachment, CrmConflict, load_original
from src.connectors.crm.registry import build_crm_connector
from src.connectors.payments.settlement import SettlementEvidence, build_settlement_connector
from src.db.models import ActionType, AuditLog, AuthorizedUser, CrmOperation, PaymentDraft, Receipt
from src.db.session import SessionLocal


logger = logging.getLogger(__name__)


def enabled(settings, provider: str) -> bool:
    return bool(getattr(settings, "crm_files_and_posting_enabled", False)
                and settings.crm_provider == "terrasoft_mssql" and not settings.crm_dry_run
                and not settings.payment_dry_run_for(provider))


def enroll(db, receipt: Receipt, draft: PaymentDraft, settings) -> CrmOperation:
    """No commit: caller commits this outbox row with the bank draft."""
    if (not enabled(settings, draft.provider_name) or not draft.provider_payment_id
            or draft.provider_payment_id.startswith("dry-run-")):
        raise CrmConflict("crm_live_bank_draft_required")
    db.flush()
    op = CrmOperation(id=str(uuid4()), payment_draft_id=draft.id,
                      target_database=settings.terrasoft_database, state="pending_create",
                      payload={"version": 2}, attempts=0, next_attempt_at=dt.datetime.utcnow())
    db.add(op)
    receipt.validation_payload = {**(receipt.validation_payload or {}),
                                  "crm_operation_id": op.id, "crm_enhanced_state": op.state}
    return op


def error_code(exc: Exception) -> str:
    value = str(exc)
    if re.fullmatch(r"(?:crm|attachment|source|settlement|bank|monobank|privat24)_[a-z0-9_]{1,135}", value):
        return value
    return "crm_dependency_error"


def encode_evidence(evidence: SettlementEvidence) -> dict:
    result = asdict(evidence)
    result["amount"] = str(evidence.amount)
    result["paid_at"] = evidence.paid_at.isoformat()
    return result


def decode_evidence(value: dict) -> SettlementEvidence:
    payload = dict(value)
    payload["amount"] = Decimal(payload["amount"])
    payload["paid_at"] = dt.datetime.fromisoformat(payload["paid_at"])
    return SettlementEvidence(**payload)


def transaction_key(evidence: SettlementEvidence) -> str:
    raw = f"{evidence.provider}\n{evidence.source_account}\n{evidence.transaction_id}"
    return f"{evidence.provider}:{hashlib.sha256(raw.encode()).hexdigest()}"


def save_confirmation(root: str, operation_id: str, attachment: CrmAttachment, max_bytes: int) -> dict:
    if len(attachment.content) > max_bytes:
        raise CrmConflict("attachment_too_large")
    root_path = Path(root).resolve(strict=True)
    parent = root_path / "_bank_confirmations"
    if parent.is_symlink() or not parent.resolve().is_relative_to(root_path):
        raise CrmConflict("source_file_outside_storage")
    parent.mkdir(mode=0o700, exist_ok=True)
    folder = parent / str(UUID(operation_id))
    if folder.is_symlink() or not folder.resolve().is_relative_to(root_path):
        raise CrmConflict("source_file_outside_storage")
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / attachment.sha256
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != attachment.sha256:
            raise CrmConflict("crm_confirmation_file_conflict") from None
    else:
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(attachment.content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return {"path": str(path), "filename": attachment.filename, "role": attachment.role,
            "mime_type": attachment.mime_type, "sha256": attachment.sha256, "size": len(attachment.content)}


def read_confirmation(root: str, descriptor: dict, max_bytes: int) -> CrmAttachment:
    from types import SimpleNamespace
    value = load_original(SimpleNamespace(storage_path=descriptor["path"],
                          original_filename=descriptor["filename"], mime_type=descriptor["mime_type"]),
                          root, max_bytes, descriptor["sha256"])
    return CrmAttachment(descriptor["role"], value.filename, value.content, value.mime_type)


class CrmSyncService:
    def __init__(self, *, settings=None, session_factory=None, connector=None, bank_factory=None):
        self.settings = settings or get_settings()
        self.session_factory = session_factory or SessionLocal
        self.connector = connector or build_crm_connector(self.settings)
        self.bank_factory = bank_factory or build_settlement_connector

    @staticmethod
    def _audit(db, op: CrmOperation, message: str, payload: dict) -> None:
        draft = op.payment_draft
        db.add(AuditLog(receipt_id=draft.receipt_id, telegram_user_id=draft.receipt.telegram_user_id,
                        action_type=ActionType.validation_completed, message=message,
                        payload={"crm_operation_id": op.id, **payload}))

    @staticmethod
    def _publish_state(db, op: CrmOperation) -> None:
        receipt = op.payment_draft.receipt
        payload = dict(receipt.validation_payload or {})
        payload.update({"crm_operation_id": op.id, "crm_enhanced_state": op.state,
                        "crm_enhanced_error": op.last_error})
        if op.cf_number:
            payload.update({"crm_sync_status": "crm_synced", "crm_external_id": op.id,
                            "crm_cf_number": op.cf_number,
                            "crm_recipient_found": (op.payload or {}).get("recipient_found"),
                            "crm_source_attached": bool((op.payload or {}).get("source_attachment"))})
        receipt.validation_payload = payload
        db.add(receipt)

    def run_cycle(self) -> None:
        if not getattr(self.settings, "crm_files_and_posting_enabled", False) or self.settings.crm_dry_run:
            return
        with self.session_factory() as db:
            ids = [row[0] for row in db.query(CrmOperation.id)
                   .filter(CrmOperation.state != "posted", CrmOperation.next_attempt_at <= dt.datetime.utcnow())
                   .order_by(CrmOperation.next_attempt_at, CrmOperation.created_at).limit(30).all()]
        for operation_id in ids:
            self.step(operation_id)

    def prepare_and_create(self, operation_id: str) -> None:
        # Each phase commits its recoverable state before the next external call.
        for _ in range(2):
            if not self.step(operation_id, creation_only=True):
                break

    def step(self, operation_id: str, *, creation_only: bool = False) -> bool:
        try:
            with self.session_factory() as db:
                op = (db.query(CrmOperation).filter(CrmOperation.id == operation_id)
                      .with_for_update(skip_locked=True).first())
                if op is None or op.state == "posted":
                    return False
                draft = op.payment_draft
                if not enabled(self.settings, draft.provider_name):
                    return False
                if op.target_database != self.settings.terrasoft_database:
                    raise CrmConflict("crm_outbox_target_mismatch")
                if draft.provider_payment_id is None or draft.provider_payment_id.startswith("dry-run-"):
                    raise CrmConflict("crm_live_bank_draft_required")
                payload = dict(op.payload or {})
                if payload.get("version") != 2:
                    raise CrmConflict("crm_outbox_version_mismatch")
                if creation_only and op.state not in {"pending_create", "prepared"}:
                    return False
                before = op.state
                if before == "pending_create":
                    original = load_original(draft.receipt, self.settings.receipt_storage_dir,
                                             self.settings.crm_attachment_max_bytes, op.source_sha256)
                    payload["expected"] = self.connector.prepare_operation(op.id, draft.receipt, draft)
                    op.source_sha256 = original.sha256
                    op.state = "prepared"
                elif before == "prepared":
                    original = load_original(draft.receipt, self.settings.receipt_storage_dir,
                                             self.settings.crm_attachment_max_bytes, op.source_sha256)
                    result = self.connector.ensure_operation(payload["expected"], original)
                    payload.update(result)
                    op.cf_number = result["cf_number"]
                    op.state = "waiting_payment"
                elif before == "waiting_payment":
                    confirmed = self.bank_factory(self.settings, draft.provider_name).confirm_payment(
                        draft, f"RCPT{draft.receipt_id:04d}")
                    if confirmed is not None:
                        key = transaction_key(confirmed.evidence)
                        other = db.query(CrmOperation.id).filter(CrmOperation.bank_transaction_key == key,
                                                                CrmOperation.id != op.id).first()
                        if other:
                            raise CrmConflict("crm_bank_transaction_already_claimed")
                        if confirmed.receipt is not None:
                            attachment = CrmAttachment("bank-receipt", confirmed.receipt.filename,
                                                       confirmed.receipt.content, confirmed.receipt.mime_type)
                        elif draft.provider_name == "monobank":
                            content = json.dumps({"document_type": "bank_statement_data",
                                                  "provider": "monobank", "bank_data": confirmed.bank_payload},
                                                 ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                            attachment = CrmAttachment("bank-statement", f"monobank-{op.cf_number}-statement.json",
                                                       content, "application/json")
                        else:
                            raise CrmConflict("crm_bank_receipt_missing")
                        descriptor = save_confirmation(self.settings.receipt_storage_dir, op.id, attachment,
                                                       self.settings.crm_attachment_max_bytes)
                        op.bank_transaction_key = key
                        payload.update({"evidence": encode_evidence(confirmed.evidence),
                                        "bank_payload": confirmed.bank_payload, "confirmation_file": descriptor,
                                        "confirmed_at": dt.datetime.utcnow().isoformat()})
                        op.state = "confirmed"
                        db.flush()  # unique transaction claim before any CRM posting
                elif before == "confirmed":
                    evidence = decode_evidence(payload["evidence"])
                    if op.bank_transaction_key != transaction_key(evidence):
                        raise CrmConflict("crm_bank_claim_mismatch")
                    # A CRM outage must not turn an old observation into a
                    # later posting after the bank has reversed the debit.
                    fresh = self.bank_factory(self.settings, draft.provider_name).confirm_payment(
                        draft, f"RCPT{draft.receipt_id:04d}")
                    if fresh is None:
                        raise CrmConflict("crm_confirmation_no_longer_final")
                    if encode_evidence(fresh.evidence) != encode_evidence(evidence):
                        raise CrmConflict("crm_confirmation_changed")
                    attachment = read_confirmation(self.settings.receipt_storage_dir, payload["confirmation_file"],
                                                   self.settings.crm_attachment_max_bytes)
                    result = self.connector.post_operation(op.id, payload["expected"], evidence, attachment)
                    payload["posting"] = result
                    payload["recipient_found"] = True  # posting revalidated the current CRM recipient
                    payload["posted_at"] = dt.datetime.utcnow().isoformat()
                    op.state = "posted"
                else:
                    raise CrmConflict("crm_outbox_state_unknown")
                op.payload = payload
                op.last_error = None
                op.attempts = 0
                op.next_attempt_at = dt.datetime.utcnow() + dt.timedelta(
                    seconds=0 if op.state != before and op.state != "waiting_payment"
                    else max(30, self.settings.crm_sync_poll_seconds))
                self._publish_state(db, op)
                if op.state != before:
                    self._audit(db, op, "CRM workflow advanced", {"from": before, "to": op.state})
                db.commit()
                return op.state != before
        except Exception as exc:
            constraint = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", "")
            code = ("crm_bank_transaction_already_claimed"
                    if isinstance(exc, IntegrityError) and constraint == "crm_operations_bank_transaction_key_key"
                    else error_code(exc))
            logger.warning("CRM workflow deferred operation=%s reason=%s", operation_id, code)
            try:
                self._record_failure(operation_id, code)
            except Exception:
                logger.warning("CRM workflow could not persist retry state operation=%s", operation_id)
            return False

    def _record_failure(self, operation_id: str, code: str) -> None:
        with self.session_factory() as db:
            op = (db.query(CrmOperation).filter(CrmOperation.id == operation_id)
                  .with_for_update(skip_locked=True).first())
            if op is None or op.state == "posted":
                return
            changed = op.last_error != code
            op.last_error = code
            op.attempts += 1
            op.next_attempt_at = dt.datetime.utcnow() + dt.timedelta(seconds=min(900, 30 * 2 ** min(op.attempts, 5)))
            self._publish_state(db, op)
            if changed:
                self._audit(db, op, "CRM workflow requires retry or review", {"reason": code, "state": op.state})
            db.commit()

    def pending_receipt_deliveries(self) -> list[dict]:
        """Preserve existing Privat receipt delivery, independent of CRM success."""
        jobs = []
        with self.session_factory() as db:
            ops = (db.query(CrmOperation).join(PaymentDraft)
                   .filter(PaymentDraft.provider_name == "privat24", CrmOperation.bank_transaction_key.is_not(None))
                   .order_by(CrmOperation.created_at).all())
            for op in ops:
                payload = op.payload or {}
                descriptor = payload.get("confirmation_file")
                if not descriptor or descriptor.get("role") != "bank-receipt":
                    continue
                receipt = op.payment_draft.receipt
                chats = [receipt.telegram_chat_id] if receipt.telegram_chat_id else []
                if self.settings.privat24_receipt_notify_phone:
                    accountant = db.query(AuthorizedUser).filter(
                        AuthorizedUser.phone_number == self.settings.privat24_receipt_notify_phone).first()
                    if accountant and accountant.telegram_chat_id:
                        chats.append(accountant.telegram_chat_id)
                sent = payload.get("receipt_sent_to") or {}
                targets = [chat for chat in dict.fromkeys(chats) if str(chat) not in sent]
                if targets:
                    jobs.append({"operation_id": op.id, "cf_number": op.cf_number, "chats": targets,
                                 "attachment": read_confirmation(self.settings.receipt_storage_dir, descriptor,
                                                                 self.settings.crm_attachment_max_bytes)})
                if len(jobs) >= 20:
                    break
        return jobs

    def record_receipt_delivery(self, operation_id: str, chat_id: int) -> None:
        with self.session_factory() as db:
            op = db.query(CrmOperation).filter(CrmOperation.id == operation_id).with_for_update().one()
            payload = dict(op.payload or {})
            sent = dict(payload.get("receipt_sent_to") or {})
            sent[str(chat_id)] = dt.datetime.utcnow().isoformat()
            payload["receipt_sent_to"] = sent
            op.payload = payload
            # Retain the existing delivery checkpoint if the application is
            # rolled back; the legacy monitor must not resend acknowledged PDFs.
            draft = op.payment_draft
            provider_payload = dict(draft.provider_payload or {})
            delivery = dict(provider_payload.get("receipt_delivery") or {})
            legacy_sent = dict(delivery.get("sent_to") or {})
            legacy_sent.update(sent)
            statement = (payload.get("bank_payload") or {}).get("statement") or {}
            delivery.update({"sent_to": legacy_sent, "reference": statement.get("REF"),
                             "refn": statement.get("REFN"), "transaction": statement,
                             "matched_at": dt.datetime.utcnow().isoformat()})
            provider_payload["receipt_delivery"] = delivery
            draft.provider_payload = provider_payload
            chats = [draft.receipt.telegram_chat_id] if draft.receipt.telegram_chat_id else []
            if self.settings.privat24_receipt_notify_phone:
                accountant = db.query(AuthorizedUser).filter(
                    AuthorizedUser.phone_number == self.settings.privat24_receipt_notify_phone).first()
                if accountant and accountant.telegram_chat_id:
                    chats.append(accountant.telegram_chat_id)
            draft.status = "receipt_sent" if chats and all(str(chat) in legacy_sent for chat in chats) else "receipt_delivery_pending"
            db.commit()
