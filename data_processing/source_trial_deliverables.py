# -*- coding: utf-8 -*-
"""§7.1 deliverable generator for the offline source trial (output/source_trial_20260922).

Every number here is program-generated from the real artifacts already on disk; nothing is
hand-typed. Runs fully offline and never touches production paths or the prior pilot dir.
Reuses the verified helpers (build_isolation_preview / year_policy / source_pilot) instead
of re-deriving grouping, year policy or identity logic.
"""
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import build_isolation_preview as bp
import community_attributes
import source_pilot
import year_policy

ROOT = Path(__file__).resolve().parent.parent
TRIAL = ROOT / 'output' / 'source_trial_20260922'
PREVIEW_METRO = TRIAL / 'preview' / 'data' / 'metro_operational.min.json'
SEED = 20260922

# Roles are descriptive provenance labels for the input manifest, not data.
MANIFEST_FILES = (
    ('data_processing/fang_config.json', 'fang config'),
    ('output/fang/profiles.json', 'fang primary-source profiles'),
    ('output/fang/status.json', 'fang run status'),
    ('output/fang/audit.json', 'fang audit'),
    ('output/fang/coverage.json', 'fang coverage'),
    ('output/catalog/catalog.sqlite3', 'catalog database'),
    ('output/catalog/directory_coverage.json', 'directory coverage ledger'),
    ('data_processing/fixtures/community_regression_samples.json', 'community regression samples'),
    ('output/source_pilot_20260921/observations.jsonl', 'prior pilot observations'),
    ('output/source_pilot_20260921/requests.jsonl', 'prior pilot requests'),
    ('output/source_pilot_20260921/checks.jsonl', 'prior pilot checks'),
    ('output/source_pilot_20260921/summary.json', 'prior pilot summary'),
    ('data_processing/frontend/realestate/data/metro_operational.min.json', 'operational metro station set'),
)
MANIFEST_RAW_DIRS = (
    ('output/source_pilot_20260921/raw', 'prior pilot snapshots'),
    ('output/source_trial_20260922/raw', 'trial snapshots'),
    ('output/data_source_research_20260921', 'data source research'),
)
PLANNED_STAGES = (
    ('入口与结构探测', 240),
    ('目录扩大试爬', 2800),
    ('字段与身份补全', 760),
    ('续跑与时效复查', 200),
)
COORD_NULL_FIELDS = ('raw', 'candidate_systems', 'conversion_method', 'unified_wgs84',
                     'verification_state', 'source', 'evidence_note')
CHECK_COORD_FIELDS = ('raw', 'candidate_systems', 'unified_wgs84', 'verification_state',
                      'evidence_path')
CITIES = ('北京', '上海')
METRO_RADIUS_M = 2000.0


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _rel(path):
    p = Path(path)
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        # Path outside ROOT (e.g. OS temp out_dir); keep absolute posix form
        return p.as_posix()


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    path.write_text(text, encoding='utf-8')


