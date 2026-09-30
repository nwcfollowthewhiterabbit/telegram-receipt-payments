from __future__ import annotations

import unittest

from src.services.openai_errors import (
    OPENAI_QUOTA_EXHAUSTED_MESSAGE,
    is_openai_quota_exhausted,
    upload_failure_message,
)


class FakeOpenAIError(RuntimeError):
    def __init__(self, message: str, *, code=None, body=None) -> None:
        super().__init__(message)
        self.code = code
        self.body = body


class OpenAIQuotaErrorTest(unittest.TestCase):
    def test_recognizes_explicit_error_code(self) -> None:
        error = FakeOpenAIError("rate limit", code="insufficient_quota")

        self.assertTrue(is_openai_quota_exhausted(error))

    def test_recognizes_nested_openai_body(self) -> None:
        error = FakeOpenAIError(
            "request failed",
            body={"error": {"message": "You have no credits remaining.", "code": "insufficient_quota"}},
        )

        self.assertTrue(is_openai_quota_exhausted(error))

    def test_recognizes_wrapped_quota_error(self) -> None:
        cause = FakeOpenAIError("exceeded your current quota")
        wrapper = RuntimeError("vision processing failed")
        wrapper.__cause__ = cause

        self.assertTrue(is_openai_quota_exhausted(wrapper))

    def test_does_not_label_transient_rate_limit_as_empty_balance(self) -> None:
        error = FakeOpenAIError("Too many requests", code="rate_limit_exceeded")

        self.assertFalse(is_openai_quota_exhausted(error))
        self.assertEqual(upload_failure_message(error, "generic"), "generic")

    def test_returns_clear_user_message_without_raw_provider_error(self) -> None:
        error = FakeOpenAIError("private diagnostic", code="insufficient_quota")

        message = upload_failure_message(error, "generic")

        self.assertEqual(message, OPENAI_QUOTA_EXHAUSTED_MESSAGE)
        self.assertIn("закінчилися кошти", message)
        self.assertIn("не створено", message)
        self.assertNotIn("private diagnostic", message)


if __name__ == "__main__":
    unittest.main()
