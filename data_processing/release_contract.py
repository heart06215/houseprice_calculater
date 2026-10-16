"""Fail publication on city, month, housing type, evidence or history mismatches."""
import json
import math
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from data_store import ROOT


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_city_extrema(city):
    """Match extrema and their dates to this city's own finite positive prices."""
    name = city['city']
    rows = sorted((r for r in city['monthly'] if isinstance(r.get('price'), (int, float))
                   and not isinstance(r['price'], bool) and math.isfinite(r['price'])
                   and r['price'] > 0), key=lambda r: r['date'])
    peak = max((r['price'] for r in rows), default=None)
    minimum = min((r['price'] for r in rows), default=None)
    peaks = [r for r in rows if r['price'] == peak]
    peak_date = peaks[0]['date'] if peaks else None
    peak_year = int(peak_date[:4]) if peak_date else None
    current = city['current_price']
    drop = round((current - peak) / peak * 100, 1) if peak and isinstance(current, (int, float)) and current > 0 else None
    require(city['max_price'] == peak and city['min_price'] == minimum, name + ': extrema mismatch')
    require(city.get('peak_date') == peak_date, name + ': peak date mismatch (earliest tied maximum)')
    require(city.get('peak_year') == peak_year, name + ': peak year mismatch')
    require(city.get('drop_from_peak') == drop, name + ': peak drawdown mismatch')
    return dict(city=name, price=peak, date=peak_date, dates=[r['date'] for r in peaks],
                method=peaks[0].get('method') if peaks else None)


def official_month(rows, month, expected_cities=None):
    from stats_gov_crawler import AREA_TYPES
    require(bool(re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month)), 'Invalid report month')
    require(month < date.today().strftime('%Y-%m'), 'Unfinished official report month')
    groups = {'new_house': ['合计'], 'second_hand': ['合计'],
              'new_house_by_area': AREA_TYPES, 'second_hand_by_area': AREA_TYPES}
    require(len(rows) == 560, month + ': expected 560 official rows')
    keys = set()
    cities = {r.get('city') for r in rows}
    require(len(cities) == 70 and None not in cities, month + ': expected 70 cities')
    if expected_cities is not None:
        require(cities == set(expected_cities), month + ': city set mismatch')
    for row in rows:
        kind = row.get('table_type')
        require(kind in groups and row.get('area_type') in groups[kind], month + ': housing type/area mismatch')
        key = (row['city'], kind, row['area_type'])
        require(key not in keys, month + ': duplicate official row ' + str(key))
        keys.add(key)
        require(row.get('report_month') == month, month + ': filename/report month mismatch')
        require(urlparse(row.get('url', '')).hostname in ('www.stats.gov.cn', 'stats.gov.cn'), 'Nonofficial index URL')
        prefix = 'new_house' if kind.startswith('new_house') else 'second_hand'
        other = 'second_hand' if prefix == 'new_house' else 'new_house'
        for field in ('mom', 'yoy'):
            value = row.get(prefix + '_' + field)
            require(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 50 <= value <= 150, month + ': invalid index')
        require(all(row.get(other + '_' + field) is None for field in ('mom', 'yoy', 'annual')),
                month + ': new/second-hand fields mixed')
    expected = {(city, kind, area) for city in cities for kind, areas in groups.items() for area in areas}
    require(keys == expected, month + ': incomplete city/type/area grid')
    return keys


