# -*- coding: utf-8 -*-
"""External (贝壳/链家) candidate probe: budgeted network entry + offline freeze + omission check.

Isolated from the 安居客 trial: reuses ``source_pilot`` budget/persistence/raw-io helpers and the
verified deliverable helpers by import only (those modules are never modified). Every parser is
pure and accepts saved HTML, so ``freeze_candidates`` / ``omission_check`` never need the network.
No network call happens at import time.

Safety rules mirror the pilot: single concurrency, ``trust_env`` disabled, constant plain UA,
manual redirects that each count against the budget, stop+pause on block/challenge, never rotate
proxy/account/cookie, never touch production data or the prior pilot/trial dirs.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from source_pilot import (
    BudgetStore, BudgetLockError, save_raw, copy_to_failed, challenge_evidence,
    now_iso, append_jsonl, read_jsonl, write_jsonl, _to_text, _now_epoch,
)

# ---------------------------------------------------------------------------
# A) constants
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output' / 'anjuke_community_trial_20260923'
RUN_STATE = OUT / 'run_state'
RAW_DIR = OUT / 'raw'
GLOBAL_LEDGER = 'output/source_trial_20260922/run_state/budget.json'
SOURCE = '贝壳链家'
STAGE = '外部候选探测'
STAGE_CAP = 40
PLATFORM = '贝壳链家'
PLATFORM_DAILY_CAP = 300
COMBINED_DAILY_CAP = 600
MIN_INTERVAL = 5.0
SEED_BASE_TRIAL_USED = 597
VERSION = 'external_candidate/v1'
# A single plain UA covering the desktop rendering path (same string as anjuke_community.UA).
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')
MAX_REDIRECT_HOPS = 5
KE_BJ_SEED = 'https://bj.ke.com/xiaoqu/'
KE_SH_SEED = 'https://sh.ke.com/xiaoqu/'
VILLA_FORMS = ('别墅', '独栋', '联排', '叠拼', '双拼')
# Default stage caps for this isolated round; 外部候选探测 is 40 unless overridden per call.
STAGE_CAPS_EXTERNAL = {'入口探测': 40, '分区目录': 600, '详情与候选核对': 120, '续跑复查': 40,
                       '外部候选探测': 40}
SELECTION_METHOD = ('冻结外部候选名单：每城目标>=50，各行政区尽量>=2，'
                    '优先别墅/混合标签；仅取未参与建库的外部平台目录页。')
METHOD_TEXT = ('对冻结外部候选名单逐条在实验目录按 (city, 归一化名称) 匹配；'
               'found 需 年份+形态+可用位置+地铁可判 全为真，否则 undecidable；'
               '未命中为 not_found。')
LIMITATIONS = [
    '样本为目录外的便利性样本，不是全市召回率。',
    '未覆盖行政区不等于零遗漏。',
    '仅使用列表页字段，坐标/年份常缺失。',
    'post_inclusion_fixed 定义：not_found 且外部记录自带可用位置；无外部坐标时为 0。',
    'post_inclusion_discovery_only 定义：not_found 计数。',
]

# ---------------------------------------------------------------------------
# parsing constants
# ---------------------------------------------------------------------------
KE_DETAIL_RE = re.compile(r'/xiaoqu/(\d{6,})/?$')
KE_DISTRICT_RE = re.compile(r'/xiaoqu/([a-z][a-z0-9]*)/?$')
KE_PAGE_RE = re.compile(r'/xiaoqu/pg\d+/?$')
KE_YEAR_RE = re.compile(r'(\d{4})\s*年(?:建|建成)')
KE_COUNT_SET_RE = re.compile(r'共\s*(\d[\d,]*)\s*套')
KE_COUNT_XQ_RE = re.compile(r'共\s*(\d[\d,]*)\s*个小区')
KE_ADDRESS_RE = re.compile(r'(?:小区)?地址[：:]\s*([^\s；;，,<>]+)')
KE_COORD_JSON_RE = re.compile(
    r'"?lng"?\s*[:=]\s*(-?\d+(?:\.\d+)?)[^0-9-]+"?lat"?\s*[:=]\s*(-?\d+(?:\.\d+)?)')
KE_COORD_PAIR_RE = re.compile(r'(-?1[12]\d\.\d+)\s*[,，]\s*(-?[23]\d\.\d+)')
KE_TYPE_TAGS = ('别墅', '独栋', '联排', '叠拼', '双拼', '塔楼', '板楼', '平房', '四合院')
# Body markers that mean a real challenge/login-wall (matched lowercased).
KE_CHALLENGE_MARKERS = ('安全验证', '访问已被拦截', '滑块验证', '人机验证', '操作过于频繁',
                        '请先登录', 'captcha', 'antibot')
# A redirect to clogin.ke.com is a login wall: no challenge shell, just a login page.
KE_LOGIN_HOST_PREFIX = 'clogin.'
# Strong body signals only: 用户登录 is a nav link on many normal pages, never a marker.
KE_LOGIN_MARKERS = ('证书登录', '请先登录', '登录后查看')
KE_PASSWORD_INPUT_RE = re.compile(r'type\s*=\s*["\']?\s*password', re.I)
KE_TXTPASSWORD_RE = re.compile(r'id\s*=\s*["\']?\s*txtPassword', re.I)
KE_FORM_RE = re.compile(r'<form\b', re.I)
KE_EXPLICIT_AREA_RE = re.compile(r'([\u4e00-\u9fff]{2,8}(?:区|县))')
KE_AREA_TOKEN_RE = re.compile(r'[\u4e00-\u9fff]{2,8}')

# ---------------------------------------------------------------------------
# government 老旧小区 project-notice (.docx) constants
# ---------------------------------------------------------------------------
GOV_PLATFORM = 'gov4'
GOV_SOURCE = '北京市住房和城乡建设委员会'
BJ_DISTRICT_RE = re.compile(r'^[\u4e00-\u9fff]{2,4}区$')
_W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W_TBL = f'{{{_W_NS}}}tbl'
W_TR = f'{{{_W_NS}}}tr'
W_TC = f'{{{_W_NS}}}tc'
W_T = f'{{{_W_NS}}}t'
W_VMERGE = f'{{{_W_NS}}}vMerge'
BJ_PAREN_RE = re.compile(r'[（(][^）)]*[）)]')
BJ_ZHJIA_LOU_RE = re.compile(r'([甲丙])(\d+)号楼')
BJ_BUILDING_LIST_RE = re.compile(r'\d+(?:-\d+)?(?:、\d+(?:-\d+)?)*号楼')
BJ_STANDALONE_LOU_RE = re.compile(r'号楼')
BJ_TRAILING_ZHJIA_RE = re.compile(r'(?<=[^、])([甲丙])\d+号?$')
BJ_TRAIL_TRIM_RE = re.compile(r'[、，,。;；·\s]+$')


def _sleep(seconds):
    """Sleep hook; injectable so tests never touch real time."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# B) budget store seeded from the shared global ledger
# ---------------------------------------------------------------------------
class ExternalBudgetStore(BudgetStore):
    """Cross-process budget for 贝壳链家 that never resets a fresh local budget.json.

    First use seeds a fresh state from the shared global ledger (trial_used/next_request_id
    carried forward), so request ids stay globally unique and cumulative. Counters persist.
    """

    def __init__(self, run_state_dir, *, clock=None, stage_caps=None):
        super().__init__(
            run_state_dir,
            stage_caps=stage_caps or STAGE_CAPS_EXTERNAL,
            trial_total=4000,
            platform_daily_caps={'安居客': 300, '贝壳链家': 300},
            combined_daily_cap=600,
            min_interval_seconds=5.0,
            clock=clock or _now_epoch,
        )

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


