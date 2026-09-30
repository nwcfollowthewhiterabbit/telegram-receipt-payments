from __future__ import annotations

from collections.abc import Mapping
from typing import Any


OPENAI_QUOTA_EXHAUSTED_MESSAGE = (
    "Не вдалося обробити рахунок: на балансі OpenAI (ChatGPT API) "
    "закінчилися кошти. Повідомте адміністратора та повторіть надсилання "
    "після поповнення балансу. Платіж і банківську чернетку не створено."
)

_QUOTA_MARKERS = (
    "insufficient_quota",
    "you have no credits remaining",
    "exceeded your current quota",
)


def _contains_quota_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_quota_marker(key) or _contains_quota_marker(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_quota_marker(item) for item in value)
    if value is None:
        return False
    text = str(value).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def is_openai_quota_exhausted(error: BaseException) -> bool:
    """Recognize exhausted billing quota without treating every HTTP 429 as billing failure."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _contains_quota_marker(getattr(current, "code", None)):
            return True
        if _contains_quota_marker(getattr(current, "body", None)):
            return True
        if _contains_quota_marker(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


def upload_failure_message(error: BaseException, generic_message: str) -> str:
    if is_openai_quota_exhausted(error):
        return OPENAI_QUOTA_EXHAUSTED_MESSAGE
    return generic_message
