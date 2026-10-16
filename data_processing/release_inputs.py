"""Portable, hashed input snapshots for fresh CI checkouts and offline rebuilds."""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path, PurePosixPath

from data_store import ROOT

BUNDLE = Path('data_processing/published_inputs.zip')
INPUTS = (
    'output/macro/clean.json','output/macro/status.json',
    'output/income/clean.json', 'output/income/status.json',
    'output/income/archive_audit.json',
    'output/cih/clean.json', 'output/cih/land.json',
    'output/cih/ranking_source.json', 'output/cih/rankings.json',
    'output/market/quotes.json', 'output/policies/clean.json',
    'output/anjuke_recheck/annual_quotes.json',
    'output/anjuke_recheck/update_rotation.json',
    'output/engine_validation/index_sources.json',
)


def safe_path(value):
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'output':
        raise ValueError('Invalid snapshot path: ' + value)
    image = path.suffix in ('.jpg', '.jpeg', '.png') and path.parts[:4] == ('output','warehouse','raw','official_wage_image')
    attachment = path.suffix == '.xlsx' and path.parts[:4] == ('output','warehouse','raw','official_wage_attachment')
    if ':' in value or (path.suffix not in ('.json', '.html', '.js') and not image and not attachment):
        raise ValueError('Unsupported snapshot file: ' + value)
    return path.as_posix()


def references(value):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str) and value.get('sha256'):
            yield value
        # Older NBS records also carry a legacy raw_file label; their canonical
        # evidence is the nested path/sha256 object. Flat references belong to market quotes.
        if (isinstance(value.get('raw_file'), str) and value.get('raw_sha256')
                and value['raw_file'].replace('\\', '/').startswith('output/market/raw/')):
            yield {'path': value['raw_file'], 'sha256': value['raw_sha256']}
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def write_bundle(target, contents):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.zip.tmp')
    manifest = {}
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, content in contents:
                manifest[name] = hashlib.sha256(content).hexdigest()
                info = zipfile.ZipInfo(name, date_time=(2021, 4, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, content)
            bundle.writestr(zipfile.ZipInfo('manifest.json', date_time=(2021, 4, 1, 0, 0, 0)),
                            json.dumps(manifest, sort_keys=True))
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return len(manifest)


def capture(root=ROOT):
    root = Path(root)
    paths = {p: None for p in INPUTS if (root / p).exists()}
    paths.update({p.relative_to(root).as_posix(): None
                  for p in (root / 'output/engine_validation').glob('20??-??.json')})
    source_files = list(paths) + [p.relative_to(root).as_posix()
        for name in ('anjuke', 'stats_gov') for p in (root / 'output' / name).glob('*.json')]
    for name in source_files:
        for ref in references(json.loads((root / name).read_text(encoding='utf-8'))):
            path = safe_path(ref['path'])
            if paths.get(path) == ref['sha256']:
                continue
            if not (root / path).is_file():
                raise ValueError('Missing source evidence: ' + path)
            if hashlib.sha256((root / path).read_bytes()).hexdigest() != ref['sha256']:
                raise ValueError('Source evidence hash mismatch: ' + path)
            paths[path] = ref['sha256']
    return write_bundle(root / BUNDLE, ((name, (root / name).read_bytes()) for name in sorted(paths)))


def checkpoint_rotation(root=ROOT):
    """Persist the attempt cursor while keeping the last validated inputs and evidence."""
    root = Path(root)
    name = 'output/anjuke_recheck/update_rotation.json'
    cursor = root / name
    if not cursor.exists():
        return False
    content = cursor.read_bytes()
    if not isinstance(json.loads(content), dict):
        raise ValueError('Invalid Anjuke rotation cursor')
    contents = {}
    with zipfile.ZipFile(root / BUNDLE) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        for path, sha in manifest.items():
            path = safe_path(path)
            saved = bundle.read(path)
            if hashlib.sha256(saved).hexdigest() != sha:
                raise ValueError('Input snapshot hash mismatch: ' + path)
            contents[path] = saved
    if contents.get(name) == content:
        return False
    contents[name] = content
    write_bundle(root / BUNDLE, sorted(contents.items()))
    return True


def restore(root=ROOT):
    """Seed missing runtime files only; never overwrite newer local observations."""
    root = Path(root)
    with zipfile.ZipFile(root / BUNDLE) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        for name, sha in manifest.items():
            name = safe_path(name)
            content = bundle.read(name)
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError('Input snapshot hash mismatch: ' + name)
            target = root / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + '.restore.tmp')
                temp.write_bytes(content)
                os.replace(temp, target)
    return len(manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', action='store_true')
    args = parser.parse_args()
    print('Snapshot files:', capture() if args.capture else restore())
