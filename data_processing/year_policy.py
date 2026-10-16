"""§2 统一年份主平台 selection + §2.2 年份筛选规则 (offline; no network).

Rule: pick ONE unified year
primary platform by combined 北京+上海 valid-year community count, then emit
source_comparison.json / year_policy.json / checks_sample.jsonl. Reuses
source_pilot / fang_store / judgement; never modifies them. Offline only.
"""
import argparse
import json
import random
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import fang_store
import judgement
import source_pilot
from source_pilot import (dedup_observations, extract_phase, fetch_status,
                          now_iso, observation_key, parent_community_count,
                          phase_number, read_jsonl, strip_phase, write_jsonl)

# A1: this layer's own version; fang_store keeps its 'year-primary-1' publish-year policy.
YEAR_POLICY_VERSION = 'year-primary-2'
YEAR_POLICY_FIELDS = fang_store.YEAR_POLICY_FIELDS

TARGET_CITIES = ('北京', '上海')
# §2.2 rule 4: only an explicit community-year field outranks promotional body text.
EXPLICIT_YEAR_FIELDS = frozenset({'小区目录建成年份', '小区建筑年代', '建筑年代',
                                  '建成年代', '小区年代'})
# §2.1: these may never influence ranking.
FORBIDDEN_CRITERIA = ('官网广告数量', '网页字节数', '价格记录数', '请求次数', '非空字符串数')
TIE_BREAK_ORDER = ('两城覆盖均衡', '区县覆盖广', '获取稳定性')
WHOLE_COMMUNITY_SCOPES = ('community', '小区', 'community_or_phase', '')
VALID_YEAR_CATEGORIES = ('exact_year', 'decade_only', 'range')
BOUNDARY_KINDS = ('年代范围', '混合小区', '同名分期', '缺失值', '异常年份')
# Completion semantics only; these inputs carry no 拿地/开盘/预售/文章日期.
VALID_YEAR_MEANINGS = (None, '', 'completion')
YEAR_1950 = 1949

DEFAULT_OUT_DIR = 'output/source_trial_20260922'
REPO_FANG_PROFILES = 'output/fang/profiles.json'
REPO_FANG_STATUS = 'output/fang/status.json'
REPO_FANG_COVERAGE = 'output/fang/coverage.json'
# A1: compare the current trial round, not the stale pilot observations.
REPO_OBSERVATIONS = 'output/source_trial_20260922/observations.jsonl'
REPO_REQUESTS = 'output/source_trial_20260922/requests.jsonl'
REPO_EMBEDDED_COUNTS = 'output/source_trial_20260922/embedded_counts.json'

CANDIDATES = {
    'fang': {
        'label': '房天下',
        'inputs': {'profiles': REPO_FANG_PROFILES, 'status': REPO_FANG_STATUS,
                   'coverage': REPO_FANG_COVERAGE},
    },
    'lizihang': {
        'label': '丽兹行',
        'inputs': {'observations': REPO_OBSERVATIONS, 'requests': REPO_REQUESTS,
                   'embedded_counts': REPO_EMBEDDED_COUNTS},
    },
    'centanet': {
        'label': '中原',
        'inputs': {'observations': REPO_OBSERVATIONS, 'requests': REPO_REQUESTS,
                   'embedded_counts': REPO_EMBEDDED_COUNTS},
    },
}

# 丽兹行 buildYear 与 中原 建造年代 都是结构化的小区年代字段(非宣传正文).
OBS_YEAR_FIELD = {'丽兹行': '小区建筑年代', '中原': '建筑年代'}

SELECTION_FIELD_MEANINGS = {
    'primary_source': '选定的统一年份主平台名称',
    'policy_version': '年份策略规则版本',
    'selected_source_id': '主平台内该小区/分期的来源身份',
    'year_raw': '主平台观测到的年份原始值(start/end/precision/scope)',
    'year_start': '年份范围起始(单年时等于 year_end)',
    'year_end': '年份范围结束',
    'year_scope': '年份适用范围(小区/分期/多期)',
    'selection_reason': '为何采用该平台年份(跨来源差异不阻断的说明)',
    'cross_source_difference': '其他平台的不同年份,仅作差异提示',
}


# ---------------------------------------------------------------------------
# io helpers
# ---------------------------------------------------------------------------
def _read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def _json_write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def _first_present(*values):
    for value in values:
        if value not in (None, '', [], {}):
            return value
    return None


# ---------------------------------------------------------------------------
# §2.1 record normalization / candidate loading
# ---------------------------------------------------------------------------
def _norm_year_value(raw):
    """Canonical year fact dict consumed by fang_store.select_publish_year."""
    raw = raw if isinstance(raw, dict) else {}
    return {'start': raw.get('start'), 'end': raw.get('end'),
            'precision': raw.get('precision'), 'scope': raw.get('scope'),
            'meaning': raw.get('meaning'), 'label': raw.get('label')}


