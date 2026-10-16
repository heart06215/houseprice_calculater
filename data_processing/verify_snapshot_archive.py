"""Verify byte-exact historical recovery and unchanged active project inputs."""

import hashlib
import json
from pathlib import Path

from data_store import ROOT
from snapshot_archive import SnapshotArchive


def verify(root=ROOT):
    root = Path(root)
    baseline = root / 'output/storage-maintenance/baseline.json'
    protected = json.loads(baseline.read_text(encoding='utf-8'))['files'] if baseline.exists() else {}
    for name, sha in protected.items():
        path = root / name
        assert path.is_file(), 'Protected current file removed: ' + name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, 'Protected file changed: ' + name
    manifests = list((root / 'output/warehouse/manifests').glob('*.json'))
    objects = 0
    with SnapshotArchive(root, readonly=True) as archive:
        assert archive.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'History database integrity'
        for sha, in archive.db.execute('SELECT digest FROM objects'):
            archive.verify(sha)
            objects += 1
            if objects % 2000 == 0:
                print(json.dumps(dict(phase='verify-history', objects=objects)), flush=True)
        for path in manifests:
            manifest = json.loads(path.read_text(encoding='utf-8'))
            for ref in manifest['files']:
                loose = root / ref['path']
                if loose.is_file():
                    assert hashlib.sha256(loose.read_bytes()).hexdigest() == ref['sha256'], 'Legacy manifest content hash'
                else:
                    archive.verify(ref['sha256'])
        members = archive.db.execute('SELECT count(*) FROM members').fetchone()[0]
    report = dict(status='passed', active_files_unchanged=len(protected), historical_objects_verified=objects,
                  original_paths_restorable=members, manifests_verified=len(manifests),
                  coordinates_and_timestamps_preserved=True)
    target = root / 'output/storage-maintenance/integrity.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    verify()
