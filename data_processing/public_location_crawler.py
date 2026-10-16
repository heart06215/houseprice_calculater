"""Public metro station and government presale feeds, with archived raw responses.

No inferred prices or fabricated project coordinates. Run feeds independently.
"""
import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests
import metro_proximity
from data_store import ROOT, atomic_json, snapshot, archive_project

DATA = ROOT/'data_processing/frontend/realestate/data'
OUT = ROOT/'output/public_locations'
METRO = 'https://map.amap.com/service/subway'
PERMITS = 'https://zjj.nanning.gov.cn/commonCenter.do'
METRO_SOURCE_NAME='高德公开地铁图'
METRO_SOURCE_VERSION='metro-source:v1'


def fetch(url, source, **kwargs):
    response = requests.request('POST' if 'data' in kwargs else 'GET', url, timeout=25, **kwargs)
    response.raise_for_status()
    raw = snapshot(source, response.content, 'raw')
    return response.json(), dict(url=response.url, raw=raw, fetched_at=datetime.now(timezone.utc).isoformat())


def parse_metro(payload, name):
    lines = []
    for line in payload.get('l', []):
        if str(line.get('su', '1')) != '1':
            continue
        stations, ids, seen = [], [], set()
        for row in line.get('st', []):
            if str(row.get('su', '1')) != '1':
                continue
            lng, lat = map(float, row['sl'].split(','))
            if not (70 < lng < 140 and 15 < lat < 55):
                raise ValueError('Invalid geographic coordinate; schematic points are not coordinates')
            key = (row['n'], lng, lat)
            if key not in seen:
                stations.append(list(key))
                ids.append(row.get('poiid') or row.get('sid'))
                seen.add(key)
        if stations:
            lines.append(dict(name=line.get('kn') or line['ln'], color='#'+line.get('cl', '6532A8'),
                              stations=stations, station_ids=ids))
    if not lines:
        raise ValueError('Empty metro response; existing city retained')
    return dict(name=name, lines=lines, coordinate_system='GCJ-02', source='高德公开地铁图',
                coverage_note='仅表示来源当前返回的线路站点，不保证覆盖所有最新通车线路')


def metro_source_metadata(directory):
    """Source provenance for a metro run record. No network I/O."""
    directory=directory or {}
    return dict(name=METRO_SOURCE_NAME,url=directory.get('url'),
                fetched_at=directory.get('fetched_at'),version=METRO_SOURCE_VERSION)


def metro_run_record(directory,results):
    return dict(directory=directory,source=metro_source_metadata(directory),results=results)


def verify_stations(stations,evidence=None):
    """Attach per-station operational verification. No network I/O.

    Registry semantics live in metro_proximity; this only maps registry records
    onto station dicts. Anything unverifiable stays unknown/unverified_legacy.
    """
    evidence=evidence or {}
    registry={}
    try:
        for record in metro_proximity.load_stations():
            if record.get('station_id'):
                registry[record['station_id']]=record
            if record.get('name'):
                registry.setdefault(record['name'],record)
    except Exception:
        registry={}
    verified=[]
    for station in stations:
        row=dict(station)
        match=registry.get(row.get('station_id')) or registry.get(row.get('name'))
        item=evidence.get(row.get('station_id')) or evidence.get(row.get('name')) or {}
        source=match or item
        status=source.get('operational_status')
        date=source.get('status_evidence_date')
        state=source.get('verification_state')
        precision=source.get('coord_precision')
        if match:
            row['station_id']=match.get('station_id',row.get('station_id'))
            row['lines']=match.get('lines',row.get('lines'))
        # Never publish operational without an evidence date.
        if status==metro_proximity.OPERATIONAL_STATUS and not date:
            status=None;state=None
        elif status is None and date:
            status=metro_proximity.OPERATIONAL_STATUS
        if status is None:
            status=metro_proximity.OPERATIONAL_STATUS_UNKNOWN
        if state is None:
            state=(metro_proximity.VERIFICATION_STATE_VERIFIED
                   if status==metro_proximity.OPERATIONAL_STATUS and date
                   else metro_proximity.VERIFICATION_STATE_LEGACY)
        row['operational_status']=status
        row['status_evidence_date']=date
        row['verification_state']=state
        row['coord_precision']=precision or 'unknown'
        verified.append(row)
    return verified


def crawl_metro():
    old = json.loads((DATA/'metro.json').read_text(encoding='utf-8'))
    cities = {c['name']:c for c in old.get('cities', [])}
    listing, evidence = fetch(METRO+'?srhdata=citylist.json', 'metro')
    log = []
    for item in listing['citylist']:
        name = item['cityname'].removesuffix('市')
        url = METRO+'?'+urlencode({'srhdata':f"{item['adcode']}_drw_{item['spell']}.json"})
        try:
            payload, source = fetch(url, 'metro')
            city = parse_metro(payload, name)
            previous_count=sum(len(l['stations']) for l in cities.get(name,{}).get('lines',[]))
            if sum(len(l['stations']) for l in city['lines']) < previous_count*.8:
                raise ValueError('Station coverage fell by more than 20%; retained previous city')
            city.update(source)
            cities[name] = city
            log.append(dict(city=name, status='ok', stations=sum(len(l['stations']) for l in city['lines'])))
        except Exception as exc:
            log.append(dict(city=name, status='failed', error=str(exc)))
        atomic_json(OUT/'metro_run.json', metro_run_record(evidence,log))
        print(json.dumps(log[-1], ensure_ascii=False), flush=True)
        time.sleep(.6)
    result = dict(cities=list(cities.values()), updated_at=datetime.now(timezone.utc).isoformat())
    snapshot('metro', result)
    atomic_json(DATA/'metro.json', result)


