"""Install the exact CI image after verified bot and CRM backups.

Only a stopped ReceiptFlow is replaced. The existing database container and
credentials are retained. Failures restore the previous release override and
restart the previous image; persistent records are never restored over new data.
"""
import argparse
import datetime as dt
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time

from scripts.backup_bot import ROOT, BACKUPS, digest, run


def read_journal(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('evidence_file_required')
    data = json.loads(path.read_text())
    unsigned = dict(data)
    signature = unsigned.pop('record_sha256', None)
    raw = json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(',', ':'), default=str).encode()
    if hashlib.sha256(raw).hexdigest() != signature:
        raise RuntimeError('evidence_digest_mismatch')
    return data


def validate_crm_evidence(backup, rehearsal, execution_sha, ci_run_id):
    if (backup.get('stage') != 'verified' or backup.get('verified') is not True
            or backup.get('execution_sha') != execution_sha or backup.get('ci_run_id') != ci_run_id
            or backup.get('backup_checksum_verified') is not True
            or backup.get('dbcc_physical_only_completed') is not True):
        raise RuntimeError('crm_backup_not_verified')
    if (rehearsal.get('execution_sha') != execution_sha or rehearsal.get('ci_run_id') != ci_run_id
            or rehearsal.get('stage') != 'rehearsal_passed' or rehearsal.get('passed') is not True
            or rehearsal.get('verified') is not True or rehearsal.get('all_fixture_writes_rolled_back') is not True
            or rehearsal.get('backup_record_sha256') != backup.get('record_sha256')
            or rehearsal.get('restore_database') != backup.get('restore_database')):
        raise RuntimeError('crm_rehearsal_not_passed')


