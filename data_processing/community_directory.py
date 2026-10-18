# -*- coding: utf-8 -*-
"""进阶小区目录采集：安居客移动目录（分页+详情）与 58 同城小区目录。

两站都用同一个 58/安居客反爬网关（`@@xxzlGatewayUrl` → `callback.58.com/antibot/verifycode`）。
本模块用一个浏览器上下文复用 cookie，按可见元素走既有的滑块/一键验证状态机，
因此同一会话只在第一页触发一次验证，后续分页与详情直接复用。

字段边界：
- 58 列表自带“YYYY年竣工 + 区县 - 商圈 - 地址 + 标签（含别墅）+ 在售套数”；
- 安居客列表给名称/形态标签/位置，详情页补年代；
- 坐标若来自页面且未标注坐标系，一律标 `coord_system='unknown'`，不得直接当 WGS84。

python data_processing/community_directory.py --source 58 --city 北京 --pages 3
python data_processing/community_directory.py --source anjuke --city 北京 --pages 7 --details 60
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from data_store import ROOT, atomic_json, snapshot

OUT = ROOT / 'output/community_directory'
CITY_SLUG = {'北京': 'bj', '上海': 'sh', '广州': 'gz', '深圳': 'sz', '天津': 'tj',
             '南京': 'nj', '杭州': 'hz', '武汉': 'wh', '成都': 'cd', '重庆': 'cq',
             '苏州': 'su', '西安': 'xa', '长沙': 'cs', '青岛': 'qd', '郑州': 'zz'}
YEAR_RE = re.compile(r'(\d{4})\s*年')
LOCATION_SPLIT_RE = re.compile(r'\s*[-－—]\s*')


def now():
    return datetime.now(timezone.utc).isoformat()


def city_slug(city):
    return CITY_SLUG.get(city)


def list_url(source, slug, page=1):
    if source == '58':
        return f'https://{slug}.58.com/xiaoqu/' if page <= 1 else f'https://{slug}.58.com/xiaoqu/p{page}/'
    return f'https://m.anjuke.com/{slug}/xiaoqu/' if page <= 1 else f'https://m.anjuke.com/{slug}/xiaoqu-p{page}/'


def split_location(text):
    """'朝阳 - 华威桥 - 松榆南路38号' -> (district, subarea, address)."""
    parts = [p.strip() for p in LOCATION_SPLIT_RE.split(text or '') if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], ' - '.join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) == 1:
        return None, None, parts[0]
    return None, None, None


def year_from_text(text):
    match = YEAR_RE.search(text or '')
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def parse_58_list(html, *, city, url, fetched_at, raw=None, snapshot_path=None):
    """58 小区列表：每张 a.li-row 一条，含竣工年代与区县-商圈-地址。"""
    doc = BeautifulSoup(html or '', 'html.parser')
    records = {}
    for anchor in doc.select('a.li-row'):
        href = anchor.get('href') or ''
        if '/xiaoqu/' not in href:
            continue
        title = anchor.select_one('.li-community-title')
        name = title.get_text(' ', strip=True) if title else None
        if not name:
            continue
        spans = [re.sub(r'\s+', ' ', node.get_text(' ', strip=True))
                 for node in anchor.select('.props > span') if node.get_text(strip=True)]
        year_text = next((s for s in spans if '竣工' in s or '建成' in s), None)
        location = next((s for s in spans if LOCATION_SPLIT_RE.search(s)), None)
        district, subarea, address = split_location(location)
        tags = [t.get_text(strip=True) for t in anchor.select('.prop-tag') if t.get_text(strip=True)]
        sale = None
        sale_link = anchor.select_one('.detail-link a')
        if sale_link:
            sale = re.search(r'\((\d+)\)', sale_link.get_text())
            sale = int(sale.group(1)) if sale else None
        detail_url = urljoin(url, href)
        record = dict(schema='community_directory/v1', source='58同城', source_group='58',
                      city=city, name=name, detail_url=detail_url, source_url=detail_url,
                      fetched_at=fetched_at, raw=raw, snapshot=snapshot_path,
                      year_text=year_text, year=year_from_text(year_text),
                      year_meaning='completion', year_scope='community',
                      district=district, subarea=subarea, address=address,
                      tags=tags, pure_villa=('别墅' in tags), on_sale_count=sale,
                      lng=None, lat=None, coord_system=None, coord_precision_level='unknown')
        records[detail_url] = record
    return list(records.values())


def merge_records(store, incoming):
    for record in incoming:
        key = record.get('detail_url') or record.get('name')
        current = store.get(key)
        if not current:
            store[key] = record
            continue
        for field in ('year_text', 'district', 'subarea', 'address', 'lng', 'lat'):
            if not current.get(field) and record.get(field):
                current[field] = record[field]
        if not current.get('tags') and record.get('tags'):
            current['tags'] = record['tags']
    return list(store.values())


def fetch_page(page, context, url, out, key, *, solve=True, deadline_s=60, timeout=45000, max_seconds=600):
    """Navigate an existing context, solve visible challenges, return HTML; cookies persist."""
    from anjuke_anonymous import next_challenge_action, page_is_challenge
    actions, sliders, intelli = [], [], 0
    html, final_url, status = '', url, 0
    network = []

    def on_response(response):
        network.append(dict(url=response.url))

    page.on('response', on_response)
    try:
        response = page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        status = response.status if response else 0
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                html, final_url = page.content(), page.url
            except Exception:
                page.wait_for_timeout(200)
                continue
            if not solve or not page_is_challenge(final_url, html):
                break
            action = next_challenge_action(page, context, out, key, actions, sliders, intelli,
                                           solve=solve, network=network)
            if action:
                actions.append(action)
                if action == 'click_intellisense':
                    intelli += 1
            page.wait_for_timeout(1200 if action else 500)
        try:
            html, final_url = page.content(), page.url
        except Exception:
            pass
    finally:
        try:
            page.remove_listener('response', on_response)
        except Exception:
            pass
    return dict(html=html, final_url=final_url, status=status, actions=actions,
                challenge=page_is_challenge(final_url, html))


def collect(source, city, *, pages=3, details=0, interval=2.0, out=OUT, max_seconds=2400):
    from browser_runtime import browser_context
    slug = city_slug(city)
    if not slug:
        raise ValueError(f'Unknown city slug for {city}')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report = dict(schema='community_directory/v1', source=source, city=city, started_at=now(),
                  pages=[], list_records=0, details=0, errors=[])
    store = {}
    deadline = time.monotonic() + max_seconds
    with browser_context(headless=True, locale='zh-CN', timezone_id='Asia/Shanghai',
                         viewport={'width': 1440, 'height': 1000}) as context:
        page = context.new_page()
        for page_no in range(1, pages + 1):
            if time.monotonic() > deadline:
                report['errors'].append('time_budget_exhausted')
                break
            url = list_url(source, slug, page_no)
            try:
                result = fetch_page(page, context, url, out, f'{source}_{slug}_{page_no}')
            except Exception as exc:
                report['errors'].append(f'list_{page_no}:{type(exc).__name__}')
                break
            if result['challenge']:
                report['errors'].append(f'challenge_on_page_{page_no}')
                break
            raw = snapshot('community_directory', result['html'].encode('utf-8'), 'raw', 'html')
            if source == '58':
                records = parse_58_list(result['html'], city=city, url=result['final_url'],
                                        fetched_at=now(), raw=raw)
            else:
                import anjuke_community as ac
                parsed = ac.parse_catalog(result['html'], url=result['final_url'], fetched_at=now(),
                                          snapshot_path=raw.get('path'))
                records = parsed.get('records') if isinstance(parsed, dict) else parsed
            merge_records(store, records)
            report['pages'].append(dict(page=page_no, url=url, final_url=result['final_url'],
                                        records=len(records), actions=result['actions'],
                                        sha256=raw.get('sha256')))
            report['list_records'] = len(store)
            print(json.dumps(report['pages'][-1], ensure_ascii=False), flush=True)
            if page_no < pages:
                time.sleep(interval)
        if source == 'anjuke' and details > 0:
            import anjuke_community as ac
            targets = [r for r in store.values() if r.get('detail_url')][:details]
            for record in targets:
                if time.monotonic() > deadline:
                    report['errors'].append('detail_time_budget_exhausted')
                    break
                try:
                    result = fetch_page(page, context, record['detail_url'], out, f'ajk_detail_{record.get("stable_id") or record["name"]}')
                    if result['challenge']:
                        report['errors'].append(f'detail_challenge:{record["name"]}')
                        continue
                    parsed = ac.parse_detail(result['html'], url=result['final_url'], fetched_at=now())
                    for field in ('year_text', 'year', 'address_text', 'district_text', 'subarea_text',
                                  'type_tags', 'lng', 'lat', 'coord_system_evidence', 'price_text'):
                        if parsed.get(field) not in (None, '', []):
                            record[field] = parsed[field]
                    record['detail_fetched_at'] = now()
                    record['detail_snapshot'] = snapshot('community_directory', result['html'].encode('utf-8'),
                                                         'raw', 'html').get('sha256')
                    report['details'] += 1
                except Exception as exc:
                    report['errors'].append(f'detail:{record["name"]}:{type(exc).__name__}')
                time.sleep(interval)
    records = list(store.values())
    payload = {'schema': 'community_directory/v1', 'source': source, 'city': city,
               'fetched_at': now(), 'count': len(records), 'records': records}
    (out / f'{source}_{city}.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    atomic_json(out / 'records.json', payload)
    report['finished_at'] = now()
    report['count'] = len(records)
    atomic_json(out / 'report.json', report)
    return report


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['58', 'anjuke'], required=True)
    parser.add_argument('--city', default='北京')
    parser.add_argument('--pages', type=int, default=3)
    parser.add_argument('--details', type=int, default=0, help='仅 anjuke：详情页补充数量')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 60 or not 0 <= args.details <= 2000 or args.interval < 1:
        parser.error('pages 1..60, details 0..2000, interval >= 1')
    if not city_slug(args.city):
        parser.error('未配置的城市: ' + args.city)
    report = collect(args.source, args.city, pages=args.pages, details=args.details,
                     interval=args.interval, out=args.out)
    print(json.dumps({k: report[k] for k in ('source', 'city', 'list_records', 'details', 'count', 'errors')},
                     ensure_ascii=False, indent=2))
    return 0 if report.get('count') else 2


if __name__ == '__main__':
    raise SystemExit(main())
