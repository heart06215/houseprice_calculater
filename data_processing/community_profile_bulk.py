"""Resumable, paced public directory collection. Unverified profiles stay in a candidate store."""
import argparse
import json
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from data_store import atomic_json
from community_profile_crawler import SOURCES, OUT, FRONT, read, now, name_key, district_key

STATE = OUT / 'bulk_state.json'
CANDIDATES = OUT / 'candidates.json'


def local_url(url, city):
    p=urlparse(url)
    return p.scheme=='https' and p.netloc==urlparse(SOURCES[city]).netloc and p.path.startswith('/housing/') and not p.query


def page_info(html, city, url):
    doc=BeautifulSoup(html,'html.parser'); text=doc.get_text(' ',strip=True)
    total=re.search(r'找到\s*([\d,]+)\s*个小区',text)
    pages=re.search(r'共\s*(\d+)\s*页',text)
    districts={district_key(f['properties']['name']) for f in read(FRONT/'districts'/f'{city}.json',{}).get('features',[])}
    capped=bool(pages and int(pages[1])>=100)
    groups=[]
    for a in doc.select('a[href]'):
        label=a.get_text(strip=True); target=urljoin(url,a['href'])
        if not local_url(target,city) or target==url:continue
        if url==SOURCES[city] and label in ('别墅','住宅','公寓','商住公寓'):
            groups.append((0 if label=='别墅' else 1,target,label))
        elif capped and (district_key(label) in districts or label in ('5年内','5-10年','10-15年','15-20年','20年以上')):
            groups.append((2 if district_key(label) in districts else 3,target,label))
    return dict(reported_count=int(total[1].replace(',','')) if total else None,
                reported_pages=int(pages[1]) if pages else None,capped=capped,
                partitions=[dict(url=t,label=l) for _,t,l in sorted(set(groups))])


def refresh_matches(records):
    locations=read(FRONT/'communities.json',{}).get('communities',[]); index={}
    for r in locations:index.setdefault((r['city'],name_key(r['name']),district_key(r.get('district'))),[]).append(r['id'])
    pending=[]
    for p in records.values():
        ids=index.get((p['city'],name_key(p['name']),district_key(p['district'])),[])
        pending.append(dict(profile_id=p['id'],city=p['city'],name=p['name'],map_ids=ids,
                            state='needs_address_phase_review' if len(ids)==1 else 'ambiguous' if ids else 'needs_location',
                            identity_verified=False))
    atomic_json(OUT/'matching_candidates.json',dict(updated_at=now(),records=pending))
    return dict(Counter(p['state'] for p in pending))


def run(cities,max_pages,interval):
    """Compatibility entry point; all Fang requests share the persistent pipeline."""
    from fang_crawler import run as collect
    return collect(cities,max_pages,max(10,interval))


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cities',default='北京,上海')
    parser.add_argument('--max-pages',type=int,default=240)
    parser.add_argument('--interval',type=float,default=30)
    args=parser.parse_args();cities=args.cities.split(',')
    if not all(c in SOURCES for c in cities) or not 1<=args.max_pages<=500 or args.interval<10:parser.error('Invalid city/page limit/interval (minimum 10 seconds).')
    from update_data import single_writer
    with single_writer():
        print(json.dumps(run(cities,args.max_pages,args.interval),ensure_ascii=False,indent=2))
