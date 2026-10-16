"""Monthly LPR under the August 2019 mechanism, with official raw evidence."""
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from data_store import snapshot

LPR_URL = 'https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/currency/bk-lpr.json'
HISTORY_URL = 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis'
HISTORY_PAGE = 'https://www.chinamoney.com.cn/chinese/bklpr/'
PBC_DIRECTORY = 'https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/3876551/index.html'
FIRST = '2019-08-20'
FACTS = ('as_of', 'one_year', 'five_year', 'unit')
ANNOUNCEMENT = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*?贷款市场报价利率')


def point(day, one, five, url, source='全国银行间同业拆借中心'):
    date.fromisoformat(day)
    if not FIRST <= day <= date.today().isoformat():
        raise ValueError('LPR date outside the current mechanism or in the future')
    values = [float(one), float(five)]
    if any(not math.isfinite(v) or not 0 < v < 20 for v in values):
        raise ValueError('Invalid LPR percentage')
    return dict(as_of=day, one_year=values[0], five_year=values[1], unit='%', source=source, source_url=url)


def parse_lpr(content):
    payload = json.loads(content)
    rows = payload['records']
    if len(rows) != 2 or {r['termCode'] for r in rows} != {'1Y', '5Y'}:
        raise ValueError('Incomplete or duplicate LPR terms')
    if payload.get('head', {}).get('rep_code', '200') != '200':
        raise ValueError('LPR request failed')
    terms = {r['termCode']: r['shibor'] for r in rows}
    return point(payload['data']['showDateCN'][:10], terms['1Y'], terms['5Y'], LPR_URL)


