# -*- coding: utf-8 -*-
"""Merge community_directory observations (5i5j/58/anjuke) into the canonical catalog.

Reads ``output/community_directory/records.json`` and routes every record through
``community_catalog.decide_match``: verified reuse, auto-accept, alias merge, new
record, or pending-location. Raw source evidence and coordinates stay on the source
entity; a source coordinate whose system is unknown is stored as ``other`` and is
never compared as WGS84.

python data_processing/community_directory_publish.py --records output/community_directory/records.json
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from data_store import ROOT, atomic_json
from coordinate_audit import bd09_to_wgs84, gcj02_to_wgs84
import community_catalog as catalog

OUT = ROOT / 'output/community_directory'
GROUP_BY_SOURCE = {'我爱我家': '5i5j', '58同城': '58', '安居客': 'anjuke'}
# Verified against OSM same-name points (see coordinate_audit.py); convert before matching.
SOURCE_COORD_SYSTEM = {'5i5j': 'bd09'}
CONVERT_TO_WGS84 = {'bd09': bd09_to_wgs84, 'gcj02': gcj02_to_wgs84}


def now():
    return datetime.now(timezone.utc).isoformat()


def city_codes():
    try:
        from metro_proximity import _load_city_codes
        return _load_city_codes() or {}
    except Exception:
        return {}


def source_group(record):
    return record.get('source_group') or GROUP_BY_SOURCE.get(record.get('source'), 'other')


def source_object_id(record):
    """Stable per-source object id; never derived from account/session data."""
    url = record.get('detail_url') or record.get('source_url') or ''
    group = source_group(record)
    if group == '5i5j':
        match = re.search(r'/xiaoqu/(\d+)\.html', url)
        return f'5i5j:{match.group(1)}' if match else None
    if group == '58':
        match = re.search(r'/xiaoqu/([A-Za-z0-9_-]+)/', url)
        return f'58:{match.group(1)}' if match else None
    if group == 'anjuke':
        stable = record.get('stable_id')
        if stable:
            return f'anjuke:{stable}'
        match = re.search(r'/(?:community|xiaoqu)/(\d+)', url)
        return f'anjuke:{match.group(1)}' if match else None
    return None


def year_fields(record):
    start = record.get('year')
    end = record.get('year_end')
    text = record.get('year_text')
    if start is None and text:
        match = re.search(r'(\d{4})', text)
        start = int(match.group(1)) if match else None
    if end is None:
        end = start
    if start is None:
        precision = None
    elif end and end != start:
        precision = 'range'
    elif text and ('约' in text or '参考' in text):
        precision = 'approximate'
    else:
        precision = 'exact'
    return start, end, precision, text


def residential_forms(record):
    tags = list(record.get('tags') or record.get('type_tags') or [])
    if '别墅' in tags:
        return ['别墅'], []
    return [], []


def observation(record, codes):
    start, end, precision, text = year_fields(record)
    forms, subtypes = residential_forms(record)
    lng, lat = record.get('lng'), record.get('lat')
    declared = record.get('coord_system') or SOURCE_COORD_SYSTEM.get(source_group(record))
    if lng is not None and lat is not None:
        if declared in CONVERT_TO_WGS84:
            lat, lng = CONVERT_TO_WGS84[declared](lat, lng)
            coord_system, precision = 'wgs84', 'point'
        elif declared == 'wgs84':
            coord_system, precision = 'wgs84', 'point'
        else:
            # Unverified source coordinates stay on the payload but never match as WGS84.
            coord_system, precision = 'other', 'unknown'
    else:
        coord_system, precision = None, 'unknown'
    return dict(
        source=source_group(record), source_id=source_object_id(record),
        name=record.get('name'), aliases=record.get('aliases') or [],
        phase=record.get('phase'),
        address=record.get('address') or record.get('address_text') or record.get('location_text'),
        city_code=codes.get(record.get('city')), district_code=record.get('district_code'),
        longitude=lng, latitude=lat,
        coord_system=coord_system, precision_level=precision,
        has_osm=False, year_start=start, year_end=end, year_precision=precision,
        year_meaning='completion', year_scope='community', year_label=text,
        residential_forms=forms, villa_subtypes=subtypes,
        pure_villa='别墅' in (record.get('tags') or record.get('type_tags') or []),
        source_url=record.get('detail_url') or record.get('source_url'),
        fetched_at=record.get('fetched_at') or now())


def load_records(path):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, dict):
        return payload.get('records') or []
    return payload


def merge(records_path=None, db=None, limit=None, out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    records_path = Path(records_path) if records_path else out / 'records.json'
    if not records_path.exists():
        raise ValueError(f'No community_directory records at {records_path}')
    records = load_records(records_path)[:limit] if limit else load_records(records_path)
    codes = city_codes()
    conn = catalog.connect(db)
    catalog.create_schema(conn)
    actions = Counter()
    by_source = Counter()
    errors = []
    try:
        for record in records:
            group = source_group(record)
            by_source[group] += 1
            try:
                obs = observation(record, codes)
                if not obs['name'] or not obs['source_id']:
                    actions['skipped_incomplete'] += 1
                    continue
                result = catalog.decide_match(conn, obs)
                actions[result.get('action') or result.get('case') or 'unknown'] += 1
            except Exception as exc:
                actions['error'] += 1
                errors.append(dict(name=record.get('name'), source=group,
                                   error=f'{type(exc).__name__}: {exc}'[:200]))
        report = dict(schema='community_directory_merge/v1', at=now(), records=len(records),
                      by_source=dict(by_source), actions=dict(actions), errors=errors[:50])
        atomic_json(out / 'catalog_merge.json', report)
        return report
    finally:
        conn.close()


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path)
    parser.add_argument('--db', help='catalog sqlite path; default output/catalog/catalog.sqlite3')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args(argv)
    report = merge(args.records, args.db, args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
