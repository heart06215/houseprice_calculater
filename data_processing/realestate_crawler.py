"""Official national real-estate cumulative series, with per-report evidence."""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from pathlib import Path

from bs4 import BeautifulSoup
from data_store import ROOT, snapshot
from stats_gov_crawler import ArticleLinks, BASE

TITLE = re.compile(r'(\d{4})年(?:(?:1[—－–-])?(\d{1,2})月份|(上半年|全年))?全国房地产(?:市场基本情况|开发投资(?:和销售情况|(?:增长|下降)[\d.]+%))')
FIRST_MONTH = '2021-02'
ARCHIVE = json.loads(Path(__file__).with_name('realestate_sources.json').read_text(encoding='utf-8'))
BASIS = '年初至报告月累计值；同比按官方可比口径'
FIELDS = (
    ('房地产开发投资', '全国房地产开发投资额', '亿元'),
    ('其中住宅投资', '其中住宅投资额', '亿元'),
    ('房屋新开工面积', '房屋新开工面积', '万㎡'),
    ('商品房销售面积', '新建商品房销售面积', '万㎡'),
    ('商品房销售额', '新建商品房销售额', '亿元'),
)


def today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def report_month(title):
    match = TITLE.search(title)
    if not match:
        raise ValueError('Unrecognized cumulative macro period')
    year = int(match[1])
    month = int(match[2]) if match[2] else (6 if match[3] == '上半年' else 12)
    period = f'{year:04d}-{month:02d}'
    # NBS publishes January-February together, never a standalone January point.
    if not 2 <= month <= 12 or period >= today().strftime('%Y-%m'):
        raise ValueError('Unfinished or nonreporting macro period')
    return period