# ---------------------------------------------------------------------------
# C) pure parsers (accept bytes|str html; never raise out of the public API)
# ---------------------------------------------------------------------------
def _page_text(html):
    try:
        return BeautifulSoup(_to_text(html or b''), 'html.parser').get_text(' ', strip=True)
    except Exception:
        return _to_text(html or b'')


def _ke_year(text):
    match = KE_YEAR_RE.search(text or '')
    return match.group(0).strip() if match else None


def _ke_declared_count(text):
    match = KE_COUNT_SET_RE.search(text or '') or KE_COUNT_XQ_RE.search(text or '')
    return int(match.group(1).replace(',', '')) if match else None


def _ke_leading_areas(text):
    """Leading Chinese area words from card text; explicit 区/县 labels win."""
    if not text:
        return []
    explicit = KE_EXPLICIT_AREA_RE.findall(text)
    if explicit:
        return explicit[:2]
    return [token for token in KE_AREA_TOKEN_RE.findall(text) if '年' not in token][:2]


def _ke_card_labels(container, base_url, detail_id):
    """Text of in-card anchors pointing at a pinyin district slug (never the detail id)."""
    labels = []
    if container is None:
        return labels
    for anchor in container.find_all('a', href=True):
        absu = urljoin(base_url, anchor['href'])
        match = KE_DISTRICT_RE.search(urlsplit(absu).path)
        if not match:
            continue
        slug = match.group(1)
        if slug == detail_id or re.fullmatch(r'pg\d+', slug):
            continue
        text = anchor.get_text(' ', strip=True)
        if text:
            labels.append(text)
    return labels


def _ke_records(doc, url):
    records = []
    seen = set()
    for anchor in doc.find_all('a', href=True):
        absu = urljoin(url, anchor['href'])
        match = KE_DETAIL_RE.search(urlsplit(absu).path)
        if not match or absu in seen:
            continue
        seen.add(absu)
        container = anchor.find_parent(['li', 'div']) or anchor.parent
        text = container.get_text(' ', strip=True) if container \
            else anchor.get_text(' ', strip=True)
        labels = _ke_card_labels(container, url, match.group(1)) or _ke_leading_areas(text)
        records.append({
            'name': anchor.get_text(' ', strip=True),
            'detail_url': absu,
            'stable_id': match.group(1),
            'district_text': labels[0] if labels else None,
            'subarea_text': labels[1] if len(labels) > 1 else None,
            'year_text': _ke_year(text),
            'type_tags': [tag for tag in KE_TYPE_TAGS if tag in text],
            'address_text': None,
        })
    return records


def _ke_district_links(doc, base_url):
    links = []
    seen = set()
    for anchor in doc.find_all('a', href=True):
        absu = urljoin(base_url, anchor['href'])
        match = KE_DISTRICT_RE.search(urlsplit(absu).path)
        if not match or re.fullmatch(r'pg\d+', match.group(1)) or absu in seen:
            continue
        seen.add(absu)
        links.append({'name': anchor.get_text(' ', strip=True), 'url': absu})
    return links


def _ke_next_page(doc, base_url):
    for anchor in doc.find_all('a', href=True):
        absu = urljoin(base_url, anchor['href'])
        if '下一页' in anchor.get_text(' ', strip=True) \
                or KE_PAGE_RE.search(urlsplit(absu).path):
            return absu
    return None


def parse_ke_catalog(html, *, url):
    """贝壳 city/district list page -> records + nav links. Pure; exact keys; never raises."""
    result = {'records': [], 'district_links': [], 'next_page_url': None,
              'title': '', 'declared_count': None, 'page_text_len': 0}
    try:
        raw = _to_text(html)
        doc = BeautifulSoup(raw, 'html.parser')
        result['title'] = doc.title.get_text(' ', strip=True) if doc.title else ''
        text = doc.get_text(' ', strip=True)
        result['page_text_len'] = len(text)
        result['declared_count'] = _ke_declared_count(text)
        result['district_links'] = _ke_district_links(doc, url)
        result['next_page_url'] = _ke_next_page(doc, url)
        result['records'] = _ke_records(doc, url)
    except Exception:
        return result
    return result


def _ke_detail_name(doc, title):
    h1 = doc.find('h1')
    if h1 and h1.get_text(' ', strip=True):
        return h1.get_text(' ', strip=True)
    meta = doc.find('meta', attrs={'property': 'og:title'}) or \
        doc.find('meta', attrs={'name': 'title'})
    if meta and meta.get('content'):
        return meta['content'].strip()
    if title:
        return re.split(r'[-_|]', title)[0].strip() or None
    return None


def parse_ke_detail(html, *, url):
    """贝壳 detail page -> one record + title. Pure; exact keys; never raises.

    lng/lat are the RAW regex values only; the coordinate system is never invented.
    """
    result = {'record': None, 'title': ''}
    try:
        raw = _to_text(html)
        doc = BeautifulSoup(raw, 'html.parser')
        title = doc.title.get_text(' ', strip=True) if doc.title else ''
        result['title'] = title
        text = doc.get_text(' ', strip=True)
        match = KE_DETAIL_RE.search(urlsplit(url or '').path)
        lng = lat = None
        coord = KE_COORD_JSON_RE.search(raw) or KE_COORD_PAIR_RE.search(raw)
        if coord:
            lng, lat = coord.group(1), coord.group(2)
        labels = _ke_leading_areas(text)
        address = KE_ADDRESS_RE.search(text)
        result['record'] = {
            'name': _ke_detail_name(doc, title),
            'detail_url': url,
            'stable_id': match.group(1) if match else None,
            'address_text': address.group(1).strip() if address else None,
            'district_text': labels[0] if labels else None,
            'year_text': _ke_year(text),
            'type_tags': [tag for tag in KE_TYPE_TAGS if tag in text],
            'lng_raw': lng,
            'lat_raw': lat,
        }
    except Exception:
        return result
    return result


def _login_markers(final_url, requested_url, title, text, redirect_chain=None, raw=None):
    """Named evidence proving a login wall; empty list means no strong login signal.

    Strong signals only: a login host/path on the final, requested or any redirect hop; a password
    input or 证书登录/请先登录/登录后查看 in the body; or a bare 登录 title corroborated by
    ``service=`` or a form carrying a password field. A bare 用户登录 nav link is never a signal.
    """
    hits = []
    html = raw or ''
    urls = [final_url, requested_url]
    for hop in (redirect_chain or []):
        if isinstance(hop, dict):
            urls += [hop.get('url'), hop.get('location'), hop.get('next')]
    for value in urls:
        if not value:
            continue
        parts = urlsplit(value)
        if (parts.netloc or '').lower().startswith(KE_LOGIN_HOST_PREFIX):
            hits.append('url:clogin_host')
        if '/login' in (parts.path or '').lower():
            hits.append('url:login_path')
    has_password_field = bool(KE_PASSWORD_INPUT_RE.search(html)
                              or KE_TXTPASSWORD_RE.search(html))
    if has_password_field:
        hits.append('body:password_input')
    for marker in KE_LOGIN_MARKERS:
        if marker in (text or ''):
            hits.append(f'body_term:{marker}')
    if (title or '').strip() == '登录':
        has_service = 'service=' in (final_url or '').lower() \
            or 'service=' in (requested_url or '').lower()
        has_password_form = has_password_field and bool(KE_FORM_RE.search(html))
        if has_service or has_password_form:
            hits.append('title:登录')
    return hits


def _ke_challenge_markers(title, text, records, url, body, status):
    """Named markers proving a challenge; empty list means no challenge evidence."""
    hits = []
    if status in (403, 429):
        hits.append(f'status:{status}')
    hits += list(challenge_evidence(title, text, bool(records), final_url=url, body=body))
    low = (text or '').lower()
    for marker in KE_CHALLENGE_MARKERS:
        if marker.lower() in low:
            hits.append(f'body_term:{marker}')
    return hits


