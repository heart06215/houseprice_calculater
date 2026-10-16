"""Public community profiles; strict identity matching, evidence retained, no listing prices.

python data_processing/community_profile_crawler.py --city 上海 --max-pages 2
"""
import argparse
import json
import re
import unicodedata
from datetime import datetime,timezone
from urllib.parse import urljoin,urlparse
from bs4 import BeautifulSoup
from data_store import ROOT,atomic_json
from community_attributes import year_value,summarize_years,market_types,finalize_types,excluded_residence

OUT=ROOT/'output/community_profiles'
FRONT=ROOT/'data_processing/frontend/realestate/data'
SOURCES={'上海':'https://sh.esf.fang.com/housing/','北京':'https://esf.fang.com/housing/','广州':'https://gz.esf.fang.com/housing/'}


def read(path,default):return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
def now():return datetime.now(timezone.utc).isoformat()
def name_key(s):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(s or ''))).casefold()
def district_key(s):return re.sub(r'(?:新区|区|县)$','',name_key(s))


def parse_page(html,city,url,fetched_at,raw):
    """Legacy row shape over parse_cards, so unknown districts are queued, never dropped."""
    rows=[]
    for r in parse_cards(html,city,url,fetched_at,raw):
        completion=r.get('completion_year',{})
        evidence=completion.get('evidence') if completion.get('status')=='recorded' else None
        rows.append(dict(id=r['id'],city=city,name=r['name'],district=(r.get('district') or None),source='房天下',
            source_url=r['evidence_url'],evidence_url=url,fetched_at=fetched_at,raw=raw,
            year=evidence[0] if evidence else None,
            year_text=r.get('year_text',''),address=r.get('address') or '',address_truncated=r.get('address_truncated',False),
            housing_types=r.get('housing_types') or [],pure_villa=r.get('pure_villa',False),type_text=r.get('type_text',''),
            identity_verified=False))
    if not rows:raise ValueError('未识别到小区卡片；保留历史资料')
    next_link=next((a.get('href') for a in BeautifulSoup(html,'html.parser').find_all('a') if a.get_text(strip=True)=='下一页'),None)
    next_url=urljoin(url,next_link) if next_link else None
    if next_url and (urlparse(next_url).netloc!=urlparse(url).netloc or not urlparse(next_url).path.startswith('/housing/')):next_url=None
    return rows,next_url


def parse_cards(html,city,url,fetched_at,raw):
    """One card = one 小区/分期; a missing year or unknown district is preserved, never guessed."""
    doc=BeautifulSoup(html,'html.parser')
    if re.search(r'captcha|forbidden|antibot',url,re.I) or re.search(r'访问已被拦截|请输入验证码|安全验证',doc.title.get_text() if doc.title else ''):
        raise ValueError('来源要求验证，停止本批并保留历史资料')
    districts=[f['properties']['name'] for f in read(FRONT/'districts'/f'{city}.json',{}).get('features',[])]
    records=[]
    # Start from the whole card, including cards whose year or district is missing.
    for card in doc.select('dl.plotListwrap') or doc.select('dl'):
        text=card.get_text(' ',strip=True)
        anchors=card.select('.title a, .tit a, a.plotTit, h3 a, h4 a')
        if not anchors:anchors=[a for a in card.select('a[title]') if a.get_text(strip=True)]
        titles={(a.get_text(' ',strip=True),urljoin(url,a.get('href',''))) for a in anchors if a.get_text(strip=True)}
        if len(titles)!=1:continue
        name,detail=next(iter(titles));host=urlparse(detail).hostname or ''
        if not host.endswith('.fang.com') or urlparse(detail).scheme not in ('http','https'):continue
        if excluded_residence({},name):continue
        type_text=' '.join(n.get_text(' ',strip=True) for n in card.select('.plotFangType, .type, .plotType, .tag, .tags'))
        if re.search(r'商铺|写字楼|商业|酒店',type_text) and not re.search(r'住宅|别墅|公寓',type_text):continue
        dates=[]
        for leaf in card.find_all(string=re.compile('年建成')):
            match=re.fullmatch(r'\s*(.*?)\s*年建成\s*',str(leaf))
            if match:dates.append(match[1].strip())
        dates=list(dict.fromkeys(dates))
        evidence=[]
        for date in dates:
            parsed=year_value(date)
            if parsed:evidence.append(parsed|dict(scope='小区',source='房天下',source_url=url,field='小区目录建成年份',value=date+'年建成',
                fetched_at=fetched_at,note='小区目录标注的建成年份，非竣工验收证明'))
        # Only exactly one known name followed by a dash resolves the district; otherwise it stays unresolved.
        known=[d for d in districts if re.search(re.escape(district_key(d))+r'\s*[-－]',text)]
        candidates=list(dict.fromkeys(re.findall(r'([\u4e00-\u9fff]{1,6}(?:新区|区|县))',text)))
        district=known[0] if len(known)==1 else (candidates[0] if candidates else '')
        address_p=next((p for p in card.select('dd > p') if p.select_one('a[href^="/house-a"]')),None)
        address_text=' '.join(address_p.find_all(string=True,recursive=False)).strip() if address_p else ''
        address_text=address_text.lstrip('-－ ').strip()
        kinds,pure=market_types(type_text)
        records.append(dict(id=detail+'|'+name,city=city,name=name,source='房天下',source_url=url,evidence_url=detail,fetched_at=fetched_at,raw=raw,
            district=district,district_normalized=district_key(district),district_status='resolved' if len(known)==1 else 'unresolved',
            district_candidates=candidates,address=address_text,address_truncated=bool(re.search(r'\.\.|…',address_text)),
            year_text=' / '.join(dates),completion_year=summarize_years(evidence),forms=kinds,housing_types=kinds,pure_villa=pure,
            villa_subtypes=[t for t in dict.fromkeys(type_text.split()) if '别墅' in t],type_text=type_text,
            listing_count=len(card.select('a[href^="/house-a"]'))))
    return records