def _read_jsonl(path):
    """Rows from a jsonl artifact; a missing/empty file reads as no rows (unlike bp._read_jsonl)."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _read_first_line(path):
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                return line
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- #
# shared trial context (observations + identity groups + evidence)
# --------------------------------------------------------------------------- #

def _evidence_map(obs_rows):
    ev = {}
    for row in obs_rows:
        key = (row.get('source'), row.get('city'), str(row.get('source_id')))
        ev[key] = {
            'snapshot_path': row.get('snapshot_path'),
            'coord_snapshot_path': row.get('coord_snapshot_path'),
            'detail_snapshot_path': row.get('detail_snapshot_path'),
            'source_url': row.get('source_url'),
            'list_url': row.get('list_url'),
        }
    return ev


def _load_context(year_policy_path=None):
    obs_rows = bp._read_jsonl(TRIAL / 'observations.jsonl')
    fang_records = year_policy.load_candidate_records('fang', ROOT)
    groups, conflicts = bp._group_members(obs_rows, fang_records, return_conflicts=True)
    year_doc = bp._read_json(year_policy_path or (TRIAL / 'year_policy.json'), {}) or {}
    primary = year_doc.get('primary_source') or '房天下'
    return {'obs_rows': obs_rows, 'groups': groups, 'conflicts': conflicts, 'primary': primary,
            'ev': _evidence_map(obs_rows), 'year_doc': year_doc}


def _build_index(ctx):
    """Key by the full group key; also expose a (city, name) -> [(obs, meta)] lookup."""
    by_key = {}
    by_name = {}
    for key in sorted(ctx['groups']):
        obs, meta = bp._build_observation(key, ctx['groups'][key], ctx['primary'])
        by_key[key] = (obs, meta)
        name_key = (meta['city'], bp.catalog._normalize_name(meta['name']))
        by_name.setdefault(name_key, []).append((obs, meta))
    return {'by_key': by_key, 'by_name': by_name}


def _pick_hit(hits, source_id):
    """Prefer the same-name hit whose members carry the requested source_id."""
    if not hits:
        return None
    if source_id is not None:
        wanted = str(source_id)
        for obs, meta in hits:
            if any(str(m.get('source_id')) == wanted for m in (meta.get('members') or [])):
                return obs, meta
    return hits[0]


def _member_snapshot_paths(member, ev):
    row = ev.get((member.get('platform'), member.get('city'), str(member.get('source_id'))))
    paths = []
    if row:
        for field in ('snapshot_path', 'coord_snapshot_path', 'detail_snapshot_path'):
            if row.get(field):
                paths.append(row[field])
    return paths


def _member_evidence_paths(member, ev, primary):
    paths = list(_member_snapshot_paths(member, ev))
    if member.get('platform') == primary and member.get('source_url'):
        paths.append(member['source_url'])
    return paths


def _first_member_path(member, ev):
    paths = _member_snapshot_paths(member, ev)
    if paths:
        return paths[0]
    return member.get('source_url')


def _coord_evidence_path(source, members, ev):
    for member in members:
        if member.get('platform') == source:
            path = _first_member_path(member, ev)
            if path:
                return path
    return None


# --------------------------------------------------------------------------- #
# 1) manifest
# --------------------------------------------------------------------------- #

def cmd_manifest():
    year_doc = bp._read_json(TRIAL / 'year_policy.json', {}) or {}
    files = []
    for rel, role in MANIFEST_FILES:
        path = ROOT / rel
        if path.exists():
            files.append({'path': rel, 'bytes': path.stat().st_size,
                          'sha256': bp._sha256_file(path), 'role': role})
        else:
            files.append({'path': rel, 'present': False, 'reason': 'missing'})
    raw_dirs = []
    for rel, role in MANIFEST_RAW_DIRS:
        path = ROOT / rel
        if path.exists():
            count = sum(1 for p in path.rglob('*') if p.is_file())
            raw_dirs.append({'path': rel, 'files': count, 'role': role})
        else:
            raw_dirs.append({'path': rel, 'present': False, 'reason': 'missing', 'role': role})
    metro = bp._read_json(PREVIEW_METRO, {}) or {}
    manifest = {
        'schema': 'input_manifest/v1',
        'generated_at': _now_iso(),
        'trial_date': '20260922',
        'primary_source': year_doc.get('primary_source'),
        'files': files,
        'raw_dirs': raw_dirs,
        'station_set': {
            'version': (metro.get('versions') or {}).get('station_set'),
            'file': _rel(PREVIEW_METRO),
            'sha256': bp._sha256_file(PREVIEW_METRO),
            'note': metro.get('note'),
        },
    }
    bp._write_json(TRIAL / 'input_manifest.json', manifest)
    print(json.dumps({'command': 'manifest', 'files': len(files),
                      'raw_dirs': len(raw_dirs)}, ensure_ascii=False))
    return manifest


# --------------------------------------------------------------------------- #
# 2) run-config
# --------------------------------------------------------------------------- #

def _run_commands():
    run_dir = TRIAL / '_run'
    commands = []
    if run_dir.exists():
        for path in sorted(run_dir.glob('*.cmd')):
            commands.append({'file': path.name, 'content_first_line': _read_first_line(path)})
    return commands


def _source_scope():
    comparison = bp._read_json(TRIAL / 'source_comparison.json', {}) or {}
    candidates = comparison.get('candidates') or {}
    items = list(candidates.values()) if isinstance(candidates, dict) else list(candidates)
    scope = []
    for candidate in items:
        by_city = candidate.get('by_city') or {}
        scope.append({
            'candidate_id': candidate.get('candidate_id'),
            'label': candidate.get('label'),
            'combined_valid_year': candidate.get('combined_valid_year'),
            'per_city': {city: {'valid_year_total': by_city.get(city, {}).get('valid_year_total'),
                                'denominator': by_city.get(city, {}).get('denominator')}
                         for city in by_city},
        })
    return scope


def cmd_run_config():
    year_doc = bp._read_json(TRIAL / 'year_policy.json', {}) or {}
    budget = bp._read_json(TRIAL / 'run_state' / 'budget.json', {}) or {}
    stage_used = budget.get('stage_used') or {}
    planned = []
    for stage, cap in PLANNED_STAGES:
        used = stage_used.get(stage, 0)
        planned.append({'stage': stage, 'cap': cap, 'used': used, 'remaining': cap - used})
    config = {
        'schema': 'run_config/v1',
        'generated_at': _now_iso(),
        'environment': {
            'python': sys.executable,
            'python_version': sys.version.split()[0],
            'node': 'D:/nodejs/node.exe',
            'platform': sys.platform,
            'cwd': str(ROOT),
        },
        'trial_date': budget.get('trial_date') or '20260922',
        'execution_dates': sorted((budget.get('platform_daily_used') or {}).keys()),
        'primary_source': year_doc.get('primary_source'),
        'budget': {
            'actual': budget,
            'planned_allocation': planned,
            'planned_total_cap': sum(cap for _, cap in PLANNED_STAGES),
        },
        'commands': _run_commands(),
        'source_scope': _source_scope(),
        'pause_resume': {
            'checkpoint': 'output/source_trial_20260922/run_state/checkpoint.json',
            'resume_command': ('D:/python/python.exe data_processing/source_catalog_expand.py '
                               'run --resume --out output/source_trial_20260922 --max-requests N'),
            'platform_paused': budget.get('platform_paused'),
            'budget_exhausted': budget.get('budget_exhausted'),
            'note': 'offline round; no new network requests',
        },
        'network': {'offline_round': True,
                    'note': 'No network used for deliverable generation'},
    }
    bp._write_json(TRIAL / 'run_config.json', config)
    print(json.dumps({'command': 'run-config', 'commands': len(config['commands']),
                      'source_scope': len(config['source_scope'])}, ensure_ascii=False))
    return config


# --------------------------------------------------------------------------- #
# 3) communities
# --------------------------------------------------------------------------- #

def _community_row(obs, meta, primary, ev):
    coord = meta.get('coord')
    if coord is not None:
        coordinates = dict(coord)
    else:
        coordinates = {field: None for field in COORD_NULL_FIELDS}
        coordinates['coordinate_reason'] = '无可用源坐标'
    policy = meta.get('year_policy') or {}
    name = meta.get('name')
    paths = set()
    members = []
    for member in meta.get('members', []):
        paths.update(_member_evidence_paths(member, ev, primary))
        members.append({
            'platform': member.get('platform'),
            'source_id': member.get('source_id'),
            'name': member.get('name'),
            'address': member.get('address'),
            'year_raw': member.get('year_raw'),
            'coords_raw': member.get('coords_raw'),
            'evidence_path': _first_member_path(member, ev),
        })
    return {
        'schema': 'community_trial/v1',
        'city': meta.get('city'),
        'name': name,
        'district': meta.get('district'),
        'address': meta.get('address'),
        'source': meta.get('source'),
        'source_id': meta.get('source_id'),
        'platform_ids': dict(meta.get('platform_ids') or {}),
        'aliases': obs.get('aliases') or [],
        'parent_name': source_pilot.strip_phase(name),
        'phase': source_pilot.extract_phase(name),
        'phase_number': source_pilot.phase_number(name),
        'has_villa': meta.get('has_villa'),
        'villa_subtypes': meta.get('villa_subtypes') or [],
        'mixed_state': meta.get('mixed_state'),
        'forms': meta.get('forms') or [],
        'type_text': obs.get('type_text'),
        'coordinates': coordinates,
        'year': {
            'primary_source': policy.get('primary_source'),
            'policy_version': policy.get('policy_version'),
            'selected_source_id': policy.get('selected_source_id'),
            'year_raw': policy.get('year_raw'),
            'year_start': policy.get('year_start'),
            'year_end': policy.get('year_end'),
            'year_scope': policy.get('year_scope'),
            'selection_reason': policy.get('selection_reason'),
            'cross_source_difference': policy.get('cross_source_difference'),
        },
        'year_status': policy.get('status'),
        'evidence_paths': sorted(paths),
        'members': members,
    }


def _build_community_rows(ctx):
    rows = []
    for key in sorted(ctx['groups']):
        obs, meta = bp._build_observation(key, ctx['groups'][key], ctx['primary'])
        rows.append(_community_row(obs, meta, ctx['primary'], ctx['ev']))
    return rows


def cmd_communities():
    ctx = _load_context()
    rows = _build_community_rows(ctx)
    _write_jsonl(TRIAL / 'communities.jsonl', rows)
    print(json.dumps({'command': 'communities', 'rows': len(rows)}, ensure_ascii=False))
    return rows


# --------------------------------------------------------------------------- #
# 4) missing candidates
# --------------------------------------------------------------------------- #

def _missing_row(row, lookup):
    city = row.get('city')
    name = row.get('name')
    hits = lookup.get((city, bp.catalog._normalize_name(name))) or []
    hit = _pick_hit(hits, row.get('source_id'))
    if hit is None:
        return {
            'schema': 'missing_candidate/v1',
            'city': city, 'district': row.get('district'), 'name': name,
            'source': row.get('source'), 'source_id': row.get('source_id'),
            'found_in_trial_catalog': False,
            'matched_community_name': None,
            'primary_platform_year_supplied': None,
            'primary_year_reason': '未在实验目录发现',
            'location_supplied': None,
            'location_reason': '未在实验目录发现',
            'judgement': 'not_found',
        }
    _, meta = hit
    policy = meta.get('year_policy') or {}
    year_supplied = policy.get('status') == 'recorded'
    location_supplied = meta.get('coord') is not None
    return {
        'schema': 'missing_candidate/v1',
        'city': city, 'district': row.get('district'), 'name': name,
        'source': row.get('source'), 'source_id': row.get('source_id'),
        'found_in_trial_catalog': True,
        'matched_community_name': meta.get('name'),
        'primary_platform_year_supplied': year_supplied,
        'primary_year_reason': '实验目录已记录主平台年份' if year_supplied else '实验目录未记录主平台年份',
        'location_supplied': location_supplied,
        'location_reason': '实验目录已记录可用坐标' if location_supplied else '实验目录无可用坐标',
        'judgement': 'found',
    }


def _missing_sample(pool):
    by_district = {}
    for row in pool:
        by_district.setdefault((row.get('city'), row.get('district')), []).append(row)
    selected = []
    chosen = set()

    def row_key(row):
        return (row.get('source'), row.get('city'), str(row.get('source_id')), row.get('name'))

    for city, district in sorted(by_district, key=lambda k: (str(k[0]), str(k[1]))):
        rows = by_district[(city, district)]
        rng = random.Random(f'{SEED}{city}{district}')
        for row in rng.sample(rows, min(2, len(rows))):
            key = row_key(row)
            if key in chosen:
                continue
            chosen.add(key)
            selected.append(row)

    by_city = {}
    for row in pool:
        by_city.setdefault(row.get('city'), []).append(row)
    for city in sorted(by_city):
        current = sum(1 for row in selected if row.get('city') == city)
        if current >= 50:
            continue
        remaining = [row for row in by_city[city] if row_key(row) not in chosen]
        rng = random.Random(SEED)
        need = 50 - current
        for row in rng.sample(remaining, min(need, len(remaining))):
            key = row_key(row)
            if key in chosen:
                continue
            chosen.add(key)
            selected.append(row)
    return selected, by_district, by_city


DEFAULT_MISSING_SELECTION = ('冻结外部候选名单：每城目标>=50，各行政区尽量>=2；'
                             '仅取未参与建库的外部平台页面。')
MISSING_BLOCKED_REASON = '外部来源受阻（安居客 58 反爬网关）'
FROZEN_LIST_META = 'frozen_list_meta'


def _resolve_primary(out_dir):
    """Primary platform from out_dir/year_policy.json, else the trial year_policy."""
    for candidate in (Path(out_dir) / 'year_policy.json', TRIAL / 'year_policy.json'):
        primary = (bp._read_json(candidate, {}) or {}).get('primary_source')
        if primary:
            return primary
    return '房天下'


def _missing_per_city(rows):
    per_city = {}
    for row in rows:
        entry = per_city.setdefault(row.get('city'), {'found': 0, 'not_found': 0})
        entry[row.get('judgement')] = entry.get(row.get('judgement'), 0) + 1
    return per_city


def cmd_missing(out_dir=None, external_path=None, primary_source=None):
    """§3.3 omission check over a FROZEN EXTERNAL candidate pool.

    The candidate pool is the external jsonl (meta record excluded) ONLY; the trial
    observations are never used as the pool, so this cannot self-reference the catalog.
    """
    out_dir = Path(out_dir) if out_dir else TRIAL
    external_path = Path(external_path) if external_path else out_dir / 'external_candidates.jsonl'
    if primary_source is None:
        primary_source = _resolve_primary(out_dir)
    pool_rows = _read_jsonl(external_path)
    candidates = [row for row in pool_rows if row.get('record_type') != FROZEN_LIST_META]
    meta_record = next((row for row in pool_rows
                        if row.get('record_type') == FROZEN_LIST_META), {})
    selection_method = meta_record.get('selection_method') or DEFAULT_MISSING_SELECTION

    if not candidates:
        _write_jsonl(out_dir / 'missing_candidates.jsonl', [])
        meta = {
            'schema': 'missing_candidates_meta/v1',
            'generated_at': _now_iso(),
            'primary_source': primary_source,
            'status': 'blocked',
            'status_reason': MISSING_BLOCKED_REASON,
            'selection_method': selection_method,
            'per_city': {},
            'totals': {'found': 0, 'not_found': 0, 'blocked': True},
            'note': '未执行遗漏检验：无可用外部候选；不得报告“未找到 0”。',
            'external_pool_not_used_for_build': True,
        }
        bp._write_json(out_dir / 'missing_candidates.meta.json', meta)
        print(json.dumps({'command': 'missing', 'rows': 0, 'status': 'blocked'},
                         ensure_ascii=False))
        return []

    ctx = _load_context()
    lookup = _build_index(ctx)['by_name']
    rows = [_missing_row(row, lookup) for row in candidates]
    _write_jsonl(out_dir / 'missing_candidates.jsonl', rows)
    totals = {
        'found': sum(1 for row in rows if row['judgement'] == 'found'),
        'not_found': sum(1 for row in rows if row['judgement'] == 'not_found'),
        'blocked': False,
    }
    meta = {
        'schema': 'missing_candidates_meta/v1',
        'generated_at': _now_iso(),
        'primary_source': primary_source,
        'status': 'ok',
        'selection_method': selection_method,
        'per_city': _missing_per_city(rows),
        'totals': totals,
        'external_pool_not_used_for_build': True,
    }
    bp._write_json(out_dir / 'missing_candidates.meta.json', meta)
    print(json.dumps({'command': 'missing', 'rows': len(rows), 'totals': totals},
                     ensure_ascii=False))
    return rows


# --------------------------------------------------------------------------- #
# 4b) coverage (unified district codes + directory completeness)
# --------------------------------------------------------------------------- #

COVERAGE_SKIP_LABELS = ('全城', None, '', '其他')
COVERAGE_RESPONSE_STATUSES = ('成功', '截断', '阻断', '空目录', '预算未完成')


def _cell_pages_complete(cell):
    """A cell's directory is complete only when 成功 AND every declared page was read."""
    status = cell.get('status')
    declared = cell.get('declared_pages')
    read = cell.get('pages_read')
    truncated = bool(cell.get('truncated'))
    return (status == '成功' and read is not None and declared is not None
            and read >= declared and not truncated)


