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
        f"Распарсенный результат [{_mode_label()}]:",
        f"Поставщик: {receipt.extracted_supplier_name or 'не найден'}",
        f"ЕДРПОУ/ИНН: {receipt.extracted_supplier_tax_id or 'не найден'}",
        f"IBAN: {receipt.extracted_supplier_iban or 'не найден'}",
        f"Банк: {receipt.extracted_supplier_bank_name or 'не найден'}",
        f"МФО: {receipt.extracted_supplier_mfo or 'не найден'}",
        f"Счет: {receipt.extracted_invoice_number or 'не найден'}",
        f"Дата: {receipt.extracted_invoice_date or 'не найдена'}",
        f"Сумма: {receipt.extracted_amount or 'не определена'} {receipt.extracted_currency or ''}",
        f"Категорія закупівлі: {PaymentPurposeBuilder.infer_category(validation_view)}",
        f"Призначення: {built_purpose}",
        f"Статус: {receipt.status.value}",
    ]
    missing_fields = receipt.validation_payload.get("missing_fields") or []
    if missing_fields:
        lines.append(f"Отсутствует/нечитаемо: {', '.join(missing_fields)}")
    if preflight_errors:
        lines.append(f"Preflight ошибки: {', '.join(preflight_errors)}")
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
            await message.answer("Доступ закрыт.")
            return
        await message.answer(
            f"Режим: {_mode_label()}.\nПришлите счет на оплату как фото, PDF, XLS или XLSX. Бот распарсит реквизиты, прогонит preflight-проверку, сохранит результат в БД и подготовит черновик платежа."
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
            await message.answer("Доступ закрыт.")
            return

        await message.answer("Фото получено. Разбираю счет на оплату и проверяю реквизиты.")
        with SessionLocal() as db:
            receipt = await pipeline.handle_photo(message.bot, db, message)

        if receipt.status == ReceiptStatus.unreadable:
            await message.answer(
                f"Счет не прошел проверку.\n\nПричина: {receipt.validation_summary}\n\nОтправьте более четкое фото."
            )
            return

        if receipt.status == ReceiptStatus.requires_manual_review:
            await message.answer(
                _render_receipt_result(receipt)
                + "\n\nПлатеж не создан. Документ требует ручной проверки реквизитов."
            )
            return

        lines = _render_receipt_result(receipt).splitlines()
        if receipt.status == ReceiptStatus.validated:
            lines.append("")
            lines.append("Реквизитов достаточно для распознавания, но черновик платежа пока не создан.")
            await message.answer("\n".join(lines))
            return

        lines.append("")
        if receipt.status == ReceiptStatus.dry_run_created:
            lines.append("Создан локальный dry-run черновик. В банк документ не отправлялся.")
        else:
            lines.append("Черновик платежа создан без подписи. Дальше его проверяет и подписывает человек.")
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
            await message.answer("Доступ закрыт.")
            return

        await message.answer("Файл получен. Разбираю счет на оплату и проверяю реквизиты.")
        try:
            with SessionLocal() as db:
                receipt = await pipeline.handle_document(message.bot, db, message)
        except ValueError:
            await message.answer("Поддерживаются только фото, PDF, XLS и XLSX.")
            return

        if receipt.status == ReceiptStatus.unreadable:
            await message.answer(
                f"Счет не прошел проверку.\n\nПричина: {receipt.validation_summary}\n\nОтправьте более четкое изображение."
            )
            return

        if receipt.status == ReceiptStatus.requires_manual_review:
            await message.answer(
                _render_receipt_result(receipt)
                + "\n\nПлатеж не создан. Документ требует ручной проверки реквизитов."
            )
            return

        lines = _render_receipt_result(receipt).splitlines()
        if receipt.status == ReceiptStatus.validated:
            lines.append("")
            lines.append("Реквизитов достаточно для распознавания, но черновик платежа пока не создан.")
            await message.answer("\n".join(lines))
            return

        lines.append("")
        if receipt.status == ReceiptStatus.dry_run_created:
            lines.append("Создан локальный dry-run черновик. В банк документ не отправлялся.")
        else:
            lines.append("Черновик платежа создан без подписи. Дальше его проверяет и подписывает человек.")
        await message.answer("\n".join(lines))

    @dp.message()
    async def fallback(message: Message) -> None:
        await message.answer("Ожидаю счет на оплату как фото, PDF, XLS или XLSX. Команда /start покажет краткую инструкцию.")
