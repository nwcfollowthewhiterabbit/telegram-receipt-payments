from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class StoredIncomingFile:
    telegram_user_id: int
    telegram_chat_id: int
    telegram_file_id: str
    original_filename: str
    storage_path: Path
    mime_type: str