def _type_fields(tags, pure_villa=False):
    tags = list(tags or [])
    has_villa = bool(pure_villa) or source_pilot.has_villa_type(tags)
    has_nonvilla = source_pilot.has_nonvilla_type(tags)
    return {'type_tags': tags, 'has_villa': has_villa,
            'has_nonvilla': has_nonvilla, 'mixed': has_villa and has_nonvilla}


def _fang_record(record):
    year = record.get('year') if isinstance(record.get('year'), dict) else None
    field_evidence = record.get('field_evidence') or {}
    year_evidence = field_evidence.get('year') if isinstance(
        field_evidence.get('year'), dict) else {}
    raw = year_evidence.get('raw') if isinstance(year_evidence.get('raw'), dict) else {}
    year_raw = _norm_year_value(year) if year else None
    tags = record.get('housing_types') or record.get('residential_forms') or []
    base = {
        'platform': '房天下',
        'city': record.get('city'),
        'district': record.get('district'),
        # A3: keep 地址 for real identity matching (门牌/house signal), not just the name.
        'address': record.get('address'),
        'name': record.get('name'),
        'phase': extract_phase(record.get('name')),
        'identity': record.get('id'),
        'has_stable_id': bool(record.get('id')),
        'year_raw': year_raw,
        'year_field': year.get('field') if year else None,
        'year_precision': year.get('precision') if year else None,
        'evidence': {
            'source_url': _first_present(year.get('source_url') if year else None,
                                         record.get('evidence_url'), record.get('source_url')),
            'snapshot_path': raw.get('path'),
        },
    }
    base.update(_type_fields(tags, record.get('pure_villa')))
    return base


def _observation_record(row):
    source = row.get('source')
    year_value = row.get('year_value')
    if year_value is not None:
        year_raw = {'start': year_value, 'end': year_value, 'precision': 'year',
                    'scope': '小区', 'meaning': 'completion', 'label': row.get('year_text')}
    else:
        year_raw = None
    identity = row.get('source_id') or f"{source}:{row.get('city')}:{row.get('name')}"
    return {
        'platform': source,
        'city': row.get('city'),
        'district': row.get('district'),
        'name': row.get('name'),
        'phase': row.get('phase') or extract_phase(row.get('name')),
        'identity': identity,
        'has_stable_id': bool(row.get('source_id')),
        'year_raw': year_raw,
        'year_field': OBS_YEAR_FIELD.get(source) if year_raw else None,
        'year_precision': year_raw.get('precision') if year_raw else None,
        'evidence': {'source_url': row.get('source_url'),
                     'snapshot_path': row.get('snapshot_path')},
        **_type_fields(row.get('type_tags'), row.get('has_villa')),
    }


def _dedupe_records(records):
    """Platform-internal identity dedup; repeated pagination never double counts."""
    seen = OrderedDict()
    for record in records:
        key = (record.get('platform'), record.get('identity'))
        if key not in seen:
            seen[key] = record
            continue
        # A later row may fill a missing year fact; it never overwrites one.
        kept = seen[key]
        if not kept.get('year_raw') and record.get('year_raw'):
            kept['year_raw'] = record['year_raw']
            kept['year_field'] = record.get('year_field')
            kept['year_precision'] = record.get('year_precision')
    return list(seen.values())


def load_candidate_records(candidate_id, repo_root=Path('.'), merge_observations=False):
    """Normalized candidate records: identity/year/evidence fields, offline only.

    A1 房天下 caliber = profiles.json + 房天下 observations.jsonl rows, dedup by id;
    merge_observations keeps the profiles-only default for other callers.
    """
    repo_root = Path(repo_root)
    meta = CANDIDATES[candidate_id]
    if candidate_id == 'fang':
        data = _read_json(repo_root / meta['inputs']['profiles']) or {}
        records = [ _fang_record(r) for r in data.get('records', []) ]
        if not merge_observations:
            return records
        obs = read_jsonl(repo_root / REPO_OBSERVATIONS)
        return _dedupe_records([_fang_record(r) for r in obs
                                if r.get('source') == '房天下'] + records)
    source = meta['label']
    rows = [r for r in read_jsonl(repo_root / meta['inputs']['observations'])
            if r.get('source') == source]
    return [_observation_record(r) for r in dedup_observations(rows)]


# ---------------------------------------------------------------------------
# §2.1 evaluation
# ---------------------------------------------------------------------------
def _year_category(record):
    year = record.get('year_raw') or {}
    start, end = year.get('start'), year.get('end')
    if start is None and end is None:
        return 'missing_year'
    if year.get('meaning') not in VALID_YEAR_MEANINGS:
        return 'semantic_unknown'
    precision = str(year.get('precision') or record.get('year_precision') or '').lower()
    label = str(year.get('label') or '')
    if precision == 'decade' or label.endswith('年代'):
        return 'decade_only'
    if start is not None and end is not None and start != end:
        return 'range'
    return 'exact_year'


def _has_evidence(record):
    evidence = record.get('evidence') or {}
    return bool(evidence.get('source_url') or evidence.get('snapshot_path'))


def _is_valid_year_record(record):
    return (bool(record.get('name')) and bool(record.get('has_stable_id'))
            and _year_category(record) in VALID_YEAR_CATEGORIES
            and _has_evidence(record))


