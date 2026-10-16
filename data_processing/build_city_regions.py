"""Build nationwide administrative polygons and align all 70 statistical cities."""
import json
import time
import requests
from data_store import ROOT, atomic_json, snapshot

DATA=ROOT/'data_processing/frontend/realestate/data'
CACHE=ROOT/'output/administrative_regions'


def fetch(code):
    path=CACHE/f'{code}.json'
    if path.exists():return json.loads(path.read_text(encoding='utf-8'))
    url=f'https://geo.datav.aliyun.com/areas_v3/bound/{code}_full.json'
    r=requests.get(url,timeout=25);r.raise_for_status();data=r.json()
    if data.get('type')!='FeatureCollection':raise ValueError('Invalid boundary response')
    snapshot('administrative_regions',r.content,'raw')
    atomic_json(path,data)
    time.sleep(.3)
    return data


def build():
    sample=json.loads((ROOT/'data_processing/frontend/intersection_housing_data.json').read_text(encoding='utf-8'))['cities']
    wanted={c['city']:c for c in sample}
    provinces=fetch(100000)
    regions=[]
    for f in provinces['features']:
        p=f['properties'];code=p.get('adcode')
        if not isinstance(code,int):
            regions.append(f)
            continue
        if code in (110000,120000,310000,500000,710000,810000,820000):
            regions.append(f)
        else:
            regions.extend(fetch(code)['features'])
        print(p['name'],flush=True)
    # NBS Dali refers to Dali city, not the whole autonomous prefecture.
    regions=[f for f in regions if f['properties'].get('adcode')!=532900]+fetch(532900)['features']
    config=json.loads((DATA/'cities.json').read_text(encoding='utf-8'))
    found=set()
    for f in regions:
        p=f['properties'];name=p['name'].removesuffix('市')
        if name in wanted:
            p['name']=name;p['sample70']=True;found.add(name)
            c=wanted[name]
            config[name]=dict(config.get(name,{}),center=[c['lon'],c['lat']],zoom=11,adcode=p['adcode'],sample70=True)
        else:p['sample70']=False
    if found!=set(wanted):raise ValueError('Missing boundaries: '+str(set(wanted)-found))
    result=dict(type='FeatureCollection',features=regions,source_url='https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json',sample_city_count=len(found))
    atomic_json(DATA/'city_regions.json',result)
    atomic_json(DATA/'cities.json',config)
    snapshot('city_regions',result)
    print(f'Validated {len(found)} statistical cities; {len(regions)} administrative polygons',flush=True)


if __name__=='__main__':build()
