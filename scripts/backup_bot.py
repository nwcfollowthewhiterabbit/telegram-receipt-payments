"""Verified local update backup; execute only from a pushed, CI-verified checkout.

Copies credentials privately, never prints them. Stops only ReceiptFlow while
capturing the database/files, verifies an isolated restore, and leaves it stopped
on success for immediate deployment. On failure attempts to restart the old bot.
No external storage and no database replacement. Default is read-only.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

ROOT = Path('/home/ubuntu/receipt-paybot')
BACKUPS = Path('/home/ubuntu/backups')


def run(args, **kw):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if result.returncode:
        raise RuntimeError('command_failed:' + args[0])
    return result.stdout


def compose_environment():
    """Use Compose's effective environment, including dotenv quoting semantics.

    Never print or persist the returned bytes. Feed them directly to Docker's
    --env-file /dev/stdin; no shell interpolation or credential copying.
    """
    config = json.loads(run(['docker', 'compose', '-p', 'receipt-paybot', 'config', '--format', 'json'], cwd=ROOT))
    values = config['services']['bot']['environment']
    if not isinstance(values, dict):
        raise RuntimeError('effective_environment_invalid')
    lines = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str) or any(c in value for c in ('\n', '\r', '\0')):
            raise RuntimeError('effective_environment_invalid')
        lines.append(key + '=' + value)
    return ('\n'.join(lines) + '\n').encode()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('receipt_tree_invalid')
    def unreadable(error):
        raise RuntimeError('receipt_tree_unreadable') from None
    result = {}
    try:
        for directory, folders, files in os.walk(root, onerror=unreadable, followlinks=False):
            for name in sorted(folders + files):
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise RuntimeError('receipt_symlinks_require_review')
                if stat.S_ISDIR(mode):
                    continue
                if not stat.S_ISREG(mode):
                    raise RuntimeError('receipt_special_file_requires_review')
                result[str(path.relative_to(root))] = digest(path)
    except OSError:
        raise RuntimeError('receipt_tree_unreadable') from None
    return result


def readonly_container(image, mounts, program):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise RuntimeError('invalid_reader_image')
    if program not in {'python', 'tar'}:
        raise RuntimeError('invalid_reader_program')
    command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '0:0', '--entrypoint', program]
    for source, target in mounts:
        source = Path(source)
        if not source.is_absolute() or any(c in str(source) + str(target) for c in (',', '\n', '\r', '\0')):
            raise RuntimeError('invalid_reader_mount')
        command.extend(['--mount', f'type=bind,src={source},dst={target},readonly'])
    return [*command, image]


def container_tree_hash(root, image):
    """Read private source files as container root; keep names/hashes in memory."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('receipt_tree_invalid')
    # The baseline image may predate this helper. Execute this exact checkout's
    # fixed hash implementation, not an import from the old image.
    code = ('import hashlib,json,os,stat\nfrom pathlib import Path\n'
            + inspect.getsource(digest) + '\n' + inspect.getsource(tree_hash)
            + '\nprint(json.dumps(tree_hash(Path("/source")),sort_keys=True))\n')
    payload = run([*readonly_container(image, [(root, '/source')], 'python'), '-c', code])
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise RuntimeError('receipt_hash_result_invalid')
    for name, value in result.items():
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or not name
                or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
            raise RuntimeError('receipt_hash_result_invalid')
    return result


def archive_runtime_files(image, destination, files):
    required = {'.env', 'docker-compose.yml', 'docker-compose.override.yml', 'data/receipts'}
    if not required <= set(files) or set(files) - required - {'scripts/smoke_receipt_history.py'}:
        raise RuntimeError('runtime_archive_entries_invalid')
    link = ROOT / 'docker-compose.override.yml'
    if not link.is_symlink():
        raise RuntimeError('release_override_not_symlink')
    target = link.resolve(strict=True)
    if not target.is_file():
        raise RuntimeError('release_override_target_invalid')
    # The override is an absolute symlink outside ROOT; mount exactly its file,
    # never the containing release directory or any writable host location.
    mounts = [(ROOT, str(ROOT))]
    if not target.is_relative_to(ROOT):
        mounts.append((target, str(target)))
    command = [*readonly_container(image, mounts, 'tar'), '-C', str(ROOT), '-hczf', '-', *files]
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('runtime_archive_failed')


