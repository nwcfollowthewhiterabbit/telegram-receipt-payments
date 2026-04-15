from __future__ import annotations

from pathlib import Path

import openpyxl
import xlrd
from pypdf import PdfReader


class DocumentTextExtractor:
    SUPPORTED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}

    def extract(self, file_path: str) -> str:
        path = Path(file_path)
        extension = path.suffix.lower()
        if extension == ".pdf":
            return self._extract_pdf(path)
        if extension == ".xlsx":
            return self._extract_xlsx(path)
        if extension == ".xls":
            return self._extract_xls(path)
        raise ValueError("unsupported_text_document_type")

    def _extract_pdf(self, path: Path) -> str:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                chunks.append(f"[Page {index}]\n{text}")
        return "\n\n".join(chunks)

    def _extract_xlsx(self, path: Path) -> str:
        workbook = openpyxl.load_workbook(path, data_only=True)
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() for value in row if value not in (None, "")]
                if values:
                    rows.append(" | ".join(values))
            if rows:
                chunks.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
        return "\n\n".join(chunks)

    def _extract_xls(self, path: Path) -> str:
        workbook = xlrd.open_workbook(path)
        chunks: list[str] = []
        for sheet in workbook.sheets():
            rows: list[str] = []
            for row_index in range(sheet.nrows):
                values = [str(value).strip() for value in sheet.row_values(row_index) if str(value).strip()]
                if values:
                    rows.append(" | ".join(values))
            if rows:
                chunks.append(f"[Sheet: {sheet.name}]\n" + "\n".join(rows))
        return "\n\n".join(chunks)
