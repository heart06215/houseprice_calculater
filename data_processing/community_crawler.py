"""Named residential places from OpenStreetMap; no listing prices or invented locations.

Public Overpass, sequential bounded city jobs; raw + clean snapshots retained.
python data_processing/community_crawler.py --cities 上海,北京 --max-cities 2
"""
import argparse
import json
import math
import re
import time
from datetime import datetime, timezone, timedelta
import requests
from data_store import ROOT, atomic_json, snapshot
from community_attributes import PROFILE_VERSION,SOURCE_TAGS,osm_attributes,finalize_types,summarize_years,excluded_residence

DATA=ROOT/'data_processing/frontend/realestate/data'
OUT=ROOT/'output/communities'
ENDPOINT='https://overpass-api.de/api/interpreter'
QUERY_VERSION=4
COMMUNITY_NAME=r'(小区|家园|花园|新村|公寓|别墅|苑|住宅区|居民区|家属院|住宅楼|住宅大楼)$'

def now(): return datetime.now(timezone.utc).isoformat()

def place_type(tags, name):
    """Residential geography only; a name is not proof of tenure or commercial title."""
    if excluded_residence(tags,name):return None
    residential=tags.get('landuse')=='residential'
    dwelling=tags.get('building') in ('apartments','residential','house','detached','semidetached_house','terrace','villa')
    supplemental=bool(re.search(COMMUNITY_NAME,name)) and (tags.get('place') in ('neighbourhood','quarter') or tags.get('building')=='yes')
    if tags.get('building') in ('commercial','retail','office','industrial'):return None
    if not (residential or dwelling or tags.get('residential')=='apartments' or supplemental):return None
    return '住宅'

def community_query(area='.a'):
    base=''.join(f'nwr{tag}[~"^name(:zh)?$"~"."](area{area});' for tag in ('["landuse"="residential"]','["building"~"^(apartments|residential|house|detached|semidetached_house|terrace|villa)$"]','["residential"="apartments"]'))
    extra=''.join(f'nwr{tag}[~"^name(:zh)?$"~"{COMMUNITY_NAME}"](area{area});' for tag in ('["place"~"^(neighbourhood|quarter)$"]','["building"="yes"]'))
    return '('+base+extra+');out center tags;'

def parse(payload, city, evidence):
    if payload.get('remark') or not isinstance(payload.get('elements'),list):
        raise ValueError('Overpass incomplete response; retained previous city')
    rows=[]
    for e in payload['elements']:
        tags=e.get('tags',{})
        name=(tags.get('name:zh') or tags.get('name') or '').strip()
        kind=place_type(tags,name)
        if not name or not kind or e.get('type') not in ('node','way','relation'):continue
        # A bare block number is not a searchable community/apartment name.
        if re.fullmatch(r'(?:[a-zA-Z]?\d+[a-zA-Z]?|[a-zA-Z]|[零〇一二三四五六七八九十百甲乙丙丁]+)(?:号|号?楼|号?栋|幢|座|单元|室|#)?',re.sub(r'\s+','',name)):continue
        if re.fullmatch(r'(?:[A-Za-z]?\d+[A-Za-z]?(?:号楼|号|幢|栋|座|单元|室|舍)?[-—、,至/]?)+',re.sub(r'\s+','',name)):continue
        coords=e.get('center',e); lat,lng=coords.get('lat'),coords.get('lon')
        if not isinstance(lat,(int,float)) or not isinstance(lng,(int,float)) or not (15<lat<55 and 70<lng<140):continue
        url=f"https://www.openstreetmap.org/{e['type']}/{e['id']}"
        row_evidence=e.get('_evidence') or evidence
        attributes=osm_attributes(tags,name,url,row_evidence['fetched_at'])
        rows.append(dict(id=f"osm-{e['type']}-{e['id']}",name=name,community=name,city=city,
            district=tags.get('addr:district') or tags.get('addr:subdistrict') or '',
            address=tags.get('addr:full') or ''.join(tags.get(k,'') for k in ('addr:street','addr:housenumber')),
            **attributes,source_tags={k:tags[k] for k in SOURCE_TAGS if k in tags},lat=lat,lng=lng,coordinate_system='WGS84',precision='exact' if e['type']=='node' else 'centroid',
            source='osm',source_url=url,fetched_at=row_evidence['fetched_at'],
            raw=row_evidence['raw'],location_note='OSM 住宅位置；范围中心不代表入口，形态不代表产权或房屋新旧质量',license='ODbL 1.0'))
    if not rows:raise ValueError('No named residential places returned; retained previous city')
    return deduplicate(rows)