def _stability(by_city):
    completions = [c.get('tested', {}).get('completion') for c in by_city.values()]
    return {'blocked_scopes': sum(1 for c in completions if c == '阻断'),
            'success_scopes': sum(1 for c in completions if c == '成功')}


def _districts_of(by_city):
    names = set()
    for city in by_city.values():
        names.update(city.get('districts', {}))
    return names


def rank_candidates(candidates):
    """Sort desc by combined_valid_year; ties by TIE_BREAK_ORDER. Forbidden fields unused."""
    items = list(candidates.values()) if isinstance(candidates, dict) else list(candidates)
    items.sort(key=lambda c: (-c.get('combined_valid_year', 0), _imbalance(c),
                              -c.get('district_count', 0),
                              _stability(c.get('by_city', {})).get('blocked_scopes', 0),
                              -_stability(c.get('by_city', {})).get('success_scopes', 0),
                              c.get('candidate_id', '')))
    return items


def _imbalance(candidate):
    by_city = candidate.get('by_city', {})
    bj = by_city.get('北京', {}).get('valid_year_total', 0) or 0
    sh = by_city.get('上海', {}).get('valid_year_total', 0) or 0
    return abs(bj - sh)


def _tested_block(platform, city, city_records, request_rows, status):
    districts = sorted({r['district'] for r in city_records if r.get('district')})
    reqs = [r for r in (request_rows or []) if r.get('source') == platform
            and r.get('city') == city]
    if reqs:
        state = fetch_status(reqs)
        completion, reason = state['state'], state['reason']
        pages = len(reqs)
    elif status:
        last = status.get('last_run') or {}
        completion = last.get('state') or '成功'
        reason = last.get('error') or ''
        pages = last.get('successful_pages') or 0
    else:
        completion, reason, pages = '未测', '无请求记录', 0
    block = {'districts': districts, 'district_count': len(districts),
             'pages': pages, 'completion': completion, 'unfinished_reason': reason}
    if status:
        sc = (status.get('by_city') or {}).get(city) or {}
        block['status_with_year'] = sc.get('with_year')
    return block


def evaluate_candidate(records, request_rows=None, status=None):
    """Per-city valid-year breakdown + combined_valid_year. Counts are program-generated."""
    platform = records[0].get('platform') if records else ''
    label = next((m['label'] for m in CANDIDATES.values() if m['label'] == platform), platform)
    unique = _dedupe_records(records)
    by_city = {}
    for city in TARGET_CITIES:
        city_records = [r for r in unique if r.get('city') == city]
        cats = {name: 0 for name in ('exact_year', 'decade_only', 'range',
                                     'missing_year', 'semantic_unknown')}
        exclusions = defaultdict(int)
        districts = defaultdict(int)
        valid = 0
        for record in city_records:
            category = _year_category(record)
            cats[category] = cats.get(category, 0) + 1
            if record.get('district'):
                districts[record['district']] += 1
            if _is_valid_year_record(record):
                valid += 1
                continue
            if not record.get('name'):
                exclusions['missing_name'] += 1
            elif not record.get('has_stable_id'):
                exclusions['no_stable_id'] += 1
            elif category in ('missing_year', 'semantic_unknown'):
                exclusions[category] += 1
            elif not _has_evidence(record):
                exclusions['no_evidence'] += 1
            else:
                exclusions['unknown'] += 1
        parents = parent_community_count([
            {'source': platform, 'city': city, 'name': r['name'],
             'source_id': r['identity'] if r.get('has_stable_id') else None}
            for r in city_records])
        phase_count = sum(1 for r in city_records if r.get('phase'))
        name_ids = defaultdict(set)
        for r in city_records:
            name_ids[r.get('name')].add(r.get('identity'))
        possible_dup = sum(max(0, len(ids) - 1) for ids in name_ids.values())
        by_city[city] = {
            'denominator': len(city_records),
            'valid_year_total': valid,
            'exact_year': cats['exact_year'],
            'decade_only': cats['decade_only'],
            'range': cats['range'],
            'missing_year': cats['missing_year'],
            'semantic_unknown': cats['semantic_unknown'],
            'exclusions': dict(exclusions),
            'districts': dict(sorted(districts.items())),
            'parent_community_count': parents['count'],
            'phase_count': phase_count,
            'possible_duplicate_count': possible_dup,
            'tested': _tested_block(platform, city, city_records, request_rows, status),
        }
    combined = sum(by_city[c]['valid_year_total'] for c in TARGET_CITIES)
    result = {'platform': platform, 'label': label, 'combined_valid_year': combined,
              'by_city': by_city}
    for city in TARGET_CITIES:
        result[city] = by_city[city]
    return result


def _load_legacy_audit(out_dir):
    """Prior B3/B4 page-count audit, embedded verbatim; idempotent on re-run."""
    path = Path(out_dir) / 'source_comparison.json'
    if not path.exists():
        return {}
    obj = json.loads(path.read_text(encoding='utf-8'))
    if obj.get('schema') == 'source_comparison/v1':
        return obj.get('legacy_page_count_audit') or {}
    return obj


