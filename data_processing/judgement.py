"""Single source of truth for year/form/location/metro judgements.

Pure stdlib so any repo module can import it without a circular dependency. All
construction-year event semantics, object scope, phase binding and coordinate
precision rules live here; callers (community_catalog, metro_proximity, the
acceptance audit) only adapt the returned status/reason.
"""
import json
import re

# Canonical meaning aliases: only ``completion`` counts as a 建成 year.
COMPLETION_MEANINGS = {'completion'}
NON_COMPLETION_MEANINGS = {'opening', 'land', 'presale', 'filing', 'certificate', 'sale'}

_MEANING_ALIASES = {}


def _register_meaning(canonical, *aliases):
    for alias in aliases:
        _MEANING_ALIASES[alias] = canonical


_register_meaning('completion', 'completion', 'completed', 'built', '建成', '竣工', '交付')
_register_meaning('opening', 'opening', '开盘')
_register_meaning('land', 'land', '拿地')
_register_meaning('presale', 'presale', 'pre_sale', '预售')
_register_meaning('filing', 'filing', '备案')
_register_meaning('certificate', 'certificate', '取证', '预售证', 'permit', '许可证')
_register_meaning('sale', 'sale', '销售')

# Scope aliases -> canonical object scope for a dated evidence item.
_SCOPE_ALIASES = {
    'community': 'community', '小区': 'community', '社区': 'community',
    '小区/分期': 'community', 'community_or_phase': 'community',
    'building': 'building', '单栋': 'building', '栋': 'building', '楼栋': 'building',
    'building_level': 'building', 'single_building': 'building',
    'phase': 'phase', '分期': 'phase', '期': 'phase', 'phase_level': 'phase', '组团': 'phase',
    'multi_phase': 'multi_phase', '多期': 'multi_phase', '分期集合': 'multi_phase',
    'multiple_phases': 'multi_phase', 'complex': 'multi_phase', 'mixed_phase': 'multi_phase',
}

# Location precision aliases -> canonical precision level.
_PRECISION_ALIASES = {
    'point': 'point', 'exact': 'point', '点': 'point',
    'centroid': 'representative_point', 'representative_point': 'representative_point',
    'osm_geometry': 'representative_point', 'geometry': 'representative_point',
    '几何中心': 'representative_point', '代表点': 'representative_point',
    'parcel': 'parcel', '地块': 'parcel',
    'street': 'street', '街道': 'street',
    'district': 'district', 'county': 'district', '区县': 'district',
}

