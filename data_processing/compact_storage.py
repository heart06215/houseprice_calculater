"""Compact inactive snapshots without altering current data, coordinates or evidence."""

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from stat import S_ISDIR

from data_store import ROOT
from snapshot_archive import ARCHIVE, SnapshotArchive, safe_member


# Explicit opt-in: old generated previews, not crawl inputs or original evidence.
GENERATED_OUTPUTS = (
    'data_processing/standalone_dashboard.html',
    'data_processing/frontend/realestate/map_inline.html',
    'output/source_trial_20260922/preview',
    'output/anjuke_community_trial_20260923/preview',
    'output/anjuke_browser_trial_20260924/run-E/preview',
    'output/anjuke_execution_0924/2026-09-25/run-05/preview',
)


def compact_generated(root=ROOT):
    """Archive generated copies byte-for-byte before removing their loose files."""
    from update_workspace import UpdateWorkspace
    root = Path(root).resolve()
    with UpdateWorkspace(root).single_writer(), SnapshotArchive(root) as archive:
        report = dict(before=measure(root), archived=[], skipped=[])
        for name in GENERATED_OUTPUTS:
            path = root / name
            if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents) if p != root and root in p.parents):
                raise ValueError('Refusing a linked generated output: ' + name)
            path = safe_member(root, name)
            if not path.exists():
                continue
            try:
                if path.is_dir():
                    item = archive_tree(archive, path, remove_files=True)
                    # Remove only empty folders, leaving any newly created files alone.
                    for base, dirs, files in os.walk(path, topdown=False):
                        try:
                            Path(base).rmdir()
                        except OSError:
                            pass
                else:
                    item = archive.put_file(path)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                        raise ValueError('Generated output changed while archiving: ' + name)
                    path.unlink()
                    item = dict(path=name, files=1, bytes=item['bytes'])
                report['archived'].append(item)
                print(json.dumps(item), flush=True)
            except PermissionError as error:
                report['skipped'].append(read_failure(error, root, 'generated-output'))
        report['after'] = measure(root)
        report['status'] = 'compacted_with_skips' if report['skipped'] else 'compacted'
        target = root / 'output/storage-maintenance/generated-report.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def restore_tree(archive, member):
    """Restore an archived directory, refusing to overwrite changed local files."""
    target = safe_member(archive.root, member)
    prefix = target.relative_to(archive.root).as_posix() + '/'
    names = [row[0] for row in archive.db.execute('SELECT path FROM members') if row[0].startswith(prefix)]
    if not names:
        raise FileNotFoundError('Unknown archive directory: ' + member)
    for name in names:
        archive.restore(name)
    return len(names)


def read_failure(error, root, phase):
    path = Path(error.filename) if error.filename else Path(root)
    try:
        name = path.relative_to(root).as_posix()
    except ValueError:
        name = str(path)
    return dict(path=name, phase=phase, error=type(error).__name__,
                reason=str(error), errno=error.errno,
                winerror=getattr(error, 'winerror', None))


def measure(root):
    root = Path(root)
    sizes, counts, unique, unreadable = Counter(), Counter(), {}, []
    def record_error(error):
        unreadable.append(read_failure(error, root, 'enumerate'))
    for base, dirs, files in os.walk(root, onerror=record_error):
        dirs[:] = [d for d in dirs if not os.path.islink(Path(base) / d) and not os.path.isjunction(Path(base) / d)]
        for name in files:
            path = Path(base) / name
            try:
                stat = path.stat()
            except OSError as error:
                unreadable.append(read_failure(error, root, 'stat'))
                continue
            top = path.relative_to(root).parts[0]
            sizes[top] += stat.st_size
            counts[top] += 1
            unique[(stat.st_dev, stat.st_ino)] = stat.st_size
    return dict(logical_bytes=sum(sizes.values()), unique_bytes=sum(unique.values()),
                files=sum(counts.values()), directories=dict(sizes),
                complete=not unreadable,
                scope='all-accessible-files' if not unreadable else 'observed-files-only',
                unreadable_paths=unreadable)


def live_references(root):
    """Pin all clean evidence cited by current data, including the retained development copy."""
    refs = set()
    for folder in (root / 'data_processing/frontend', root / 'output'):
        for base, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ('warehouse', 'update_work', 'release-checkouts', 'storage-maintenance', 'assets', '__pycache__')]
            for name in names:
                if not name.endswith('.json'):
                    continue
                path = Path(base) / name
                try:
                    with path.open('rb') as stream:
                        if stream.read(1) not in (b'{', b'['):
                            continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeError):
                    continue
                def visit(value):
                    if isinstance(value, dict):
                        for child in value.values():
                            visit(child)
                    elif isinstance(value, list):
                        for child in value:
                            visit(child)
                    elif isinstance(value, str):
                        name = value.replace('\\', '/')
                        if name.startswith('output/warehouse/clean/'):
                            refs.add(safe_member(root, name))
                visit(data)
    return refs


