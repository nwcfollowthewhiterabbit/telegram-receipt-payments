from __future__ import annotations

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from src.config import get_settings
from src.db.models import ActionType, ReceiptStatus
from src.db.session import SessionLocal
from src.services.audit import write_audit_log
from src.services.purpose_builder import PaymentPurposeBuilder
from src.services.receipt_pipeline import ReceiptPipeline
from src.services.schemas import ReceiptValidationResult


settings = get_settings()
pipeline = ReceiptPipeline()


def _is_allowed(user_id: int) -> bool:
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


def _mode_label() -> str:
    return "DRY RUN" if settings.privat24_dry_run else "LIVE"


def _render_receipt_result(receipt) -> str:
    validation_view = ReceiptValidationResult(
        readable=True,
        summary=receipt.validation_summary or "",
        supplier_name=receipt.extracted_supplier_name,
        supplier_tax_id=receipt.extracted_supplier_tax_id,
        supplier_iban=receipt.extracted_supplier_iban,
        supplier_bank_name=receipt.extracted_supplier_bank_name,
        supplier_mfo=receipt.extracted_supplier_mfo,
        invoice_number=receipt.extracted_invoice_number,
        invoice_date=receipt.extracted_invoice_date,
        amount=receipt.extracted_amount,
        currency=receipt.extracted_currency,
        procurement_category=receipt.validation_payload.get("procurement_category"),
        payment_purpose=receipt.validation_payload.get("payment_purpose"),
        missing_fields=receipt.validation_payload.get("missing_fields") or [],
        raw_text=receipt.validation_payload.get("raw_text"),
    )
    built_purpose = PaymentPurposeBuilder.build(validation_view)
    preflight_errors = receipt.validation_payload.get("preflight_errors") or []
    lines = [
        f"Результат розпізнавання [{_mode_label()}]:",
        f"Постачальник: {receipt.extracted_supplier_name or 'не знайдено'}",
        f"ЄДРПОУ/ІПН: {receipt.extracted_supplier_tax_id or 'не знайдено'}",
        f"IBAN: {receipt.extracted_supplier_iban or 'не знайдено'}",
        f"Банк: {receipt.extracted_supplier_bank_name or 'не знайдено'}",
        f"МФО: {receipt.extracted_supplier_mfo or 'не знайдено'}",
        f"Рахунок: {receipt.extracted_invoice_number or 'не знайдено'}",
        f"Дата: {receipt.extracted_invoice_date or 'не знайдено'}",
        f"Сума: {receipt.extracted_amount or 'не визначено'} {receipt.extracted_currency or ''}",
        f"Категорія закупівлі: {PaymentPurposeBuilder.infer_category(validation_view)}",
        f"Призначення: {built_purpose}",
        f"Статус: {receipt.status.value}",
    ]
    missing_fields = receipt.validation_payload.get("missing_fields") or []
    if missing_fields:
        lines.append(f"Відсутнє або нечитабельне: {', '.join(missing_fields)}")
    if preflight_errors:
        lines.append(f"Помилки preflight: {', '.join(preflight_errors)}")
    return "\n".join(lines)


def register_handlers(dp: Dispatcher) -> None:
    @dp.message(Command("start"))
    async def handle_start(message: Message) -> None:
        if not _is_allowed(message.from_user.id):
            with SessionLocal() as db:
                write_audit_log(
                    db,
                    action_type=ActionType.access_denied,
                    telegram_user_id=message.from_user.id,
                    message="Unauthorized /start attempt",
                )
            await message.answer("Доступ заборонено.")
            return
        await message.answer(
            f"Режим: {_mode_label()}.\nНадішліть рахунок на оплату як фото, PDF, XLS або XLSX. Бот розпізнає реквізити, виконає preflight-перевірку, збереже результат у БД і підготує чернетку платежу."
        )

    @dp.message(F.photo)
    async def handle_photo(message: Message) -> None:
        if not _is_allowed(message.from_user.id):
            with SessionLocal() as db:
                write_audit_log(
                    db,
                    action_type=ActionType.access_denied,
                    telegram_user_id=message.from_user.id,
                    message="Unauthorized photo upload",
                )
            await message.answer("Доступ заборонено.")
            return

        await message.answer("Фото отримано. Розбираю рахунок на оплату та перевіряю реквізити.")
        with SessionLocal() as db:
            receipt = await pipeline.handle_photo(message.bot, db, message)

        if receipt.status == ReceiptStatus.unreadable:
            await message.answer(
                f"Рахунок не пройшов перевірку.\n\nПричина: {receipt.validation_summary}\n\nНадішліть чіткіше фото."
            )
            return

        if receipt.status == ReceiptStatus.requires_manual_review:
            await message.answer(
                _render_receipt_result(receipt)
                + "\n\nПлатіж не створено. Документ потребує ручної перевірки реквізитів."
            )
            return

        lines = _render_receipt_result(receipt).splitlines()
        if receipt.status == ReceiptStatus.validated:
            lines.append("")
            lines.append("Реквізитів достатньо для розпізнавання, але чернетку платежу поки не створено.")
            await message.answer("\n".join(lines))
            return

        lines.append("")
        if receipt.status == ReceiptStatus.dry_run_created:
            lines.append("Створено локальну dry-run чернетку. До банку документ не надсилався.")
        else:
            lines.append("Чернетку платежу створено без підпису. Далі її перевіряє та підписує людина.")
        await message.answer("\n".join(lines))

    @dp.message(F.document)
    async def handle_document(message: Message) -> None:
        if not _is_allowed(message.from_user.id):
            with SessionLocal() as db:
                write_audit_log(
                    db,
                    action_type=ActionType.access_denied,
                    telegram_user_id=message.from_user.id,
                    message="Unauthorized document upload",
                )
            await message.answer("Доступ заборонено.")
            return

        await message.answer("Файл отримано. Розбираю рахунок на оплату та перевіряю реквізити.")
        try:
            with SessionLocal() as db:
                receipt = await pipeline.handle_document(message.bot, db, message)
        except ValueError:
            await message.answer("Підтримуються лише фото, PDF, XLS і XLSX.")
            return

        if receipt.status == ReceiptStatus.unreadable:
            await message.answer(
                f"Рахунок не пройшов перевірку.\n\nПричина: {receipt.validation_summary}\n\nНадішліть чіткіше зображення."
            )
            return

        if receipt.status == ReceiptStatus.requires_manual_review:
            await message.answer(
                _render_receipt_result(receipt)
                + "\n\nПлатіж не створено. Документ потребує ручної перевірки реквізитів."
            )
            return

        lines = _render_receipt_result(receipt).splitlines()
        if receipt.status == ReceiptStatus.validated:
            lines.append("")
            lines.append("Реквізитів достатньо для розпізнавання, але чернетку платежу поки не створено.")
            await message.answer("\n".join(lines))
            return

        lines.append("")
        if receipt.status == ReceiptStatus.dry_run_created:
            lines.append("Створено локальну dry-run чернетку. До банку документ не надсилався.")
        else:
            lines.append("Чернетку платежу створено без підпису. Далі її перевіряє та підписує людина.")
        await message.answer("\n".join(lines))

    @dp.message()
    async def fallback(message: Message) -> None:
        await message.answer("Очікую рахунок на оплату як фото, PDF, XLS або XLSX. Команда /start покаже коротку інструкцію.")
