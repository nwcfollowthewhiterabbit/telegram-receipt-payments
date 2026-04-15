from __future__ import annotations

import datetime as dt
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


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


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
    company_name: str = field(default_factory=lambda: os.getenv("COMPANY_NAME", ""))
    default_currency: str = field(default_factory=lambda: os.getenv("DEFAULT_CURRENCY", "UAH"))

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
