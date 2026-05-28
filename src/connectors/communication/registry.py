from __future__ import annotations

from src.config import Settings
from src.connectors.communication.telegram import TelegramReceiptAdapter


def build_communication_adapter(settings: Settings) -> TelegramReceiptAdapter:
    if settings.communication_provider == "telegram":
        return TelegramReceiptAdapter(settings.receipt_storage_dir)
    raise RuntimeError(f"Unsupported communication provider: {settings.communication_provider}")