def cmd_coverage(out_dir=None, coverage_src=None, alias_path=None):
    """§6.1 city/行政区 coverage using the canonical district-code alias table.

    Raw labels (浦东/浦东新区) collapse to one code; a null label is never counted as a
    district. A directory 成功 response is reported separately from "all pages read".
    """
    out_dir = Path(out_dir) if out_dir else TRIAL
    coverage_src = Path(coverage_src) if coverage_src else TRIAL / 'coverage.json'
    alias_path = Path(alias_path) if alias_path else out_dir / 'district_aliases.json'
    coverage = bp._read_json(coverage_src, {}) or {}
    aliases = bp._read_json(alias_path, {}) or {}
    alias_index = aliases.get('alias_index') or {}
    code_canonical = {}
    city_districts = {}
    for city, entry in (aliases.get('cities') or {}).items():
        canonical = []
        for district in entry.get('districts') or []:
            code_canonical[district['code']] = district['canonical']
            canonical.append(district['canonical'])
        city_districts[city] = canonical

    cells = []
    observed_labels = {}
    covered_by_city = {}
    raw_counts_by_city = {}
    city_cell_counts = {}
    completeness = {'success': 0, 'truncated': 0, 'blocked': 0, 'budget_incomplete': 0,
                    'success_but_not_pages_complete': 0, 'pages_complete': 0,
                    'tested': 0, 'untested': 0, 'cells_total': 0}
    for cell in coverage.get('cells') or []:
        city = cell.get('city')
        label = cell.get('district')
        # Pre-unification raw district labels (浦东 vs 浦东新区 vs null) as written by the source.
        raw_label = '<null>' if label is None else label
        raw_bucket = raw_counts_by_city.setdefault(city, {})
        raw_bucket[raw_label] = raw_bucket.get(raw_label, 0) + 1
        code = alias_index.get(label) if label not in COVERAGE_SKIP_LABELS else None
        if code is not None:
            observed_labels[label] = code
            covered_by_city.setdefault(city, set()).add(code)
        enriched = dict(cell)
        enriched['district_code'] = code
        enriched['has_response'] = bool((cell.get('requests') or 0) > 0
                                        or cell.get('status') in COVERAGE_RESPONSE_STATUSES)
        enriched['directory_complete'] = _cell_pages_complete(cell)
        cells.append(enriched)
        has_response = enriched['has_response']
        completeness['cells_total'] += 1
        completeness['tested' if has_response else 'untested'] += 1
        cell_counter = city_cell_counts.setdefault(city, {'tested': 0, 'untested': 0, 'cells_total': 0})
        cell_counter['cells_total'] += 1
        cell_counter['tested' if has_response else 'untested'] += 1
        status = cell.get('status')
        if status == '成功':
            completeness['success'] += 1
            if _cell_pages_complete(cell):
                completeness['pages_complete'] += 1
            else:
                completeness['success_but_not_pages_complete'] += 1
        elif status == '截断':
            completeness['truncated'] += 1
        elif status == '阻断':
            completeness['blocked'] += 1
        elif status == '预算未完成':
            completeness['budget_incomplete'] += 1

    completeness['tested_denominator'] = completeness['cells_total']
    completeness['pages_complete_denominator'] = completeness['success']

    cities = {}
    alias_evidence = dict(observed_labels)
    for city, canonical in city_districts.items():
        codes = covered_by_city.get(city, set())
        covered = sorted(code_canonical[c] for c in codes if c in code_canonical)
        for code in codes:
            if code in code_canonical:
                alias_evidence.setdefault(code_canonical[code], code)
        missing = [name for name in canonical if name not in set(covered)]
        counter = city_cell_counts.get(city, {'tested': 0, 'untested': 0, 'cells_total': 0})
        cities[city] = {
            'covered': covered,
            'covered_count': len(covered),
            'missing': missing,
            'missing_count': len(missing),
            'tested': counter['tested'],
            'untested': counter['untested'],
            'cells_total': counter['cells_total'],
        }

    doc = {
        'schema': 'anjuke_trial_coverage/v1',
        'generated_at': _now_iso(),
        'source_coverage': _rel(coverage_src),
        'alias_path': _rel(alias_path),
        'null_not_counted': True,
        'alias_collapse_evidence': alias_evidence,
        'raw_label_counts': raw_counts_by_city,
        'raw_distinct_count': {city: len(counts) for city, counts in raw_counts_by_city.items()},
        'cities': cities,
        'cells': cells,
        'summary': dict(coverage.get('summary') or {}),
        'directory_completeness': completeness,
        'validation_scope': {
            'scope': 'external_small_sample_discovery_validation',
            'statement': '本轮只作外部小样发现验证：已测/未测/截断分别统计，不能据此推断全城完整性。',
            'full_city_goal': 'UNVERIFIED',
            'full_city_statement': '全城目标尚未验收；目录行政标签覆盖不等于读完目录。',
            'tested_definition': '已测=有响应的目录单元(has_response=True)；未测=无请求且无响应；截断=响应成功但未读完全部声明分页。',
            'denominators': {'tested': completeness['cells_total'],
                             'pages_complete': completeness['success']},
        },
    }
    bp._write_json(out_dir / 'coverage.json', doc)
    print(json.dumps({'command': 'coverage', 'cities': {c: v['covered_count']
                      for c, v in cities.items()}, 'completeness': completeness},
                     ensure_ascii=False))
    return doc