def _evidence_pointers(records):
    pointers = []
    for record in records:
        evidence = record.get('evidence') or {}
        pointers.append({'identity': record.get('identity'),
                         'source_url': evidence.get('source_url'),
                         'snapshot_path': evidence.get('snapshot_path'),
                         'year_field': record.get('year_field')})
    return pointers


def build_source_comparison(repo_root=Path('.'), out_dir=DEFAULT_OUT_DIR):
    """§2.1 candidate comparison: per city/区县 valid years + ranking + preserved legacy."""
    repo_root = Path(repo_root)
    candidates = OrderedDict()
    for candidate_id, meta in CANDIDATES.items():
        records = load_candidate_records(candidate_id, repo_root,
                                         merge_observations=(candidate_id == 'fang'))
        request_rows = read_jsonl(repo_root / meta['inputs']['requests']) \
            if meta['inputs'].get('requests') else None
        status = _read_json(repo_root / meta['inputs']['status']) \
            if meta['inputs'].get('status') else None
        evaluation = evaluate_candidate(records, request_rows=request_rows, status=status)
        candidates[candidate_id] = {
            'candidate_id': candidate_id,
            'label': meta['label'],
            'inputs': meta['inputs'],
            'by_city': evaluation['by_city'],
            'combined_valid_year': evaluation['combined_valid_year'],
            'district_count': len(_districts_of(evaluation['by_city'])),
            'stability': _stability(evaluation['by_city']),
            'raw_evidence_pointers': _evidence_pointers(records),
        }
    return {
        'schema': 'source_comparison/v1',
        'generated_at': now_iso(),
        'policy_version': YEAR_POLICY_VERSION,
        'rules': {
            'selection_criteria': '按北京+上海合计有效年份小区数排序,选一个统一主平台',
            'valid_record_requirements': [
                '有稳定的平台内来源身份', '属于目标城市(北京/上海)', '确为小区或可关联小区分期',
                '有可解释的建成/建筑年代字段及原文依据(source_url或snapshot/raw path)'],
            'dedup_unit': {
                'fang': 'id; 房天下 = profiles.json + observations.jsonl(房天下) 去重',
                'observations': 'source_id, 缺失时 (source,city,name)'},
            'year_category_split': list(('exact_year', 'decade_only(start==end除外)',
                                         'range', 'missing_year', 'semantic_unknown')),
            'forbidden_criteria': list(FORBIDDEN_CRITERIA),
            'tie_break_order': list(TIE_BREAK_ORDER),
            'phase_and_parent_counted_separately': True,
        },
        'forbidden_criteria_used': [],
        'candidates': candidates,
        'ranking': rank_candidates(candidates),
        'legacy_page_count_audit': _load_legacy_audit(out_dir),
    }


# ---------------------------------------------------------------------------
# §2.1 selection result
# ---------------------------------------------------------------------------
def assess_beijing_target(bj_valid, sh_valid):
    """A1: only judge whether the two-city valid-year coverage is imbalanced.

    A raw count ratio can never prove city-wide catalog/type/location/metro conditions, so
    the statement never claims a target is met.
    """
    bj_valid = bj_valid or 0
    sh_valid = sh_valid or 0
    if bj_valid == 0 and sh_valid == 0:
        imbalanced = False
    elif bj_valid == 0 or sh_valid == 0:
        imbalanced = True
    else:
        imbalanced = max(bj_valid, sh_valid) >= 10 * min(bj_valid, sh_valid)
    statement = (f'两城有效年份数量:北京 {bj_valid} 条、上海 {sh_valid} 条;'
                 f'该数量比例不能证明全城目录、类型、定位与地铁条件已满足,'
                 f'仅用于描述两城覆盖是否失衡。')
    return imbalanced, statement


def _tie_break_applied(winner, runner):
    if runner is None or winner.get('combined_valid_year') != runner.get('combined_valid_year'):
        return []
    if _imbalance(winner) != _imbalance(runner):
        return ['两城覆盖均衡']
    applied = ['两城覆盖均衡']
    if winner.get('district_count') != runner.get('district_count'):
        applied.append('区县覆盖广')
        return applied
    applied.append('区县覆盖广')
    applied.append('获取稳定性')
    return applied


