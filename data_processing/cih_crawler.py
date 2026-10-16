"""Bounded public CIH crawler. Preserve source HTML, clean tables and provenance."""
import argparse
import json
import re
import time
import urllib.request
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from data_store import ROOT, snapshot, atomic_json

SEEDS = ['https://www.cih-index.com/', 'https://www.cih-index.com/data/land.html',
         'https://www.cih-index.com/mall/house', 'https://www.cih-index.com/data/house.html',
         'https://www.cih-index.com/data/index/newHouse.html',
         'https://www.cih-index.com/data/index/esfHouse.html',
         'https://www.cih-index.com/data/index/rentIndex.html']
OUTPUT = ROOT/'output/cih/clean.json'


def parse(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, noscript, header, footer'):
        node.decompose()
    text = soup.get_text(' ', strip=True)
    article = soup.select_one('.trade-detail-container')
    summary = soup.select_one('.trade-detail-summary')
    tables = []
    for table in soup.select('table'):
        rows = [[c.get_text(' ', strip=True) for c in tr.select('th,td')] for tr in table.select('tr')]
        rows = [r for r in rows if r]
        if rows:
            tables.append(rows)
    prices = []
    labels = [('新建住宅', 'new_house'), ('二手住宅','second_hand'), ('租赁住宅','rent'), ('地价','land')]
    for label, kind in labels:
        # Scope to the explicit dated sample-average section; changes are already percentages.
        pattern = re.compile(re.escape(label) + r'\s*\([^)]*\)\s*平均(?:价格|租金)\(([^)]+)\)\s*([\d.]+)\s*环比\s*([+-]?[\d.]+)%\s*同比\s*([+-]?[\d.]+)%\s*城市\s*环比涨跌\s*样本平均价格\s*(.*?)(?=新建住宅\s*\(|二手住宅\s*\(|租赁住宅\s*\(|地价\s*\(|房地产企业|$)', re.S)
        match = pattern.search(text)
        if not match:
            continue
        period = match[1]
        dt = re.fullmatch(r'(\d{4})年(\d{2})月', period)
        period = f'{dt[1]}-{dt[2]}' if dt else period
        for city, change, price in re.findall(r'([\u4e00-\u9fff]{2,8})\s+([+-]?\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)', match[5]):
            prices.append(dict(city=city, kind=kind, period=period, price=float(price), mom_pct=float(change),
                               unit='元/㎡·月' if kind=='rent' else '元/㎡', source_url=url,
                               basis='中指样本平均价格；非官方70城成交均价'))
    daily = []
    for city, day, count, area in re.findall(r'([\u4e00-\u9fff]{2,8})\s*\((\d{4}-\d{2}-\d{2})\)\s*成交套数：\s*([\d.]+)\s*套\s*成交面积：\s*([\d.]+)\s*万㎡', text):
        daily.append(dict(city=city,date=day,units=int(float(count)),area_wan_sqm=float(area),source_url=url))
    links = []
    for a in soup.select('a[href]'):
        href=urljoin(url,a['href'])
        title=a.get_text(' ',strip=True)
        if title and urlparse(href).hostname=='www.cih-index.com' and re.search(r'/(news/|report/detail/|data/|mall/)', urlparse(href).path):
            links.append(dict(title=title,url=href))
    return dict(url=url,title=soup.title.get_text(strip=True) if soup.title else url,
                text=text, article_text=article.get_text(' ',strip=True) if article else None,
                summary=summary.get_text(' ',strip=True) if summary else None,
                tables=tables, prices=prices, daily=daily, links=links)


def reparse():
    data=json.loads(OUTPUT.read_text(encoding='utf-8'))
    observations={(r['city'],r['kind'],r['period']):r for r in data.get('observations',[])}
    for index,old in enumerate(data['pages']):
        page=parse((ROOT/old['raw']['path'].replace('\\','/')).read_text(encoding='utf-8'),old['url'])
        page.update(raw=old['raw'],fetched_at=old['fetched_at'])
        page['clean_snapshot']=snapshot('cih',page)
        data['pages'][index]=page
        for row in page['prices']:
            observations[(row['city'],row['kind'],row['period'])]=dict(row,fetched_at=old['fetched_at'])
    data['observations']=list(observations.values())
    atomic_json(OUTPUT,data)
    snapshot('cih_dataset',data)
    return data


def crawl(max_pages=20, delay=1.5):
    old=json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {'pages':[]}
    pages={p['url']:p for p in old['pages']}
    history={(r['city'],r['kind'],r['period']):r for r in old.get('observations',[])}
    daily_history={(r['city'],r['date']):r for r in old.get('daily_observations',[])}
    for page in old['pages']:
        for row in page.get('daily',[]):daily_history[(row['city'],row['date'])]=dict(row,fetched_at=page['fetched_at'])
    queue=list(SEEDS)
    visited=set()
    log=[]
    last_success=old.get('last_success') or old.get('updated_at')
    while queue and len(visited)<max_pages:
        url=queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        now=datetime.now(timezone.utc).isoformat()
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Language':'zh-CN,zh;q=0.9'})
            with urllib.request.urlopen(req,timeout=25) as response:
                raw=response.read()
                if urlparse(response.url).hostname!='www.cih-index.com':
                    raise ValueError('redirect outside public source')
            raw_info=snapshot('cih',raw,'raw','html')
            page=parse(raw.decode('utf-8'),url)
            if len(page['text'])<300 or '验证码' in page['title']:
                raise ValueError('blocked or incomplete response')
            if url==SEEDS[0] and any(sum(r['kind']==kind for r in page['prices'])<90 for kind in ('new_house','second_hand')):
                raise ValueError('homepage price coverage incomplete; retained previous clean data')
            page.update(fetched_at=now,raw=raw_info)
            for row in page['prices']:
                history[(row['city'],row['kind'],row['period'])]=dict(row,fetched_at=now,raw_sha256=raw_info['sha256'])
            for row in page['daily']:
                daily_history[(row['city'],row['date'])]=dict(row,fetched_at=now,raw_sha256=raw_info['sha256'])
            page['clean_snapshot']=snapshot('cih',page)
            pages[url]=page
            last_success=now
            log.append(dict(url=url,status='ok',prices=len(page['prices']),tables=len(page['tables'])))
            if url==SEEDS[0]:
                queue.extend(l['url'] for l in page['links'] if '/news/' in l['url'])
                queue.extend(l['url'] for l in page['links'] if '/data/house/' in l['url'] or '/data/land/' in l['url'])
        except Exception as exc:
            log.append(dict(url=url,status='failed',error=str(exc)))
        result=dict(source='中指研究院 / 中指云',updated_at=last_success,last_success=last_success,checked_at=now,pages=list(pages.values()),observations=list(history.values()),daily_observations=list(daily_history.values()),last_run=log)
        atomic_json(OUTPUT,result)
        print(json.dumps(log[-1],ensure_ascii=False),flush=True)
        time.sleep(delay)
    snapshot('cih_dataset',result)
    return result


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-pages',type=int,default=20)
    parser.add_argument('--reparse',action='store_true',help='从留存网页重新清洗，无需联网')
    args=parser.parse_args()
    if not 1<=args.max_pages<=100:
        parser.error('max-pages must be 1..100')
    reparse() if args.reparse else crawl(args.max_pages)