# --------------------------------------------------------------------------- #
# 5) checks
# --------------------------------------------------------------------------- #

CHECKS_CONCLUSIONS = ('忠实提取', '提取不符', '不可核（无快照）')
CHECKS_NO_SNAPSHOT = '不可核（无快照）'


def _norm_text(value):
    return re.sub(r'[\s·・]', '', str(value or ''))


def _fang_record_index(fang_profiles):
    doc = bp._read_json(fang_profiles, {}) or {}
    index = {}
    for record in doc.get('records', []):
        if record.get('id'):
            index[record['id']] = record
    return index


def _check_tokens(doc):
    return [s.strip() for s in doc.stripped_strings if s.strip()]


def _first_heading(doc):
    for selector in ('h1', 'h2', 'h3'):
        node = doc.select_one(selector)
        if node and node.get_text(' ', strip=True):
            return node.get_text(' ', strip=True)
    return ''


def _label_value(tokens, label):
    """Value following a field label; the label and value may share one string."""
    for i, token in enumerate(tokens):
        if label in token:
            tail = token.split(label, 1)[1].strip().lstrip('：:').strip()
            if tail:
                return tail
            return tokens[i + 1] if i + 1 < len(tokens) else ''
    return ''


def _check_row_result(row, record):
    """Independent fidelity check of one primary-platform row against its saved snapshot.

    Verifies the extracted values against the WHOLE raw page text. The prior label-adjacency
    read the wrong token (e.g. a type value 楼房 after 建筑年代) and a generic doc.title, so
    name/year/type failed even when the page really carried the extracted values.
    """
    source_url = record.get('source_url') or record.get('evidence_url') or ''
    raw = (record.get('raw') or {}).get('path')
    if not raw or not Path(raw).exists():
        return CHECKS_NO_SNAPSHOT, '无快照：无法逐条核验', raw
    try:
        html = Path(raw).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return CHECKS_NO_SNAPSHOT, '快照不可读', raw
    doc = BeautifulSoup(html, 'html.parser')
    for node in doc(['script', 'style']):
        node.decompose()
    for node in doc.select('[hidden], [style*="display:none"], [style*="display: none"]'):
        node.decompose()
    text = _norm_text(doc.get_text(' ', strip=True))

    ok_name = bool(record.get('name')) and _norm_text(record['name']) in text

    rec_year = (record.get('year') or {}).get('start')
    ok_year = rec_year is not None and (f'{rec_year}年' in text or str(rec_year) in text)

    types = record.get('housing_types') or []
    ok_type = all(_norm_text(t) in text for t in types) if types else True

    numeric = str(record.get('id') or '').rsplit(':', 1)[-1]
    ok_id = bool(numeric) and f'/loupan/{numeric}' in source_url

    reason = '名称{0};年份{1};类型{2};身份{3}'.format(
        '一致' if ok_name else '不符', '一致' if ok_year else '不符',
        '一致' if ok_type else '不符', '一致' if ok_id else '不符')
    conclusion = '忠实提取' if (ok_name and ok_year and ok_type and ok_id) else '提取不符'
    return conclusion, reason, raw