def select_primary_platform(comparison):
    """Winner by combined valid years; tie-break order exposed; two-city coverage assessed."""
    ranking = comparison.get('ranking') or rank_candidates(comparison.get('candidates', {}))
    winner = ranking[0]
    by_city = winner['by_city']
    bj = by_city.get('北京', {}).get('valid_year_total', 0)
    sh = by_city.get('上海', {}).get('valid_year_total', 0)
    imbalanced, cov_statement = assess_beijing_target(bj, sh)
    runner = ranking[1] if len(ranking) > 1 else None
    reason = (f'北京+上海合计有效年份小区数最高({winner["combined_valid_year"]}),'
              f'选定 {winner["label"]};排序按 {"/".join(TIE_BREAK_ORDER)} 比较。')
    if winner['combined_valid_year'] == 0 or (runner is not None and (
            runner['combined_valid_year'] == winner['combined_valid_year'])):
        reason += '（受预算/阻断限制,结论为本轮已获得有效年份数据最多的平台,非全站最大值证明。）'
    label = winner['label']
    unified_met = bool(bj > 0 and sh > 0)
    if unified_met:
        caliber_statement = (f'两城统一年份口径已覆盖：主平台 {label} 北京 {bj} 条、上海 {sh} 条。')
    else:
        empty = '北京' if bj == 0 else '上海'
        caliber_statement = (f'两城统一年份口径仍未达到：主平台 {label} 在{empty}无有效年份数据'
                             f'(北京 {bj}, 上海 {sh})；不按城市切换年份平台。')
    return {
        'primary_source': label,
        'candidate_id': winner['candidate_id'],
        'combined_valid_year': winner['combined_valid_year'],
        'per_city': {city: {'valid_year_total': by_city.get(city, {}).get('valid_year_total', 0),
                            'denominator': by_city.get(city, {}).get('denominator', 0)}
                     for city in TARGET_CITIES},
        'selection_reason': reason,
        'tie_break_applied': _tie_break_applied(winner, runner),
        'two_city_coverage_imbalanced': imbalanced,
        'two_city_coverage_statement': cov_statement,
        'unified_two_city_year_caliber_met': unified_met,
        'two_city_year_caliber_statement': caliber_statement,
    }


# ---------------------------------------------------------------------------
# §2.2 helpers
# ---------------------------------------------------------------------------
def platform_year_label(source):
    return f'平台记录年份，来源：{source}'


def _canonical_scope(value):
    """Reuse judgement's scope aliasing instead of duplicating the table."""
    return judgement.normalize_year(
        {'year': {'status': 'recorded', 'start': 2000, 'end': 2000,
                  'scope': value}})['scope']


def _fact_key(value):
    return (value.get('start'), value.get('end'), value.get('precision'),
            value.get('scope'), value.get('meaning'))


def _obs_platform(obs):
    return obs.get('platform') or obs.get('source')


def _obs_field(obs):
    return obs.get('year_field') or obs.get('field')


def _obs_value(obs):
    value = obs.get('value') if isinstance(obs.get('value'), dict) else obs.get('year_raw')
    if isinstance(value, dict):
        return _norm_year_value(value)
    start, end = obs.get('start'), obs.get('end')
    if start is None and end is None:
        return None
    return {'start': start, 'end': end, 'precision': obs.get('precision'),
            'scope': obs.get('scope'), 'meaning': obs.get('meaning'), 'label': None}


def _policy_from_fact(fact, primary_source, selected_source_id, others, policy_version):
    raw = {'start': fact[0], 'end': fact[1], 'precision': fact[2],
           'scope': fact[3], 'meaning': fact[4]}
    return {
        'primary_source': primary_source,
        'policy_version': policy_version,
        'selected_source_id': selected_source_id,
        'year_raw': raw,
        'year_start': raw['start'],
        'year_end': raw['end'],
        'year_scope': raw['scope'],
        'selection_reason': (f'跨来源年份差异不阻断：采用主平台 {primary_source} 的观测年份，'
                             f'其他平台差异仅作提示'),
        'cross_source_difference': others,
        'status': 'recorded',
    }