def deduplicate(rows):
    groups={};result=[]
    for r in sorted(rows,key=lambda r:r['id']):
        key=(r['city'],re.sub(r'\s+','',r['name']))
        nearby=next((p for p in groups.get(key,[]) if math.hypot((p['lng']-r['lng'])*math.cos(math.radians(r['lat'])),p['lat']-r['lat'])*111<.15),None)
        if nearby:
            nearby.setdefault('related_osm_ids',[]).append(r['id'])
            nearby.update(finalize_types(nearby.get('housing_types',[])+r.get('housing_types',[]),nearby.get('type_evidence',[])+r.get('type_evidence',[])))
            nearby['completion_year']=summarize_years(nearby.get('completion_year',{}).get('evidence',[])+r.get('completion_year',{}).get('evidence',[]))
        else:groups.setdefault(key,[]).append(r);result.append(r)
    return result

def build():
    cities={};rows=[]
    for path in sorted(OUT.glob('city-*.json')):
        p=json.loads(path.read_text(encoding='utf-8'))
        if p.get('profile_version')!=PROFILE_VERSION:p=reparse_city(path,p)
        assign_districts(p['communities'],p['city']);rows.extend(p['communities'])
        cities[p['city']]={k:p[k] for k in ('count','fetched_at','source_url')}
    from community_profile_crawler import enrich
    profile_stats=enrich(rows)
    from fang_publish import enrich as enrich_fang
    fang_stats=enrich_fang(rows)
    profile_stats.update(year_known=sum(r.get('completion_year',{}).get('status')=='recorded' for r in rows),
                         year_unknown=sum(r.get('completion_year',{}).get('status')=='unknown' for r in rows),
                         year_conflicts=sum(r.get('completion_year',{}).get('status')=='conflict' for r in rows),
                         type_known=sum(bool(r.get('housing_types')) for r in rows))
    result=dict(schema_version=2,profile_version=PROFILE_VERSION,profile_stats=profile_stats,fang_stats=fang_stats,updated_at=max((c['fetched_at'] for c in cities.values()),default=None),
        source='OpenStreetMap contributors',license='ODbL 1.0',attribution_url='https://www.openstreetmap.org/copyright',
        coverage_note='已收录住宅地点；形态区分来源标签与名称线索，建成年份保留范围、适用对象和来源；未标注不代表新建，不代表完整覆盖或当前挂牌。',
        cities=cities,communities=rows)
    if rows:
        snapshot('communities_dataset',result);atomic_json(DATA/'communities.json',result)
    return result

def in_ring(x,y,ring):
    inside=False
    for i,a in enumerate(ring):
        b=ring[i-1]
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:inside=not inside
    return inside

def assign_districts(rows,city):
    path=DATA/'districts'/f'{city}.json'
    if not path.exists():return
    areas=[];names=set()
    for f in json.loads(path.read_text(encoding='utf-8')).get('features',[]):
        g=f.get('geometry') or {};polygons=[g['coordinates']] if g.get('type')=='Polygon' else g.get('coordinates',[]) if g.get('type')=='MultiPolygon' else []
        for poly in polygons:
            ring=poly[0]
            name=f['properties'].get('name','');names.add(name)
            areas.append((name,(min(p[0] for p in ring),min(p[1] for p in ring),max(p[0] for p in ring),max(p[1] for p in ring)),poly))
    for r in rows:
        if r['district'] in names:continue
        if r['district']:r['source_district']=r['district']
        r['district']=''
        x,y=r['lng'],r['lat']
        for name,(xmin,ymin,xmax,ymax),poly in areas:
            if xmin<=x<=xmax and ymin<=y<=ymax and in_ring(x,y,poly[0]) and not any(in_ring(x,y,hole) for hole in poly[1:]):
                r['district']=name;r['district_basis']='小区位置与本地行政区界空间匹配，边界附近以地址为准';break

