"""Bounded free-source pilot: fetch 3 source/city combos (or parse saved snapshots) and emit
machine-readable observations/requests/checks/summary. Small sampler, not a crawler framework.

Safety rules are hard-coded: single concurrency, constant User-Agent, no proxy/cookie rotation,
manual redirects that count against the request budget, stop on block/challenge, never touch
production data.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

UA = '70CityHousingResearch/1.0 (public community profiles)'
MAX_REDIRECT_HOPS = 5
CHALLENGE_TERMS = ('验证码', '安全验证', '访问已被拦截', 'captcha', 'forbidden', 'antibot')
# Body text terms are narrower: hidden login forms ship a 验证码 placeholder on normal pages,
# so generic 验证码 must never block a directory from body text alone.
CHALLENGE_BODY_TERMS = ('安全验证', '访问已被拦截', '滑块验证', '拖动滑块', '请完成验证', '人机验证')
# Structural challenge markers: a CAPTCHA shell proves itself via its host/URL/body without
# relying on a generic nav word (登录 also appears on normal pages and caused false blocks).
CHALLENGE_HOST_MARKERS = ('check.3g.fang.com',)
CHALLENGE_URL_MARKERS = ('/check.html',)
CHALLENGE_BODY_MARKERS = ('checkyzm', 'slider.on("complete"', 'slidertools', 'class="slider"')
# Pages that mean "stop this combo immediately".
STOP_CLASSES = ('blocked_403', 'blocked_429', 'http_202', 'empty_body', 'challenge')
YEAR_NOTE = '小区目录标注建成年份,非竣工证明'
MISSING_YEAR_NOTE = '该页未标注年份,保留记录待核'
# Detail pages embed an unlabeled coordinate pair; never treat it as WGS-84 without evidence.
# B2: candidate list must include BD-09 (Baidu), not just GCJ-02/WGS-84.
DETAIL_COORD_NOTE = '详情页含坐标但页面未标注坐标系,候选GCJ-02/WGS-84/BD-09,待核不得直接采用'
# Embedded list coords are raw values only; unverified system -> never distance-ready.
EMBEDDED_COORD_NOTE = '未核实坐标系(原值):候选GCJ-02/WGS-84/BD-09,不得计为可算地铁距离'
CENTANET_EMBEDDED_COORD_NOTE = \
    '列表内嵌地图坐标(原值),候选GCJ-02/WGS-84/BD-09,坐标系未核实不得计为可算地铁距离'
EMBEDDED_REPORT_NOTE = \
    '内嵌坐标仅保留原值,坐标系未核实(候选GCJ-02/WGS-84/BD-09),不计为可算地铁距离'
CENTANET_DEFAULT_PAGES = 100
CENTANET_VILLA_PAGES = 23
LIZIHANG_MAX_PAGES = 22
# A5: card <img data-src> and NUXT fileUrl share a file name once the resize suffix is gone.
LIZIHANG_IMAGE_SIZE_RE = re.compile(r'\.\d+x\d+\.(?:jpe?g|png)$', re.IGNORECASE)
LIZIHANG_PAGE_IMAGE = 'page_image'
LIZIHANG_PENDING = 'pending_association'
LIZIHANG_RECORD_LINK_METHODS = ('stable_id', LIZIHANG_PAGE_IMAGE, 'unlinked')
PENDING_REASON_NO_IMAGE = 'no_image'
PENDING_REASON_NO_MATCH = 'no_embedded_match'
PENDING_REASON_AMBIGUOUS = 'ambiguous_page_proof'
PENDING_REASON_CLAIMED = 'record_already_linked'
# Detail detection: title sits immediately before lng/lat in the inline JS object.
DETAIL_COORD_RE = re.compile(r'"lng"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"lat"\s*:\s*(-?\d+(?:\.\d+)?)')
DETAIL_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]*)"\s*,\s*"lng"\s*:')
DETAIL_ID_RE = re.compile(r'/xiaoqu/(xq-[a-z0-9]+)/')
DETAIL_ESTATE_RE = re.compile(r'estateCode=([0-9A-Za-z]+)')
CONFLICT_CONCLUSION = '待核:需先核对地址/期次/事件含义(建造/竣工/交付/开盘),不得取平均或最新覆盖'
VILLA_TAGS = ('别墅', '独栋', '联排', '双拼', '叠拼')
# OSM house/terrace tags are plain residence clues, never an explicit villa form.
PLAIN_RESIDENCE_TAGS = ('独栋/联排住宅',)
# B6: villa vs mixed must be recorded separately; a name/OSM hint is a clue, never proof.
NON_VILLA_TAGS = ('楼房', '公寓', '普通住宅', '住宅', '高层', '小高层', '多层', '洋房',
                  '商住', '塔楼', '板楼')
VILLA_SUBTYPE_TAGS = ('独栋', '联排', '双拼', '叠拼')
UNKNOWN_TYPE_TAGS = ('--', '', '未知', '暂无', '待定')
MIXED_STATES = ('mixed', 'pure_villa', 'non_villa', 'unknown')
CLUE_OSM_VALUES = ('house', 'terrace', 'detached', 'semidetached_house', 'duplex',
                   'single_family', 'cottage')
CLUE_NAME_NOTE = '名称含别墅,仅线索'
CLUE_OSM_NOTE = 'OSM普通住宅类型(house/terrace等),仅线索'
CLUE_RESIDENCE_NOTE = '故居,仅线索'
# A6: the /t2/ directory path is a platform villa-class clue, never a page-stated type.
T2_PATH_RE = re.compile(r'/t2(?:g\d+)?/?$')
CLUE_URL_T2_NOTE = '目录URL路径/t2/为平台别墅分类线索,非页面明确类型;不能证明纯别墅'
# B5: association levels. Confirmation needs >=2 substantive signals; only confirmed may transfer.
ASSOC_LEVELS = ('confirmed', 'candidate', 'rejected', 'single_source')
CONFIRM_MIN_SIGNALS = 2
TRANSFERABLE_FIELDS = ('coords_raw', 'coord_raw_system', 'coord_distance_ready',
                       'year_value', 'year_text', 'address')
CN_NUM = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5',
          '六': '6', '七': '7', '八': '8', '九': '9', '十': '10'}
# Sentinel for an embedded identifier reference the bounded parser could not resolve.
UNRESOLVED = object()
# Distinguishes "key absent" from an explicit JS null.
_MISSING = object()
# Shanghai/Beijing plausibility windows for a raw (system-unverified) coordinate pair.
COORD_RANGES = {'上海': (120.8, 122.2, 30.6, 31.9),
                '北京': (115.4, 117.6, 39.4, 41.1)}
COORD_SYSTEM_CANDIDATES = ['GCJ-02', 'WGS-84', 'BD-09']

# ---------------------------------------------------------------------------
# B3/B4 evidence-aware counting: five separated numbers, never fabricated
# ---------------------------------------------------------------------------
SCOPE_DEFAULT = 'default'
SCOPE_VILLA = 'villa'
SCOPE_BJ = 'bj_lizihang'
SCOPE_SH = 'sh_lizihang'
SCOPE_COMBINED = 'combined'
# Selected primary platform whose OWN year feeds 主平台年份可用 (cross-source year never overrides it).
PRIM_PLATFORM_SOURCE = '丽兹行'
# Declared catalog total: matches "共 N 个小区" AFTER tags are stripped to text.
PAGE_DECLARED_RE = re.compile(r'共\s*([\d,]+)\s*个小区')
LOUPAN_PAGE_RE = re.compile(r'/loupan/p(\d+)')
XIAOQU_PAGE_RE = re.compile(r'/xiaoqu/[^"\'\s<>]*?p(\d+)')
CLAIMED_SNIPPET_MAX = 120
# Hard-block failure classes for 阻断 (subset of STOP_CLASSES that mean "blocked", not malformed).
BLOCK_CLASSES = ('blocked_403', 'blocked_429', 'challenge')
FETCH_STATES = ('未测', '成功', '空目录', '截断', '阻断', '预算未完成')
PAGE_DECLARED_UNKNOWN = '页面未声明总数'
CROSS_YEAR_POLICY_NOTE = ('跨来源年份分歧不阻断主平台年份可用计数;'
                          '但存在年份冲突的配对不计入独立事实核验通过')

# ---------------------------------------------------------------------------
# request budget policy (persisted across processes; docs §4.2 / B7)
# ---------------------------------------------------------------------------
STAGE_CAPS = {'入口与结构探测': 240, '目录扩大试爬': 2800, '字段与身份补全': 760,
              '续跑与时效复查': 200}
TRIAL_TOTAL_CAP = 4000
PLATFORM_DAILY_CAP = 300
COMBINED_DAILY_CAP = 600
MIN_INTERVAL_SECONDS = 5.0
BUDGET_REASONS = ('platform_paused', 'trial_total', 'stage_cap', 'platform_daily_cap',
                  'combined_daily_cap', 'interval_not_elapsed')
DEFAULT_TRIAL_DATE = '20260922'
# 房天下 keeps its own stricter policy from fang_config.json.
FANG_DAILY_CAP = 60

COMBOS = {
    'bj_lizihang': {
        'source': '丽兹行', 'city': '北京',
        'pages': [{'url': 'https://bj.lizihang.com/loupan', 'kind': 'list'},
                  {'url': 'https://bj.lizihang.com/loupan/p2', 'kind': 'list'},
                  {'url': 'https://bj.lizihang.com/loupan/p3', 'kind': 'list'},
                  {'url': 'https://bj.lizihang.com/loupan/p22', 'kind': 'list'}],
    },
    'sh_lizihang': {
        'source': '丽兹行', 'city': '上海',
        'pages': [{'url': 'https://sh.lizihang.com/loupan', 'kind': 'list'},
                  {'url': 'https://sh.lizihang.com/loupan/p2', 'kind': 'list'},
                  {'url': 'https://sh.lizihang.com/loupan/p3', 'kind': 'list'},
                  {'url': 'https://sh.lizihang.com/loupan/p22', 'kind': 'list'}],
    },
    'sh_centanet': {
        'source': '中原', 'city': '上海',
        'pages': [{'url': 'https://sh.centanet.com/xiaoqu/', 'kind': 'list'},
                  {'url': 'https://sh.centanet.com/xiaoqu/g2/', 'kind': 'list'},
                  {'url': 'https://sh.centanet.com/xiaoqu/g100/', 'kind': 'list'},
                  {'url': 'https://sh.centanet.com/xiaoqu/t2/', 'kind': 'list'},
                  # Detail pages: 仁恒滨江园(2期), 中环和润苑, 锦秋花园(别墅), 龙湖郦城
                  {'url': 'https://sh.centanet.com/xiaoqu/xq-kbhdpxsvxt/', 'kind': 'detail',
                   'expected_name': '仁恒滨江园(2期)', 'expected_source_id': 'xq-kbhdpxsvxt'},
                  {'url': 'https://sh.centanet.com/xiaoqu/xq-pedawkwyws/', 'kind': 'detail',
                   'expected_name': '中环和润苑', 'expected_source_id': 'xq-pedawkwyws'},
                  {'url': 'https://sh.centanet.com/xiaoqu/xq-peypwkwyys/', 'kind': 'detail',
                   'expected_name': '锦秋花园(别墅)', 'expected_source_id': 'xq-peypwkwyys'},
                  # B9: t2 list link 龙湖郦城(云屏路88弄) -> xq-peeewgwyws; xq-peeewgwyys is 联丰路47弄.
                  {'url': 'https://sh.centanet.com/xiaoqu/xq-peeewgwyws/', 'kind': 'detail',
                   'expected_name': '龙湖郦城', 'expected_source_id': 'xq-peeewgwyws'}],
    },
}

# Offline snapshot name -> (source, city, parser kind).
OFFLINE_MAP = {
    'lizihang_bj_loupan': ('丽兹行', '北京', 'lizihang'),
    'lizihang_sh_loupan': ('丽兹行', '上海', 'lizihang'),
    'centanet_sh': ('中原', '上海', 'centanet'),
    'centanet_sh_villa': ('中原', '上海', 'centanet'),
    'centanet_sh_villa_last': ('中原', '上海', 'centanet'),
    'centanet_sh_jinqiu': ('中原', '上海', 'centanet'),
    'centanet_sh_longhu': ('中原', '上海', 'centanet'),
    'centanet_sh_detail': ('中原', '上海', 'centanet'),
}

COMBO_OF = {
    ('丽兹行', '北京'): 'bj_lizihang',
    ('丽兹行', '上海'): 'sh_lizihang',
    ('中原', '上海'): 'sh_centanet',
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_epoch():
    """Wall-clock seconds; injectable so tests never touch real time."""
    return time.time()


def _sleep(seconds):
    """Sleep hook; injectable so tests never actually sleep."""
    time.sleep(seconds)


def extract_phase(name):
    if not name:
        return None
    match = re.search(r'([一二三四五六七八九十\d]+)\s*(?:期|分期|组)', name)
    return match.group(0).strip() if match else None


def phase_number(name):
    if not name:
        return None
    match = re.search(r'([一二三四五六七八九十\d]+)\s*(?:期|分期|组)', name)
    if not match:
        return None
    raw = match.group(1)
    return CN_NUM.get(raw, raw)


def strip_phase(name):
    """Base community name with phase markers removed, for cross-source matching."""
    if not name:
        return ''
    text = re.sub(r'[（(]\s*[一二三四五六七八九十\d]+\s*(?:期|分期|组)\s*[)）]', '', name)
    text = re.sub(r'[一二三四五六七八九十\d]+\s*(?:期|分期|组)', '', text)
    text = re.sub(r'[\s\-—·]+', '', text)
    return text


def is_plain_residence_tag(tag):
    return str(tag or '').strip() in PLAIN_RESIDENCE_TAGS


def is_villa_tag(tag):
    if is_plain_residence_tag(tag):
        return False
    return any(word in tag for word in VILLA_TAGS)


def _is_unknown_type_tag(tag):
    return (tag or '').strip() in UNKNOWN_TYPE_TAGS


def villa_subtypes_of(tags):
    return [t for t in (tags or [])
            if not is_plain_residence_tag(t) and any(s in t for s in VILLA_SUBTYPE_TAGS)]


def has_villa_type(tags):
    return any(is_villa_tag(t) for t in (tags or []) if not _is_unknown_type_tag(t))


def _is_nonvilla_tag(tag):
    return (not _is_unknown_type_tag(tag)) and (not is_villa_tag(tag)) \
        and any(w in tag for w in NON_VILLA_TAGS)


def has_nonvilla_type(tags):
    return any(_is_nonvilla_tag(t) for t in (tags or []))


def classify_mixed_state(tags):
    """mixed=普通楼房/公寓 AND villa; pure_villa=villa only; non_villa=neither; unknown=no usable tag."""
    tags = list(tags or [])
    if not tags or all(_is_unknown_type_tag(t) for t in tags):
        return 'unknown'
    villa = has_villa_type(tags)
    non_villa = has_nonvilla_type(tags)
    if villa and non_villa:
        return 'mixed'
    if villa:
        return 'pure_villa'
    if non_villa:
        return 'non_villa'
    return 'unknown'


def villa_clue_reasons(name=None, osm_tags=None, is_former_residence=False):
    """Name/OSM/故居 are CLUES only, never proof of a commodity residential villa community."""
    reasons = []
    if name and '别墅' in name:
        reasons.append(CLUE_NAME_NOTE)
    for value in (osm_tags or []):
        if str(value).strip().lower() in CLUE_OSM_VALUES:
            reasons.append(CLUE_OSM_NOTE)
            break
    if is_former_residence:
        reasons.append(CLUE_RESIDENCE_NOTE)
    return reasons


def combo_of(source, city):
    return COMBO_OF.get((source, city), f'{city}_{source}')


def slug_of(url):
    path = urlsplit(url).path.strip('/')
    cleaned = re.sub(r'[^0-9A-Za-z]+', '_', path).strip('_')
    return cleaned or 'root'


def _is_empty(value):
    """True for placeholder values a later record is allowed to fill in."""
    return value is None or value == '' or value == [] or value == {}


def merge_observation(target, extra):
    """A later record fills only the kept row's empty fields; never nulls an existing value.

    A detail record can supply coords_raw/coord_snapshot_path/detail_snapshot_path/
    address/year_value that a list card lacked.
    """
    for key, value in extra.items():
        if _is_empty(target.get(key)) and not _is_empty(value):
            target[key] = value
    # Detail evidence upgrades the row and records the snapshot the merged fields came from.
    if extra.get('record_kind') == 'detail' and target.get('record_kind') != 'detail':
        target['record_kind'] = 'detail'
    if extra.get('coords_raw'):
        for field in ('coord_snapshot_path', 'detail_snapshot_path'):
            if _is_empty(target.get(field)) and not _is_empty(extra.get(field)):
                target[field] = extra[field]
        if _is_empty(target.get('coord_snapshot_path')) and extra.get('snapshot_path'):
            target['coord_snapshot_path'] = extra['snapshot_path']
            target['detail_snapshot_path'] = extra['snapshot_path']


def observation_key(row):
    """Stable identity: source_id when present, otherwise the raw community name.

    A5: an id-less pending card must not auto-merge with a same-name card from another page,
    so pending rows also key on the snapshot/list URL they were parsed from.
    """
    if row.get('source_id'):
        return (row['source'], row['city'], row['source_id'])
    if row.get('link_method') == LIZIHANG_PENDING:
        return (row['source'], row['city'], row['name'], row.get('snapshot_path'),
                row.get('list_url'))
    return (row['source'], row['city'], row['name'])


def dedup_observations(rows):
    seen = {}
    unique = []
    for row in rows:
        key = observation_key(row)
        if key in seen:
            merge_observation(seen[key], row)
            continue
        seen[key] = row
        unique.append(row)
    return unique


def group_observations(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['city'], strip_phase(row['name']), phase_number(row['name']))].append(row)
    return groups


def _norm_address(value):
    if not value:
        return ''
    text = str(value).replace('（', '(').replace('）', ')')
    return re.sub(r'[\s\-—·,，]', '', text)


# A leading 省/市/区/县 prefix is administrative scope, never part of the door token.
_ADMIN_PREFIX_RE = re.compile(r'^[\u4e00-\u9fff]{2,3}(?:省|自治区|特别行政区|市|区|县|旗)')
# First street/弄 token with a house number; the optional 弄/号/支弄 suffix is kept.
_HOUSE_TOKEN_RE = re.compile(r'[\u4e00-\u9fff]{1,8}?(?:大道|大街|路|街|道|弄|巷)\d+(?:支弄|弄|号)?')


def _strip_admin_prefix(text):
    """Drop leading 省/市/区/县 prefixes so two spellings share a door token."""
    for _ in range(4):
        stripped = _ADMIN_PREFIX_RE.sub('', text)
        if stripped == text:
            break
        text = stripped
    return text


def house_number(address):
    """Normalized 门牌/door token: first `路|街|道|弄|巷` + digits (+ 弄/号/支弄).

    '太平街8号(陶然亭公园西侧)' and '北京市西城区太平街8号' both yield '太平街8号'.
    An address without a door token returns '' so a missing door can never match.
    """
    text = _norm_address(address)
    if not text:
        return ''
    match = _HOUSE_TOKEN_RE.search(_strip_admin_prefix(text))
    return match.group(0) if match else ''


def _url_slug(url):
    """The xq-... slug in a centanet URL path, or None when the path carries none."""
    if not url:
        return None
    match = DETAIL_ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


def _norm_identity(value):
    if not value:
        return ''
    text = str(value).replace('（', '(').replace('）', ')')
    return re.sub(r'[\s\-—·]', '', text)


def _name_matches(expected_name, actual_name):
    """Expected name equals the parsed name or is a substring of it (phase suffix is ok)."""
    expected_text = _norm_identity(expected_name)
    actual_text = _norm_identity(actual_name)
    if not expected_text or not actual_text:
        return None
    return expected_text == actual_text or expected_text in actual_text


def _identity_field(expected_value, actual_value, match):
    if not expected_value or not actual_value:
        return {'expected': expected_value, 'actual': actual_value, 'match': None}
    return {'expected': expected_value, 'actual': actual_value,
            'match': bool(match(expected_value, actual_value))}


def check_object_consistency(row, expected):
    """Prove a parsed detail row is the intended community, not merely a parseable page.

    source_id and the final URL slug must equal the expected seed exactly; the expected
    name must equal the parsed name or appear as its substring; address is compared only
    when both sides carry one. A missing expected/actual identity field -> 'undetermined';
    a present-but-different field -> 'object_mismatch'.
    """
    expected = expected or {}
    row = row or {}
    expected_id = expected.get('source_id')
    expected_name = expected.get('name')
    fields = {
        'source_id': _identity_field(expected_id, row.get('source_id'),
                                     lambda a, b: a == b),
        'slug': _identity_field(expected_id, _url_slug(row.get('source_url')),
                                lambda a, b: a == b),
        'name': _identity_field(expected_name, row.get('name'), _name_matches),
    }
    expected_address = expected.get('address')
    actual_address = row.get('address')
    if expected_address and actual_address:
        fields['address'] = {'expected': expected_address, 'actual': actual_address,
                             'match': _norm_address(expected_address)
                             == _norm_address(actual_address)}
    else:
        fields['address'] = {'expected': expected_address, 'actual': actual_address,
                             'match': None}
    if any(field['match'] is False for field in fields.values()):
        status = 'object_mismatch'
    elif any(field['match'] is None for key, field in fields.items() if key != 'address'):
        status = 'undetermined'
    else:
        status = 'ok'
    return {'status': status, 'fields': fields}


def association_signals(a, b):
    base_a, base_b = strip_phase(a.get('name')), strip_phase(b.get('name'))
    name_match = bool(base_a) and base_a == base_b
    addr_a, addr_b = _norm_address(a.get('address')), _norm_address(b.get('address'))
    door_a, door_b = house_number(a.get('address')), house_number(b.get('address'))
    house_match = bool(door_a) and bool(door_b) and door_a == door_b
    phase_a, phase_b = phase_number(a.get('name')), phase_number(b.get('name'))
    return {
        'name_match': name_match,
        'alias_match': name_match,
        'address_match': bool(addr_a) and addr_a == addr_b,
        # A shared door token proves the same place even when the raw address text differs.
        'house_match': house_match,
        'address_conflict': (bool(addr_a) and bool(addr_b) and addr_a != addr_b
                             and not house_match),
        'phase_match': phase_a is not None and phase_a == phase_b,
    }


def associate_pair(a, b, source_mapping=False):
    """Candidate vs confirmed. Confirmed needs >=2 positive signals and NO address conflict."""
    signals = association_signals(a, b)
    signals['source_mapping'] = bool(source_mapping)
    positive = sum(1 for k in ('name_match', 'address_match', 'house_match',
                               'phase_match', 'source_mapping')
                   if signals[k])
    if signals['address_conflict']:
        level = 'candidate'
    elif positive >= CONFIRM_MIN_SIGNALS:
        level = 'confirmed'
    elif positive >= 1:
        level = 'candidate'
    else:
        level = 'rejected'
    reasons = [k for k, v in signals.items() if v and k != 'address_conflict']
    if signals['address_conflict']:
        reasons.append('address_conflict')
    return {'level': level, 'may_transfer': level == 'confirmed',
            'positive_signals': positive, 'min_signals': CONFIRM_MIN_SIGNALS,
            'signals': signals, 'reason': reasons,
            'reason_text': level + ':' + ('+'.join(reasons) if reasons else 'no_signal')}


def associate_group(members, mappings=None):
    by_source = defaultdict(list)
    for m in members:
        by_source[m.get('source')].append(m)
    sources = sorted(by_source)
    if len(sources) < 2:
        return {'level': 'single_source', 'may_transfer': False, 'pairs': [],
                'positive_signals': 0, 'min_signals': CONFIRM_MIN_SIGNALS,
                'reason_text': 'single_source'}
    pairs = []
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            a, b = by_source[sources[i]][0], by_source[sources[j]][0]
            mapping = bool(mappings and tuple(sorted((sources[i], sources[j]))) in mappings)
            pair = associate_pair(a, b, mapping)
            pair['sources'] = [sources[i], sources[j]]
            pairs.append(pair)
    if all(p['level'] == 'confirmed' for p in pairs):
        level = 'confirmed'
    elif all(p['level'] != 'rejected' for p in pairs):
        level = 'candidate'
    else:
        level = 'rejected'
    return {'level': level, 'may_transfer': level == 'confirmed', 'pairs': pairs,
            'positive_signals': max(p['positive_signals'] for p in pairs),
            'min_signals': CONFIRM_MIN_SIGNALS,
            'reason_text': level + ':' + '|'.join(p['reason_text'] for p in pairs)}


def may_transfer(association, field=None):
    ok = bool((association or {}).get('may_transfer'))
    if field is not None:
        ok = ok and field in TRANSFERABLE_FIELDS
    return ok


def transfer_confirmed(target, donor, association, fields):
    """Only a confirmed association may move year/coords; otherwise return [] and change nothing."""
    if not may_transfer(association):
        return []
    moved = []
    for field in fields:
        if not may_transfer(association, field):
            continue
        if _is_empty(target.get(field)) and not _is_empty(donor.get(field)):
            target[field] = donor[field]
            moved.append(field)
    return moved


def backfill_position(target, donor, association):
    return transfer_confirmed(target, donor, association, ['coords_raw'])


def inherit_year(target, donor, association):
    return transfer_confirmed(target, donor, association, ['year_value', 'year_text'])


def _type_roots(tags):
    roots = set()
    if has_villa_type(tags):
        roots.add('villa')
    if has_nonvilla_type(tags):
        roots.add('non_villa')
    return roots


def compare_type_tags(tags_a, tags_b):
    """Containment comparison. Missing/unknown is NOT a match; disjoint roots is a conflict."""
    a, b = list(tags_a or []), list(tags_b or [])
    if not _type_roots(a) or not _type_roots(b):
        return {'state': 'unknown', 'reason': 'missing_type'}
    if _type_roots(a).isdisjoint(_type_roots(b)):
        return {'state': 'conflict', 'reason': 'explicit_difference'}
    if _type_roots(a) == _type_roots(b):
        return {'state': 'consistent', 'reason': 'same_root'}
    return {'state': 'consistent', 'reason': 'containment'}


def compare_group_types(members):
    tags_list = [list(m.get('type_tags') or []) for m in members]
    if len(tags_list) < 2:
        return {'state': 'single_source', 'reason': 'single_source'}
    states = []
    for i in range(len(tags_list)):
        for j in range(i + 1, len(tags_list)):
            states.append(compare_type_tags(tags_list[i], tags_list[j]))
    if any(s['state'] == 'conflict' for s in states):
        return {'state': 'conflict', 'reason': 'explicit_difference'}
    if any(s['state'] == 'unknown' for s in states):
        return {'state': 'unknown', 'reason': 'missing_type'}
    return {'state': 'consistent', 'reason': 'containment_or_same'}


# ---------------------------------------------------------------------------
# bounded JS literal parser (docs §4.1 / B1 / B2)
#
# Remote scripts are parsed as DATA, never evaluated: no eval/exec/Function/JS engine.
# A recursive-descent reader handles exactly the literal grammar Nuxt/中原 emit
# (null/bool/number/string/array/object/identifier-reference) and returns the UNRESOLVED
# sentinel for any identifier it cannot resolve from the param->arg environment.
# ---------------------------------------------------------------------------
class _ParseError(ValueError):
    """Malformed embedded literal; callers degrade instead of executing remote JS."""


_JS_NUMBER_RE = re.compile(r'-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')
_JS_WS = ' \t\r\n\f\v'
_JS_IDENT_START = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$')
_JS_IDENT_BODY = _JS_IDENT_START | set('0123456789')
_JS_PAIRS = {'{': '}', '[': ']', '(': ')'}
_JS_CLOSERS = set(_JS_PAIRS.values())


def _to_text(html):
    """Decode a snapshot body to text once; accept bytes or str."""
    if isinstance(html, (bytes, bytearray)):
        return html.decode('utf-8', 'ignore')
    return html


def _skip_ws(text, pos):
    """Skip whitespace and // or /* */ comments (minified payloads may inline comments)."""
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch in _JS_WS:
            pos += 1
        elif ch == '/' and pos + 1 < n and text[pos + 1] == '/':
            newline = text.find('\n', pos)
            pos = n if newline < 0 else newline + 1
        elif ch == '/' and pos + 1 < n and text[pos + 1] == '*':
            end = text.find('*/', pos + 2)
            pos = n if end < 0 else end + 2
        else:
            break
    return pos