def _page_block_markers(html, *, url, status, title, records=None, final_url=None,
                        redirect_chain=None):
    """Login-wall + challenge markers for a fetched page; empty list means not blocked."""
    raw = _to_text(html or b'')
    text = _page_text(raw)
    body = html if isinstance(html, (bytes, bytearray)) else raw.encode('utf-8', 'ignore')
    hits = _login_markers(final_url or url, url, title, text, redirect_chain, raw=raw)
    hits += _ke_challenge_markers(title, text, records or [], url, body, status)
    return hits


def classify_ke_page(html, *, url, status, title, records, declared_count,
                     final_url=None, redirect_chain=None):
    """One verdict for a fetched page. 202/empty body is never a success."""
    records = records or []
    raw = _to_text(html or b'')
    text = _page_text(raw)
    if _page_block_markers(html, url=url, status=status, title=title, records=records,
                           final_url=final_url, redirect_chain=redirect_chain):
        return 'verify_challenge'
    if status == 202 or not raw.strip():
        return 'structure_change'
    if records:
        return 'normal'
    if declared_count == 0 or ('暂无' in text and not records):
        return 'empty_directory'
    return 'structure_change'


def classify_fallback_page(html, *, url, status, title, final_url=None, redirect_chain=None):
    """Light classifier for the generic fallback probe: no parser, no records."""
    raw = _to_text(html or b'')
    if _page_block_markers(html, url=url, status=status, title=title, final_url=final_url,
                           redirect_chain=redirect_chain):
        return 'verify_challenge'
    if status == 202 or not raw.strip():
        return 'structure_change'
    if status == 200:
        return 'normal'
    return 'structure_change'


def _bj_community_name(project_name):
    """Normalize a project name to a community name by stripping building-number suffixes."""
    raw = (project_name or '').strip()
    if not raw:
        return raw
    name = BJ_PAREN_RE.sub('', raw)
    name = re.sub(r'^证载[^，,、]*[，,、]?', '', name)
    name = BJ_ZHJIA_LOU_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}号', name)
    name = BJ_BUILDING_LIST_RE.sub('', name)
    name = BJ_STANDALONE_LOU_RE.sub('', name)
    name = BJ_TRAILING_ZHJIA_RE.sub('', name)
    name = BJ_TRAIL_TRIM_RE.sub('', name).strip()
    return name or raw


def parse_bj_oldcommunity_docx(path):
    """Parse a 老旧小区 notice .docx table into {district, project_name, community_name}.

    The 区 column is located from the header (text ``区``) or, failing that, the first cell that
    looks like a 区 label. A vertically merged 区 cell carries its text only on the vMerge restart
    row; continuation rows (``<w:vMerge/>``) inherit the previous 区. 总计/合计/header rows and
    rows with an empty name cell are skipped; duplicates collapse by (district, community_name).
    """
    rows = []
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read('word/document.xml')
    except (OSError, zipfile.BadZipFile, KeyError):
        return rows
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return rows

    table_rows = []
    for table in root.iter(W_TBL):
        for tr in table.findall(W_TR):
            cells = []
            for tc in tr.findall(W_TC):
                text = ''.join(node.text or '' for node in tc.iter(W_T)).strip()
                has_vmerge = any(True for _ in tc.iter(W_VMERGE))
                cells.append({'text': text, 'vmerge': has_vmerge})
            table_rows.append(cells)

    district_idx = None
    name_idx = None
    for cells in table_rows:
        for index, cell in enumerate(cells):
            if district_idx is None and cell['text'] == '区':
                district_idx = index
            if name_idx is None and cell['text'] == '项目名称':
                name_idx = index
        if district_idx is not None and name_idx is not None:
            break
    if district_idx is None:
        for cells in table_rows:
            match = next((i for i, cell in enumerate(cells)
                          if BJ_DISTRICT_RE.match(cell['text'])), None)
            if match is not None:
                district_idx = match
                break
    if district_idx is None:
        district_idx = 1
    if name_idx is None:
        name_idx = 2

    seen = set()
    current_district = None
    for cells in table_rows:
        district_cell = cells[district_idx] if district_idx < len(cells) \
            else {'text': '', 'vmerge': False}
        # A non-empty 区 label sets the district (header ``区`` is not a label); an empty vMerge
        # continuation cell keeps the previous district; anything else leaves it unchanged.
        if district_cell['text']:
            if BJ_DISTRICT_RE.match(district_cell['text']):
                current_district = district_cell['text']
        elif district_cell['vmerge']:
            pass
        name_cell = cells[name_idx]['text'] if name_idx < len(cells) else ''
        if not name_cell:
            continue
        if any(marker in name_cell for marker in ('总计', '合计', '项目名称', '序号')):
            continue
        community = _bj_community_name(name_cell)
        key = (current_district, community)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'district': current_district, 'project_name': name_cell,
                     'community_name': community})
    return rows


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


def _row_login_markers(row):
    """Login evidence recomputed from a recorded row (URLs + title + retained body)."""
    raw = ''
    path_str = row.get('snapshot_path')
    if path_str and Path(path_str).exists():
        try:
            raw = Path(path_str).read_text(encoding='utf-8', errors='ignore')
        except OSError:
            raw = ''
    return _login_markers(row.get('final_url'), row.get('requested_url'),
                          row.get('title'), _page_text(raw), row.get('redirect_chain'), raw=raw)


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _store_stage_caps(stage_cap=None):
    """Per-call stage caps: None keeps the default 40 外部候选探测 cap."""
    if stage_cap is None:
        return None
    caps = dict(STAGE_CAPS_EXTERNAL)
    caps['外部候选探测'] = stage_cap
    return caps


