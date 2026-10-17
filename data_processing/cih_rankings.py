"""Parse dated historical ranking literals from the archived public CIH bundle.

Never execute downloaded JavaScript. This dataset is historical, not a live feed.
"""
import json
import re
import urllib.request
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from data_store import ROOT, snapshot, atomic_json


def parse_rankings(script, source_url):
    result = []
    for key, kind, label in [('enterPriseRankRes', 'sales', '房地产企业销售金额'),
                             ('landRankRes', 'land', '权益拿地金额')]:
        match = re.search(r'\b' + key + r':(\[)', script)
        if not match:
            continue
        start = match.start(1)
        depth, quoted, escaped, end = 0, False, False, None
        for i in range(start, len(script)):
            char = script[i]
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == '[':
                depth += 1
            elif char == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError('Incomplete ranking literal')
        literal = script[start:end]
        # Tokenize strings separately so company names cannot become code or keys.
        literal = re.sub(r'"(?:\\.|[^"\\])*"|([A-Za-z_$][\w$]*)\s*:',
                         lambda m: json.dumps(m[1]) + ':' if m[1] else m[0], literal)
        for period in json.loads(literal):
            if not re.fullmatch(r'\d{4}年\d{1,2}(?:-\d{1,2})?月', period['indexName']):
                raise ValueError('Missing ranking period')
            rows = []
            for rank, row in enumerate(period['areaAndSaleRankingDtoList'], 1):
                value = row['amount']
                if not isinstance(value, (int, float)) or value < 0:
                    raise ValueError('Invalid ranking amount')
                rows.append(dict(rank=rank, enterprise=row['enterpriseNameAmount'], amount=value))
            result.append(dict(kind=kind, label=label, period=period['indexName'], unit='亿元',
                               rows=rows, source_url=source_url,
                               status='公开脚本历史快照，非实时榜单'))
    return result


def build_rankings():
    path = ROOT/'output/cih/ranking_source.json'
    if not path.exists():
        return []
    source = json.loads(path.read_text(encoding='utf-8'))
    result = parse_rankings((ROOT/source['raw']['path'].replace('\\','/')).read_text(encoding='utf-8'), source['url'])
    target=ROOT/'output/cih/rankings.json'
    previous=json.loads(target.read_text(encoding='utf-8')) if target.exists() else []
    history={(r['kind'],r['period']):r for r in previous}
    history.update({(r['kind'],r['period']):r for r in result})
    result=list(history.values())
    atomic_json(target, result)
    snapshot('cih_rankings', result)
    return result


def fetch_public_bundle():
    """One public static-resource request; fail without replacing saved clean data."""
    dataset = json.loads((ROOT/'output/cih/clean.json').read_text(encoding='utf-8'))
    home = next(p for p in dataset['pages'] if p['url'] == 'https://www.cih-index.com/')
    soup = BeautifulSoup((ROOT/home['raw']['path'].replace('\\', '/')).read_text(encoding='utf-8'), 'html.parser')
    urls = [s.get('src', '') for s in soup.select('script[src]')]
    url = next(u for u in urls if re.fullmatch(r'https://cihwebstatic\.soufunimg\.com/cih/node-cih-index/[\d.]+/js/chunk/home/index\.[\w]+\.js', u))
    with urllib.request.urlopen(url, timeout=25) as response:
        raw = response.read()
    rankings = parse_rankings(raw.decode('utf-8'), url)
    if {r['kind'] for r in rankings} != {'sales', 'land'}:
        raise ValueError('Ranking coverage incomplete; previous data retained')
    source = dict(url=url, raw=snapshot('cih', raw, 'raw', 'js'), fetched_at=datetime.now(timezone.utc).isoformat())
    atomic_json(ROOT/'output/cih/ranking_source.json', source)
    return build_rankings()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='Fetch one public bundle linked by the archived homepage')
    args = parser.parse_args()
    rows = fetch_public_bundle() if args.fetch else build_rankings()
    print(f'{len(rows)} historical ranking periods preserved')
