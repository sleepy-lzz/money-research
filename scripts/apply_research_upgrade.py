"""Verified source-only update. Default is dry-run; runtime/.env/accounts are out of scope.

Close the workbench and scheduled runner before applying or rolling back. No process
control, network, package installation or database access is performed by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from uuid import uuid4

BLOCKED = {'runtime', '.env', '.venv', '.git', '__pycache__', '.pytest_cache', '.research-upgrade-backups'}


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def safe(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in {'..', '.'} or p in BLOCKED for p in parts) or '\\' in relative or ':' in relative:
        raise ValueError('unsafe_or_private_path:' + relative)
    path = root.joinpath(*parts)
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('symlink_rejected:' + relative)
    if path.exists() and not path.is_file():
        raise ValueError('non_file_target:' + relative)
    return path


def atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.upgrade-tmp')
    try:
        with temp.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def rollback(target: Path, backup_id: str, commit: bool = False) -> dict:
    target = target.resolve()
    if not backup_id or any(c not in '0123456789abcdef' for c in backup_id):
        raise ValueError('invalid_backup_id')
    folder = target / '.research-upgrade-backups' / backup_id
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError('backup_symlink_rejected')
    meta = json.loads((folder / 'recovery.json').read_text(encoding='utf-8'))
    if meta['target'] != str(target):
        raise ValueError('recovery_target_mismatch')
    # Validate ALL preimages and current states before changing ANY file.
    for item in meta['files']:
        path = safe(target, item['path'])
        if sha(path) not in {item['before_sha256'], item['after_sha256']}:
            raise ValueError('rollback_local_edit_conflict:' + item['path'])
        if item['before_sha256'] and sha(safe(folder / 'before', item['path'])) != item['before_sha256']:
            raise ValueError('backup_corrupted:' + item['path'])
    if commit:
        for item in meta['files']:
            path = safe(target, item['path'])
            if item['before_sha256']:
                atomic(path, safe(folder / 'before', item['path']).read_bytes())
            else:
                path.unlink(missing_ok=True)
        meta['status'] = 'rolled_back'
        atomic(folder / 'recovery.json', json.dumps(meta, ensure_ascii=False, indent=2).encode())
    return dict(status='rolled_back' if commit else 'rollback_dry_run', backup_id=backup_id,
                changed_source_files=len(meta['files']), runtime_and_env_untouched=True)


def upgrade(package: Path, target: Path, commit: bool = False) -> dict:
    if target.is_symlink() or package.is_symlink():
        raise ValueError('root_symlink_rejected')
    target, package = target.resolve(), package.resolve()
    if target == package or not (target / 'pyproject.toml').is_file():
        raise ValueError('target_must_be_existing_separate_project')
    manifest = json.loads((package / 'upgrade-manifest.json').read_text(encoding='utf-8'))
    items = manifest['files']
    if len({x['path'] for x in items}) != len(items):
        raise ValueError('duplicate_manifest_path')
    already = []
    for item in items:
        source, dest = safe(package, item['path']), safe(target, item['path'])
        if sha(source) != item['after_sha256']:
            raise ValueError('package_source_corrupted:' + item['path'])
        current = sha(dest)
        already.append(current == item['after_sha256'])
        if current not in {item['before_sha256'], item['after_sha256']}:
            raise ValueError('local_source_conflict:' + item['path'])
    if items and all(already):
        return dict(status='already_installed_identical', changed_source_files=0)
    if any(already):
        raise ValueError('mixed_version_target; use_verified_recovery_before_retry')
    plan = dict(status='dry_run', target=str(target), changed_source_files=len(items),
                paths=[x['path'] for x in items], runtime_and_env_untouched=True)
    if not commit:
        return plan
    backups = target / '.research-upgrade-backups'
    if backups.is_symlink():
        raise ValueError('backup_root_symlink_rejected')
    ident = uuid4().hex
    folder = backups / ident
    folder.mkdir(parents=True, exist_ok=False)
    for item in items:
        if item['before_sha256']:
            path = safe(folder / 'before', item['path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe(target, item['path']), path)
    meta = dict(target=str(target), files=items, status='prepared')
    atomic(folder / 'recovery.json', json.dumps(meta, ensure_ascii=False, indent=2).encode())
    try:
        for item in items:
            dest = safe(target, item['path'])
            if sha(dest) != item['before_sha256']:
                raise ValueError('source_changed_during_upgrade:' + item['path'])
            atomic(dest, safe(package, item['path']).read_bytes())
        meta['status'] = 'installed'
        atomic(folder / 'recovery.json', json.dumps(meta, ensure_ascii=False, indent=2).encode())
    except Exception:
        # Journal also permits manual recovery after an interrupted process. If a
        # third party edited a file, rollback refuses rather than overwriting it.
        rollback(target, ident, True)
        raise
    return plan | dict(status='installed', backup_id=ident,
                       rollback_command=f'python "{Path(__file__).resolve()}" --target "{target}" --rollback {ident} --apply')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', metavar='BACKUP_ID')
    args = parser.parse_args()
    try:
        result = rollback(args.target, args.rollback, args.apply) if args.rollback else upgrade(args.package, args.target, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='rejected', error=str(exc)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
