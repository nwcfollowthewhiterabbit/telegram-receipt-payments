from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.bot.handlers import register_handlers
from src.config import get_settings
from src.db.session import init_db


logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = get_settings()
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    register_handlers(dp)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
