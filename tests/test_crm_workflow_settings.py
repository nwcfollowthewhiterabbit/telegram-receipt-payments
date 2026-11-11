import unittest
from src.config import Settings


class WorkflowSettingsTests(unittest.TestCase):
    def test_poll_and_attachment_bounds_reject_invalid_configuration(self):
        for field, values in [('crm_sync_poll_seconds', [0, 29, 901]),
                              ('crm_attachment_max_bytes', [0, -1, 20971521])]:
            for value in values:
                with self.subTest(field=field, value=value):
                    settings = Settings(telegram_bot_token='test', crm_provider='terrasoft_mssql')
                    setattr(settings, field, value)
                    with self.assertRaises(RuntimeError):
                        settings.validate()

    def test_enabled_workflow_requires_supported_crm_provider(self):
        with self.assertRaises(RuntimeError):
            Settings(telegram_bot_token='test', crm_provider='none', crm_files_and_posting_enabled=True).validate()
        Settings(telegram_bot_token='test', crm_provider='terrasoft_mssql', crm_files_and_posting_enabled=True).validate()
