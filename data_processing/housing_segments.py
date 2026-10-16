"""Separate observed yuan/sqm quotes from official price indices by housing type."""
import json
from pathlib import Path
from datetime import date
from statistics import mean, median
import math

ROOT = Path(__file__).resolve().parent.parent


def build(data):
    cities = {c['city']: c for c in data['cities']}
    records = {kind: {} for kind in ('new_house','second_hand')}
    for path in sorted((ROOT/'output/stats_gov').glob('*.json')):
        for row in json.loads(path.read_text(encoding='utf-8')):
            kind, city, month = row.get('table_type'), row.get('city'), row.get('report_month','')
            if kind not in records or city not in cities or row.get('area_type') != '合计' or month >= date.today().strftime('%Y-%m'):
                continue
            mom, yoy = row.get(kind+'_mom'), row.get(kind+'_yoy')
            if not isinstance(mom,(float,int)) or not math.isfinite(mom) or not 50 <= mom <= 150:
                continue
            records[kind].setdefault(city,{})[month] = dict(month=month, mom_pct=round(mom-100,2),
                yoy_pct=round(yoy-100,2) if isinstance(yoy,(float,int)) and math.isfinite(yoy) else None,
                source_url=row.get('url'), price=None, price_source=None, provisional=False)
    quotes_path=ROOT/'output/market/quotes.json'
    quotes=json.loads(quotes_path.read_text(encoding='utf-8')) if quotes_path.exists() else []
    for r in quotes:
        if r['kind'] not in records or r['city'] not in cities:
            continue
        point=records[r['kind']].setdefault(r['city'],{}).setdefault(r['month'],dict(month=r['month'],mom_pct=None,yoy_pct=None,source_url=None))
        point.update(price=r['price'],price_source=r['source_url'],provisional=r.get('provisional',False),fetched_at=r.get('fetched_at'))
    segments={}
    for kind, by_city in records.items():
        result=[]
        for city, months in by_city.items():
            ordered=[months[k] for k in sorted(months)]
            value=100.0
            prev=None
            for row in ordered:
                y,m=map(int,row['month'].split('-'))
                ordinal=y*12+m
                if prev is None:
                    base=row['month']
                elif ordinal != prev+1 or row['mom_pct'] is None:
                    value=None
                elif value is not None:
                    value *= 1+row['mom_pct']/100
                row['index']=round(value,2) if value is not None else None
                row['index_base']=base
                prev=ordinal
            result.append(dict(city=city,tier=cities[city]['tier'],monthly=ordered))
        segments[kind]=dict(label='新房房价' if kind=='new_house' else '二手房房价',cities=result)
    latest=max((r['month'] for c in segments['new_house']['cities'] for r in c['monthly'] if r['mom_pct'] is not None),default=None)
    research=[]
    for tier in ('all','S','1','2','3'):
        entry={'tier':tier,'month':latest}
        for kind in records:
            rows=[r for city,months in records[kind].items() if tier=='all' or cities[city]['tier']==tier for month,r in months.items() if month==latest and r['mom_pct'] is not None]
            yoy=[r['yoy_pct'] for r in rows if r['yoy_pct'] is not None]
            n=len(rows)
            entry[kind]=dict(n=n,up=sum(r['mom_pct']>0 for r in rows),down=sum(r['mom_pct']<0 for r in rows),
                flat=sum(r['mom_pct']==0 for r in rows),median_mom=round(median(r['mom_pct'] for r in rows),2) if n else None,
                mean_yoy=round(mean(yoy),2) if yoy else None,
                breadth=round((sum(r['mom_pct']>0 for r in rows)-sum(r['mom_pct']<0 for r in rows))/n*100,1) if n else None)
        entry['gap_pp']=round(entry['new_house']['median_mom']-entry['second_hand']['median_mom'],2) if entry['new_house']['n'] and entry['second_hand']['n'] else None
        research.append(entry)
    data['housing_segments']=segments
    data['research']=dict(month=latest,groups=research,basis='国家统计局合计指数；分组同比为城市等权平均，非官方分级加权指数')
    return data


def main():
    target=ROOT/'data_processing/frontend/intersection_housing_data.json'
    data=build(json.loads(target.read_text(encoding='utf-8')))
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