def cmd_checks(out_dir=None, sample_path=None, fang_profiles=None):
    """§5 A4: give every inherited 200-row check a real, non-null conclusion.

    Reads the inherited checks sample and the primary platform's saved snapshots; a row
    with no snapshot is explicitly 不可核（无快照）, never a silent null.
    """
    out_dir = Path(out_dir) if out_dir else TRIAL
    sample_path = Path(sample_path) if sample_path else TRIAL / 'checks.jsonl'
    fang_profiles = Path(fang_profiles) if fang_profiles else Path('output/fang/profiles.json')
    if not fang_profiles.is_absolute():
        fang_profiles = ROOT / fang_profiles
    index = _fang_record_index(fang_profiles)

    rows = []
    distribution = Counter()
    checked_at = _now_iso()
    for sample in _read_jsonl(sample_path):
        record = index.get(sample.get('community_id'))
        if record is None:
            platform_ids = (sample.get('identity') or {}).get('platform_ids') or {}
            record = next((index[v] for v in platform_ids.values() if v in index), None)
        if record is None:
            conclusion, reason, evidence_path = (
                CHECKS_NO_SNAPSHOT, '无快照：未匹配到主平台记录', None)
        else:
            conclusion, reason, evidence_path = _check_row_result(sample, record)
        row = dict(sample)
        row['conclusion'] = conclusion
        row['conclusion_reason'] = reason
        row['checked_at'] = checked_at
        row['evidence_path'] = evidence_path
        rows.append(row)
        distribution[conclusion] += 1

    _write_jsonl(out_dir / 'checks.jsonl', rows)
    meta = {
        'schema': 'checks_review_meta/v1',
        'generated_at': checked_at,
        'total': len(rows),
        'distribution': dict(distribution),
        'allowed_conclusions': list(CHECKS_CONCLUSIONS),
        'sample_source': _rel(sample_path),
        'fang_profiles': _rel(fang_profiles),
    }
    bp._write_json(out_dir / 'checks_review.meta.json', meta)
    print(json.dumps({'command': 'checks', 'rows': len(rows),
                      'distribution': dict(distribution)}, ensure_ascii=False))
    return rows


