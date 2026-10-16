"""Anjuke (安居客) mobile community-directory adapter: budgeted network entry + offline replay.

Isolated from the existing pilot: reuses source_pilot's budget/persistence/raw-io helpers by
import only (source_pilot.py is never modified). Every parser is pure and accepts saved HTML,
so `replay` never needs the network. No network call happens at import time.

Safety rules mirror the pilot: single concurrency, trust_env disabled (no env proxies), constant
plain UA, manual redirects that each count against the budget, stop on block/challenge, never
touch production data.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from source_pilot import (
    BudgetStore, BudgetLockError, save_raw, copy_to_failed, challenge_evidence,
    is_challenge, now_iso, append_jsonl, read_jsonl, slug_of, _to_text, _now_epoch,
    MIN_INTERVAL_SECONDS, TRIAL_TOTAL_CAP, COMBINED_DAILY_CAP, PLATFORM_DAILY_CAP,
)

# ---------------------------------------------------------------------------
# A) constants
# ---------------------------------------------------------------------------
SOURCE = '安居客'
PLATFORM = '安居客'
MOBILE_BJ_CATALOG = 'https://m.anjuke.com/bj/xiaoqu/'
MOBILE_SH_CANDIDATE = 'https://m.anjuke.com/sh/xiaoqu/'
STAGE_CAPS_ANJUKE = {'入口探测': 40, '分区目录': 600, '详情与候选核对': 120, '续跑复查': 40}
ANJUKE_ROUND_CAP = 800
GLOBAL_LEDGER = 'output/source_trial_20260922/run_state/budget.json'
GLOBAL_TRIAL_CAP = 4000
MIN_INTERVAL = 5.0
PLATFORM_DAILY_CAP_ANJUKE = 300
COMBINED_DAILY_CAP_ANJUKE = 600
MAX_REDIRECT_HOPS = 5
# The shared cumulative trial count this round seeds from (frozen ledger snapshot).
SEED_BASE_TRIAL_USED = 597
VERSION = 'anjuke_community/v1'
# A single plain UA covering desktop and mobile rendering paths.
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')

# ---------------------------------------------------------------------------
# parsing constants
# ---------------------------------------------------------------------------
EMPTY_MARKERS = ('暂无小区', '无小区', '暂无数据', '暂无结果', '没有找到', '未找到相关',
                 '暂无匹配', '暂无内容', '空空如也')
PAGE_DECLARED_RE = re.compile(r'共\s*([\d,]+)\s*个小区')
DETAIL_PATH_RE = re.compile(r'/(?:community|xiaoqu)/([^/?#]+?)(?:\.html)?/?$')
DISTRICT_PATH_RE = re.compile(r'/(?:xiaoqu|community|haofang)/([^/?#]+)/?$')
CITY_PATH_RE = re.compile(r'^/([a-z]{2,6})/xiaoqu/?$')
NON_COMMUNITY_PATH_RE = re.compile(r'/(?:fangjia|loupan|esf|sale)(?:/|$)')
# Body text markers that mean a real challenge/login-wall, never a plain nav 登录 link.
CHALLENGE_TEXT_MARKERS = ('安全验证', '访问已被拦截', '滑块验证', '拖动滑块', '请完成验证',
                          '人机验证', '访问过于频繁', '操作过于频繁', '操作频繁',
                          '请先登录', '登录后查看', 'captcha', 'verify_slider', 'verify-slider',
                          'antibot')
# The 58/安居客 anti-bot gateway shell (hidden gateway URL + JS redirect), matched lowercased.
GATEWAY_MARKERS = ('@@xxzlgatewayurl', 'callback.58.com/antibot', 'antibot/verifycode',
                   'serialid=', 'namespace=cloud_ajk')
CATALOG_CONTAINER_SELECTORS = (
    'ul.list', '.list-content', '.list-wrap', '[class*="xiaoqu-list"]',
    '[class*="community-list"]', '[class*="list-container"]', '.house-list',
    '[class*="list-box"]', '[class*="xiaoqu_list"]',
)
HOUSE_FORM_TAGS = ('别墅', '独栋', '双拼', '联排', '叠拼', '楼房', '公寓')
VILLA_TAGS = ('别墅', '独栋', '双拼', '联排', '叠拼')
DETAIL_COORD_NOTE = '详情页含坐标但页面未标注坐标系,候选GCJ-02/WGS-84/BD-09,待核不得直接采用'

# embedded-JSON key vocabularies (first present non-empty wins)
NAME_KEYS = ('name', 'community_name', 'xiaoqu_name', 'communityName', 'xq_name',
             'resblock_name', 'name_text', 'title')
URL_KEYS = ('url', 'detail_url', 'community_url', 'xiaoqu_url', 'detailUrl', 'link', 'href')
ID_KEYS = ('id', 'community_id', 'xiaoqu_id', 'communityId', 'xq_id')
DISTRICT_KEYS = ('district', 'district_name', 'districtName', 'area', 'region', 'quyu')
SUBAREA_KEYS = ('subarea', 'sub_area', 'bizcircle', 'block', 'business_area', 'shangquan')
ADDRESS_KEYS = ('address', 'addr', 'address_text', 'addr_text')
YEAR_KEYS = ('build_year', 'year', 'built_year', 'buildYear', 'completion_year',
             'nianfen', 'build_year_text')
TYPE_KEYS = ('type_tags', 'tags', 'house_type', 'property_type', 'wuxing', 'type')
LNG_KEYS = ('lng', 'longitude', 'map_x', 'mapX', 'x')
LAT_KEYS = ('lat', 'latitude', 'map_y', 'mapY', 'y')
PRICE_KEYS = ('price', 'price_text', 'avg_price', 'reference_price', 'priceText')
SALE_KEYS = ('on_sale', 'sale_count', 'onsale', 'on_sale_count', 'saleCount')
RENT_KEYS = ('on_rent', 'rent_count', 'onrent', 'on_rent_count', 'rentCount')

YEAR_SINGLE_RE = re.compile(r'(\d{4})\s*年')
YEAR_RANGE_RE = re.compile(r'(\d{4})\s*[-~至到]\s*(\d{4})\s*年')
ADDRESS_RE = re.compile(r'(?:小区)?地址[：:]\s*([^\s；;，,]+)')
DISTRICT_RE = re.compile(r'(?:行政区|区县|区域)[：:]\s*([^\s；;，,]+)')
SUBAREA_RE = re.compile(r'(?:商圈|板块|片区|区域)[：:]\s*([^\s；;，,]+)')
PHASE_RE = re.compile(r'(\d+|[一二三四五六七八九十]+)\s*期')
GROUP_RE = re.compile(r'(?:组团|组)[：:]\s*([^\s；;，,]+)')
PARENT_RE = re.compile(r'父小区[：:]\s*([^\s；;，,]+)')
COORD_JSON_RE = re.compile(
    r'"?lng"?\s*[:=]\s*(-?\d+(?:\.\d+)?)[^0-9-]+"?lat"?\s*[:=]\s*(-?\d+(?:\.\d+)?)')
COORD_PAIR_RE = re.compile(r'(-?1[12]\d\.\d+)\s*[,，]\s*(-?[23]\d\.\d+)')
PRICE_RE = re.compile(r'(?:参考)?(?:售价|均价|价格)[：:]\s*([0-9][0-9,\.]*\s*元/[㎡平]?米?)')
RENT_TEXT_RE = re.compile(r'(?:参考)?租金[：:]\s*([0-9][0-9,\.]*\s*元/(?:月|㎡|平))')
ONSALE_RE = re.compile(r'在售\s*(\d+)\s*套')
ONRENT_RE = re.compile(r'在租\s*(\d+)\s*套')

# capability fields -> (record key, value kind)
FIELD_DEFS = (
    ('name', 'name', 'text'),
    ('stable_id', 'stable_id', 'text'),
    ('detail_link', 'detail_url', 'text'),
    ('alias', 'alias', 'text'),
    ('city', 'city', 'text'),
    ('district', 'district_text', 'text'),
    ('subarea', 'subarea_text', 'text'),
    ('address', 'address_text', 'text'),
    ('phase', 'phase', 'text'),
    ('year_raw', 'year_text', 'year'),
    ('year_scope', 'year_scope', 'text'),
    ('type_tags', 'type_tags', 'list'),
    ('villa_proof', 'villa_proof', 'text'),
    ('lng_raw', 'lng', 'coord'),
    ('lat_raw', 'lat', 'coord'),
    ('coord_system_evidence', 'coord_system_evidence', 'text'),
    ('location_meaning', 'location_meaning', 'text'),
    ('group', 'group', 'text'),
    ('parent', 'parent', 'text'),
    ('price_text', 'price_text', 'text'),
    ('rent_text', 'rent_text', 'text'),
    ('on_sale_count', 'on_sale_count', 'count'),
    ('on_rent_count', 'on_rent_count', 'count'),
)
FIELD_GROUPS = (
    ('小区身份', ['name', 'stable_id', 'detail_link', 'alias', 'city']),
    ('行政区与地址', ['district', 'subarea', 'address', 'phase']),
    ('建成年份', ['year_raw', 'year_scope']),
    ('住宅形态', ['type_tags', 'villa_proof']),
    ('坐标', ['lng_raw', 'lat_raw', 'coord_system_evidence', 'location_meaning']),
    ('分期关系', ['phase', 'group', 'parent']),
    ('参考价格/租金', ['price_text', 'rent_text']),
    ('在售/在租数量', ['on_sale_count', 'on_rent_count']),
)
FORBIDDEN_SUBSTITUTION = {
    'stable_id': '禁止用房源/挂牌id替代小区稳定id',
    'detail_link': '禁止用列表/价格页链接替代小区详情链接',
    'city': '禁止用页面站点推断城市',
    'year_raw': '禁止用挂牌/交易年份替代建成年份',
    'year_scope': '禁止把未知范围当作单一年份',
    'lng_raw': '禁止把未核实坐标系原值当作已核实坐标',
    'lat_raw': '禁止把未核实坐标系原值当作已核实坐标',
    'coord_system_evidence': '禁止在缺少证据时断定坐标系',
    'location_meaning': '禁止把行政区中心点替代小区位置',
    'price_text': '禁止把挂牌价替代参考价',
    'rent_text': '禁止把在租房源租金替代小区参考租金',
    'on_sale_count': '禁止把缺失当作0',
    'on_rent_count': '禁止把缺失当作0',
    'group': '禁止用同名小区推断分期关系',
    'parent': '禁止用同名推断父小区',
    'phase': '禁止用名称推断分期',
    'villa_proof': '禁止以名称含别墅作为住宅形态证据',
    'type_tags': '禁止由URL路径推断住宅形态',
}


def _sleep(seconds):
    """Sleep hook; injectable clock keeps tests off real time."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# B) budget store seeded from the shared global ledger