def _skip_string(text, pos):
    """Return the index after a quoted string without decoding; never raises."""
    quote = text[pos]
    i = pos + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _scan_balanced(text, pos):
    """Index just past the bracket that closes text[pos] (one of { [ (); -1 if unbalanced."""
    opener = text[pos]
    if opener not in _JS_PAIRS:
        return -1
    depth = 0
    i = pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'':
            i = _skip_string(text, i)
            continue
        if ch in _JS_PAIRS:
            depth += 1
        elif ch in _JS_CLOSERS:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _read_ident(text, pos):
    n = len(text)
    i = pos
    while i < n and text[i] in _JS_IDENT_BODY:
        i += 1
    return text[pos:i], i


def _ident_continues(text, pos):
    """True if a keyword match is really an identifier prefix (e.g. `nullish`)."""
    return pos < len(text) and text[pos] in _JS_IDENT_BODY


def _parse_js_string(text, pos):
    quote = text[pos]
    i = pos + 1
    out = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\':
            nxt = text[i + 1] if i + 1 < n else ''
            if nxt == 'u' and i + 5 < n:
                try:
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                except ValueError:
                    out.append(text[i + 2:i + 6])
                i += 6
            elif nxt == 'x' and i + 3 < n:
                try:
                    out.append(chr(int(text[i + 2:i + 4], 16)))
                except ValueError:
                    pass
                i += 4
            elif nxt == 'n':
                out.append('\n')
                i += 2
            elif nxt == 't':
                out.append('\t')
                i += 2
            elif nxt == 'r':
                out.append('\r')
                i += 2
            elif nxt == 'b':
                out.append('\b')
                i += 2
            elif nxt == 'f':
                out.append('\f')
                i += 2
            elif nxt in ('/', '\\', '"', "'"):
                out.append(nxt)
                i += 2
            else:
                out.append(nxt)
                i += 2
        elif ch == quote:
            return ''.join(out), i + 1
        else:
            out.append(ch)
            i += 1
    raise _ParseError('unterminated string')


def _parse_js_number(text, pos):
    match = _JS_NUMBER_RE.match(text, pos)
    if not match:
        raise _ParseError('bad number')
    raw = match.group(0)
    if '.' in raw or 'e' in raw or 'E' in raw:
        return float(raw), match.end()
    return int(raw), match.end()


def _skip_js_value(text, pos):
    """Best-effort skip of an unsupported value to the next top-level , } ] or )."""
    depth = 0
    i = pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'':
            i = _skip_string(text, i)
            continue
        if ch in _JS_PAIRS:
            depth += 1
        elif ch in _JS_CLOSERS:
            if depth == 0:
                return i
            depth -= 1
        elif ch == ',' and depth == 0:
            return i
        i += 1
    return n


def _parse_js_array(text, pos, env):
    i = _skip_ws(text, pos + 1)
    items = []
    if i < len(text) and text[i] == ']':
        return items, i + 1
    while i < len(text):
        if text[i] == ']':
            return items, i + 1
        start = i
        try:
            value, i = _parse_js_value(text, start, env)
        except _ParseError:
            value = UNRESOLVED
            i = _skip_js_value(text, start)
        items.append(value)
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ',':
            i = _skip_ws(text, i + 1)
            continue
        if i < len(text) and text[i] == ']':
            return items, i + 1
        raise _ParseError('bad array')
    raise _ParseError('unterminated array')


def _parse_js_object(text, pos, env):
    i = _skip_ws(text, pos + 1)
    result = {}
    if i < len(text) and text[i] == '}':
        return result, i + 1
    while i < len(text):
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == '}':
            return result, i + 1
        if i < len(text) and text[i] in '"\'':
            key, i = _parse_js_string(text, i)
        elif i < len(text) and text[i] in _JS_IDENT_START:
            key, i = _read_ident(text, i)
        elif i < len(text) and (text[i].isdigit() or text[i] == '-'):
            key, i = _parse_js_number(text, i)
        else:
            raise _ParseError('bad object key')
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ':':
            vpos = _skip_ws(text, i + 1)
            try:
                value, i = _parse_js_value(text, vpos, env)
            except _ParseError:
                value = UNRESOLVED
                i = _skip_js_value(text, vpos)
        else:
            value = env.get(key, UNRESOLVED)
        result[str(key)] = value
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ',':
            i = _skip_ws(text, i + 1)
            continue
        if i < len(text) and text[i] == '}':
            return result, i + 1
        raise _ParseError('bad object')
    raise _ParseError('unterminated object')


def _parse_js_value(text, pos, env):
    """Recursive-descent literal reader. Returns (value, next_pos)."""
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        raise _ParseError('unexpected end')
    ch = text[pos]
    if ch == '{':
        return _parse_js_object(text, pos, env)
    if ch == '[':
        return _parse_js_array(text, pos, env)
    if ch in '"\'':
        return _parse_js_string(text, pos)
    if ch == '!':
        nxt = _skip_ws(text, pos + 1)
        if nxt < len(text) and text[nxt] in '01':
            return text[nxt] == '0', nxt + 1
        raise _ParseError('unary !')
    if ch == '-' or ch.isdigit() or (ch == '.' and pos + 1 < len(text)
                                     and text[pos + 1].isdigit()):
        return _parse_js_number(text, pos)
    if text.startswith('null', pos) and not _ident_continues(text, pos + 4):
        return None, pos + 4
    if text.startswith('true', pos) and not _ident_continues(text, pos + 4):
        return True, pos + 4
    if text.startswith('false', pos) and not _ident_continues(text, pos + 5):
        return False, pos + 5
    if text.startswith('undefined', pos) and not _ident_continues(text, pos + 9):
        return None, pos + 9
    if text.startswith('void', pos) and not _ident_continues(text, pos + 4):
        nxt = _skip_ws(text, pos + 4)
        if nxt < len(text) and text[nxt] == '0':
            return None, nxt + 1
        raise _ParseError('void')
    if ch in _JS_IDENT_START:
        ident, npos = _read_ident(text, pos)
        # `function`/`new` expressions are not literals; let the caller skip them.
        if ident in ('function', 'new'):
            raise _ParseError(f'unsupported expression {ident!r}')
        return env.get(ident, UNRESOLVED), npos
    raise _ParseError(f'unexpected {ch!r} at {pos}')


def _parse_js_literal(text, env=None):
    """Parse one literal from the head of text; identifier refs resolve via env."""
    env = env or {}
    pos = _skip_ws(text, 0)
    if pos >= len(text):
        return None
    value, _ = _parse_js_value(text, pos, env)
    return value


def _split_js_args(text):
    """Split a top-level comma list (function params or call args) without evaluation."""
    if not text.strip():
        return []
    args = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'':
            i = _skip_string(text, i)
            continue
        if ch in _JS_PAIRS:
            depth += 1
        elif ch in _JS_CLOSERS:
            depth -= 1
        elif ch == ',' and depth == 0:
            args.append(text[start:i])
            start = i + 1
        i += 1
    args.append(text[start:])
    return args