def parse_report(content, url):
    if urlparse(url).hostname != 'www.stats.gov.cn':
        raise ValueError('Nonofficial macro source')
    soup = BeautifulSoup(content, 'html.parser')
    title = soup.title.get_text() if soup.title else ''
    if urlparse(url).path.startswith('/xxgk/'):
        title = next((h.get_text() for h in soup.find_all('h2') if TITLE.search(h.get_text())), title)
    month = report_month(title)
    meta = soup.find('meta', attrs={'name': 'PubDate'})
    # Older official templates place the original date in a heading and have no PubDate meta.
    date_text = meta.get('content', '') if meta else next((h.get_text(strip=True)
        for h in soup.find_all('h2') if re.match(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}\s', h.get_text(strip=True))), '')
    published = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', date_text)
    if not published and urlparse(url).path.startswith('/xxgk/'):
        published = re.search(r'成文日期\s*(\d{4})年(\d{1,2})月(\d{1,2})日', soup.get_text(' ', strip=True))
    if not published:
        raise ValueError('Official publication date missing')
    published_at = datetime(*map(int, published.groups())).date()
    if published_at > today() or published_at.strftime('%Y-%m') <= month:
        raise ValueError('Publication date/report period mismatch')
    article_title = soup.find('meta', attrs={'name': 'ArticleTitle'})
    if article_title and report_month(article_title.get('content', '')) != month:
        raise ValueError('Article metadata/report period mismatch')
    table = next((t for t in soup.find_all('table') if '房地产开发投资' in t.get_text()
                  and '房屋新开工面积' in t.get_text() and '商品房销售面积' in t.get_text()), None)
    if table is None:
        raise ValueError('Official national macro table missing')
    found = {}
    residential_next = False
    for tr in table.find_all('tr'):
        cells = [re.sub(r'\s+', '', c.get_text()) for c in tr.find_all(['td', 'th'])]
        if len(cells) < 3:
            continue
        name = cells[0]
        normalized = name.removeprefix('新建')
        field = next((f for f in FIELDS if normalized.startswith(f[0])), None)
        if residential_next and name in ('其中：住宅', '其中:住宅', '住宅'):
            field = FIELDS[1]
        residential_next = bool(field and field[0] == '房地产开发投资')
        if not field:
            continue
        _, label, unit = field
        if label in found:
            raise ValueError('Duplicate national macro metric')
        if field != FIELDS[1] and ('亿元' if unit == '亿元' else '万平方米') not in name:
            raise ValueError('Macro metric unit changed')
        try:
            value = float(cells[1].replace(',', ''))
            yoy = float(cells[2].replace('%', '').replace(',', ''))
        except ValueError as exc:
            raise ValueError('Macro value/unit column changed') from exc
        if not math.isfinite(value) or not 0 < value < 100000000 or not math.isfinite(yoy) or not -100 <= yoy <= 10000:
            raise ValueError('Invalid macro values')
        found[label] = dict(label=label, value=value, unit=unit, yoy=yoy)
    if len(found) != len(FIELDS):
        raise ValueError('Macro metric grid incomplete')
    year, end = map(int, month.split('-'))
    return dict(period=f'{year}年1-{end}月', month=month, source='国家统计局', source_url=url,
                published_at=published_at.isoformat(), basis=BASIS,
                items=[found[label] for _, label, _ in FIELDS])


def expected_months(last):
    return [f'{year}-{month:02d}' for year in range(int(FIRST_MONTH[:4]), int(last[:4]) + 1)
            for month in range(2, 13) if FIRST_MONTH <= f'{year}-{month:02d}' <= last]


def collect(saved, fetch, max_pages=100):
    """Backfill missing reports once; thereafter check the live publication directory."""
    history = {r['month']: r for r in saved.get('history', [])}
    # Reparse the retained latest HTML when upgrading a snapshot-only release.
    if not history and saved.get('raw'):
        from release_inputs import safe_path
        raw = saved['raw']
        content = (ROOT / safe_path(raw['path'])).read_bytes()
        if hashlib.sha256(content).hexdigest() != raw['sha256']:
            raise ValueError('Macro evidence hash mismatch')
        record = parse_report(content.decode('utf-8'), saved['source_url'])
        history[record['month']] = dict(record, raw=raw, fetched_at=saved['fetched_at'])
    last = max(history, default='')
    missing_live = set(expected_months(last)) - history.keys() - ARCHIVE.keys() if last else set()
    stop = min(missing_live) if missing_live else (last or '2023-03')
    links, errors = {}, []
    for page in range(max_pages):
        url = BASE + ('index.html' if page == 0 else f'index_{page}.html')
        try:
            parser = ArticleLinks()
            parser.feed(fetch(url, timeout=20, retries=1))
            periods = []
            for href, title in parser.links:
                if not TITLE.search(title):
                    continue
                period = report_month(title)
                absolute = urljoin(url, href)
                if urlparse(absolute).hostname != 'www.stats.gov.cn':
                    continue
                periods.append(period)
                if period >= FIRST_MONTH:
                    links.setdefault(period, absolute)
            if periods and min(periods) <= stop:
                break
        except Exception as exc:
            errors.append(f'directory {page}: {exc}')
            break
    if not links:
        raise ValueError('Official macro directory returned no report; ' + '; '.join(errors))
    latest = max(links)
    if latest < saved.get('month', ''):
        raise ValueError('Source period moved backwards')
    # The live directory starts in 2023; older reports use the retained official archive URLs.
    links.update({period: url for period, url in ARCHIVE.items() if period not in history and period <= latest})
    for period, url in sorted(links.items()):
        if period in history and period != latest:
            continue
        try:
            content = fetch(url, timeout=20, retries=1)
            record = parse_report(content, url)
            if record['month'] != period:
                raise ValueError('Directory/article period mismatch')
            history[period] = dict(record, fetched_at=datetime.now(timezone.utc).isoformat(),
                                   raw=snapshot('official_macro', content.encode('utf-8'), 'raw', 'html'))
        except Exception as exc:
            errors.append(f'{period}: {exc}')
    if not history:
        raise ValueError('No validated real-estate report; ' + '; '.join(errors))
    rows = sorted(history.values(), key=lambda r: r['month'])
    missing = sorted(set(expected_months(max(latest, rows[-1]['month']))) - history.keys())
    if missing:
        errors.append('Missing reports: ' + ', '.join(missing))
    return dict(rows[-1], history=rows), errors


def validate_evidence(record, root=ROOT, previous=None):
    from release_inputs import safe_path
    rows = record.get('history', [])
    if not rows or [r['month'] for r in rows] != sorted({r['month'] for r in rows}):
        raise ValueError('Missing, duplicate or unsorted real-estate history')
    for row in rows:
        raw = row['raw']
        content = (root / safe_path(raw['path'])).read_bytes()
        if hashlib.sha256(content).hexdigest() != raw['sha256']:
            raise ValueError('Macro evidence hash mismatch')
        parsed = parse_report(content.decode('utf-8'), row['source_url'])
        if any(row.get(key) != value for key, value in parsed.items()):
            raise ValueError('Macro source/period/unit/value mismatch')
    if {k: v for k, v in record.items() if k != 'history'} != rows[-1]:
        raise ValueError('Macro latest/history mismatch')
    if previous:
        retained = {r['month'] for r in rows}
        old = {r['month'] for r in previous.get('history', [])}
        if not old.issubset(retained) or record['month'] < previous.get('month', ''):
            raise ValueError('Macro historical periods lost')