# ---------------------------------------------------------------------------
class AnjukeBudgetStore(BudgetStore):
    """Cross-process budget for Anjuke that never resets a fresh local budget.json.

    First use seeds a fresh state from the shared global ledger (trial_used/next_request_id
    carried forward), so request ids stay globally unique and cumulative. Counters persist.
    """

    def __init__(self, run_state_dir, *, clock=None):
        super().__init__(
            run_state_dir,
            stage_caps=STAGE_CAPS_ANJUKE,
            trial_total=GLOBAL_TRIAL_CAP,
            platform_daily_caps={'安居客': PLATFORM_DAILY_CAP_ANJUKE},
            combined_daily_cap=COMBINED_DAILY_CAP_ANJUKE,
            min_interval_seconds=MIN_INTERVAL,
            clock=clock or _now_epoch,
        )
        self.anjuke_round_cap = ANJUKE_ROUND_CAP

    def load(self):
        if self.budget_path.exists():
            return json.loads(self.budget_path.read_text(encoding='utf-8'))
        state = self._default_state()
        try:
            global_state = json.loads(Path(GLOBAL_LEDGER).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            global_state = {}
        for key in ('trial_used', 'next_request_id', 'platform_daily_used',
                    'combined_daily_used', 'last_request_at', 'platform_paused',
                    'budget_exhausted'):
            if key in global_state:
                state[key] = json.loads(json.dumps(global_state[key]))
        state['seeded_from'] = GLOBAL_LEDGER
        state['seeded_at'] = now_iso()
        self._save(state)
        return state

    def reserve(self, platform, stage, combo=None, requested_url=None, now=None):
        """Super's decision, but refuse once this round has spent its 800 Anjuke requests."""
        state = self.load()
        used_this_round = state.get('trial_used', 0) - SEED_BASE_TRIAL_USED
        if used_this_round >= self.anjuke_round_cap:
            return self._refused('anjuke_round_cap')
        return super().reserve(platform, stage, combo, requested_url, now=now)

    def reconciliation(self):
        """Global vs round budget: effective_remaining = min(global_remaining, round_remaining)."""
        state = self.load()
        global_used = state.get('trial_used', 0)
        global_remaining = max(0, self.trial_total - global_used)
        used_this_round = max(0, global_used - SEED_BASE_TRIAL_USED)
        round_remaining = max(0, self.anjuke_round_cap - used_this_round)
        return {
            'global_ledger': GLOBAL_LEDGER,
            'global_used': global_used,
            'global_cap': self.trial_total,
            'global_remaining': global_remaining,
            'anjuke_round_cap': self.anjuke_round_cap,
            'effective_remaining': min(global_remaining, round_remaining),
        }


# ---------------------------------------------------------------------------
# C) pure parsers (accept bytes|str html; never raise out of the public API)
# ---------------------------------------------------------------------------
def _first(d, keys):
    for key in keys:
        if key in d:
            value = d[key]
            if value not in (None, '', [], {}):
                return value
    return None


def _raw_first(d, keys):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r'-?\d+', value.replace(',', ''))
        return int(match.group(0)) if match else None
    return None


def _to_tags(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in re.split(r'[/,，、|;；\s]+', value) if t.strip()]
    return []


def _city_from_url(url):
    if not url:
        return None
    path = urlsplit(url).path
    match = re.match(r'^/([a-z]{2,6})/', path)
    if not match:
        return None
    return {'bj': '北京', 'sh': '上海'}.get(match.group(1))


def _year_scope_of(text):
    if not text:
        return None
    if YEAR_RANGE_RE.search(text):
        return 'range'
    if YEAR_SINGLE_RE.search(text):
        return 'single'
    return None


def _phase_of(name):
    if not name:
        return None
    match = PHASE_RE.search(name)
    return match.group(0).strip() if match else None


def _looks_like_detail_id(seg):
    """True only for a real community id: >=3 digits, optional slug affixes, never 'pN'."""
    if not seg:
        return False
    seg = re.sub(r'\.html$', '', seg)
    return bool(re.fullmatch(r'(?!p\d+$)[A-Za-z0-9_-]*\d{3,}[A-Za-z0-9_-]*', seg))


def _detail_segment(url):
    """The path segment right after /community/ or /xiaoqu/, iff the path has one."""
    match = DETAIL_PATH_RE.search(urlsplit(url or '').path)
    return match.group(1) if match else None


def _community_id_from_url(url):
    """Stable community id from a detail URL; never a listing/house/pagination id."""
    if not url:
        return None
    seg = _detail_segment(url)
    if _looks_like_detail_id(seg):
        return re.sub(r'\.html$', '', seg)
    trailing = re.search(r'/(\d{3,})/?$', urlsplit(url).path)
    return trailing.group(1) if trailing else None


def _is_community_detail_url(url):
    if NON_COMMUNITY_PATH_RE.search(urlsplit(url).path):
        return False
    return _looks_like_detail_id(_detail_segment(url))


def _is_district_url(url):
    """A district/板块 catalog slug: alpha only, never a pagination pN or a community id."""
    match = DISTRICT_PATH_RE.search(urlsplit(url).path)
    if not match:
        return False
    seg = match.group(1)
    if re.fullmatch(r'p\d+', seg) or _looks_like_detail_id(seg):
        return False
    return bool(re.fullmatch(r'[a-z][a-z_-]{2,}', seg))


