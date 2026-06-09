from __future__ import annotations

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from src.config import get_settings
from src.connectors.communication.registry import build_communication_adapter
from src.db.models import ActionType, AuthorizedUser, Receipt, ReceiptStatus
from src.db.session import SessionLocal
from src.services.audit import write_audit_log
from src.services.purpose_builder import PaymentPurposeBuilder
from src.services.receipt_pipeline import ReceiptPipeline
from src.services.schemas import ReceiptValidationResult


settings = get_settings()
pipeline = ReceiptPipeline()
telegram_adapter = build_communication_adapter(settings)


def _is_allowed(user_id: int) -> bool:
    if settings.allowed_user_ids and user_id in settings.allowed_user_ids:
        return True
    if not settings.allowed_phone_numbers and not settings.allowed_user_ids:
        return True
    with SessionLocal() as db:
        return db.query(AuthorizedUser).filter(AuthorizedUser.telegram_user_id == user_id).first() is not None


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("380") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+38{digits}"
    return f"+{digits}" if digits else value.strip()


def _contact_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Надіслати мій номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _mode_label() -> str:
    mode = "DRY RUN" if settings.payment_dry_run else "LIVE"
    return f"{settings.payment_provider.upper()} / {mode}"


def _receipt_mode_label(receipt) -> str:
    payload = receipt.validation_payload or {}
    if payload.get("payment_ready") and not payload.get("payment_provider"):
        return "ОЧІКУЄ ВИБОРУ БАНКУ"
    provider = (payload.get("payment_provider") or settings.payment_provider).upper()
    execution_mode = payload.get("execution_mode")
    if execution_mode == "dry_run":
        return f"{provider} / DRY RUN"
    if execution_mode == "live":
        return f"{provider} / LIVE"
    return _mode_label()


def _payment_choice_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Приват24", callback_data=f"pay:privat24:{receipt_id}"),
                InlineKeyboardButton(text="monobank", callback_data=f"pay:monobank:{receipt_id}"),
            ]
        ]
    )


def _is_ready_for_payment_choice(receipt: Receipt) -> bool:
    return receipt.status == ReceiptStatus.validated and bool((receipt.validation_payload or {}).get("payment_ready"))


def _receipt_status_label(receipt: Receipt) -> str:
    if _is_ready_for_payment_choice(receipt):
        return "очікує вибору банку, платіж ще не створено"
    if receipt.status == ReceiptStatus.dry_run_created:
        return "dry-run чернетку створено"
    if receipt.status == ReceiptStatus.bank_created:
        return "банківську чернетку створено"
    if receipt.status == ReceiptStatus.payment_draft_failed:
        return "помилка створення чернетки"
    if receipt.status == ReceiptStatus.requires_manual_review:
        return "потрібна ручна перевірка"
    return receipt.status.value


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
        f"Результат розпізнавання [{_receipt_mode_label(receipt)}]:",
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
        f"Статус: {_receipt_status_label(receipt)}",
    ]
    missing_fields = receipt.validation_payload.get("missing_fields") or []
    if missing_fields:
        lines.append(f"Відсутнє або нечитабельне: {', '.join(missing_fields)}")
    if preflight_errors:
        lines.append(f"Помилки preflight: {', '.join(preflight_errors)}")
    payment_create_error = receipt.validation_payload.get("payment_create_error")
    if payment_create_error:
        lines.append(f"Помилка створення платежу: {payment_create_error}")
    payment_validation = receipt.validation_payload.get("payment_validation") or {}
    payment_validation_errors = payment_validation.get("errors") or []
    if payment_validation_errors:
        lines.append(f"Помилки контрольної перевірки: {', '.join(payment_validation_errors)}")
    elif payment_validation.get("summary"):
        lines.append(f"Контрольна перевірка: {payment_validation['summary']}")
    return "\n".join(lines)


