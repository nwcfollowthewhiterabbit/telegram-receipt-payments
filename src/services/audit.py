from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.models import ActionType, AuditLog


def write_audit_log(
    db: Session,
    action_type: ActionType,
    message: str,
    telegram_user_id: int | None = None,
    receipt_id: int | None = None,
    payload: dict | None = None,
) -> AuditLog:
    log = AuditLog(
        receipt_id=receipt_id,
        action_type=action_type,
        telegram_user_id=telegram_user_id,
        message=message,
        payload=payload or {},
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