def save_city(city,payload,evidence):
    rows=parse(payload,city,evidence);path=OUT/f'city-{city}.json'
    previous=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    baseline=previous['count'] if previous else 0
    if previous and previous.get('profile_version')!=PROFILE_VERSION and previous.get('raw'):
        baseline=len(parse(json.loads((ROOT/previous['raw']['path']).read_text(encoding='utf-8')),city,previous))
    if previous and len(rows)<baseline*.8:raise ValueError('Coverage fell by >20% under the same filter; retained previous city')
    if previous:snapshot('communities_city',previous)
    item=dict(city=city,communities=rows,count=len(rows),profile_version=PROFILE_VERSION,**evidence)
    snapshot('communities_city',item);atomic_json(path,item)
    return item

def reparse_city(path,prior):
    raw=ROOT/prior['raw']['path']
    if not raw.exists():raise ValueError('Missing original community evidence: '+prior['city'])
    return save_city(prior['city'],json.loads(raw.read_text(encoding='utf-8')),{k:prior[k] for k in ('fetched_at','raw','source_url')})

def reparse_saved():
    """Reclassify archived evidence without claiming a new source observation."""
    for path in sorted(OUT.glob('city-*.json')):
        prior=json.loads(path.read_text(encoding='utf-8'))
        reparse_city(path,prior)
    return build()

def supplement_query(city):
    name=json.dumps(city+'市',ensure_ascii=False)
    # IDs verified in archived source responses; avoids a global administrative-area scan.
    area_id={'北京':3600912940,'上海':3600913067}.get(city)
    area=f'area({area_id})->.a;' if area_id else f'area["boundary"="administrative"]["admin_level"~"^(4|5|6)$"]["name"={name}]->.a;'
    tags=('["building"~"^(residential|house|detached|semidetached_house|terrace|villa)$"][~"^name(:zh)?$"~"."]',
          f'["place"~"^(neighbourhood|quarter)$"][~"^name(:zh)?$"~"{COMMUNITY_NAME}"]',
          f'["building"="yes"][~"^name(:zh)?$"~"{COMMUNITY_NAME}"]')
    return '[out:json][timeout:30];'+area+'.a out tags;('+''.join('nwr'+tag+'(area.a);' for tag in tags)+');out center tags;'

def merge_supplement(city,payload,evidence):
    if payload.get('remark') or not isinstance(payload.get('elements'),list):raise ValueError('Incomplete supplement; previous city retained')
    path=OUT/f'city-{city}.json';prior=json.loads(path.read_text(encoding='utf-8'))
    old=json.loads((ROOT/prior['raw']['path']).read_text(encoding='utf-8'))
    elements={}
    for e in old['elements']:
        elements[(e.get('type'),e.get('id'))]=dict(e,_evidence=e.get('_evidence') or {k:prior[k] for k in ('fetched_at','raw','source_url')})
    for e in payload['elements']:
        elements[(e.get('type'),e.get('id'))]=dict(e,_evidence=evidence)
    combined=dict(elements=list(elements.values()),note='Derived union; every object retains its actual raw response and fetch date')
    union=snapshot('communities_osm_union',combined)
    return save_city(city,combined,dict(fetched_at=evidence['fetched_at'],raw=union,source_url=ENDPOINT))