def _is_city_path(url):
    return bool(CITY_PATH_RE.match(urlsplit(url).path))


def _is_next_page(anchor, url):
    text = anchor.get_text(' ', strip=True)
    if '下一页' in text or text in ('下页', '»'):
        return True
    rel = anchor.get('rel') or []
    if 'next' in rel:
        return True
    query = parse_qs(urlsplit(url).query)
    if query.get('page') and query['page'][0] not in ('1', ''):
        return True
    return bool(re.search(r'/p\d+/?$', urlsplit(url).path))


def _json_record(node, base_url, location='embedded_json', page_url=None):
    name = _as_text(_first(node, NAME_KEYS))
    url_raw = _first(node, URL_KEYS)
    detail_url = urljoin(base_url, url_raw) if isinstance(url_raw, str) and url_raw else None
    cid = _first(node, ID_KEYS)
    stable_id = str(cid) if cid is not None else _community_id_from_url(detail_url)
    year_text = _as_text(_first(node, YEAR_KEYS))
    type_tags = _to_tags(_first(node, TYPE_KEYS))
    # A card without a detail URL still sits under the city section of the catalog page.
    city = _city_from_url(detail_url)
    city_location = location if city else None
    if city is None and page_url:
        city = _city_from_url(page_url)
        if city:
            city_location = 'url_city_section'
    record = {
        'name': name,
        'detail_url': detail_url,
        'stable_id': stable_id,
        'district_text': _as_text(_first(node, DISTRICT_KEYS)),
        'subarea_text': _as_text(_first(node, SUBAREA_KEYS)),
        'address_text': _as_text(_first(node, ADDRESS_KEYS)),
        'year_text': year_text,
        'year_scope': _year_scope_of(year_text),
        'type_tags': type_tags,
        'lng': _raw_first(node, LNG_KEYS),
        'lat': _raw_first(node, LAT_KEYS),
        'coord_system_evidence': None,
        'price_text': _as_text(_first(node, PRICE_KEYS)),
        'rent_text': _as_text(_first(node, RENT_KEYS)),
        'on_sale_count': _to_int(_raw_first(node, SALE_KEYS)),
        'on_rent_count': _to_int(_raw_first(node, RENT_KEYS)),
        'city': city,
        'alias': None,
        'phase': _phase_of(name),
        'group': None,
        'parent': None,
        'location_meaning': None,
        'villa_proof': next((t for t in type_tags if t in VILLA_TAGS), None),
        '_locations': {},
    }
    record['_locations'] = {defn[1]: location for defn in FIELD_DEFS}
    record['_locations']['detail_url'] = 'embedded_json' if detail_url else None
    record['_locations']['city'] = city_location
    return record


def _looks_like_community(node):
    keys = set(node.keys())
    if not any(k in keys for k in NAME_KEYS):
        return False
    if any(k in keys for k in URL_KEYS) or any(k in keys for k in ID_KEYS):
        return True
    signals = 0
    for group in (DISTRICT_KEYS, SUBAREA_KEYS, ADDRESS_KEYS, YEAR_KEYS, LNG_KEYS, PRICE_KEYS):
        if any(k in keys for k in group):
            signals += 1
    return signals >= 2