def validate_args(args):
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', args.run_id):
        raise RuntimeError('invalid_run_id')
    if not re.fullmatch(r'[0-9a-f]{40}', args.execution_sha):
        raise RuntimeError('invalid_execution_sha')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.expected_image):
        raise RuntimeError('invalid_expected_image')
    if not re.fullmatch(r'[0-9]+', args.ci_run_id):
        raise RuntimeError('invalid_ci_run_id')


def perform(args):
    validate_args(args)
    backup = BACKUPS / ('receipt-paybot-' + args.run_id)
    restore = 'receipt-paybot-restore-' + args.run_id.lower()
    old_image = run(['docker', 'inspect', 'receipt-paybot-bot-1', '--format', '{{.Image}}']).decode().strip()
    if old_image != args.expected_image:
        raise RuntimeError('runtime_image_changed')
    if run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip() != args.execution_sha:
        raise RuntimeError('checkout_revision_mismatch')
    if run(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no']).strip():
        raise RuntimeError('tracked_checkout_changes')
    if run(['docker', 'inspect', 'receipt-paybot-db-1', '--format', '{{.State.Health.Status}}']).decode().strip() != 'healthy':
        raise RuntimeError('database_not_healthy')
    if backup.exists() or backup.is_symlink():
        raise RuntimeError('backup_target_exists')
    if not (ROOT / 'docker-compose.override.yml').is_symlink():
        raise RuntimeError('release_override_not_symlink')
    container_tree_hash(ROOT / 'data/receipts', old_image)
    if not args.execute:
        return {'mode': 'read_only_plan', 'execution_sha': args.execution_sha, 'runtime_image_matches': True, 'backup_target_empty': True}
    os.umask(0o077)
    backup.mkdir(mode=0o700)
    rollback_tag = 'receipt-paybot:rollback-' + args.run_id.lower()
    run(['docker', 'tag', old_image, rollback_tag])
    with open(backup / 'source-baseline.tar', 'wb') as stream:
        p = subprocess.run(['git', '-C', str(ROOT), 'archive', args.execution_sha], stdout=stream, stderr=subprocess.PIPE)
        if p.returncode:
            raise RuntimeError('source_archive_failed')
    with gzip.open(backup / 'rollback-image.tar.gz', 'wb') as stream:
        p = subprocess.Popen(['docker', 'save', rollback_tag], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        shutil.copyfileobj(p.stdout, stream)
        if p.wait():
            raise RuntimeError('image_archive_failed')
    stopped, restore_created = False, False
    try:
        pg_image = run(['docker', 'inspect', 'receipt-paybot-db-1', '--format', '{{.Image}}']).decode().strip()
        run(['docker', 'run', '-d', '--name', restore, '--network', 'none',
             '--tmpfs', '/var/lib/postgresql/data:rw,size=536870912',
             '-e', 'POSTGRES_USER=restorecheck', '-e', 'POSTGRES_DB=restorecheck',
             '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', pg_image])
        restore_created = True
        info = json.loads(run(['docker', 'inspect', restore]))[0]
        if info['HostConfig']['NetworkMode'] != 'none' or '/var/lib/postgresql/data' not in info['HostConfig']['Tmpfs']:
            raise RuntimeError('restore_isolation_failed')
        run(['docker', 'exec', restore, 'sh', '-c', 'for n in $(seq 1 30); do pg_isready -U restorecheck -d restorecheck >/dev/null && exit 0; sleep 1; done; exit 1'])
        stopped = True  # a failed stop may still have stopped the container
        run(['docker', 'stop', '--time', '90', 'receipt-paybot-bot-1'])
        if run(['docker', 'inspect', 'receipt-paybot-bot-1', '--format', '{{.State.Running}}']).decode().strip() != 'false':
            raise RuntimeError('bot_not_stopped')
        print(json.dumps({'stage': 'consistent_snapshot', 'bot_stopped': True}), flush=True)
        with open(backup / 'database.dump', 'wb') as stream:
            p = subprocess.run(['docker', 'exec', 'receipt-paybot-db-1', 'sh', '-c', 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl -Fc'], stdout=stream, stderr=subprocess.PIPE)
            if p.returncode:
                raise RuntimeError('database_dump_failed')
        files = ['.env', 'docker-compose.yml', 'docker-compose.override.yml', 'data/receipts']
        if (ROOT / 'scripts/smoke_receipt_history.py').exists():
            files.append('scripts/smoke_receipt_history.py')
        archive_runtime_files(old_image, backup / 'runtime-files.tar.gz', files)
        with open(backup / 'database.dump', 'rb') as stream:
            run(['docker', 'exec', '-i', restore, 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', 'restorecheck', '-d', 'restorecheck'], stdin=stream)
        live_cmd = ['docker', 'exec', '-i', 'receipt-paybot-db-1', 'sh', '-c', 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
        restore_cmd = ['docker', 'exec', '-i', restore, 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'restorecheck', '-d', 'restorecheck']
        table_query = b"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"
        live_tables, restored_tables = run(live_cmd, input=table_query), run(restore_cmd, input=table_query)
        if live_tables != restored_tables:
            raise RuntimeError('database_tables_differ')
        tables = live_tables.decode().splitlines()
        if not {'receipts', 'payment_drafts', 'audit_logs', 'authorized_users'} <= set(tables):
            raise RuntimeError('required_tables_missing')
        equality = {}
        for table in tables:
            if not re.fullmatch(r'[a-z_]+', table):
                raise RuntimeError('unexpected_table_name')
            sql = ("BEGIN READ ONLY; SELECT count(*),md5(coalesce(string_agg(md5(row_to_json(x)::text),'' ORDER BY id),'')) FROM public." + table + ' x; COMMIT;').encode()
            equality[table] = run(live_cmd, input=sql) == run(restore_cmd, input=sql)
        if not all(equality.values()):
            raise RuntimeError('database_content_mismatch')
        restored_root = backup / 'file-restore-check'
        restored_root.mkdir(mode=0o700)
        run(['tar', '-xzf', str(backup / 'runtime-files.tar.gz'), '-C', str(restored_root)])
        checks = {name: digest(ROOT / name) == digest(restored_root / name) for name in files if name != 'data/receipts'}
        checks['receipts'] = container_tree_hash(ROOT / 'data/receipts', old_image) == tree_hash(restored_root / 'data/receipts')
        if not all(checks.values()):
            raise RuntimeError('file_restore_mismatch')
        shutil.copy2(restored_root / '.env', backup / 'previous.env')
        shutil.copy2(restored_root / 'docker-compose.override.yml', backup / 'compose.rollback.yml')
        names = ('database.dump', 'runtime-files.tar.gz', 'source-baseline.tar', 'rollback-image.tar.gz', 'previous.env', 'compose.rollback.yml')
        sums = {name: digest(backup / name) for name in names}
        (backup / 'sha256sums.txt').write_text(''.join(value + '  ' + name + '\n' for name, value in sums.items()))
        run(['sha256sum', '--check', 'sha256sums.txt'], cwd=backup)
        evidence = {'verified_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'execution_sha': args.execution_sha,
                    'ci_run_id': args.ci_run_id, 'baseline_image': old_image, 'rollback_tag': rollback_tag,
                    'restore_database_equal': equality, 'restore_files_equal': checks, 'checksums_verified': True,
                    'restore_isolated_network_none_tmpfs': True, 'bot_stopped': True, 'offsite': False}
        (backup / 'verified.json').write_text(json.dumps(evidence, indent=2) + '\n')
        shutil.rmtree(restored_root)
        run(['docker', 'rm', '-f', restore])
        restore_created = False
        return evidence
    except BaseException:
        if stopped:
            subprocess.run(['docker', 'start', 'receipt-paybot-bot-1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise
    finally:
        if restore_created:
            subprocess.run(['docker', 'rm', '-f', restore], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    for name in ('run-id', 'execution-sha', 'expected-image', 'ci-run-id'):
        parser.add_argument('--' + name, required=True)
    try:
        print(json.dumps(perform(parser.parse_args())), flush=True)
        return 0
    except BaseException as exc:
        # A subprocess's stderr can contain sensitive configuration.
        print(json.dumps({'status': 'failed', 'reason': str(exc) if type(exc) is RuntimeError else 'backup_failed'}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
