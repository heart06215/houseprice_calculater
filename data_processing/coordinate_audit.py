# -*- coding: utf-8 -*-
"""判定来源经纬度的坐标系：与 WGS84 参照点（OSM/地铁）同名互证。

对每个同名且唯一匹配的来源点，分别按 raw(视为 WGS84)、GCJ-02→WGS84、BD-09→WGS84
三种假设计算与参照点的距离，取中位误差最小的假设作为候选结论。只做判定与报告，
不修改任何来源数据；是否采用由调用方决定。

python data_processing/coordinate_audit.py --source output/community_directory/5i5j_北京.jsonl \
    --reference output/communities/city-北京.json
"""
import argparse
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from data_store import ROOT, atomic_json

OUT = ROOT / 'output/community_directory'
PI = math.pi
AXIS = 6378245.0
EE = 0.00669342162296594323


def now():
    return datetime.now(timezone.utc).isoformat()


def _out_of_china(lat, lng):
    return not (0.8293 <= lat <= 55.8271 and 72.004 <= lng <= 137.8347)


def _transform_lat(x, y):
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat, lng):
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((AXIS * (1 - EE)) / (magic * sqrtmagic) * PI)
    dlng = (dlng * 180.0) / (AXIS / sqrtmagic * math.cos(radlat) * PI)
    return round(lat + dlat, 6), round(lng + dlng, 6)


def gcj02_to_wgs84(lat, lng):
    if _out_of_china(lat, lng):
        return lat, lng
    glat, glng = wgs84_to_gcj02(lat, lng)
    dlat, dlng = glat - lat, glng - lng
    return round(lat - dlat, 6), round(lng - dlng, 6)


def gcj02_to_bd09(lat, lng):
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * PI * 3000.0 / 180.0)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * PI * 3000.0 / 180.0)
    return round(z * math.sin(theta) + 0.006, 6), round(z * math.cos(theta) + 0.0065, 6)


def bd09_to_gcj02(lat, lng):
    x, y = lng - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * PI * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * PI * 3000.0 / 180.0)
    return round(z * math.sin(theta), 6), round(z * math.cos(theta), 6)


def bd09_to_wgs84(lat, lng):
    glat, glng = bd09_to_gcj02(lat, lng)
    return gcj02_to_wgs84(glat, glng)


HYPOTHESES = {'raw': lambda lat, lng: (lat, lng), 'gcj02': gcj02_to_wgs84,
              'bd09': bd09_to_wgs84}


def haversine(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def name_key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def load_records(path):
    path = Path(path)
    if path.suffix.lower() == '.jsonl':
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(payload, dict):
        return payload.get('records') or payload.get('communities') or []
    return payload


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def audit(source, reference, *, tolerance_m=2000):
    index = defaultdict(list)
    for row in reference:
        if row.get('lat') is None or row.get('lng') is None:
            continue
        index[name_key(row.get('name'))].append(row)
    pairs = []
    for row in source:
        if row.get('lng') is None or row.get('lat') is None:
            continue
        candidates = index.get(name_key(row.get('name')), [])
        if len(candidates) != 1:
            continue
        ref = candidates[0]
        distances = {}
        for name, transform in HYPOTHESES.items():
            lat, lng = transform(row['lat'], row['lng'])
            distances[name] = round(haversine(lat, lng, ref['lat'], ref['lng']), 1)
        pairs.append(dict(name=row.get('name'), city=row.get('city'), reference=ref.get('name'),
                          distances=distances))
    summary = {}
    for name in HYPOTHESES:
        values = [p['distances'][name] for p in pairs]
        summary[name] = dict(pairs=len(values), median_m=_median(values),
                             within_200m=sum(1 for v in values if v <= 200),
                             within_500m=sum(1 for v in values if v <= 500))
    winner = None
    ranked = [h for h in summary if summary[h]['median_m'] is not None]
    if ranked:
        winner = min(ranked, key=lambda h: summary[h]['median_m'])
    return dict(schema='coordinate_audit/v1', at=now(), pairs=len(pairs), summary=summary,
                winner=winner, tolerance_m=tolerance_m, samples=pairs[:100])


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--out', type=Path, default=OUT / 'coordinate_audit.json')
    args = parser.parse_args(argv)
    result = audit(load_records(args.source), load_records(args.reference))
    atomic_json(args.out, result)
    print(json.dumps({k: result[k] for k in ('pairs', 'winner', 'summary')}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