def _walk_json(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _iter_embedded_json(raw):
    for marker in ('__INITIAL_STATE__', '__NEXT_DATA__'):
        pos = raw.find(marker)
        if pos == -1:
            continue
        for opener in ('{', '['):
            start = raw.find(opener, pos)
            if start == -1:
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(raw, start)
            except ValueError:
                continue
            yield value
            break
    for match in re.finditer(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S | re.I):
        try:
            yield json.loads(match.group(1).strip())
        except ValueError:
            continue


def _embedded_records(raw, url):
    records = []
    try:
        for value in _iter_embedded_json(raw):
            for node in _walk_json(value):
                if isinstance(node, dict) and _looks_like_community(node):
                    records.append(_json_record(node, url, page_url=url))
    except Exception:
        return records
    seen = set()
    unique = []
    for record in records:
        key = record.get('detail_url') or record.get('name')
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _year_from_text(text):
    if not text:
        return None, None
    range_match = YEAR_RANGE_RE.search(text)
    if range_match:
        return range_match.group(0).strip(), 'range'
    single = YEAR_SINGLE_RE.search(text)
    if single:
        return single.group(0).strip(), 'single'
    return None, None


def _tags_from_text(text):
    return [tag for tag in HOUSE_FORM_TAGS if text and tag in text]


def _dom_record(anchor, url):
    absu = urljoin(url, anchor['href'])
    name = anchor.get_text(' ', strip=True) or anchor.get('title')
    card = anchor.find_parent(['li', 'div', 'section'])
    text = card.get_text(' ', strip=True) if card else (name or '')
    year_text, year_scope = _year_from_text(text)
    type_tags = _tags_from_text(text)
    record = {
        'name': _as_text(name),
        'detail_url': absu,
        'stable_id': _community_id_from_url(absu),
        'district_text': None,
        'subarea_text': None,
        'address_text': None,
        'year_text': year_text,
        'year_scope': year_scope,
        'type_tags': type_tags,
        'lng': None, 'lat': None,
        'coord_system_evidence': None,
        'price_text': None, 'rent_text': None,
        'on_sale_count': None, 'on_rent_count': None,
        'city': _city_from_url(absu),
        'alias': None,
        'phase': _phase_of(name),
        'group': None, 'parent': None,
        'location_meaning': None,
        'villa_proof': next((t for t in type_tags if t in VILLA_TAGS), None),
        '_locations': {defn[1]: 'dom' for defn in FIELD_DEFS},
    }
    return record


def _dom_records(doc, url):
    records = []
    seen = set()
    for anchor in doc.find_all('a', href=True):
        absu = urljoin(url, anchor['href'])
        if not _is_community_detail_url(absu) or absu in seen:
            continue
        seen.add(absu)
        records.append(_dom_record(anchor, url))
    return records


def _collect_links(doc, url):
    out = {'district_links': [], 'city_links': [], 'detail_links': [], 'next_page_url': None}
    seen = set()
    for anchor in doc.find_all('a', href=True):
        href = anchor['href']
        if not href or href.startswith(('javascript:', '#')):
            continue
        absu = urljoin(url, href)
        if 'anjuke.com' not in urlsplit(absu).netloc or absu in seen:
            continue
        if NON_COMMUNITY_PATH_RE.search(urlsplit(absu).path):
            continue
        name = anchor.get_text(' ', strip=True)
        if _is_community_detail_url(absu):
            seen.add(absu)
            out['detail_links'].append(absu)
            continue  # a detail link is never also pagination
        elif _is_city_path(absu):
            seen.add(absu)
            out['city_links'].append({'name': name, 'url': absu})
        elif _is_district_url(absu):
            seen.add(absu)
            out['district_links'].append({'name': name, 'url': absu})
        if out['next_page_url'] is None and _is_next_page(anchor, absu):
            out['next_page_url'] = absu
    return out


def _declared_count(text):
    match = PAGE_DECLARED_RE.search(text or '')
    if match:
        return int(match.group(1).replace(',', ''))
    fallback = re.search(r'(\d+)\s*个小区', text or '')
    return int(fallback.group(1)) if fallback else None


def parse_catalog(html, *, url, fetched_at, snapshot_path=None):
    """Catalog page -> records + nav links. Pure; returns exact keys, never raises."""
    result = {'records': [], 'next_page_url': None, 'district_links': [], 'city_links': [],
              'detail_links': [], 'declared_count': None, 'container_found': False,
              'empty_marker': False, 'title': ''}
    try:
        raw = _to_text(html)
        doc = BeautifulSoup(raw, 'html.parser')
        result['title'] = doc.title.get_text(' ', strip=True) if doc.title else ''
        text = doc.get_text(' ', strip=True)
        links = _collect_links(doc, url)
        result['district_links'] = links['district_links']
        result['city_links'] = links['city_links']
        result['detail_links'] = links['detail_links']
        result['next_page_url'] = links['next_page_url']
        embedded = _embedded_records(raw, url)
        dom_records = _dom_records(doc, url) if not embedded else []
        result['records'] = embedded or dom_records
        result['declared_count'] = _declared_count(text)
        result['empty_marker'] = any(marker in text for marker in EMPTY_MARKERS)
        result['container_found'] = bool(embedded) or bool(dom_records) or \
            bool(doc.select(','.join(CATALOG_CONTAINER_SELECTORS)))
    except Exception:
        return result
    return result


def _detail_name(doc, title):
    node = doc.select_one('h1')
    if node and node.get_text(strip=True):
        return node.get_text(' ', strip=True)
    meta = doc.find('meta', attrs={'property': 'og:title'}) or \
        doc.find('meta', attrs={'name': 'title'})
    if meta and meta.get('content'):
        return meta['content'].strip()
    if title:
        return re.split(r'[-_|]', title)[0].strip() or None
    return None


def _match_text(pattern, text):
    match = pattern.search(text or '')
    return match.group(1).strip() if match else None


def parse_detail(html, *, url, fetched_at, snapshot_path=None):
    """Detail page -> one record + sections. Pure; returns exact keys, never raises."""
    result = {'record': None, 'title': '', 'sections': {}, 'container_found': False}
    try:
        raw = _to_text(html)
        doc = BeautifulSoup(raw, 'html.parser')
        title = doc.title.get_text(' ', strip=True) if doc.title else ''
        result['title'] = title
        text = doc.get_text(' ', strip=True)

        name = _detail_name(doc, title)
        address = _match_text(ADDRESS_RE, text)
        district = _match_text(DISTRICT_RE, text)
        subarea = _match_text(SUBAREA_RE, text)
        if not district and address:
            district = re.search(r'([^省市区县]{2,8}[区县])', address)
            district = district.group(1) if district else None
        year_text, year_scope = _year_from_text(text)
        type_tags = _tags_from_text(text)
        group = _match_text(GROUP_RE, text)
        parent = _match_text(PARENT_RE, text)
        phase = _phase_of(name)

        lng = lat = None
        coord = COORD_JSON_RE.search(raw) or COORD_PAIR_RE.search(raw)
        if coord:
            lng, lat = coord.group(1), coord.group(2)
        coord_note = DETAIL_COORD_NOTE if lng is not None else None

        price_text = _match_text(PRICE_RE, text)
        rent_text = _match_text(RENT_TEXT_RE, text)
        on_sale = ONSALE_RE.search(text)
        on_rent = ONRENT_RE.search(text)

        stable_id = _community_id_from_url(url)
        if lng is not None:
            location_meaning = '详情页原始坐标,坐标系未核实'
        elif address:
            location_meaning = '页面文本地址'
        else:
            location_meaning = None

        record = {
            'name': name,
            'detail_url': url,
            'stable_id': stable_id,
            'district_text': district,
            'subarea_text': subarea,
            'address_text': address,
            'year_text': year_text,
            'year_scope': year_scope,
            'type_tags': type_tags,
            'lng': lng,
            'lat': lat,
            'coord_system_evidence': coord_note,
            'price_text': price_text,
            'rent_text': rent_text,
            'on_sale_count': int(on_sale.group(1)) if on_sale else None,
            'on_rent_count': int(on_rent.group(1)) if on_rent else None,
            'city': _city_from_url(url),
            'alias': None,
            'phase': phase,
            'group': group,
            'parent': parent,
            'location_meaning': location_meaning,
            'villa_proof': next((t for t in type_tags if t in VILLA_TAGS), None),
            '_locations': {defn[1]: 'dom' for defn in FIELD_DEFS},
        }
        result['record'] = record
        result['container_found'] = bool(
            record['name'] or record['year_text'] or record['type_tags']
            or record['address_text'] or record['lng'] is not None)
        result['sections'] = _detail_sections(record)
    except Exception:
        return result
    return result


def _detail_sections(record):
    sections = {}
    for label, fields in FIELD_GROUPS:
        present = [field for field in fields if _field_has_value(record, field)]
        if present:
            sections[label] = present
    return sections


def _field_has_value(record, field):
    record_key = next((defn[1] for defn in FIELD_DEFS if defn[0] == field), field)
    value = record.get(record_key)
    return value not in (None, '', [], {})


def _coord_state(lng, lat):
    if lng is None and lat is None:
        return 'missing'
    if lng is not None and lat is not None:
        return 'observed'
    return 'partial'


def normalize_fields(record):
    """Per-field {'value','raw','state','location'}; missing != zero; keeps raw + location."""
    record = record or {}
    locations = record.get('_locations') or {}
    out = {}
    for field, record_key, kind in FIELD_DEFS:
        raw = record.get(record_key)
        if kind == 'count':
            state = 'observed' if record_key in record and raw is not None else 'missing'
            value = raw if state == 'observed' else None
        elif kind == 'year':
            if raw in (None, ''):
                state, value = 'missing', None
            elif record.get('year_scope') in ('single', 'range'):
                state, value = 'observed', raw
            else:
                state, value = 'partial', raw
        elif kind == 'list':
            state = 'observed' if raw else 'missing'
            value = list(raw) if state == 'observed' else None
        elif kind == 'coord':
            state = _coord_state(record.get('lng'), record.get('lat'))
            value = raw if state != 'missing' else None
        else:
            state = 'observed' if raw not in (None, '', [], {}) else 'missing'
            value = raw if state == 'observed' else None
        out[field] = {'value': value, 'raw': raw, 'state': state,
                      'location': locations.get(record_key)}
    return out


def normalize_name(name):
    """Case/punctuation-insensitive name key used only for a missing-id fallback."""
    if not name:
        return ''
    return re.sub(r'[\s\-—·（）()【】\[\]]+', '', str(name)).lower()


def identity_key(record):
    """Stable community id from the detail URL; fallback is an explicit missing-id marker."""
    record = record or {}
    cid = _community_id_from_url(record.get('detail_url') or '')
    if not cid:
        cid = _community_id_from_url(record.get('requested_url') or '')
    if cid:
        return str(cid)
    return 'missing-id:' + normalize_name(record.get('name'))


def merge_records(prior, incoming, page_class):
    """Contract: a parse failure/block NEVER overwrites or clears a valid prior record.

    structure_change/verify_challenge (or no incoming) keep prior untouched; empty_directory
    keeps prior evidence too (a genuine empty directory is not a parse failure); normal merges
    by identity_key with incoming replacing same-key prior and prior-only keys retained.
    """
    prior = list(prior or [])
    incoming = list(incoming or [])
    if page_class in ('structure_change', 'verify_challenge') or not incoming:
        return prior
    if page_class == 'empty_directory':
        return prior
    merged = {}
    order = []
    for record in prior:
        key = identity_key(record)
        if key not in merged:
            order.append(key)
        merged[key] = record
    for record in incoming:
        key = identity_key(record)
        if key not in merged:
            order.append(key)
        merged[key] = record
    return [merged[key] for key in order]


def _gateway_excerpt(snapshot_path):
    """The gateway URL/text excerpt from a saved body, for verbatim block evidence."""
    if not snapshot_path:
        return None
    try:
        raw = Path(snapshot_path).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None
    match = re.search(r'https?://[^\s"\'<>]*antibot[^\s"\'<>]*', raw)
    if match:
        return match.group(0)
    match = re.search(r'@@xxzlGatewayUrl[^>]*>([^<]+)<', raw, re.I)
    return match.group(1).strip() if match else None


def _page_text(html):
    try:
        return BeautifulSoup(_to_text(html or b''), 'html.parser').get_text(' ', strip=True)
    except Exception:
        return _to_text(html or b'')


def _challenge_markers(html, title, records, url, status):
    """Named markers proving a challenge; empty list means no challenge evidence."""
    hits = []
    if status in (403, 429):
        hits.append(f'status:{status}')
    raw = _to_text(html or b'')
    text = _page_text(raw)
    body = html if isinstance(html, (bytes, bytearray)) else raw.encode('utf-8', 'ignore')
    hits += list(challenge_evidence(title, text, bool(records), final_url=url, body=body))
    low = text.lower()
    for marker in CHALLENGE_TEXT_MARKERS:
        if marker.lower() in low:
            hits.append(f'body_term:{marker}')
    # The 58/安居客 anti-bot gateway shell: a hidden gateway URL plus a JS redirect.
    gateway = any(marker in low for marker in GATEWAY_MARKERS)
    if gateway:
        hits.append('body:gateway_shell')
    for marker in GATEWAY_MARKERS:
        if marker in low:
            hits.append(f'body:{marker}')
    if gateway and 'window.location.assign' in low:
        hits.append('body:window.location.assign')
    return hits


def _is_verify_challenge(html, title, records, url, status):
    return bool(_challenge_markers(html, title, records, url, status))


def classify_page(html, *, url, status, title, container_found, empty_marker, records):
    """One verdict for a fetched page. 202/empty body is never a success."""
    records = records or []
    if _is_verify_challenge(html, title, records, url, status):
        return 'verify_challenge'
    if NON_COMMUNITY_PATH_RE.search(urlsplit(url).path):
        return 'non_community'
    raw = _to_text(html or b'')
    if status == 202 or not raw.strip():
        return 'structure_change'
    if records:
        return 'normal'
    if container_found and empty_marker:
        return 'empty_directory'
    return 'structure_change'


# ---------------------------------------------------------------------------
# D) network entry
# ---------------------------------------------------------------------------
def _http_get(session, url, timeout):
    started = time.monotonic()
    try:
        response = session.get(url, timeout=(timeout, timeout), allow_redirects=False)
    except requests.RequestException as exc:
        return {'response': None, 'error_type': type(exc).__name__,
                'elapsed_ms': round((time.monotonic() - started) * 1000)}
    return {'response': response, 'error_type': None,
            'elapsed_ms': round((time.monotonic() - started) * 1000)}


def _block_reason(row):
    if row.get('retry_after'):
        return f"retry_after:{row['retry_after']}"
    if row.get('status') in (403, 429):
        return f"http_{row['status']}"
    return 'verify_challenge'


def _read_text(path):
    return Path(path).read_text(encoding='utf-8')


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _target_summary(row, parsed):
    """entry_probe target entry; evidence recomputed from the re-parsed page when available."""
    verification = row.get('page_class') == 'verify_challenge'
    evidence = {'snapshot_path': row.get('snapshot_path')}
    if isinstance(parsed, dict) and 'records' in parsed:
        evidence.update({
            'declared_count': parsed.get('declared_count'),
            'detail_links': len(parsed.get('detail_links') or []),
            'district_links': len(parsed.get('district_links') or []),
            'city_links': len(parsed.get('city_links') or []),
            'next_page_url': parsed.get('next_page_url'),
            'container_found': parsed.get('container_found'),
            'empty_marker': parsed.get('empty_marker'),
        })
    elif isinstance(parsed, dict) and 'record' in parsed:
        record = parsed.get('record') or {}
        evidence.update({
            'stable_id': record.get('stable_id'),
            'name': record.get('name'),
            'year_text': record.get('year_text'),
        })
    return {
        'url_class': row.get('url_class'),
        'requested_url': row.get('requested_url'),
        'final_url': row.get('final_url'),
        'status': row.get('status'),
        'title': row.get('title'),
        'fetched_at': row.get('requested_at'),
        'sha256': row.get('sha256'),
        'verification': verification,
        'page_class': row.get('page_class'),
        'records': row.get('records_parsed') or 0,
        'raw_text_location': row.get('snapshot_path'),
        'evidence': evidence,
    }


def _hash_verification(rows):
    checked = matched = 0
    failed = []
    for row in rows:
        path_str = row.get('snapshot_path')
        digest = row.get('sha256')
        if not path_str or not digest:
            continue
        checked += 1
        path = Path(path_str)
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            matched += 1
        else:
            failed.append({'requested_url': row.get('requested_url'),
                           'snapshot_path': path_str, 'sha256': digest})
    return {'checked': checked, 'matched': matched, 'failed': failed}


def _count_raw_snapshots(out_dir):
    raw_dir = Path(out_dir) / 'raw'
    if not raw_dir.exists():
        return 0
    return sum(1 for _ in raw_dir.glob('*.html'))


def _find_desktop_link(html, base_url):
    try:
        doc = BeautifulSoup(_to_text(html), 'html.parser')
    except Exception:
        return None
    for anchor in doc.find_all('a', href=True):
        absu = urljoin(base_url, anchor['href'])
        host = urlsplit(absu).netloc
        if 'anjuke.com' not in host or host.startswith('m.'):
            continue
        if re.search(r'/(?:xiaoqu|community)/', urlsplit(absu).path):
            return absu
    return None


def run_probe(out_dir, *, city='北京', catalog_url=MOBILE_BJ_CATALOG, budget=40,
              stage='入口探测', resume=False, timeout=20.0, transport=None, clock=None):
    """Budgeted entry probe. Returns a summary dict; prints JSON; never exceeds `budget`."""
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_state_dir.mkdir(parents=True, exist_ok=True)
    transport = transport or _http_get
    clock = clock or _now_epoch
    combo = f'{city}_{SOURCE}'

    store = AnjukeBudgetStore(run_state_dir, clock=clock)
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        print(json.dumps({'mode': 'probe', 'error': 'budget_lock', 'detail': str(exc)},
                         ensure_ascii=False))
        return {'mode': 'probe', 'out': str(out_dir), 'error': 'budget_lock',
                'exit_code': 2}

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA

    checkpoint_path = run_state_dir / 'checkpoint.json'
    completed = set()
    if resume and checkpoint_path.exists():
        try:
            completed = set(json.loads(checkpoint_path.read_text(encoding='utf-8'))
                            .get('completed_url_classes') or [])
        except ValueError:
            completed = set()

    state = {'stop': False, 'blocked': False, 'budget_exhausted': False,
             'internal_error': None, 'any_failure': False, 'requests_made': 0}
    by_class = {}
    body_by_class = {}
    parsed_by_class = {}
    blocks = []
    notes = []

    def emit(row):
        append_jsonl(out_dir / 'requests.jsonl', [row])

    def base_row(url, request_id, url_class):
        return {
            'source': SOURCE, 'city': city, 'combo': combo, 'requested_url': url,
            'final_url': url, 'requested_at': now_iso(), 'status': None,
            'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0, 'body_state': None,
            'request_id': request_id, 'stage': stage, 'platform': PLATFORM,
            'network_attempt': True, 'url_class': url_class, 'page_class': None,
            'title': None, 'records_parsed': 0, 'unique_request_id': uuid.uuid4().hex,
        }

    def reserve_attempt(url):
        while True:
            result = store.reserve(PLATFORM, stage, combo, url, now=clock())
            if result['allowed']:
                return result['request_id']
            if result['reason'] == 'interval_not_elapsed':
                _sleep(result['wait_seconds'])
                continue
            store.record_exhausted(platform=PLATFORM, stage=stage, combo=combo,
                                   requested_url=url, reason=result['reason'], now=clock())
            refusal = base_row(url, None, None)
            refusal['failure_class'] = 'budget_exhausted'
            refusal['network_attempt'] = False
            refusal['blocking_reason'] = result['reason']
            emit(refusal)
            state['budget_exhausted'] = True
            state['any_failure'] = True
            state['stop'] = True
            return None

    def handle_success(row, body, url, url_class):
        doc = BeautifulSoup(_to_text(body), 'html.parser')
        title = doc.title.get_text(' ', strip=True) if doc.title else ''
        row['title'] = title
        if url_class == 'detail':
            parsed = parse_detail(body, url=url, fetched_at=row['requested_at'],
                                  snapshot_path=row['snapshot_path'])
            records = [parsed['record']] if parsed.get('record') else []
            container = parsed.get('container_found', False)
            empty = False
        else:
            parsed = parse_catalog(body, url=url, fetched_at=row['requested_at'],
                                   snapshot_path=row['snapshot_path'])
            records = parsed['records']
            container = parsed['container_found']
            empty = parsed['empty_marker']
        row['records_parsed'] = len(records)
        row['cards_parsed'] = len(records)
        row['page_class'] = classify_page(body, url=url, status=row['status'], title=title,
                                          container_found=container, empty_marker=empty,
                                          records=records)
        parsed_by_class[url_class] = parsed
        body_by_class[url_class] = body

    def process_attempt(url, chain, url_class):
        request_id = reserve_attempt(url)
        if request_id is None:
            return None
        state['requests_made'] += 1
        row = base_row(url, request_id, url_class)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, timeout)
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
        row['body_state'] = 'retained' if body else 'no_body'
        if body:
            row['snapshot_path'] = str(save_raw(raw_dir, combo, url, body, row['sha256']))
        if status in (403, 429):
            row['failure_class'] = 'blocked_403' if status == 403 else 'blocked_429'
        elif status == 202:
            row['failure_class'] = 'http_202'
        elif 300 <= status < 400 and response.headers.get('Location'):
            location = response.headers.get('Location')
            next_url = urljoin(url, location)
            row['final_url'] = next_url
            row['redirect_chain'] = list(chain) + [{'url': url, 'status': status,
                                                    'location': location, 'next': next_url}]
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        else:
            handle_success(row, body, url, url_class)
        emit(row)
        return row

    def execute_target(target):
        url_class = target['url_class']
        if url_class in completed:
            notes.append(f'skipped_completed:{url_class}')
            return
        current = target['url']
        chain = []
        final_row = None
        while True:
            if state['requests_made'] >= budget:
                notes.append(f'probe_budget_cap:{url_class}')
                return
            row = process_attempt(current, chain, url_class)
            if row is None or state['stop']:
                return
            if row['status'] is None:
                state['internal_error'] = row['error_type']
                state['any_failure'] = True
                state['stop'] = True
                return
            redirect = (300 <= row['status'] < 400 and row['redirect_chain']
                        and row['redirect_chain'][-1]['url'] == current)
            if redirect:
                if len(row['redirect_chain']) >= MAX_REDIRECT_HOPS:
                    state['internal_error'] = 'TooManyRedirects'
                    state['any_failure'] = True
                    state['stop'] = True
                    return
                chain = row['redirect_chain']
                current = chain[-1]['next']
                continue
            final_row = row
            break
        if final_row is None:
            return
        by_class[url_class] = final_row
        if final_row.get('page_class') == 'verify_challenge' \
                or final_row.get('status') in (403, 429):
            reason = _block_reason(final_row)
            store.pause_platform(PLATFORM, reason, now=clock())
            blocks.append({'url_class': url_class,
                           'requested_url': final_row['requested_url'],
                           'status': final_row.get('status'),
                           'retry_after': final_row.get('retry_after'),
                           'reason': reason,
                           'evidence_verbatim': (final_row.get('title') or '')[:200]})
            state['blocked'] = True
            state['stop'] = True
            return
        completed.add(url_class)
        checkpoint_path.write_text(json.dumps(
            {'completed_url_classes': sorted(completed), 'updated_at': now_iso()},
            ensure_ascii=False, indent=2), encoding='utf-8')

    # run_config written before the first request so the plan is frozen on disk
    reconciliation = store.reconciliation()
    _write_json(out_dir / 'run_config.json', {
        'schema': 'anjuke_run_config/v1', 'source': SOURCE, 'city': city,
        'catalog_url': catalog_url, 'budget': budget, 'stage': stage,
        'run_state_dir': str(run_state_dir), 'global_ledger': GLOBAL_LEDGER,
        'anjuke_round_cap': ANJUKE_ROUND_CAP,
        'effective_remaining': reconciliation['effective_remaining'],
        'command': 'probe', 'version': VERSION, 'run_date': now_iso()[:10],
    })

    try:
        # T1: BJ mobile catalog
        execute_target({'url_class': 'bj_mobile_catalog', 'url': catalog_url})

        t1_row = by_class.get('bj_mobile_catalog')
        t1_parsed = parsed_by_class.get('bj_mobile_catalog')
        t1_body = body_by_class.get('bj_mobile_catalog')

        targets = []
        desktop_url = None
        if t1_parsed and not state['stop']:
            for link in t1_parsed.get('city_links') or []:
                if re.search(r'anjuke\.com/sh(?:/|$)', link.get('url') or ''):
                    targets.append({'url_class': 'sh_mobile_catalog', 'url': link['url']})
                    break
            else:
                targets.append({'url_class': 'sh_mobile_catalog_candidate',
                                'url': MOBILE_SH_CANDIDATE})
            if t1_parsed.get('next_page_url'):
                targets.append({'url_class': 'pagination',
                                'url': t1_parsed['next_page_url']})
            else:
                notes.append('pagination_not_discovered')
            district_links = t1_parsed.get('district_links') or []
            if district_links:
                targets.append({'url_class': 'district_catalog',
                                'url': district_links[0]['url']})
            else:
                notes.append('district_not_discovered')
            detail_links = t1_parsed.get('detail_links') or []
            if detail_links:
                targets.append({'url_class': 'detail', 'url': detail_links[0]})
            else:
                notes.append('detail_not_discovered')
            if t1_body:
                desktop_url = _find_desktop_link(t1_body, catalog_url)

        # T2
        for target in targets:
            if state['stop']:
                break
            execute_target(target)
            if target['url_class'] in ('sh_mobile_catalog', 'sh_mobile_catalog_candidate') \
                    and desktop_url is None:
                body = body_by_class.get(target['url_class'])
                if body:
                    desktop_url = _find_desktop_link(body, target['url'])

        # T3 desktop (discovered only; never guessed)
        if not state['stop'] and desktop_url:
            execute_target({'url_class': 'desktop_catalog', 'url': desktop_url})

        # mirror non-success/challenge bodies so later runs cannot erase evidence
        all_rows = read_jsonl(out_dir / 'requests.jsonl')
        for row in all_rows:
            if row.get('snapshot_path') and (row.get('failure_class')
                                             or row.get('status') != 200):
                copy_to_failed(raw_dir, row['snapshot_path'])

        entries = {name: _target_summary(row, parsed_by_class.get(name))
                   for name, row in by_class.items()}
        targets_out = [entries[name] for name in entries]
        desktop_entry = entries.get('desktop_catalog') or _not_discovered_entry('desktop_catalog')
        detail_entry = entries.get('detail') or _not_run_entry('detail')
        sh_key = 'sh_mobile_catalog' if 'sh_mobile_catalog' in entries \
            else 'sh_mobile_catalog_candidate'

        hash_ver = _hash_verification(all_rows)
        request_rows = [row for row in all_rows if row.get('request_id') is not None]
        requests_used = {
            'total': len(request_rows),
            'by_url_class': dict(_count_by_class(request_rows)),
        }
        entry_probe = {
            'schema': 'anjuke_entry_probe/v1',
            'generated_at': now_iso(),
            'trial_dir': str(out_dir),
            'budget': store.report(now=clock()),
            'budget_reconciliation': store.reconciliation(),
            'targets': targets_out,
            'bj_entry': entries.get('bj_mobile_catalog') or _not_run_entry('bj_mobile_catalog'),
            'sh_entry': entries.get(sh_key) or _not_run_entry(sh_key),
            'desktop': desktop_entry,
            'detail_sample': detail_entry,
            'blocks': blocks,
            'raw_snapshot_count': _count_raw_snapshots(out_dir),
            'hash_verification': hash_ver,
            'requests_used': requests_used,
            'notes': notes + [
                '坐标仅保留原值,坐标系未核实不得直接采用',
                '阻断即暂停平台并停止,不使用代理/账号/cookie轮换',
            ],
        }
        _write_json(out_dir / 'entry_probe.json', entry_probe)
    finally:
        store.release_lock()

    exit_code = 1 if state['internal_error'] else \
        2 if (state['blocked'] or state['budget_exhausted']) else 0
    summary = dict(entry_probe)
    summary.update({'mode': 'probe', 'out': str(out_dir),
                    'blocked': state['blocked'],
                    'budget_exhausted': state['budget_exhausted'],
                    'internal_error': state['internal_error'],
                    'exit_code': exit_code})
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def _count_by_class(rows):
    counts = defaultdict(int)
    for row in rows:
        counts[row.get('url_class') or 'unknown'] += 1
    return counts


