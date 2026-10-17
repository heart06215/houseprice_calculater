"""Archive named rivers, sea/coasts, and large lake/reservoir geometry by city.

python data_processing/hydrology_crawler.py --cities 上海 --max-cities 1
This is geographic surface water, NOT drinking-water protection or safety data.
"""
import argparse
import json
import math
import re
import time
from datetime import datetime, timezone, timedelta
import requests
from geographiclib.geodesic import Geodesic
from data_store import ROOT, atomic_json, snapshot
from community_crawler import DATA, ENDPOINT, in_ring, now

OUT=ROOT/'output/hydrology'
FILTER_VERSION=4
MIN_WATER_AREA_KM2=.1
MIN_RIVER_WIDTH_M=20
FILTER_POLICY=dict(version=FILTER_VERSION,min_water_area_km2=MIN_WATER_AREA_KM2,
    min_explicit_river_width_m=MIN_RIVER_WIDTH_M,named_river_lines_only=True,river_lines_require_width=True,
    excluded=['highway','stream','canal','drain','ditch','wadi','tidal_channel','pond','basin','wastewater','spring'])
SOURCE_TAGS=('natural','waterway','water','landuse','highway','intermittent','seasonal','width')

def coordinates(items):
    result=[]
    for p in items or []:
        x,y=p.get('lon'),p.get('lat')
        if not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not math.isfinite(x+y) or not (-180<=x<=180 and -90<=y<=90):return []
        if not result or result[-1]!=[x,y]:result.append([x,y])
    return result

def rings(parts):
    """Stitch exact shared OSM endpoints; never close an incomplete shoreline by guessing."""
    pending=[list(p) for p in parts if len(p)>1];closed=[]
    while pending:
        line=pending.pop(0)
        while line[0]!=line[-1]:
            found=False
            for i,p in enumerate(pending):
                if line[-1]==p[0]:line+=p[1:]
                elif line[-1]==p[-1]:line+=p[-2::-1]
                elif line[0]==p[-1]:line=p[:-1]+line
                elif line[0]==p[0]:line=p[:0:-1]+line
                else:continue
                pending.pop(i);found=True;break
            if not found:return None
        if len(line)<4:return None
        closed.append(line)
    return closed

def river_width_m(tags):
    width=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:m|米)?\s*',str(tags.get('width','')))
    return float(width[1]) if width else None

def major_kind(tags,name):
    """OSM classification takes precedence over misleading names (e.g. Beijing's 北海)."""
    waterway=tags.get('waterway',''); natural=tags.get('natural',''); water=tags.get('water','')
    if tags.get('highway') or natural=='spring':return None
    if waterway in FILTER_POLICY['excluded'] or water in FILTER_POLICY['excluded']:return None
    if natural=='coastline':return '海岸线'
    if natural in ('water','bay') and (water in ('sea','bay','estuary') or natural=='bay'):return '海域'
    if water=='lake' and natural=='water':return '湖泊'
    if water=='reservoir' or tags.get('landuse')=='reservoir':return '水库'
    if waterway in ('river','riverbank') or (water=='river' and natural=='water'):
        if tags.get('intermittent')=='yes' or tags.get('seasonal') not in (None,'no'):return None
        width=river_width_m(tags)
        if width is not None and width<MIN_RIVER_WIDTH_M:return None
        if re.search(r'(?:沟|渠|溪)$|灌渠|排水',name):return None
        return '江河'
    if natural=='water' and not water:return '大型水面'
    return None

def water_area_km2(geometry):
    """Ellipsoidal surface area, subtracting islands/holes regardless of ring orientation."""
    polygons=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates'] if geometry['type']=='MultiPolygon' else []
    def area(ring):
        polygon=Geodesic.WGS84.Polygon()
        for lon,lat in ring:polygon.AddPoint(lat,lon)
        return abs(polygon.Compute()[2])
    return sum(max(0,area(p[0])-sum(area(h) for h in p[1:])) for p in polygons)/1e6