def parse_nuxt_payload(html):
    """Extract the list embedded in window.__NUXT__ as data. Returns {'var', 'items'} or None.

    The payload is `(function(a,b,...){ body }(arg1,arg2,...))`; the body assigns
    `<ident>.list=[...]` where <ident> varies per page. Identifier references inside the
    list resolve through the param->arg environment; anything unresolved stays UNRESOLVED.
    """
    raw = _to_text(html)
    marker = re.search(r'window\.__NUXT__\s*=', raw)
    if not marker:
        return None
    i = _skip_ws(raw, marker.end())
    if i >= len(raw) or raw[i] != '(':
        return None
    i = _skip_ws(raw, i + 1)
    if not raw.startswith('function', i):
        return None
    i = _skip_ws(raw, i + len('function'))
    if i >= len(raw) or raw[i] != '(':
        return None
    params_end = _scan_balanced(raw, i)
    if params_end < 0:
        return None
    params = [p.strip() for p in _split_js_args(raw[i + 1:params_end - 1]) if p.strip()]
    i = _skip_ws(raw, params_end)
    if i >= len(raw) or raw[i] != '{':
        return None
    body_end = _scan_balanced(raw, i)
    if body_end < 0:
        return None
    body = raw[i + 1:body_end - 1]
    i = _skip_ws(raw, body_end)
    if i >= len(raw) or raw[i] != '(':
        return None
    args_end = _scan_balanced(raw, i)
    if args_end < 0:
        return None
    args = _split_js_args(raw[i + 1:args_end - 1])

    env = {}
    for name, arg_text in zip(params, args):
        try:
            env[name] = _parse_js_literal(arg_text, env)
        except _ParseError:
            env[name] = UNRESOLVED

    for match in re.finditer(r'([A-Za-z_$][\w$]*)\s*\.\s*list\s*=\s*\[', body):
        ident = match.group(1)
        array_start = body.index('[', match.start())
        try:
            items, _ = _parse_js_value(body, array_start, env)
        except _ParseError:
            continue
        if isinstance(items, list) and any(
                isinstance(item, dict) and 'resblockName' in item for item in items):
            return {'var': ident, 'items': items}
    return None


def _record_field(item, field, unresolved):
    """Read one field; missing key or unresolved ref is recorded, never guessed."""
    value = item.get(field, _MISSING)
    if value is _MISSING or value is UNRESOLVED:
        unresolved.append(field)
        return None
    return value


def _to_float(value):
    if value is None or value is UNRESOLVED or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_build_year(raw_value, unresolved):
    """Resolve buildYear to int, or flag it; never infer from another field."""
    year = None
    if isinstance(raw_value, str):
        match = re.search(r'(\d{4})', raw_value)
        if match:
            year = int(match.group(1))
    elif isinstance(raw_value, int) and not isinstance(raw_value, bool):
        year = raw_value
    if year is None and 'buildYear' not in unresolved:
        unresolved.append('buildYear')
    return year


def lizihang_image_key(url):
    """Page-level key: the image file name shared by a card <img> and the NUXT fileUrl.

    The card renders a resized variant (e.g. `....jpg.450x300.jpg`); stripping the
    trailing `.<w>x<h>.<ext>` suffix yields the stored file name. Returns a lowercased
    basename or None.
    """
    if not isinstance(url, str) or not url:
        return None
    path = urlsplit(url).path if '://' in url else url
    name = path.rsplit('/', 1)[-1]
    name = LIZIHANG_IMAGE_SIZE_RE.sub('', name)
    return name.lower() or None


def lizihang_embedded_records(html, snapshot_path):
    """Normalized embedded `list` objects from window.__NUXT__ (address/coords/year)."""
    payload = parse_nuxt_payload(html)
    if not payload:
        return []
    var = payload['var']
    records = []
    for index, item in enumerate(payload['items']):
        if not isinstance(item, dict):
            continue

        def pointer(field):
            return f'{snapshot_path}::window.__NUXT__.{var}.list[{index}].{field}'

        unresolved = []
        name = _record_field(item, 'resblockName', unresolved)
        admin_addr = _record_field(item, 'adminAddr', unresolved)
        map_x = _record_field(item, 'mapX', unresolved)
        map_y = _record_field(item, 'mapY', unresolved)
        build_year_raw = _record_field(item, 'buildYear', unresolved)
        build_year = _parse_build_year(build_year_raw, unresolved)

        code = item.get('resblockCode')
        resblock_code = code if isinstance(code, str) and code else None
        id_val = item.get('id')
        if isinstance(id_val, int) and not isinstance(id_val, bool):
            source_id = str(id_val)
        elif isinstance(id_val, str) and id_val:
            source_id = id_val
        elif resblock_code:
            source_id = resblock_code
        else:
            source_id = None

        file_url = item.get('fileUrl') if isinstance(item.get('fileUrl'), str) else None
        image_key = lizihang_image_key(file_url)

        evidence = {field: pointer(field) for field in
                    ('id', 'resblockCode', 'resblockName', 'adminAddr', 'mapX', 'mapY',
                     'buildYear', 'fileUrl') if field in item}
        records.append({
            'source_id': source_id,
            'resblock_code': resblock_code,
            'resblock_name': name,
            'admin_addr': admin_addr,
            'build_year_raw': build_year_raw,
            'build_year': build_year,
            'map_x': map_x,
            'map_y': map_y,
            'file_url': file_url,
            'image_key': image_key,
            'evidence': evidence,
            'unresolved': unresolved,
        })
    return records


def validate_coord(city, lng, lat):
    """Plausibility check against the city window; a raw system-unverified value only."""
    lng = _to_float(lng)
    lat = _to_float(lat)
    if lng is None or lat is None:
        return False
    bounds = COORD_RANGES.get(city)
    if not bounds:
        return False
    min_lng, max_lng, min_lat, max_lat = bounds
    return min_lng <= lng <= max_lng and min_lat <= lat <= max_lat


def parse_centanet_map_post_data(html, snapshot_path, city='上海'):
    """Parse `var mapPostData = [...]` as data (no eval); invalid points are flagged, not dropped."""
    raw = _to_text(html)
    match = re.search(r'var\s+mapPostData\s*=\s*\[', raw)
    if not match:
        return []
    array_start = raw.index('[', match.start())
    array_end = _scan_balanced(raw, array_start)
    if array_end < 0:
        return []
    try:
        entries = _parse_js_literal(raw[array_start:array_end])
    except _ParseError:
        return []
    if not isinstance(entries, list):
        return []

    records = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        unresolved = []

        def pointer(field):
            return f'{snapshot_path}::mapPostData[{index}].{field}'

        lng = _to_float(entry.get('Lng'))
        lat = _to_float(entry.get('Lat'))
        if lng is None:
            unresolved.append('Lng')
        if lat is None:
            unresolved.append('Lat')
        name = entry.get('Name') if isinstance(entry.get('Name'), str) else None
        if name is None:
            unresolved.append('Name')
        price = entry.get('Price') if isinstance(entry.get('Price'), str) else None
        url = entry.get('Url') if isinstance(entry.get('Url'), str) else None
        if url is None:
            unresolved.append('Url')
        source_id = None
        if url:
            id_match = re.search(r'/xiaoqu/(xq-[a-z0-9]+)/', url)
            if id_match:
                source_id = id_match.group(1)
        if source_id is None:
            unresolved.append('source_id')

        valid = validate_coord(city, lng, lat)
        evidence = {field: pointer(field) for field in ('Lat', 'Lng', 'Name', 'Price', 'Url')
                    if field in entry}
        records.append({
            'source_id': source_id,
            'name': name,
            'lng': lng,
            'lat': lat,
            'price': price,
            'url': url,
            'coord_valid': valid,
            'coord_issue': None if valid else 'out_of_range',
            'evidence': evidence,
            'unresolved': unresolved,
        })
    return records


# ---------------------------------------------------------------------------
# parsers (pure: html, list_url, city, source, fetched_at, origin, snapshot_path)
# ---------------------------------------------------------------------------
def build_observation(source, city, source_id, name, district, subarea, address, year_text,
                      year_value, type_raw, type_tags, source_url, list_url, fetched_at,
                      origin, snapshot_path, price_raw=None, listings_raw=None, metro_raw=None,
                      record_kind='list_card', coords_raw=None, coord_system_evidence=None,
                      coord_snapshot_path=None, detail_snapshot_path=None,
                      coord_raw_system=None, coord_distance_ready=False, link_method=None,
                      link_evidence=None,
                      unresolved=None, evidence=None, clue_villa=None, clue_evidence=None,
                      osm_tags=None, is_former_residence=False):
    tags = list(type_tags or [])
    subtypes = villa_subtypes_of(tags)
    state = classify_mixed_state(tags)
    has_v = has_villa_type(tags)
    clue_reasons = list(clue_evidence or [])
    if clue_villa is None:
        clue_reasons = clue_reasons + villa_clue_reasons(name, osm_tags, is_former_residence)
    clue_v = bool(clue_reasons) and not has_v
    if has_v or state == 'non_villa':
        type_evidence = 'explicit'
    elif clue_v:
        type_evidence = 'clue'
    else:
        type_evidence = 'unknown'
    return {
        'source': source,
        'city': city,
        'source_id': source_id,
        'name': name,
        'district': district,
        'subarea': subarea,
        'phase': extract_phase(name),
        'address': address,
        'year_text': year_text,
        'year_value': year_value,
        'year_interpretation': YEAR_NOTE if year_value is not None else MISSING_YEAR_NOTE,
        'type_raw': type_raw,
        'type_tags': tags,
        'has_villa': has_v,
        'villa_subtypes': subtypes,
        'mixed_state': state,
        'clue_villa': clue_v,
        'clue_evidence': clue_reasons,
        'type_evidence': type_evidence,
        'is_mixed': state == 'mixed',
        'record_kind': record_kind,
        'coords_raw': coords_raw,
        'coord_system_evidence': coord_system_evidence,
        'coord_raw_system': coord_raw_system,
        'coord_distance_ready': coord_distance_ready,
        'link_method': link_method,
        'link_evidence': link_evidence,
        'unresolved': list(unresolved or []),
        'evidence': dict(evidence or {}),
        'source_url': source_url,
        'list_url': list_url,
        'fetched_at': fetched_at,
        'origin': origin,
        'snapshot_path': snapshot_path,
        'coord_snapshot_path': coord_snapshot_path,
        'detail_snapshot_path': detail_snapshot_path,
        'price_raw': price_raw,
        'listings_raw': listings_raw,
        'metro_raw': metro_raw,
    }


def _lizihang_card(card, host):
    """Visible card fields, keyed for a stable-id merge with embedded records."""
    name_node = card.select_one('h3')
    name = name_node.get_text(' ', strip=True) if name_node else None

    district = subarea = None
    mes = card.select_one('.houseMes > span') or card.select_one('.houseMes span')
    if mes:
        parts = [p.strip() for p in mes.get_text(' ', strip=True).split('·') if p.strip()]
        if parts:
            district = parts[0]
        if len(parts) > 1:
            subarea = parts[1]

    type_raw = None
    type_tags = []
    year_text = None
    year_value = None
    type_node = card.select_one('p.houseType') or card.select_one('p[class*="houseType"]')
    if type_node:
        segments = [s.strip() for s in type_node.get_text(' ', strip=True).split('·') if s.strip()]
        year_seg = next((s for s in segments if '年建成' in s), None)
        type_seg = next((s for s in segments if '年建成' not in s), None)
        year_text = year_seg
        if year_seg:
            match = re.search(r'(\d{4})\s*年建成', year_seg)
            if match:
                year_value = int(match.group(1))
        type_raw = type_seg
        if type_seg:
            type_tags = [t.strip() for t in type_seg.split('/') if t.strip()]

    price_node = card.select_one('p.price')
    price_raw = price_node.get_text(' ', strip=True) if price_node else None
    listings_node = card.select_one('[class*="SaleorRent"]')
    listings_raw = listings_node.get_text(' ', strip=True) if listings_node else None

    link = card.select_one("a[href*='-esf/?id=']") or card.select_one("a[href*='-zl/?id=']")
    source_id = None
    source_url = None
    if link is not None and link.has_attr('href'):
        href = link['href']
        query = parse_qs(urlsplit(href).query)
        if query.get('id'):
            source_id = unquote(query['id'][0])
        source_url = urljoin(host + '/', href)

    image_node = card.select_one('img[data-src]')
    image_src = image_node.get('data-src') if image_node else None
    image_key = lizihang_image_key(image_src)

    return {
        'source_id': source_id, 'name': name, 'district': district, 'subarea': subarea,
        'type_raw': type_raw, 'type_tags': type_tags, 'year_text': year_text,
        'year_value': year_value, 'price_raw': price_raw, 'listings_raw': listings_raw,
        'source_url': source_url, 'image_key': image_key, 'image_src': image_src,
    }


def _lizihang_pending_reason(card, records_by_key, cards_by_key):
    key = card.get('image_key')
    if not key:
        return PENDING_REASON_NO_IMAGE
    rindexes = records_by_key.get(key, [])
    if not rindexes:
        return PENDING_REASON_NO_MATCH
    if len(rindexes) > 1 or len(cards_by_key.get(key, [])) > 1:
        return PENDING_REASON_AMBIGUOUS
    return PENDING_REASON_CLAIMED


def parse_lizihang(html, list_url, city, source, fetched_at, origin, snapshot_path):
    """One observation per embedded list object, card fields merged by PROVABLE key only.

    B1: the window.__NUXT__ list carries adminAddr/mapX/mapY/buildYear that the visible
    cards lack. A5: cards whose 在售/在租 count is 0 have no href id, so they are associated
    to a record by the PAGE-LEVEL image key (card <img data-src> file name == fileUrl file
    name), never by name or array order. Cards still unprovable are emitted as
    pending_association rows.
    """
    raw = _to_text(html)
    doc = BeautifulSoup(raw, 'html.parser')
    host = '{0.scheme}://{0.netloc}'.format(urlsplit(list_url))

    cards = [_lizihang_card(node, host)
             for node in doc.select('ul.buildingUl > li.buildingList')]
    cards_by_id = {}
    for index, card in enumerate(cards):
        sid = card['source_id']
        if sid and sid not in cards_by_id:
            cards_by_id[sid] = index

    records = lizihang_embedded_records(raw, snapshot_path)
    records_by_key = defaultdict(list)
    for index, record in enumerate(records):
        if record['image_key']:
            records_by_key[record['image_key']].append(index)

    cards_by_key = defaultdict(list)
    for index, card in enumerate(cards):
        if card['image_key']:
            cards_by_key[card['image_key']].append(index)

    record_link = {}  # record_index -> (card_index, link_method, link_evidence)
    card_link = {}    # card_index -> record_index
    for index, record in enumerate(records):
        sid = record['source_id']
        card_index = cards_by_id.get(sid) if sid else None
        if card_index is None or card_index in card_link:
            continue
        card_link[card_index] = index
        record_link[index] = (card_index, 'stable_id',
                              {'method': 'stable_id', 'card_href_id': sid, 'record_id': sid})

    for key, rindexes in records_by_key.items():
        cindexes = cards_by_key.get(key, [])
        if len(rindexes) != 1 or len(cindexes) != 1:
            continue
        rindex, card_index = rindexes[0], cindexes[0]
        if rindex in record_link or card_index in card_link:
            continue
        record = records[rindex]
        card_link[card_index] = rindex
        record_link[rindex] = (card_index, LIZIHANG_PAGE_IMAGE, {
            'method': 'page_image', 'card_image_key': key,
            'record_file_url': record['file_url'],
            'record_evidence': record['evidence'].get('fileUrl'),
            'uniqueness': {'records_with_key': 1, 'cards_with_key': 1}})

    rows = []
    for index, record in enumerate(records):
        sid = record['source_id']
        link = record_link.get(index)
        card = cards[link[0]] if link else None
        link_method = link[1] if link else 'unlinked'
        link_evidence = link[2] if link else None

        name = record['resblock_name'] or (card['name'] if card else None)
        year_value = record['build_year']
        year_text = f'{year_value}年' if year_value is not None else None
        map_x = _to_float(record['map_x'])
        map_y = _to_float(record['map_y'])
        coords_raw = {'lng': map_x, 'lat': map_y} \
            if map_x is not None and map_y is not None else None
        unresolved = list(record['unresolved'])
        if card is None:
            unresolved.append('card')
        evidence = dict(record['evidence'])
        if card is not None:
            evidence['card'] = f'{snapshot_path}::card.{sid}'

        row = build_observation(
            source, city, sid or (card['source_id'] if card else None), name,
            card['district'] if card else None, card['subarea'] if card else None,
            record['admin_addr'], year_text, year_value,
            card['type_raw'] if card else None, card['type_tags'] if card else [],
            card['source_url'] if card else None, list_url, fetched_at, origin, snapshot_path,
            price_raw=card['price_raw'] if card else None,
            listings_raw=card['listings_raw'] if card else None,
            coords_raw=coords_raw, coord_raw_system=EMBEDDED_COORD_NOTE,
            coord_distance_ready=False, link_method=link_method,
            link_evidence=link_evidence, unresolved=unresolved, evidence=evidence)
        row['resblock_name'] = name
        row['admin_addr'] = record['admin_addr']
        row['map_x'] = record['map_x']
        row['map_y'] = record['map_y']
        row['build_year_raw'] = record['build_year_raw']
        row['resblock_code'] = record['resblock_code']
        row['card_url'] = card['source_url'] if card else None
        rows.append(row)

    for card_index, card in enumerate(cards):
        if card_index in card_link:
            continue
        sid = card['source_id']
        reason = _lizihang_pending_reason(card, records_by_key, cards_by_key)
        unresolved = ['embedded', reason]
        if not sid:
            unresolved.append('source_id')
        row = build_observation(
            source, city, sid, card['name'], card['district'], card['subarea'], None,
            card['year_text'], card['year_value'], card['type_raw'], card['type_tags'],
            card['source_url'], list_url, fetched_at, origin, snapshot_path,
            price_raw=card['price_raw'], listings_raw=card['listings_raw'],
            coord_distance_ready=False, link_method=LIZIHANG_PENDING,
            link_evidence={'page_proof': reason, 'image_key': card['image_key']},
            unresolved=unresolved)
        row['resblock_name'] = card['name']
        row['admin_addr'] = None
        row['map_x'] = None
        row['map_y'] = None
        row['build_year_raw'] = None
        row['resblock_code'] = None
        row['card_url'] = card['source_url']
        rows.append(row)
    return rows


def lizihang_association_counts(rows):
    """Three separate A5 counts; never summed into 新增小区.

    raw_rows            = rows emitted by the parser (original rows).
    stable_id_objects   = distinct non-null source_id objects.
    pending_association = rows with no source_id and link_method LIZIHANG_PENDING.
    Relation: raw_rows >= stable_id_objects and raw_rows >= pending_association.
    Equality raw_rows == stable_id_objects + pending_association is NOT required
    (id-less rows may still be 'unlinked'; ids may repeat across pages).
    """
    rows = list(rows or [])
    ids = {str(r['source_id']) for r in rows if r.get('source_id')}
    pending = sum(1 for r in rows
                  if not r.get('source_id')
                  and r.get('link_method') == LIZIHANG_PENDING)
    return {'raw_rows': len(rows), 'stable_id_objects': len(ids),
            'pending_association': pending}


