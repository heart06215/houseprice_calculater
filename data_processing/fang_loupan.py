# -*- coding: utf-8 -*-
"""房天下楼盘库（newhouse.fang.com）采集：列表分页 + 详情 JSON-LD 结构化提取。

2026-10-18 逆向结论（实测，非推测）：
- 城市站是 ``https://{host}.newhouse.fang.com/house/s/``（host 如 bj/sh/gz）。
- 深分页会先跳到 ``check.3g.fang.com`` 的 SliderTools 前端滑块，但服务端只要求
  请求携带 ``otherid`` cookie；实测**写入任意 32 位十六进制值都放行**，无需浏览器、
  无需拖动。``otherid`` 由 ``rand-gen.js`` 按小时写入，这里按同一算法生成，仅作
  形似，值本身不被校验。
- 列表页 JSON-LD ``ItemList`` 给出 ``numberOfItems`` 与本页条目；分页路径为
  ``/house/s/b9{page}/``（第 2 页 ``b92``，第 29 页 ``b929``）。
- 详情页 JSON-LD ``RealEstateListing`` 给出名称/地址/板块/经纬度，
  ``Product.additionalProperty`` 给出开盘时间、物业类别、建筑类别、开发商等。

语义边界：开盘时间 ≠ 小区竣工年代；入库时标注 ``year_meaning='opening'`` 作为线索，
不与中原/安居客的“小区建成年代”混用。坐标为来源原值，未做坐标系验证前标 ``unknown``。

python data_processing/fang_loupan.py --cities 北京,上海 --max-pages 3 --max-details 60
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from data_store import ROOT, atomic_json, snapshot

OUT = ROOT / 'output/fang_loupan'
CITY_HOST = {'北京': 'bj', '上海': 'sh', '广州': 'gz', '深圳': 'sz', '天津': 'tj',
             '南京': 'nj', '杭州': 'hz', '武汉': 'wh', '成都': 'cd', '重庆': 'cq',
             '苏州': 'su', '西安': 'xa', '长沙': 'cs', '青岛': 'qd', '郑州': 'zz'}
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
CHECK_HOST = 'check.3g.fang.com'


def now():
    return datetime.now(timezone.utc).isoformat()


def otherid(moment=None):
    """Same hourly MD5 as rand-gen.js; the server only checks that otherid exists."""
    moment = moment or (datetime.now(timezone.utc) + timedelta(hours=8))
    salt = 'ETFio#dr' + moment.strftime('%Y%m%d%H') + 'lzkrZlt'
    return hashlib.md5(salt.encode('utf-8')).hexdigest()


def make_session(moment=None):
    session = requests.Session()
    session.trust_env = False
    session.headers.update({'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
    # The deep-pagination gate only requires this cookie to be present.
    session.cookies.set('otherid', otherid(moment), domain='.fang.com')
    return session


def city_host(city):
    return CITY_HOST.get(city)


def list_url(host, page=1):
    base = f'https://{host}.newhouse.fang.com/house/s/'
    return base if page <= 1 else f'https://{host}.newhouse.fang.com/house/s/b9{page}/'


def detail_url(host, loupan_id):
    return f'https://{host}.newhouse.fang.com/loupan/{loupan_id}.htm'


def is_check_page(final_url, body):
    return CHECK_HOST in (final_url or '') or 'checkyzm.min.js' in (body or '')


def _json_ld(body):
    blocks = []
    for match in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', body or '', re.S):
        try:
            blocks.append(json.loads(match.group(1)))
        except ValueError:
            continue
    return blocks


def parse_list(body):
    """Return {number_of_items, last_page, items:[{loupan_id,name,url,description}]}."""
    items = {}
    number = None
    for block in _json_ld(body):
        if not isinstance(block, dict) or block.get('@type') != 'ItemList':
            continue
        number = block.get('numberOfItems') or number
        for entry in block.get('itemListElement') or []:
            item = entry.get('item') if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                continue
            match = re.search(r'loupan/(\d+)\.htm', item.get('url') or '')
            if not match:
                continue
            items.setdefault(match.group(1), dict(loupan_id=match.group(1), name=item.get('name'),
                                                  url=item.get('url'), description=item.get('description')))
    if not items:  # fallback when the JSON-LD list is absent
        for match in re.finditer(r'loupan/(\d+)\.htm', body or ''):
            items.setdefault(match.group(1), dict(loupan_id=match.group(1), name=None, url=None, description=None))
    last_page = None
    tail = re.search(r'href="[^"]*?/house/s/b9(\d+)/"[^>]*>\s*尾页', body or '')
    if tail:
        last_page = int(tail.group(1))
    return dict(number_of_items=number, last_page=last_page, items=list(items.values()))


def _clean_name(listing, product):
    for text in ((product or {}).get('name'), (listing or {}).get('keywords')):
        if not text:
            continue
        head = re.split(r'[,\uFF0C]', text)[0].strip()
        head = re.split(r'\s+-\s+|-(?=北京|上海|广州|深圳|天津|重庆)', head)[0].strip()
        if 1 < len(head) <= 40:
            return head
    return None


def _props(product):
    values = {}
    for entry in (product or {}).get('additionalProperty') or []:
        if isinstance(entry, dict) and entry.get('name'):
            values[entry['name']] = entry.get('value')
    return values


def extract_detail(body, *, host=None, loupan_id=None, source_url=None, fetched_at=None, raw=None, city=None):
    """Detail page -> flat record. JSON-LD first; regex only as a documented fallback."""
    listing, product = None, None
    for block in _json_ld(body):
        if not isinstance(block, dict):
            continue
        types = block.get('@type')
        types = types if isinstance(types, list) else [types]
        if 'RealEstateListing' in types:
            listing = block
        elif 'Product' in types:
            product = block
    props = _props(product)
    geo = (listing or {}).get('geo') or {}
    address = (listing or {}).get('address') or {}
    placed = (listing or {}).get('containedInPlace') or {}
    latitude = _as_float(geo.get('latitude'))
    longitude = _as_float(geo.get('longitude'))
    if latitude is None or longitude is None:
        lat = re.search(r'"latitude":"?([\d.]+)', body or '')
        lng = re.search(r'"longitude":"?([\d.]+)', body or '')
        latitude = latitude if latitude is not None else _as_float(lat.group(1) if lat else None)
        longitude = longitude if longitude is not None else _as_float(lng.group(1) if lng else None)
    return dict(
        schema='fang_loupan/v1', source='房天下楼盘库', source_group='fang',
        city=city, loupan_id=loupan_id,
        source_id=f'fang:{host}:{loupan_id}' if host and loupan_id else None,
        source_url=source_url, fetched_at=fetched_at, raw=raw,
        name=_clean_name(listing, product),
        name_raw=(product or {}).get('name') or (listing or {}).get('name'),
        aliases=[(listing or {}).get('keywords')] if (listing or {}).get('keywords') else [],
        district=address.get('addressLocality') or props.get('所属区域') or None,
        area=placed.get('name') or props.get('所属板块') or None,
        address=(address.get('streetAddress') or props.get('楼盘地址') or '').strip() or None,
        longitude=longitude, latitude=latitude, coord_system='unknown', coord_precision_level='point',
        year_opening=props.get('开盘时间'), delivery=props.get('交房时间'),
        year_meaning='opening', year_scope='group', year_label='开盘时间',
        property_category=props.get('物业类别'), building_category=props.get('建筑类别'),
        developer=props.get('开发商') or ((product or {}).get('brand') or {}).get('name'),
        sales_status=props.get('销售状态'), ring_road=props.get('环线位置'),
        presale_permit=props.get('预售许可证'), main_layout=props.get('主力户型'),
        price_display=_as_float(((product or {}).get('offers') or {}).get('price')),
        description=(listing or {}).get('description'), properties=props,
    )


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch(session, url, *, kind, host, city, out):
    """GET with bounded retries; retain the raw body as evidence. Returns (body, meta)."""
    last = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=(15, 30), allow_redirects=True)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(2 * (attempt + 1))
            continue
        body = response.content.decode('utf-8', 'ignore')
        digest = hashlib.sha256(response.content).hexdigest()
        raw = snapshot('fang_loupan', response.content, 'raw', 'html')
        meta = dict(url=url, final_url=response.url, status=response.status_code,
                    bytes=len(response.content), sha256=digest, raw=raw, kind=kind,
                    host=host, city=city, fetched_at=now())
        if is_check_page(response.url, body):
            meta['check_page'] = True
            return None, meta
        if response.status_code != 200:
            last = f'HTTP {response.status_code}'
            time.sleep(2 * (attempt + 1))
            continue
        return body, meta
    return None, dict(url=url, status=0, error=last, kind=kind, host=host, city=city)


def collect_city(city, *, out=OUT, max_pages=3, max_details=200, interval=3.0, session=None,
                 max_seconds=1800):
    host = city_host(city)
    if not host:
        raise ValueError(f'Unknown Fang city host for {city}; add it to CITY_HOST')
    session = session or make_session()
    out = Path(out)
    deadline = time.monotonic() + max_seconds
    report = dict(city=city, host=host, pages=[], items=0, details=0, errors=[], records=[])
    seen = {}
    for page in range(1, max_pages + 1):
        if time.monotonic() > deadline:
            report['errors'].append('time_budget_exhausted')
            break
        body, meta = fetch(session, list_url(host, page), kind='list', host=host, city=city, out=out)
        report['pages'].append(meta)
        if body is None:
            if meta.get('check_page'):
                report['errors'].append(f'check_page_on_page_{page}')
            break
        parsed = parse_list(body)
        for item in parsed['items']:
            seen.setdefault(item['loupan_id'], item)
        if parsed['last_page'] and page >= parsed['last_page']:
            break
        if page < max_pages:
            time.sleep(interval)
    report['items'] = len(seen)
    for loupan_id, item in seen.items():
        if report['details'] >= max_details or time.monotonic() > deadline:
            break
        body, meta = fetch(session, detail_url(host, loupan_id), kind='detail', host=host, city=city, out=out)
        if body is None:
            report['errors'].append(f'detail_{loupan_id}:{meta.get("status") or meta.get("error")}')
            continue
        record = extract_detail(body, host=f'{host}.newhouse.fang.com', loupan_id=loupan_id,
                                source_url=detail_url(host, loupan_id), fetched_at=meta['fetched_at'],
                                raw=meta['raw'], city=city)
        if not record.get('name'):
            record['name'] = item.get('name')
        report['records'].append(record)
        report['details'] += 1
        time.sleep(interval)
    out.mkdir(parents=True, exist_ok=True)
    (out / f'loupans_{city}.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in report['records']), encoding='utf-8')
    return report


def crawl(cities=None, *, max_pages=3, max_details=200, interval=3.0, out=OUT, session=None):
    cities = cities or ['北京', '上海']
    session = session or make_session()
    result = dict(schema='fang_loupan/v1', started_at=now(), cities={}, records=0)
    for city in cities:
        report = collect_city(city, out=out, max_pages=max_pages, max_details=max_details,
                              interval=interval, session=session)
        result['cities'][city] = {k: report[k] for k in ('host', 'items', 'details', 'errors')}
        result['records'] += report['details']
        atomic_json(Path(out) / 'report.json', result)
    result['finished_at'] = now()
    atomic_json(Path(out) / 'report.json', result)
    return result


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cities', help='逗号分隔，如 北京,上海')
    parser.add_argument('--max-pages', type=int, default=3, help='每个城市最多翻页数')
    parser.add_argument('--max-details', type=int, default=200, help='每个城市最多详情页数')
    parser.add_argument('--interval', type=float, default=3.0, help='请求间隔秒，最小 1')
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if not 1 <= args.max_pages <= 200 or not 1 <= args.max_details <= 5000 or args.interval < 1:
        parser.error('max-pages 1..200, max-details 1..5000, interval >= 1')
    cities = args.cities.split(',') if args.cities else ['北京', '上海']
    unknown = [c for c in cities if not city_host(c)]
    if unknown:
        parser.error('未配置的房天下城市: ' + ','.join(unknown))
    result = crawl(cities, max_pages=args.max_pages, max_details=args.max_details,
                   interval=args.interval, out=args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['records'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