def validate(data, previous=None, root=ROOT):
    from inverse_price_engine import load_anjuke_data, load_stats_mom_data, propagate_price
    root = Path(root)
    cities = data.get('cities', [])
    names = [c['city'] for c in cities]
    require(len(names) == 70 and len(set(names)) == 70, 'Dashboard must contain 70 unique cities')
    indices = {}
    official = {}
    files = sorted((root / 'output/stats_gov').glob('stats_gov_*.json'))
    require(bool(files), 'Official source dataset missing')
    for path in files:
        month = path.stem[-7:]
        rows = json.loads(path.read_text(encoding='utf-8'))
        official_month(rows, month, names)
        for row in rows:
            prefix = 'new_house' if row['table_type'].startswith('new_house') else 'second_hand'
            official[(month, row['city'], prefix, row['area_type'])] = row
            if row['table_type'] == 'second_hand':
                indices[(row['city'], month)] = row['second_hand_mom']
    months = [p.stem[-7:] for p in files]
    ordinals = [int(m[:4])*12 + int(m[5:]) for m in months]
    require(months[0] == '2021-04' and ordinals == list(range(ordinals[0], ordinals[-1]+1)),
            'Official monthly history has gaps')
    observations = load_anjuke_data()
    source_indices = load_stats_mom_data()
    available = observed = estimated = 0
    for city in cities:
        name = city['city']
        rows = city['monthly']
        dates = [r['date'] for r in rows]
        require(dates == sorted(set(dates)) and bool(dates), name + ': duplicate/unsorted months')
        require(set(months).issubset(dates), name + ': official month omitted')
        require(city['current_price'] == rows[-1]['price'], name + ': current price/month mismatch')
        mom = {(y, m): v for (c, y, m), v in source_indices.items() if c == name}
        for row in rows:
            require(row['date'] < date.today().strftime('%Y-%m'), name + ': unfinished price month')
            ym = tuple(map(int, row['date'].split('-')))
            actual = observations.get((name, *ym))
            method = row.get('method')
            if actual is not None:
                require(method == 'listing_observation' and row['price'] == actual,
                        name + ': verified observation replaced ' + row['date'])
                observed += 1
            elif method == 'index_estimate':
                anchor = tuple(map(int, row['anchor_date'].split('-')))
                price = propagate_price(observations.get((name, *anchor)), anchor, ym, mom)
                require(price is not None and price == row['price'], name + ': derived chain mismatch ' + row['date'])
                estimated += 1
            else:
                require(method == 'unavailable' and row['price'] is None, name + ': unsupported absolute price')
            if row['price'] is not None:
                require(isinstance(row['price'], (int, float)) and math.isfinite(row['price']) and row['price'] > 0,
                        name + ': invalid price')
                available += 1
        validate_city_extrema(city)
    for kind in ('new_house', 'second_hand'):
        segment = data.get('housing_segments', {}).get(kind, {}).get('cities', [])
        require({c['city'] for c in segment} == set(names), kind + ': segment city mismatch')
        for city in segment:
            for row in city['monthly']:
                source = official.get((row['month'], city['city'], kind, '合计'))
                if source:
                    require(row['mom_pct'] == round(source[kind+'_mom']-100, 2)
                            and row['yoy_pct'] == round(source[kind+'_yoy']-100, 2),
                            kind + ': official index projection mismatch')
    info = data.get('information', {})
    from market_evidence import validate_projection as validate_market
    market_quotes = validate_market(data, root)
    releases = info.get('releases', [])
    require({r['month'] for r in releases} == set(months), 'Release months mismatch')
    for release in releases:
        require(len(release['rows']) == 560, 'Incomplete release projection')
        keys = set()
        for row in release['rows']:
            key = (release['month'], row['city'], row['kind'], row['area'])
            require(key not in keys and key in official, 'Duplicate/mismatched release projection')
            keys.add(key)
            source = official[key]
            require(row['mom_pct'] == round(source[row['kind']+'_mom']-100, 2)
                    and row['yoy_pct'] == round(source[row['kind']+'_yoy']-100, 2), 'Release index conversion mismatch')
    for row in info.get('cih', {}).get('prices', []):
        require(row['kind'] in ('new_house', 'second_hand', 'rent', 'land'), 'Unknown CIH sample type')
        require(row.get('unit') == ('元/㎡·月' if row['kind'] == 'rent' else '元/㎡'), 'CIH sample unit mismatch')
        require(isinstance(row['price'], (int, float)) and math.isfinite(row['price']) and row['price'] > 0, 'Invalid CIH price')
    income_path=root/'output/income/clean.json'
    if income_path.exists():
        saved=json.loads(income_path.read_text(encoding='utf-8'))
        from income_crawler import validate_evidence as validate_income
        validate_income(saved, root)
        for field in ('income','minimum_wages'):
            require(info.get('income',{}).get(field)==saved.get(field), 'Income source/projection mismatch: '+field)
    cih_path=root/'output/cih/clean.json'
    if cih_path.exists():
        saved=json.loads(cih_path.read_text(encoding='utf-8'))
        expected={(r['city'],r['kind'],r['period']):r for r in saved.get('observations',[])}
        for page in saved.get('pages',[]):
            for row in page.get('prices',[]):
                expected[(row['city'],row['kind'],row['period'])]=dict(row,fetched_at=page['fetched_at'])
        projected=info.get('cih',{}).get('prices',[])
        keys={(r['city'],r['kind'],r['period']) for r in projected}
        require(len(projected)==len(keys) and keys==set(expected), 'CIH source/projection coverage mismatch')
        for row in projected:
            require(row==expected.get((row['city'],row['kind'],row['period'])), 'CIH city/type/month/value mismatch')
    from realestate_crawler import validate_evidence as validate_realestate
    land = info.get('cih', {}).get('land')
    if land:
        from cih_land import validate_evidence as validate_land
        validate_land(land, root)
    from lpr_crawler import validate_evidence
    macro_path=root/'output/macro/clean.json'
    if macro_path.exists():
        require(info.get('macro',{})==json.loads(macro_path.read_text(encoding='utf-8')), 'Macro source/projection mismatch')
    for kind,record in info.get('macro',{}).items():
        require(kind in ('lpr','realestate'), 'Unknown macro series')
        if kind=='lpr':
            validate_evidence(record,root,(previous or {}).get('information',{}).get('macro',{}).get('lpr'))
            continue
        validate_realestate(record,root,(previous or {}).get('information',{}).get('macro',{}).get('realestate'))
    if previous:
        old_cities = {c['city']: c for c in previous['cities']}
        for city in cities:
            current = {r['date']: r for r in city['monthly']}
            for row in old_cities[city['city']]['monthly']:
                if row.get('price') is not None:
                    require(current.get(row['date'], {}).get('price') is not None,
                            city['city'] + ': valid historical price lost ' + row['date'])
        old_info = previous.get('information', {})
        for key, fields in [('cih', ('prices', 'daily', 'rankings')), ('income', ('income', 'minimum_wages'))]:
            for field in fields:
                require(len(info.get(key, {}).get(field, [])) >= len(old_info.get(key, {}).get(field, [])),
                        key + '/' + field + ': history shrank')
                identity={('cih','prices'):('city','kind','period'),('cih','daily'):('city','date'),
                          ('cih','rankings'):('kind','period'),('income','income'):('year',),
                          ('income','minimum_wages'):('as_of',)}[(key,field)]
                ids=lambda rows:{tuple(r.get(k) for k in identity) for r in rows}
                require(ids(old_info.get(key,{}).get(field,[])).issubset(ids(info.get(key,{}).get(field,[]))),
                        key+'/'+field+': historical period identities lost')
        for kind,old in old_info.get('macro',{}).items():
            current=info.get('macro',{}).get(kind,{})
            field='as_of' if kind=='lpr' else 'month'
            require(current.get(field,'')>=old.get(field,''), 'Macro publication moved backwards')
    return dict(cities=70, official_months=len(months), official_rows=len(official),
                price_rows=available, observed_rows=observed, estimated_rows=estimated,
                latest_official=months[-1], market_quotes=market_quotes)


if __name__ == '__main__':
    path = ROOT / 'data_processing/frontend/intersection_housing_data.json'
    print(validate(json.loads(path.read_text(encoding='utf-8'))))