def is_detail_page(raw, doc):
    """Detail pages carry one lowercase lng/lat (the subject coord) or an estateCode.

    B2: list pages also render div.house-item cards, so card presence cannot mark a list;
    only the lowercase subject coordinate (or estateCode) does. Recommended house-items on a
    detail page are therefore never attributed to the subject.
    """
    return bool(DETAIL_COORD_RE.search(raw)) or 'estateCode=' in raw


def parse_centanet_detail(html, list_url, city, source, fetched_at, origin, snapshot_path,
                          expected=None):
    raw = html.decode('utf-8', 'ignore') if isinstance(html, (bytes, bytearray)) else html
    doc = BeautifulSoup(raw, 'html.parser')
    coord = DETAIL_COORD_RE.search(raw)
    if not coord:
        return []
    text = doc.get_text(' ', strip=True)

    name = None
    title_match = DETAIL_TITLE_RE.search(raw)
    if title_match:
        name = title_match.group(1).strip() or None
    if not name and doc.title:
        name = doc.title.get_text(' ', strip=True) or None

    source_id = None
    id_match = DETAIL_ID_RE.search(urlsplit(list_url).path)
    if id_match:
        source_id = id_match.group(1)
    if not source_id:
        estate_match = DETAIL_ESTATE_RE.search(raw)
        if estate_match:
            source_id = f'xq-{estate_match.group(1)}'

    address = None
    address_match = re.search(r'小区地址[：:]\s*([^\s<]+)', text)
    if address_match:
        address = address_match.group(1).strip()
    else:
        line_match = re.search(r'】\s*([^【】]+?)\s*(?:分享给好友|小区图片)', text)
        if line_match:
            address = line_match.group(1).strip() or None

    year_value = None
    year_text = None
    year_match = re.search(r'建造年代[：:]\s*(\d{4})', raw) or \
        re.search(r'(\d{4})\s*年建成', raw)
    if year_match:
        year_value = int(year_match.group(1))
        year_text = f'{year_value}年'

    type_raw = None
    type_tags = []
    clue_evidence = villa_clue_reasons(name)

    coords_raw = {'lng': float(coord.group(1)), 'lat': float(coord.group(2))}
    row = build_observation(source, city, source_id, name, None, None, address,
                            year_text, year_value, type_raw, type_tags, list_url,
                            list_url, fetched_at, origin, snapshot_path,
                            record_kind='detail', coords_raw=coords_raw,
                            coord_system_evidence=DETAIL_COORD_NOTE,
                            coord_raw_system=DETAIL_COORD_NOTE, coord_distance_ready=False,
                            link_method='detail',
                            coord_snapshot_path=snapshot_path,
                            detail_snapshot_path=snapshot_path,
                            clue_villa=bool(clue_evidence), clue_evidence=clue_evidence)
    # B9: parsing success does not prove the intended object; record the identity check.
    row['object_consistency'] = check_object_consistency(row, expected)
    return [row]


def parse_centanet(html, list_url, city, source, fetched_at, origin, snapshot_path):
    """List pages merge `var mapPostData` coords by the SAME source_id only.

    B2: an unmatched map point is emitted as its own unlinked row; it is NEVER backfilled
    into a different card. Detail pages keep the subject's single coordinate.
    """
    raw = _to_text(html)
    doc = BeautifulSoup(raw, 'html.parser')
    if is_detail_url(list_url) or is_detail_page(raw, doc):
        return parse_centanet_detail(raw, list_url, city, source, fetched_at, origin, snapshot_path)

    map_by_id = {}
    unmatched_points = []
    for entry in parse_centanet_map_post_data(raw, snapshot_path, city):
        sid = entry['source_id']
        if sid and sid not in map_by_id:
            map_by_id[sid] = entry
        else:
            unmatched_points.append(entry)

    url_villa_clue = bool(T2_PATH_RE.search(urlsplit(list_url).path))
    rows = []
    matched_ids = set()
    for card in doc.select('div.house-item'):
        title_a = card.select_one('h4.house-title a')
        name = None
        href = None
        if title_a is not None:
            name = title_a.get('title') or title_a.get_text(' ', strip=True)
            href = title_a.get('href')
        source_id = None
        if href:
            match = re.search(r'/xiaoqu/(xq-[^/]+)/', href)
            if match:
                source_id = match.group(1)
        source_url = urljoin(list_url, href) if href else None

        district = subarea = None
        txts = card.select('.house-txt')
        if txts:
            anchors = txts[0].select('a')
            if len(anchors) >= 1:
                district = anchors[0].get_text(' ', strip=True)
            if len(anchors) >= 2:
                subarea = anchors[1].get_text(' ', strip=True)

        address = None
        for node in txts:
            if '建造年代' in node.get_text(' ', strip=True):
                continue
            span = node.select_one('span')
            if span:
                candidate = span.get_text(' ', strip=True)
                if candidate and '年' not in candidate:
                    address = candidate
                    break

        card_text = card.get_text(' ', strip=True)
        year_value = None
        year_text = None
        match = re.search(r'建造年代[：:]\s*(\d{4})\s*年', card_text)
        if match:
            year_value = int(match.group(1))
            year_text = f'{year_value}年'

        metro_match = re.search(r'距\s*(\d+)\s*号线\s*(.+?)\s*站\s*(\d+)\s*米', card_text)
        metro_raw = metro_match.group(0) if metro_match else None

        price_node = card.select_one('p.price-nub') or card.select_one('[class*="price-nub"]')
        price_raw = price_node.get_text(' ', strip=True) if price_node else None

        # A6: never turn the /t2/ path into an explicit 别墅 type; it is a clue only.
        type_raw = None
        type_tags = []

        entry = map_by_id.get(source_id) if source_id else None
        coords_raw = None
        coord_note = None
        unresolved = []
        evidence = {}
        link_method = 'card_only'
        if entry is not None:
            matched_ids.add(source_id)
            link_method = 'stable_id'
            if entry['lng'] is not None and entry['lat'] is not None:
                coords_raw = {'lng': entry['lng'], 'lat': entry['lat']}
            coord_note = CENTANET_EMBEDDED_COORD_NOTE
            unresolved = list(entry['unresolved'])
            evidence = dict(entry['evidence'])

        rows.append(build_observation(source, city, source_id, name, district, subarea, address,
                                      year_text, year_value, type_raw, type_tags, source_url,
                                      list_url, fetched_at, origin, snapshot_path,
                                      price_raw=price_raw, metro_raw=metro_raw,
                                      coords_raw=coords_raw, coord_raw_system=coord_note,
                                      coord_distance_ready=False, link_method=link_method,
                                      unresolved=unresolved, evidence=evidence,
                                      clue_villa=url_villa_clue,
                                      clue_evidence=([CLUE_URL_T2_NOTE] if url_villa_clue
                                                     else None)))

    for entry in unmatched_points:
        coords_raw = None
        if entry['lng'] is not None and entry['lat'] is not None:
            coords_raw = {'lng': entry['lng'], 'lat': entry['lat']}
        rows.append(build_observation(
            source, city, entry['source_id'], entry['name'], None, None, None, None, None,
            None, [], entry['url'], list_url, fetched_at, origin, snapshot_path,
            record_kind='map_point', price_raw=entry['price'],
            coords_raw=coords_raw, coord_raw_system=CENTANET_EMBEDDED_COORD_NOTE,
            coord_distance_ready=False, link_method='unlinked',
            unresolved=list(entry['unresolved']), evidence=dict(entry['evidence'])))
    return rows


PARSERS = {'lizihang': parse_lizihang, 'centanet': parse_centanet,
           'centanet_detail': parse_centanet_detail}


def is_detail_url(url):
    return bool(DETAIL_ID_RE.search(urlsplit(url).path))


def parser_for(source, kind, url):
    """Pick a parser from explicit kind, falling back to URL shape."""
    if source == '丽兹行':
        return PARSERS['lizihang']
    if source == '中原':
        return PARSERS['centanet_detail'] if kind == 'detail' or is_detail_url(url) \
            else PARSERS['centanet']
    return None


def centanet_scope(obs):
    """Split centanet records into the two catalog scopes the site advertises."""
    path = urlsplit(obs.get('list_url') or '').path
    if re.search(r'/t2(?:g\d+)?/?$', path):
        return 'villa'
    if obs.get('record_kind') == 'detail' and (obs.get('has_villa') or obs.get('clue_villa')):
        return 'villa'
    return 'default'