def cmd_primary_checks(out_dir=None, per_city=100):
    """Best-effort primary-platform fidelity review: re-parse snapshots and compare rows.

    The new primary (中原) is taken from source_comparison ranking[0]. Every distinct snapshot
    is re-parsed ONCE, then each sampled row is compared against the re-parsed row of the same
    source_id. Fully guarded: a failure is recorded in checks_review.meta.json, never raised.
    """
    out_dir = Path(out_dir) if out_dir else TRIAL
    meta_path = out_dir / 'checks_review.meta.json'
    try:
        comparison = bp._read_json(out_dir / 'source_comparison.json', {}) or {}
        ranking = comparison.get('ranking') or []
        primary = (ranking[0].get('label') if ranking else None) or '房天下'
        rows = [r for r in _read_jsonl(TRIAL / 'observations.jsonl')
                if r.get('source') == primary]
        rows = source_pilot.dedup_observations(rows)
        rows = [r for r in rows if r.get('source_id') and r.get('snapshot_path')
                and Path(r['snapshot_path']).exists()]

        by_city = {}
        for row in rows:
            by_city.setdefault(row.get('city'), []).append(row)
        sample = []
        for city in sorted(by_city, key=lambda value: str(value)):
            city_rows = sorted(by_city[city], key=lambda r: str(r.get('source_id')))
            sample.extend(city_rows[:per_city])

        reparsed = {}
        for path in sorted({r['snapshot_path'] for r in sample}):
            try:
                html = Path(path).read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            seed = next(r for r in sample if r['snapshot_path'] == path)
            parsed = source_pilot.parse_centanet(
                html, seed.get('list_url'), seed.get('city'), seed.get('source'),
                seed.get('fetched_at'), seed.get('origin'), path)
            reparsed[path] = {p.get('source_id'): p for p in parsed if p.get('source_id')}

        distribution = Counter()
        review_rows = []
        for row in sample:
            match = (reparsed.get(row['snapshot_path']) or {}).get(row.get('source_id'))
            if match is None:
                conclusion = '提取不符'
            else:
                same_name = _norm_text(match.get('name')) == _norm_text(row.get('name'))
                same_year = match.get('year_value') == row.get('year_value')
                same_id = match.get('source_id') == row.get('source_id')
                conclusion = '忠实提取' if (same_name and same_year and same_id) else '提取不符'
            review_rows.append({
                'city': row.get('city'),
                'source': primary,
                'source_id': row.get('source_id'),
                'snapshot_path': row.get('snapshot_path'),
                'conclusion': conclusion,
            })
            distribution[conclusion] += 1

        _write_jsonl(out_dir / 'checks_primary.jsonl', review_rows)
        meta = bp._read_json(meta_path, {}) or {}
        meta['primary_platform_review'] = {
            'source': primary,
            'total': len(review_rows),
            'distribution': dict(distribution),
            'sample_source': _rel(TRIAL / 'observations.jsonl'),
        }
        bp._write_json(meta_path, meta)
        print(json.dumps({'command': 'primary-checks', 'source': primary,
                          'total': len(review_rows), 'distribution': dict(distribution)},
                         ensure_ascii=False))
        return review_rows
    except Exception as exc:  # offline best effort; never fail the A4 deliverable
        meta = bp._read_json(meta_path, {}) or {}
        meta['primary_platform_review'] = {'status': 'error', 'reason': str(exc)}
        bp._write_json(meta_path, meta)
        print(json.dumps({'command': 'primary-checks', 'status': 'error',
                          'reason': str(exc)}, ensure_ascii=False))
        return []


