from __future__ import annotations

import asyncio
import logging

from aiogram.types import BufferedInputFile

from src.config import get_settings
from src.services.crm_sync import CrmSyncService


logger = logging.getLogger(__name__)


class CrmWorkflowMonitor:
    def __init__(self, bot):
        self.bot = bot
        self.settings = get_settings()
        self.service = CrmSyncService(settings=self.settings)

    async def run(self):
        if not self.settings.crm_files_and_posting_enabled or self.settings.crm_dry_run:
            return
        while True:
            try:
                await asyncio.to_thread(self.service.run_cycle)
                jobs = await asyncio.to_thread(self.service.pending_receipt_deliveries)
                for job in jobs:
                    attachment = job["attachment"]
                    for chat_id in job["chats"]:
                        try:
                            await self.bot.send_document(
                                chat_id=chat_id,
                                document=BufferedInputFile(attachment.content, filename=attachment.filename),
                                caption=f"Банківська квитанція. Оплату підтверджено банком. CRM: {job['cf_number']}.")
                            await asyncio.to_thread(self.service.record_receipt_delivery, job["operation_id"], chat_id)
                        except Exception:
                            logger.warning("Confirmed bank receipt delivery deferred operation=%s", job["operation_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                # Driver/provider exceptions can include credentials and data.
                logger.warning("CRM workflow monitor cycle deferred")
            await asyncio.sleep(max(30, self.settings.crm_sync_poll_seconds))
