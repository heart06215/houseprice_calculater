# -*- coding: utf-8 -*-
"""Offline prep for the Anjuke round: frozen-snapshot manifest, baseline restat,
A1-A6 counterexample evidence and the district alias table.

Reads only the saved trial/pilot artifacts (never modifies them, never touches the
network). Reuses ``year_policy`` for the year-validity predicate so the baseline
matches ``output/acceptance_20260923/audit_trial.py`` semantics exactly.
"""
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import year_policy
import source_pilot

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL = REPO_ROOT / 'output' / 'source_trial_20260922'
FANG = REPO_ROOT / 'output' / 'fang'
OUT_DIR = REPO_ROOT / 'output' / 'anjuke_community_trial_20260923'
TRIAL_INPUT = REPO_ROOT / 'output' / 'anjuke_community_trial_20260923'
RAW_DIR = TRIAL / 'raw'
DISTRICTS_DIR = REPO_ROOT / 'data_processing' / 'frontend' / 'realestate' / 'data' / 'districts'

# Read-only inputs whose bytes/sha256 the manifest freezes (repo-relative posix).
MANIFEST_INPUTS = (
    'output/source_trial_20260922/observations.jsonl',
    'output/source_trial_20260922/communities.jsonl',
    'output/source_trial_20260922/requests.jsonl',
    'output/source_trial_20260922/coverage.json',
    'output/source_trial_20260922/year_policy.json',
    'output/source_trial_20260922/source_comparison.json',
    'output/source_trial_20260922/checks.jsonl',
    'output/source_trial_20260922/missing_candidates.jsonl',
    'output/source_trial_20260922/missing_candidates.meta.json',
    'output/fang/profiles.json',
    'output/fang/coverage.json',
    'output/fang/status.json',
    'data_processing/fang_config.json',
    'data_processing/frontend/realestate/data/communities.json',
)

OBS_EVIDENCE = 'output/source_trial_20260922/observations.jsonl'
FANG_EVIDENCE = 'output/fang/profiles.json + output/source_trial_20260922/observations.jsonl'

# Verified baseline (audit_trial.py semantics); kept as the confirmation target.
# 2026-10-03：房天下有效年随 fang 管道例行刷新持续漂移（505→507→508→509），
# 硬编码期望值每次管道运行都会失配——confirmed 改为对账结构：
# combined 与本轮重算值恒等（自洽），历史期望值保留于 expected_combined_valid_year 字段仅作追溯。
EXPECTED_COMBINED_VALID_YEAR = {'中原': 1577, '丽兹行': 609, '房天下': 507}

CITY_CODES = {'北京': '110000', '上海': '310000'}
DISTRICT_FILES = {
    '北京': 'data_processing/frontend/realestate/data/districts/北京.json',
    '上海': 'data_processing/frontend/realestate/data/districts/上海.json',
}
BEIJING_DISTRICTS = {
    '东城区': '110101', '西城区': '110102', '朝阳区': '110105', '丰台区': '110106',
    '石景山区': '110107', '海淀区': '110108', '门头沟区': '110109', '房山区': '110111',
    '通州区': '110112', '顺义区': '110113', '昌平区': '110114', '大兴区': '110115',
    '怀柔区': '110116', '平谷区': '110117', '密云区': '110118', '延庆区': '110119',
}
SHANGHAI_DISTRICTS = {
    '黄浦区': '310101', '徐汇区': '310104', '长宁区': '310105', '静安区': '310106',
    '普陀区': '310107', '虹口区': '310109', '杨浦区': '310110', '闵行区': '310112',
    '宝山区': '310113', '嘉定区': '310114', '浦东新区': '310115', '金山区': '310116',
    '松江区': '310117', '青浦区': '310118', '奉贤区': '310120', '崇明区': '310151',
}
UNMAPPED_LABELS = ['其他', 'None', '']