def _not_discovered_entry(url_class):
    return {'url_class': url_class, 'requested_url': None, 'final_url': None, 'status': None,
            'title': None, 'fetched_at': None, 'sha256': None, 'verification': False,
            'page_class': None, 'records': 0, 'raw_text_location': None,
            'evidence': {'discovered': False}}


def _not_run_entry(url_class):
    return {'url_class': url_class, 'requested_url': None, 'final_url': None, 'status': None,
            'title': None, 'fetched_at': None, 'sha256': None, 'verification': False,
            'page_class': None, 'records': 0, 'raw_text_location': None,
            'evidence': {'run': False}}


# ---------------------------------------------------------------------------
# E) offline replay
# ---------------------------------------------------------------------------
def run_replay(out_dir, *, transport=None, clock=None):
    """Offline: re-parse every saved snapshot, rebuild entry_probe/field_capability + manifest."""
    out_dir = Path(out_dir)
    clock = clock or _now_epoch
    rows = read_jsonl(out_dir / 'requests.jsonl')
    replayed = []
    verified_rows = []
    replay_ok_total = 0
    merged_by_class = {}
    for row in rows:
        path_str = row.get('snapshot_path')
        digest = row.get('sha256')
        if not path_str:
            continue
        path = Path(path_str)
        file_ok = bool(digest) and path.exists() \
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        replay_ok = False
        records = []
        parsed = None
        page_class = row.get('page_class')
        title = row.get('title')
        markers = []
        url_class = row.get('url_class')
        url = row.get('final_url') or row.get('requested_url')
        if file_ok:
            body = path.read_bytes()
            try:
                if url_class == 'detail':
                    parsed = parse_detail(body, url=url, fetched_at=row.get('requested_at'),
                                          snapshot_path=str(path))
                    records = [parsed['record']] if parsed.get('record') else []
                    container = parsed.get('container_found', False)
                    empty = False
                else:
                    parsed = parse_catalog(body, url=url, fetched_at=row.get('requested_at'),
                                           snapshot_path=str(path))
                    records = parsed['records']
                    container = parsed.get('container_found', False)
                    empty = parsed.get('empty_marker', False)
                # Replay is the source of truth: recompute the verdict from the saved body.
                title = parsed.get('title') or title
                markers = _challenge_markers(body, title, records, url, row.get('status'))
                page_class = classify_page(body, url=url, status=row.get('status'),
                                           title=title, container_found=container,
                                           empty_marker=empty, records=records)
                replay_ok = True
                replay_ok_total += 1
            except Exception:
                replay_ok = False
        # Accumulate per url_class through the merge contract: a failed/blocked re-parse
        # never clears records already recovered from an earlier snapshot of the same class.
        # An unverified/tampered snapshot is never merged.
        if file_ok:
            merged_by_class[url_class] = merge_records(
                merged_by_class.get(url_class), records, page_class)
        verified_rows.append({'row': row, 'parsed': parsed, 'records': records,
                              'file_ok': file_ok, 'page_class': page_class,
                              'title': title, 'markers': markers})
        replayed.append({
            'unique_request_id': row.get('unique_request_id'),
            'url_class': row.get('url_class'),
            'requested_url': row.get('requested_url'),
            'snapshot_path': path_str,
            'sha256': digest,
            'file_ok': file_ok,
            'replay_ok': replay_ok,
            'replayed_records': len(records),
            'page_class': page_class,
        })

    entries = {}
    for item in verified_rows:
        row = item['row']
        entry = _target_summary(row, item['parsed'])
        entry['page_class'] = item['page_class']
        entry['title'] = item['title']
        entry['verification'] = item['page_class'] == 'verify_challenge'
        if item['markers']:
            entry['evidence']['challenge_evidence'] = item['markers']
        entries[row.get('url_class')] = entry

    targets_out = [entries[key] for key in entries]
    blocks = []
    for item in verified_rows:
        row = item['row']
        if item['page_class'] != 'verify_challenge' and row.get('status') not in (403, 429):
            continue
        excerpt = _gateway_excerpt(row.get('snapshot_path')) or (item['title'] or '')
        blocks.append({'url_class': row.get('url_class'),
                       'requested_url': row.get('requested_url'),
                       'status': row.get('status'),
                       'retry_after': row.get('retry_after'),
                       'reason': _block_reason(row),
                       'evidence_verbatim': excerpt[:200],
                       'challenge_evidence': item['markers']})

    store = AnjukeBudgetStore(out_dir / 'run_state', clock=clock or _now_epoch)
    # Persist the block so a later run refuses the platform (idempotent).
    for block in blocks:
        store.pause_platform(PLATFORM, block['reason'], now=clock())
    request_rows = [row for row in rows if row.get('request_id') is not None]
    entry_probe = {
        'schema': 'anjuke_entry_probe/v1',
        'generated_at': now_iso(),
        'trial_dir': str(out_dir),
        'budget': store.report(),
        'budget_reconciliation': store.reconciliation(),
        'targets': targets_out,
        'bj_entry': entries.get('bj_mobile_catalog') or _not_run_entry('bj_mobile_catalog'),
        'sh_entry': entries.get('sh_mobile_catalog')
        or entries.get('sh_mobile_catalog_candidate') or _not_run_entry('sh_mobile_catalog'),
        'desktop': entries.get('desktop_catalog') or _not_discovered_entry('desktop_catalog'),
        'detail_sample': entries.get('detail') or _not_run_entry('detail'),
        'blocks': blocks,
        'raw_snapshot_count': _count_raw_snapshots(out_dir),
        'hash_verification': _hash_verification(rows),
        'requests_used': {'total': len(request_rows),
                          'by_url_class': dict(_count_by_class(request_rows))},
        'notes': ['replay: offline only, rebuilt from saved snapshots',
                  '坐标仅保留原值,坐标系未核实不得直接采用'],
        'replay': replayed,
    }
    _write_json(out_dir / 'entry_probe.json', entry_probe)

    all_records = [(item['row'].get('url_class'), record)
                   for item in verified_rows for record in item['records']]
    capability = build_field_capability(all_records)
    _write_json(out_dir / 'field_capability.json', capability)

    artifacts = _artifact_hashes(out_dir, ['run_config.json', 'requests.jsonl',
                                           'entry_probe.json', 'field_capability.json'])
    _update_manifest(out_dir, artifacts, len(request_rows), _count_raw_snapshots(out_dir))

    summary = {'mode': 'replay', 'out': str(out_dir), 'requests': len(rows),
               'snapshots_replayed': replay_ok_total,
               'records_replayed': sum(item['replayed_records'] for item in replayed),
               'merged_records': sum(len(records) for records in merged_by_class.values()),
               'field_capability': capability, 'ok': True}
    print(json.dumps({k: summary[k] for k in ('mode', 'out', 'requests',
                                              'snapshots_replayed', 'records_replayed',
                                              'merged_records')},
                     ensure_ascii=False))
    return summary


