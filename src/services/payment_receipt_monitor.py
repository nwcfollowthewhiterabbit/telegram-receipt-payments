from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.orm import Session

from src.clients.privat24 import Privat24Client
from src.config import get_settings
from src.db.models import AuthorizedUser, PaymentDraft, Receipt
from src.db.session import SessionLocal


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReceiptNotificationJob:
    draft_id: int
    receipt_id: int
    chat_ids: list[int]
    filename: str
    content: bytes
    reference: str
    refn: str
    transaction: dict[str, Any]


class PaymentReceiptMonitor:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.settings = get_settings()
        self.privat24 = Privat24Client()

    async def run(self) -> None:
        while True:
            try:
                jobs = await asyncio.to_thread(self._collect_jobs)
                for job in jobs:
                    await self._deliver_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Payment receipt monitor cycle failed")
            await asyncio.sleep(max(30, self.settings.privat24_receipt_poll_seconds))

    def _collect_jobs(self) -> list[ReceiptNotificationJob]:
        if self.settings.privat24_dry_run or not self.settings.privat24_api_token:
            return []

        with SessionLocal() as db:
            pending_drafts = self._load_pending_drafts(db)
            if not pending_drafts:
                return []

            transactions = self.privat24.get_transactions(
                account=self.settings.privat24_source_account,
                start_date=dt.date.today() - dt.timedelta(days=max(1, self.settings.privat24_receipt_lookback_days)),
                end_date=dt.date.today(),
                limit=100,
            )
            jobs: list[ReceiptNotificationJob] = []
            for draft in pending_drafts:
                match = self._match_transaction(draft, transactions)
                if not match:
                    continue
                recipients = self._recipient_chat_ids(db, draft)
                if not recipients:
                    continue
                delivery = self._delivery_state(draft)
                already_sent = {int(chat_id) for chat_id in delivery.get("sent_to", {}).keys()}
                target_chat_ids = [chat_id for chat_id in recipients if chat_id not in already_sent]
                if not target_chat_ids:
                    if draft.status != "receipt_sent":
                        draft.status = "receipt_sent"
                        db.add(draft)
                        db.commit()
                    continue

                filename, content = self.privat24.print_receipt(
                    account=self.settings.privat24_source_account,
                    reference=match["REF"],
                    refn=str(match["REFN"]),
                )
                jobs.append(
                    ReceiptNotificationJob(
                        draft_id=draft.id,
                        receipt_id=draft.receipt_id,
                        chat_ids=target_chat_ids,
                        filename=filename,
                        content=content,
                        reference=str(match["REF"]),
                        refn=str(match["REFN"]),
                        transaction=match,
                    )
                )
            return jobs

    def _load_pending_drafts(self, db: Session) -> list[PaymentDraft]:
        drafts = (
            db.query(PaymentDraft)
            .join(Receipt, Receipt.id == PaymentDraft.receipt_id)
            .filter(PaymentDraft.provider_name == "privat24")
            .filter(~PaymentDraft.provider_payment_id.like("dry-run-%"))
            .order_by(PaymentDraft.id.asc())
            .all()
        )
        result: list[PaymentDraft] = []
        for draft in drafts:
            delivery = self._delivery_state(draft)
            if draft.status == "receipt_sent" and delivery.get("sent_to"):
                continue
            result.append(draft)
        return result

    @staticmethod
    def _delivery_state(draft: PaymentDraft) -> dict[str, Any]:
        payload = draft.provider_payload or {}
        delivery = payload.get("receipt_delivery")
        if isinstance(delivery, dict):
            return delivery
        return {}

    def _recipient_chat_ids(self, db: Session, draft: PaymentDraft) -> list[int]:
        chat_ids: list[int] = []
        receipt = draft.receipt
        if receipt and receipt.telegram_chat_id:
            chat_ids.append(receipt.telegram_chat_id)
        notify_phone = self.settings.privat24_receipt_notify_phone
        if notify_phone:
            accountant = db.query(AuthorizedUser).filter(AuthorizedUser.phone_number == notify_phone).first()
            if accountant and accountant.telegram_chat_id:
                chat_ids.append(accountant.telegram_chat_id)
        return list(dict.fromkeys(chat_ids))

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        return " ".join((value or "").upper().split())

    @staticmethod
    def _normalize_amount(value: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value.quantize(Decimal("0.01"))
        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(Decimal("0.01"))
        text = str(value).strip().replace(" ", "").replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None

    def _match_transaction(self, draft: PaymentDraft, transactions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        target_amount = self._normalize_amount(draft.amount)
        target_iban = self._normalize_text(draft.beneficiary_iban)
        target_purpose = self._normalize_text(draft.purpose)
        target_name = self._normalize_text(draft.beneficiary_name)

        for item in transactions:
            reference = str(item.get("REF") or "").strip()
            refn = str(item.get("REFN") or "").strip()
            if not reference or not refn or not refn.isdigit():
                continue
            account = self._normalize_text(item.get("AUT_CNTR_ACC"))
            if target_iban and account and target_iban != account:
                continue
            amount = self._normalize_amount(item.get("SUM"))
            if target_amount is not None and amount != target_amount:
                continue
            purpose = self._normalize_text(item.get("OSND"))
            if target_purpose and purpose and target_purpose not in purpose:
                continue
            name = self._normalize_text(item.get("AUT_CNTR_NAM"))
            if target_name and name and target_name not in name and name not in target_name:
                continue
            return item
        return None

    async def _deliver_job(self, job: ReceiptNotificationJob) -> None:
        sent_to: dict[str, str] = {}
        failed_to: dict[str, str] = {}
        caption = self._receipt_caption(job.transaction)
        for chat_id in job.chat_ids:
            try:
                document = BufferedInputFile(job.content, filename=job.filename)
                await self.bot.send_document(chat_id=chat_id, document=document, caption=caption)
                sent_to[str(chat_id)] = dt.datetime.utcnow().isoformat()
            except Exception as exc:
                failed_to[str(chat_id)] = str(exc)
                logger.exception("Failed to send payment receipt to chat %s", chat_id)
        await asyncio.to_thread(self._record_delivery_result, job, sent_to, failed_to)

    def _record_delivery_result(
        self,
        job: ReceiptNotificationJob,
        sent_to: dict[str, str],
        failed_to: dict[str, str],
    ) -> None:
        with SessionLocal() as db:
            draft = db.query(PaymentDraft).filter(PaymentDraft.id == job.draft_id).first()
            if not draft:
                return
            payload = dict(draft.provider_payload or {})
            delivery = dict(payload.get("receipt_delivery") or {})
            delivery_sent = dict(delivery.get("sent_to") or {})
            delivery_failed = dict(delivery.get("failed_to") or {})
            delivery_sent.update(sent_to)
            delivery_failed.update(failed_to)
            delivery.update(
                {
                    "reference": job.reference,
                    "refn": job.refn,
                    "matched_at": dt.datetime.utcnow().isoformat(),
                    "sent_to": delivery_sent,
                    "failed_to": delivery_failed,
                    "transaction": job.transaction,
                }
            )
            payload["receipt_delivery"] = delivery
            draft.provider_payload = payload
            recipients = self._recipient_chat_ids(db, draft)
            if recipients and all(str(chat_id) in delivery_sent for chat_id in recipients):
                draft.status = "receipt_sent"
            else:
                draft.status = "receipt_delivery_pending"
            db.add(draft)
            db.commit()

    @staticmethod
    def _receipt_caption(transaction: dict[str, Any]) -> str:
        amount = transaction.get("SUM") or "?"
        date = transaction.get("DAT_OD") or ""
        time = transaction.get("TIM_P") or ""
        recipient = transaction.get("AUT_CNTR_NAM") or "одержувач"
        return (
            "Банківська квитанція про виконаний платіж.\n"
            f"Одержувач: {recipient}\n"
            f"Сума: {amount}\n"
            f"Дата: {date} {time}".strip()
        )
