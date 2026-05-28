from __future__ import annotations

import ast
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _assert_no_aiogram_in_services() -> None:
    violations: list[str] = []
    for path in (ROOT / "src" / "services").glob("*.py"):
        imports = _imports(path)
        if any(item == "aiogram" or item.startswith("aiogram.") for item in imports):
            violations.append(str(path.relative_to(ROOT)))
    if violations:
        raise SystemExit(f"Service layer imports aiogram: {', '.join(violations)}")


def _assert_registries_build() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "architecture-check")
    os.environ.setdefault("PAYMENT_PROVIDER", "monobank")
    os.environ.setdefault("MONOBANK_DRY_RUN", "true")
    os.environ.setdefault("CRM_PROVIDER", "terrasoft_mssql")
    os.environ.setdefault("CRM_DRY_RUN", "true")

    from src.config import get_settings
    from src.connectors.crm.registry import build_crm_connector
    from src.connectors.payments.registry import build_payment_connector

    get_settings.cache_clear()
    settings = get_settings()
    payment = build_payment_connector(settings)
    crm = build_crm_connector(settings)
    if payment.provider_name != "monobank":
        raise SystemExit("Payment registry did not build monobank connector")
    if crm.provider_name != "terrasoft_mssql":
        raise SystemExit("CRM registry did not build terrasoft_mssql connector")


def main() -> None:
    _assert_no_aiogram_in_services()
    _assert_registries_build()
    print("architecture checks passed")


if __name__ == "__main__":
    main()
