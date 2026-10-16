"""Content-addressed evidence and lossless, deduplicated project history."""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'output' / 'warehouse'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def snapshot(source, payload, layer='clean', suffix='json'):
    content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(content).hexdigest()
    path = STORE / layer / source / f'{digest}.{suffix}'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    return {'sha256': digest, 'path': path.relative_to(ROOT).as_posix(), 'bytes': len(content)}


def sqlite_snapshot(source, target):
    import sqlite3
    """Write a transactionally consistent copy of a SQLite DB.

    Uses the online backup API so a database written by another connection can
    never yield a half-written snapshot. Returns the target path."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.snapshot.tmp')
    with contextlib.closing(sqlite3.connect(str(source))) as src, \
         contextlib.closing(sqlite3.connect(str(temporary))) as dst:
        src.backup(dst)
    os.replace(temporary, target)
    return target


def _consistent_bytes(path):
    """Read bytes for the manifest; SQLite files are snapshotted first."""
    path = Path(path)
    if path.suffix in ('.sqlite3', '.sqlite'):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / path.name
            sqlite_snapshot(path, copy)
            return copy.read_bytes()
    return path.read_bytes()


def archive_project():
    from snapshot_archive import SnapshotArchive, ARCHIVE
    files = list((ROOT/'output/anjuke').glob('*.json')) + list((ROOT/'output/stats_gov').glob('*.json'))
    files += list((ROOT/'output/market').glob('*.json'))
    files += list((ROOT/'output/anjuke_recheck').glob('*.json'))
    files += list((ROOT/'output/engine_validation').glob('*.json'))
    files += list((ROOT/'output/cih').glob('*.json'))
    files += list((ROOT/'output/public_locations').glob('*.json'))
    files += list((ROOT/'output/communities').glob('*.json'))
    files += list((ROOT/'output/community_profiles').glob('*.json'))
    files += list((ROOT/'output/fang').glob('*.json'))
    files += list((ROOT/'output/fang').glob('*.sqlite3'))
    files += list((ROOT/'output/income').glob('*.json'))
    files += list((ROOT/'output/hydrology').glob('*.json'))
    files += list((ROOT/'output/catalog').glob('*.json'))
    files += list((ROOT/'output/catalog').glob('*.sqlite3'))
    files += list((ROOT/'output/community_baseline').glob('*.json'))
    files += list((ROOT/'output/community_baseline').glob('*.md'))
    files += list((ROOT/'output/verification').glob('*.json'))
    files += [ROOT/'data_processing/frontend/realestate/data/water.json']
    files += list((ROOT/'data_processing/frontend/realestate/data/water').glob('*.json'))
    files += list((ROOT/'output/policies').glob('*.json'))
    files += list((ROOT/'output/administrative_regions').glob('*.json'))
    files += [ROOT/'data_processing/frontend/realestate/data/cities.json', ROOT/'data_processing/frontend/realestate/data/city_regions.json']
    files += [ROOT/'data_processing/frontend/realestate/data/metro.json']
    files += list((ROOT/'data_processing/frontend/realestate/data/communities').glob('*.json'))
    files += list((ROOT/'data_processing/frontend/realestate/data/districts').glob('*.json'))
    files += [ROOT/'data_processing/frontend/realestate/data/metro_operational.json',
              ROOT/'data_processing/frontend/realestate/data/metro_operational.min.json']
    files += [ROOT/'data_processing/frontend/realestate/data/communities.json', ROOT/'data_processing/frontend/update_status.json']
    files += list((ROOT/'data_processing/frontend/realestate/data').glob('properties*.json'))
    files += [ROOT/'data_processing/frontend/intersection_housing_data.json']
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'files': []}
    with SnapshotArchive(ROOT) as archive:
        for path in dict.fromkeys(files):
            if path.exists():
                content = _consistent_bytes(path)
                digest = hashlib.sha256(content).hexdigest()
                relative = f'output/warehouse/clean/project/{digest}.json'
                item = archive.put_bytes(content, relative)
                manifest['files'].append(dict(original=str(path.relative_to(ROOT)), path=relative,
                    archive=ARCHIVE.as_posix(), **item))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    atomic_json(STORE/'manifests'/f'{stamp}.json', manifest)
    return manifest


def read_snapshot(ref):
    from snapshot_archive import read_snapshot as read
    return read(ref, ROOT)