def run_probe(out_dir=OUT, *, budget=40, stage=STAGE, timeout=20.0, transport=None, clock=None,
              max_district_pages_per_city=6, max_extra_pages_per_city=2, headers=None,
              stage_cap=None):
    """Budgeted, discovery-driven entry probe of the 贝壳/链家 directory. Returns a summary dict."""
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_state_dir.mkdir(parents=True, exist_ok=True)
    transport = transport or _http_get
    clock = clock or _now_epoch

    store = ExternalBudgetStore(run_state_dir, clock=clock,
                                stage_caps=_store_stage_caps(stage_cap))
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        return {'error': 'budget_lock', 'detail': str(exc)}

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA
    if headers:
        session.headers.update(headers)

    # run_config written before the first request so the plan is frozen on disk
    _write_json(out_dir / 'run_config_external.json', {
        'schema': 'external_run_config/v1', 'source': SOURCE, 'platform': PLATFORM,
        'stage': stage, 'budget': budget, 'command': 'probe', 'version': VERSION,
        'run_date': now_iso()[:10], 'global_ledger': GLOBAL_LEDGER, 'stage_cap': STAGE_CAP,
    })

    state = {'stop': False, 'blocked': False, 'requests_made': 0}
    blocks = []
    notes = []
    targets = []
    parsed_by_url = {}
    scheduled = set()
    district_enqueued = defaultdict(int)
    extra_enqueued = defaultdict(int)
    queue = deque()

    def base_row(city, url, request_id, url_class):
        return {
            'source': SOURCE, 'city': city, 'combo': f'{city}_{PLATFORM}',
            'requested_url': url, 'final_url': url, 'requested_at': now_iso(),
            'status': None, 'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0, 'body_state': None,
            'request_id': request_id, 'stage': stage, 'platform': PLATFORM,
            'network_attempt': True, 'url_class': url_class, 'page_class': None,
            'title': None, 'records_parsed': 0, 'unique_request_id': uuid.uuid4().hex,
        }

    def enqueue(city, url, url_class):
        if not url or url in scheduled:
            return
        scheduled.add(url)
        queue.append((city, url, url_class))

    def reserve_attempt(city, url, url_class):
        combo = f'{city}_{PLATFORM}'
        while True:
            result = store.reserve(PLATFORM, stage, combo, url, now=clock())
            if result['allowed']:
                return result['request_id']
            if result['reason'] == 'interval_not_elapsed':
                _sleep(result['wait_seconds'])
                continue
            store.record_exhausted(platform=PLATFORM, stage=stage, combo=combo,
                                   requested_url=url, reason=result['reason'], now=clock())
            refusal = base_row(city, url, None, url_class)
            refusal['failure_class'] = 'budget_exhausted'
            refusal['network_attempt'] = False
            refusal['blocking_reason'] = result['reason']
            append_jsonl(out_dir / 'requests.jsonl', [refusal])
            notes.append(f"budget_exhausted:{result['reason']}")
            state['stop'] = True
            return None

    def process_attempt(city, url, url_class, chain):
        request_id = reserve_attempt(city, url, url_class)
        if request_id is None:
            return None
        state['requests_made'] += 1
        row = base_row(city, url, request_id, url_class)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, timeout)
        response = outcome['response']
        row['elapsed_ms'] = outcome['elapsed_ms']
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome['error_type']
            row['body_state'] = 'no_body'
            append_jsonl(out_dir / 'requests.jsonl', [row])
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
            row['snapshot_path'] = str(save_raw(raw_dir, row['combo'], url, body, row['sha256']))
        title = ''
        records = []
        declared = None
        if body:
            try:
                doc = BeautifulSoup(_to_text(body), 'html.parser')
                title = doc.title.get_text(' ', strip=True) if doc.title else ''
            except Exception:
                title = ''
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
            row['failure_class'] = 'redirect'
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        elif body:
            if url_class == 'detail':
                parsed = parse_ke_detail(body, url=url)
                records = [parsed['record']] if parsed.get('record') else []
                title = parsed.get('title') or title
            else:
                parsed = parse_ke_catalog(body, url=url)
                records = parsed['records']
                declared = parsed['declared_count']
                title = parsed.get('title') or title
                parsed_by_url[url] = parsed
        row['title'] = title
        row['records_parsed'] = len(records)
        row['cards_parsed'] = len(records)
        row['page_class'] = classify_ke_page(body, url=url, status=status, title=title,
                                             records=records, declared_count=declared,
                                             final_url=row.get('final_url'),
                                             redirect_chain=row.get('redirect_chain'))
        append_jsonl(out_dir / 'requests.jsonl', [row])
        if row.get('snapshot_path') and (row.get('failure_class') or status != 200):
            copy_to_failed(raw_dir, row['snapshot_path'])
        return row

    def execute(city, url, url_class):
        chain = []
        current = url
        while True:
            if state['requests_made'] >= budget:
                notes.append('probe_budget_cap')
                return None
            row = process_attempt(city, current, url_class, chain)
            if row is None or state['stop'] or row.get('status') is None:
                return row
            redirect = (300 <= row['status'] < 400 and row['redirect_chain']
                        and row['redirect_chain'][-1]['url'] == current)
            if not redirect:
                return row
            if len(row['redirect_chain']) >= MAX_REDIRECT_HOPS:
                notes.append('too_many_redirects')
                state['stop'] = True
                return row
            chain = row['redirect_chain']
            current = chain[-1]['next']

    def discover(city, key, url_class, row):
        parsed = parsed_by_url.get(key)
        if parsed is None or row.get('page_class') != 'normal':
            return
        if url_class == 'city_general':
            villa_used = False
            for link in parsed.get('district_links') or []:
                link_url = link.get('url')
                if not link_url:
                    continue
                slug_match = KE_DISTRICT_RE.search(urlsplit(link_url).path)
                slug = slug_match.group(1) if slug_match else ''
                if 'villa' in slug or 'bieshu' in slug:
                    if villa_used:
                        continue
                    villa_used = True
                    enqueue(city, link_url, 'villa')
                elif district_enqueued[city] < max_district_pages_per_city:
                    district_enqueued[city] += 1
                    enqueue(city, link_url, 'district')
        next_url = parsed.get('next_page_url')
        if next_url and extra_enqueued[city] < max_extra_pages_per_city:
            extra_enqueued[city] += 1
            enqueue(city, next_url, 'extra')

    for city, seed in (('北京', KE_BJ_SEED), ('上海', KE_SH_SEED)):
        enqueue(city, seed, 'city_general')

    try:
        while queue and not state['stop']:
            city, url, url_class = queue.popleft()
            row = execute(city, url, url_class)
            if row is None:
                break
            targets.append({
                'url_class': row.get('url_class'),
                'requested_url': row.get('requested_url'),
                'status': row.get('status'),
                'title': row.get('title'),
                'sha256': row.get('sha256'),
                'page_class': row.get('page_class'),
                'records': row.get('records_parsed') or 0,
            })
            if row.get('page_class') == 'verify_challenge' or row.get('status') in (403, 429):
                reason = 'login_wall' if _row_login_markers(row) else _block_reason(row)
                store.pause_platform(PLATFORM, reason, now=clock())
                blocks.append({'url_class': row.get('url_class'),
                               'requested_url': row.get('requested_url'),
                               'status': row.get('status'),
                               'retry_after': row.get('retry_after'),
                               'reason': reason,
                               'evidence_verbatim': row.get('title'),
                               'final_url': row.get('final_url'),
                               'sha256': row.get('sha256')})
                state['blocked'] = True
                state['stop'] = True
                break
            discover(city, row.get('final_url') or url, url_class, row)
    finally:
        rows = read_jsonl(out_dir / 'requests.jsonl')
        requests_used = sum(1 for r in rows if r.get('request_id') is not None)
        summary = {
            'schema': 'external_probe/v1',
            'generated_at': now_iso(),
            'platform': PLATFORM,
            'requests_used': requests_used,
            'budget': budget,
            'targets': targets,
            'blocks': blocks,
            'notes': notes + ['阻断即暂停平台并停止,不使用代理/账号/cookie轮换',
                              '仅公开小区目录列表页,低量探测;坐标/年份常缺失'],
            'budget_report': store.report(now=clock()),
        }
        _write_json(out_dir / 'external_probe.json', summary)
        store.release_lock()
    return summary


