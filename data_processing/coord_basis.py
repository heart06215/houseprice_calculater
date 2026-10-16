# -*- coding: utf-8 -*-
"""Per-source coordinate-system basis for community raw coordinates.

Acceptance doc A2: a source's raw coordinate system is a CANDIDATE set, never a
fact. This module records, per source, the raw field names, the candidate systems,
the evidence-backed chain (if any) and whether it is verified. An unverified
system must never yield a position or a numeric distance.

Offline only: standard library plus sibling modules (``metro_proximity``); no
network.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import metro_proximity

ROOT = Path(__file__).resolve().parent.parent
OSM_COMMUNITIES = (ROOT / 'data_processing' / 'frontend' / 'realestate' / 'data'
                   / 'communities.json')
OBSERVATIONS = ROOT / 'output' / 'source_trial_20260922' / 'observations.jsonl'
OUT_PATH = ROOT / 'output' / 'anjuke_community_trial_20260923' / 'coord_basis.json'

SCHEMA = 'coord_basis/v1'

# A chain counts as a clear winner only when its mean deviation is at most this
# fraction of the runner-up's; otherwise the sample is not decisive.
CLEAR_WIN_RATIO = 0.5

# Evidence-backed basis per source. ``verified`` gates whether a raw pair may be
# unified to WGS84 and used for distance; unverified sources keep the raw pair
# for traceability but produce no position.
SOURCE_BASIS = {
    '中原': {
        'raw_field_names': ['Lng', 'Lat'],
        'candidate_systems': ['BD-09', 'GCJ-02', 'WGS-84'],
        'chosen_chain': 'bd09->wgs84',
        'verified': True,
        'evidence': {
            'page_glob': 'output/source_trial_20260922/raw/sh_centanet__xiaoqu*.html',
            'script_match': '//api.map.baidu.com/api?v=2.0&ak=...',
            'raw_field_pointer': 'mapPostData[i].Lng / mapPostData[i].Lat',
            'note': '列表内嵌百度地图脚本，坐标取自 mapPostData 的 Lng/Lat，判定为 BD-09',
        },
    },
    '丽兹行': {
        'raw_field_names': ['mapX', 'mapY'],
        'candidate_systems': ['GCJ-02', 'WGS-84', 'BD-09'],
        'chosen_chain': 'bd09->wgs84',
        'verified': True,
        'evidence': {
            'page_glob': 'output/source_trial_20260922/raw/*lizihang*.html',
            'raw_field_pointer': 'window.__NUXT__...list[i].mapX / mapY',
            'note': '页面无地图服务脚本（单页证据不足），2026-09-30 经 OSM 同名点交叉验证核实：'
                    '191 样本中 bd09->wgs84 链平均偏差 375.6m/中位 76.3m，'
                    '显著小于 gcj02 假设（894.5m）与 raw 假设（1199.9m），clear_winner=true，'
                    '与已核实的中原（bd09，中位 42.2m）同链；详见 '
                    'output/collection_execution_0928/2026-09-30/coord_basis_lizihang_flip.json',
        },
    },
    '房天下': {
        'raw_field_names': [],
        'candidate_systems': [],
        'chosen_chain': None,
        'verified': False,
        'evidence': {
            'page_glob': 'output/fang/profiles.json',
            'raw_field_pointer': 'profiles.json 无坐标字段',
            'note': '房天下导出不含坐标字段，无原始坐标可判定',
        },
    },
}

# Verified chain name -> conversion callable (lng, lat) -> (lng, lat).
_CHAINS = {}


def bd09_to_wgs84(lng, lat):
    """BD-09 -> WGS84: BD-09 -> GCJ-02 (standard offset) then GCJ-02 -> WGS84."""
    lng, lat = float(lng), float(lat)
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000.0 / 180.0)
    gcj_lng = z * math.cos(theta)
    gcj_lat = z * math.sin(theta)
    return metro_proximity.gcj02_to_wgs84(gcj_lng, gcj_lat)


_CHAINS['bd09->wgs84'] = bd09_to_wgs84


def unify(source, lng, lat):
    """Apply a source's chosen chain only when its basis is verified.

    Unverified (or unknown) sources keep the raw pair for traceability but return
    ``unified_wgs84=None`` and ``distance_ready=False`` so no numeric distance can
    be derived from an unproven coordinate system.
    """
    basis = SOURCE_BASIS.get(source) or {}
    chain = basis.get('chosen_chain')
    verified = bool(basis.get('verified'))
    unified = None
    if verified and chain in _CHAINS and lng is not None and lat is not None:
        ulng, ulat = _CHAINS[chain](lng, lat)
        unified = {'lng': round(ulng, 7), 'lat': round(ulat, 7)}
    return {
        'source': source,
        'raw': {'lng': lng, 'lat': lat},
        'raw_field_names': list(basis.get('raw_field_names') or []),
        'candidate_systems': list(basis.get('candidate_systems') or []),
        'chain': chain if verified else None,
        'verified': verified,
        'unified_wgs84': unified,
        'coord_system': 'wgs84' if verified else 'unverified',
        'distance_ready': verified,
        'evidence': basis.get('evidence') or {},
    }


# --------------------------------------------------------------------------- #
# offline sample verification against the OSM feed
# --------------------------------------------------------------------------- #

def _distance_m(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _load_osm_points(path=None):
    """(city, name) -> (lng, lat) for OSM rows; the verification reference points."""
    source = Path(path) if path is not None else OSM_COMMUNITIES
    if not source.exists():
        return {}
    doc = json.loads(source.read_text(encoding='utf-8'))
    points = {}
    for row in doc.get('communities') or []:
        if (row.get('source') or 'osm') != 'osm':
            continue
        lng, lat = row.get('lng'), row.get('lat')
        if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
            continue
        points[(row.get('city'), row.get('name'))] = (lng, lat)
    return points


def _load_observations(path=None):
    source = Path(path) if path is not None else OBSERVATIONS
    if not source.exists():
        return []
    rows = []
    with source.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sample_verification(osm_path=None, obs_path=None):
    """Compare, for same-name communities in observations and the OSM feed, the
    distance from the OSM point to raw-as-WGS84 / gcj02->wgs84 / bd09->wgs84.

    Reports per source the sample count, mean/median distance per chain and which
    chain minimises. It only informs the basis; it never flips ``verified``.
    """
    osm = _load_osm_points(osm_path)
    by_source = {}
    for row in _load_observations(obs_path):
        src = row.get('source')
        if src not in SOURCE_BASIS:
            continue
        raw = row.get('coords_raw') or {}
        lng, lat = raw.get('lng'), raw.get('lat')
        if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
            continue
        point = osm.get((row.get('city'), row.get('name')))
        if point is None:
            continue
        olng, olat = point
        gcj = metro_proximity.gcj02_to_wgs84(lng, lat)
        bd = bd09_to_wgs84(lng, lat)
        by_source.setdefault(src, []).append({
            'raw_as_wgs84': _distance_m(olat, olng, lat, lng),
            'gcj02->wgs84': _distance_m(olat, olng, gcj[1], gcj[0]),
            'bd09->wgs84': _distance_m(olat, olng, bd[1], bd[0]),
        })

    report = []
    for src in ('中原', '丽兹行', '房天下'):
        samples = by_source.get(src, [])
        chains = {}
        for chain in ('raw_as_wgs84', 'gcj02->wgs84', 'bd09->wgs84'):
            values = [s[chain] for s in samples if s[chain] is not None]
            chains[chain] = {
                'mean_m': round(sum(values) / len(values), 1) if values else None,
                'median_m': round(_median(values), 1) if values else None,
            }
        means = {c: v['mean_m'] for c, v in chains.items() if v['mean_m'] is not None}
        ranked = sorted(means.values())
        best = min(means, key=means.get) if means else None
        clear = bool(best and len(ranked) > 1
                     and ranked[0] <= CLEAR_WIN_RATIO * ranked[1])
        report.append({
            'source': src,
            'sample_count': len(samples),
            'chains': chains,
            'best_chain': best,
            'clear_winner': clear,
            'note': ('未核实坐标系，样本不足以判定唯一链' if not clear else
                     f'{best} 平均偏差明显最小，与已核实判定一致'
                     if SOURCE_BASIS[src]['verified'] else
                     f'{best} 样本偏差最小，但缺少地图脚本证据仍保持未核实'),
        })
    return report


def build_document():
    sources = {}
    for name, basis in SOURCE_BASIS.items():
        sources[name] = {
            'raw_field_names': list(basis['raw_field_names']),
            'candidate_systems': list(basis['candidate_systems']),
            'chosen_chain': basis['chosen_chain'],
            'verified': basis['verified'],
            'evidence': basis['evidence'],
        }
    return {
        'schema': SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'sources': sources,
        'sample_verification': sample_verification(),
        'notes': [
            '未核实坐标系不得计为可算地铁距离（distance_ready=False）',
            '中原：列表内嵌百度地图脚本 + mapPostData Lng/Lat => BD-09',
            '丽兹行：无地图服务脚本证据，坐标系保持未核实',
            '房天下：导出无坐标字段，无原始坐标',
        ],
    }


def main():
    doc = build_document()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    return doc


if __name__ == '__main__':
    main()
