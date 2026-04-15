from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.db.session import SessionLocal, init_db
from src.services.receipt_pipeline import ReceiptPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch test invoice parsing on local image files.")
    parser.add_argument("path", help="Path to an image file or directory with invoice images")
    args = parser.parse_args()

    root = Path(args.path)
    if not root.exists():
        raise SystemExit(f"Path not found: {root}")

    files = [root] if root.is_file() else sorted(
        item
        for item in root.iterdir()
        if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".xls", ".xlsx"}
    )
    if not files:
        raise SystemExit("No image files found")

    init_db()
    pipeline = ReceiptPipeline()
    with SessionLocal() as db:
        for file_path in files:
            receipt = pipeline.process_local_file(db, str(file_path))
            print(json.dumps(
                {
                    "file": file_path.name,
                    "status": receipt.status.value,
                    "summary": receipt.validation_summary,
                    "supplier_name": receipt.extracted_supplier_name,
                    "supplier_tax_id": receipt.extracted_supplier_tax_id,
                    "supplier_iban": receipt.extracted_supplier_iban,
                    "supplier_bank_name": receipt.extracted_supplier_bank_name,
                    "supplier_mfo": receipt.extracted_supplier_mfo,
                    "invoice_number": receipt.extracted_invoice_number,
                    "invoice_date": receipt.extracted_invoice_date,
                    "amount": str(receipt.extracted_amount) if receipt.extracted_amount is not None else None,
                    "currency": receipt.extracted_currency,
                    "payment_purpose": receipt.validation_payload.get("payment_purpose"),
                    "missing_fields": receipt.validation_payload.get("missing_fields"),
                },
                ensure_ascii=False,
            ))


if __name__ == "__main__":
    main()
