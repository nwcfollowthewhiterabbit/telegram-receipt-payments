from __future__ import annotations

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import backup_bot, deploy_bot


SHA = "a" * 40
CI = "12345"
OLD_IMAGE = "sha256:" + "1" * 64
NEW_IMAGE = "sha256:" + "2" * 64
RUN = "20261111T190000Z-a1234567"


def seal(value):
    unsigned = dict(value)
    unsigned.pop("record_sha256", None)
    raw = json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()
    return {**unsigned, "record_sha256": hashlib.sha256(raw).hexdigest()}


def crm_evidence():
    original = seal({"stage": "verified", "verified": True, "execution_sha": SHA, "ci_run_id": CI,
                     "backup_checksum_verified": True, "dbcc_physical_only_completed": True,
                     "source_schema_matches": True, "restore_database": "ReceiptFlowRestore_synthetic"})
    rehearsal = seal({"stage": "rehearsal_passed", "verified": True, "passed": True,
                      "execution_sha": SHA, "ci_run_id": CI, "all_fixture_writes_rolled_back": True,
                      "backup_record_sha256": original["record_sha256"],
                      "restore_database": original["restore_database"]})
    return original, rehearsal


class EvidenceContractTest(unittest.TestCase):
    def test_rehearsal_requires_same_exact_backup_clone_source_and_cleanup(self):
        original, rehearsal = crm_evidence()
        deploy_bot.validate_crm_evidence(original, rehearsal, SHA, CI)
        for field, replacement in (("stage", "rehearsal_started"), ("passed", "true"),
                                   ("verified", False), ("ci_run_id", "999"),
                                   ("backup_record_sha256", "b" * 64),
                                   ("restore_database", "unrelated_clone"),
                                   ("all_fixture_writes_rolled_back", False)):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "crm_rehearsal_not_passed"):
                deploy_bot.validate_crm_evidence(original, {**rehearsal, field: replacement}, SHA, CI)
        for field, replacement in (("execution_sha", "b" * 40), ("ci_run_id", "999"),
                                   ("verified", "true"), ("stage", "backup_verified"),
                                   ("backup_checksum_verified", False), ("dbcc_physical_only_completed", False)):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "crm_backup_not_verified"):
                deploy_bot.validate_crm_evidence({**original, field: replacement}, rehearsal, SHA, CI)

    def test_journal_rejects_tampering_and_symlink(self):
        original, _ = crm_evidence()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps(original))
            self.assertEqual(deploy_bot.read_journal(path), original)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(RuntimeError, "evidence_file_required"):
                deploy_bot.read_journal(link)
            path.write_text(json.dumps({**original, "passed": True}))
            with self.assertRaisesRegex(RuntimeError, "evidence_digest_mismatch"):
                deploy_bot.read_journal(path)


class ComposeEnvironmentTest(unittest.TestCase):
    def test_effective_values_preserve_literal_characters_without_dotenv_quotes_or_logging(self):
        values = {"TERRASOFT_DATABASE": "Terrasoft", "EMPTY_VALUE": "",
                  "SYNTHETIC_SECRET": "keep 'quotes' \"double\" = # $literal $(no-shell)"}
        config = json.dumps({"services": {"bot": {"environment": values}}}).encode()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(backup_bot, "run", return_value=config) as run, \
             redirect_stdout(output), redirect_stderr(errors):
            result = backup_bot.compose_environment()
        self.assertIn(b"TERRASOFT_DATABASE=Terrasoft\n", result)
        self.assertNotIn(b"TERRASOFT_DATABASE='Terrasoft'", result)
        self.assertIn(("SYNTHETIC_SECRET=" + values["SYNTHETIC_SECRET"] + "\n").encode(), result)
        self.assertIn(b"EMPTY_VALUE=\n", result)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual(run.call_args.args[0],
                         ["docker", "compose", "-p", "receipt-paybot", "config", "--format", "json"])
        self.assertNotIn(values["SYNTHETIC_SECRET"], repr(run.call_args))

    def test_invalid_environment_keys_values_and_line_injection_are_rejected(self):
        invalid = [[], {"BAD=KEY": "value"}, {"1INVALID": "value"},
                   {"VALID": None}, {"VALID": 123}, {"VALID": True},
                   {"VALID": "before\nafter"}, {"VALID": "before\rafter"},
                   {"VALID": "before\0after"}, {"BAD\nKEY": "value"}]
        for values in invalid:
            config = json.dumps({"services": {"bot": {"environment": values}}}).encode()
            with self.subTest(values=values), patch.object(backup_bot, "run", return_value=config), \
                 self.assertRaisesRegex(RuntimeError, "^effective_environment_invalid$"):
                backup_bot.compose_environment()

    def test_subprocess_failure_never_surfaces_captured_environment_or_stderr(self):
        result = SimpleNamespace(returncode=1, stdout=b"SYNTHETIC_PRIVATE_ENV", stderr=b"SYNTHETIC_PRIVATE_ERROR")
        with patch.object(backup_bot.subprocess, "run", return_value=result), \
             self.assertRaisesRegex(RuntimeError, "^command_failed:docker$"):
            backup_bot.compose_environment()


class ReleasePlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "runtime"
        self.backups = self.home / "backups"
        self.releases = self.home / "releases"
        for folder in (self.root, self.backups, self.releases):
            folder.mkdir()
        (self.root / "data/receipts").mkdir(parents=True)
        (self.root / "data/receipts/synthetic.pdf").write_bytes(b"synthetic fixture only")
        self.old_override = self.releases / "old.yml"
        self.old_override.write_text("services:\n  bot:\n    image: receipt-paybot:old-synthetic\n")
        (self.root / "docker-compose.override.yml").symlink_to(self.old_override)
        (self.root / ".env").write_text("CRM_PROVIDER=none\n")
        self.backup = self.backups / "receipt-paybot-previous"
        self.backup.mkdir(mode=0o700)
        (self.backup / "previous.env").write_bytes((self.root / ".env").read_bytes())
        (self.backup / "compose.rollback.yml").write_bytes(self.old_override.read_bytes())
        self.verified = {
            "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "execution_sha": SHA, "ci_run_id": CI, "checksums_verified": True,
            "restore_database_equal": {name: True for name in ("receipts", "payment_drafts", "audit_logs", "authorized_users")},
            "restore_files_equal": {"receipts": True, ".env": True}, "baseline_image": OLD_IMAGE,
            "restore_isolated_network_none_tmpfs": True, "bot_stopped": True,
        }
        self.write_bot_evidence()
        self.crm_path = self.home / "crm.json"
        self.rehearsal_path = self.home / "rehearsal.json"
        original, rehearsal = crm_evidence()
        self.crm_path.write_text(json.dumps(original))
        self.rehearsal_path.write_text(json.dumps(rehearsal))
        (self.releases / SHA).mkdir(mode=0o700)
        self.calls = []
        self.container_stdin = []
        self.effective_environment = {"TERRASOFT_DATABASE": "Terrasoft", "SYNTHETIC_SECRET": "PRIVATE_EFFECTIVE_VALUE"}
        self.bot_reads = []
        self.current_image = OLD_IMAGE
        self.current_running = False
        self.restart_count = 0
        self.fail_candidate_health = False
        self.fail_rollback_health = False
        self.labels_sha = SHA
        self.head_sha = SHA
        self.dirty = False
        self.db_health = "healthy"
        real_path = Path
        def mapped_path(value):
            return self.releases if value == "/home/ubuntu/receipt-paybot-releases" else real_path(value)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (backup_bot, deploy_bot):
            self.stack.enter_context(patch.object(module, "ROOT", self.root))
            self.stack.enter_context(patch.object(module, "BACKUPS", self.backups))
            self.stack.enter_context(patch.object(module, "run", self.fake_run))
        self.stack.enter_context(patch.object(deploy_bot, "Path", mapped_path))
        self.stack.enter_context(patch.object(deploy_bot.time, "sleep"))
        self.stack.enter_context(patch.object(deploy_bot.subprocess, "run", self.subprocess_run))

    def write_bot_evidence(self):
        (self.backup / "verified.json").write_text(json.dumps(self.verified))

    def args(self, execute=False):
        return SimpleNamespace(execution_sha=SHA, ci_run_id=CI, backup=str(self.backup),
                               crm_evidence=str(self.crm_path), rehearsal_evidence=str(self.rehearsal_path),
                               run_id=RUN, expected_image=OLD_IMAGE, execute=execute)

    def subprocess_run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:4] == ["docker", "compose", "-p", "receipt-paybot"] and "stop" in args:
            self.current_running = False
            return SimpleNamespace(returncode=0)
        raise AssertionError("Unexpected unmocked subprocess")

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "sha256sum":
            return b""
        if args[0] == "git":
            return (self.head_sha + "\n").encode() if "rev-parse" in args else (b" M changed.py\n" if self.dirty else b"")
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": NEW_IMAGE, "Config": {"Labels": {"org.opencontainers.image.revision": self.labels_sha}}}]).encode()
        if args[:3] == ["docker", "inspect", "receipt-paybot-bot-1"]:
            if "--format" in args:
                return self.current_image.encode()
            self.bot_reads.append(self.current_image)
            return json.dumps([{"Id": "synthetic-bot-container", "Image": self.current_image, "State": {"Running": self.current_running},
                                "RestartCount": self.restart_count}]).encode()
        if args[:3] == ["docker", "inspect", "receipt-paybot-db-1"]:
            if "--format" in args:
                return self.db_health.encode()
            return json.dumps([{"Id": "synthetic-db-container", "State": {"Health": {"Status": self.db_health}}}]).encode()
        if args[:2] == ["docker", "run"]:
            if "--read-only" not in args or "--rm" not in args:
                raise AssertionError("Unexpected mutable Docker run")
            self.container_stdin.append(kwargs.get("input"))
            return b"candidate_preflight_passed"
        if args[:2] == ["docker", "start"]:
            self.current_running = True
            return b"synthetic-bot-container"
        if args[:4] == ["docker", "compose", "-p", "receipt-paybot"]:
            if args[4:] == ["config", "--format", "json"]:
                return json.dumps({"services": {"bot": {"environment": self.effective_environment}}}).encode()
            if "up" in args:
                self.current_running = True
                old = (self.root / "docker-compose.override.yml").resolve() == self.old_override
                self.current_image = OLD_IMAGE if old else NEW_IMAGE
                self.restart_count = int(self.fail_rollback_health if old else self.fail_candidate_health)
                return b""
            if "exec" in args:
                return b"runtime_schema_target_passed"
        raise AssertionError("Unexpected release command")

    def assert_no_release_mutation(self):
        self.assertEqual((self.root / "docker-compose.override.yml").resolve(), self.old_override)
        self.assertFalse((self.releases / SHA / "compose.release.yml").exists())
        self.assertFalse(any(args[:4] == ["docker", "compose", "-p", "receipt-paybot"]
                             and args[4:5] != ["config"] for args in self.calls))

    def test_backup_default_plan_is_read_only(self):
        result = backup_bot.perform(self.args())
        self.assertEqual(result["mode"], "read_only_plan")
        self.assertFalse((self.backups / ("receipt-paybot-" + RUN)).exists())
        self.assertFalse(any(args[:2] in (["docker", "run"], ["docker", "stop"], ["docker", "tag"]) for args in self.calls))

    def test_backup_identity_health_collision_guards_precede_mutation(self):
        for kind in ("image", "revision", "dirty", "health", "collision"):
            with self.subTest(kind=kind):
                self.current_image = "sha256:" + "9" * 64 if kind == "image" else OLD_IMAGE
                self.head_sha = "b" * 40 if kind == "revision" else SHA
                self.dirty = kind == "dirty"
                self.db_health = "unhealthy" if kind == "health" else "healthy"
                if kind == "collision":
                    (self.backups / ("receipt-paybot-" + RUN)).mkdir()
                with self.assertRaises(RuntimeError):
                    backup_bot.perform(self.args(execute=True))
        self.assertFalse(any(args[:2] in (["docker", "run"], ["docker", "stop"], ["docker", "tag"]) for args in self.calls))

    def test_deploy_plan_runs_only_read_only_preflight(self):
        result = deploy_bot.perform(self.args())
        self.assertEqual(result["mode"], "read_only_plan")
        self.assert_no_release_mutation()
        self.assertFalse(self.current_running)

    def test_preflight_passes_effective_environment_only_as_stdin_bytes(self):
        # A quoted dotenv source must not be fed directly to docker --env-file.
        quoted = b"TERRASOFT_DATABASE='Terrasoft'\n"
        (self.root / ".env").write_bytes(quoted)
        (self.backup / "previous.env").write_bytes(quoted)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            deploy_bot.perform(self.args())
        self.assertEqual(self.container_stdin,
                         [b"SYNTHETIC_SECRET=PRIVATE_EFFECTIVE_VALUE\nTERRASOFT_DATABASE=Terrasoft\n"])
        docker_run = next(args for args in self.calls if args[:2] == ["docker", "run"])
        self.assertEqual(docker_run[docker_run.index("--env-file") + 1], "/dev/stdin")
        self.assertNotIn(str(self.root / ".env"), docker_run)
        self.assertNotIn("PRIVATE_EFFECTIVE_VALUE", repr(self.calls) + output.getvalue() + errors.getvalue())
        self.assert_no_release_mutation()

    def test_changed_override_or_environment_blocks_before_swap(self):
        for file in (self.old_override, self.root / ".env"):
            before = file.read_bytes()
            file.write_bytes(before + b"#changed\n")
            with self.subTest(file=file.name), self.assertRaisesRegex(RuntimeError, "changed_after_backup"):
                deploy_bot.perform(self.args(execute=True))
            file.write_bytes(before)
        self.assert_no_release_mutation()

    def test_stale_or_failed_backup_never_deploys(self):
        for kind in ("old", "future", "data"):
            self.verified["verified_at_utc"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=60 if kind == "future" else -60 if kind == "old" else 0)).isoformat()
            self.verified["restore_database_equal"]["receipts"] = kind != "data"
            self.write_bot_evidence()
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                deploy_bot.perform(self.args(execute=True))
        self.assert_no_release_mutation()

    def test_wrong_artifact_or_running_previous_bot_never_deploys(self):
        self.labels_sha = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "image_revision_mismatch"):
            deploy_bot.perform(self.args(execute=True))
        self.labels_sha = SHA
        self.current_running = True
        with self.assertRaisesRegex(RuntimeError, "old_bot_must_be_stopped"):
            deploy_bot.perform(self.args(execute=True))
        self.assert_no_release_mutation()

    def test_success_swaps_only_bot_and_records_exact_image(self):
        result = deploy_bot.perform(self.args(execute=True))
        self.assertEqual(result["image_id"], NEW_IMAGE)
        self.assertTrue(result["database_container_preserved"])
        self.assertTrue((self.releases / SHA / "deployment.json").is_file())
        up = next(args for args in self.calls if "up" in args)
        for option in ("--no-build", "--no-deps", "--pull", "never"):
            self.assertIn(option, up)
        self.assertEqual(up[-1], "bot")

    def test_failed_candidate_restores_and_verifies_previous_image(self):
        self.fail_candidate_health = True
        with self.assertRaises(RuntimeError):
            deploy_bot.perform(self.args(execute=True))
        self.assertEqual((self.root / "docker-compose.override.yml").resolve(), self.old_override)
        self.assertEqual(self.current_image, OLD_IMAGE)
        self.assertTrue(self.current_running)
        self.assertEqual(self.bot_reads[-1], OLD_IMAGE)
        self.assertGreaterEqual(len(self.bot_reads), 3)
        self.assertTrue((self.releases / SHA / "rollback.json").is_file())
        self.assertFalse((self.releases / SHA / "deployment.json").exists())

    def test_failed_rollback_health_is_not_reported_as_verified(self):
        self.fail_candidate_health = self.fail_rollback_health = True
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            deploy_bot.perform(self.args(execute=True))
        self.assertFalse((self.releases / SHA / "deployment.json").exists())

    def test_early_gate_failure_can_restart_only_the_unchanged_baseline(self):
        result = deploy_bot.recover_baseline_if_unchanged(self.args(execute=True))
        self.assertTrue(result["baseline_restart_attempted"])
        self.assertTrue(result["baseline_running_verified"])
        self.assertIn(["docker", "start", "synthetic-bot-container"], self.calls)
        self.assertEqual(self.bot_reads[-1], OLD_IMAGE)
        self.assert_no_release_mutation()

    def test_recovery_never_restarts_changed_image_configuration_or_failed_health(self):
        for kind in ("image", "env", "override", "health", "restarts"):
            before_env = (self.root / ".env").read_bytes()
            before_override = self.old_override.read_bytes()
            self.current_image = NEW_IMAGE if kind == "image" else OLD_IMAGE
            self.db_health = "unhealthy" if kind == "health" else "healthy"
            self.restart_count = 1 if kind == "restarts" else 0
            if kind == "env": (self.root / ".env").write_text("changed")
            if kind == "override": self.old_override.write_text("changed")
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                deploy_bot.recover_baseline_if_unchanged(self.args(execute=True))
            (self.root / ".env").write_bytes(before_env)
            self.old_override.write_bytes(before_override)
        self.assertFalse(any(args[:2] == ["docker", "start"] for args in self.calls))

    def test_read_only_failure_never_attempts_baseline_restart(self):
        result = deploy_bot.recover_baseline_if_unchanged(self.args())
        self.assertFalse(result["baseline_restart_attempted"])
        self.assertFalse(self.calls)

    def test_recovery_requires_recent_evidence_that_backup_stopped_the_bot(self):
        self.verified["bot_stopped"] = False
        self.write_bot_evidence()
        with self.assertRaisesRegex(RuntimeError, "backup_unverified"):
            deploy_bot.recover_baseline_if_unchanged(self.args(execute=True))
        self.verified["bot_stopped"] = True
        self.verified["verified_at_utc"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.write_bot_evidence()
        with self.assertRaisesRegex(RuntimeError, "backup_stale"):
            deploy_bot.recover_baseline_if_unchanged(self.args(execute=True))
        self.assertFalse(any(args[:2] == ["docker", "start"] for args in self.calls))

    def test_deploy_rejects_symlink_backup_or_evidence_arguments(self):
        for key, original in (("backup", self.backup), ("crm_evidence", self.crm_path),
                              ("rehearsal_evidence", self.rehearsal_path)):
            link = self.home / (key + "-link")
            link.symlink_to(original)
            args = self.args(execute=True)
            setattr(args, key, str(link))
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                deploy_bot.perform(args)
        self.assert_no_release_mutation()


if __name__ == "__main__":
    unittest.main()