# ---------------------------------------------------------------------------
# jsonl + raw snapshot io
# ---------------------------------------------------------------------------
def write_jsonl(path, rows):
    """Atomic full-file write; callers pass the deduped union of previous and new rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(payload, encoding='utf-8')
    os.replace(tmp, path)


def read_jsonl(path):
    """Rows from an earlier run; a missing or empty artifact reads as no rows."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_jsonl(path, rows):
    """Append-only: creates parents, never truncates. Distinct from write_jsonl's atomic rewrite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def merge_observations(existing, incoming):
    """Union of accumulated + new observations, deduped by observation_key."""
    return dedup_observations(list(existing) + list(incoming))


def merge_requests(existing, incoming):
    """ONLY for content/source-object dedup (e.g. building communities.jsonl). NEVER use for the network request log; every real network attempt must be preserved, never deduped.

    Key is content identity when a sha256 exists, else source-object identity; first row wins.
    """
    merged = {}
    for row in list(existing) + list(incoming):
        if row.get('sha256'):
            key = ('content', row['sha256'])
        else:
            key = ('object', row.get('source'), row.get('source_id') or row.get('name'))
        merged.setdefault(key, row)
    return list(merged.values())


# ---------------------------------------------------------------------------
# persistent request budget (shared across processes; docs §4.2 / B7)
# ---------------------------------------------------------------------------
class BudgetLockError(RuntimeError):
    """Another process already holds the shared budget lock; fail fast, never wait."""


def _is_plain_int(value):
    """True only for a real int; bool (an int subclass) and float/str are rejected."""
    return isinstance(value, int) and not isinstance(value, bool)


def _default_platform_daily_caps():
    return {'丽兹行': PLATFORM_DAILY_CAP, '中原': PLATFORM_DAILY_CAP, '房天下': FANG_DAILY_CAP}


class BudgetStore:
    """Append-only, cross-process request budget with a single-writer lock.

    Every real network attempt (initial, redirect hop, retry, timeout) consumes one request_id.
    Counters persist in budget.json so restarting a process can never reset the trial total.
    """

    def __init__(self, run_state_dir, *, stage_caps=None, trial_total=TRIAL_TOTAL_CAP,
                 platform_daily_caps=None, platform_overrides=None,
                 combined_daily_cap=COMBINED_DAILY_CAP, min_interval_seconds=MIN_INTERVAL_SECONDS,
                 clock=_now_epoch, trial_date=DEFAULT_TRIAL_DATE):
        self.run_state_dir = Path(run_state_dir)
        self.budget_path = self.run_state_dir / 'budget.json'
        self.lock_path = self.run_state_dir / 'budget.lock'
        self.stage_caps = dict(stage_caps or STAGE_CAPS)
        self.trial_total = trial_total
        self.platform_daily_caps = dict(platform_daily_caps or _default_platform_daily_caps())
        self.platform_overrides = dict(platform_overrides or {})
        self.combined_daily_cap = combined_daily_cap
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self.trial_date = trial_date
        self._lock_owned = False

    # -- lock ---------------------------------------------------------------
    def acquire_lock(self):
        """Exclusive create; a held lock raises BudgetLockError instead of blocking."""
        self.run_state_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BudgetLockError(
                f'budget lock held: {self.lock_path} (pid {self._lock_pid()})')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'pid': os.getpid(), 'at': now_iso()}))
        self._lock_owned = True

    def _lock_pid(self):
        try:
            return json.loads(self.lock_path.read_text(encoding='utf-8')).get('pid')
        except (OSError, ValueError):
            return 'unknown'

    def release_lock(self):
        if self._lock_owned and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        self._lock_owned = False

    def __enter__(self):
        self.acquire_lock()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release_lock()
        return False

    # -- state io -----------------------------------------------------------
    def _default_state(self):
        return {
            'schema_version': 1,
            'trial_date': self.trial_date,
            'trial_total_cap': self.trial_total,
            'trial_used': 0,
            'next_request_id': 1,
            'stage_caps': dict(self.stage_caps),
            'stage_used': {},
            'platform_daily_caps': dict(self.platform_daily_caps),
            'platform_daily_used': {},
            'combined_daily_cap': self.combined_daily_cap,
            'combined_daily_used': {},
            'last_request_at': {},
            'budget_exhausted': [],
            'platform_paused': {},
            'updated_at': now_iso(),
        }

    def load(self):
        if not self.budget_path.exists():
            return self._default_state()
        return json.loads(self.budget_path.read_text(encoding='utf-8'))

    def _save(self, state):
        self.run_state_dir.mkdir(parents=True, exist_ok=True)
        state['updated_at'] = now_iso()
        tmp = self.budget_path.with_suffix(self.budget_path.suffix + '.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.budget_path)

    def _day_key(self, now):
        return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')

    def _end_of_day_epoch(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return day.timestamp() + 86400

    def pause_platform(self, platform, reason, until=None, now=None):
        """Pause a platform (default: rest of the UTC day) so reserve() refuses it."""
        now = self.clock() if now is None else now
        state = self.load()
        end = self._end_of_day_epoch(now) if until is None else until
        state.setdefault('platform_paused', {})[platform] = {
            'reason': reason, 'until': end,
            'paused_at': datetime.fromtimestamp(now, timezone.utc).isoformat()}
        state['budget_exhausted'].append({
            'at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'platform': platform, 'stage': None, 'combo': None,
            'requested_url': None, 'reason': 'platform_paused', 'detail': reason})
        self._save(state)
        return state['platform_paused'][platform]

    def platform_paused(self, platform, now=None):
        """The active pause entry for a platform, or None when absent/expired."""
        now = self.clock() if now is None else now
        entry = (self.load().get('platform_paused') or {}).get(platform)
        if not entry:
            return None
        until = entry.get('until')
        if until is not None and now >= until:
            return None
        return entry

    def _platform_cap(self, platform):
        override = self.platform_overrides.get(platform, {})
        if override.get('daily_cap') is not None:
            return override['daily_cap']
        return self.platform_daily_caps.get(platform, PLATFORM_DAILY_CAP)

    # -- reserve / block ----------------------------------------------------
    def reserve(self, platform, stage, combo=None, requested_url=None, now=None):
        """Decide before sending. Allowed reserves one request_id; blocked never counts."""
        now = self.clock() if now is None else now
        state = self.load()
        override = self.platform_overrides.get(platform, {})

        pause_until = override.get('pause_until')
        if pause_until is not None and now < pause_until:
            return self._refused('platform_paused')

        paused = (state.get('platform_paused') or {}).get(platform)
        if paused:
            until = paused.get('until')
            if until is None or now < until:
                return self._refused('platform_paused')

        if state['trial_used'] >= self.trial_total:
            return self._refused('trial_total')

        if stage in self.stage_caps and \
                state['stage_used'].get(stage, 0) >= self.stage_caps[stage]:
            return self._refused('stage_cap')

        day = self._day_key(now)
        if state['platform_daily_used'].get(day, {}).get(platform, 0) >= self._platform_cap(platform):
            return self._refused('platform_daily_cap')

        if state['combined_daily_used'].get(day, 0) >= self.combined_daily_cap:
            return self._refused('combined_daily_cap')

        interval = override.get('interval_seconds')
        if interval is None:
            interval = self.min_interval_seconds
        last = state['last_request_at'].get(platform)
        if last is not None and now - last < interval:
            return self._refused('interval_not_elapsed',
                                 wait_seconds=round(interval - (now - last), 3))

        request_id = state['next_request_id']
        state['next_request_id'] = request_id + 1
        state['trial_used'] += 1
        state['stage_used'][stage] = state['stage_used'].get(stage, 0) + 1
        day_platform = state['platform_daily_used'].setdefault(day, {})
        day_platform[platform] = day_platform.get(platform, 0) + 1
        state['combined_daily_used'][day] = state['combined_daily_used'].get(day, 0) + 1
        state['last_request_at'][platform] = now
        self._save(state)
        return {'allowed': True, 'request_id': request_id, 'reason': None, 'wait_seconds': 0.0}

    def _refused(self, reason, wait_seconds=0.0):
        return {'allowed': False, 'request_id': None, 'reason': reason,
                'wait_seconds': wait_seconds}

    def record_exhausted(self, *, platform, stage, combo=None, requested_url=None, reason,
                         now=None):
        now = self.clock() if now is None else now
        state = self.load()
        event = {'at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                 'platform': platform, 'stage': stage, 'combo': combo,
                 'requested_url': requested_url, 'reason': reason}
        state['budget_exhausted'].append(event)
        self._save(state)
        return event

    def report(self, now=None):
        """Read-only snapshot of budget usage; never mutates budget.json."""
        now = self.clock() if now is None else now
        state = self.load()
        used = state['trial_used']
        report = {
            'trial': {'used': used, 'cap': self.trial_total,
                      'remaining': max(0, self.trial_total - used)},
            'stages': {},
            'platforms_daily': {},
            'combined_daily': {},
            'budget_exhausted_events': list(state['budget_exhausted']),
            'platform_paused': {k: dict(v) for k, v in
                                (state.get('platform_paused') or {}).items()},
            'next_request_id': state['next_request_id'],
            'trial_date': state['trial_date'],
        }
        for stage, cap in self.stage_caps.items():
            stage_used = state['stage_used'].get(stage, 0)
            report['stages'][stage] = {'used': stage_used, 'cap': cap,
                                       'remaining': max(0, cap - stage_used)}
        for day_key, platforms in state['platform_daily_used'].items():
            report['platforms_daily'][day_key] = {
                platform: {'used': n, 'cap': self._platform_cap(platform)}
                for platform, n in platforms.items()}
        for day_key, n in state['combined_daily_used'].items():
            report['combined_daily'][day_key] = {'used': n, 'cap': self.combined_daily_cap}
        return report


def load_platform_policy_overrides(config_path='data_processing/fang_config.json',
                                   status_path='output/fang/status.json'):
    """Read-only 房天下 policy: interval/cap from fang_config, pause.until from fang status."""
    config_file = Path(config_path)
    status_file = Path(status_path)
    if not config_file.exists():
        return {}
    config = json.loads(config_file.read_text(encoding='utf-8'))
    pause_until = None
    pause_reason = None
    if status_file.exists():
        pause = (json.loads(status_file.read_text(encoding='utf-8')).get('pause') or {})
        pause_reason = pause.get('reason')
        if pause.get('until'):
            pause_until = datetime.fromisoformat(pause['until']).timestamp()
    return {'房天下': {
        'interval_seconds': float(config.get('interval_seconds', MIN_INTERVAL_SECONDS)),
        'daily_cap': int(config.get('daily_request_limit', PLATFORM_DAILY_CAP)),
        'pause_until': pause_until,
        'pause_reason': pause_reason,
    }}


def validate_budget_config(budget=None, stage_caps=None, trial_total=TRIAL_TOTAL_CAP,
                           retries=0, interval=None):
    """Reject illegal budget parameters early: negatives, non-ints, caps over the trial total."""
    if budget is not None and not _is_plain_int(budget):
        raise ValueError(f'budget must be an int, got {budget!r}')
    if budget is not None and budget < 0:
        raise ValueError(f'budget must be >= 0, got {budget!r}')
    if not _is_plain_int(trial_total):
        raise ValueError(f'trial_total must be an int, got {trial_total!r}')
    if trial_total <= 0:
        raise ValueError(f'trial_total must be positive, got {trial_total!r}')
    if not _is_plain_int(retries):
        raise ValueError(f'retries must be an int, got {retries!r}')
    if retries < 0:
        raise ValueError(f'retries must be >= 0, got {retries!r}')
    if interval is not None:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError(f'interval must be a number, got {interval!r}')
        if interval < 0:
            raise ValueError(f'interval must be >= 0, got {interval!r}')
    caps = STAGE_CAPS if stage_caps is None else stage_caps
    for stage, cap in caps.items():
        if not _is_plain_int(cap):
            raise ValueError(f'stage cap for {stage} must be an int, got {cap!r}')
        if cap < 0:
            raise ValueError(f'stage cap for {stage} must be >= 0, got {cap!r}')
        if cap > trial_total:
            raise ValueError(
                f'stage cap for {stage} exceeds trial_total ({cap} > {trial_total})')
    return True


SNAPSHOT_SUPERSEDED_NOTE = ('body superseded by a later fetch of the same URL; '
                            'hash/size/status retained')
SNAPSHOT_MISSING_NOTE = 'no body retained for this request'


def save_raw(raw_dir, combo, url, body, digest):
    """Full content-hash snapshot name; distinct bodies of one URL never overwrite each other."""
    slug = slug_of(url)
    html_path = raw_dir / f'{combo}__{slug}__{digest}.html'
    if html_path.exists() and html_path.read_bytes() != body:
        # Same name, different bytes: suffix so the earlier body survives as its own file.
        index = 2
        while True:
            candidate = raw_dir / f'{combo}__{slug}__{digest}__{index}.html'
            if not candidate.exists() or candidate.read_bytes() == body:
                html_path = candidate
                break
            index += 1
    html_path.write_bytes(body)
    html_path.with_suffix('.sha256').write_text(digest, encoding='utf-8')
    return html_path


def copy_to_failed(raw_dir, snapshot_path):
    """Non-success bodies also land in raw/failed/; success pages stay in raw/ only."""
    src = Path(snapshot_path)
    failed_dir = raw_dir / 'failed'
    failed_dir.mkdir(parents=True, exist_ok=True)
    dst = failed_dir / src.name
    dst.write_bytes(src.read_bytes())
    sidecar = src.with_suffix('.sha256')
    if sidecar.exists():
        dst.with_suffix('.sha256').write_text(sidecar.read_text(encoding='utf-8'),
                                              encoding='utf-8')
    return dst


def verify_retention(rows):
    """Re-check every write that each retained snapshot still exists and matches its sha256."""
    for row in rows:
        path_str = row.get('snapshot_path')
        digest = row.get('sha256')
        row.pop('snapshot_note', None)
        retained = False
        if path_str and digest:
            path = Path(path_str)
            retained = path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        row['snapshot_retained'] = retained
        if not retained:
            row['snapshot_note'] = SNAPSHOT_SUPERSEDED_NOTE if digest else SNAPSHOT_MISSING_NOTE
    return rows


REPLAY_REPORT_SCHEMA = 'source_pilot_replay_report/v1'
REPLAY_REPORT_DIR = 'output/source_trial_20260922'


def _snapshot_replayable(row):
    """True only when the recorded file still exists AND hashes to the recorded sha256."""
    path_str = row.get('snapshot_path')
    digest = row.get('sha256')
    if not (path_str and digest):
        return False
    path = Path(path_str)
    return path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def replayable_ratio(requests):
    """Verifiable fraction of recorded requests: file present AND sha256 matches the record.

    A superseded/overwritten body or a no_body row is never counted; each unreplayable
    request keeps its explicit marker/note so the historical gap stays visible.
    """
    requests = list(requests or [])
    missing = []
    numerator = 0
    for row in requests:
        if _snapshot_replayable(row):
            numerator += 1
            continue
        missing.append({
            'requested_url': row.get('requested_url'),
            'snapshot_path': row.get('snapshot_path'),
            'sha256': row.get('sha256'),
            'body_state': row.get('body_state'),
            'snapshot_retained': row.get('snapshot_retained'),
            'marker': (row.get('snapshot_note') or row.get('body_state')
                       or row.get('failure_class') or 'no_snapshot_recorded'),
        })
    denominator = len(requests)
    return {
        'numerator': numerator,
        'denominator': denominator,
        'ratio': (numerator / denominator) if denominator else 0.0,
        'missing': missing,
    }


def build_replay_report(requests):
    """Ratio + per-request replayability + preserved missing markers, for offline auditing."""
    ratio = replayable_ratio(requests)
    report = {
        'schema': REPLAY_REPORT_SCHEMA,
        'generated_at': now_iso(),
        'numerator': ratio['numerator'],
        'denominator': ratio['denominator'],
        'ratio': ratio['ratio'],
        'missing': ratio['missing'],
        'requests': [
            {
                'requested_url': row.get('requested_url'),
                'snapshot_path': row.get('snapshot_path'),
                'sha256': row.get('sha256'),
                'body_state': row.get('body_state'),
                'snapshot_retained': row.get('snapshot_retained'),
                'replayable': _snapshot_replayable(row),
                'marker': (row.get('snapshot_note') or row.get('body_state')
                           or row.get('failure_class')),
            }
            for row in requests
        ],
    }
    return report


def write_replay_report(requests_path='output/source_pilot_20260921/requests.jsonl',
                        out_path=REPLAY_REPORT_DIR + '/replay_report.json'):
    """Emit the replay report under the new trial dir; never writes into the old snapshot dir."""
    report = build_replay_report(read_jsonl(Path(requests_path)))
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def challenge_url_markers(url=None, redirect_chain=None):
    """Structural CAPTCHA hits from the final URL and every redirect hop (host/URL markers)."""
    hits = []
    candidates = [url or '']
    for hop in (redirect_chain or []):
        if not isinstance(hop, dict):
            continue
        for key in ('url', 'location', 'next'):
            value = hop.get(key)
            if value:
                candidates.append(value)
    joined = ' '.join(candidates)
    if any(host in joined for host in CHALLENGE_HOST_MARKERS):
        hits.append('url:check_host')
    for marker in CHALLENGE_URL_MARKERS:
        if marker in joined:
            hits.append(f'url:{marker}')
    return hits


def challenge_body_markers(body):
    """Structural CAPTCHA hits from the raw snapshot body (slider shell markers)."""
    text = _to_text(body or b'').lower()
    return [f'body:{m}' for m in CHALLENGE_BODY_MARKERS if m in text]


def challenge_evidence(title=None, text=None, has_cards=False, final_url=None,
                       redirect_chain=None, body=None):
    """Named evidence for a challenge verdict: URL/body structure plus weak text terms.

    URL/body markers are structural and decisive; CHALLENGE_TERMS match the title only,
    while the narrower CHALLENGE_BODY_TERMS are checked against body text and only when
    no cards parsed, so a hidden login form's 验证码 placeholder never blocks a directory.
    """
    hits = list(challenge_url_markers(final_url, redirect_chain))
    hits += challenge_body_markers(body)
    title_low = (title or '').lower()
    text_low = (text or '').lower()
    for term in CHALLENGE_TERMS:
        if term.lower() in title_low:
            hits.append(f'title:{term}')
    if not has_cards:
        for term in CHALLENGE_BODY_TERMS:
            if term in text_low:
                hits.append(f'body_term:{term}')
    return hits


def is_challenge(title, text, has_cards, final_url=None, redirect_chain=None, body=None):
    return bool(challenge_evidence(title, text, has_cards, final_url, redirect_chain, body))


def _text_around(text, start, end, limit=CLAIMED_SNIPPET_MAX):
    """Short excerpt centered on a match so a declared number stays provable."""
    pad = max(0, (limit - (end - start)) // 2)
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    return text[left:right].strip()[:limit]


def _is_villa_ref(url=None, snapshot_path=None):
    """True when a page/snapshot is the 中原 villa (/t2) catalog, not the default one."""
    path = urlsplit(url or '').path
    if re.search(r'/t2(?:g\d+)?/?$', path):
        return True
    name = Path(snapshot_path or '').name.lower()
    return 'villa' in name or '_t2' in name or 't2_' in name


def parse_page_nav(raw_html, snapshot_path=None):
    """Navigation evidence: highest page index + every index seen, from saved html only.

    Recognizes 丽兹行 `/loupan/pN` and 中原 `/xiaoqu/.../pN` links; never fetches a page.
    """
    raw = _to_text(raw_html)
    loupan = sorted({int(n) for n in LOUPAN_PAGE_RE.findall(raw)})
    xiaoqu = sorted({int(n) for n in XIAOQU_PAGE_RE.findall(raw)})
    seen = sorted(set(loupan) | set(xiaoqu))
    return {
        'max_page': max(seen) if seen else None,
        'pages_seen': seen,
        'evidence': {'snapshot_path': str(snapshot_path) if snapshot_path else None,
                     'loupan_pages': loupan, 'xiaoqu_pages': xiaoqu},
    }


def merge_signals(store, source, html, snapshot_path=None, url=None, city=None):
    """Accumulate per-source declared-total + navigation evidence (tags stripped first).

    A declared `共 N 个小区` may be split across nested tags; text extraction joins it, and
    each hit keeps its snapshot path, scope hint and a short raw excerpt so it is provable.
    Never invents a number when the page declares none.
    """
    raw = _to_text(html)
    text = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
    bucket = store.setdefault(source, {'claimed': [], 'max_page': None, 'claims': []})
    bucket.setdefault('claims', [])
    for match in PAGE_DECLARED_RE.finditer(text):
        value = int(match.group(1).replace(',', ''))
        snippet = _text_around(text, match.start(), match.end())
        claim = {
            'value': value,
            'unit': '小区',
            'text': match.group(0),
            'city': city,
            'scope_hint': SCOPE_VILLA if _is_villa_ref(url, snapshot_path) else SCOPE_DEFAULT,
            'observed_at': now_iso(),
            'snapshot_path': str(snapshot_path) if snapshot_path else None,
            'snippet': snippet,
            'evidence': {'snapshot_path': str(snapshot_path) if snapshot_path else None,
                         'snippet': snippet},
        }
        bucket['claimed'].append(value)
        bucket['claims'].append(claim)
        bucket['observed_at'] = claim['observed_at']
        bucket['evidence'] = claim['evidence']
    nav = parse_page_nav(raw, snapshot_path)
    if nav['max_page'] is not None:
        bucket['max_page'] = nav['max_page']


# ---------------------------------------------------------------------------
# checks + summary
# ---------------------------------------------------------------------------
def check_reasons(obs, members, combo_obs):
    reasons = []
    if obs['district'] and sum(1 for x in combo_obs if x['district'] == obs['district']) == 1:
        reasons.append('different_district')
    if obs.get('mixed_state') == 'mixed':
        reasons.append('mixed_type')
    if obs['has_villa']:
        reasons.append('villa_present')
    if obs.get('clue_villa') and not obs.get('has_villa'):
        reasons.append('villa_clue_only')
    if obs['year_value'] is None:
        reasons.append('missing_year')
    if obs['phase']:
        reasons.append('same_name_phase')
    if obs['source_id'] is None:
        reasons.append('missing_source_id')
    if len({m['source'] for m in members}) > 1:
        reasons.append('cross_source')
    return reasons


def build_check_row(combo, obs, members, reasons, association=None):
    association = association or associate_group(members)
    sources = {m['source'] for m in members}
    cross = {m['source']: {'year': m['year_value'], 'type': m['type_raw'], 'address': m['address'],
                           'phase': m['phase'], 'source_id': m['source_id']}
             for m in members}
    years = [m['year_value'] for m in members if m['year_value'] is not None]
    independent = [{'source': m['source'], 'source_id': m['source_id'], 'name': m['name'],
                    'year_value': m['year_value'], 'snapshot_path': m['snapshot_path'],
                    'phase': m['phase'], 'address': m['address']}
                   for m in members if m['source'] != obs['source']]

    if len(sources) <= 1:
        identity_check = year_check = type_check = location_check = 'single_source'
    elif association['level'] != 'confirmed':
        identity_check = association['level']
        year_check = type_check = location_check = '待核'
    else:
        identity_check = 'agree'
        if len(set(years)) > 1:
            year_check = 'conflict'
        elif years:
            year_check = 'agree'
        else:
            year_check = '待核'
        type_cmp = compare_group_types(members)
        type_check = {'consistent': 'agree', 'conflict': 'conflict',
                      'unknown': '待核'}.get(type_cmp['state'], '待核')
        # Detail coords exist but the system is unlabeled -> 待核, never a district-centre guess.
        location_check = '待核'

    if identity_check in ('candidate', 'rejected'):
        conclusion = '待核:关联为候选,证据不足不得继承年份/坐标'
    elif year_check == 'conflict':
        conclusion = CONFLICT_CONCLUSION
    elif len(sources) <= 1:
        conclusion = 'single_source:仅单一平台记录,待补充独立来源'
    elif year_check == 'agree':
        conclusion = 'agree:交叉来源年份一致,但仍非竣工证明'
    else:
        conclusion = '待核:证据不足'

    unresolved = ['无坐标,无法核验位置']
    if obs['year_value'] is None:
        unresolved.append('缺年份标注')
    if year_check == 'conflict':
        unresolved.append('期次/事件含义(建造/竣工/交付/开盘)待核')

    return {
        'combo': combo,
        'object_key': f"{obs['source']}|{obs['city']}|{obs['source_id'] or obs['name']}",
        'name': obs['name'],
        'city': obs['city'],
        'selection_reason': reasons,
        'independent_basis': independent,
        'identity_check': identity_check,
        'year_check': year_check,
        'type_check': type_check,
        'location_check': location_check,
        'cross_source_values': cross,
        'association': {'level': association['level'],
                        'may_transfer': association['may_transfer'],
                        'reason_text': association['reason_text'],
                        'positive_signals': association['positive_signals'],
                        'min_signals': association['min_signals'],
                        'pairs': association['pairs']},
        'type_comparison': compare_group_types(members),
        'conclusion': conclusion,
        'unresolved': unresolved,
    }


def dedup_checks(rows):
    """One check per object_key; build_checks may visit the same object from several groups."""
    seen = set()
    unique = []
    for row in rows:
        if row['object_key'] in seen:
            continue
        seen.add(row['object_key'])
        unique.append(row)
    return unique


def build_checks(observations):
    groups = group_observations(observations)
    combo_obs = defaultdict(list)
    for obs in observations:
        combo_obs[combo_of(obs['source'], obs['city'])].append(obs)

    checks = []
    for combo, obs_list in combo_obs.items():
        candidates = []
        seen = set()
        for obs in obs_list:
            key = (obs['city'], strip_phase(obs['name']), phase_number(obs['name']))
            if key in seen:
                continue
            seen.add(key)
            members = groups[key]
            candidates.append((check_reasons(obs, members, obs_list), obs, members, key))
        candidates.sort(key=lambda item: len(item[0]), reverse=True)

        chosen = []
        chosen_keys = set()
        # Cross-source groups are the scarce evidence; always surface them (e.g. 仁恒滨江园).
        for cand in candidates:
            if len({m['source'] for m in cand[2]}) > 1:
                chosen.append(cand)
                chosen_keys.add(cand[3])
        for cand in candidates:
            if len(chosen) >= 10:
                break
            if cand[3] in chosen_keys:
                continue
            chosen.append(cand)
            chosen_keys.add(cand[3])

        for reasons, obs, members, _key in chosen:
            checks.append(build_check_row(combo, obs, members, reasons))
    return dedup_checks(checks)


def scope_block(source, rows, groups, claimed, pages, per_page, request_rows, notes):
    """Metrics for one advertised catalog scope (a source, or a source subset)."""
    unique = dedup_observations(rows)
    source_requests = [r for r in request_rows if r.get('source') == source]
    blocked_class = next((r.get('failure_class') for r in source_requests
                          if r.get('failure_class') in ('blocked_403', 'blocked_429', 'challenge')), None)
    scope_keys = {(o['city'], strip_phase(o['name']), phase_number(o['name'])) for o in rows}
    group_members = [groups[key] for key in scope_keys]
    independent_support = sum(1 for o in unique
                              if len({m['source'] for m in groups[
                                  (o['city'], strip_phase(o['name']), phase_number(o['name']))]}) > 1)
    judgeable = sum(1 for g in group_members
                    if len({m['source'] for m in g}) > 1
                    and all(m['year_value'] is not None for m in g))
    with_year = sum(1 for o in unique if o['year_value'] is not None)
    with_coords = sum(1 for o in unique if o['coords_raw'])
    return {
        '官网声称数量': claimed,
        '页面显示总数': pages * per_page,
        '实际抓到唯一小区数': len(unique),
        '字段完整数': sum(1 for o in unique
                          if o['year_value'] is not None and o['type_raw']
                          and o['address'] and o['coords_raw']),
        '独立核验支持数': independent_support,
        '本次查询可判数': judgeable,
        'type_counters': type_counters(unique),
        'funnel': {
            '解析条数': len(rows),
            '去重后唯一数': len(unique),
            '年份非空数': with_year,
            '类型可判数': sum(1 for o in unique
                              if (o.get('mixed_state')
                                  or classify_mixed_state(o.get('type_tags'))) != 'unknown'),
            '地址非空数': sum(1 for o in unique if o['address']),
            '坐标可用数': with_coords,
            '年份类型坐标齐全数': sum(1 for o in unique
                                      if o['year_value'] is not None
                                      and (o['type_raw'] or o['type_tags']) and o['coords_raw']),
        },
        'page_cap': f'{pages} pages x{per_page}',
        'per_page': per_page,
        'requests_used': len(source_requests),
        'blocked': {'blocked': blocked_class is not None, 'class': blocked_class},
        'combos': sorted({combo_of(o['source'], o['city']) for o in rows}),
        'notes': notes,
    }


# ---------------------------------------------------------------------------
# B3: five separated catalog numbers (page-declared / nav estimate / enumerated /
# parent communities / fetch status). Unknown stays BLANK with a reason, never 0.
# ---------------------------------------------------------------------------
def _claim_matches_scope(claim, city, scope):
    """Attribute a declared total to a scope by capture evidence, never by max/min guessing."""
    hint = claim.get('scope_hint')
    if scope == SCOPE_VILLA:
        return hint == SCOPE_VILLA
    if scope == SCOPE_DEFAULT:
        return hint == SCOPE_DEFAULT
    # City-split scopes (bj/sh): any claim captured for this city; otherwise any unhinted claim.
    if city:
        return claim.get('city') == city
    return hint is None


def page_declared_count(source, city, scope, signals):
    """The page's OWN declared catalog total for one scope, or BLANK when it declares none.

    Value is copied verbatim from a `共 N 个小区` hit: never a product, never 0-as-measured.
    """
    blank = {'value': '', 'unit': '小区', 'text': '', 'observed_at': '',
             'city': city, 'scope': scope, 'evidence': {}, 'reason': PAGE_DECLARED_UNKNOWN}
    bucket = (signals or {}).get(source, {}) or {}
    claims = list(bucket.get('claims') or [])
    for claim in claims:
        if _claim_matches_scope(claim, city, scope):
            return {
                'value': claim['value'], 'unit': claim.get('unit', '小区'),
                'text': claim.get('text', ''), 'observed_at': claim.get('observed_at', ''),
                'city': city, 'scope': scope,
                'evidence': {'snapshot_path': claim.get('snapshot_path'),
                             'snippet': claim.get('snippet', '')},
                'reason': '',
            }
    if not claims:
        # Legacy signals without per-claim evidence: attribute only when unambiguous.
        values = sorted({v for v in (bucket.get('claimed') or []) if _is_plain_int(v)})
        if len(values) == 1:
            return {'value': values[0], 'unit': '小区', 'text': '', 'observed_at': '',
                    'city': city, 'scope': scope, 'evidence': {}, 'reason': ''}
    return blank


def nav_capacity_estimate(pages, per_page, evidence, city=None, scope=None):
    """Upper-bound ESTIMATE pages*per_page from navigation depth; explicitly NOT official."""
    value = pages * per_page if (_is_plain_int(pages) and _is_plain_int(per_page)) else None
    return {
        'value': value, 'numerator': value, 'denominator': None,
        'label': 'ESTIMATE', 'cn_label': '导航容量估算', 'formula': 'pages*per_page',
        'source': 'navigation', 'is_official': False, 'pages': pages, 'per_page': per_page,
        'city': city, 'scope': scope, 'observed_at': now_iso(), 'evidence': evidence,
        'reason': '' if value is not None else '缺少页数/每页数,无法估算',
    }


def enumerated_unique_ids(rows):
    """Distinct enumerated objects after dedup, split by whether a stable id was present."""
    unique = dedup_observations(rows)
    with_id = sum(1 for r in unique if r.get('source_id'))
    return {'with_id': with_id, 'without_id': len(unique) - with_id, 'total': len(unique)}


def enumerated_unique_ids_without_id(rows):
    """Count of deduped enumerated rows carrying no stable source_id (deduped by name)."""
    return sum(1 for r in dedup_observations(rows) if not r.get('source_id'))


def parent_community_count(rows, groups=None):
    """Distinct parent communities after collapsing phase variants of one base name.

    phases_separate counts bases with >1 distinct phase label; unresolved_duplicate_count
    counts bases holding >1 differing source_id under a single source.
    """
    unique = dedup_observations(rows)
    bases = set()
    phases_by_base = defaultdict(set)
    ids_by_base = defaultdict(set)
    for r in unique:
        base = (r['city'], strip_phase(r['name']))
        bases.add(base)
        phases_by_base[base].add(phase_number(r['name']))
        if r.get('source_id'):
            ids_by_base[base].add((r['source'], r['source_id']))
    phases_separate = sum(1 for phases in phases_by_base.values() if len(phases) > 1)
    unresolved = 0
    for ids in ids_by_base.values():
        per_source = defaultdict(set)
        for src, sid in ids:
            per_source[src].add(sid)
        if any(len(s) > 1 for s in per_source.values()):
            unresolved += 1
    return {'count': len(bases), 'phases_separate': phases_separate,
            'unresolved_duplicate_count': unresolved}


def fetch_status(source_requests, total_pages_expected=None, pages_read=0, truncated=False,
                 city=None, scope=None):
    """Completion state for one directory: 未测/成功/空目录/截断/阻断/预算未完成 + evidence."""
    rows = list(source_requests or [])
    evidence = {
        'requests': len(rows),
        'failure_classes': sorted({r.get('failure_class') for r in rows
                                   if r.get('failure_class')}),
        'statuses': sorted({r.get('status') for r in rows if r.get('status') is not None}),
        'snapshot_paths': sorted({r.get('snapshot_path') for r in rows
                                  if r.get('snapshot_path')}),
    }
    state, reason = '成功', ''
    if not rows:
        state, reason = '未测', '无请求记录'
    elif any(r.get('failure_class') in BLOCK_CLASSES for r in rows):
        state, reason = '阻断', next(r.get('failure_class') for r in rows
                                     if r.get('failure_class') in BLOCK_CLASSES)
    elif any(r.get('failure_class') == 'budget_exhausted' for r in rows):
        state, reason = '预算未完成', 'budget_exhausted'
    elif truncated or (total_pages_expected is not None and pages_read < total_pages_expected):
        state, reason = '截断', f'已读{pages_read}/预计{total_pages_expected}'
    elif sum(int(r.get('cards_parsed') or 0) for r in rows) == 0:
        state, reason = '空目录', '解析0条'
    return {'state': state, 'numerator': state, 'denominator': None, 'unit': '状态',
            'label': '获取状态', 'city': city, 'scope': scope, 'observed_at': now_iso(),
            'evidence': evidence, 'reason': reason}


def _metric(label, numerator, denominator, city, scope, evidence, observed_at, reason=''):
    """Uniform metric envelope: numerator/denominator plus where/when and why-unknown."""
    return {'label': label, 'numerator': numerator, 'denominator': denominator,
            'city': city, 'scope': scope, 'observed_at': observed_at,
            'evidence': evidence, 'reason': reason}


def type_evidence_counters(rows):
    """A6: separate explicit villa/non-villa from clue-only and unknown.

    A /t2/-only tag (the whole type is a villa tag and the row came from the /t2/ path)
    is demoted to a clue: the path proves a villa candidate, not a pure-villa community,
    and says nothing about the remaining default-directory objects.
    """
    rows = list(rows or [])
    explicit_villa = explicit_non_villa = unknown = clue_only = 0
    clue_sources = {'目录/t2/': 0, '名称': 0, 'OSM': 0}
    for row in rows:
        tags = [t for t in (row.get('type_tags') or []) if not _is_unknown_type_tag(t)]
        has_v = bool(row.get('has_villa')) or has_villa_type(tags)
        t2_only = bool(T2_PATH_RE.search(urlsplit(row.get('list_url') or '').path)) \
            and bool(tags) and all(is_villa_tag(t) for t in tags) and has_v
        name_clue = bool(villa_clue_reasons(row.get('name')))
        if has_v and not t2_only:
            explicit_villa += 1
        elif has_nonvilla_type(tags):
            explicit_non_villa += 1
        elif row.get('clue_villa') or name_clue or t2_only:
            clue_only += 1
            evidence = list(row.get('clue_evidence') or [])
            if t2_only or CLUE_URL_T2_NOTE in evidence:
                clue_sources['目录/t2/'] += 1
            if name_clue or CLUE_NAME_NOTE in evidence:
                clue_sources['名称'] += 1
            if CLUE_OSM_NOTE in evidence:
                clue_sources['OSM'] += 1
        else:
            unknown += 1
    return {
        '明确含别墅': explicit_villa,
        '明确不含': explicit_non_villa,
        '未知': unknown,
        '仅线索': clue_only,
        '仅线索依据': clue_sources,
        'note': '/t2/标签只证明含别墅候选,不证明纯别墅,也不证明其余对象不含别墅;普通目录无类型记为未知',
    }


def type_counters(rows):
    rows = list(rows or [])
    states = [(r.get('mixed_state') or classify_mixed_state(r.get('type_tags'))) for r in rows]
    evidence = type_evidence_counters(rows)
    return {
        '明确含别墅': evidence['明确含别墅'],
        '明确不含': evidence['明确不含'],
        '未知': evidence['未知'],
        '仅线索': evidence['仅线索'],
        '仅线索依据': evidence['仅线索依据'],
        '混合状态': {s: states.count(s) for s in MIXED_STATES},
    }


def build_scope_counts(source, city, scope, rows, groups, signals, request_rows,
                       pages=None, per_page=None, notes=None,
                       total_pages_expected=None, pages_read=None, truncated=False):
    """B3 catalog counts for one scope, each number kept separate and evidence-backed.

    Never multiplies to fabricate an official total and never reports 0 as if measured:
    an unknown count is '' with a reason. Legacy funnel/request keys are preserved.
    """
    rows = list(rows or [])
    unique = dedup_observations(rows)
    source_requests = [r for r in (request_rows or []) if r.get('source') == source]
    bucket = (signals or {}).get(source, {}) or {}

    enum = enumerated_unique_ids(unique)
    parents = parent_community_count(unique, groups)
    declared = page_declared_count(source, city, scope, signals or {})

    nav_pages = pages if _is_plain_int(pages) else (bucket.get('max_page') or 0)
    nav_per_page = per_page if _is_plain_int(per_page) else 0
    estimate = nav_capacity_estimate(
        nav_pages, nav_per_page,
        {'snapshot_path': None, 'max_page_seen': bucket.get('max_page'), 'formula': 'pages*per_page'},
        city, scope)
    if pages_read is None:
        pages_read = sum(1 for r in source_requests if r.get('cards_parsed'))
    status = fetch_status(source_requests, total_pages_expected=total_pages_expected,
                          pages_read=pages_read, truncated=truncated, city=city, scope=scope)

    observed = now_iso()
    declared_metric = dict(declared)
    declared_metric.update({'label': '页面声明总数', 'ascii_key': 'page_declared_count',
                            'numerator': declared['value'], 'denominator': None})
    enum_metric = _metric('枚举唯一ID', enum['with_id'], enum['total'], city, scope,
                          {'with_id': enum['with_id'], 'without_id': enum['without_id'],
                           'snapshot_paths': sorted({o.get('snapshot_path') for o in unique
                                                     if o.get('snapshot_path')})},
                          observed,
                          '' if enum['with_id'] else ('无可识别ID' if enum['total']
                                                      else '无记录'))
    enum_metric['ascii_key'] = 'enumerated_unique_ids'
    parent_metric = _metric('父小区数', parents['count'], enum['total'], city, scope,
                            {'phases_separate': parents['phases_separate'],
                             'unresolved_duplicate_count': parents['unresolved_duplicate_count']},
                            observed, '' if parents['count'] else '无记录')
    parent_metric['ascii_key'] = 'parent_community_count'

    return {
        # B3: five numbers, each a separate field
        'page_declared_count': declared_metric,
        'nav_capacity_estimate': estimate,
        'enumerated_unique_ids': enum_metric,
        'parent_community_count': parent_metric,
        'fetch_status': status,
        'type_counters': type_counters(unique),
        # legacy keys kept so nothing downstream breaks
        '实际抓到唯一小区数': len(unique),
        'funnel': {
            '解析条数': len(rows),
            '去重后唯一数': len(unique),
            '年份非空数': sum(1 for o in unique if o['year_value'] is not None),
            '类型可判数': sum(1 for o in unique
                              if (o.get('mixed_state')
                                  or classify_mixed_state(o.get('type_tags'))) != 'unknown'),
            '地址非空数': sum(1 for o in unique if o['address']),
            '坐标可用数': sum(1 for o in unique if o['coords_raw']),
            '年份类型坐标齐全数': sum(1 for o in unique
                                      if o['year_value'] is not None
                                      and (o['type_raw'] or o['type_tags']) and o['coords_raw']),
        },
        'page_cap': f'{nav_pages} pages x{nav_per_page}',
        'per_page': nav_per_page,
        'requests_used': len(source_requests),
        'blocked': {'blocked': any(r.get('failure_class') in BLOCK_CLASSES
                                   for r in source_requests),
                    'class': next((r.get('failure_class') for r in source_requests
                                   if r.get('failure_class') in BLOCK_CLASSES), None)},
        'combos': sorted({combo_of(o['source'], o['city']) for o in rows}),
        'notes': list(notes or []),
    }


# ---------------------------------------------------------------------------
# B4: cross-source pairs (counted once) + five quality metrics
# ---------------------------------------------------------------------------
def _pair_representative(members):
    """Anchor a pair on best evidence: a stable id first, then a detail record."""
    with_id = [m for m in members if m.get('source_id')]
    pool = with_id or members
    details = [m for m in pool if m.get('record_kind') == 'detail']
    return details[0] if details else pool[0]


def _pair_scope(members):
    if any(centanet_scope(m) == SCOPE_VILLA for m in members):
        return SCOPE_VILLA
    return SCOPE_DEFAULT


def resolve_cross_source_pairs(observations, groups):
    """One record per deduped cross-source pair; conflicts counted once, not per direction.

    Groups rows by (city, base name, phase); only groups spanning >=2 sources emit pairs.
    Same-platform list/detail agreement is flagged and never treated as an independent basis.
    Year-conflict pairs are kept but excluded from independent verification.
    """
    pairs = {}
    rows = list(observations or [])
    for key, members in (groups or {}).items():
        city, base, phase_num = key
        by_source = defaultdict(list)
        for m in members:
            by_source[m['source']].append(m)
        sources = sorted(by_source)
        if len(sources) < 2:
            continue
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                a = _pair_representative(by_source[sources[i]])
                b = _pair_representative(by_source[sources[j]])
                ref_a = (a['source'], a['source_id'] or a['name'])
                ref_b = (b['source'], b['source_id'] or b['name'])
                object_key = tuple(sorted([ref_a, ref_b]))
                dedupe_key = (city, base, phase_num, object_key)
                if dedupe_key in pairs:
                    continue
                years = {r['year_value'] for r in (a, b) if r.get('year_value') is not None}
                year_conflict = len(years) > 1
                # Cross-source (distinct platforms) is the only independent basis here.
                basis_independent = a['source'] != b['source']
                list_detail_only = not basis_independent
                assoc = associate_pair(a, b)
                if assoc['level'] == 'confirmed':
                    identity = 'agree'
                elif assoc['level'] == 'candidate':
                    identity = 'candidate'
                else:
                    identity = '待核'
                pairs[dedupe_key] = {
                    'pair_key': dedupe_key,
                    'city': city,
                    'scope': _pair_scope([a, b]),
                    'object_key': object_key,
                    'year_conflict': year_conflict,
                    'identity': identity,
                    'association': assoc,
                    'position': '可用' if (a.get('coords_raw') and b.get('coords_raw')) else '待核',
                    'metro_distance': '待核',
                    'list_detail_agreement_only': list_detail_only,
                    'independent_basis': basis_independent,
                    'verified': basis_independent and not list_detail_only and not year_conflict,
                    'evidence': sorted({r.get('snapshot_path') for r in (a, b)
                                        if r.get('snapshot_path')}),
                }
    return list(pairs.values())


def resolve_primary_year_conflict(pair):
    """Cross-source year disagreement is recorded but never blocks the primary-platform year."""
    conflict = bool((pair or {}).get('year_conflict'))
    return {'year_conflict': conflict, 'blocks_primary_year': False,
            'counts_toward_independent_verification': not conflict,
            'note': CROSS_YEAR_POLICY_NOTE}


# Alias kept so the v2 B3 function is discoverable under either name.
scope_counts_v2 = build_scope_counts


def _row_in_scope(row, city, scope):
    """True when a row belongs to the requested city/scope filter (None = no filtering)."""
    if city and row.get('city') != city:
        return False
    if scope in (SCOPE_DEFAULT, SCOPE_VILLA):
        return centanet_scope(row) == scope
    if scope in (SCOPE_BJ, SCOPE_SH):
        return row.get('city') == ('北京' if scope == SCOPE_BJ else '上海')
    return True


def _indep_reason(verified_count, pair_count):
    """Caveat for 独立事实核验通过 whenever it cannot show a positive count.

    A pair is never verified when year_conflict is set, so pairs may exist while
    verified_count stays 0; distinguish "no pair at all" from "pairs rejected".
    """
    if verified_count > 0:
        return ''
    if pair_count == 0:
        return '无跨来源配对'
    return f'{pair_count - verified_count}对跨来源配对因年份冲突或缺乏独立依据未通过'


def build_quality_metrics(observations, groups, city=None, scope=None,
                          primary_source=PRIM_PLATFORM_SOURCE):
    """Five B4 quality metrics, each with numerator/denominator/city/scope/observed_at/evidence.

    Cross-source year disagreement never blocks 主平台年份可用 (it uses the primary platform's
    own year) but a year-conflict pair is excluded from 独立事实核验通过.
    """
    rows = [o for o in (observations or []) if _row_in_scope(o, city, scope)]
    unique = dedup_observations(rows)
    local_groups = group_observations(rows) if (city or scope or not groups) \
        else groups
    pairs = resolve_cross_source_pairs(rows, local_groups)
    observed = now_iso()

    def paths(items):
        return sorted({r.get('snapshot_path') for r in items if r.get('snapshot_path')})

    primary_rows = [r for r in unique if r.get('source') == primary_source]
    year_num = sum(1 for r in primary_rows if r.get('year_value') is not None)
    year_metric = _metric(
        '主平台年份可用', year_num, len(primary_rows), city, scope,
        {'primary_source': primary_source, 'policy': CROSS_YEAR_POLICY_NOTE,
         'snapshot_paths': paths(primary_rows)},
        observed, '' if primary_rows else f'主平台({primary_source})无记录')

    agree_pairs = [p for p in pairs if p['identity'] == 'agree']
    agree_refs = {tuple(ref) for p in agree_pairs for ref in p['object_key']}
    identity_num = sum(1 for r in unique
                       if r.get('source_id') or (r['source'], r['name']) in agree_refs)
    identity_metric = _metric(
        '身份已关联', identity_num, len(unique), city, scope,
        {'stable_id_rows': sum(1 for r in unique if r.get('source_id')),
         'agree_pair_keys': [list(p['pair_key']) for p in agree_pairs]},
        observed, '' if identity_num else ('无稳定ID且无一致配对' if unique else '无记录'))

    position_num = sum(1 for r in unique if r.get('coords_raw'))
    position_metric = _metric(
        '位置可用', position_num, len(unique), city, scope,
        {'snapshot_paths': paths([r for r in unique if r.get('coords_raw')])},
        observed, '' if position_num else '无原始坐标')
    coordsys_metric = _metric(
        '坐标系已核', 0, len(unique), city, scope,
        {'candidates': list(COORD_SYSTEM_CANDIDATES),
         'policy': '坐标仅保留原值,候选GCJ-02/WGS-84/BD-09,不得判为已核'},
        observed, '坐标系未标注,不得判为已核')

    combo_num = sum(1 for r in unique
                    if r.get('year_value') is not None and r.get('has_villa')
                    and r.get('coords_raw'))
    combo_metric = _metric(
        '组合筛选命中', combo_num, len(unique), city, scope,
        {'criteria': '年份非空+别墅类型+原始坐标'}, observed,
        '' if combo_num else '无同时满足年份/别墅类型/坐标的记录')

    verified = [p for p in pairs if p['verified']]
    indep_metric = _metric(
        '独立事实核验通过', len(verified), len(pairs), city, scope,
        {'verified_pair_keys': [list(p['pair_key']) for p in verified],
         'policy': CROSS_YEAR_POLICY_NOTE}, observed,
        _indep_reason(len(verified), len(pairs)))

    return {
        'primary_source': primary_source,
        'cross_source_pairs_total': len(pairs),
        '主平台年份可用': year_metric,
        '身份已关联': identity_metric,
        '位置可用': position_metric,
        '坐标系已核': coordsys_metric,
        '组合筛选命中': combo_metric,
        '独立事实核验通过': indep_metric,
    }


def combo_blocks(observations, request_rows):
    """Per-combo request health over the accumulated set (requests_used/blocked/gaps)."""
    combos = {combo_of(o['source'], o['city']) for o in observations}
    combos.update(row.get('combo') for row in request_rows if row.get('combo'))
    blocks = {}
    for combo in sorted(combos):
        rows = [r for r in request_rows if r.get('combo') == combo]
        # STOP_CLASSES doubles as the hard-block set (403/429/202/empty_body/challenge).
        blocked_class = next((r.get('failure_class') for r in rows
                              if r.get('failure_class') in STOP_CLASSES), None)
        blocks[combo] = {
            'requests_used': len(rows),
            'blocked': {'blocked': blocked_class is not None, 'class': blocked_class},
            'gaps': {
                'budget_exhausted': any(r.get('failure_class') == 'budget_exhausted' for r in rows),
                'transport_error': any(r.get('failure_class') == 'transport_error' for r in rows),
            },
        }
    return blocks


def build_summary(observations, request_rows, signals, primary_source=PRIM_PLATFORM_SOURCE):
    """Per-scope B3 counts + B4 quality metrics; unknowns stay BLANK, totals never fabricated.

    Declared totals are routed by capture evidence: 中原 default目录 claims the larger
    directory, 中原 villa claims the /t2 snapshot, 丽兹行 declares nothing (blank).
    """
    groups = group_observations(observations)
    per_source = {}

    lz_rows = [o for o in observations if o['source'] == '丽兹行']
    lz_bj = [o for o in lz_rows if o['city'] == '北京']
    lz_sh = [o for o in lz_rows if o['city'] == '上海']
    max_page = signals.get('丽兹行', {}).get('max_page') or LIZIHANG_MAX_PAGES

    def lizihang_block(rows, city, scope):
        counts = build_scope_counts(
            '丽兹行', city, scope, rows, groups, signals, request_rows,
            pages=max_page, per_page=20,
            notes=['官网未声明小区总数(页面声明总数为空)',
                   f'导航容量估算取末页索引{max_page}×20,仅估算非官方'])
        counts.update(build_quality_metrics(rows, groups, city=city, scope=scope,
                                            primary_source=primary_source))
        return counts

    per_source['bj_lizihang'] = lizihang_block(lz_bj, '北京', SCOPE_BJ)
    per_source['sh_lizihang'] = lizihang_block(lz_sh, '上海', SCOPE_SH)
    # Compatibility: the old combined key is kept alongside the city-split keys.
    per_source['丽兹行'] = lizihang_block(lz_rows, None, SCOPE_COMBINED)

    cn_rows = [o for o in observations if o['source'] == '中原']

    def centanet_block(rows, scope, notes, total_pages):
        counts = build_scope_counts(
            '中原', '上海', scope, rows, groups, signals, request_rows,
            pages=total_pages, per_page=10, notes=notes,
            total_pages_expected=total_pages)
        counts.update(build_quality_metrics(rows, groups, city='上海', scope=scope,
                                            primary_source=primary_source))
        return counts

    per_source['中原'] = {
        'default': centanet_block(
            [o for o in cn_rows if centanet_scope(o) == SCOPE_DEFAULT], SCOPE_DEFAULT,
            ['官网全目录声明数量取自页面文本 共 N 个小区',
             '按快照来源归属 default 目录,非按大小猜测'], CENTANET_DEFAULT_PAGES),
        'villa': centanet_block(
            [o for o in cn_rows if centanet_scope(o) == SCOPE_VILLA], SCOPE_VILLA,
            ['官网别墅目录声明数量取自页面文本 共 N 个小区',
             '按 /t2 别墅快照归属,非按大小猜测'], CENTANET_VILLA_PAGES),
    }
    return {
        'sources': per_source,
        'primary_source': primary_source,
        'combos': combo_blocks(observations, request_rows),
        'observations_total': len(observations),
        'requests_total': len(request_rows),
        'notes': [
            '待核项不得取平均/最新覆盖/多数投票。',
            '详情页/内嵌地图坐标均未标注坐标系(候选GCJ-02/WGS-84/BD-09),定位核验仍待核。',
            '丽兹行内嵌地址与原始坐标已提取,但坐标系未核实,不计为可算地铁距离。',
            CROSS_YEAR_POLICY_NOTE,
        ],
    }


def legacy_type_check(members):
    sigs = {tuple(m.get('type_tags') or []) for m in members}
    nonempty = {s for s in sigs if s}
    if len(nonempty) > 1:
        return 'conflict'
    if nonempty:
        return 'agree'
    return '待核'


def build_identity_types_report(observations, raw_dir=None,
                                source_path='output/source_pilot_20260921/observations.jsonl'):
    rows = list(observations or [])
    groups = group_observations(rows)
    multi = {k: m for k, m in groups.items() if len({x['source'] for x in m}) > 1}
    old_mixed = [r for r in rows if r.get('is_mixed')]
    old_pure = [r for r in old_mixed
                if (r.get('type_tags') or [])
                and all(is_villa_tag(t) for t in r['type_tags'])]
    moves = {}
    group_rows = []
    for key, members in multi.items():
        old_check = legacy_type_check(members)
        new_cmp = compare_group_types(members)
        new_check = {'consistent': 'agree', 'conflict': 'conflict',
                     'unknown': '待核'}.get(new_cmp['state'], '待核')
        moves[f'{old_check}->{new_check}'] = moves.get(f'{old_check}->{new_check}', 0) + 1
        group_rows.append({
            'group_key': list(key),
            'members': [{'source': m['source'], 'name': m['name'], 'address': m['address'],
                         'phase': m['phase'], 'source_id': m['source_id'],
                         'type_tags': list(m.get('type_tags') or []),
                         'year_value': m['year_value']} for m in members],
            'old_type_check': old_check,
            'new_type_check': new_check,
            'type_comparison': new_cmp,
            'association': associate_group(members),
        })
    obs_rows = [{
        'source': r['source'], 'city': r['city'], 'name': r['name'],
        'address': r.get('address'), 'phase': r.get('phase'),
        'source_id': r.get('source_id'), 'type_raw': r.get('type_raw'),
        'type_tags': list(r.get('type_tags') or []),
        'old_is_mixed': bool(r.get('is_mixed')), 'old_has_villa': bool(r.get('has_villa')),
        'new_has_villa': has_villa_type(r.get('type_tags')),
        'new_villa_subtypes': villa_subtypes_of(r.get('type_tags')),
        'new_mixed_state': classify_mixed_state(r.get('type_tags')),
        'name_only_clue': bool(villa_clue_reasons(r.get('name'))) and not has_villa_type(r.get('type_tags')),
        'snapshot_path': r.get('snapshot_path'),
    } for r in rows]
    return {
        'schema': 'source_pilot_identity_types/v1',
        'generated_at': now_iso(),
        'source_observations': source_path,
        'raw_dir': str(raw_dir) if raw_dir else '',
        'policy': {
            'confirmation': {'min_signals': CONFIRM_MIN_SIGNALS,
                             'signals': ['name_match', 'address_match', 'phase_match',
                                         'source_mapping'],
                             'address_conflict_blocks': True,
                             'note': '>=2 substantive signals and no address conflict; only confirmed may transfer year/coords'},
            'type_containment': {'villa_tags': list(VILLA_TAGS),
                                 'non_villa_tags': list(NON_VILLA_TAGS),
                                 'rule': 'parent/child containment is not a conflict; disjoint roots conflict; missing is unknown'},
            'mixed_states': list(MIXED_STATES),
            'clue_only': 'OSM house/terrace, 故居, name containing 别墅 -> clue_villa, never has_villa',
        },
        'totals': {'observations': len(rows), 'multi_source_groups': len(multi)},
        'old_vs_new': {
            'old_is_mixed_total': len(old_mixed),
            'old_is_mixed_actually_pure_villa': len(old_pure),
            'old_is_mixed_pure_villa_with_独栋+联排': sum(
                1 for r in old_pure
                if '独栋' in r['type_tags'] and '联排' in r['type_tags']),
            'new_mixed_state': type_counters(rows)['混合状态'],
            'moves': moves,
            'moves_conflict_to_consistent': moves.get('conflict->agree', 0),
            'moves_consistent_to_unknown': moves.get('agree->待核', 0),
        },
        'type_counters': type_counters(rows),
        'groups': group_rows,
        'observations': obs_rows,
    }


def build_embedded_report(snapshot_dir):
    """Count embedded-data extraction per saved html (B1/B2), offline and read-only.

    Each file row records how many embedded objects exist and how many carry address,
    coords, year, stable id, and how many actually link to a card by id. Invalid raw
    points are counted, not dropped. Coordinates stay raw-only (not distance-ready).
    """
    snap = Path(snapshot_dir)
    files = []
    for html_path in sorted(snap.glob('*.html')):
        name = html_path.name
        if 'lizihang' in name:
            source, kind = '丽兹行', 'lizihang'
        elif 'centanet' in name:
            source, kind = '中原', 'centanet'
        else:
            continue
        city = '北京' if name.startswith('bj_') else '上海' if name.startswith('sh_') else None
        raw = html_path.read_text(encoding='utf-8', errors='ignore')
        row = {
            'page': name, 'source': source, 'city': city, 'total_embedded': 0,
            'with_address': 0, 'with_coords': 0, 'with_year': 0, 'with_stable_id': 0,
            'linked_by_id': 0, 'unlinked': 0, 'linked_by_image': 0, 'raw_rows': 0,
            'stable_id_objects': 0, 'pending_association': 0, 'unresolved_counts': {},
            'invalid_points': 0,
        }
        unresolved_counts = row['unresolved_counts']
        if kind == 'lizihang':
            records = lizihang_embedded_records(raw, str(html_path))
            row['total_embedded'] = len(records)
            for record in records:
                if record['admin_addr']:
                    row['with_address'] += 1
                lng = _to_float(record['map_x'])
                lat = _to_float(record['map_y'])
                if lng is not None and lat is not None:
                    row['with_coords'] += 1
                    if not validate_coord(city, lng, lat):
                        row['invalid_points'] += 1
                if record['build_year'] is not None:
                    row['with_year'] += 1
                if record['source_id']:
                    row['with_stable_id'] += 1
                for field in record['unresolved']:
                    unresolved_counts[field] = unresolved_counts.get(field, 0) + 1
            list_url = f'https://{"bj" if city == "北京" else "sh"}.lizihang.com/loupan'
            parsed = parse_lizihang(raw, list_url, city, source, None, 'offline_snapshot',
                                    str(html_path))
        else:
            records = parse_centanet_map_post_data(raw, str(html_path), city)
            row['total_embedded'] = len(records)
            for record in records:
                if record['lng'] is not None and record['lat'] is not None:
                    row['with_coords'] += 1
                if record['source_id']:
                    row['with_stable_id'] += 1
                if not record['coord_valid']:
                    row['invalid_points'] += 1
                for field in record['unresolved']:
                    unresolved_counts[field] = unresolved_counts.get(field, 0) + 1
            list_url = f'https://{"bj" if city == "北京" else "sh"}.centanet.com/xiaoqu/'
            parsed = parse_centanet(raw, list_url, city, source, None, 'offline_snapshot',
                                    str(html_path))
        row['linked_by_id'] = sum(1 for obs in parsed if obs.get('link_method') == 'stable_id')
        row['unlinked'] = sum(1 for obs in parsed if obs.get('link_method') == 'unlinked')
        row['linked_by_image'] = sum(1 for obs in parsed
                                     if obs.get('link_method') == LIZIHANG_PAGE_IMAGE)
        counts = lizihang_association_counts(parsed)
        row['raw_rows'] = counts['raw_rows']
        row['stable_id_objects'] = counts['stable_id_objects']
        row['pending_association'] = counts['pending_association']
        files.append(row)
    return {
        'snapshot_dir': str(snap),
        'coord_system_candidates': list(COORD_SYSTEM_CANDIDATES),
        'note': EMBEDDED_REPORT_NOTE,
        'files': files,
    }


def build_lizihang_identity_report(snapshot_dirs):
    """A5: per-page and per-city raw_rows / stable_id_objects / pending_association.

    Re-parses saved Lizihang snapshots offline. Counts stay separate (never summed into
    an '新增小区' total). Cities are deduped with dedup_observations; pages are raw.
    """
    pages = []
    per_city = defaultdict(list)
    for dir_path in snapshot_dirs:
        snap = Path(dir_path)
        for html_path in sorted(snap.glob('*lizihang*.html')):
            name = html_path.name
            if name.startswith('bj_'):
                city = '北京'
            elif name.startswith('sh_'):
                city = '上海'
            else:
                continue
            text = html_path.read_text(encoding='utf-8', errors='ignore')
            list_url = f'https://{"bj" if city == "北京" else "sh"}.lizihang.com/loupan'
            rows = parse_lizihang(text, list_url, city, '丽兹行', None, 'offline_snapshot',
                                  str(html_path))
            pages.append({'page': name, 'city': city, 'snapshot_dir': str(snap),
                          **lizihang_association_counts(rows)})
            per_city[city].extend(rows)

    wanted = ('公园大道', '花木绿城锦绣兰庭')
    cities = {}
    examples = {}
    for city, rows in per_city.items():
        unique = dedup_observations(rows)
        counts = lizihang_association_counts(unique)
        counts['rows_before_dedup'] = len(rows)
        cities[city] = counts
        for row in unique:
            if row.get('name') in wanted:
                examples.setdefault(row.get('name'), []).append({
                    'name': row.get('name'),
                    'page': Path(row['snapshot_path']).name if row.get('snapshot_path') else None,
                    'city': row.get('city'), 'source_id': row.get('source_id'),
                    'link_method': row.get('link_method'),
                    'link_evidence': row.get('link_evidence'),
                    'type_raw': row.get('type_raw'),
                    'coords_raw': row.get('coords_raw'),
                })
    return {
        'snapshot_dirs': [str(Path(d)) for d in snapshot_dirs],
        'pages': pages,
        'cities': cities,
        'examples': examples,
        'relation_note': 'raw_rows >= stable_id_objects and raw_rows >= pending_association; '
                         'never summed into 新增小区',
    }


# ---------------------------------------------------------------------------
# B9: offline object-identity check for detail seeds (parse success != right object)
# ---------------------------------------------------------------------------
def _detail_slug_from_name(name):
    """Recover the xq-... slug from a saved centanet detail snapshot filename."""
    match = re.search(r'_xq_([a-z0-9]+)', name)
    return f'xq-{match.group(1)}' if match else None


def _saved_list_links(raw_dir):
    """slug -> {name, snapshot_path} from saved 中原 list pages' mapPostData (read-only)."""
    links = {}
    for path in sorted(Path(raw_dir).glob('*.html')):
        if 'centanet' not in path.name or '_xq_' in path.name:
            continue
        for entry in parse_centanet_map_post_data(path.read_bytes(), str(path), '上海'):
            if entry['source_id'] and entry['name']:
                links.setdefault(entry['source_id'],
                                 {'name': entry['name'], 'snapshot_path': str(path)})
    return links