_EXHAUSTIVE_TOKENS = ('完整类型清单', '__exhaustive__')
# OSM plain-residence tags (house/terrace) are clues only, never explicit villa.
PLAIN_RESIDENCE_TOKENS = ('独栋/联排住宅',)
_NEGATION_RE = re.compile(r'^(?:无|非|不含|没有)')
_LOU_RE = re.compile(r'楼房|高层|多层|洋房|小高层')
_VILLA_PHASE_KEYS = ('kind', 'type', 'name', 'value')
_USABLE_PRECISIONS = ('point', 'representative_point', 'parcel')


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _json_load(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _list(value):
    """Coerce a JSON string, list or scalar into a list of tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        loaded = _json_load(value)
        if isinstance(loaded, list):
            return list(loaded)
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _truthy(value):
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes')
    return value == 1


def _as_float(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _raw_part(row, key):
    """Canonical sub-dict extracted from a raw row mapping."""
    if key == 'year':
        nested = row.get('completion_year') if isinstance(row.get('completion_year'), dict) else {}

        def pick(*names):
            for name in names:
                if row.get(name) is not None:
                    return row.get(name)
                if nested.get(name) is not None:
                    return nested.get(name)
            return None
        return {'status': pick('year_status', 'status'),
                'start': pick('year_start', 'start'),
                'end': pick('year_end', 'end'),
                'precision': pick('year_precision', 'precision'),
                'meaning': pick('year_meaning', 'meaning'),
                'scope': pick('year_scope', 'scope'),
                'phase': pick('year_phase', 'phase'),
                'label': pick('year_label', 'label', 'raw_text')}
    if key == 'form':
        return {'residential_forms': row.get('residential_forms'),
                'housing_types': row.get('housing_types'),
                'villa_subtypes': row.get('villa_subtypes'),
                'forms_exhaustive': row.get('forms_exhaustive'),
                'pure_villa': row.get('pure_villa'),
                'type_evidence': row.get('type_evidence'),
                'villa_phases': row.get('villa_phases')}
    if key == 'location':
        return {'coordinate_system': _first(row, 'coordinate_system', 'coord_system'),
                'precision_level': _first(row, 'precision_level', 'precision'),
                'accuracy_m': row.get('accuracy_m'),
                'usable': row.get('usable')}
    if key == 'metro':
        return {'meters': _first(row, 'meters', 'distance_m'),
                'lower_m': _first(row, 'lower_m', 'distance_lower_m'),
                'upper_m': _first(row, 'upper_m', 'distance_upper_m'),
                'station': row.get('station'),
                'station_id': _first(row, 'station_id', 'nearest_station_id'),
                'caliber': _first(row, 'caliber', 'distance_basis'),
                'scope': _first(row, 'scope', 'station_operational_scope'),
                'versions': row.get('versions')}
    return {}


def _part(evidence, key):
    """Return the named sub-dict of a bundle, else canonicalize the raw row."""
    if isinstance(evidence, dict):
        sub = evidence.get(key)
        if isinstance(sub, dict):
            return sub
        return _raw_part(evidence, key)
    return {}


def _canonical_meaning(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return _MEANING_ALIASES.get(text.lower(), text)


def _canonical_scope(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return _SCOPE_ALIASES.get(text.lower())


def _canonical_precision(raw):
    if raw is None:
        return 'unknown'
    text = str(raw).strip().lower()
    if not text:
        return 'unknown'
    return _PRECISION_ALIASES.get(text, 'unknown')


def _is_negated(form):
    text = str(form or '').strip()
    if _NEGATION_RE.match(text):
        return True
    return any(token in text for token in ('无别墅', '非别墅', '不含别墅'))


# --------------------------------------------------------------------------- #
# normalizers
# --------------------------------------------------------------------------- #

def normalize_year(evidence):
    """Normalize year evidence to status/start/end/precision/meaning/scope/phase/label."""
    part = _part(evidence, 'year')
    start = _as_int(_first(part, 'start', 'year_start'))
    end = _as_int(_first(part, 'end', 'year_end'))
    raw_status = str(_first(part, 'status', 'year_status') or '').strip().lower()
    if raw_status == 'conflict':
        status = 'conflict'
    elif start is None and end is None:
        status = raw_status or 'unknown'
    else:
        # A dated item is recorded even when a legacy row left year_status unset.
        status = 'recorded'
    raw_precision = _first(part, 'precision', 'year_precision')
    precision = str(raw_precision).strip().lower() if raw_precision not in (None, '') else None
    return {'status': status, 'start': start, 'end': end, 'precision': precision,
            'meaning': _canonical_meaning(_first(part, 'meaning', 'year_meaning')),
            'scope': _canonical_scope(_first(part, 'scope', 'year_scope')),
            'phase': _first(part, 'phase', 'year_phase'),
            'label': _first(part, 'label', 'year_label')}


def normalize_form(evidence):
    """Normalize residential form evidence (residential_forms/housing_types)."""
    part = _part(evidence, 'form')
    forms = _list(part.get('residential_forms'))
    if not forms:
        forms = _list(part.get('housing_types'))
    villa_subtypes = _list(part.get('villa_subtypes'))
    pure_villa = _truthy(part.get('pure_villa'))
    known = [str(token) for token in forms + villa_subtypes]
    observed = [token for token in known
                if not _is_negated(token) and token.strip() not in _EXHAUSTIVE_TOKENS]
    negated = [token for token in known if _is_negated(token)]
    complete = _truthy(part.get('forms_exhaustive')) or bool(set(known) & set(_EXHAUSTIVE_TOKENS))
    plain_residence = [token for token in observed if token in PLAIN_RESIDENCE_TOKENS]
    villa = bool(pure_villa) or bool(villa_subtypes) or any(
        '别墅' in token for token in observed)
    lou = any(_LOU_RE.search(token) for token in observed)
    villa_phases = _list(part.get('villa_phases'))
    if not villa_phases:
        for entry in _list(part.get('type_evidence')):
            if not isinstance(entry, dict):
                continue
            blob = ' '.join(str(entry.get(key) or '') for key in _VILLA_PHASE_KEYS)
            if '别墅' in blob and entry.get('scope') == 'phase' and entry.get('phase'):
                villa_phases.append(entry['phase'])
    return {'forms': forms, 'villa_subtypes': villa_subtypes, 'pure_villa': bool(pure_villa),
            'observed': observed, 'negated': negated, 'complete': complete,
            'villa': villa, 'lou': lou,
            'plain_residence': bool(plain_residence), 'clue': plain_residence,
            'has_evidence': bool(forms or villa_subtypes or pure_villa),
            'villa_phases': villa_phases}


def normalize_location(evidence):
    """Normalize location evidence; ``precision_level`` is canonical."""
    part = _part(evidence, 'location')
    system = _first(part, 'coordinate_system', 'coord_system')
    system = str(system).strip().lower() if system not in (None, '') else None
    precision_level = _canonical_precision(_first(part, 'precision_level', 'precision'))
    return {'coordinate_system': system, 'precision_level': precision_level,
            'accuracy_m': _as_float(part.get('accuracy_m')),
            'usable': precision_level in _USABLE_PRECISIONS}


def normalize_metro(evidence):
    """Normalize metro evidence; ``None`` when no distance evidence exists."""
    part = _part(evidence, 'metro')
    meters = _as_float(_first(part, 'meters', 'distance_m'))
    lower = _as_float(_first(part, 'lower_m', 'distance_lower_m'))
    upper = _as_float(_first(part, 'upper_m', 'distance_upper_m'))
    station = part.get('station')
    station_id = _first(part, 'station_id', 'nearest_station_id')
    if meters is None and lower is None and upper is None and station is None and station_id is None:
        return None
    return {'meters': meters, 'lower_m': lower, 'upper_m': upper, 'station': station,
            'station_id': station_id,
            'caliber': _first(part, 'caliber', 'distance_basis'),
            'scope': _first(part, 'scope', 'station_operational_scope'),
            'versions': part.get('versions')}


def build_evidence(row, metro=None):
    """Build the normalized evidence bundle consumed by the condition functions."""
    return {'year': normalize_year(row), 'form': normalize_form(row),
            'location': normalize_location(row), 'metro': metro}


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #

def year_condition(evidence, year_from, year_to):
    """Judge a dated evidence item against an inclusive year window."""
    if year_from is None and year_to is None:
        return {'status': 'supported', 'reason': '未限定年份'}
    year = normalize_year(evidence)
    if year['status'] == 'conflict':
        return {'status': 'pending', 'reason': '年份证据冲突'}
    start, end = year['start'], year['end']
    if year['status'] != 'recorded' or (start is None and end is None):
        return {'status': 'pending', 'reason': '年份未知'}
    meaning = year['meaning']
    if meaning not in (None, '') and meaning not in COMPLETION_MEANINGS:
        return {'status': 'pending', 'reason': f'事件语义为{meaning}，不能作为建成依据'}
    scope = year['scope']
    if scope == 'building':
        return {'status': 'pending', 'reason': '单栋日期不能作为小区建成年份'}
    if scope == 'multi_phase':
        return {'status': 'pending', 'reason': '多期年份范围不能确定别墅分期'}
    if scope == 'phase':
        villa_phases = normalize_form(evidence)['villa_phases']
        year_phase = year['phase']
        if not year_phase:
            return {'status': 'pending', 'reason': '分期未标注'}
        if villa_phases and year_phase not in villa_phases:
            joined = '、'.join(str(phase) for phase in villa_phases)
            return {'status': 'pending',
                    'reason': f'年份分期{year_phase}与别墅分期{joined}不一致'}
        if not villa_phases:
            return {'status': 'pending', 'reason': f'缺少别墅分期证据，无法确认{year_phase}'}
    if year['precision'] in ('approximate', 'decade'):
        return {'status': 'pending', 'reason': f'年份精度为{year["precision"]}'}
    low = start if start is not None else end
    high = end if end is not None else start
    if high < low:
        low, high = high, low
    bound_low = year_from if year_from is not None else float('-inf')
    bound_high = year_to if year_to is not None else float('inf')
    if high < bound_low or low > bound_high:
        return {'status': 'excluded', 'reason': f'年份 {low}-{high} 不在 {bound_low}-{bound_high}'}
    if low >= bound_low and high <= bound_high:
        return {'status': 'supported', 'reason': f'年份 {low}-{high} 符合 {bound_low}-{bound_high}'}
    return {'status': 'pending', 'reason': f'年份 {low}-{high} 部分重叠 {bound_low}-{bound_high}'}


def form_condition(evidence, forms, hints=True):
    """Tri-state residential form judgement (supported/excluded/pending/unknown)."""
    form = normalize_form(evidence)
    if not forms:
        return {'status': 'supported', 'reason': '未限定住宅形态'}
    observed, villa_subtypes, negated = form['observed'], form['villa_subtypes'], form['negated']
    if not observed and not villa_subtypes and not negated:
        return {'status': 'unknown', 'reason': '住宅形态未知'}
    want = set(forms)
    hit = want & set(observed)
    if not hit and '别墅' in want and villa_subtypes:
        hit = {'别墅'}
    if hit:
        return {'status': 'supported', 'state': '发现一种类型',
                'reason': f'发现一种类型：命中{sorted(hit)}'}
    negated_wanted = {token for token in negated if any(str(w) in token for w in want)}
    if negated_wanted:
        return {'status': 'excluded', 'state': '明确不存在某类型',
                'reason': f'明确不存在某类型：明确否定{sorted(negated_wanted)}'}
    if form['complete']:
        return {'status': 'excluded', 'state': '完整类型清单',
                'reason': f'完整类型清单{sorted(observed)}不含查询形态'}
    return {'status': 'pending',
            'reason': f'已发现形态{sorted(observed)}但清单不完整，未列出的形态不构成否定证据'}


def location_condition(evidence):
    """Precision gate: only point/representative-point/parcel positions are usable."""
    location = normalize_location(evidence)
    precision = location['precision_level']
    if precision in _USABLE_PRECISIONS:
        return {'status': 'supported', 'reason': f'坐标精度为{precision}，可用于严格判断',
                'usable': True}
    if precision in ('street', 'district'):
        return {'status': 'pending', 'reason': '区县/街道中心仅用于待核定位', 'usable': False}
    return {'status': 'unknown', 'reason': '位置精度未知', 'usable': False}


def metro_condition(evidence, max_m):
    """Judge the nearest-station distance bound against an inclusive threshold."""
    if max_m is None:
        return {'status': 'supported', 'reason': '未限定地铁距离',
                'lower_m': None, 'upper_m': None}
    metro = normalize_metro(evidence)
    if not metro:
        return {'status': 'pending', 'reason': '地铁距离未知', 'lower_m': None, 'upper_m': None}
    lower, upper = metro['lower_m'], metro['upper_m']
    if lower is None or upper is None:
        return {'status': 'pending', 'reason': '地铁距离未知', 'lower_m': lower, 'upper_m': upper}
    if upper <= max_m:
        return {'status': 'supported', 'reason': f'地铁距离上限{upper:.0f}米 ≤ {max_m:.0f}米',
                'lower_m': lower, 'upper_m': upper}
    if lower > max_m:
        return {'status': 'excluded', 'reason': f'地铁距离下限{lower:.0f}米 > {max_m:.0f}米',
                'lower_m': lower, 'upper_m': upper}
    return {'status': 'pending',
            'reason': f'地铁距离下限{lower:.0f}米..上限{upper:.0f}米跨越阈值{max_m:.0f}米',
            'lower_m': lower, 'upper_m': upper}


def metro_bounds(distance_m, accuracy_m):
    """Distance interval widened by the geometry accuracy, clamped at zero."""
    if accuracy_m is not None and accuracy_m > 0:
        return max(0.0, distance_m - accuracy_m), distance_m + accuracy_m
    return distance_m, distance_m


def metro_status(lower_m, upper_m, max_m):
    """Inclusive threshold judgement for a distance interval."""
    if upper_m <= max_m:
        return 'supported'
    if lower_m > max_m:
        return 'excluded'
    return 'pending'
