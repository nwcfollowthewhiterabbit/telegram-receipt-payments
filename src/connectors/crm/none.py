from __future__ import annotations

from src.db.models import PaymentDraft, Receipt
from src.services.schemas import CrmSyncResult


class NoopCrmConnector:
    provider_name = "none"

    def sync_receipt(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> CrmSyncResult:
        return CrmSyncResult(
            synced=False,
            provider_name=self.provider_name,
            status="crm_disabled",
            payload={
                "receipt_id": receipt.id,
                "payment_draft_id": payment_draft.id if payment_draft else None,
            },
        )