def _saved_detail_rows(raw_dir):
    """slug -> parsed detail row from saved 中原 detail pages (read-only, never fetches)."""
    rows = {}
    for path in sorted(Path(raw_dir).glob('*.html')):
        slug = _detail_slug_from_name(path.name)
        if not slug or 'centanet' not in path.name:
            continue
        url = f'https://sh.centanet.com/xiaoqu/{slug}/'
        parsed = parse_centanet_detail(path.read_bytes(), url, '上海', '中原', None,
                                       'offline_snapshot', str(path))
        if parsed:
            rows[slug] = parsed[0]
    return rows


def detail_seed_object_report(raw_dir):
    """B9 offline report: per detail-seed object consistency + slug resolution.

    Saved evidence only. List pages give name->slug link evidence, detail pages give the
    parsed object. A seed whose expected slug has no snapshot is corroborated against the
    list link; saved detail snapshots not claimed by any seed are surfaced as mismatch
    candidates so a wrong slug (parsed fine but wrong object) cannot hide.
    """
    raw_dir = Path(raw_dir)
    list_links = _saved_list_links(raw_dir)
    snapshots = _saved_detail_rows(raw_dir)
    detail_pages = [p for p in COMBOS['sh_centanet']['pages'] if p.get('kind') == 'detail']
    expected_names = [p['expected_name'] for p in detail_pages if p.get('expected_name')]
    expected_slugs = {_url_slug(p.get('url')) for p in detail_pages}

    seeds = []
    for page in detail_pages:
        slug = _url_slug(page.get('url'))
        expected = {'name': page.get('expected_name'),
                    'source_id': page.get('expected_source_id')}
        row = snapshots.get(slug)
        seeds.append({
            'url': page.get('url'), 'slug': slug, 'expected': expected,
            'snapshot_path': row.get('snapshot_path') if row else None,
            'actual': {'source_id': row.get('source_id'), 'name': row.get('name'),
                       'address': row.get('address')} if row else {},
            'list_link_name': (list_links.get(slug) or {}).get('name'),
            'consistency': check_object_consistency(row, expected),
        })

    candidates = []
    for slug in sorted(set(list_links) | set(snapshots)):
        row = snapshots.get(slug)
        name = (row.get('name') if row else None) or (list_links.get(slug) or {}).get('name')
        candidates.append({
            'slug': slug, 'name': name,
            'source': 'detail_snapshot' if row else 'list_link',
            'snapshot_path': row.get('snapshot_path') if row
            else (list_links.get(slug) or {}).get('snapshot_path'),
            'matches_expected': [expected for expected in expected_names
                                 if _name_matches(expected, name)],
        })

    resolution = []
    for seed in seeds:
        if seed['snapshot_path'] is not None:
            continue
        list_name = seed['list_link_name']
        expected_name = seed['expected'].get('name')
        if not (list_name and _name_matches(expected_name, list_name)):
            continue
        wrong = [{'slug': c['slug'], 'name': c['name'], 'verdict': 'object_mismatch'}
                 for c in candidates
                 if c['source'] == 'detail_snapshot' and c['slug'] not in expected_slugs
                 and expected_name not in c['matches_expected']]
        resolution.append({
            'expected_name': expected_name,
            'expected_source_id': seed['slug'],
            'correct_slug': seed['slug'],
            'correct_evidence': {'source': 'list_link', 'name': list_name,
                                 'snapshot_path': (list_links.get(seed['slug'])
                                                   or {}).get('snapshot_path')},
            'wrong_candidates': wrong,
        })

    return {
        'schema': 'source_pilot_detail_object/v1',
        'raw_dir': str(raw_dir),
        'generated_at': now_iso(),
        'seeds': seeds,
        'candidates': candidates,
        'resolution': resolution,
    }