# --------------------------------------------------------------------------- #
# 6) metrics
# --------------------------------------------------------------------------- #

def _haversine(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _load_operational_stations():
    doc = bp._read_json(PREVIEW_METRO, {}) or {}
    cols = doc.get('cols') or []
    pos = {name: i for i, name in enumerate(cols)}
    stations = []
    for rows in (doc.get('cities') or {}).values():
        for row in rows:
            op = row[pos['op']] if 'op' in pos and pos['op'] < len(row) else None
            if op != 'operational':
                continue
            lng = row[pos['lng']] if 'lng' in pos and pos['lng'] < len(row) else None
            lat = row[pos['lat']] if 'lat' in pos and pos['lat'] < len(row) else None
            if lng is None or lat is None:
                continue
            stations.append((lng, lat))
    return (doc.get('versions') or {}).get('station_set'), stations


def _within_2000(unified, stations):
    if not unified:
        return False
    lng, lat = unified.get('lng'), unified.get('lat')
    if lng is None or lat is None:
        return False
    for station_lng, station_lat in stations:
        if _haversine(lat, lng, station_lat, station_lng) <= METRO_RADIUS_M:
            return True
    return False


def _city_metrics(rows, stations):
    archives = len(rows)
    parents = len({row['parent_name'] for row in rows})
    phases = sum(1 for row in rows if row.get('phase'))
    year_exact = sum(1 for row in rows
                     if row.get('year_status') == 'recorded'
                     and row['year']['year_start'] is not None
                     and row['year']['year_start'] == row['year']['year_end'])
    year_range = sum(1 for row in rows
                     if row.get('year_status') == 'recorded'
                     and row['year']['year_start'] is not None
                     and row['year']['year_start'] != row['year']['year_end'])
    year_missing = sum(1 for row in rows if row.get('year_status') == 'unknown')
    year_conflict = sum(1 for row in rows if row.get('year_status') == 'conflict')
    year_pending_other = archives - year_exact - year_range - year_missing - year_conflict
    type_villa = sum(1 for row in rows if row.get('has_villa'))
    type_non_villa = sum(1 for row in rows if row.get('mixed_state') == 'non_villa')
    type_name_hint = sum(1 for row in rows
                         if '别墅' in (row.get('name') or '') and not row.get('has_villa'))
    mixed = sum(1 for row in rows if row.get('mixed_state') == 'mixed')
    pure_villa = sum(1 for row in rows if row.get('mixed_state') == 'pure_villa')
    location_raw_coord = sum(1 for row in rows
                             if (row.get('coordinates') or {}).get('raw') is not None)
    location_distance_ready = sum(1 for row in rows
                                  if (row.get('coordinates') or {}).get('unified_wgs84') is not None)
    location_system_verified = sum(1 for row in rows
                                   if (row.get('coordinates') or {}).get('verified') is True)
    joint = [row for row in rows
             if row.get('year_status') == 'recorded' and row.get('has_villa')
             and (row.get('coordinates') or {}).get('raw') is not None]
    joint_within = sum(1 for row in joint
                       if _within_2000((row.get('coordinates') or {}).get('unified_wgs84'),
                                       stations))
    counter_fn = getattr(source_pilot, 'type_evidence_counters', None)
    counters = counter_fn(rows) if counter_fn else {}
    return {
        'archives': archives,
        'parents': parents,
        'phases': phases,
        'year_exact': year_exact,
        'year_range': year_range,
        'year_missing': year_missing,
        'year_conflict': year_conflict,
        'year_pending_other': year_pending_other,
        'type_villa': type_villa,
        'type_non_villa': type_non_villa,
        'type_name_hint': type_name_hint,
        'mixed': mixed,
        'pure_villa': pure_villa,
        'location_raw_coord': location_raw_coord,
        'location_system_verified': location_system_verified,
        'location_system_verified_reason': ('按 coord_basis 已核实来源计数'
                                            if location_system_verified > 0 else '坐标系未核实'),
        'location_distance_ready': location_distance_ready,
        'joint_year_villa_location': len(joint),
        'joint_year_villa_location_within_2000m': joint_within,
        # A6: evidence-based type counters (kept separate from the legacy mixed_state keys).
        'type_explicit_villa': counters.get('明确含别墅', 0),
        'type_explicit_nonvilla': counters.get('明确不含', 0),
        'type_unknown': counters.get('未知', 0),
        'type_clue': counters.get('仅线索', 0),
        'type_clue_basis': counters.get('type_clue_basis') or counters.get('仅线索依据') or '',
        'type_counter_note': ('中原 /t2/ 目录仅作“含别墅候选”依据，不等于纯别墅，'
                              '也不证明普通目录不含别墅。'),
    }


def _total_metrics(rows, stations):
    totals = _city_metrics(rows, stations)
    totals['parents'] = len({row['parent_name'] for row in rows})
    return totals


def _source_metrics(obs_rows):
    sources = {}
    for row in obs_rows:
        key = f"{row.get('source')}|{row.get('city')}"
        entry = sources.setdefault(key, {
            'source': row.get('source'), 'city': row.get('city'),
            'observations': 0, 'unique_source_ids': set(), 'villa': 0, 'year_non_null': 0})
        entry['observations'] += 1
        if row.get('source_id') is not None:
            entry['unique_source_ids'].add(str(row.get('source_id')))
        if row.get('has_villa'):
            entry['villa'] += 1
        if row.get('year_value') is not None:
            entry['year_non_null'] += 1
    for entry in sources.values():
        entry['unique_source_ids'] = len(entry['unique_source_ids'])
    return dict(sorted(sources.items()))


def _directory_metrics():
    coverage = bp._read_json(TRIAL / 'coverage.json', {}) or {}
    cells = []
    for cell in coverage.get('cells') or []:
        declared = cell.get('declared_pages')
        if declared is None:
            continue
        cells.append({'source': cell.get('source'), 'city': cell.get('city'),
                      'scope': cell.get('catalog_kind'), 'page_declared_count': declared})
    return {'summary': coverage.get('summary'),
            'summary_by_stage': coverage.get('summary_by_stage'),
            'cells': cells}


def _quality_cost(requests, city_metrics, requests_by_city):
    failure_classes = {}
    for row in requests:
        failure = row.get('failure_class')
        if failure:
            failure_classes[failure] = failure_classes.get(failure, 0) + 1
    replay = bp._read_json(TRIAL / 'replay_report.json', {}) or {}
    numerator = replay.get('numerator')
    try:
        numerator = int(numerator) if numerator is not None else None
    except (TypeError, ValueError):
        numerator = None
    per_100 = {}
    for city in CITIES:
        joint = city_metrics.get(city, {}).get('joint_year_villa_location', 0)
        total = requests_by_city.get(city, 0)
        per_100[city] = None if not total else round(joint / total * 100, 4)
    return {
        'requests_total': len(requests),
        'failure_classes': failure_classes,
        'replayable': {'numerator': numerator,
                       'denominator': replay.get('denominator'),
                       'ratio': replay.get('ratio')},
        'per_100_requests_new_joint_usable': per_100,
    }


def cmd_metrics(out_dir=None):
    out_dir = Path(out_dir) if out_dir else TRIAL
    ctx = _load_context(out_dir / 'year_policy.json')
    rows = _build_community_rows(ctx)
    station_version, stations = _load_operational_stations()
    requests = bp._read_jsonl(TRIAL / 'requests.jsonl')
    requests_by_city = {}
    for row in requests:
        requests_by_city[row.get('city')] = requests_by_city.get(row.get('city'), 0) + 1

    city_metrics = {city: _city_metrics([r for r in rows if r.get('city') == city], stations)
                    for city in CITIES}
    totals = _total_metrics(rows, stations)
    scale_target = {}
    for city in CITIES:
        metrics = city_metrics[city]
        scale_target[city] = {
            'archives': metrics['archives'],
            'villa_candidates': metrics['type_villa'],
            'meets_archives_1000': metrics['archives'] >= 1000,
            'meets_villa_200': metrics['type_villa'] >= 200,
        }
    notes = [
        f'地铁站集版本: {station_version}',
        '坐标系未核实: 原始坐标候选 GCJ-02/WGS-84/BD-09, 不计为系统已核实',
        'joint_year_villa_location_within_2000m 仅用 operational 站点, 直线距离(WGS84)<=2000m',
    ]
    metrics = {
        'schema': 'trial_coverage_metrics/v1',
        'generated_at': _now_iso(),
        'primary_source': ctx['primary'],
        'policy_version': ctx['year_doc'].get('policy_version'),
        'cities': city_metrics,
        'totals': totals,
        'sources': _source_metrics(ctx['obs_rows']),
        'directory': _directory_metrics(),
        'quality_cost': _quality_cost(requests, city_metrics, requests_by_city),
        'scale_target': scale_target,
        'station_set': station_version,
        'notes': notes,
    }
    out_path = out_dir / 'tests' / 'coverage_metrics.json'
    bp._write_json(out_path, metrics)
    print(json.dumps(metrics, ensure_ascii=False))
    return metrics


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def _configure_stdout():
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


_DISPATCH = {
    'manifest': cmd_manifest,
    'run-config': cmd_run_config,
    'communities': cmd_communities,
    'missing': cmd_missing,
    'checks': cmd_checks,
    'checks-review': cmd_checks,
    'coverage': cmd_coverage,
    'metrics': cmd_metrics,
}
ALL_ORDER = ('manifest', 'run-config', 'communities', 'missing', 'checks', 'metrics')
_OPTION_KEYS = {
    '--out': 'out',
    '--external': 'external',
    '--coverage-src': 'coverage_src',
    '--alias-path': 'alias_path',
    '--sample': 'sample',
    '--fang-profiles': 'fang_profiles',
    '--primary-source': 'primary_source',
}


def _parse_opts(argv):
    opts = {}
    i = 0
    while i < len(argv):
        key = _OPTION_KEYS.get(argv[i])
        if key and i + 1 < len(argv):
            opts[key] = argv[i + 1]
            i += 2
            continue
        i += 1
    return opts


def main(argv=None):
    _configure_stdout()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        print('usage: source_trial_deliverables.py <manifest|run-config|communities|'
              'missing|checks|checks-review|coverage|metrics|all> [--out DIR]')
        return 2
    command = argv[0]
    opts = _parse_opts(argv[1:])
    if command == 'all':
        for name in ALL_ORDER:
            _DISPATCH[name]()
    elif command == 'missing':
        cmd_missing(out_dir=opts.get('out'), external_path=opts.get('external'),
                    primary_source=opts.get('primary_source'))
    elif command in ('checks', 'checks-review'):
        cmd_checks(out_dir=opts.get('out'), sample_path=opts.get('sample'),
                   fang_profiles=opts.get('fang_profiles'))
    elif command == 'coverage':
        cmd_coverage(out_dir=opts.get('out'), coverage_src=opts.get('coverage_src'),
                     alias_path=opts.get('alias_path'))
    elif command in _DISPATCH:
        _DISPATCH[command]()
    else:
        print(f'unknown command: {command}')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