def perform(args):
    if not re.fullmatch(r'[0-9a-f]{40}', args.execution_sha) or not re.fullmatch(r'[0-9]+', args.ci_run_id):
        raise RuntimeError('invalid_release_identity')
    backup_arg = Path(args.backup)
    if backup_arg.is_symlink():
        raise RuntimeError('invalid_backup_directory')
    backup = backup_arg.resolve(strict=True)
    if backup.parent != BACKUPS:
        raise RuntimeError('invalid_backup_directory')
    verified = json.loads((backup / 'verified.json').read_text())
    if verified['execution_sha'] != args.execution_sha or verified['ci_run_id'] != args.ci_run_id:
        raise RuntimeError('backup_execution_mismatch')
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(verified['verified_at_utc'])
    if not dt.timedelta(0) <= age <= dt.timedelta(minutes=30):
        raise RuntimeError('bot_backup_stale')
    if not verified['checksums_verified'] or not all(verified['restore_database_equal'].values()) or not all(verified['restore_files_equal'].values()):
        raise RuntimeError('bot_backup_unverified')
    run(['sha256sum', '--check', 'sha256sums.txt'], cwd=backup)
    if digest(ROOT / '.env') != digest(backup / 'previous.env'):
        raise RuntimeError('configuration_changed_after_backup')
    if not (ROOT / 'docker-compose.override.yml').is_symlink() or digest(ROOT / 'docker-compose.override.yml') != digest(backup / 'compose.rollback.yml'):
        raise RuntimeError('release_override_changed_after_backup')
    if run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip() != args.execution_sha:
        raise RuntimeError('checkout_revision_mismatch')
    if run(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no']).strip():
        raise RuntimeError('tracked_checkout_changes')
    image = 'receipt-paybot:' + args.execution_sha
    info = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if info['Config']['Labels'].get('org.opencontainers.image.revision') != args.execution_sha:
        raise RuntimeError('image_revision_mismatch')
    old = json.loads(run(['docker', 'inspect', 'receipt-paybot-bot-1']))[0]
    if old['Image'] != verified['baseline_image'] or old['State']['Running']:
        raise RuntimeError('old_bot_must_be_stopped_at_backup_image')
    db_before = json.loads(run(['docker', 'inspect', 'receipt-paybot-db-1']))[0]
    if db_before['State']['Health']['Status'] != 'healthy':
        raise RuntimeError('database_not_healthy')
    # Actual backup/clone identity is rechecked in an image using the production
    # connector's read-only target guard. No bank endpoints or bot polling.
    evidence_arg = Path(args.crm_evidence)
    if evidence_arg.is_symlink():
        raise RuntimeError('crm_evidence_required')
    evidence = evidence_arg.resolve(strict=True)
    if not evidence.is_file():
        raise RuntimeError('crm_evidence_required')
    rehearsal_arg = Path(args.rehearsal_evidence)
    if rehearsal_arg.is_symlink():
        raise RuntimeError('crm_rehearsal_evidence_required')
    rehearsal = rehearsal_arg.resolve(strict=True)
    if not rehearsal.is_file():
        raise RuntimeError('crm_rehearsal_evidence_required')
    validate_crm_evidence(read_journal(evidence), read_journal(rehearsal), args.execution_sha, args.ci_run_id)
    preflight = '''
from pathlib import Path
from sqlalchemy import text
from scripts.verify_terrasoft_backup import open_admin, read_verified_evidence, cleanup_verified_clone
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector
from src.connectors.crm.terrasoft_posting import owner_adminunit, native_acl_exclusions
from sqlalchemy import create_engine
v=read_verified_evidence(Path('/evidence/verified.json'))
with open_admin(120) as admin:
    cleanup_verified_clone(admin,v,None,execute=False)
c=TerrasoftMssqlConnector()
assert c.settings.crm_files_and_posting_enabled and not c.settings.crm_dry_run
engine=create_engine(c._validate_live_target(),hide_parameters=True,connect_args={'login_timeout':10,'timeout':30})
with engine.connect() as x:
    c._verify_connection_database(x)
    c._attachment_policy(x)
    native_acl_exclusions(x)
    owner_adminunit(x,'D8D923D0-1A2E-44E1-9072-3D82451A7A7E')
engine.dispose()
print('candidate_preflight_passed')
'''
    run(['docker', 'run', '--rm', '--read-only', '--tmpfs', '/tmp:rw,size=64m',
         '--env-file', str(ROOT / '.env'), '-e', 'CRM_FILES_AND_POSTING_ENABLED=true',
         '-v', str(evidence) + ':/evidence/verified.json:ro', image, 'python', '-c', preflight])
    if not args.execute:
        return {'mode': 'read_only_plan', 'preflight_passed': True, 'image_revision': args.execution_sha}
    release = Path('/home/ubuntu/receipt-paybot-releases') / args.execution_sha
    if not release.is_dir() or release.is_symlink():
        raise RuntimeError('private_release_directory_required')
    override = release / 'compose.release.yml'
    if override.exists():
        raise RuntimeError('release_override_exists')
    override.write_text('services:\n  bot:\n    image: ' + image + '\n    environment:\n      CRM_FILES_AND_POSTING_ENABLED: "true"\n')
    os.chmod(override, 0o600)
    previous_override = (ROOT / 'docker-compose.override.yml').resolve(strict=True)
    temp = ROOT / '.receiptflow-release-link'
    if temp.exists() or temp.is_symlink():
        raise RuntimeError('temporary_release_link_exists')
    compose = ['docker', 'compose', '-p', 'receipt-paybot']
    try:
        temp.symlink_to(override)
        os.replace(temp, ROOT / 'docker-compose.override.yml')
        run([*compose, 'up', '-d', '--no-build', '--no-deps', '--pull', 'never', 'bot'], cwd=ROOT)
        time.sleep(8)
        current = json.loads(run(['docker', 'inspect', 'receipt-paybot-bot-1']))[0]
        if current['Image'] != info['Id'] or not current['State']['Running'] or current['RestartCount'] != 0:
            raise RuntimeError('runtime_health_failed')
        db_after = json.loads(run(['docker', 'inspect', 'receipt-paybot-db-1']))[0]
        if db_after['Id'] != db_before['Id'] or db_after['State']['Health']['Status'] != 'healthy':
            raise RuntimeError('database_container_changed')
        check = '''
from sqlalchemy import text
from src.db.session import engine
from src.config import get_settings
from src.connectors.crm.terrasoft_mssql import TerrasoftMssqlConnector
s=get_settings()
assert s.crm_files_and_posting_enabled
assert TerrasoftMssqlConnector().verify_connection_target()=='Terrasoft'
with engine.connect() as c:
    assert c.execute(text("SELECT to_regclass('public.crm_operations')")).scalar_one()
print('runtime_schema_target_passed')
'''
        run([*compose, 'exec', '-T', 'bot', 'python', '-c', check], cwd=ROOT)
        result = {'installed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'execution_sha': args.execution_sha,
                  'ci_run_id': args.ci_run_id, 'image_id': info['Id'], 'database_container_preserved': True,
                  'crm_target_verified': True, 'crm_workflow_enabled': True, 'runtime_running': True,
                  'restart_count': current['RestartCount'], 'bot_backup_verified': True,
                  'crm_backup_identity_rechecked': True, 'rehearsal_passed': True}
        (release / 'deployment.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    except BaseException:
        subprocess.run([*compose, 'stop', '-t', '90', 'bot'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if temp.is_symlink():
            temp.unlink()
        temp.symlink_to(previous_override)
        os.replace(temp, ROOT / 'docker-compose.override.yml')
        run([*compose, 'up', '-d', '--no-build', '--no-deps', '--pull', 'never', 'bot'], cwd=ROOT)
        time.sleep(8)
        restored = json.loads(run(['docker', 'inspect', 'receipt-paybot-bot-1']))[0]
        restored_db = json.loads(run(['docker', 'inspect', 'receipt-paybot-db-1']))[0]
        recovered = (restored['Image'] == verified['baseline_image'] and restored['State']['Running']
                     and restored['RestartCount'] == 0 and restored_db['Id'] == db_before['Id']
                     and restored_db['State']['Health']['Status'] == 'healthy')
        (release / 'rollback.json').write_text(json.dumps({'observed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'baseline_image_restored': recovered, 'execution_sha': args.execution_sha}) + '\n')
        if not recovered:
            raise RuntimeError('rollback_health_not_verified') from None
        raise


def recover_baseline_if_unchanged(args):
    """Recover a stopped baseline after an early gate failure, never a new target."""
    if not args.execute:
        return {'baseline_restart_attempted': False}
    path = Path(args.backup)
    if path.is_symlink() or path.resolve(strict=True).parent != BACKUPS:
        raise RuntimeError('baseline_recovery_backup_invalid')
    path = path.resolve(strict=True)
    verified_path = path / 'verified.json'
    if verified_path.is_symlink():
        raise RuntimeError('baseline_recovery_backup_invalid')
    verified = json.loads(verified_path.read_text())
    if (verified.get('execution_sha') != args.execution_sha or verified.get('ci_run_id') != args.ci_run_id
            or verified.get('checksums_verified') is not True
            or verified.get('bot_stopped') is not True
            or not verified.get('restore_database_equal') or not all(value is True for value in verified['restore_database_equal'].values())
            or not verified.get('restore_files_equal') or not all(value is True for value in verified['restore_files_equal'].values())):
        raise RuntimeError('baseline_recovery_backup_unverified')
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(verified['verified_at_utc'])
    if not dt.timedelta(0) <= age <= dt.timedelta(minutes=30):
        raise RuntimeError('baseline_recovery_backup_stale')
    run(['sha256sum', '--check', 'sha256sums.txt'], cwd=path)
    if (digest(ROOT / '.env') != digest(path / 'previous.env')
            or not (ROOT / 'docker-compose.override.yml').is_symlink()
            or digest(ROOT / 'docker-compose.override.yml') != digest(path / 'compose.rollback.yml')):
        raise RuntimeError('baseline_recovery_configuration_changed')
    old = json.loads(run(['docker', 'inspect', 'receipt-paybot-bot-1']))[0]
    db = json.loads(run(['docker', 'inspect', 'receipt-paybot-db-1']))[0]
    if (old['Image'] != verified['baseline_image'] or old['RestartCount'] != 0
            or db['State']['Health']['Status'] != 'healthy'):
        raise RuntimeError('baseline_recovery_target_changed')
    restarted = not old['State']['Running']
    if restarted:
        # Container ID prevents a same-name replacement from being started.
        run(['docker', 'start', old['Id']])
        time.sleep(3)
    current = json.loads(run(['docker', 'inspect', 'receipt-paybot-bot-1']))[0]
    current_db = json.loads(run(['docker', 'inspect', 'receipt-paybot-db-1']))[0]
    if (current['Id'] != old['Id'] or current['Image'] != verified['baseline_image']
            or not current['State']['Running'] or current['RestartCount'] != 0
            or current_db['Id'] != db['Id'] or current_db['State']['Health']['Status'] != 'healthy'):
        raise RuntimeError('baseline_recovery_health_not_verified')
    return {'baseline_restart_attempted': restarted, 'baseline_running_verified': True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    for key in ('execution-sha','ci-run-id','backup','crm-evidence','rehearsal-evidence'):
        parser.add_argument('--'+key,required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(perform(args)),flush=True)
        return 0
    except BaseException as exc:
        recovery = {'baseline_restart_attempted': False}
        if args.execute:
            try:
                recovery = recover_baseline_if_unchanged(args)
            except BaseException:
                recovery = {'baseline_recovery_verified': False, 'baseline_requires_review': True}
        print(json.dumps({'status':'failed','reason':str(exc) if type(exc) is RuntimeError else 'deployment_failed', **recovery}),flush=True)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