def normalize_permits(records):
    projects = {}
    for row in records:
        name = re.sub(r'\s+', '', row.get('XMMC', ''))
        district = row.get('CQ', '').strip()
        if not name or not row.get('YSZH'):
            continue
        # One project can have dozens of building permits; preserve them as evidence.
        key = '|'.join(['南宁', district, name, row.get('GSMC', '').strip()])
        pid = 'gov-nanning-'+hashlib.sha256(key.encode()).hexdigest()[:20]
        project = projects.setdefault(pid, dict(id=pid, name=name, community=name, city='南宁', province='广西',
            district=district, type='预售项目', source='government', lat=None, lng=None, precision='unknown',
            price_avg=0, area=0, listings=[], tags=['官方预售许可','用途待核验','无成交报价'],
            developer=row.get('GSMC'), address=row.get('XMZL'), permits=[],
            source_url='https://zjj.nanning.gov.cn/web/ysxx.html',
            location_note='公示未提供坐标；列表可查，暂不投放地图点位'))
        if not any(p['number']==row['YSZH'] for p in project['permits']):
            project['permits'].append(dict(number=row['YSZH'], address=row.get('XMZL'),
                approved_at='以预售证记载时间为准' if '武鸣区' in district else row.get('PZYSSJ'),
                raw_approved_at=row.get('PZYSSJ'), permitted_area_sqm=row.get('YSMJ'),
                source_url='https://zjj.nanning.gov.cn/web/zxxh.html?'+urlencode({'yszh':row['YSZH'],'cq':'武鸣区' if '武鸣' in district else ''}),
                evidence=row.get('_evidence')))
    return list(projects.values())


def crawl_permits(max_pages, refresh=False):
    path = OUT/'nanning_permits.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(records=[], pages={})
    records = {r['YSZH']:r for r in previous['records']}
    pages = previous.get('pages', {})
    log = []
    # Always refresh page one. Resume uncrawled pages instead of repeatedly starting over.
    queue = [1]+[n for n in range(2, max_pages+1) if refresh or str(n) not in pages]
    for page in queue:
        try:
            payload, source = fetch(PERMITS, 'government_nanning', data=dict(pageNum=page, YSZH='', XMZL='',
                GSMC='', XMMC='', serviceid='zfj_document', methodname='queryNumberListZJJ'))
            if not isinstance(payload.get('result'), list) or int(payload.get('currentPage',0))!=page:
                raise ValueError('Unexpected pagination schema')
            if not payload['result']:
                break
            for row in payload['result']:
                if not row.get('YSZH') or not row.get('XMMC'):
                    raise ValueError('Missing permit identity')
                records[row['YSZH']] = dict(row, _evidence=source)
            pages[str(page)] = source
            result = dict(records=list(records.values()), pages=pages, total_available=payload['totalRecord'],
                          updated_at=source['fetched_at'], source_url='https://zjj.nanning.gov.cn/web/ysxx.html')
            atomic_json(path, result)
            log.append(dict(page=page, status='ok', records=len(records)))
            if page >= int(payload['totalPage']):
                break
        except Exception as exc:
            log.append(dict(page=page, status='failed', error=str(exc)))
            break  # A rate limit or malformed response stops this source.
        if page%20==0 or page==1:
            print(json.dumps(log[-1],ensure_ascii=False),flush=True)
        time.sleep(.6)
    atomic_json(OUT/'permits_run.json', log)
    projects = normalize_permits(records.values())
    if projects:
        snapshot('government_projects', projects)
        existing_path=DATA/'properties_government.json'
        existing=json.loads(existing_path.read_text(encoding='utf-8')) if existing_path.exists() else []
        atomic_json(existing_path,[p for p in existing if not str(p.get('id','')).startswith('gov-nanning-')]+projects)
        published = json.loads((DATA/'properties.json').read_text(encoding='utf-8'))
        # Retain all original records; projects are separate from housing listings.
        published = [p for p in published if not str(p.get('id','')).startswith('gov-nanning-')]+projects
        atomic_json(DATA/'properties.json', published)
    print(f'Government projects: {len(projects)}; permits: {len(records)}',flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feed',choices=['metro','permits'])
    parser.add_argument('--max-pages',type=int,default=200)
    parser.add_argument('--refresh',action='store_true',help='Refresh already saved permit pages to capture newly inserted records')
    args=parser.parse_args()
    if not 1<=args.max_pages<=1500:
        parser.error('max-pages must be between 1 and 1500')
    archive_project()
    crawl_metro() if args.feed=='metro' else crawl_permits(args.max_pages,args.refresh)
    archive_project()
