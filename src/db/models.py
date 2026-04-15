from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ReceiptStatus(str, enum.Enum):
    uploaded = "uploaded"
    unreadable = "unreadable"
    validated = "validated"
    requires_manual_review = "requires_manual_review"
    dry_run_created = "dry_run_created"
    bank_created = "bank_created"
    payment_draft_created = "payment_draft_created"
    payment_draft_failed = "payment_draft_failed"


class ActionType(str, enum.Enum):
    receipt_uploaded = "receipt_uploaded"
    validation_completed = "validation_completed"
    preflight_failed = "preflight_failed"
    payment_draft_requested = "payment_draft_requested"
    payment_draft_created = "payment_draft_created"
    payment_draft_failed = "payment_draft_failed"
    access_denied = "access_denied"


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_chat_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    status: Mapped[ReceiptStatus] = mapped_column(Enum(ReceiptStatus), default=ReceiptStatus.uploaded, index=True)
    extracted_supplier_name: Mapped[str | None] = mapped_column(String(255))
    extracted_supplier_tax_id: Mapped[str | None] = mapped_column(String(32))
    extracted_supplier_iban: Mapped[str | None] = mapped_column(String(64))
    extracted_supplier_bank_name: Mapped[str | None] = mapped_column(String(255))
    extracted_supplier_mfo: Mapped[str | None] = mapped_column(String(32))
    extracted_invoice_number: Mapped[str | None] = mapped_column(String(64))
    extracted_invoice_date: Mapped[str | None] = mapped_column(String(32))
    extracted_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    extracted_currency: Mapped[str | None] = mapped_column(String(16))
    validation_summary: Mapped[str | None] = mapped_column(Text)
    validation_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )

    payment_drafts: Mapped[list["PaymentDraft"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")


class PaymentDraft(Base):
    __tablename__ = "payment_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), index=True)
    provider_name: Mapped[str] = mapped_column(String(64), default="privat24")
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    provider_pack_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source_account: Mapped[str | None] = mapped_column(String(64))
    beneficiary_name: Mapped[str] = mapped_column(String(255))
    beneficiary_tax_id: Mapped[str | None] = mapped_column(String(32))
    beneficiary_iban: Mapped[str | None] = mapped_column(String(64))
    beneficiary_bank_name: Mapped[str | None] = mapped_column(String(255))
    beneficiary_mfo: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(16), default="UAH")
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), default="created")
    provider_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    receipt: Mapped[Receipt] = relationship(back_populates="payment_drafts")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[ActionType] = mapped_column(Enum(ActionType), index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    receipt: Mapped[Receipt | None] = relationship(back_populates="audit_logs")