async def _answer_receipt_result(message: Message, receipt: Receipt) -> None:
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
    lines.append("")
    if _is_ready_for_payment_choice(receipt):
        lines.append("Платіж ще НЕ створено і НЕ відправлено в банк.")
        lines.append("Щоб створити чернетку платежу, натисніть банк нижче:")
        await message.answer("\n".join(lines), reply_markup=_payment_choice_keyboard(receipt.id))
        return

    if receipt.status == ReceiptStatus.validated:
        lines.append("Реквізитів достатньо для розпізнавання, але чернетку платежу поки не створено.")
        await message.answer("\n".join(lines))
        return

    if receipt.status == ReceiptStatus.dry_run_created:
        lines.append("Створено локальну dry-run чернетку. До банку документ не надсилався.")
    elif receipt.status == ReceiptStatus.payment_draft_failed:
        lines.append("Чернетку платежу не створено. Спробуйте інший банк або перевірте реквізити.")
    else:
        lines.append("Чернетку платежу створено без підпису. Далі її перевіряє та підписує людина.")
    await message.answer("\n".join(lines))


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
            await message.answer(
                "Доступ до бота дозволено лише для авторизованих номерів. Надішліть свій контакт кнопкою нижче.",
                reply_markup=_contact_request_keyboard(),
            )
            return
        await message.answer(
            f"Поточний режим за замовчуванням: {_mode_label()}.\nНадішліть рахунок на оплату як фото, PDF, XLS або XLSX. Бот розпізнає реквізити, виконає preflight-перевірку, збереже результат у БД і запропонує вибрати Приват24 або monobank для чернетки платежу.",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(F.contact)
    async def handle_contact(message: Message) -> None:
        contact = message.contact
        if not contact:
            return
        if contact.user_id and contact.user_id != message.from_user.id:
            await message.answer(
                "Потрібно надіслати саме свій контакт через кнопку Telegram.",
                reply_markup=_contact_request_keyboard(),
            )
            return

        normalized_phone = _normalize_phone(contact.phone_number)
        if normalized_phone not in settings.allowed_phone_numbers:
            with SessionLocal() as db:
                write_audit_log(
                    db,
                    action_type=ActionType.access_denied,
                    telegram_user_id=message.from_user.id,
                    message="Unauthorized phone contact",
                    payload={"phone_number": normalized_phone},
                )
            await message.answer("Цей номер не має доступу до бота.", reply_markup=ReplyKeyboardRemove())
            return

        with SessionLocal() as db:
            existing = db.query(AuthorizedUser).filter(AuthorizedUser.telegram_user_id == message.from_user.id).first()
            if existing:
                existing.telegram_chat_id = message.chat.id
                existing.phone_number = normalized_phone
                db.add(existing)
                db.commit()
            else:
                db.add(
                    AuthorizedUser(
                        telegram_user_id=message.from_user.id,
                        telegram_chat_id=message.chat.id,
                        phone_number=normalized_phone,
                    )
                )
                db.commit()

        await message.answer(
            f"Доступ підтверджено для номера {normalized_phone}.\nНадішліть рахунок на оплату як фото, PDF, XLS або XLSX.",
            reply_markup=ReplyKeyboardRemove(),
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
            await message.answer(
                "Доступ заборонено. Спочатку підтвердьте номер телефону через /start.",
                reply_markup=_contact_request_keyboard(),
            )
            return

        await message.answer("Фото отримано. Розбираю рахунок на оплату та перевіряю реквізити.")
        incoming_file = await telegram_adapter.save_photo(message.bot, message)
        with SessionLocal() as db:
            receipt = pipeline.process_incoming_file(db, incoming_file)

        await _answer_receipt_result(message, receipt)

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
            await message.answer(
                "Доступ заборонено. Спочатку підтвердьте номер телефону через /start.",
                reply_markup=_contact_request_keyboard(),
            )
            return

        await message.answer("Файл отримано. Розбираю рахунок на оплату та перевіряю реквізити.")
        try:
            incoming_file = await telegram_adapter.save_document(message.bot, message)
            with SessionLocal() as db:
                receipt = pipeline.process_incoming_file(db, incoming_file)
        except ValueError:
            await message.answer("Підтримуються лише фото, PDF, XLS і XLSX.")
            return

        await _answer_receipt_result(message, receipt)

    @dp.callback_query(F.data.startswith("pay:"))
    async def handle_payment_provider_choice(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_allowed(callback.from_user.id):
            await callback.answer("Доступ заборонено.", show_alert=True)
            return

        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Некоректний вибір банку.", show_alert=True)
            return
        _, provider, receipt_id_raw = parts
        try:
            receipt_id = int(receipt_id_raw)
        except ValueError:
            await callback.answer("Некоректний рахунок.", show_alert=True)
            return

        with SessionLocal() as db:
            receipt = db.get(Receipt, receipt_id)
            if not receipt:
                await callback.answer("Рахунок не знайдено.", show_alert=True)
                return
            if receipt.telegram_user_id != callback.from_user.id:
                await callback.answer("Цей рахунок належить іншому користувачу.", show_alert=True)
                return
            if receipt.status in {ReceiptStatus.dry_run_created, ReceiptStatus.bank_created}:
                await callback.answer("Чернетку вже створено.", show_alert=True)
                return
            if not _is_ready_for_payment_choice(receipt) and receipt.status != ReceiptStatus.payment_draft_failed:
                await callback.answer("Рахунок ще не готовий до створення платежу.", show_alert=True)
                return

            await callback.answer("Створюю чернетку платежу...")
            receipt = pipeline.create_payment_draft_for_receipt(db, receipt, provider)

        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(_render_receipt_result(receipt))

    @dp.message()
    async def fallback(message: Message) -> None:
        if not _is_allowed(message.from_user.id):
            await message.answer(
                "Щоб користуватися ботом, підтвердьте свій номер телефону через /start.",
                reply_markup=_contact_request_keyboard(),
            )
            return
        await message.answer("Очікую рахунок на оплату як фото, PDF, XLS або XLSX. Команда /start покаже коротку інструкцію.")