def water_query(city):
    name=json.dumps(city+'市',ensure_ascii=False)
    return f'[out:json][timeout:50];area["boundary"="administrative"]["admin_level"~"^(4|5|6)$"]["name"={name}]->.a;.a out tags;(way["waterway"="river"]["width"][~"^name(:zh)?$"~"."](area.a);way["natural"="coastline"](area.a);wr["natural"="bay"](area.a);wr["waterway"="riverbank"](area.a);wr["natural"="water"](area.a);wr["landuse"="reservoir"](area.a););out geom;'

def parse(payload,city,evidence):
    if payload.get('remark') or not isinstance(payload.get('elements'),list):raise ValueError('Incomplete water response; previous geometry retained')
    features=[];covered=set();skipped=0;excluded=0
    # Relations first; suppress duplicate member areas only after a valid relation is built.
    for e in sorted(payload['elements'],key=lambda e:e.get('type')!='relation'):
        t=e.get('tags',{});name=t.get('name:zh') or t.get('name') or ''
        kind=major_kind(t,name)
        area=t.get('natural') in ('water','bay') or t.get('landuse')=='reservoir' or t.get('waterway')=='riverbank'
        if not kind:excluded+=e.get('type') in ('node','way','relation');continue
        if (e.get('type'),e.get('id')) in covered:continue
        geometry=None;members=[]
        if e['type']=='way':
            c=coordinates(e.get('geometry'))
            if kind!='海岸线' and area and len(c)>=4 and c[0]==c[-1]:geometry={'type':'Polygon','coordinates':[c]}
            elif (kind=='海岸线' or (kind=='江河' and not area)) and len(c)>=2:geometry={'type':'LineString','coordinates':c}
        elif area and e['type']=='relation':
            members=[m for m in e.get('members',[]) if m.get('type')=='way']
            valid=all(coordinates(m.get('geometry')) for m in members)
            outer=rings([coordinates(m.get('geometry')) for m in members if m.get('role','') in ('','outer')]) if valid else None
            inner=rings([coordinates(m.get('geometry')) for m in members if m.get('role')=='inner']) if valid else None
            if outer and inner is not None:
                polygons=[[r] for r in outer];orphan=False
                for hole in inner:
                    parent=next((p for p in polygons if in_ring(*hole[0],p[0])),None)
                    if parent is None:orphan=True;break
                    parent.append(hole)
                if not orphan:geometry={'type':'MultiPolygon','coordinates':polygons}
        if not geometry:skipped+=1;continue
        surface_area=water_area_km2(geometry) if geometry['type'] in ('Polygon','MultiPolygon') else None
        if (surface_area is not None and kind!='海域' and surface_area<MIN_WATER_AREA_KM2) or (geometry['type']=='LineString' and kind=='江河' and (not name or (river_width_m(t) or 0)<MIN_RIVER_WIDTH_M)):
            excluded+=1;continue
        covered.update(('way',m['ref']) for m in members)
        props={'city':city,'name':name or '未命名'+kind,'kind':kind,'source_tags':{k:t[k] for k in SOURCE_TAGS if k in t},'source_url':f"https://www.openstreetmap.org/{e['type']}/{e['id']}"}
        if surface_area is not None:props['area_km2']=round(surface_area,6)
        features.append({'type':'Feature','id':f"osm-{e['type']}-{e['id']}",'properties':props,'geometry':geometry})
    return dict(type='FeatureCollection',city=city,features=features,count=len(features),skipped_incomplete=skipped,excluded_by_scale=excluded,filter_version=FILTER_VERSION,filter_policy=FILTER_POLICY,coordinate_system='WGS84',license='ODbL 1.0',**evidence)