def supplement(cities,direct=False):
    status_path=OUT/'supplement_status.json';status=json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
    session=requests.Session();session.trust_env=not direct;log=[]
    for city in cities:
        if not (OUT/f'city-{city}.json').exists():raise ValueError('Supplement requires an existing city')
        prior=status.get(city,{})
        if prior.get('checked_at') and datetime.fromisoformat(prior['checked_at'])>datetime.now(timezone.utc)-timedelta(days=30 if prior.get('status')=='ok' else 1):
            if prior.get('query_version')==2 or '504' not in prior.get('error',''):continue
        entry=dict(city=city,checked_at=now(),query_version=2)
        try:
            response=session.post(ENDPOINT,data={'data':supplement_query(city)},headers={'User-Agent':'70CityHousingResearch/1.0'},timeout=65)
            response.raise_for_status();raw=snapshot('communities_osm_supplement',response.content,'raw','json');payload=response.json()
            if not any(e.get('type')=='area' and e.get('tags',{}).get('name')==city+'市' for e in payload.get('elements',[])):raise ValueError('City administrative area unresolved')
            before=json.loads((OUT/f'city-{city}.json').read_text(encoding='utf-8'))['count']
            item=merge_supplement(city,payload,dict(fetched_at=entry['checked_at'],raw=raw,source_url=ENDPOINT))
            entry.update(status='ok',last_success=entry['checked_at'],before=before,count=item['count'],raw=raw)
        except Exception as exc:entry.update(status='failed',error=str(exc))
        status[city]=entry;atomic_json(status_path,status);log.append(entry);print(json.dumps(entry,ensure_ascii=False),flush=True)
        if entry['status']=='failed' and '504' not in entry.get('error',''):break
        time.sleep(3)
    build();return log

def crawl(cities=None,max_cities=4,force=False,direct=False):
    cfg=json.loads((DATA/'cities.json').read_text(encoding='utf-8'))
    logpath=OUT/'status.json';status=json.loads(logpath.read_text(encoding='utf-8')) if logpath.exists() else {}
    candidates=cities or [c for c in cfg if cfg[c].get('sample70')]
    if any(c not in cfg for c in candidates):raise ValueError('Unknown configured city')
    candidates=sorted(candidates,key=lambda c:status.get(c,{}).get('checked_at',''))
    log=[]
    session=requests.Session();session.trust_env=not direct
    for city in candidates:
        prior=status.get(city,{})
        stamp=prior.get('last_success') if prior.get('status')=='ok' else prior.get('checked_at')
        if not force and stamp and datetime.fromisoformat(stamp)>datetime.now(timezone.utc)-timedelta(days=30 if prior.get('status')=='ok' and prior.get('query_version')==QUERY_VERSION else 1):continue
        if len(log)>=max_cities:break
        attempt=now(); entry=dict(prior,city=city,checked_at=attempt)
        try:
            name=json.dumps(city+'市',ensure_ascii=False)
            query=f'[out:json][timeout:90];area["boundary"="administrative"]["admin_level"~"^(4|5|6)$"]["name"={name}]->.a;.a out tags;'+community_query()
            r=session.post(ENDPOINT,data={'data':query},headers={'User-Agent':'70CityHousingResearch/1.0 (public residential geography)'},timeout=110)
            r.raise_for_status();raw=snapshot('communities_osm',r.content,'raw','json');payload=r.json()
            if not any(e.get('type')=='area' and e.get('tags',{}).get('name')==city+'市' for e in payload.get('elements',[])):raise ValueError('City administrative area unresolved')
            item=save_city(city,payload,dict(fetched_at=attempt,raw=raw,source_url=ENDPOINT))
            entry.update(status='ok',last_success=attempt,count=item['count'],query_version=QUERY_VERSION,error=None)
        except Exception as exc:entry.update(status='failed',error=str(exc))
        status[city]=entry;atomic_json(logpath,status);log.append(entry)
        print(json.dumps(entry,ensure_ascii=False),flush=True)
        build();time.sleep(2)
        # Rate-limit / transport failures end the batch rather than switching endpoints.
        if entry['status']=='failed':break
    build();return log

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cities');p.add_argument('--max-cities',type=int,default=4);p.add_argument('--force',action='store_true');p.add_argument('--build',action='store_true');p.add_argument('--reparse',action='store_true')
    p.add_argument('--direct',action='store_true',help='Direct networking when the environment proxy is unavailable')
    p.add_argument('--supplement',action='store_true',help='Incremental missing residential categories for existing cities')
    a=p.parse_args()
    if not 1<=a.max_cities<=71:p.error('max-cities must be 1..71')
    if a.supplement:supplement(a.cities.split(',') if a.cities else ['北京','上海'],a.direct)
    else:reparse_saved() if a.reparse else build() if a.build else crawl(a.cities.split(',') if a.cities else None,a.max_cities,a.force,a.direct)
