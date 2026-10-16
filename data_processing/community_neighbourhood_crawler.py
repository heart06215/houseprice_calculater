"""Small, bounded named residential-neighbourhood node supplementation.

No building scan. Exact municipal boundary validates every candidate before merging.
"""
import argparse
import json
import time
from datetime import datetime,timezone,timedelta
import requests
import community_crawler as C
from data_store import atomic_json,snapshot

def crawl(cities=('北京','上海'),direct=False):
    ids={'北京':3600912940,'上海':3600913067}
    path=C.OUT/'neighbourhood_status.json'
    status=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    session=requests.Session();session.trust_env=not direct;log=[]
    for city in cities:
        prior=status.get(city,{})
        if prior.get('checked_at') and datetime.fromisoformat(prior['checked_at'])>datetime.now(timezone.utc)-timedelta(days=30 if prior.get('status')=='ok' else 1):continue
        entry=dict(city=city,checked_at=C.now())
        query=f'[out:json][timeout:20];area({ids[city]})->.a;node["place"~"^(neighbourhood|quarter)$"](area.a);out;'
        try:
            response=session.post(C.ENDPOINT,data={'data':query},timeout=35,headers={'User-Agent':'70CityHousingResearch/1.0'})
            response.raise_for_status();raw=snapshot('community_neighbourhood_osm',response.content,'raw','json');payload=response.json()
            entry['raw']=raw
            if payload.get('remark') or not isinstance(payload.get('elements'),list):raise ValueError('Incomplete neighbourhood response; retained existing data')
            evidence=dict(fetched_at=entry['checked_at'],raw=raw,source_url=C.ENDPOINT)
            # The provider's area filter is checked independently against our city districts.
            valid=[]
            for element in payload['elements']:
                try:rows=C.parse({'elements':[element]},city,evidence)
                except ValueError:continue
                for row in rows:row['district']=''
                C.assign_districts(rows,city)
                if rows[0].get('district'):valid.append(element)
            previous=json.loads((C.OUT/f'city-{city}.json').read_text(encoding='utf-8'))
            old_ids={i for r in previous['communities'] for i in [r['id'],*r.get('related_osm_ids',[])]}
            item=C.merge_supplement(city,dict(elements=valid),evidence)
            entry.update(status='ok',last_success=entry['checked_at'],candidates=len(payload['elements']),accepted=len(valid),
                         new_ids=sum(f"osm-{e['type']}-{e['id']}" not in old_ids for e in valid),count=item['count'])
        except Exception as exc:entry.update(status='failed',error=str(exc))
        status[city]=entry;atomic_json(path,status);log.append(entry);print(json.dumps(entry,ensure_ascii=False),flush=True)
        if entry['status']=='failed':break
        time.sleep(3)
    C.build();return log

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cities',default='北京,上海');p.add_argument('--direct',action='store_true');args=p.parse_args()
    cities=args.cities.split(',')
    if any(c not in ('北京','上海') for c in cities):p.error('Only 北京 and 上海 are configured')
    crawl(cities,args.direct)