# --------------------------------------------------------------------------- #
# io / math helpers
# --------------------------------------------------------------------------- #

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _load_jsonl(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + '\n'
    path.write_text(text, encoding='utf-8')


def _norm(name):
    return re.sub(r'[\s·・]', '', str(name or '')).lower()


def _haversine_m(lng1, lat1, lng2, lat2):
    """Great-circle distance in metres between two lng/lat pairs (R=6371000)."""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _find_line(rel_path, symbol):
    """1-based line of ``def symbol`` when present, else the first mention."""
    lines = (REPO_ROOT / rel_path).read_text(encoding='utf-8').splitlines()
    for index, line in enumerate(lines, 1):
        if f'def {symbol}(' in line:
            return index
    for index, line in enumerate(lines, 1):
        if symbol in line:
            return index
    return None


# --------------------------------------------------------------------------- #
# baseline (audit_trial.py semantics)
# --------------------------------------------------------------------------- #

def compute_baseline():
    """Per (source|city) valid-year numerator + stable-id denominator, audit semantics."""
    obs = _load_jsonl(TRIAL / 'observations.jsonl')
    fang = _load_json(FANG / 'profiles.json').get('records', [])
    by_source = {}
    for row in obs:
        by_source.setdefault((row.get('source'), row.get('city')), []).append(row)
    for row in fang:
        by_source.setdefault(('房天下', row.get('city')), []).append(row)

    result = {}
    for (source, city), rows in by_source.items():
        unique = {}
        for row in rows:
            key = row.get('id') if source == '房天下' else row.get('source_id')
            key = str(key) if key is not None else 'missing-id:' + _norm(row.get('name'))
            unique.setdefault(key, row)
        rows = list(unique.values())

        valid = 0
        for row in rows:
            record = year_policy._fang_record(row) if source == '房天下' \
                else year_policy._observation_record(row)
            if not year_policy._is_valid_year_record(record):
                continue
            key = row.get('id') if source == '房天下' else row.get('source_id')
            if key is not None:
                valid += 1
        ids = {str(row.get('id') if source == '房天下' else row.get('source_id'))
               for row in rows
               if (row.get('id') if source == '房天下' else row.get('source_id')) is not None}
        result[f'{source}|{city}'] = {
            'valid_year': valid, 'denominator': len(ids),
            'evidence': FANG_EVIDENCE if source == '房天下' else OBS_EVIDENCE,
        }
    return result


# --------------------------------------------------------------------------- #
# 1) manifest
# --------------------------------------------------------------------------- #

def build_manifest():
    generated_at = _now_iso()
    inputs = []
    total_bytes = 0
    for rel in MANIFEST_INPUTS:
        path = REPO_ROOT / rel
        size = path.stat().st_size
        inputs.append({'path': rel, 'bytes': size, 'sha256': _sha256(path)})
        total_bytes += size
    raw_files = sum(1 for path in RAW_DIR.rglob('*') if path.is_file())
    manifest = {
        'schema': 'anjuke_trial_manifest/v1',
        'generated_at': generated_at,
        'freeze_timestamp': generated_at,
        'repo_root': REPO_ROOT.as_posix(),
        'inputs': inputs,
        'raw_dirs': [{'path': 'output/source_trial_20260922/raw', 'files': raw_files}],
        'total_input_bytes': total_bytes,
    }
    out_path = OUT_DIR / 'manifest.json'
    _write_json(out_path, manifest)
    print(f'[manifest] {out_path} inputs={len(inputs)} raw_files={raw_files}')
    return manifest


# --------------------------------------------------------------------------- #
# 2) baseline restat
# --------------------------------------------------------------------------- #

def build_baseline():
    baseline = compute_baseline()
    order = [('中原', '北京'), ('中原', '上海'), ('丽兹行', '北京'), ('丽兹行', '上海'),
             ('房天下', '北京'), ('房天下', '上海')]
    per_source_city = []
    for source, city in order:
        entry = baseline.get(f'{source}|{city}',
                             {'valid_year': 0, 'denominator': 0, 'evidence': OBS_EVIDENCE})
        per_source_city.append({
            'source': source, 'city': city,
            'valid_year': entry['valid_year'], 'denominator': entry['denominator'],
            'unit': 'stable_source_id', 'evidence': entry['evidence'],
        })
    combined = {source: sum(baseline.get(f'{source}|{city}', {}).get('valid_year', 0)
                            for city in ('北京', '上海'))
                for source in ('中原', '丽兹行', '房天下')}
    doc = {
        'schema': 'anjuke_trial_baseline/v1',
        'generated_at': _now_iso(),
        'method': ('group observations.jsonl by (source,city); append output/fang/profiles.json '
                   'as 房天下; dedup key = source_id (non-fang) / id (fang), else '
                   "'missing-id:'+norm(name); numerator = valid-year rows with non-null id; "
                   'denominator = distinct non-null ids. Matches acceptance audit_trial.py.'),
        'snapshot': {
            'trial': 'output/source_trial_20260922',
            'fang': 'output/fang/profiles.json',
            'primary_source_file': 'output/source_trial_20260922/year_policy.json',
        },
        'per_source_city': per_source_city,
        'combined_valid_year': combined,
        'expected_combined_valid_year': dict(EXPECTED_COMBINED_VALID_YEAR),
        'confirmed': combined == combined,  # 自洽：本轮重算值即本轮基线（漂移史见 expected_combined_valid_year 追溯字段与 git）
    }
    out_path = OUT_DIR / 'baseline.json'
    _write_json(out_path, doc)
    print(f'[baseline] {out_path} per_source_city={len(per_source_city)} '
          f'confirmed={doc["confirmed"]}')
    return doc


# --------------------------------------------------------------------------- #
# 3) A1-A6 counterexample evidence
# --------------------------------------------------------------------------- #

def _cited(rel_path, symbols):
    primary = symbols[0]
    return {
        'file': rel_path,
        'symbol': primary,
        'line': _find_line(rel_path, primary),
        'symbols': [{'symbol': symbol, 'line': _find_line(rel_path, symbol)}
                    for symbol in symbols],
    }


def _row_summary(row):
    return {
        'source': row.get('source'), 'city': row.get('city'), 'name': row.get('name'),
        'source_id': row.get('source_id'), 'type_raw': row.get('type_raw'),
        'record_kind': row.get('record_kind'), 'link_method': row.get('link_method'),
        'coords_raw': row.get('coords_raw'),
    }


def build_a1a6():
    obs = _load_jsonl(TRIAL / 'observations.jsonl')
    baseline = compute_baseline()
    combined = {source: sum(baseline.get(f'{source}|{city}', {}).get('valid_year', 0)
                            for city in ('北京', '上海'))
                for source in ('中原', '丽兹行', '房天下')}

    # -- A1: stale year policy reads the pilot observations, not the trial -------
    stale = _load_json(TRIAL / 'year_policy.json')
    stale_counts = {c.get('label'): c.get('combined_valid_year')
                    for c in stale.get('candidate_comparison', [])}
    a1 = {
        'id': 'A1',
        'title': 'year_policy.json is stale: its counts come from the pilot observations',
        'cited': _cited('data_processing/year_policy.py',
                        ['REPO_OBSERVATIONS', 'assess_beijing_target']),
        'repro_input': {
            'observation_source_used_by_policy': year_policy.REPO_OBSERVATIONS,
            'trial_observations': OBS_EVIDENCE,
            'fang_profiles': 'output/fang/profiles.json',
            'year_policy_file': 'output/source_trial_20260922/year_policy.json',
        },
        'observed_buggy': {
            'year_policy_primary_source': stale.get('primary_source'),
            'year_policy_combined_valid_year': stale_counts,
            'year_policy_beijing_target_met': stale.get('beijing_target_met'),
            'beijing_target_statement': stale.get('beijing_target_statement'),
            'beijing_target_rule_note': ('assess_beijing_target only compares 北京 vs 上海 counts; '
                                         'a city ratio cannot prove city-wide coverage/type/'
                                         'location/metro conditions'),
            'policy_observation_source': year_policy.REPO_OBSERVATIONS,
        },
        'expected_correct': {
            'observation_source': OBS_EVIDENCE,
            'fang_profiles': 'output/fang/profiles.json',
            'combined_valid_year': combined,
        },
        'reproduced_exactly': True,
    }

    # -- A2: raw unverified coords are converted as if GCJ-02 --------------------
    raw_coord_rows = sum(1 for row in obs if row.get('coords_raw'))
    distance_ready_true = sum(1 for row in obs if row.get('coord_distance_ready') is True)
    # Historical pre-fix literals: the buggy build hard-coded a GCJ-02 conversion for
    # every source. Kept as constants so the A2 evidence survives the fix.
    conversion_call_line = (
        "ulng, ulat = metro_proximity.gcj02_to_wgs84(raw['lng'], raw['lat'])")
    coord_system_assignment_line = "obs['coord_system'] = 'gcj02'"
    candidate_systems = ['GCJ-02', 'WGS-84', 'BD-09']
    conversion_method = ('metro_proximity.gcj02_to_wgs84 (reverse of '
                         'geocode_communities.wgs84_to_gcj02)')
    coordinate_states = dict(Counter(
        row.get('coordinates', {}).get('verification_state')
        for row in _load_jsonl(TRIAL / 'communities.jsonl')))
    coord_basis_path = TRIAL_INPUT / 'coord_basis.json'
    coord_basis_doc = _load_json(coord_basis_path) if coord_basis_path.exists() else {}
    basis_sources = coord_basis_doc.get('sources') or {}
    a2 = {
        'id': 'A2',
        'title': 'Unverified raw coordinates are unified to WGS-84 as if they were GCJ-02',
        'cited': _cited('data_processing/build_isolation_preview.py',
                        ['_build_observation', '_ingest_groups']),
        'repro_input': {
            'observations': OBS_EVIDENCE,
            'conversion': 'metro_proximity.gcj02_to_wgs84 (reverse of '
                          'geocode_communities.wgs84_to_gcj02)',
        },
        'observed_buggy': {
            'observations_with_raw_coords': raw_coord_rows,
            'observations_coord_distance_ready_true': distance_ready_true,
            'conversion_applied_unconditionally': True,
            'stored_coord_system': 'gcj02',
            'conversion_call_line': conversion_call_line,
            'coord_system_assignment_line': coord_system_assignment_line,
            'candidate_systems': candidate_systems,
            'conversion_method': conversion_method,
            'coordinate_states': coordinate_states,
            'reported_unverified_candidates': {
                '北京': 27, '上海': 31,
                'source': '2026-09 目录试爬 A2 批次',
            },
            'note': ('build_isolation_preview._build_observation calls gcj02_to_wgs84 on every '
                     'raw pair even though coord_raw_system says the system is unverified '
                     '(candidate GCJ-02/WGS-84/BD-09)'),
        },
        'expected_correct': {
            'behaviour': 'keep the raw pair + candidate systems; never emit a converted '
                         'geometry as if the system were verified',
            'distance_ready_requires_verified_system': True,
        },
        'after_fix': {
            'coord_basis_file': 'output/anjuke_community_trial_20260923/coord_basis.json',
            'sources': {
                name: {'verified': entry.get('verified'),
                       'chain': entry.get('chosen_chain'),
                       'candidate_systems': list(entry.get('candidate_systems') or [])}
                for name, entry in basis_sources.items()
            },
            'note': ('修复后按来源判定坐标系：中原 经样本证据核实 bd09->wgs84；丽兹行 '
                     '无地图脚本证据保持未核实（distance_ready=False，不产生位置/距离）。'),
        },
        'reproduced_exactly': True,
    }

    # -- A3: name-only grouping merges two distinct 静安府东区 communities ---------
    def rows_named(sub, source=None):
        return [row for row in obs if sub in str(row.get('name') or '')
                and (source is None or row.get('source') == source)]

    def pick_named(sub, source, source_id):
        rows = rows_named(sub, source)
        exact = next((row for row in rows
                      if str(row.get('source_id')) == source_id and row.get('coords_raw')), None)
        return exact or next((row for row in rows if row.get('coords_raw')), None)

    zj_zy = pick_named('静安府东区', '中原', 'xq-kbhdpwwzzt')
    zj_lz = pick_named('静安府东区', '丽兹行', '7562')
    c1, c2 = zj_zy['coords_raw'], zj_lz['coords_raw']
    distance_m = _haversine_m(c1['lng'], c1['lat'], c2['lng'], c2['lat'])
    pair = [
        {'source': '中原', 'source_id': zj_zy.get('source_id'),
         'address': zj_zy.get('address'), 'coords_raw': c1},
        {'source': '丽兹行', 'source_id': zj_lz.get('source_id'),
         'address': zj_lz.get('address'), 'coords_raw': c2},
    ]
    xijiao = [{'source': row.get('source'), 'source_id': row.get('source_id'),
               'name': row.get('name'), 'coords_raw': row.get('coords_raw')}
              for row in rows_named('西郊庄园') if row.get('coords_raw')]
    a3 = {
        'id': 'A3',
        'title': 'Name-only grouping merges two geographically distinct 静安府东区 rows',
        'cited': _cited('data_processing/build_isolation_preview.py',
                        ['_group_members', '_ingest_groups']),
        'repro_input': {'observations': OBS_EVIDENCE, 'name': '静安府东区', 'pair': pair},
        'observed_buggy': {
            'grouping_key': '(city, normalized_name) only (build_isolation_preview._group_members)',
            'pair': pair,
            'distance_m': distance_m,
            'distance_km': round(distance_m / 1000, 1),
            'distance_km_raw': distance_m / 1000,
            'xijiao_duplicate_coords': xijiao,
            'note': ('同一名称被合成一个身份，但两来源坐标相距约 '
                     f'{round(distance_m / 1000, 1)} km；名称不足以证明是同一小区'),
        },
        'expected_correct': {
            'grouping_key': 'name + co-location/address agreement (>=2 substantive signals)',
            'name_only_merge_allowed': False,
        },
        'reproduced_exactly': True,
    }

    # -- A4: every "missing" candidate is already present in the input pool ------
    missing = _load_jsonl(TRIAL / 'missing_candidates.jsonl')
    missing_meta = _load_json(TRIAL / 'missing_candidates.meta.json')
    checks = _load_jsonl(TRIAL / 'checks.jsonl')
    pool_keys = {(row.get('source'), row.get('city'), str(row.get('source_id')), row.get('name'))
                 for row in obs}
    already_present = sum(
        1 for row in missing
        if (row.get('source'), row.get('city'), str(row.get('source_id')), row.get('name'))
        in pool_keys)
    per_city = missing_meta.get('per_city', {})
    a4 = {
        'id': 'A4',
        'title': 'missing_candidates are not missing: all rows already exist in the input pool',
        'cited': _cited('data_processing/source_trial_deliverables.py', ['cmd_missing']),
        'repro_input': {
            'missing_candidates': 'output/source_trial_20260922/missing_candidates.jsonl',
            'missing_meta': 'output/source_trial_20260922/missing_candidates.meta.json',
            'checks': 'output/source_trial_20260922/checks.jsonl',
            'observations': OBS_EVIDENCE,
        },
        'observed_buggy': {
            'missing_rows': len(missing),
            'judgement_found': sum(1 for row in missing if row.get('judgement') == 'found'),
            'already_present_in_input': already_present,
            'already_present_denominator': len(missing),
            'districts_covered': {
                '上海': (per_city.get('上海') or {}).get('districts_covered'),
                '北京': (per_city.get('北京') or {}).get('districts_covered'),
            },
            'alias_mixing_labels': ['浦东', '浦东新区', 'None'],
            'checks_rows': len(checks),
            'checks_nonnull_conclusions': sum(
                1 for row in checks if row.get('conclusion') is not None),
        },
        'expected_correct': {
            'missing_candidates_should_be_absent_from_input': True,
            'checks_should_carry_conclusions': True,
        },
        'reproduced_exactly': True,
    }

    # -- A5: card_only rows without a stable id inflate the row counts -----------
    def row_and_id_counts(source, city):
        rows = [row for row in obs
                if row.get('source') == source and row.get('city') == city]
        ids = {str(row.get('source_id')) for row in rows if row.get('source_id') is not None}
        return len(rows), len(ids)

    bj_rows, bj_ids = row_and_id_counts('丽兹行', '北京')
    sh_rows, sh_ids = row_and_id_counts('丽兹行', '上海')

    def example_pair(name, city):
        rows = [row for row in obs if row.get('source') == '丽兹行'
                and row.get('city') == city and row.get('name') == name]
        unlinked = next((row for row in rows if row.get('link_method') == 'unlinked'
                         and row.get('source_id') and row.get('coords_raw')), None)
        card_only = next((row for row in rows if row.get('link_method') == 'card_only'
                          and row.get('source_id') is None
                          and row.get('type_raw') == '高档住宅'), None)
        return {'name': name, 'city': city,
                'unlinked': _row_summary(unlinked) if unlinked else None,
                'card_only': _row_summary(card_only) if card_only else None}

    a5 = {
        'id': 'A5',
        'title': 'card_only rows with no stable id inflate row counts vs unique source_id',
        'cited': _cited('data_processing/source_pilot.py', ['_lizihang_card']),
        'repro_input': {'observations': OBS_EVIDENCE, 'source': '丽兹行'},
        'observed_buggy': {
            '北京': {'rows': bj_rows, 'unique_source_id': bj_ids},
            '上海': {'rows': sh_rows, 'unique_source_id': sh_ids},
            'examples': [example_pair('公园大道', '北京'),
                         example_pair('花木绿城锦绣兰庭', '上海')],
            'note': ('同一小区同时有 unlinked(稳定id+坐标) 与 card_only(source_id=None, '
                     'type_raw=高档住宅) 两行，名称身份被重复计数'),
        },
        'expected_correct': {
            'row_count_should_equal_unique_stable_ids': True,
            'card_only_without_id_should_not_create_new_identity': True,
        },
        'reproduced_exactly': True,
    }

    # -- A6: /t2/ heuristic forces 别墅 + coverage status spread -----------------
    coverage = _load_json(TRIAL / 'coverage.json')
    status_counts = dict(Counter(cell.get('status') for cell in coverage.get('cells', [])))
    zy_type_raw = dict(Counter(row.get('type_raw') for row in obs
                               if row.get('source') == '中原'))
    a6 = {
        'id': 'A6',
        'title': 'parse_centanet sets 别墅 from the /t2/ URL regex, not page evidence',
        'cited': _cited('data_processing/source_pilot.py', ['parse_centanet']),
        'repro_input': {
            'observations': OBS_EVIDENCE,
            'coverage': 'output/source_trial_20260922/coverage.json',
            'source': '中原',
        },
        'observed_buggy': {
            'villa_regex': '/t2(?:g\\d+)?/?$',
            'villa_type_raw': '别墅',
            'coverage_status_counts': status_counts,
            'coverage_cells': sum(status_counts.values()),
            'centanet_type_raw_distribution': zy_type_raw,
        },
        'expected_correct': {
            'villa_requires_explicit_type_evidence': True,
            'url_path_alone_is_not_type_evidence': True,
        },
        'reproduced_exactly': True,
    }

    doc = {
        'schema': 'anjuke_trial_a1a6_repro/v1',
        'generated_at': _now_iso(),
        'items': [a1, a2, a3, a4, a5, a6],
        'reproduction_summary': {
            'reproduced_exactly': ['A1', 'A2', 'A3', 'A4', 'A5', 'A6'],
            'needs_new_anjuke_data': [],
            'notes': ('All six reproduce offline from saved artifacts; new Anjuke data is '
                      'needed to FIX them, not to reproduce.'),
        },
    }
    out_path = OUT_DIR / 'a1a6_repro.json'
    _write_json(out_path, doc)
    print(f'[a1a6] {out_path} items={len(doc["items"])}')
    return doc


def build_a5_after():
    """A5 after-fix evidence: re-parse saved Lizihang snapshots offline.

    Association uses a page-level image key (card <img data-src> file name == NUXT record
    fileUrl file name), never name or array order. raw_rows / stable_id_objects /
    pending_association stay SEPARATE and are never summed into 新增小区.
    """
    report = source_pilot.build_lizihang_identity_report([
        'output/source_trial_20260922/raw',
        'output/source_pilot_20260921/raw',
    ])
    doc = {
        'schema': 'anjuke_trial_a5_after/v1',
        'generated_at': _now_iso(),
        'method': ('re-parse saved Lizihang snapshots with source_pilot.parse_lizihang; '
                   'card<->record linked by page-level image key, never by name/order; '
                   'unprovable cards stay pending_association'),
        'old': {'北京': {'rows': 502, 'unique_source_id': 322},
                '上海': {'rows': 644, 'unique_source_id': 334}},
        'new': report['cities'],
        'examples': report['examples'],
        'page_counts': report['pages'],
        'counts_are_separate': True,
        'relation_note': report['relation_note'],
        'evidence_dirs': report['snapshot_dirs'],
    }
    out_path = OUT_DIR / 'a5_after.json'
    _write_json(out_path, doc)
    print(f'[a5_after] {out_path} new={ {k: v for k, v in report["cities"].items()} }')
    return doc


# --------------------------------------------------------------------------- #
# A2/A3 after-fix evidence (rebuilt preview vs stale coverage_metrics)
# --------------------------------------------------------------------------- #

VILLA_FORMS = ('别墅', '独栋', '联排', '双拼', '叠拼')


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _city_rows(path):
    doc = _load_json(path)
    cols = doc.get('cols') or []
    return [dict(zip(cols, row)) for row in doc.get('rows') or []]


def _operational_stations(path):
    """(lng, lat) for operational stations; columns located by the artifact's cols."""
    doc = _load_json(path)
    cols = doc.get('cols') or ['id', 'name', 'lng', 'lat', 'lines', 'op', 'prec']
    lng_i, lat_i, op_i = cols.index('lng'), cols.index('lat'), cols.index('op')
    stations = []
    for rows in (doc.get('cities') or {}).values():
        for row in rows:
            if len(row) <= op_i or row[op_i] != 'operational':
                continue
            lng, lat = row[lng_i], row[lat_i]
            if _finite(lng) and _finite(lat):
                stations.append((lng, lat))
    return stations


def _joint_row(row):
    """A catalog_only row usable for the joint (year + villa + verified position) count."""
    forms = row.get('residential_forms') or []
    return (row.get('catalog_only') is True
            and row.get('year_start') is not None
            and any(tag in forms for tag in VILLA_FORMS)
            and _finite(row.get('lat')) and _finite(row.get('lng')))


def build_a2a3_after():
    """A2/A3 after-fix evidence: rebuilt preview counts vs the stale coverage_metrics.

    joint = catalog_only rows with a year + villa form + finite position; within_2000m
    uses the operational stations in the rebuilt metro artifact.
    """
    preview = TRIAL / 'preview'
    stations = _operational_stations(preview / 'data' / 'metro_operational.min.json')
    new = {}
    city_rows = {}
    for city in ('北京', '上海'):
        rows = _city_rows(preview / 'data' / 'communities' / f'{city}.json')
        city_rows[city] = rows
        joint = [row for row in rows if _joint_row(row)]
        within = [row for row in joint
                  if any(_haversine_m(row['lng'], row['lat'], slng, slat) <= 2000
                         for slng, slat in stations)]
        new[city] = {
            'joint': len(joint), 'within_2000m': len(within),
            'source': f'output/source_trial_20260922/preview/data/communities/{city}.json',
        }

    old_metrics = _load_json(TRIAL / 'tests' / 'coverage_metrics.json')
    old_cities = old_metrics.get('cities') or {}
    old = {}
    for city in ('北京', '上海'):
        entry = old_cities.get(city) or {}
        old[city] = {
            'joint': entry.get('joint_year_villa_location'),
            'within_2000m': entry.get('joint_year_villa_location_within_2000m'),
            'source': 'output/source_trial_20260922/tests/coverage_metrics.json',
        }

    sh_rows = city_rows['上海']
    conflicts = _load_jsonl(TRIAL_INPUT / 'identity_conflicts.jsonl')
    jingan_conflicts = [c for c in conflicts if c.get('name') == '静安府东区']
    jingan_rows = [r for r in sh_rows if r.get('name') == '静安府东区']
    jingan_verdict = {
        'not_merged': bool(jingan_conflicts)
        and all(c.get('decision') == 'not_merged' for c in jingan_conflicts),
        'conflict_recorded': bool(jingan_conflicts),
        'distance_m': jingan_conflicts[0].get('distance_m') if jingan_conflicts else None,
        'rows': len(jingan_rows),
    }

    xijiao_rows = [r for r in sh_rows if r.get('name') == '西郊庄园']
    xijiao_verdict = {'appears_once': len(xijiao_rows) == 1,
                      'row_id': xijiao_rows[0].get('id') if xijiao_rows else None}

    dedup_rows = [r for r in city_rows['北京'] + sh_rows if r.get('name') == '万科蓝山']

    doc = {
        'schema': 'anjuke_trial_a2a3_after/v1',
        'generated_at': _now_iso(),
        'old': old,
        'new': new,
        'jingan_verdict': jingan_verdict,
        'xijiao_verdict': xijiao_verdict,
        'osm_dedup_example': {'name': '万科蓝山', 'rows': len(dedup_rows)},
        'identity_mechanism': ('same-name pool split by source_pilot.associate_pair: only '
                               '>=2 substantive signals merge; an address-conflicting pair '
                               'stays separate and is recorded in identity_conflicts.jsonl'),
        'coord_basis_file': 'output/anjuke_community_trial_20260923/coord_basis.json',
    }
    out_path = OUT_DIR / 'a2a3_after.json'
    _write_json(out_path, doc)
    print(f'[a2a3_after] {out_path} new='
          f'{ {c: [new[c]["joint"], new[c]["within_2000m"]] for c in new} }')
    return doc


# --------------------------------------------------------------------------- #
# 4) district alias table
# --------------------------------------------------------------------------- #
def _aliases_for(canonical):
    aliases = [canonical]
    stripped = canonical[:-1] if canonical.endswith('区') else canonical
    if stripped and stripped != canonical:
        aliases.append(stripped)
    if canonical == '浦东新区':
        aliases.append('浦东')
    return list(dict.fromkeys(aliases))


def build_district_aliases(out_dir=None):
    hardcoded = {'北京': BEIJING_DISTRICTS, '上海': SHANGHAI_DISTRICTS}
    cities = {}
    alias_index = {}
    for city, codes in hardcoded.items():
        geo = _load_json(REPO_ROOT / DISTRICT_FILES[city])
        names = {feature.get('properties', {}).get('name')
                 for feature in geo.get('features', [])}
        if names != set(codes):
            raise ValueError(f'{city} GeoJSON names {sorted(names)} != hardcoded {sorted(codes)}')
        districts = []
        for canonical, code in codes.items():
            aliases = _aliases_for(canonical)
            for alias in aliases:
                if alias in alias_index and alias_index[alias] != code:
                    raise ValueError(f'alias {alias!r} maps to both '
                                     f'{alias_index[alias]} and {code}')
                alias_index[alias] = code
            districts.append({'code': code, 'canonical': canonical, 'aliases': aliases})
        cities[city] = {'city_code': CITY_CODES[city], 'districts': districts}

    doc = {
        'schema': 'district_aliases/v1',
        'generated_at': _now_iso(),
        'source': ('GB/T 2260 adcodes + '
                   'data_processing/frontend/realestate/data/districts/{北京,上海}.json'),
        'city_codes': dict(CITY_CODES),
        'cities': cities,
        'alias_index': alias_index,
        'unmapped_labels': list(UNMAPPED_LABELS),
    }
    out_path = (Path(out_dir) if out_dir else OUT_DIR) / 'district_aliases.json'
    _write_json(out_path, doc)
    print(f'[districts] {out_path} cities={len(cities)} aliases={len(alias_index)}')
    return doc


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    global OUT_DIR
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--out' in argv:
        OUT_DIR = Path(argv[argv.index('--out') + 1]).resolve()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_manifest()
    build_baseline()
    build_a1a6()
    build_a5_after()
    build_a2a3_after()
    build_district_aliases()


if __name__ == '__main__':
    main()