def build_field_capability(records_by_class):
    """Aggregate replayed records into per-field evidence for the downstream md author."""
    grouped = defaultdict(list)
    for url_class, record in records_by_class:
        normalized = normalize_fields(record)
        for field, info in normalized.items():
            grouped[field].append((url_class, info, record))

    out = []
    for group_label, fields in FIELD_GROUPS:
        for field in fields:
            observed = [(cls, info, rec) for cls, info, rec in grouped.get(field, [])
                        if info['state'] == 'observed']
            partial = [(cls, info, rec) for cls, info, rec in grouped.get(field, [])
                       if info['state'] == 'partial']
            total = len(grouped.get(field, []))
            if not observed and not partial:
                status = '未观察到'
            elif observed and len(observed) == total:
                status = '实际观察到'
            else:
                status = '仅部分页面有'
            source_pages = sorted({cls for cls, _, _ in (observed + partial) if cls})
            locations = sorted({info['location'] for _, info, _ in (observed + partial)
                                if info['location']})
            samples = []
            for _, info, _ in observed:
                value = info.get('value')
                if value not in (None, '', [], {}) and value not in samples:
                    samples.append(value)
                if len(samples) >= 3:
                    break
            out.append({
                'group': group_label,
                'field': field,
                'status': status,
                'source_page': ','.join(source_pages),
                'raw_text_location': ','.join(locations),
                'sample_values': samples,
                'observed_count': len(observed),
                'partial_count': len(partial),
                'total_count': total,
                'notes': '' if total else '重放记录中无该字段',
                'forbidden_substitution_check': FORBIDDEN_SUBSTITUTION.get(field, ''),
            })
    return {'schema': 'anjuke_field_capability/v1', 'generated_at': now_iso(),
            'fields': out}