def select_object_year(observations, primary_source, selected_source_id=None,
                       policy_version=YEAR_POLICY_VERSION):
    """§2.2 rules 1-4. Returns the 9-field year policy dict or a status dict.

    Explicit 小区年代 fields outrank promotional body text; the cross-source positive path
    delegates to fang_store.select_publish_year (never reimplemented). A missing primary year
    stays unknown with other platforms only as auxiliary clues.
    """
    facts = OrderedDict()
    for obs in observations or []:
        value = _obs_value(obs)
        if value is None:
            continue
        key = _fact_key(value)
        entry = facts.setdefault(key, {'value': value, 'explicit': False, 'platforms': set()})
        entry['platforms'].add(_obs_platform(obs))
        if _obs_field(obs) in EXPLICIT_YEAR_FIELDS:
            entry['explicit'] = True
    explicit_exists = any(entry['explicit'] for entry in facts.values())
    body_text_ignored = []
    used = OrderedDict()
    for key, entry in facts.items():
        if explicit_exists and not entry['explicit']:
            body_text_ignored.append({'value': entry['value'],
                                      'platforms': sorted(entry['platforms'])})
            continue
        used[key] = entry

    conflict = {
        'source_id': selected_source_id,
        'values': [{'value': entry['value'],
                    'evidence': [{'platform': platform}
                                 for platform in sorted(entry['platforms'])]}
                   for entry in used.values()],
    }
    policy = fang_store.select_publish_year(conflict, primary_source, policy_version)
    if policy is not None:
        policy['status'] = 'recorded'
        if body_text_ignored:
            policy['body_text_ignored'] = body_text_ignored
        return policy

    primary_keys = [key for key, entry in used.items()
                    if primary_source in entry['platforms']]
    if not primary_keys:
        auxiliary = [{'platform': platform, 'year_raw': entry['value']}
                     for entry in used.values() for platform in sorted(entry['platforms'])
                     if platform != primary_source]
        return {'status': 'unknown', 'primary_source': primary_source,
                'policy_version': policy_version, 'selected_source_id': selected_source_id,
                'year_raw': None, 'year_start': None, 'year_end': None, 'year_scope': None,
                'selection_reason': '主平台缺年份,保持未知;其他平台年份仅作辅助线索,不合并',
                'cross_source_difference': [], 'auxiliary_clues': auxiliary}

    if len(primary_keys) == 1:
        key = primary_keys[0]
        others = [{'platform': platform, 'value': entry['value']}
                  for entry_key, entry in used.items() if entry_key != key
                  for platform in sorted(entry['platforms'])]
        return _policy_from_fact(key, primary_source, selected_source_id, others,
                                 policy_version)

    scopes = {key[3] for key in primary_keys}
    if len(scopes) == 1:
        return {'status': 'pending', 'status_text': '待核', 'primary_source': primary_source,
                'policy_version': policy_version, 'selected_source_id': selected_source_id,
                'year_raw': None, 'year_start': None, 'year_end': None, 'year_scope': None,
                'selection_reason': '主平台同一适用范围存在互相矛盾的明确年份,待核,'
                                    '不机械取最早或最新值',
                'cross_source_difference': []}
    community_keys = [key for key in primary_keys if key[3] in WHOLE_COMMUNITY_SCOPES]
    if len(community_keys) == 1:
        key = community_keys[0]
        others = [{'platform': platform, 'value': entry['value']}
                  for entry_key, entry in used.items() if entry_key != key
                  for platform in sorted(entry['platforms'])]
        return _policy_from_fact(key, primary_source, selected_source_id, others,
                                 policy_version)
    return {'status': 'pending', 'status_text': '待核', 'primary_source': primary_source,
            'policy_version': policy_version, 'selected_source_id': selected_source_id,
            'year_raw': None, 'year_start': None, 'year_end': None, 'year_scope': None,
            'selection_reason': '主平台存在多个适用范围不同的明确年份且无法唯一确定,待核',
            'cross_source_difference': []}


def range_membership(year_start, year_end, q_from, q_to):
    """§2.2 rule 5: definite only when FULLY inside; partial kept separate."""
    evidence = {'year': {'status': 'recorded', 'start': year_start, 'end': year_end}}
    result = judgement.year_condition(evidence, q_from, q_to)
    status, reason = result['status'], result.get('reason', '')
    if status == 'supported':
        return 'definite'
    if status == 'excluded':
        return 'outside'
    if year_start is None and year_end is None:
        return 'unknown'
    if '未知' in reason:
        return 'unknown'
    return 'partial'


def phase_scope_guard(year_scope, target_scope):
    """§2.2 rule 5: a phase year must NOT be expanded to the whole community."""
    scope = _canonical_scope(year_scope)
    target = _canonical_scope(target_scope) if target_scope not in (None, '') else 'community'
    blocked = scope == 'phase' and target in ('community', None)
    if blocked:
        return {'blocked': True, 'status': 'blocked',
                'reason': '分期年份不能扩展为整个小区所有组团的年份(§2.2 规则5)'}
    return {'blocked': False, 'status': 'allowed', 'reason': ''}