def build():
    cities={}
    for p in OUT.glob('city-*.json'):
        item=json.loads(p.read_text(encoding='utf-8'))
        if item.get('filter_version')!=FILTER_VERSION:item=reparse_city(p,item)
        city=item['city']
        cities[city]={k:item[k] for k in ('city','count','fetched_at','skipped_incomplete','excluded_by_scale','filter_version','filter_policy','license')}
        # HTTP downloads one requested city's geometry, not every city at map startup.
        target=DATA/'water'/f'{city}.json';target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_suffix('.json.tmp');temp.write_text(json.dumps(item,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8');temp.replace(target)
    result={'source':'OpenStreetMap contributors','license':'ODbL 1.0','coverage_note':'有名称且来源标注宽度至少20米的江河线、海域/海岸线，以及不小于0.1平方公里的江河水面、湖泊、水库和其他水面；排除溪流、沟渠、池塘和泉点。不代表饮用水源保护区、水质或供水范围。','filter_version':FILTER_VERSION,'filter_policy':FILTER_POLICY,'cities':cities}
    snapshot('hydrology_dataset',result);atomic_json(DATA/'water.json',result)
    return result

def crawl(cities=None,max_cities=1,force=False):
    cfg=json.loads((DATA/'cities.json').read_text(encoding='utf-8'));status_path=OUT/'status.json'
    status=json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {};log=[]
    candidates=cities or [c for c in cfg if cfg[c].get('sample70')]
    if any(c not in cfg for c in candidates):raise ValueError('Unknown configured city')
    for city in sorted(candidates,key=lambda c:status.get(c,{}).get('checked_at','')):
        prior=status.get(city,{})
        if not force and prior.get('checked_at') and datetime.fromisoformat(prior['checked_at'])>datetime.now(timezone.utc)-timedelta(days=30 if prior.get('status')=='ok' else 1):continue
        if len(log)>=max_cities:break
        entry=dict(prior,city=city,checked_at=now())
        try:
            query=water_query(city)
            response=requests.post(ENDPOINT,data={'data':query},headers={'User-Agent':'70CityHousingResearch/1.0 (public water geography)'},timeout=65)
            response.raise_for_status();raw=snapshot('hydrology_osm',response.content,'raw','json');payload=response.json()
            if not any(e.get('type')=='area' and e.get('tags',{}).get('name')==city+'市' for e in payload.get('elements',[])):raise ValueError('City area unresolved')
            item=parse(payload,city,dict(fetched_at=entry['checked_at'],raw=raw,source_url=ENDPOINT));path=OUT/f'city-{city}.json'
            if not item['count']:raise ValueError('No valid water geometry; previous city retained')
            if path.exists():
                previous=json.loads(path.read_text(encoding='utf-8'))
                if previous.get('filter_version')!=FILTER_VERSION:previous=reparse_city(path,previous)
                if item['count']<previous['count']*.8:raise ValueError('Water coverage fell >20% under the same filter; previous city retained')
            snapshot('hydrology_city',item);atomic_json(path,item)
            entry.update(status='ok',last_success=entry['checked_at'],count=item['count'],filter_version=FILTER_VERSION,error=None)
        except Exception as exc:entry.update(status='failed',error=str(exc))
        status[city]=entry;atomic_json(status_path,status);log.append(entry);print(json.dumps(entry,ensure_ascii=False),flush=True)
        if entry['status']=='failed':break
        time.sleep(2)
    build();return log

def reparse_city(path,prior):
    ref=prior.get('raw',{}).get('path');raw=ROOT/ref.replace('\\', '/') if ref else None
    if not raw or not raw.exists():raise ValueError('Raw water evidence missing; cannot migrate filter for '+prior['city'])
    payload=json.loads(raw.read_text(encoding='utf-8'))
    item=parse(payload,prior['city'],{k:prior[k] for k in ('fetched_at','raw','source_url')})
    snapshot('hydrology_city',prior);snapshot('hydrology_city',item);atomic_json(path,item)
    return item

def reparse_saved():
    """Apply the strict major-water filter to archived raw responses, without networking."""
    changed=0
    for path in sorted(OUT.glob('city-*.json')):
        prior=json.loads(path.read_text(encoding='utf-8'));item=reparse_city(path,prior);changed+=1
        print(json.dumps({'city':item['city'],'before':prior['count'],'after':item['count'],'filter_version':FILTER_VERSION},ensure_ascii=False),flush=True)
    build();return changed

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cities');p.add_argument('--max-cities',type=int,default=1);p.add_argument('--force',action='store_true');p.add_argument('--build',action='store_true');p.add_argument('--reparse',action='store_true')
    a=p.parse_args()
    if not 1<=a.max_cities<=70:p.error('max-cities must be 1..70')
    reparse_saved() if a.reparse else build() if a.build else crawl(a.cities.split(',') if a.cities else None,a.max_cities,a.force)