def _artifact_hashes(out_dir, names):
    artifacts = []
    for name in names:
        path = out_dir / name
        if not path.exists():
            continue
        data = path.read_bytes()
        artifacts.append({'path': name, 'bytes': len(data),
                          'sha256': hashlib.sha256(data).hexdigest()})
    return artifacts


def _update_manifest(out_dir, artifacts, requests_used, raw_snapshot_count):
    """Additive manifest update; preserves every existing key; tmp+os.replace."""
    manifest_path = out_dir / 'manifest.json'
    existing = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding='utf-8'))
        except ValueError:
            existing = {}
    existing['anjuke_probe'] = {
        'generated_at': now_iso(),
        'artifacts': artifacts,
        'requests_used': requests_used,
        'raw_snapshot_count': raw_snapshot_count,
    }
    _write_json(manifest_path, existing)


# ---------------------------------------------------------------------------
# F) single arbitrary fetch (no plan logic; used to document a paused refusal)
# ---------------------------------------------------------------------------
def run_fetch(out_dir, *, url, url_class, city='北京', stage='入口探测', timeout=20.0,
              transport=None, clock=None):
    """One budgeted attempt for an arbitrary URL/label; appends one row, saves raw."""
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_state_dir.mkdir(parents=True, exist_ok=True)
    transport = transport or _http_get
    clock = clock or _now_epoch
    combo = f'{city}_{SOURCE}'

    store = AnjukeBudgetStore(run_state_dir, clock=clock)
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        print(json.dumps({'mode': 'fetch', 'error': 'budget_lock', 'detail': str(exc)},
                         ensure_ascii=False))
        return {'mode': 'fetch', 'out': str(out_dir), 'error': 'budget_lock', 'exit_code': 2}

    session = requests.Session()
    session.trust_env = False
    session.headers['User-Agent'] = UA

    def base_row(request_id):
        return {
            'source': SOURCE, 'city': city, 'combo': combo, 'requested_url': url,
            'final_url': url, 'requested_at': now_iso(), 'status': None,
            'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0, 'body_state': None,
            'request_id': request_id, 'stage': stage, 'platform': PLATFORM,
            'network_attempt': True, 'url_class': url_class, 'page_class': None,
            'title': None, 'records_parsed': 0, 'unique_request_id': uuid.uuid4().hex,
        }

    try:
        result = store.reserve(PLATFORM, stage, combo, url, now=clock())
        if not result['allowed']:
            store.record_exhausted(platform=PLATFORM, stage=stage, combo=combo,
                                   requested_url=url, reason=result['reason'], now=clock())
            row = base_row(None)
            row['failure_class'] = 'budget_exhausted'
            row['network_attempt'] = False
            row['blocking_reason'] = result['reason']
            append_jsonl(out_dir / 'requests.jsonl', [row])
            summary = {'mode': 'fetch', 'out': str(out_dir), 'url_class': url_class,
                       'refused': True, 'reason': result['reason'], 'exit_code': 2}
            print(json.dumps(summary, ensure_ascii=False))
            return summary

        row = base_row(result['request_id'])
        outcome = transport(session, url, timeout)
        response = outcome['response']
        row['elapsed_ms'] = outcome['elapsed_ms']
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome['error_type']
            row['body_state'] = 'no_body'
        else:
            body = response.content
            row['status'] = response.status_code
            row['content_type'] = response.headers.get('Content-Type')
            row['retry_after'] = response.headers.get('Retry-After')
            row['bytes'] = len(body)
            row['sha256'] = hashlib.sha256(body).hexdigest()
            row['body_state'] = 'retained' if body else 'no_body'
            if body:
                row['snapshot_path'] = str(save_raw(raw_dir, combo, url, body, row['sha256']))
            if response.status_code in (403, 429):
                row['failure_class'] = 'blocked_403' if response.status_code == 403 \
                    else 'blocked_429'
            elif response.status_code != 200:
                row['failure_class'] = 'transport_error'
                row['error_type'] = f'http_{response.status_code}'
            else:
                doc = BeautifulSoup(_to_text(body), 'html.parser')
                title = doc.title.get_text(' ', strip=True) if doc.title else ''
                row['title'] = title
                row['page_class'] = classify_page(
                    body, url=url, status=response.status_code, title=title,
                    container_found=False, empty_marker=False, records=[])
                if row['page_class'] == 'verify_challenge':
                    row['failure_class'] = 'challenge'
        append_jsonl(out_dir / 'requests.jsonl', [row])
        if row.get('snapshot_path') and (row.get('failure_class')
                                         or row.get('status') != 200):
            copy_to_failed(raw_dir, row['snapshot_path'])
        summary = {'mode': 'fetch', 'out': str(out_dir), 'url_class': url_class,
                   'status': row['status'], 'page_class': row['page_class'],
                   'failure_class': row['failure_class'], 'snapshot_path': row['snapshot_path'],
                   'exit_code': 0 if row['failure_class'] is None else 2}
        print(json.dumps(summary, ensure_ascii=False))
        return summary
    finally:
        store.release_lock()