def archive_tree(archive, folder, remove_files=False):
    root = archive.root
    folder = folder.resolve()
    if folder == root or root not in folder.parents or folder == (root / 'output').resolve():
        raise ValueError('Refusing to archive/remove an unsafe directory')
    count, size, archived_paths = 0, 0, []
    def fail_read(error):
        raise error
    for base, dirs, files in os.walk(folder, onerror=fail_read):
        for d in dirs:
            if os.path.islink(Path(base) / d) or os.path.isjunction(Path(base) / d):
                raise ValueError('Refusing a linked archive directory')
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError('Refusing a linked archive file')
            item = archive.put_file(path)
            count += 1
            size += item['bytes']
            if remove_files:
                archived_paths.append((path, item['sha256']))
            if count % 500 == 0:
                print(json.dumps(dict(phase='archive', folder=folder.relative_to(root).as_posix(), files=count, bytes=size)), flush=True)
    # A later unreadable subtree must not leave a partly deleted source tree.
    for path, sha in archived_paths:
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('File changed before archive cleanup: ' + str(path))
    for path, _ in archived_paths:
        path.unlink()
    return dict(path=folder.relative_to(root).as_posix(), files=count, bytes=size)


def compact_clean(archive, root):
    refs = live_references(root)
    count, size = 0, 0
    for path in (root / 'output/warehouse/clean').rglob('*'):
        if not path.is_file() or path.resolve() in refs:
            continue
        if not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]+', path.name):
            continue
        item = archive.put_file(path)
        if item['sha256'] != path.stem:
            raise ValueError('Unexpected CAS snapshot name: ' + str(path))
        path.unlink()
        count += 1
        size += item['bytes']
        if count % 500 == 0:
            print(json.dumps(dict(phase='clean-history', files=count, bytes=size)), flush=True)
    return dict(root=root.relative_to(archive.root).as_posix(), files=count, bytes=size, live_snapshots=len(refs))


def compact(root=ROOT):
    from update_workspace import UpdateWorkspace
    root = Path(root).resolve()
    out = root / 'output/storage-maintenance'
    out.mkdir(parents=True, exist_ok=True)
    with UpdateWorkspace(root).single_writer():
        before = measure(root)
        report = dict(before=before, history=[], archived_directories=[], skipped=[])
        with SnapshotArchive(root) as archive:
            # Preserve all historical versions rather than deleting older manifests.
            report['history'].append(compact_clean(archive, root))
            development = root / 'output/update_work/crawler-audit-nd4ger0y'
            if development.is_dir():
                report['history'].append(compact_clean(archive, development))
            for parent, prefix in (('output/update_work', 'run-'), ('output/release-checkouts', 'verified-')):
                allowed = (root / parent).resolve()
                for folder in allowed.glob(prefix + '*'):
                    phase = 'inspect'
                    try:
                        info = folder.stat()
                        if not S_ISDIR(info.st_mode) or folder.resolve().parent != allowed or os.path.isjunction(folder):
                            raise ValueError('Invalid temporary checkout directory')
                        if prefix == 'run-' and time.time() - info.st_mtime < 24 * 3600:
                            continue
                        phase = 'archive'
                        report['archived_directories'].append(archive_tree(archive, folder))
                        phase = 'remove-verified-directory'
                        shutil.rmtree(folder)
                    except PermissionError as error:
                        item = read_failure(error, root, phase)
                        item['directory'] = folder.relative_to(root).as_posix()
                        report['skipped'].append(item)
                        print(json.dumps(dict(event='skipped', **item), ensure_ascii=False), flush=True)
            report['archive'] = dict(path=ARCHIVE.as_posix(), bytes=archive.path.stat().st_size,
                objects=archive.db.execute('SELECT count(*) FROM objects').fetchone()[0],
                chunks=archive.db.execute('SELECT count(*) FROM chunks').fetchone()[0],
                members=archive.db.execute('SELECT count(*) FROM members').fetchone()[0])
        report['after'] = measure(root)
        report['status'] = 'compacted_with_skips' if report['skipped'] else 'compacted'
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--restore', help='Restore one original workspace-relative path from the lossless archive')
    action.add_argument('--restore-tree', help='Restore all files under an archived workspace-relative directory')
    action.add_argument('--generated', action='store_true', help='Archive old generated previews and standalone HTML copies')
    args = parser.parse_args()
    if args.restore or args.restore_tree:
        from update_workspace import UpdateWorkspace
        with UpdateWorkspace(ROOT).single_writer():
            with SnapshotArchive(ROOT, readonly=True) as archive:
                print(restore_tree(archive, args.restore_tree) if args.restore_tree else archive.restore(args.restore))
    elif args.generated:
        print(json.dumps(compact_generated(), ensure_ascii=False))
    else:
        compact()
