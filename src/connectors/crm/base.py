from __future__ import annotations

from typing import Protocol

from src.db.models import PaymentDraft, Receipt
from src.services.schemas import CrmSyncResult


class CrmConnector(Protocol):
    provider_name: str

    def sync_receipt(self, receipt: Receipt, payment_draft: PaymentDraft | None) -> CrmSyncResult:
        raise NotImplementedError