# ---------------------------------------------------------------------------
# G) cli
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description='Anjuke mobile community-directory adapter.')
    sub = parser.add_subparsers(dest='command', required=True)

    probe = sub.add_parser('probe', help='budgeted entry probe against the mobile catalog')
    probe.add_argument('--city', default='北京')
    probe.add_argument('--url', default=MOBILE_BJ_CATALOG)
    probe.add_argument('--budget', type=int, default=40)
    probe.add_argument('--out', required=True)
    probe.add_argument('--stage', choices=list(STAGE_CAPS_ANJUKE), default='入口探测')
    probe.add_argument('--resume', action='store_true')
    probe.add_argument('--timeout', type=float, default=20.0)
    probe.add_argument('--interval', type=float, default=MIN_INTERVAL)

    replay = sub.add_parser('replay', help='offline replay from saved snapshots')
    replay.add_argument('--out', required=True)

    fetch = sub.add_parser('fetch', help='one budgeted attempt for an arbitrary URL/label')
    fetch.add_argument('--url', required=True)
    fetch.add_argument('--url-class', required=True)
    fetch.add_argument('--city', default='北京')
    fetch.add_argument('--out', required=True)
    fetch.add_argument('--stage', choices=list(STAGE_CAPS_ANJUKE), default='入口探测')
    fetch.add_argument('--timeout', type=float, default=20.0)

    budget = sub.add_parser('budget', help='print persisted budget report and reconciliation')
    budget.add_argument('--out', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == 'probe':
        summary = run_probe(args.out, city=args.city, catalog_url=args.url,
                            budget=args.budget, stage=args.stage, resume=args.resume,
                            timeout=args.timeout)
        return summary.get('exit_code', 0)
    if args.command == 'replay':
        result = run_replay(args.out)
        return 0 if result.get('ok') else 1
    if args.command == 'fetch':
        summary = run_fetch(args.out, url=args.url, url_class=args.url_class,
                            city=args.city, stage=args.stage, timeout=args.timeout)
        return summary.get('exit_code', 0)
    store = AnjukeBudgetStore(Path(args.out) / 'run_state')
    print(json.dumps({'report': store.report(), 'reconciliation': store.reconciliation()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