def queue_unresolved(records,path=None):
    """Upsert unresolved cards into 待核 so a missing district is reviewed, never dropped."""
    pending=[r for r in records if r.get('district_status')=='unresolved']
    if not pending:return 0
    path=path or OUT/'pending_district.json'
    payload=read(path,{'schema_version':1,'entries':[]})
    entries={e['id']:e for e in payload.get('entries',[])}
    for record in pending:entries[record['id']]=record|dict(queued_at=now())
    payload.update(schema_version=1,entries=list(entries.values()),updated_at=now())
    atomic_json(path,payload)
    return len(pending)


def enrich(rows):
    profiles=read(OUT/'records.json',{'records':[]}).get('records',[])
    index={}
    for row in rows:index.setdefault((row['city'],name_key(row['name'])),[]).append(row)
    matched=set();unmatched=0;ambiguous=0
    for p in profiles:
        # Names and districts alone do not establish address/phase identity.
        if not p.get('identity_verified'):continue
        candidates=[r for r in index.get((p.get('city'),name_key(p.get('name'))),[]) if r.get('district') and district_key(r['district'])==district_key(p.get('district'))]
        if len(candidates)!=1:
            ambiguous+=len(candidates)>1;unmatched+=not candidates;continue
        if not p.get('raw') or not p.get('source_url') or not p.get('fetched_at'):continue
        row=candidates[0];matched.add(row['id'])
        year=p.get('year')
        if year and year.get('scope')=='小区':
            row['completion_year']=summarize_years(row['completion_year']['evidence']+[year])
        kinds=p.get('housing_types',[])
        if kinds:
            prior=[e for e in row['type_evidence'] if e.get('confidence')!='name_hint']
            additions=[dict(kind=k,source=p['source'],source_url=p['evidence_url'],field='小区物业形态',value=p['type_text'],fetched_at=p['fetched_at'],confidence='source') for k in kinds]
            row.update(finalize_types([e['kind'] for e in prior]+kinds,prior+additions,p.get('pure_villa',False)))
        row.setdefault('profile_sources',[]).append({k:p[k] for k in ('source','source_url','evidence_url','fetched_at','raw')})
    known=sum(r.get('completion_year',{}).get('status')=='recorded' for r in rows)
    return dict(year_known=known,year_unknown=sum(r.get('completion_year',{}).get('status')=='unknown' for r in rows),
        year_conflicts=sum(r.get('completion_year',{}).get('status')=='conflict' for r in rows),
        type_known=sum(bool(r.get('housing_types')) for r in rows),external_matched=len(matched),
        external_unmatched=unmatched,external_ambiguous=ambiguous,external_records=len(profiles))


def crawl(city=None,max_pages=2,direct=False):
    """Legacy entry; direct no longer changes the source's shared cooldown or transport."""
    from fang_crawler import run
    return run([city] if city else None,max_pages)


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--city',choices=list(SOURCES));p.add_argument('--max-pages',type=int,default=2);p.add_argument('--direct',action='store_true',help='Deprecated compatibility flag; shared transport and cooldown are always used')
    args=p.parse_args()
    if not 1<=args.max_pages<=10:p.error('max-pages must be 1..10')
    from update_data import single_writer
    with single_writer():
        print(json.dumps(crawl(args.city,args.max_pages,args.direct),ensure_ascii=False))