# ---------------------------------------------------------------------------
# §2.2/§7.1 policy artifact
# ---------------------------------------------------------------------------
def build_year_policy(comparison, selection, records=None, repo_root=Path('.'),
                      seed=20260922):
    """§2.1 selection record + §2.2 rule mapping + metadata contract + example."""
    policy_version = comparison.get('policy_version', YEAR_POLICY_VERSION)
    contract = {
        'fields': list(YEAR_POLICY_FIELDS),
        'field_meanings': {field: SELECTION_FIELD_MEANINGS[field]
                           for field in YEAR_POLICY_FIELDS},
        'rule_1_label': platform_year_label(selection['primary_source']),
    }
    rules_2_2 = {
        '1': 'platform_year_label(): 主平台清楚标注建成年份时直接作为筛选年份,注明"平台记录年份,来源:××";'
             'select_object_year() 返回 year_raw/year_start/year_end/year_scope。',
        '2': 'select_object_year() 调用 fang_store.select_publish_year,其他平台年份存入 '
             'cross_source_difference(原值+差异提示),不阻止主平台年份,不取平均、不投票。',
        '3': 'select_object_year() 主平台无年份事实时返回 status=unknown,其他平台年份放 '
             'auxiliary_clues,与主平台结果分开,不静默补值。',
        '4': 'EXPLICIT_YEAR_FIELDS(小区年代字段)优先于宣传正文(丢弃 body-text 事实);'
             '同一适用范围存在互相矛盾的明确字段时返回 status=pending/待核,不取最早或最新。',
        '5': 'range_membership() 仅完全落入用户区间返回 definite,部分重叠返回 partial;'
             'phase_scope_guard() 阻止分期年份扩展为整个小区。',
    }
    example = {
        'primary_source': selection['primary_source'],
        'policy_version': policy_version,
        'selected_source_id': 'fang:sh.esf.fang.com:1210000779',
        'year_raw': {'start': 2000, 'end': 2000, 'precision': 'year', 'scope': '小区',
                     'meaning': 'completion'},
        'year_start': 2000,
        'year_end': 2000,
        'year_scope': '小区',
        'selection_reason': (f'跨来源年份差异不阻断：采用主平台 {selection["primary_source"]} '
                             f'的观测年份，其他平台差异仅作提示'),
        'cross_source_difference': [],
    }
    return {
        'schema': 'year_policy/v1',
        'primary_source': selection['primary_source'],
        'policy_version': policy_version,
        'selection_reason': selection['selection_reason'],
        'selection_time': comparison.get('generated_at') or now_iso(),
        'candidate_comparison': [
            {'candidate_id': c['candidate_id'], 'label': c['label'],
             'combined_valid_year': c['combined_valid_year'],
             'per_city': {city: {'valid_year_total':
                                 c['by_city'].get(city, {}).get('valid_year_total', 0),
                                 'denominator': c['by_city'].get(city, {}).get('denominator', 0)}
                          for city in TARGET_CITIES},
             'district_count': c['district_count'], 'stability': c['stability']}
            for c in comparison.get('ranking', [])
        ],
        'tie_break_order': list(TIE_BREAK_ORDER),
        'two_city_year_caliber': {
            'met': selection['unified_two_city_year_caliber_met'],
            'statement': selection['two_city_year_caliber_statement'],
            'primary_per_city': selection['per_city'],
            'coverage_imbalanced': selection['two_city_coverage_imbalanced'],
            'coverage_statement': selection['two_city_coverage_statement'],
        },
        'selection_metadata_contract': contract,
        'rules_2_2': rules_2_2,
        'per_object_example': example,
        'sampling_plan': build_sampling_plan(comparison, seed=seed, records=records,
                                             repo_root=repo_root),
    }


# ---------------------------------------------------------------------------
# §2.3 checks sample (unfilled conclusion fields)
# ---------------------------------------------------------------------------
def _name_base_index(records):
    index = defaultdict(set)
    for record in records:
        index[(record.get('city'), strip_phase(record.get('name')))].add(record.get('identity'))
    return index


def _boundary_kind(record, name_index):
    year = record.get('year_raw') or {}
    start, end = year.get('start'), year.get('end')
    precision = str(year.get('precision') or record.get('year_precision') or '').lower()
    if precision in ('decade', 'range') or (start is not None and end is not None
                                            and start != end):
        return '年代范围'
    if record.get('mixed'):
        return '混合小区'
    base = (record.get('city'), strip_phase(record.get('name')))
    if record.get('phase') or len(name_index.get(base, ())) > 1:
        return '同名分期'
    if (start is None and end is None) or not record.get('year_field'):
        return '缺失值'
    current_year = datetime.now(timezone.utc).year
    if (start is not None and start < YEAR_1950) or (end is not None and end > current_year):
        return '异常年份'
    return None


def _sample_row(record, city, source, stratum, boundary_kind, seed, reason):
    year = record.get('year_raw') or {}
    return {
        'city': city,
        'source': source,
        'community_id': record.get('identity'),
        'name': record.get('name'),
        'district': record.get('district'),
        'year_raw': year,
        'year_start': year.get('start'),
        'year_end': year.get('end'),
        'year_scope': year.get('scope'),
        'stratum': stratum,
        'boundary_kind': boundary_kind,
        'selection_reason': reason,
        'random_seed': seed,
        'conclusion': None,
        'conclusion_reason': None,
        'checked_at': None,
    }


def _plan_city(city_records, name_index, rng, per_city, random_n, boundary_n):
    """Shared seeded per-city pick: random + boundary, then random top-up when pool too small."""
    target = min(per_city, len(city_records))
    entries = []
    chosen_ids = set()
    for record in rng.sample(city_records, min(random_n, len(city_records))):
        entries.append((record, 'random', None, '随机样本'))
        chosen_ids.add(record.get('identity'))
    pool = [r for r in city_records
            if r.get('identity') not in chosen_ids and _boundary_kind(r, name_index)]
    rng.shuffle(pool)
    boundary_limit = min(boundary_n, max(0, target - len(entries)), len(pool))
    for record in pool[:boundary_limit]:
        kind = _boundary_kind(record, name_index)
        entries.append((record, 'boundary', kind, f'边界样本:{kind}'))
        chosen_ids.add(record.get('identity'))
    top_up = 0
    if len(entries) < target:
        # Boundary pool exhausted: fill remaining quota with distinct random rows.
        remaining = [r for r in city_records if r.get('identity') not in chosen_ids]
        extra = rng.sample(remaining, min(target - len(entries), len(remaining)))
        for record in extra:
            entries.append((record, 'random', None, '随机补充样本:边界池不足'))
            chosen_ids.add(record.get('identity'))
            top_up += 1
    pool_counts = {kind: 0 for kind in BOUNDARY_KINDS}
    for record in city_records:
        kind = _boundary_kind(record, name_index)
        if kind:
            pool_counts[kind] += 1
    return {
        'rows': entries,
        'objects': len(city_records),
        'random': sum(1 for entry in entries if entry[1] == 'random'),
        'boundary': sum(1 for entry in entries if entry[1] == 'boundary'),
        'top_up_random': top_up,
        'total': len(entries),
        'boundary_pool': pool_counts,
    }


