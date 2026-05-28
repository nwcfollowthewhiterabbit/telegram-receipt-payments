from __future__ import annotations

from src.clients.monobank import MonobankClient
from src.clients.privat24 import Privat24Client
from src.config import Settings
from src.connectors.payments.base import PaymentConnector


def build_payment_connector(settings: Settings) -> PaymentConnector:
    if settings.payment_provider == "monobank":
        return MonobankClient()
    if settings.payment_provider == "privat24":
        return Privat24Client()
    raise RuntimeError(f"Unsupported payment provider: {settings.payment_provider}")