def parse_history(content, url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'www.chinamoney.com.cn' or parsed.path != urlparse(HISTORY_URL).path:
        raise ValueError('Nonofficial LPR history URL')
    query = parse_qs(parsed.query)
    start, end = query['strStartDate'][0], query['strEndDate'][0]
    date.fromisoformat(start); date.fromisoformat(end)
    payload = json.loads(content)
    meta = payload['data']
    if (payload['head']['rep_code'] != '200' or meta.get('message') or
            meta['startDateCN'] != start or meta['endDateCN'] != end or
            set(meta['baseCurveCfgList']) != {'1Y', '5Y'} or not payload['records']):
        raise ValueError('LPR history response is empty or has a different query/term basis')
    rows = [point(r['showDateCN'], r['1Y'], r['5Y'], url) for r in payload['records']]
    if any(not start <= r['as_of'] <= end for r in rows):
        raise ValueError('LPR history escaped the requested date range')
    if len({r['as_of'][:7] for r in rows}) != len(rows):
        raise ValueError('Duplicate LPR publication month')
    return sorted(rows, key=lambda r: r['as_of'])


def parse_pbc(content, url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'www.pbc.gov.cn' or not parsed.path.startswith(urlparse(PBC_DIRECTORY).path.rsplit('/', 1)[0] + '/'):
        raise ValueError('Nonofficial PBC LPR announcement')
    text = re.sub(r'\s+', '', BeautifulSoup(content, 'html.parser').get_text()).replace('％', '%')
    match = re.search(r'公布[，,]?(\d{4})年(\d{1,2})月(\d{1,2})日贷款市场报价利率[（(]LPR[）)]为[：:]1年期LPR为([\d.]+)%[，,]5年期以上LPR为([\d.]+)%', text)
    if not match:
        raise ValueError('PBC LPR announcement format changed')
    y, m, d, one, five = match.groups()
    return point(f'{int(y):04d}-{int(m):02d}-{int(d):02d}', one, five, url, '中国人民银行')


def evidence_rows(content, url):
    if url == LPR_URL:
        return [parse_lpr(content)]
    if urlparse(url).netloc == 'www.chinamoney.com.cn':
        return parse_history(content, url)
    return [parse_pbc(content, url)]


def validate_history(record, previous=None):
    rows = record.get('history', [])
    end = date.fromisoformat(record['as_of']).replace(day=1)
    expected, month = [], date(2019, 8, 1)
    while month <= end:
        expected.append(month.strftime('%Y-%m'))
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    if [r['as_of'][:7] for r in rows] != expected:
        raise ValueError('LPR monthly history has a gap, duplicate, or different order')
    for row in rows:
        actual = point(row['as_of'], row['one_year'], row['five_year'], row['source_url'], row['source'])
        if any(row.get(k) != v for k, v in actual.items()):
            raise ValueError('LPR history unit/value/source mismatch')
    if not rows or any(rows[-1].get(k) != record.get(k) for k in FACTS):
        raise ValueError('Latest LPR does not match the history endpoint')
    current = {r['as_of']: r for r in rows}
    for old in (previous or {}).get('history', []):
        if any(current.get(old['as_of'], {}).get(k) != old.get(k) for k in FACTS):
            raise ValueError('A previously published LPR observation changed or disappeared')
    if previous and record['as_of'] < previous.get('as_of', ''):
        raise ValueError('LPR publication moved backwards')


def validate_evidence(record, root, previous=None):
    import hashlib
    from release_inputs import safe_path
    validate_history(record, previous)
    if record.get('mechanism_start') != FIRST or record.get('history_source_url') != HISTORY_PAGE:
        raise ValueError('LPR mechanism/source metadata mismatch')
    verification = record.get('verification', {})
    if verification.get('source') != '中国人民银行' or any(verification.get(k) != record.get(k) for k in FACTS):
        raise ValueError('LPR cross-check is missing or has different rates/date')
    parsed = {}
    for row in [record, verification] + record['history']:
        raw = row['raw']
        identity = (raw['path'], raw['sha256'], row['source_url'])
        if identity not in parsed:
            content = (root / safe_path(raw['path'])).read_bytes()
            if hashlib.sha256(content).hexdigest() != raw['sha256']:
                raise ValueError('LPR raw evidence hash mismatch')
            parsed[identity] = {r['as_of']: r for r in evidence_rows(content.decode('utf-8'), row['source_url'])}
        actual = parsed[identity].get(row['as_of'], {})
        if not actual or any(row.get(k) != v for k, v in actual.items()):
            raise ValueError('LPR evidence month/term/rate mismatch')


def collect(previous, getter, gap=1):
    """Fetch at most annual history windows; PBC can supply incremental fallback."""
    def fetch(url):
        time.sleep(gap)
        return getter(url, timeout=20, retries=0)

    def attach(row, content, suffix):
        return dict(row, fetched_at=datetime.now(timezone.utc).isoformat(),
                    raw=snapshot('official_lpr', content.encode('utf-8'), 'raw', suffix))

    def directory(content, url):
        soup = BeautifulSoup(content, 'html.parser')
        links = {}
        for a in soup.select('a[istitle]'):
            match = ANNOUNCEMENT.search(a.get('title', '') or a.get_text())
            if match:
                day = '{:04d}-{:02d}-{:02d}'.format(*map(int, match.groups()))
                if FIRST <= day <= date.today().isoformat():
                    target = urljoin(url, a.get('href', ''))
                    if urlparse(target).netloc == 'www.pbc.gov.cn':
                        links[day] = target
        nxt = next((urljoin(url, a.get('tagname', '')) for a in soup.find_all('a')
                    if a.get_text(strip=True) == '下一页' and a.get('tagname', '').endswith('.html')), None)
        if nxt and (urlparse(nxt).netloc != 'www.pbc.gov.cn' or not urlparse(nxt).path.startswith(urlparse(PBC_DIRECTORY).path.rsplit('/', 1)[0] + '/')):
            raise ValueError('PBC directory pagination escaped its source')
        return links, nxt

    # A network failure switches to the second official publication channel.
    # Invalid or conflicting observations fail validation and retain the saved release.
    try:
        content = fetch(LPR_URL)
    except (OSError, RuntimeError):
        content = None
    latest = attach(parse_lpr(content), content, 'json') if content is not None else None
    catalogue = fetch(PBC_DIRECTORY)
    links, nxt = directory(catalogue, PBC_DIRECTORY)
    if not links:
        raise ValueError('PBC LPR directory has no current announcements')
    day, url = max(links.items())
    pbc_content = fetch(url)
    verified = attach(parse_pbc(pbc_content, url), pbc_content, 'html')
    if verified['as_of'] != day:
        raise ValueError('PBC directory date does not match the announcement')
    if latest and any(latest[k] != verified[k] for k in FACTS):
        raise ValueError('The two official LPR publications disagree')
    latest = latest or verified
    rows = {r['as_of'][:7]: r for r in previous.get('history', [])}

    def merge(row):
        old = rows.get(row['as_of'][:7])
        if old and any(old[k] != row[k] for k in FACTS):
            raise ValueError('Conflicting LPR month/date/term values')
        rows[row['as_of'][:7]] = row

    use_history_api = content is not None
    if use_history_api:
        start = max(rows) + '-01' if rows else FIRST
        # Re-fetch the overlapping month when advancing; unchanged months need no history request.
        if not rows or latest['as_of'] != previous.get('as_of'):
            cursor, end = date.fromisoformat(start), date.fromisoformat(latest['as_of'])
            while cursor <= end:
                stop = min(end, date(cursor.year, 12, 31))
                query = urlencode(dict(lang='CN', strStartDate=cursor.isoformat(), strEndDate=stop.isoformat()))
                url = HISTORY_URL + '?' + query
                try:
                    raw = fetch(url)
                except (OSError, RuntimeError):
                    use_history_api = False
                    break
                for row in parse_history(raw, url):
                    merge(attach(row, raw, 'json'))
                cursor = stop + timedelta(days=1)
    if not use_history_api:
        start = max(rows) + '-01' if rows else FIRST
        seen = {PBC_DIRECTORY}
        while min(links) > start and nxt:
            if nxt in seen or len(seen) >= 10:
                raise ValueError('PBC LPR pagination limit reached')
            seen.add(nxt)
            more, nxt = directory(fetch(nxt), nxt)
            links.update(more)
        for publication, url in sorted(links.items()):
            if publication >= start:
                if publication == verified['as_of']:
                    merge(verified)
                else:
                    raw = fetch(url)
                    row = parse_pbc(raw, url)
                    if row['as_of'] != publication:
                        raise ValueError('PBC announcement date mismatch')
                    merge(attach(row, raw, 'html'))
    result = dict(latest, history=[rows[k] for k in sorted(rows)], verification=verified,
                  history_source_url=HISTORY_PAGE, mechanism_start=FIRST)
    validate_history(result, previous)
    return result
