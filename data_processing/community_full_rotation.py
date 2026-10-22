# -*- coding: utf-8 -*-
"""Resumable full 58 rotation for one city (district shards + detail batched).

State is kept in ``output/community_directory/full_state.json`` so a daily Actions
run continues where the previous one stopped. The runner cache restores the whole
output directory; no account, cookie, profile or local path is used.

python data_processing/community_full_rotation.py --city 北京 --max-minutes 240 --details-batch 1200
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from data_store import ROOT, atomic_json, snapshot
import community_directory as D

OUT = ROOT / 'output/community_directory'


def state_path(out):
    return Path(out) / 'full_state.json'


def load_state(out):
    path = state_path(out)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def save_state(out, state):
    atomic_json(state_path(out), state)


def load_records(out, source, city):
    path = Path(out) / f'{source}_{city}.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def save_records(out, city, records, report):
    D.finalize(out, '58', city, records, report)


def full_58(city, *, out=OUT, max_minutes=240, details_batch=1200, interval=1.5, max_pages=50):
    from browser_runtime import browser_context
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    slug = D.source_city_slug('58', city)
    if not slug:
        raise ValueError(f'58 has no configured city entry for {city}')
    started = D.now()
    state = load_state(out)
    key = f'58:{city}'
    st = state.setdefault(key, dict(city=city, slug=slug, base_done=False, list_done=False,
                                    districts={}, details_done=[], pages=0, details=0,
                                    started_at=started))
    records = load_records(out, '58', city)
    by_url = {r.get('detail_url') or r.get('name'): r for r in records}
    report = dict(schema='community_full_rotation/v1', source='58', city=city, mode='full',
                  started_at=started, list_records=0, details=0, errors=[])
    deadline = time.monotonic() + max_minutes * 60

    def persist():
        report['list_records'] = len(by_url)
        report['details'] = st.get('details', 0)
        report['finished_at'] = D.now()
        report['state'] = dict(base_done=st['base_done'], list_done=st['list_done'],
                               districts=len(st['districts']), pages=st['pages'],
                               details_done=len(st['details_done']))
        save_records(out, city, list(by_url.values()), report)
        save_state(out, state)

    with browser_context(headless=True, locale='zh-CN', timezone_id='Asia/Shanghai',
                         viewport={'width': 1440, 'height': 1000}) as context:
        page = context.new_page()
        if not st['base_done']:
            try:
                attempt = D.fetch_page(page, context, D.list_url('58', slug, 1), out,
                                       f'full58_{city}_base')
            except Exception as exc:
                report['errors'].append(f'base:{type(exc).__name__}')
                persist()
                return report
            if attempt['challenge']:
                report['errors'].append('challenge_on_base')
                persist()
                return report
            districts = D.parse_58_districts(attempt['html'], attempt['final_url'])
            st['districts'] = {d['url']: dict(name=d['name'], pages=0, done=False) for d in districts}
            st['base_done'] = True
            save_state(out, state)
        for url, info in st['districts'].items():
            if info.get('done'):
                continue
            page_no = info.get('pages', 0) + 1
            while page_no <= max_pages and time.monotonic() < deadline:
                try:
                    attempt = D.fetch_page(page, context, D.district_page_url(url, page_no),
                                           out, f'full58_{city}_{info["name"]}_{page_no}')
                except Exception as exc:
                    report['errors'].append(f'{info["name"]}:{page_no}:{type(exc).__name__}')
                    break
                if attempt['challenge']:
                    report['errors'].append(f'challenge:{info["name"]}:{page_no}')
                    break
                raw = snapshot('community_directory', attempt['html'].encode('utf-8'), 'raw', 'html')
                rows = D.parse_58_list(attempt['html'], city=city, url=attempt['final_url'],
                                       fetched_at=D.now(), raw=raw)
                new = 0
                for row in rows:
                    item = row.get('detail_url') or row.get('name')
                    if item not in by_url:
                        by_url[item] = row
                        new += 1
                info['pages'] = page_no
                st['pages'] = st.get('pages', 0) + 1
                if not rows or new == 0:
                    info['done'] = True
                    break
                page_no += 1
                if page_no > max_pages:
                    info['done'] = True
                persist()
                time.sleep(interval)
            if all(d.get('done') for d in st['districts'].values()):
                st['list_done'] = True
            persist()
        details_done = set(st.get('details_done') or [])
        pending = [r for r in by_url.values()
                   if r.get('detail_url') and r['detail_url'] not in details_done]
        fetched = 0
        for record in pending:
            if time.monotonic() >= deadline or fetched >= details_batch:
                break
            try:
                attempt = D.fetch_page(page, context, record['detail_url'], out,
                                       f'full58_detail_{len(details_done)}')
            except Exception as exc:
                report['errors'].append(f'detail:{record.get("name")}:{type(exc).__name__}')
                break
            if attempt['challenge']:
                report['errors'].append(f'detail_challenge:{record.get("name")}')
                break
            parsed = D.parse_58_detail(attempt['html'])
            for field in ('lng', 'lat', 'coord_system', 'address', 'year_text', 'year',
                          'building_type'):
                if parsed.get(field) not in (None, '', []):
                    record[field] = parsed[field]
            record['detail_fetched_at'] = D.now()
            record['detail_snapshot'] = snapshot('community_directory', attempt['html'].encode('utf-8'),
                                                 'raw', 'html').get('sha256')
            details_done.add(record['detail_url'])
            st['details_done'] = sorted(details_done)
            st['details'] = st.get('details', 0) + 1
            fetched += 1
            if fetched % 20 == 0:
                persist()
            time.sleep(interval)
        persist()
    return report


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city', default='北京')
    parser.add_argument('--max-minutes', type=float, default=240)
    parser.add_argument('--details-batch', type=int, default=1200)
    parser.add_argument('--interval', type=float, default=1.5)
    parser.add_argument('--max-pages', type=int, default=50)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if args.max_minutes < 1 or args.details_batch < 1:
        parser.error('max-minutes and details-batch must be positive')
    result = full_58(args.city, out=args.out, max_minutes=args.max_minutes,
                     details_batch=args.details_batch, interval=args.interval,
                     max_pages=args.max_pages)
    print(json.dumps({k: result.get(k) for k in ('city', 'list_records', 'details', 'errors', 'state')},
                     ensure_ascii=False, indent=2))
    return 0 if result.get('list_records') else 2


if __name__ == '__main__':
    raise SystemExit(main())
