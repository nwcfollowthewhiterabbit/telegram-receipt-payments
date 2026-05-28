from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv


load_dotenv()


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    result: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        result.append(int(chunk))
    return result


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("380") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+38{digits}"
    if value.strip().startswith("+") and digits:
        return f"+{digits}"
    return f"+{digits}" if digits else value.strip()


def _parse_phone_list(value: str | None) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        result.append(_normalize_phone(chunk))
    return result


def _parse_json_object(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("Expected a JSON object")
    return {str(key): str(item) for key, item in parsed.items()}


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "postgresql+psycopg://receiptbot:receiptbot@db:5432/receiptbot")
    )
    allowed_user_ids: list[int] = field(default_factory=lambda: _parse_int_list(os.getenv("ALLOWED_USER_IDS")))
    allowed_phone_numbers: list[str] = field(default_factory=lambda: _parse_phone_list(os.getenv("ALLOWED_PHONE_NUMBERS")))
    receipt_storage_dir: str = field(default_factory=lambda: os.getenv("RECEIPT_STORAGE_DIR", "/app/data/receipts"))
    payment_provider: str = field(default_factory=lambda: _env_or_default("PAYMENT_PROVIDER", "privat24").lower())
    payment_dry_run_override: str | None = field(default_factory=lambda: _optional_env("PAYMENT_DRY_RUN"))
    privat24_api_base_url: str = field(default_factory=lambda: _env_or_default("PRIVAT24_API_BASE_URL", "https://acp.privatbank.ua"))
    privat24_api_token: str = field(default_factory=lambda: os.getenv("PRIVAT24_API_TOKEN", ""))
    privat24_source_account: str = field(default_factory=lambda: os.getenv("PRIVAT24_SOURCE_ACCOUNT", ""))
    privat24_payment_date: str = field(
        default_factory=lambda: _env_or_default("PRIVAT24_PAYMENT_DATE", dt.date.today().strftime("%d.%m.%Y"))
    )
    privat24_payment_accept_date: str = field(
        default_factory=lambda: _env_or_default("PRIVAT24_PAYMENT_ACCEPT_DATE", dt.date.today().strftime("%d.%m.%Y"))
    )
    privat24_dry_run: bool = field(default_factory=lambda: _parse_bool(os.getenv("PRIVAT24_DRY_RUN"), True))
    privat24_receipt_poll_seconds: int = field(
        default_factory=lambda: int(_env_or_default("PRIVAT24_RECEIPT_POLL_SECONDS", "60"))
    )
    privat24_receipt_lookback_days: int = field(
        default_factory=lambda: int(_env_or_default("PRIVAT24_RECEIPT_LOOKBACK_DAYS", "3"))
    )
    privat24_receipt_notify_phone: str = field(
        default_factory=lambda: _normalize_phone(os.getenv("PRIVAT24_RECEIPT_NOTIFY_PHONE", ""))
    )
    monobank_api_base_url: str = field(
        default_factory=lambda: _env_or_default("MONOBANK_API_BASE_URL", "https://corp-api.monobank.ua")
    )
    monobank_api_token: str = field(default_factory=lambda: os.getenv("MONOBANK_API_TOKEN", ""))
    monobank_source_iban: str = field(default_factory=lambda: os.getenv("MONOBANK_SOURCE_IBAN", ""))
    monobank_dry_run: bool = field(default_factory=lambda: _parse_bool(os.getenv("MONOBANK_DRY_RUN"), True))
    crm_provider: str = field(default_factory=lambda: _env_or_default("CRM_PROVIDER", "none").lower())
    crm_dry_run: bool = field(default_factory=lambda: _parse_bool(os.getenv("CRM_DRY_RUN"), True))
    terrasoft_mssql_url: str = field(default_factory=lambda: os.getenv("TERRASOFT_MSSQL_URL", ""))
    terrasoft_invoice_table: str = field(default_factory=lambda: os.getenv("TERRASOFT_INVOICE_TABLE", ""))
    terrasoft_database: str = field(default_factory=lambda: _env_or_default("TERRASOFT_DATABASE", "Terrasoft_test"))
    terrasoft_column_map: dict[str, str] = field(
        default_factory=lambda: _parse_json_object(os.getenv("TERRASOFT_COLUMN_MAP"))
    )
    communication_provider: str = field(default_factory=lambda: _env_or_default("COMMUNICATION_PROVIDER", "telegram").lower())
    company_name: str = field(default_factory=lambda: os.getenv("COMPANY_NAME", ""))
    default_currency: str = field(default_factory=lambda: os.getenv("DEFAULT_CURRENCY", "UAH"))

    @property
    def payment_dry_run(self) -> bool:
        if self.payment_dry_run_override is not None:
            return _parse_bool(self.payment_dry_run_override, True)
        if self.payment_provider == "monobank":
            return self.monobank_dry_run
        return self.privat24_dry_run

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if self.payment_provider not in {"privat24", "monobank"}:
            raise RuntimeError("PAYMENT_PROVIDER must be either privat24 or monobank")
        if self.crm_provider not in {"none", "terrasoft_mssql"}:
            raise RuntimeError("CRM_PROVIDER must be either none or terrasoft_mssql")
        if self.communication_provider not in {"telegram"}:
            raise RuntimeError("COMMUNICATION_PROVIDER must be telegram")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