# ---------------------------------------------------------------------------
# D2) generic low-volume fallback probe (no parser; for trying other channels)
# ---------------------------------------------------------------------------
def probe_urls(out_dir=OUT, targets=None, *, platform, stage=STAGE, budget=40, timeout=20.0,
               transport=None, clock=None, summary_name='external_probe_fallback.json',
               max_redirect_hops=5, headers=None, stage_cap=None):
    """Fetch a fixed target list under the shared budget; no parser, ledger-counted.

    Same safety as ``run_probe`` (single writer lock, interval retry, manual redirects that each
    count, save_raw + copy_to_failed, pause on block), but records only status/title/hash/bytes and
    a light page verdict. ``targets`` items are ``{'url','city','url_class'}``.
    """
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_state_dir.mkdir(parents=True, exist_ok=True)
    transport = transport or _http_get
    clock = clock or _now_epoch
    targets = list(targets or [])

    store = ExternalBudgetStore(run_state_dir, clock=clock,
                                stage_caps=_store_stage_caps(stage_cap))
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        return {'error': 'budget_lock', 'detail': str(exc)}

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA
    if headers:
        session.headers.update(headers)

    state = {'stop': False, 'blocked': False, 'requests_made': 0}
    blocks = []
    notes = []
    target_summaries = []

    def base_row(city, url, request_id, url_class):
        combo = f'{city}_{platform}' if city else platform
        return {
            'source': platform, 'city': city, 'combo': combo,
            'requested_url': url, 'final_url': url, 'requested_at': now_iso(),
            'status': None, 'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0, 'body_state': None,
            'request_id': request_id, 'stage': stage, 'platform': platform,
            'network_attempt': True, 'url_class': url_class, 'page_class': None,
            'title': None, 'records_parsed': 0, 'unique_request_id': uuid.uuid4().hex,
        }

    def reserve_attempt(city, url, url_class):
        combo = f'{city}_{platform}' if city else platform
        while True:
            result = store.reserve(platform, stage, combo, url, now=clock())
            if result['allowed']:
                return result['request_id']
            if result['reason'] == 'interval_not_elapsed':
                _sleep(result['wait_seconds'])
                continue
            store.record_exhausted(platform=platform, stage=stage, combo=combo,
                                   requested_url=url, reason=result['reason'], now=clock())
            refusal = base_row(city, url, None, url_class)
            refusal['failure_class'] = 'budget_exhausted'
            refusal['network_attempt'] = False
            refusal['blocking_reason'] = result['reason']
            append_jsonl(out_dir / 'requests.jsonl', [refusal])
            notes.append(f"budget_exhausted:{result['reason']}")
            state['stop'] = True
            return None

    def process_attempt(city, url, url_class, chain):
        request_id = reserve_attempt(city, url, url_class)
        if request_id is None:
            return None
        state['requests_made'] += 1
        row = base_row(city, url, request_id, url_class)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, timeout)
        response = outcome['response']
        row['elapsed_ms'] = outcome['elapsed_ms']
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome['error_type']
            row['body_state'] = 'no_body'
            append_jsonl(out_dir / 'requests.jsonl', [row])
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
            row['snapshot_path'] = str(save_raw(raw_dir, row['combo'], url, body, row['sha256']))
        title = ''
        if body:
            try:
                doc = BeautifulSoup(_to_text(body), 'html.parser')
                title = doc.title.get_text(' ', strip=True) if doc.title else ''
            except Exception:
                title = ''
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
            row['failure_class'] = 'redirect'
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        row['title'] = title
        row['page_class'] = classify_fallback_page(body, url=url, status=status, title=title,
                                                   final_url=row.get('final_url'),
                                                   redirect_chain=row.get('redirect_chain'))
        append_jsonl(out_dir / 'requests.jsonl', [row])
        if row.get('snapshot_path') and (row.get('failure_class') or status != 200):
            copy_to_failed(raw_dir, row['snapshot_path'])
        return row

    def execute(city, url, url_class):
        chain = []
        current = url
        while True:
            if state['requests_made'] >= budget:
                notes.append('probe_budget_cap')
                return None
            row = process_attempt(city, current, url_class, chain)
            if row is None or state['stop'] or row.get('status') is None:
                return row
            redirect = (300 <= row['status'] < 400 and row['redirect_chain']
                        and row['redirect_chain'][-1]['url'] == current)
            if not redirect:
                return row
            if len(row['redirect_chain']) >= max_redirect_hops:
                notes.append('too_many_redirects')
                state['stop'] = True
                return row
            chain = row['redirect_chain']
            current = chain[-1]['next']

    try:
        for target in targets:
            if state['stop']:
                break
            city = target.get('city')
            row = execute(city, target.get('url'), target.get('url_class'))
            if row is None:
                break
            target_summaries.append({
                'url': row.get('requested_url'),
                'city': city,
                'url_class': row.get('url_class'),
                'status': row.get('status'),
                'final_url': row.get('final_url'),
                'title': row.get('title'),
                'sha256': row.get('sha256'),
                'bytes': row.get('bytes'),
                'page_class': row.get('page_class'),
                'retry_after': row.get('retry_after'),
            })
            if row.get('page_class') == 'verify_challenge' or row.get('status') in (403, 429):
                reason = 'login_wall' if _row_login_markers(row) else _block_reason(row)
                store.pause_platform(platform, reason, now=clock())
                blocks.append({'url_class': row.get('url_class'),
                               'requested_url': row.get('requested_url'),
                               'status': row.get('status'),
                               'retry_after': row.get('retry_after'),
                               'reason': reason,
                               'evidence_verbatim': row.get('title'),
                               'final_url': row.get('final_url'),
                               'sha256': row.get('sha256')})
                state['blocked'] = True
                state['stop'] = True
    finally:
        rows = read_jsonl(out_dir / 'requests.jsonl')
        requests_used = sum(1 for r in rows if r.get('request_id') is not None)
        summary = {
            'schema': 'external_probe/v1',
            'generated_at': now_iso(),
            'platform': platform,
            'requests_used': requests_used,
            'budget': budget,
            'targets': target_summaries,
            'blocks': blocks,
            'notes': notes + ['通用低量回退探测:不解析记录,仅记录状态/标题/哈希/页面分类',
                              '阻断即暂停平台并停止,不使用代理/账号/cookie轮换'],
            'budget_report': store.report(now=clock()),
        }
        _write_json(out_dir / summary_name, summary)
        store.release_lock()
    return summary


# ---------------------------------------------------------------------------
# E) offline freeze of the external candidate list
# ---------------------------------------------------------------------------
def _is_villa(record):
    return any(tag in VILLA_FORMS for tag in (record.get('type_tags') or []))


def _district_code(alias_index, *texts):
    """Canonical district code for the first alias found in the given label texts."""
    for text in texts:
        if not text:
            continue
        if text in alias_index:
            return alias_index[text]
        for alias, code in alias_index.items():
            if alias and alias in text:
                return code
    return None


def _fill_variety(city_records, chosen, need):
    """Fill remaining city slots round-robin across districts to prefer district variety."""
    buckets = OrderedDict()
    for record in city_records:
        if record.get('district') is None or record.get('stable_id') in chosen:
            continue
        buckets.setdefault(record['district_name'], []).append(record)
    picked = []
    names = list(buckets)
    index = 0
    while len(picked) < need and any(buckets[name] for name in names):
        name = names[index % len(names)]
        if buckets[name]:
            picked.append(buckets[name].pop(0))
        index += 1
    return picked


def _frozen_content_sha256(meta, candidates):
    """Stable fingerprint of the frozen list content (the hash field itself excluded)."""
    payload_meta = {k: v for k, v in meta.items() if k != 'frozen_sha256_before_check'}
    lines = [json.dumps(payload_meta, ensure_ascii=False)]
    # `conclusion` is check-only annotation added by omission_check; exclude so repeated runs hash identically.
    for row in candidates:
        payload = {k: v for k, v in row.items() if k != 'conclusion'}
        lines.append(json.dumps(payload, ensure_ascii=False))
    return hashlib.sha256(('\n'.join(lines) + '\n').encode('utf-8')).hexdigest()


def _block_reason_summary(blocked_rows):
    if blocked_rows:
        statuses = sorted({str(row.get('status')) for row in blocked_rows})
        classes = sorted({row.get('page_class') for row in blocked_rows if row.get('page_class')})
        return (f'贝壳/链家 目录探测受阻：{len(blocked_rows)} 次验证/拦截，'
                f'状态 {statuses}，分类 {classes}')
    return '未解析到任何贝壳/链家目录记录（可能为空目录或结构变更）'