def build_sampling_plan(comparison, seed=20260922, per_city=100, random_n=60,
                        boundary_n=40, records=None, repo_root=Path('.')):
    """§2.3 plan summary; same seeding/order as build_checks_sample so counts agree."""
    ranking = comparison.get('ranking') or rank_candidates(comparison.get('candidates', {}))
    primary = ranking[0]
    if records is None:
        records = load_candidate_records(primary['candidate_id'], repo_root)
    records = _dedupe_records([r for r in records if r.get('platform') == primary['label']])
    name_index = _name_base_index(records)
    rng = random.Random(seed)
    cities = OrderedDict()
    for city in TARGET_CITIES:
        city_records = [r for r in records if r.get('city') == city]
        plan = _plan_city(city_records, name_index, rng, per_city, random_n, boundary_n)
        cities[city] = {key: plan[key] for key in
                        ('objects', 'random', 'boundary', 'top_up_random', 'total',
                         'boundary_pool')}
    topped = [city for city, counts in cities.items() if counts['top_up_random']]
    if topped:
        shortfall = '、'.join(topped)
        note = (f'每城目标 {per_city} 条(随机 {random_n}+边界 {boundary_n});'
                f'边界池不足:{shortfall} 以随机样本补足到目标条数。')
    else:
        note = (f'每城目标 {per_city} 条(随机 {random_n}+边界 {boundary_n});'
                f'各城边界池充足,无需随机补充。')
    return {
        'seed': seed,
        'per_city_target': per_city,
        'random_target': random_n,
        'boundary_target': boundary_n,
        'boundary_kinds': list(BOUNDARY_KINDS),
        'cities': cities,
        'note': note,
    }


def build_checks_sample(comparison, seed=20260922, per_city=100, random_n=60,
                        boundary_n=40, records=None, repo_root=Path('.')):
    """§2.3 sampling plan: per_city random + boundary rows; conclusion fields left unfilled."""
    ranking = comparison.get('ranking') or rank_candidates(comparison.get('candidates', {}))
    primary = ranking[0]
    if records is None:
        records = load_candidate_records(primary['candidate_id'], repo_root)
    records = _dedupe_records([r for r in records if r.get('platform') == primary['label']])
    name_index = _name_base_index(records)
    rng = random.Random(seed)

    rows = []
    for city in TARGET_CITIES:
        city_records = [r for r in records if r.get('city') == city]
        plan = _plan_city(city_records, name_index, rng, per_city, random_n, boundary_n)
        for record, stratum, boundary_kind, reason in plan['rows']:
            rows.append(_sample_row(record, city, primary['label'], stratum, boundary_kind,
                                    seed, reason))
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv):
    parser = argparse.ArgumentParser(description='§2 统一年份主平台 selection + §2.2 rules')
    parser.add_argument('--out', default=DEFAULT_OUT_DIR, help='output dir (default trial dir)')
    parser.add_argument('--seed', type=int, default=20260922)
    parser.add_argument('--repo-root', default='.')
    parser.add_argument('--source-comparison', action='store_true')
    parser.add_argument('--year-policy', action='store_true')
    parser.add_argument('--checks-sample', action='store_true')
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    flags = (args.source_comparison, args.year_policy, args.checks_sample)
    if not any(flags):
        args.source_comparison = args.year_policy = args.checks_sample = True

    repo_root = Path(args.repo_root)
    out_dir = Path(args.out)
    comparison = build_source_comparison(repo_root, out_dir=out_dir)
    selection = select_primary_platform(comparison)
    records = load_candidate_records(selection['candidate_id'], repo_root)
    policy = build_year_policy(comparison, selection, records=records, repo_root=repo_root,
                               seed=args.seed)

    if args.source_comparison:
        _json_write(out_dir / 'source_comparison.json', comparison)
    if args.year_policy:
        _json_write(out_dir / 'year_policy.json', policy)
    if args.checks_sample:
        rows = build_checks_sample(comparison, seed=args.seed, records=records,
                                   repo_root=repo_root)
        write_jsonl(out_dir / 'checks_sample.jsonl', rows)

    summary = {
        'primary_source': selection['primary_source'],
        'candidate_id': selection['candidate_id'],
        'combined_valid_year': selection['combined_valid_year'],
        'per_city': selection['per_city'],
        'two_city_coverage_imbalanced': selection['two_city_coverage_imbalanced'],
        'unified_two_city_year_caliber_met': selection['unified_two_city_year_caliber_met'],
        'tie_break_applied': selection['tie_break_applied'],
        'checks_sample_rows': len(rows) if args.checks_sample else 0,
        'out': str(out_dir),
        'seed': args.seed,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == '__main__':
    main()
