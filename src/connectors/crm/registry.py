from __future__ import annotations

from src.config import Settings
from src.connectors.crm.base import CrmConnector
from src.connectors.crm.none import NoopCrmConnector
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector


def build_crm_connector(settings: Settings) -> CrmConnector:
    if settings.crm_provider == "none":
        return NoopCrmConnector()
    if settings.crm_provider == "terrasoft_mssql":
        return TerrasoftMssqlConnector()
    raise RuntimeError(f"Unsupported CRM provider: {settings.crm_provider}")