def freeze_candidates(out_dir=OUT, *, target_per_city=50, per_district=2):
    """Freeze the external candidate list offline from saved 贝壳 catalog snapshots."""
    from build_isolation_preview import _read_json
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    aliases = _read_json(out_dir / 'district_aliases.json', {}) or {}
    alias_index = aliases.get('alias_index') or {}
    city_codes = aliases.get('city_codes') or {}
    code_canonical = {}
    city_districts = {}
    for city, entry in (aliases.get('cities') or {}).items():
        canonical = []
        for district in entry.get('districts') or []:
            code_canonical[district['code']] = district['canonical']
            canonical.append(district['canonical'])
        city_districts[city] = canonical

    records = []
    seen = set()
    blocked_rows = []
    for row in rows:
        if row.get('source') != SOURCE:
            continue
        if row.get('page_class') == 'verify_challenge' or row.get('status') in (403, 429):
            blocked_rows.append(row)
        if row.get('page_class') != 'normal' or row.get('url_class') == 'detail':
            continue
        path_str = row.get('snapshot_path')
        if not path_str or not Path(path_str).exists():
            continue
        city = row.get('city')
        try:
            html = Path(path_str).read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        parsed = parse_ke_catalog(html, url=row.get('final_url') or row.get('requested_url') or '')
        for record in parsed['records']:
            key = (city, record.get('stable_id'))
            if not record.get('stable_id') or key in seen:
                continue
            seen.add(key)
            enriched = dict(record)
            enriched['city'] = city
            enriched['url_class'] = row.get('url_class')
            records.append(enriched)

    unmapped = 0
    for record in records:
        code = _district_code(alias_index, record.get('district_text'),
                              record.get('subarea_text'))
        record['district'] = code
        record['district_name'] = code_canonical.get(code) if code else None
        if code is None:
            unmapped += 1

    by_city = defaultdict(list)
    for record in records:
        by_city[record['city']].append(record)

    selected = []
    per_district_limit = per_district
    for city in sorted(by_city):
        city_records = by_city[city]
        districts = defaultdict(list)
        for record in city_records:
            if record['district'] is not None:
                districts[record['district_name']].append(record)
        chosen = set()
        for district_name in sorted(districts):
            bucket = districts[district_name]
            ordered = [r for r in bucket if _is_villa(r)] + \
                [r for r in bucket if not _is_villa(r)]
            picked = 0
            for record in ordered:
                if picked >= per_district_limit or record['stable_id'] in chosen:
                    continue
                chosen.add(record['stable_id'])
                reason = f"贝壳目录 district={district_name}, " + (
                    '列表卡片含别墅标签' if _is_villa(record) else '列表卡片')
                selected.append((city, record, reason))
                picked += 1
        need = target_per_city - sum(1 for c, _, _ in selected if c == city)
        if need > 0:
            for record in _fill_variety(city_records, chosen, need):
                chosen.add(record['stable_id'])
                selected.append((city, record,
                                 f'补齐每城{target_per_city}, 来源页 {record.get("url_class")}'))

    per_city = {}
    district_counts = {}
    covered = defaultdict(set)
    for city, record, _reason in selected:
        per_city[city] = per_city.get(city, 0) + 1
        district_name = record.get('district_name')
        if district_name:
            district_counts.setdefault(city, {})
            district_counts[city][district_name] = \
                district_counts[city].get(district_name, 0) + 1
            covered[city].add(district_name)

    uncovered = {}
    for city, canonical in city_districts.items():
        uncovered[city] = [name for name in canonical if name not in covered.get(city, set())]

    frozen_at = now_iso()
    index_by_city = defaultdict(int)
    candidates = []
    for city, record, reason in selected:
        city_code = city_codes.get(city) or ''
        index = index_by_city[city]
        index_by_city[city] += 1
        candidates.append({
            'record_type': 'candidate',
            'schema': 'external_candidate/v1',
            'candidate_id': f'{city_code[:2]}-{index:04d}',
            'city': city,
            'district': record.get('district'),
            'district_name': record.get('district_name'),
            'name': record.get('name'),
            'source': '贝壳',
            'source_url': record.get('detail_url'),
            'source_stable_id': record.get('stable_id'),
            'subarea': record.get('subarea_text'),
            'year_text': record.get('year_text'),
            'type_tags': list(record.get('type_tags') or []),
            'selection_reason': reason,
            'frozen_at': frozen_at,
            'conclusion': {
                'found_in_trial_catalog': None,
                'primary_platform_year_supplied': None,
                'target_type_supplied': None,
                'usable_position': None,
                'metro_within_2km_decidable': None,
                'metro_within_2km': None,
                'judgement': None,
            },
        })

    meta = {
        'record_type': 'frozen_list_meta',
        'schema': 'external_candidates/v1',
        'frozen_at': frozen_at,
        'selection_method': SELECTION_METHOD,
        'channels': [{'name': '贝壳/链家', 'platform': PLATFORM,
                      'scope': '北京/上海 小区目录公开列表页(低量)', 'in_build_inputs': False}],
        'target_per_city': target_per_city,
        'per_district_target': per_district_limit,
        'candidates_count': len(candidates),
        'per_city': per_city,
        'per_district': district_counts,
        'uncovered_districts': uncovered,
        'district_unmapped': unmapped,
        'status': 'ok',
    }

    if not records:
        meta['status'] = 'blocked'
        meta['candidates_count'] = 0
        meta['block_reason'] = _block_reason_summary(blocked_rows)
        meta['frozen_sha256_before_check'] = _frozen_content_sha256(meta, [])
        write_jsonl(out_dir / 'external_candidates.jsonl', [meta])
        return meta

    meta['frozen_sha256_before_check'] = _frozen_content_sha256(meta, candidates)
    write_jsonl(out_dir / 'external_candidates.jsonl', [meta] + candidates)
    return meta


# ---------------------------------------------------------------------------
# E2) government 老旧小区 project-notice freeze (fallback channel)
# ---------------------------------------------------------------------------
GOV_CHANNELS = [
    {'name': '贝壳/链家', 'platform': '贝壳链家', 'status': 'blocked',
     'block_reason': 'login_wall',
     'evidence': {'status': 200, 'title': '登录',
                  'sha256': 'c893b8d3cf8d4d8d52e67222dfd520f4785fee924ba455e774761e1002864dee'},
     'in_build_inputs': False},
    {'name': '我爱我家(5i5j)', 'platform': 'wawj', 'status': 'blocked',
     'block_reason': 'login_wall', 'in_build_inputs': False},
    {'name': '上海网上房地产', 'platform': 'fangdi', 'status': 'blocked',
     'block_reason': 'http_412', 'in_build_inputs': False},
    {'name': '高德POI', 'platform': 'amap', 'status': 'blocked',
     'block_reason': 'antibot_captcha', 'in_build_inputs': False},
    {'name': '百度地图', 'platform': 'baidu', 'status': 'blocked',
     'block_reason': 'recaptcha', 'in_build_inputs': False},
    {'name': '北京市住房和城乡建设委员会 老旧小区综合整治项目公示', 'platform': 'gov4',
     'status': 'usable', 'scope': '北京; 央企在京第八批老旧小区综合整治项目 docx 附件; 63项',
     'in_build_inputs': False},
    {'name': 'GitHub raw open datasets', 'platform': 'opendata', 'status': 'blocked',
     'block_reason': 'connection_error', 'in_build_inputs': False},
]
GOV_SELECTION_METHOD = ('冻结政府项目清单候选：每北京行政区尽量>=2，再补齐至每城目标；'
                        '来源为北京住建委老旧小区综合整治项目公示。')
GOV_LIMITATIONS = [
    '上海全部渠道受阻/未覆盖，本清单无上海候选。',
    '候选派生自政府老旧小区项目名称（按楼栋名归一化），非小区目录原始字段。',
    '无法获得别墅/叠拼/高层等形态样本。',
    '便利性目录外样本，不是全市召回率。',
]


def _gov_uncovered(covered, city_districts):
    """Canonical districts with no selected sample; both cities always present."""
    result = {}
    for city, canonical in city_districts.items():
        result[city] = [name for name in canonical if name not in covered]
    result.setdefault('北京', [])
    result.setdefault('上海', [])
    return result