def finish_dataset(out_dir, observations, request_rows, signals):
    """Write observations/checks/summary; NEVER rewrites requests.jsonl (append-only log).

    requests.jsonl is the accumulated disk truth: every real network attempt was already
    appended by run_online and must survive re-runs, so the passed request_rows are ignored.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    observations = merge_observations(read_jsonl(out_dir / 'observations.jsonl'), observations)
    request_rows = read_jsonl(out_dir / 'requests.jsonl')

    write_jsonl(out_dir / 'observations.jsonl', observations)

    existing_checks = read_jsonl(out_dir / 'checks.jsonl')
    checks = dedup_checks(existing_checks + build_checks(observations))
    write_jsonl(out_dir / 'checks.jsonl', checks)

    summary = build_summary(observations, request_rows, signals)
    (out_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


# ---------------------------------------------------------------------------
# offline runner
# ---------------------------------------------------------------------------
def run_offline(args):
    snap_dir = Path(args.snapshot_dir)
    out_dir = Path(args.out)
    observations = []
    signals = {}
    pages = []
    for name, (source, city, kind) in OFFLINE_MAP.items():
        html_path = snap_dir / f'{name}.raw.html'
        if not html_path.exists():
            pages.append({'name': name, 'status': 'missing', 'cards': 0})
            continue
        meta_path = snap_dir / f'{name}.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.exists() else {}
        url = meta.get('url') or meta.get('requested_url') or ''
        fetched_at = meta.get('fetched_at') or now_iso()
        html = html_path.read_bytes()
        try:
            rows = PARSERS[kind](html, url, city, source, fetched_at, 'offline_snapshot', str(html_path))
        except Exception:  # one bad snapshot must not kill the pilot
            rows = []
        observations.extend(rows)
        merge_signals(signals, source, html, snapshot_path=str(html_path), url=url, city=city)
        pages.append({'name': name, 'url': url, 'fetched_at': fetched_at,
                      'cards': len(rows), 'status': 'parsed'})

    summary = finish_dataset(out_dir, observations, [], signals)
    result = {
        'mode': 'parse_only',
        'snapshot_dir': str(snap_dir),
        'out': str(out_dir),
        'pages': pages,
        'observations': len(dedup_observations(observations)),
        'checks': len(build_checks(dedup_observations(observations))),
        'summary': summary,
    }
    print(json.dumps(result, ensure_ascii=False))
    if not observations:
        return 1
    return 0


# ---------------------------------------------------------------------------
# online runner
# ---------------------------------------------------------------------------
def single_request(session, url, timeout):
    started = time.monotonic()
    try:
        response = session.get(url, timeout=(timeout, timeout), allow_redirects=False)
    except requests.RequestException as exc:
        return {'response': None, 'error_type': type(exc).__name__,
                'elapsed_ms': round((time.monotonic() - started) * 1000)}
    return {'response': response, 'error_type': None,
            'elapsed_ms': round((time.monotonic() - started) * 1000)}


def resolve_target(args):
    if args.combo:
        cfg = COMBOS[args.combo]
        source, city = cfg['source'], cfg['city']
        pages = [{'url': args.url, 'kind': None}] if args.url else cfg['pages']
        return source, city, args.combo, pages
    host = urlsplit(args.url).netloc
    source = '丽兹行' if 'lizihang' in host else '中原' if 'centanet' in host else None
    city = '北京' if host.startswith('bj.') else '上海' if host.startswith('sh.') else None
    return source, city, 'custom', [{'url': args.url, 'kind': None}]


def run_online(args, *, store=None, transport=None, clock=None):
    """Fetch pages, reserving one persistent request_id per real attempt (no local budget reset).

    transport/clock are injectable so tests exercise the loop with no network and no sleeping.
    """
    source, city, combo, pages = resolve_target(args)
    out_dir = Path(args.out)
    raw_dir = out_dir / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    transport = transport or single_request
    clock = clock or _now_epoch
    stage = getattr(args, 'stage', '入口与结构探测')
    retries = getattr(args, 'retries', 0)

    run_state_dir = Path(getattr(args, 'run_state_dir', None) or (out_dir / 'run_state'))
    if store is None:
        store = BudgetStore(run_state_dir,
                            trial_total=getattr(args, 'trial_total', TRIAL_TOTAL_CAP),
                            platform_overrides=load_platform_policy_overrides())

    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        print(json.dumps({'mode': 'http', 'combo': combo, 'error': 'budget_lock',
                          'detail': str(exc)}, ensure_ascii=False))
        return 2

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA

    request_rows = []
    observations = []
    signals = {}
    blocked_class = None
    budget_exhausted = False
    internal_error = None
    any_failure = False
    stop = False

    def base_row(url, request_id):
        return {'source': source, 'city': city, 'combo': combo, 'requested_url': url,
                'final_url': url, 'requested_at': now_iso(), 'status': None,
                'redirect_chain': [], 'failure_class': None, 'error_type': None,
                'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
                'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0,
                'body_state': None,
                'request_id': request_id, 'stage': stage, 'platform': source,
                'network_attempt': True}

    def emit(row):
        request_rows.append(row)
        append_jsonl(out_dir / 'requests.jsonl', [row])

    def reserve_attempt(url):
        """Sleep through interval gates; on any hard cap log a refusal row and stop the run."""
        nonlocal budget_exhausted, any_failure, stop
        while True:
            res = store.reserve(source, stage, combo, url, now=clock())
            if res['allowed']:
                return res['request_id']
            if res['reason'] == 'interval_not_elapsed':
                _sleep(res['wait_seconds'])
                continue
            store.record_exhausted(platform=source, stage=stage, combo=combo,
                                   requested_url=url, reason=res['reason'], now=clock())
            refusal = base_row(url, None)
            refusal['failure_class'] = 'budget_exhausted'
            refusal['network_attempt'] = False
            refusal['blocking_reason'] = res['reason']
            emit(refusal)
            budget_exhausted = True
            any_failure = True
            stop = True
            return None

    def process_attempt(url, chain, page_kind):
        """One reserve + one transport call + one fully-populated, immediately-appended row."""
        nonlocal any_failure
        request_id = reserve_attempt(url)
        if request_id is None:
            return None
        row = base_row(url, request_id)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, args.timeout)
        response = outcome['response']
        row['elapsed_ms'] = outcome['elapsed_ms']
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome['error_type']
            row['body_state'] = 'no_body'
            emit(row)
            return row

        body = response.content
        status = response.status_code
        row['status'] = status
        row['content_type'] = response.headers.get('Content-Type')
        row['retry_after'] = response.headers.get('Retry-After')
        row['bytes'] = len(body)
        row['sha256'] = hashlib.sha256(body).hexdigest()
        # Explicit marker: only a non-empty body (success or not) becomes a snapshot file.
        row['body_state'] = 'retained' if body else 'no_body'
        if body:
            # Keep every body, blocked/failed included, as evidence.
            row['snapshot_path'] = str(save_raw(raw_dir, combo, url, body, row['sha256']))

        if status == 403:
            row['failure_class'] = 'blocked_403'
        elif status == 429:
            row['failure_class'] = 'blocked_429'
        elif status == 202:
            row['failure_class'] = 'http_202'
        elif 300 <= status < 400 and response.headers.get('Location'):
            location = response.headers.get('Location')
            next_url = urljoin(url, location)
            row['redirect_chain'] = list(chain) + [{'url': url, 'status': status,
                                                    'location': location, 'next': next_url}]
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        else:
            doc = BeautifulSoup(body, 'html.parser')
            title = doc.title.get_text(' ', strip=True) if doc.title else ''
            text = doc.get_text(' ', strip=True)
            parser = parser_for(source, page_kind, url)
            cards = parser(body, url, city, source, row['requested_at'], 'http',
                           row['snapshot_path']) if parser else []
            has_cards = len(cards) > 0
            row['cards_parsed'] = len(cards)
            if not body:
                row['failure_class'] = 'empty_body'
            elif is_challenge(title, text, has_cards):
                row['failure_class'] = 'challenge'
            elif not has_cards:
                row['failure_class'] = 'no_cards'
                any_failure = True
            else:
                observations.extend(cards)
                merge_signals(signals, source, body, snapshot_path=row.get('snapshot_path'),
                              url=url, city=city)

        emit(row)
        return row

    try:
        for entry in pages:
            page_url = entry['url']
            page_kind = entry.get('kind')
            if stop:
                break

            current = page_url
            chain = []
            while True:
                attempts = 0
                row = None
                while True:
                    row = process_attempt(current, chain, page_kind)
                    if row is None:  # budget blocked; stop already set
                        break
                    connection_error = (row['status'] is None
                                        and row['failure_class'] == 'transport_error')
                    if not connection_error or attempts >= retries:
                        break
                    attempts += 1
                if row is None or stop:
                    break

                if row['status'] is None:
                    internal_error = row['error_type']
                    any_failure = True
                    stop = True
                    break

                redirect = (300 <= row['status'] < 400 and row['redirect_chain']
                            and row['redirect_chain'][-1]['url'] == current)
                if redirect:
                    if len(row['redirect_chain']) >= MAX_REDIRECT_HOPS:
                        internal_error = 'TooManyRedirects'
                        any_failure = True
                        stop = True
                        break
                    chain = row['redirect_chain']
                    current = chain[-1]['next']
                    continue

                if row['failure_class'] == 'transport_error':
                    internal_error = row['error_type']
                    any_failure = True
                    stop = True
                    break

                if row['failure_class'] in STOP_CLASSES:
                    blocked_class = row['failure_class']
                    stop = True
                break

        # Mirror non-success/challenge/blocked bodies so a later 200 cannot erase the evidence.
        for row in request_rows:
            if row.get('snapshot_path') and (row.get('failure_class')
                                             or row.get('status') != 200):
                copy_to_failed(raw_dir, row['snapshot_path'])

        summary = finish_dataset(out_dir, observations, request_rows, signals)
    finally:
        store.release_lock()

    request_ids = sum(1 for row in request_rows if row.get('request_id') is not None)
    all_ok = (not any_failure) and not budget_exhausted and blocked_class is None
    result = {
        'mode': 'http',
        'combo': combo,
        'source': source,
        'city': city,
        'url': args.url,
        'out': str(out_dir),
        'requests_used': request_ids,
        'request_ids': request_ids,
        'budget': store.report(now=clock()),
        'observations': len(dedup_observations(observations)),
        'blocked': {'blocked': blocked_class is not None, 'class': blocked_class},
        'budget_exhausted': budget_exhausted,
        'summary': summary,
    }
    print(json.dumps(result, ensure_ascii=False))

    if internal_error:
        return 1
    if not all_ok:
        return 2
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description='Bounded free-source pilot sampler.')
    parser.add_argument('--combo', choices=sorted(COMBOS),
                        help='source/city combo to sample (required unless --url or --parse-only)')
    parser.add_argument('--url', help='explicit single URL to fetch, overrides combo page list')
    parser.add_argument('--budget', type=int, default=20, help='max HTTP requests for this combo')
    parser.add_argument('--timeout', type=float, default=20, help='connect/read timeout seconds')
    parser.add_argument('--out', default=None,
                        help='output directory (default output/source_trial_<trial-date>)')
    parser.add_argument('--interval', type=float, default=5.0, help='seconds between requests (min 5)')
    parser.add_argument('--parse-only', action='store_true', help='parse local snapshots, no network')
    parser.add_argument('--snapshot-dir', default='output/data_source_research_20260921',
                        help='directory with saved snapshots for --parse-only')
    parser.add_argument('--stage', choices=list(STAGE_CAPS), default='入口与结构探测',
                        help='budget stage this run draws from')
    parser.add_argument('--trial-date', default=DEFAULT_TRIAL_DATE,
                        help='trial date used to default the output dir')
    parser.add_argument('--run-state-dir', default=None,
                        help='directory holding budget.json/budget.lock (default <out>/run_state)')
    parser.add_argument('--trial-total', type=int, default=TRIAL_TOTAL_CAP,
                        help='trial-wide request cap')
    parser.add_argument('--retries', type=int, default=0,
                        help='extra attempts after a transport error (each counts)')
    parser.add_argument('--report-budget', action='store_true',
                        help='print the persisted budget report and exit')
    parser.add_argument('--embedded-report', action='store_true',
                        help='count embedded-data extraction per snapshot and exit')
    parser.add_argument('--identity-types-report', action='store_true',
                        help='recompute identity/type model from saved evidence and exit')
    parser.add_argument('--replay-report', action='store_true',
                        help='write the replayable-ratio report for saved requests and exit')
    parser.add_argument('--lizihang-identity-report', action='store_true',
                        help='re-parse saved Lizihang snapshots and report A5 association counts')
    parser.add_argument('--observations',
                        default='output/source_pilot_20260921/observations.jsonl',
                        help='observations.jsonl for --identity-types-report')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not (args.parse_only or args.url or args.combo or args.report_budget
            or args.embedded_report or args.identity_types_report or args.replay_report
            or args.lizihang_identity_report):
        parser.error('one of --combo, --url, --parse-only, --report-budget, '
                     '--embedded-report, --identity-types-report, --replay-report or '
                     '--lizihang-identity-report is required')
    # Hard floor: never hammer a source faster than one request per 5 seconds.
    args.interval = max(MIN_INTERVAL_SECONDS, args.interval)
    if args.out is None:
        args.out = f'output/source_trial_{args.trial_date}'

    validate_budget_config(budget=args.budget, trial_total=args.trial_total,
                           retries=args.retries, interval=args.interval)

    if args.identity_types_report:
        rows = read_jsonl(Path(args.observations))
        report = build_identity_types_report(rows, raw_dir=Path(args.observations).parent / 'raw',
                                             source_path=args.observations)
        out_path = Path(args.out) / 'identity_types.json'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report['old_vs_new'], ensure_ascii=False))
        return 0

    if args.embedded_report:
        report = build_embedded_report(args.snapshot_dir)
        out_path = Path(args.out) / 'embedded_counts.json'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        return 0

    if args.report_budget:
        store = BudgetStore(args.run_state_dir or (Path(args.out) / 'run_state'),
                            trial_total=args.trial_total,
                            platform_overrides=load_platform_policy_overrides())
        print(json.dumps(store.report(), ensure_ascii=False, indent=2))
        return 0

    if args.replay_report:
        report = write_replay_report()
        print(json.dumps({key: report[key] for key in ('numerator', 'denominator', 'ratio')},
                         ensure_ascii=False))
        return 0

    if args.lizihang_identity_report:
        snapshot_dirs = ['output/source_trial_20260922/raw',
                         'output/source_pilot_20260921/raw']
        report = build_lizihang_identity_report(snapshot_dirs)
        out_path = Path(args.out) / 'lizihang_identity.json'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        print(json.dumps(report['cities'], ensure_ascii=False))
        return 0

    if args.parse_only:
        return run_offline(args)
    return run_online(args)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
