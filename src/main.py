from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher

from src.bot.handlers import register_handlers
from src.config import get_settings
from src.db.session import init_db
from src.services.payment_receipt_monitor import PaymentReceiptMonitor


logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = get_settings()
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    register_handlers(dp)
    receipt_monitor = PaymentReceiptMonitor(bot)
    monitor_task = asyncio.create_task(receipt_monitor.run())
    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task


if __name__ == "__main__":
    asyncio.run(main())