def freeze_gov_candidates(out_dir=OUT, *, target_per_city=50, per_district=2):
    """Freeze the 老旧小区 government project notice as the fallback external candidate list."""
    from build_isolation_preview import _read_json
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    aliases = _read_json(out_dir / 'district_aliases.json', {}) or {}
    alias_index = aliases.get('alias_index') or {}
    code_canonical = {}
    city_districts = {}
    for city, entry in (aliases.get('cities') or {}).items():
        canonical = []
        for district in entry.get('districts') or []:
            code_canonical[district['code']] = district['canonical']
            canonical.append(district['canonical'])
        city_districts[city] = canonical

    aggregated = []
    seen = set()
    for row in rows:
        if row.get('platform') != GOV_PLATFORM:
            continue
        path_str = row.get('snapshot_path')
        if not path_str or not Path(path_str).exists():
            continue
        source_url = row.get('requested_url') or row.get('final_url')
        for parsed in parse_bj_oldcommunity_docx(path_str):
            code = _district_code(alias_index, parsed.get('district'))
            district_name = code_canonical.get(code) if code else None
            key = (district_name, parsed.get('community_name'))
            if key in seen:
                continue
            seen.add(key)
            aggregated.append({
                'district': code, 'district_name': district_name,
                'name': parsed.get('community_name'),
                'project_name': parsed.get('project_name'), 'source_url': source_url,
            })

    frozen_at = now_iso()
    if not aggregated:
        meta = {
            'record_type': 'frozen_list_meta', 'schema': 'external_candidates/v1',
            'frozen_at': frozen_at, 'selection_method': GOV_SELECTION_METHOD,
            'channels': GOV_CHANNELS, 'target_per_city': target_per_city,
            'per_district_target': per_district, 'candidates_count': 0,
            'per_city': {'北京': 0, '上海': 0}, 'per_district': {},
            'uncovered_districts': _gov_uncovered(set(), city_districts),
            'status': 'blocked', 'block_reason': '未找到可解析的 gov4 docx 附件',
            'limitations': GOV_LIMITATIONS,
            'frozen_sha256_before_check': _frozen_content_sha256(meta, []),
        }
        write_jsonl(out_dir / 'external_candidates.jsonl', [meta])
        return meta

    by_district = defaultdict(list)
    for record in aggregated:
        if record['district_name']:
            by_district[record['district_name']].append(record)
    selected = []
    chosen = set()
    for district_name in sorted(by_district):
        for record in by_district[district_name][:per_district]:
            key = (record['district_name'], record['name'])
            if key in chosen:
                continue
            chosen.add(key)
            selected.append(record)
    if len(selected) < target_per_city:
        for record in aggregated:
            if len(selected) >= target_per_city:
                break
            if not record['district_name']:
                continue
            key = (record['district_name'], record['name'])
            if key in chosen:
                continue
            chosen.add(key)
            selected.append(record)

    candidates = []
    for index, record in enumerate(selected):
        candidates.append({
            'record_type': 'candidate', 'schema': 'external_candidate/v1',
            'candidate_id': f'11-{index:04d}', 'city': '北京',
            'district': record['district'], 'district_name': record['district_name'],
            'name': record['name'], 'source': GOV_SOURCE, 'source_url': record['source_url'],
            'source_stable_id': None, 'subarea': None, 'year_text': None, 'type_tags': [],
            'selection_reason': ('北京住建委老旧小区综合整治项目公示(政府/物业项目清单); '
                                 f"原始项目名称={record['project_name']}"),
            'frozen_at': frozen_at,
            'conclusion': {
                'found_in_trial_catalog': None,
                'primary_platform_year_supplied': None,
                'target_type_supplied': None,
                'usable_position': None,
                'metro_within_2km_decidable': None,
                'metro_within_2km': None,
                'judgement': None,
            },
        })

    per_district_counts = {}
    covered = set()
    for record in selected:
        district_name = record['district_name']
        if district_name:
            per_district_counts[district_name] = per_district_counts.get(district_name, 0) + 1
            covered.add(district_name)

    meta = {
        'record_type': 'frozen_list_meta', 'schema': 'external_candidates/v1',
        'frozen_at': frozen_at, 'selection_method': GOV_SELECTION_METHOD,
        'channels': GOV_CHANNELS, 'target_per_city': target_per_city,
        'per_district_target': per_district, 'candidates_count': len(candidates),
        'per_city': {'北京': len(candidates), '上海': 0},
        'per_district': {'北京': per_district_counts},
        'uncovered_districts': _gov_uncovered(covered, city_districts),
        'status': 'ok', 'limitations': GOV_LIMITATIONS,
    }
    meta['frozen_sha256_before_check'] = _frozen_content_sha256(meta, candidates)
    write_jsonl(out_dir / 'external_candidates.jsonl', [meta] + candidates)
    return meta


# ---------------------------------------------------------------------------
# F) offline omission check against the isolated trial catalog
# ---------------------------------------------------------------------------
def _external_position(candidate):
    """True only when the external candidate itself supplies a usable position."""
    if candidate.get('lng') is not None and candidate.get('lat') is not None:
        return True
    coords = candidate.get('coords') or {}
    return coords.get('lng') is not None and coords.get('lat') is not None


