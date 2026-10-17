"""Collect typed Anjuke market quotes and concrete second-hand listings.

python data_processing/market_crawler.py --cities 北京 --pages 3
python data_processing/market_crawler.py --import-html output/import/beijing.html --city 北京 --url https://beijing.anjuke.com/market/
"""
import argparse
import hashlib
import json
import re
import time
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from anjuke_province_crawler import CITY_SLUG_70, CITY_URL_MARKS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output' / 'market'
PROPS = ROOT / 'data_processing/frontend/realestate/data/properties_anjuke.json'


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def number(text):
    match = re.search(r'\d+(?:\.\d+)?', str(text).replace(',', ''))
    return float(match[0]) if match else None


def market_hosts(city):
    """Keep known long and current short city domains separate from other cities."""
    return {slug + '.anjuke.com' for slug in (CITY_SLUG_70[city], CITY_URL_MARKS.get(city, CITY_SLUG_70[city]))}


def parse_market(html, city, url, as_of=None):
    """Only explicitly typed, dated yuan/sqm observations are accepted."""
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text() if soup.title else ''
    if city not in title:
        return []
    today = as_of or datetime.now(timezone(timedelta(hours=8))).date()
    current_month = today.strftime('%Y-%m')
    records = {}
    # Server rendered history rows/cards retain their own type, date and unit.
    for node in soup.select('tr, li, .price-trend-item, .trend-item, .price-item'):
        text = node.get_text(' ', strip=True)
        if len(text) > 600:
            continue
        kinds = [k for k, label in [('new_house', '新房'), ('second_hand', '二手房')] if label in text]
        if len(kinds) != 1:
            continue
        dt = re.search(r'(20\d{2})[年/-]\s*(\d{1,2})月?', text)
        price = re.search(r'(\d[\d,]*(?:\.\d+)?)\s*元\s*[/／]?\s*(?:㎡|m²|平米|平方米)', text)
        if not dt or not price:
            continue
        year, month = int(dt[1]), int(dt[2])
        if not 1 <= month <= 12 or (year, month) > (today.year, today.month):
            continue
        value = number(price[1])
        if not value or value > 1000000:
            continue
        kind = kinds[0]
        key = (kind, f'{year}-{month:02d}')
        records[key] = {'city': city, 'kind': kind, 'month': key[1], 'price': value,
                        'source_url': url, 'basis': 'listing',
                        'provisional': (year, month) == (today.year, today.month)}
    # Explicit JSON observations embedded in application/json / JSON-LD payloads.
    def walk(value, inherited=None):
        if isinstance(value, list):
            for item in value:
                walk(item, inherited)
        elif isinstance(value, dict):
            kind = value.get('house_type') or value.get('property_type') or inherited
            kind = {'新房': 'new_house', '二手房': 'second_hand'}.get(kind, kind)
            month = str(value.get('month') or value.get('date') or value.get('title') or '')
            match = re.search(r'(20\d{2})[年/-](\d{1,2})', month)
            month = f'{match[1]}-{int(match[2]):02d}' if match else month
            unit = value.get('unit')
            price = value.get('avg_price', value.get('avgPrice'))
            if kind in ('new_house', 'second_hand') and re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', month) and unit in ('元/㎡', '元/平方米'):
                if isinstance(price, (float, int)) and not isinstance(price, bool) and 0 < price < 1000000 and month <= current_month:
                    records[(kind, month)] = dict(city=city, kind=kind, month=month, price=price,
                                                  source_url=url, basis='listing', provisional=month == current_month)
            for key, child in value.items():
                typed_key = {'newHouse':'new_house', 'newHouseTrend':'new_house', 'new_house':'new_house',
                             'secondHand':'second_hand', 'secondHandTrend':'second_hand', 'second_hand':'second_hand',
                             'ershoufang':'second_hand', 'xinfang':'new_house'}.get(key)
                walk(child, typed_key or kind)
    for script in soup.select('script[type="application/json"], script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    nuxt = re.search(r'window\.__NUXT__\s*=\s*(.*?)</script>', html, re.S)
    if nuxt:
        # Evaluate only the serialized page payload in a bounded separate process.
        # The context has no process/require handles and dynamic code generation is disabled.
        js = "const vm=require('node:vm'),fs=require('node:fs');const s=fs.readFileSync(0,'utf8');const o=vm.runInNewContext('('+s+')',Object.create(null),{timeout:1000,contextCodeGeneration:{strings:false,wasm:false}});process.stdout.write(JSON.stringify(o));"
        try:
            parsed = subprocess.run(['node','--max-old-space-size=96','-e',js], input=nuxt[1].strip().rstrip(';'),
                                    text=True,encoding='utf-8',capture_output=True,timeout=5,check=True)
            walk(json.loads(parsed.stdout))
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    # The city summary card supplies its month/unit; the page title supplies type/year.
    # District pages, navigation labels and individual listing prices are not city averages.
    location = urlparse(url)
    kinds = [kind for kind, label in [('new_house', '新房'), ('second_hand', '二手房')] if label in title]
    years = set(re.findall(r'(?<!\d)20\d{2}(?!\d)', title))
    city_root = (location.scheme == 'https'
                 and city in CITY_SLUG_70 and location.hostname in market_hosts(city)
                 and location.path.rstrip('/') in ('/market', '/fangjia') and not location.query)
    if city_root and city + '房价' in title and len(kinds) == len(years) == 1:
        year = int(next(iter(years)))
        summary = {}
        for card in soup.select('.price-box > .price-item'):
            label, amount = card.select_one('.title'), card.select_one('.average_title')
            month = re.fullmatch(r'(\d{1,2})月挂牌均价', label.get_text(strip=True)) if label else None
            price = re.fullmatch(r'(\d[\d,]*(?:\.\d+)?)\s*元\s*[/／]\s*(?:㎡|平方米)',
                                 amount.get_text(' ', strip=True)) if amount else None
            if not month or not price or not 1 <= int(month[1]) <= 12:
                continue
            period = f'{year}-{int(month[1]):02d}'
            value = number(price[1])
            if period > current_month or not value or value >= 1000000:
                continue
            key = (kinds[0], period)
            summary.setdefault(key, set()).add(value)
        for key, values in summary.items():
            if len(values) != 1 or (key in records and records[key]['price'] not in values):
                raise ValueError('城市均价摘要与同月报价冲突')
            records[key] = dict(city=city, kind=key[0], month=key[1], price=next(iter(values)),
                                source_url=url, basis='listing', provisional=key[1] == current_month)
    return list(records.values())


def parse_properties(html, city, url):
    soup = BeautifulSoup(html, 'html.parser')
    if not soup.title or city not in soup.title.get_text():
        return []
    results = {}
    for item in soup.select('.property, .list-item, .house-list li, .sellListContent li, .community-item, li.item-wrap'):
        mobile = 'item-wrap' in item.get('class', [])
        link = item.select_one('a[href*="/prop/view/"], a[href*="/community/view/"], .title a, a.property-ex, a.cell-wrap[href*="/sale/"]')
        if not link:
            continue
        href = urljoin(url, link.get('href', ''))
        if urlparse(href).scheme != 'https' or not (urlparse(href).hostname or '').endswith('.anjuke.com'):
            continue
        if mobile and not re.fullmatch(r'/[^/]+/sale/[SR]\d+/', urlparse(href).path):
            continue
        name_el = item.select_one('.property-content-info-comm-name, .community-name, .li-info h3, .property-content-title-name, .title, .content-title')
        name = (name_el or link).get_text(' ', strip=True)
        price_el = item.select_one('.property-price-average, .unitPrice, .price-det, .unit-price, .community-price, .house-avg-price')
        text = item.get_text(' ', strip=True)
        match = re.search(r'(\d[\d,]*(?:\.\d+)?)\s*元\s*[/／]?\s*(?:㎡|m²|平米|平方米)', price_el.get_text(' ', strip=True) if price_el else text)
        price = number(match[1]) if match else None
        if not name or not price or price > 1000000:
            continue
        descriptions = [x.get_text(' ', strip=True) for x in item.select('.content-desc') if x.get_text(strip=True)]
        facts = ' '.join(descriptions) if mobile else text
        area = re.search(r'(\d+(?:\.\d+)?)\s*(?:㎡|平米)', facts)
        total_el = item.select_one('.property-price-total, .totalPrice, .content-price')
        total = number(total_el.get_text()) if total_el else None
        layout = re.search(r'\d+室\s*\d+厅', facts)
        address = item.select_one('.property-content-info-comm-address, .address, .li-info address')
        stable_url = href.split('?')[0].rstrip('/')
        pid = 'anjuke_' + hashlib.sha256(stable_url.encode()).hexdigest()[:20]
        community = name
        if mobile:
            thumbnail = item.select_one('img[alt]')
            match = re.match(r'(.+?)(?=\d+室)', thumbnail.get('alt', '') if thumbnail else '')
            community = match[1].strip() if match else ''
        results[pid] = dict(id=pid, name=name, community=community, city=city,
                            district=descriptions[-2] if mobile and len(descriptions) >= 5 else '', type='二手房',
                            price_avg=price, area=float(area[1]) if area else 0,
                            layout=layout[0] if layout else '',
                            orientation=descriptions[2] if mobile and len(descriptions) >= 3 else '', floor='',
                            address=address.get_text(' ', strip=True) if address else '',
                            tags=[], listings=[{'area': float(area[1]) if area else 0, 'total': total or 0,
                                                'layout': layout[0] if layout else ''}],
                            source='anjuke', source_url=stable_url + '/' if mobile else href, price_basis='listing',
                            precision='city', lat=None, lng=None)
    return list(results.values())


def ingest(html, city, url, fetched_at=None):
    host = urlparse(url).hostname or ''
    if urlparse(url).scheme != 'https' or not (host == 'anjuke.com' or host.endswith('.anjuke.com')):
        raise ValueError('来源必须是安居客 HTTPS 页面')
    fetched = fetched_at or datetime.now(timezone.utc).isoformat()
    captured = datetime.fromisoformat(fetched.replace('Z', '+00:00'))
    if captured.tzinfo is None:
        raise ValueError('采集时间必须包含时区')
    digest = hashlib.sha256(html.encode('utf-8')).hexdigest()
    raw_path = OUT / 'raw' / f'{digest}.html'
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(html.encode('utf-8'))
    evidence = dict(fetched_at=fetched, raw_sha256=digest, raw_file=str(raw_path.relative_to(ROOT)))
    quotes = parse_market(html, city, url, captured.astimezone(timezone(timedelta(hours=8))).date())
    properties = parse_properties(html, city, url)
    for rows, path, key in [(quotes, OUT/'quotes.json', lambda r:(r['city'],r['kind'],r['month'])),
                            (properties, PROPS, lambda r:r['id'])]:
        old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        merged = {key(r):r for r in old}
        for row in rows:
            row.update(evidence)
            merged[key(row)] = row
        if rows:
            save_json(path, list(merged.values()))
    return len(quotes), len(properties)


def run(cities, pages=3, delay=5, mode='both'):
    report = []
    for city in cities:
        slug = CITY_URL_MARKS.get(city, CITY_SLUG_70[city])
        urls = ([f'https://{slug}.anjuke.com/market/'] if mode != 'properties' else []) + ([f'https://{slug}.anjuke.com/sale/p{p}/' for p in range(1,pages+1)] if mode != 'market' else [])
        for request_number, url in enumerate(urls):
            if request_number >= pages + 3:
                break
            entry = dict(city=city, url=url, fetched_at=datetime.now(timezone.utc).isoformat())
            try:
                req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0', 'Accept-Language':'zh-CN,zh;q=0.9'})
                with urllib.request.urlopen(req, timeout=25) as response:
                    html = response.read().decode('utf-8')
                    final_url = response.url
                if len(html) < 2000 or 'captcha' in final_url or '@@xxzlGatewayUrl' in html or '请输入验证码' in html:
                    entry['status'] = 'verification_required'
                    report.append(entry)
                    break
                q, p = ingest(html, city, url)
                entry.update(status='parsed' if q or p else 'schema_unrecognized', quotes=q, properties=p)
                # Discover new-house pages from actual links, never invent an endpoint.
                if '/market/' in url:
                    soup = BeautifulSoup(html,'html.parser')
                    for a in soup.select('a[href]'):
                        href = urljoin(url,a['href'])
                        if '新房' in a.get_text() and '/market/' in href and urlparse(href).hostname == urlparse(url).hostname and href not in urls:
                            urls.append(href)
                report.append(entry)
            except Exception as exc:
                entry.update(status='failed',error=str(exc)[:250])
                report.append(entry)
                break
            finally:
                save_json(OUT/'crawl_report.json', report)
                save_json(OUT/('crawl_report_' + mode + '.json'), report)
                print(json.dumps(entry,ensure_ascii=False),flush=True)
            time.sleep(delay)
        if report and report[-1]['status'] == 'verification_required':
            break
    return report


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cities',default='北京,上海,广州,深圳,杭州,成都,南京,武汉,天津,重庆')
    parser.add_argument('--pages',type=int,default=3)
    parser.add_argument('--mode',choices=['both','market','properties'],default='both')
    parser.add_argument('--import-html',type=Path)
    parser.add_argument('--city')
    parser.add_argument('--url')
    args=parser.parse_args()
    if args.import_html:
        if not args.city or not args.url:
            parser.error('--import-html requires --city and --url')
        print(ingest(args.import_html.read_text(encoding='utf-8'),args.city,args.url))
    else:
        cities=args.cities.split(',')
        if any(c not in CITY_SLUG_70 for c in cities) or not 1 <= args.pages <= 50:
            parser.error('unknown city or pages outside 1..50')
        run(cities,args.pages,mode=args.mode)
