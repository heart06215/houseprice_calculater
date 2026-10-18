# -*- coding: utf-8 -*-
"""进阶小区目录采集：我爱我家移动站（免登录 HTTP）、58 同城与安居客移动（浏览器）。

- `5i5j`：`m.5i5j.com/{slug}/xiaoqu/` 免登录、无挑战、纯 HTTP；列表卡片含
  区域/约建成年份/楼型/单价/在售在租，详情页含「建成年代」与来源经纬度。
  不使用任何账号、Cookie、本机浏览器资料。
- `58` / `anjuke`：同一 58/安居客反爬网关（`@@xxzlGatewayUrl` →
  `callback.58.com/antibot/verifycode`）。本模块用一个浏览器上下文复用 cookie，
  按可见元素走既有的滑块/一键验证状态机，同一会话只在第一页触发一次验证。

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

import requests
from bs4 import BeautifulSoup

from data_store import ROOT, atomic_json, snapshot

OUT = ROOT / 'output/community_directory'
SLUG_CONFIG = Path(__file__).with_name('config') / 'community_city_slugs.json'
CITY_SLUG = {'北京': 'bj', '上海': 'sh', '广州': 'gz', '深圳': 'sz', '天津': 'tj',
             '南京': 'nj', '杭州': 'hz', '武汉': 'wh', '成都': 'cd', '重庆': 'cq',
             '苏州': 'su', '西安': 'xa', '长沙': 'cs', '青岛': 'qd', '郑州': 'zz'}
_SLUGS = None
YEAR_RE = re.compile(r'(\d{4})\s*年')
LOCATION_SPLIT_RE = re.compile(r'\s*[-－—]\s*')


def now():
    return datetime.now(timezone.utc).isoformat()


def slug_config(path=None):
    """Measured per-source city slugs; a missing city means that source has no entry."""
    global _SLUGS
    if path is not None:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    if _SLUGS is None:
        try:
            _SLUGS = json.loads(SLUG_CONFIG.read_text(encoding='utf-8'))
        except Exception:
            _SLUGS = {}
    return _SLUGS


def city_slug(city):
    return CITY_SLUG.get(city)


def source_city_slug(source, city, config=None):
    """Per-source slug; a source table without the city means that source lacks it."""
    table = (config or slug_config()).get(source)
    if table is not None:
        return table.get(city)
    return CITY_SLUG.get(city)


def list_url(source, slug, page=1):
    if source == '58':
        return f'https://{slug}.58.com/xiaoqu/' if page <= 1 else f'https://{slug}.58.com/xiaoqu/p{page}/'
    if source == '5i5j':
        return (f'https://m.5i5j.com/{slug}/xiaoqu/' if page <= 1
                else f'https://m.5i5j.com/{slug}/xiaoqu/p{page}/')
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


def parse_58_districts(html, url):
    """58 列表页的区县入口：/xiaoqu/{数字}/ 才是区县，详情 slug 不是。"""
    doc = BeautifulSoup(html or '', 'html.parser')
    found = {}
    for anchor in doc.select('a[href*="/xiaoqu/"]'):
        href = anchor.get('href') or ''
        match = re.search(r'/xiaoqu/(\d+)/?$', href)
        name = anchor.get_text(' ', strip=True)
        if match and name and name != '不限':
            found[urljoin(url, href)] = name
    return [dict(name=name, url=link) for link, name in sorted(found.items())]


def district_page_url(district_url, page):
    if page <= 1:
        return district_url
    return district_url.rstrip('/') + f'/p{page}/'


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


FIVEI5J_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
              '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')


def parse_5i5j_list(html, *, city, url, fetched_at, raw=None, snapshot_path=None):
    """我爱我家移动小区列表：卡片是 div.tz_a[data-href]，含区域/约年份/楼型/单价/在售在租。"""
    doc = BeautifulSoup(html or '', 'html.parser')
    records = {}
    for card in doc.select('div.tz_a'):
        href = card.get('data-href') or ''
        if '/xiaoqu/' not in href:
            continue
        title = card.select_one('h2')
        name = title.get_text(' ', strip=True) if title else None
        if not name:
            continue
        liked = card.select_one('.xq_gz')
        price = None
        if liked and liked.get('data-price'):
            try:
                price = float(liked.get('data-price'))
            except (TypeError, ValueError):
                price = None
        ting = card.select_one('.ting')
        parts = [p.strip() for p in (ting.get_text(' ', strip=True) if ting else '').split('/') if p.strip()]
        location = parts[0] if parts else None
        year_text = next((p for p in parts if '年' in p), None)
        building = next((p for p in parts if '年' not in p and p != location), None)
        counts = card.select_one('.sp_hui')
        counts_text = counts.get_text(' ', strip=True) if counts else ''
        sale = re.search(r'在售\s*(\d+)', counts_text)
        rent = re.search(r'在租\s*(\d+)', counts_text)
        detail_url = urljoin(url, href)
        records[detail_url] = dict(
            schema='community_directory/v1', source='我爱我家', source_group='5i5j', city=city,
            name=name, detail_url=detail_url, source_url=detail_url, fetched_at=fetched_at,
            raw=raw, snapshot=snapshot_path, price=price,
            price_text=f'{price}元/㎡' if price else None,
            location_text=location, year_text=year_text, year=year_from_text(year_text),
            year_meaning='completion', year_scope='community', building_type=building,
            on_sale_count=int(sale.group(1)) if sale else None,
            on_rent_count=int(rent.group(1)) if rent else None,
            lng=None, lat=None, coord_system=None, coord_precision_level='unknown')
    return list(records.values())


def parse_5i5j_detail(html):
    """详情页补充：建成年代、小区楼型、来源经纬度（坐标系未标注）。"""
    doc = BeautifulSoup(html or '', 'html.parser')
    text = re.sub(r'\s+', ' ', doc.get_text(' ', strip=True))
    year_text = re.search(r'建成年代[：:]\s*([^\s]+)', text)
    building = re.search(r'小区楼型[：:]\s*(\S+)', text)
    lng = re.search(r'Lng\s*=\s*"([\d.]+)"', html or '')
    lat = re.search(r'Lat\s*=\s*"([\d.]+)"', html or '')
    start, end = None, None
    if year_text:
        span = re.search(r'(\d{4})\s*[-~至到]\s*(\d{4})', year_text.group(1))
        if span:
            start, end = int(span.group(1)), int(span.group(2))
        else:
            start = end = year_from_text(year_text.group(1))
    return dict(year_text=year_text.group(1) if year_text else None,
                year=start, year_end=end,
                building_type=building.group(1) if building else None,
                lng=float(lng.group(1)) if lng else None,
                lat=float(lat.group(1)) if lat else None,
                # Verified 2026-10-18 by同名互证 against OSM Beijing (median 31 m).
                coord_system='bd09' if lng else None,
                coord_system_evidence=('来源页面含 Lng/Lat；与 OSM 同名点互证判定为 BD-09'
                                       '(中位 31 m)，合并时转 WGS84') if lng else None)


def http_get(session, url, *, out, key, retries=3):
    """Plain HTTP GET with evidence; no cookie, profile or account is ever used."""
    last = None
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=(15, 30), allow_redirects=True)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(2 * (attempt + 1))
            continue
        html = response.content.decode('utf-8', 'ignore')
        raw = snapshot('community_directory', response.content, 'raw', 'html')
        meta = dict(url=url, final_url=response.url, status=response.status_code,
                    bytes=len(response.content), sha256=raw.get('sha256'), raw=raw,
                    fetched_at=now(), key=key)
        if response.status_code == 200:
            return html, meta
        last = f'HTTP {response.status_code}'
        time.sleep(2 * (attempt + 1))
    return None, dict(url=url, status=0, error=last, raw=None)


def collect_5i5j(city, *, pages=3, details=0, interval=2.0, out=OUT, max_seconds=2400):
    slug = source_city_slug('5i5j', city)
    if not slug:
        raise ValueError(f'5i5j has no configured city entry for {city}')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    session.headers.update({'User-Agent': FIVEI5J_UA, 'Accept-Language': 'zh-CN,zh;q=0.9',
                            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'})
    report = dict(schema='community_directory/v1', source='5i5j', city=city, started_at=now(),
                  pages=[], list_records=0, details=0, errors=[])
    store = {}
    deadline = time.monotonic() + max_seconds
    for page_no in range(1, pages + 1):
        if time.monotonic() > deadline:
            report['errors'].append('time_budget_exhausted')
            break
        url = list_url('5i5j', slug, page_no)
        html, meta = http_get(session, url, out=out, key=f'5i5j_{slug}_{page_no}')
        if html is None:
            report['errors'].append(f'list_{page_no}:{meta.get("error") or meta.get("status")}')
            break
        records = parse_5i5j_list(html, city=city, url=meta['final_url'], fetched_at=meta['fetched_at'],
                                  raw=meta['raw'])
        merge_records(store, records)
        report['pages'].append(dict(page=page_no, url=url, final_url=meta['final_url'],
                                    records=len(records), sha256=meta['sha256']))
        report['list_records'] = len(store)
        print(json.dumps(report['pages'][-1], ensure_ascii=False), flush=True)
        if page_no < pages:
            time.sleep(interval)
    if details > 0:
        targets = [r for r in store.values() if r.get('detail_url')][:details]
        for record in targets:
            if time.monotonic() > deadline:
                report['errors'].append('detail_time_budget_exhausted')
                break
            html, meta = http_get(session, record['detail_url'], out=out,
                                  key=f'5i5j_detail_{record["name"]}')
            if html is None:
                report['errors'].append(f'detail:{record["name"]}')
                continue
            parsed = parse_5i5j_detail(html)
            for field in ('year_text', 'year', 'year_end', 'building_type', 'lng', 'lat',
                          'coord_system', 'coord_system_evidence'):
                if parsed.get(field) not in (None, '', []):
                    record[field] = parsed[field]
            record['detail_snapshot'] = meta.get('sha256')
            report['details'] += 1
            time.sleep(interval)
    records = list(store.values())
    (out / f'5i5j_{city}.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    atomic_json(out / 'records.json', {'schema': 'community_directory/v1', 'source': '5i5j',
                                       'city': city, 'fetched_at': now(), 'count': len(records),
                                       'records': records})
    report.update(finished_at=now(), count=len(records))
    atomic_json(out / 'report.json', report)
    return report


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


def finalize(out, source, city, records, report):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{source}_{city}.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    atomic_json(out / 'records.json', {'schema': 'community_directory/v1', 'source': source,
                                       'city': city, 'fetched_at': now(), 'count': len(records),
                                       'records': records})
    report.update(finished_at=now(), count=len(records))
    atomic_json(out / 'report.json', report)
    return report


def collect_58_districts(city, *, slug, max_districts=0, pages_per_district=50, details=0,
                         interval=2.0, out=OUT, max_seconds=3600):
    """58 全量轮转：总列表 → 区县入口 → 每区县翻页，去重累积。"""
    from browser_runtime import browser_context
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report = dict(schema='community_directory/v1', source='58', city=city, mode='districts',
                  started_at=now(), districts=[], pages=[], list_records=0, details=0, errors=[])
    store = {}
    deadline = time.monotonic() + max_seconds
    with browser_context(headless=True, locale='zh-CN', timezone_id='Asia/Shanghai',
                         viewport={'width': 1440, 'height': 1000}) as context:
        page = context.new_page()
        base = list_url('58', slug, 1)
        result = None
        for attempt in range(3):
            result = fetch_page(page, context, base, out, f'58_{slug}_base_{attempt}')
            if not result['challenge']:
                break
            time.sleep(3)
        districts = [] if result['challenge'] else parse_58_districts(result['html'], result['final_url'])
        if result['challenge']:
            report['errors'].append('challenge_on_base')
        if not districts:
            # The gateway can be transient; fall back to the total-list pagination.
            for page_no in range(1, pages_per_district + 1):
                if time.monotonic() > deadline:
                    report['errors'].append('time_budget_exhausted')
                    break
                url = list_url('58', slug, page_no)
                attempt = fetch_page(page, context, url, out, f'58_{slug}_list_{page_no}')
                if attempt['challenge']:
                    report['errors'].append(f'challenge:list:p{page_no}')
                    break
                raw = snapshot('community_directory', attempt['html'].encode('utf-8'), 'raw', 'html')
                records = parse_58_list(attempt['html'], city=city, url=attempt['final_url'],
                                        fetched_at=now(), raw=raw)
                before = len(store)
                merge_records(store, records)
                new = len(store) - before
                report['pages'].append(dict(district=None, page=page_no, records=len(records),
                                            new=new, url=url))
                report['list_records'] = len(store)
                if new == 0 and page_no > 1:
                    break
                time.sleep(interval)
            return finalize(out, '58', city, list(store.values()), report)
        if max_districts:
            districts = districts[:max_districts]
        for district in districts:
            if time.monotonic() > deadline:
                report['errors'].append('time_budget_exhausted')
                break
            empty = 0
            pages_done = 0
            for page_no in range(1, pages_per_district + 1):
                if time.monotonic() > deadline:
                    report['errors'].append('time_budget_exhausted')
                    break
                url = district_page_url(district['url'], page_no)
                attempt = None
                for retry in range(2):
                    attempt = fetch_page(page, context, url, out,
                                         f'58_{slug}_{district["name"]}_{page_no}_{retry}')
                    if not attempt['challenge']:
                        break
                    time.sleep(2)
                if attempt['challenge']:
                    report['errors'].append(f'challenge:{district["name"]}:p{page_no}')
                    break
                raw = snapshot('community_directory', attempt['html'].encode('utf-8'), 'raw', 'html')
                records = parse_58_list(attempt['html'], city=city, url=attempt['final_url'],
                                        fetched_at=now(), raw=raw)
                before = len(store)
                merge_records(store, records)
                new = len(store) - before
                pages_done += 1
                report['pages'].append(dict(district=district['name'], page=page_no,
                                            records=len(records), new=new, url=url))
                report['list_records'] = len(store)
                if new == 0:
                    empty += 1
                    if empty >= 2:
                        break
                else:
                    empty = 0
                time.sleep(interval)
            report['districts'].append(dict(name=district['name'], url=district['url'],
                                            pages=pages_done, count=len(store)))
            print(json.dumps(report['districts'][-1], ensure_ascii=False), flush=True)
    return finalize(out, '58', city, list(store.values()), report)


def collect(source, city, *, pages=3, details=0, interval=2.0, out=OUT, max_seconds=2400,
            districts=0):
    if source == '5i5j':
        return collect_5i5j(city, pages=pages, details=details, interval=interval,
                            out=out, max_seconds=max_seconds)
    from browser_runtime import browser_context
    slug = source_city_slug(source, city)
    if not slug:
        raise ValueError(f'{source} has no configured city entry for {city}')
    if source == '58' and districts:
        return collect_58_districts(city, slug=slug, max_districts=districts,
                                    pages_per_district=pages, details=details,
                                    interval=interval, out=out, max_seconds=max_seconds)
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
    return finalize(out, source, city, list(store.values()), report)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['5i5j', '58', 'anjuke'], required=True)
    parser.add_argument('--city', default='北京')
    parser.add_argument('--pages', type=int, default=3)
    parser.add_argument('--details', type=int, default=0, help='仅 anjuke：详情页补充数量')
    parser.add_argument('--districts', type=int, default=0,
                        help='仅 58：按区县分片轮转，最多 N 个区县（0 关闭，99 视为全部）')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 60 or not 0 <= args.details <= 2000 or args.interval < 1 \
            or not 0 <= args.districts <= 200:
        parser.error('pages 1..60, details 0..2000, districts 0..200, interval >= 1')
    if not source_city_slug(args.source, args.city):
        parser.error(f'{args.source} 未配置城市入口: ' + args.city)
    report = collect(args.source, args.city, pages=args.pages, details=args.details,
                     interval=args.interval, out=args.out, districts=args.districts)
    print(json.dumps({k: report[k] for k in ('source', 'city', 'list_records', 'details', 'count', 'errors')},
                     ensure_ascii=False, indent=2))
    return 0 if report.get('count') else 2


if __name__ == '__main__':
    raise SystemExit(main())