def omission_check(out_dir=OUT, *, primary_source=None, external_path=None,
                   lookup=None, stations=None):
    """Check the FROZEN external candidate list for omissions in the isolated trial catalog."""
    from build_anjuke_trial_prep import _haversine_m
    from community_catalog import _normalize_name
    from source_trial_deliverables import _pick_hit
    out_dir = Path(out_dir)
    external_path = Path(external_path) if external_path else \
        out_dir / 'external_candidates.jsonl'
    rows = read_jsonl(external_path)
    meta_row = next((r for r in rows if r.get('record_type') == 'frozen_list_meta'), {})
    candidates = [r for r in rows if r.get('record_type') == 'candidate']

    if meta_row.get('status') == 'blocked' or not candidates:
        write_jsonl(out_dir / 'missing_candidates.jsonl', [])
        meta = {
            'schema': 'missing_candidates_meta/v1',
            'generated_at': now_iso(),
            'primary_source': primary_source,
            'status': 'blocked',
            'selection_method': meta_row.get('selection_method') or SELECTION_METHOD,
            'per_city': {},
            'totals': {'found': 0, 'not_found': 0, 'undecidable': 0,
                       'pre_inclusion_missed': 0, 'post_inclusion_fixed': 0,
                       'post_inclusion_discovery_only': 0},
            'note': '未执行遗漏检验：无可用外部候选；不得报告“未找到 0”。',
            'external_pool_not_used_for_build': True,
        }
        _write_json(out_dir / 'missing_candidates.meta.json', meta)
        return meta

    if lookup is None:
        import source_trial_deliverables as std
        lookup = std._build_index(std._load_context())['by_name']
    if primary_source is None:
        import source_trial_deliverables as std
        primary_source = std._resolve_primary(out_dir)
    if stations is None:
        import build_anjuke_trial_prep as prep
        stations = prep._operational_stations(
            out_dir / 'preview' / 'data' / 'metro_operational.min.json')

    checked_at = now_iso()
    out_rows = []
    conclusions = {}
    per_city = {}
    for candidate in candidates:
        city = candidate.get('city')
        name = candidate.get('name')
        hit = _pick_hit(lookup.get((city, _normalize_name(name))), None)
        found = hit is not None
        year_supplied = type_supplied = usable_position = False
        metro_decidable = False
        metro_within = None
        matched_name = None
        matched_ids = {}
        if found:
            _, community = hit
            policy = community.get('year_policy') or {}
            year_supplied = policy.get('status') == 'recorded'
            type_supplied = bool(community.get('forms'))
            coord = community.get('coord') or {}
            usable_position = bool(coord and coord.get('distance_ready'))
            metro_decidable = usable_position and bool(stations)
            if metro_decidable:
                unified = coord.get('unified_wgs84') or {}
                lng, lat = unified.get('lng'), unified.get('lat')
                if lng is not None and lat is not None:
                    metro_within = min(_haversine_m(lng, lat, s[0], s[1])
                                       for s in stations) <= 2000
                else:
                    metro_decidable = False
            matched_name = community.get('name')
            matched_ids = dict(community.get('platform_ids') or {})

        if not found:
            judgement = 'not_found'
        elif year_supplied and type_supplied and usable_position and metro_decidable:
            judgement = 'found'
        else:
            judgement = 'undecidable'

        if not found:
            primary_year_reason = '未在实验目录发现'
            location_reason = '未在实验目录发现'
            type_reason = '未在实验目录发现'
            metro_reason = '未在实验目录发现'
        else:
            primary_year_reason = '实验目录已记录主平台年份' if year_supplied \
                else '实验目录未记录主平台年份'
            location_reason = '实验目录已记录可用坐标' if usable_position \
                else '实验目录无可用坐标'
            type_reason = '实验目录已记录住宅形态' if type_supplied \
                else '实验目录未记录住宅形态'
            if not usable_position:
                metro_reason = '无可用位置,无法判定地铁距离'
            elif not stations:
                metro_reason = '无运营站点集,无法判定地铁距离'
            elif metro_within is None:
                metro_reason = '地铁距离不可判定'
            else:
                metro_reason = '距运营地铁站<=2km' if metro_within else '距运营地铁站>2km'

        conclusion = {
            'found_in_trial_catalog': found,
            'primary_platform_year_supplied': year_supplied if found else None,
            'target_type_supplied': type_supplied if found else None,
            'usable_position': usable_position if found else None,
            'metro_within_2km_decidable': metro_decidable if found else None,
            'metro_within_2km': metro_within,
            'judgement': judgement,
        }
        conclusions[candidate.get('candidate_id')] = conclusion

        entry = per_city.setdefault(city, {'found': 0, 'not_found': 0, 'undecidable': 0})
        entry[judgement] = entry.get(judgement, 0) + 1

        out_rows.append({
            'schema': 'missing_candidate/v1',
            'candidate_id': candidate.get('candidate_id'),
            'city': city,
            'district': candidate.get('district'),
            'name': name,
            'source': candidate.get('source'),
            'source_url': candidate.get('source_url'),
            'found_in_trial_catalog': found,
            'primary_platform_year_supplied': year_supplied if found else None,
            'target_type_supplied': type_supplied if found else None,
            'usable_position': usable_position if found else None,
            'metro_within_2km_decidable': metro_decidable if found else None,
            'metro_within_2km': metro_within,
            'matched_community_name': matched_name,
            'matched_platform_ids': matched_ids,
            'primary_year_reason': primary_year_reason,
            'location_reason': location_reason,
            'type_reason': type_reason,
            'metro_reason': metro_reason,
            'judgement': judgement,
            'checked_at': checked_at,
        })

    totals = {
        'found': sum(1 for row in out_rows if row['judgement'] == 'found'),
        'not_found': sum(1 for row in out_rows if row['judgement'] == 'not_found'),
        'undecidable': sum(1 for row in out_rows if row['judgement'] == 'undecidable'),
    }
    totals['pre_inclusion_missed'] = totals['not_found']
    totals['post_inclusion_fixed'] = sum(
        1 for candidate, row in zip(candidates, out_rows)
        if row['judgement'] == 'not_found' and _external_position(candidate))
    totals['post_inclusion_discovery_only'] = totals['not_found']

    frozen_hash = meta_row.get('frozen_sha256_before_check')
    after_hash = _frozen_content_sha256(meta_row, candidates)
    frozen_hash_unchanged = (frozen_hash is None) or (after_hash == frozen_hash)
    if frozen_hash_unchanged:
        frozen_hash_change_reason = ''
    else:
        frozen_hash_change_reason = (
            '旧版冻结哈希逐字统计候选行，前次检验写入的 conclusion 注释改变了哈希；'
            '现已在哈希中排除 conclusion。')


    identity_fields = ('candidate_id', 'city', 'district', 'name', 'source',
                       'source_url', 'source_stable_id')
    rewritten = []
    identity_verified = True
    for candidate in candidates:
        new_row = dict(candidate)
        new_row['conclusion'] = conclusions.get(candidate.get('candidate_id'))
        if any(candidate.get(field) != new_row.get(field) for field in identity_fields):
            identity_verified = False
        rewritten.append(new_row)
    write_jsonl(out_dir / 'external_candidates.jsonl', [meta_row] + rewritten)
    write_jsonl(out_dir / 'missing_candidates.jsonl', out_rows)

    meta = {
        'schema': 'missing_candidates_meta/v1',
        'generated_at': checked_at,
        'primary_source': primary_source,
        'status': 'ok',
        'selection_method': meta_row.get('selection_method') or SELECTION_METHOD,
        'per_city': per_city,
        'totals': totals,
        'uncovered_districts': meta_row.get('uncovered_districts') or {},
        'channel': meta_row.get('channels') or [],
        'external_pool_not_used_for_build': True,
        'method': METHOD_TEXT,
        'limitations': LIMITATIONS,
        'frozen_sha256_before_check': frozen_hash,
        'frozen_sha256_after_check': after_hash,
        'frozen_hash_unchanged': frozen_hash_unchanged,
        'frozen_hash_change_reason': frozen_hash_change_reason,
        'identity_fields_verified': identity_verified,
    }
    _write_json(out_dir / 'missing_candidates.meta.json', meta)
    return meta


# ---------------------------------------------------------------------------
# G) cli
# ---------------------------------------------------------------------------
def _parse_headers(values):
    """`--header "K: V"` list -> dict, split on the first colon."""
    headers = {}
    for raw in values or []:
        key, sep, value = raw.partition(':')
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='External (贝壳/链家) candidate probe + freeze + omission check.')
    sub = parser.add_subparsers(dest='command', required=True)
    probe = sub.add_parser('probe', help='budgeted discovery probe of the directory')
    probe.add_argument('--budget', type=int, default=40)
    probe.add_argument('--header', action='append', default=[],
                       help='"K: V" request header; repeatable')
    probe.add_argument('--stage-cap', type=int, default=None,
                       help='override 外部候选探测 stage cap (default 40)')
    probe.add_argument('--out', default=str(OUT))
    freeze = sub.add_parser('freeze', help='offline freeze of the external candidate list')
    freeze.add_argument('--out', default=str(OUT))
    gov = sub.add_parser('freeze-gov', help='freeze the 老旧小区 government project candidates')
    gov.add_argument('--out', default=str(OUT))
    check = sub.add_parser('check', help='offline omission check against the trial catalog')
    check.add_argument('--out', default=str(OUT))
    fallback = sub.add_parser('probe-fallback',
                              help='generic low-volume probe of a fixed target list')
    fallback.add_argument('--platform', required=True)
    fallback.add_argument('--target', action='append', default=[],
                          help='city|url_class|url (city may be empty); repeatable')
    fallback.add_argument('--header', action='append', default=[],
                          help='"K: V" request header; repeatable')
    fallback.add_argument('--stage-cap', type=int, default=None,
                          help='override 外部候选探测 stage cap (default 40)')
    fallback.add_argument('--budget', type=int, default=20)
    fallback.add_argument('--out', default=str(OUT))
    args = parser.parse_args(argv)

    if args.command == 'probe':
        summary = run_probe(Path(args.out), budget=args.budget,
                            headers=_parse_headers(args.header), stage_cap=args.stage_cap)
    elif args.command == 'freeze':
        summary = freeze_candidates(Path(args.out))
    elif args.command == 'freeze-gov':
        summary = freeze_gov_candidates(Path(args.out))
    elif args.command == 'probe-fallback':
        targets = []
        for raw in args.target:
            city, url_class, url = (raw.split('|', 2) + ['', ''])[:3]
            targets.append({'city': city or None, 'url_class': url_class, 'url': url})
        summary = probe_urls(Path(args.out), targets, platform=args.platform,
                             budget=args.budget, headers=_parse_headers(args.header),
                             stage_cap=args.stage_cap)
    else:
        summary = omission_check(Path(args.out))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
