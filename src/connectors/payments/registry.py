from __future__ import annotations

from src.clients.monobank import MonobankClient
from src.clients.privat24 import Privat24Client
from src.config import Settings
from src.connectors.payments.base import PaymentConnector


def build_payment_connector(settings: Settings, provider: str | None = None) -> PaymentConnector:
    provider_name = (provider or settings.payment_provider).lower()
    if provider_name == "monobank":
        return MonobankClient()
    if provider_name == "privat24":
        return Privat24Client()
    raise RuntimeError(f"Unsupported payment provider: {provider_name}")
