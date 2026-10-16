"""Recheck public year/history pages. Archive evidence; replace unsupported legacy claims only with observed rows."""
import argparse
import json
import time
import re
from bs4 import BeautifulSoup
from datetime import date,datetime,timezone
from anjuke_province_crawler import CITY_SLUG_70,CITY_URL_MARKS,CITY_DATA_BUG,fetch_fangjia_page,extract_trend,collect_rows
from data_store import ROOT,atomic_json,snapshot,archive_project

OUT=ROOT/'output/anjuke_recheck'


def annual_quotes(content,city,url,fetched_at,raw):
    """A province leaderboard has annual labels, not dated city-month observations."""
    found={}
    for row in BeautifulSoup(content,'html.parser').select('.table-tr'):
        text=row.get_text(' ',strip=True)
        match=re.fullmatch(r'(20\d{2})年'+re.escape(city)+r'房价\s+([\d,]+)元/㎡(?:\s+.*)?',text)
        if not match:continue
        year,price=int(match[1]),int(match[2].replace(',',''))
        if year>date.today().year or price<=0:continue
        if year in found and found[year]['price']!=price:raise ValueError('Conflicting annual reference rows')
        found[year]=dict(city=city,year=year,price=price,period_precision='year',source_url=url,
            fetched_at=fetched_at,raw=raw,note='页面只标年份，未注明月份；年度榜单参考，非全年成交均价，不作为月度逆推锚点')
    return list(found.values())


def save_annual(records):
    path=OUT/'annual_quotes.json'
    old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    merged={(r['city'],r['year']):r for r in old}
    merged.update({(r['city'],r['year']):r for r in records})
    atomic_json(path,[merged[k] for k in sorted(merged)])
    if records:snapshot('anjuke_annual_reference',records)


def crawl(cities=None,interval_seconds=30):
    archive_project()
    now=date.today()
    log=[]
    for city,slug in CITY_SLUG_70.items():
        if cities and city not in cities:continue
        if city in CITY_DATA_BUG:continue
        mark=CITY_URL_MARKS.get(city,slug)
        observed={}
        # The year page and summary are complementary. Empty year page != no data on summary.
        for suffix in [str(now.year),'','market']:
            if suffix=='market' and observed:continue
            url=f'https://{mark}.anjuke.com/market/' if suffix=='market' else f'https://www.anjuke.com/fangjia/{mark}{suffix}/'
            key=f'{slug}_{suffix or "history"}'
            saved=OUT/(key+'.json')
            payload=json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else None
            # Reuse same-day successful raw snapshots to avoid duplicate requests in one run.
            if payload and payload.get('raw') and payload.get('status')=='observed' and datetime.fromisoformat(payload['fetched_at']).astimezone().date()==now:
                html=(ROOT/payload['raw']['path'].replace('\\','/')).read_text(encoding='utf-8')
                info=extract_trend(html)
            else:
                html=fetch_fangjia_page(url,retries=0)
                info=extract_trend(html) if html else None
                payload=dict(url=url,fetched_at=datetime.now(timezone.utc).isoformat(),
                    raw=snapshot('anjuke_recheck',html.encode('utf-8'),'raw','html') if html else None)
            payload.update(city=city,parsed=info)
            annual=annual_quotes(html,city,url,payload['fetched_at'],payload['raw']) if html else []
            save_annual(annual)
            payload['annual_reference_count']=len(annual)
            if not info or info.get('city')!=city:
                payload['status']='unavailable' if not info else 'city_mismatch'
            else:
                rows=collect_rows(info,now.year,now.month,set())
                payload['status']='observed' if rows else 'empty'
                for (year,month),price in rows.items():
                    observed.setdefault((year,month),dict(city=city,city_pinyin=slug,year=year,month=month,
                        avg_price=price,data_source='anjuke',source_url=url,fetched_at=payload['fetched_at'],raw=payload['raw']))
            atomic_json(saved,payload)
            time.sleep(interval_seconds)
            if not html:
                break  # Transport failures and blocked responses stop this city, preserving old rows.
        if observed:
            path=ROOT/f'output/anjuke/{slug}.json'
            existing=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
            old={(int(r['year']),int(r['month'])):r for r in existing}
            conflicts=[dict(previous=old[k],observed=r) for k,r in observed.items() if k in old and old[k]['avg_price']!=r['avg_price']]
            if conflicts:snapshot('anjuke_recheck_conflicts',conflicts)
            old.update(observed)
            atomic_json(path,[old[k] for k in sorted(old)])
            snapshot('anjuke_verified',list(observed.values()))
        log.append(dict(city=city,observed_months=len(observed),latest='%04d-%02d'%max(observed) if observed else None))
        atomic_json(OUT/'run.json',log)
        print(json.dumps(log[-1],ensure_ascii=False),flush=True)
    archive_project()


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cities',help='逗号分隔；默认70城')
    a=p.parse_args();crawl(a.cities.split(',') if a.cities else None)
