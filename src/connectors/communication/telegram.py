from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from aiogram.types import Message

from src.connectors.communication.base import StoredIncomingFile
from src.services.document_text import DocumentTextExtractor


class TelegramReceiptAdapter:
    def __init__(self, receipt_storage_dir: str) -> None:
        self.receipt_storage_dir = Path(receipt_storage_dir)

    async def save_photo(self, bot: Bot, message: Message) -> StoredIncomingFile:
        photo = message.photo[-1]
        telegram_file = await bot.get_file(photo.file_id)
        self.receipt_storage_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.receipt_storage_dir / f"{message.from_user.id}_{photo.file_unique_id}.jpg"
        await bot.download_file(telegram_file.file_path, destination=str(target_path))
        return StoredIncomingFile(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            telegram_file_id=photo.file_id,
            original_filename=target_path.name,
            storage_path=target_path,
            mime_type="image/jpeg",
        )

    async def save_document(self, bot: Bot, message: Message) -> StoredIncomingFile:
        document = message.document
        mime_type = document.mime_type or ""
        extension = Path(document.file_name or "invoice.bin").suffix.lower()
        is_image = mime_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp"}
        is_supported_doc = extension in DocumentTextExtractor.SUPPORTED_EXTENSIONS
        if not is_image and not is_supported_doc:
            raise ValueError("unsupported_document_type")

        telegram_file = await bot.get_file(document.file_id)
        self.receipt_storage_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.receipt_storage_dir / f"{message.from_user.id}_{document.file_unique_id}{extension or '.bin'}"
        await bot.download_file(telegram_file.file_path, destination=str(target_path))
        return StoredIncomingFile(
            telegram_user_id=message.from_user.id,
            telegram_chat_id=message.chat.id,
            telegram_file_id=document.file_id,
            original_filename=document.file_name or target_path.name,
            storage_path=target_path,
            mime_type=mime_type,
        )
