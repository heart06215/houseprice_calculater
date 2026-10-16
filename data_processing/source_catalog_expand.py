"""Stage runner: bounded, budget-locked, resumable 目录扩大试爬 (catalog expansion trial).

Runs an ordered PLAN over 房天下/中原/丽兹行 partition catalogs: page-1 coverage plus
depth samples, every network attempt appended to an append-only requests.jsonl with a
``chunk_id``, raw bodies saved with sha256, blocks classified, fixed parsers reused,
per-cell coverage plus a resumable checkpoint. Budget/io/parsers are reused from
``source_pilot`` and the entry PLAN from ``source_entry_probe``; never re-implemented.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import source_pilot as sp
from source_pilot import (
    BLOCK_CLASSES,
    BudgetLockError,
    BudgetStore,
    FETCH_STATES,
    MAX_REDIRECT_HOPS,
    PARSERS,
    TRIAL_TOTAL_CAP,
    UA,
    _now_epoch,
    _sleep,
    append_jsonl,
    combo_of,
    copy_to_failed,
    fetch_status,
    is_challenge,
    load_platform_policy_overrides,
    merge_observations,
    merge_signals,
    now_iso,
    read_jsonl,
    save_raw,
    single_request,
    write_jsonl,
)

STAGE = '目录扩大试爬'
ENTRY_STAGE = '入口与结构探测'
DEFAULT_OUT = 'output/source_trial_20260922'
DEFAULT_MAX_REQUESTS = 200
CHECKPOINT_SCHEMA = 'source_catalog_expand_checkpoint/v1'
REPORT_SCHEMA = 'source_catalog_expand_report/v1'
COVERAGE_SCHEMA = 'source_trial_coverage/v2'
VERIFY_SCHEMA = 'source_catalog_expand_verify/v1'
# source_pilot.single_request doubles this into a (connect, read) pair.
TIMEOUT = 20
NAV_CAP = 100
CENTANET_PER_PAGE = 10
# Suspended (not fatal) budget reasons: only the offending source pauses for this chunk.
SUSPEND_REASONS = ('platform_daily_cap', 'platform_paused')
FANG_HOST = {'北京': 'https://esf.fang.com', '上海': 'https://sh.esf.fang.com'}
FANG_VILLA_PATH = '__2_3_0_0_1_0_0_0'
FANG_RESIDENCE_PATH = '__1_3_0_0_1_0_0_0'
CENTANET_HOST = 'https://sh.centanet.com'
LIZIHANG_HOST = {'北京': 'https://bj.lizihang.com', '上海': 'https://sh.lizihang.com'}

# ---------------------------------------------------------------------------
# PLAN inputs (verified partition facts; do not re-derive)
# ---------------------------------------------------------------------------
FANG_BJ_DISTRICTS = [('1', '朝阳'), ('2', '东城'), ('3', '西城'), ('6', '丰台'),
                     ('7', '石景山'), ('8', '房山'), ('9', '门头沟'), ('10', '通州'),
                     ('11', '顺义'), ('12', '昌平'), ('13', '密云'), ('14', '怀柔'),
                     ('15', '延庆'), ('16', '平谷'), ('585', '大兴'), ('15277', '海淀')]
FANG_SH_DISTRICTS = [('25', '浦东'), ('18', '闵行'), ('19', '徐汇'), ('20', '长宁'),
                     ('21', '静安'), ('23', '虹口'), ('24', '黄浦'), ('26', '杨浦'),
                     ('28', '普陀'), ('29', '嘉定'), ('30', '宝山'), ('31', '青浦'),
                     ('32', '奉贤'), ('35', '金山'), ('586', '松江'), ('996', '崇明')]
FANG_BJ_DEPTH = [('1', '朝阳'), ('15277', '海淀'), ('2', '东城'), ('3', '西城'),
                 ('6', '丰台'), ('12', '昌平'), ('585', '大兴'), ('10', '通州')]
FANG_SH_DEPTH = [('25', '浦东'), ('18', '闵行'), ('19', '徐汇'), ('20', '长宁'),
                 ('21', '静安'), ('24', '黄浦'), ('26', '杨浦'), ('28', '普陀')]

CENTANET_DISTRICT_LABELS = {
    'pudongxinqu': '浦东', 'baoshanqu': '宝山', 'changningqu': '长宁',
    'chongmingqu': '崇明', 'fengxianqu': '奉贤', 'hongkouqu': '虹口', 'huangpuqu': '黄浦',
    'jiadingqu': '嘉定', 'jinganqu': '静安', 'jinshanqu': '金山', 'minxingqu': '闵行',
    'putuoqu': '普陀', 'qingpuqu': '青浦', 'songjiangqu': '松江', 'xuhuiqu': '徐汇',
    'yangpuqu': '杨浦', 'qita': '其他',
}
CENTANET_DISTRICT_SLUGS = ['pudongxinqu', 'baoshanqu', 'changningqu', 'chongmingqu',
                           'fengxianqu', 'hongkouqu', 'huangpuqu', 'jiadingqu',
                           'jinganqu', 'jinshanqu', 'minxingqu', 'putuoqu', 'qingpuqu',
                           'songjiangqu', 'xuhuiqu', 'yangpuqu', 'qita']
CENTANET_REAL_DISTRICTS = [s for s in CENTANET_DISTRICT_SLUGS if s != 'qita']
CENTANET_BOARDS = ['lujiazui', 'anting', 'kangqiao', 'songbao', 'yangdong', 'linpinglu',
                   'zhoujiazuilu', 'zhuanqiao', 'dongwaitan']
CENTANET_ALL_VILLA_DEEP = [5, 10, 15, 20, 23]
CENTANET_ALL_DEEP = [5, 10, 25, 50, 100]
CENTANET_VILLA_DISTRICT_DEEP = ['pudongxinqu', 'minxingqu', 'songjiangqu']

# Deep-pagination resume inputs: cells already read to the listed depth during the
# exhausted 212-target pass; section E re-probes the next pages (append-only).
LIZIHANG_DEEP_P3_DONE = [
    ('北京', '110101'), ('北京', '110102'), ('北京', '110105'), ('北京', '110106'),
    ('北京', '110112'), ('北京', '110115'), ('上海', '310101'), ('上海', '310104'),
    ('上海', '310105'), ('上海', '310106'), ('上海', '310112'), ('上海', '310113'),
    ('上海', '310115'),
]
LIZIHANG_DEEP_P2_DONE = [
    ('北京', '110107'), ('北京', '110111'), ('北京', '110113'), ('上海', '310107'),
    ('上海', '310109'), ('上海', '310110'), ('上海', '310117'), ('上海', '310118'),
]
LIZIHANG_RETRY_TRUNCATED = [('北京', '110108')]
CENTANET_DECLARED_LAST = {
    'pudongxinqu': 100, 'baoshanqu': 99, 'changningqu': 68, 'chongmingqu': 6,
    'fengxianqu': 8, 'hongkouqu': 85, 'huangpuqu': 58, 'jiadingqu': 79,
    'jinganqu': 93, 'jinshanqu': 6, 'minxingqu': 79, 'putuoqu': 64,
    'qingpuqu': 17, 'songjiangqu': 10, 'xuhuiqu': 100, 'yangpuqu': 100, 'qita': 6,
}
CENTANET_BOARD_DECLARED_LAST = {
    'lujiazui': 6, 'anting': 13, 'kangqiao': 9, 'songbao': 19, 'yangdong': 6,
    'linpinglu': 9, 'zhoujiazuilu': 24, 'zhuanqiao': 6, 'dongwaitan': 13,
}

LIZIHANG_BJ_CODES = [('110101', '东城'), ('110102', '西城'), ('110105', '朝阳'),
                     ('110106', '丰台'), ('110107', '石景山'), ('110108', '海淀'),
                     ('110109', '门头沟'), ('110111', '房山'), ('110112', '通州'),
                     ('110113', '顺义'), ('110114', '昌平'), ('110115', '大兴'),
                     ('110116', '怀柔'), ('110117', '平谷'), ('110228', '密云'),
                     ('110229', '延庆')]
LIZIHANG_SH_CODES = [('310101', '黄浦'), ('310104', '徐汇'), ('310105', '长宁'),
                     ('310106', '静安'), ('310107', '普陀'), ('310109', '虹口'),
                     ('310110', '杨浦'), ('310112', '闵行'), ('310113', '宝山'),
                     ('310114', '嘉定'), ('310115', '浦东'), ('310116', '金山'),
                     ('310117', '松江'), ('310118', '青浦'), ('310120', '奉贤'),
                     ('310230', '崇明')]
LIZIHANG_BJ_DEPTH_CODES = ['110105', '110108', '110101', '110102', '110106', '110114',
                           '110115', '110112']
LIZIHANG_SH_DEPTH_CODES = ['310115', '310112', '310104', '310105', '310113', '310114',
                           '310106', '310101']
LIZIHANG_LABELS = {'北京': dict(LIZIHANG_BJ_CODES), '上海': dict(LIZIHANG_SH_CODES)}


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------
def fang_page(url, n):
    """Page N of a 房天下 housing listing: replace the 7th '_'-separated path field."""
    parts = list(urlsplit(url).path.split('_'))
    if len(parts) <= 6:
        return url
    parts[6] = str(n)
    return urlsplit(url)._replace(path='_'.join(parts)).geturl()


def fang_district_url(city, code):
    return f'{FANG_HOST[city]}/housing/{code}__0_3_0_0_1_0_0_0/'


def fang_villa_url(city):
    return f'{FANG_HOST[city]}/housing/{FANG_VILLA_PATH}/'


def fang_residence_url(city):
    return f'{FANG_HOST[city]}/housing/{FANG_RESIDENCE_PATH}/'


def fang_partition(url):
    """Partition key of a 房天下 page: its page-1 URL."""
    return fang_page(url, 1)


def centanet_page(slug, n):
    base = f'{CENTANET_HOST}/xiaoqu/{slug}/'
    return base if n <= 1 else f'{CENTANET_HOST}/xiaoqu/{slug}/g{n}/'


def centanet_villa_page(slug, n):
    base = f'{CENTANET_HOST}/xiaoqu/{slug}/t2/'
    return base if n <= 1 else f'{CENTANET_HOST}/xiaoqu/{slug}/t2g{n}/'


def centanet_all_page(n):
    return f'{CENTANET_HOST}/xiaoqu/' if n <= 1 else f'{CENTANET_HOST}/xiaoqu/g{n}/'


def centanet_all_villa_page(n):
    return f'{CENTANET_HOST}/xiaoqu/t2/' if n <= 1 else f'{CENTANET_HOST}/xiaoqu/t2g{n}/'


def centanet_partition(url):
    """Partition key of a 中原 page: strip the gN/t2gN page suffix."""
    path = urlsplit(url).path
    path = re.sub(r't2g\d+/?$', 't2/', path)
    path = re.sub(r'g\d+/?$', '', path)
    if not path.endswith('/'):
        path += '/'
    return f'{CENTANET_HOST}{path}'


def lizihang_page(city, code, n):
    base = f'{LIZIHANG_HOST[city]}/loupan/{code}'
    return base if n <= 1 else f'{base}/p{n}'


def _interleave(a, b):
    """Round-robin two lists starting with `a`; append the longer list's tail."""
    return [item for pair in zip(a, b) for item in pair] + a[len(b):] + b[len(a):]


# ---------------------------------------------------------------------------
# PLAN (ordered; index asc) — exactly 212 targets
# ---------------------------------------------------------------------------
def _t(index, source, city, district, catalog_kind, url, parser, site_pages=1, site_count=None):
    return {'index': index, 'source': source, 'city': city, 'district': district,
            'catalog_kind': catalog_kind, 'url': url, 'parser': parser,
            'site_pages': site_pages, 'site_count': site_count}


def build_plan():
    targets = []

    def add(source, city, district, catalog_kind, url, parser, site_pages=1, site_count=None):
        targets.append(_t(len(targets), source, city, district, catalog_kind, url, parser,
                          site_pages, site_count))

    # A) 房天下 page-1 coverage (36)
    for code, label in FANG_BJ_DISTRICTS:
        add('房天下', '北京', label, '普通', fang_district_url('北京', code), 'fang')
    for code, label in FANG_SH_DISTRICTS:
        add('房天下', '上海', label, '普通', fang_district_url('上海', code), 'fang')
    for city in ('北京', '上海'):
        add('房天下', city, '全城', '别墅', fang_villa_url(city), 'fang')
    for city in ('北京', '上海'):
        add('房天下', city, '全城', '住宅', fang_residence_url(city), 'fang')

    # B) 房天下 depth (24) — 60 fang targets total so the 60/day cap forces refusals.
    for city in ('北京', '上海'):
        for page in (2, 3):
            add('房天下', city, '全城', '别墅', fang_page(fang_villa_url(city), page), 'fang')
    for city in ('北京', '上海'):
        add('房天下', city, '全城', '住宅', fang_page(fang_residence_url(city), 2), 'fang')
    for code, label in FANG_BJ_DEPTH:
        add('房天下', '北京', label, '普通', fang_page(fang_district_url('北京', code), 2), 'fang')
    for code, label in FANG_SH_DEPTH:
        add('房天下', '上海', label, '普通', fang_page(fang_district_url('上海', code), 2), 'fang')
    for city in ('北京', '上海'):
        add('房天下', city, '全城', '别墅', fang_page(fang_villa_url(city), 4), 'fang')

    # C) 中原 上海 (72)
    for slug in CENTANET_DISTRICT_SLUGS:
        label = CENTANET_DISTRICT_LABELS[slug]
        add('中原', '上海', label, '普通', centanet_page(slug, 1), 'centanet')
        add('中原', '上海', label, '别墅', centanet_villa_page(slug, 1), 'centanet')
    for slug in CENTANET_REAL_DISTRICTS:  # depth: district 普通 p2
        add('中原', '上海', CENTANET_DISTRICT_LABELS[slug], '普通',
            centanet_page(slug, 2), 'centanet')
    for board in CENTANET_BOARDS:
        add('中原', '上海', board, '普通', centanet_page(board, 1), 'centanet')
    for page in CENTANET_ALL_VILLA_DEEP:
        add('中原', '上海', '全城', '别墅', centanet_all_villa_page(page), 'centanet')
    for page in CENTANET_ALL_DEEP:
        add('中原', '上海', '全城', '普通', centanet_all_page(page), 'centanet')
    for slug in CENTANET_VILLA_DISTRICT_DEEP:
        add('中原', '上海', CENTANET_DISTRICT_LABELS[slug], '别墅',
            centanet_villa_page(slug, 5), 'centanet')

    # D) 丽兹行 (80)
    for code, label in LIZIHANG_BJ_CODES:
        add('丽兹行', '北京', label, '普通', lizihang_page('北京', code, 1), 'lizihang')
    for code, label in LIZIHANG_SH_CODES:
        add('丽兹行', '上海', label, '普通', lizihang_page('上海', code, 1), 'lizihang')
    for code, label in LIZIHANG_BJ_CODES:
        add('丽兹行', '北京', label, '普通', lizihang_page('北京', code, 2), 'lizihang')
    for code, label in LIZIHANG_SH_CODES:
        add('丽兹行', '上海', label, '普通', lizihang_page('上海', code, 2), 'lizihang')
    for code in LIZIHANG_BJ_DEPTH_CODES:
        add('丽兹行', '北京', LIZIHANG_LABELS['北京'][code], '普通',
            lizihang_page('北京', code, 3), 'lizihang')
    for code in LIZIHANG_SH_DEPTH_CODES:
        add('丽兹行', '上海', LIZIHANG_LABELS['上海'][code], '普通',
            lizihang_page('上海', code, 3), 'lizihang')

    # E) deep pagination resume (200) — appended at indices 212+ so resume keys keep
    # working; legacy A-D stay at their original indices.
    lizihang_deep = []
    for city, code in LIZIHANG_DEEP_P3_DONE:
        for page in (4, 5):
            lizihang_deep.append(('丽兹行', city, LIZIHANG_LABELS[city][code], '普通',
                                  lizihang_page(city, code, page), 'lizihang'))
    for city, code in LIZIHANG_DEEP_P2_DONE:
        for page in (3, 4, 5):
            lizihang_deep.append(('丽兹行', city, LIZIHANG_LABELS[city][code], '普通',
                                  lizihang_page(city, code, page), 'lizihang'))
    for city, code in LIZIHANG_RETRY_TRUNCATED:
        # 海淀: expand-stage p1 500, p3 500; p2 already 200 -> retry p1,p3
        for page in (1, 3):
            lizihang_deep.append(('丽兹行', city, LIZIHANG_LABELS[city][code], '普通',
                                  lizihang_page(city, code, page), 'lizihang'))

    centanet_deep = []
    for slug in CENTANET_REAL_DISTRICTS:
        label = CENTANET_DISTRICT_LABELS[slug]
        for page in (3, 4, 5):
            centanet_deep.append(('中原', '上海', label, '普通',
                                  centanet_page(slug, page), 'centanet'))
        if CENTANET_DECLARED_LAST[slug] > 5:
            centanet_deep.append(('中原', '上海', label, '普通',
                                  centanet_page(slug, CENTANET_DECLARED_LAST[slug]), 'centanet'))
    for page in (2, 3, 4, 5):
        centanet_deep.append(('中原', '上海', CENTANET_DISTRICT_LABELS['qita'], '普通',
                              centanet_page('qita', page), 'centanet'))
    centanet_deep.append(('中原', '上海', CENTANET_DISTRICT_LABELS['qita'], '普通',
                          centanet_page('qita', CENTANET_DECLARED_LAST['qita']), 'centanet'))
    for board in CENTANET_BOARDS:
        for page in (2, 3, 4, 5):
            centanet_deep.append(('中原', '上海', board, '普通',
                                  centanet_page(board, page), 'centanet'))
        centanet_deep.append(('中原', '上海', board, '普通',
                              centanet_page(board, CENTANET_BOARD_DECLARED_LAST[board]), 'centanet'))
    for slug in CENTANET_DISTRICT_SLUGS:
        for page in (2, 3):
            centanet_deep.append(('中原', '上海', CENTANET_DISTRICT_LABELS[slug], '别墅',
                                  centanet_villa_page(slug, page), 'centanet'))

    for source, city, district, kind, url, parser in _interleave(lizihang_deep, centanet_deep):
        add(source, city, district, kind, url, parser)

    # F) deep continuation (appended at index 412+): Beijing-first 丽兹行 retries and
    # deeper pages, then 上海 丽兹行 retries, then 中原 上海 villa tails + 普通 mid pages.
    def _f(source, city, district, kind, url, parser):
        return (source, city, district, kind, url, parser)

    lizi_f = []
    LIZIHANG_F_BJ = [
        ('110108', [1, 3]),         # 海淀: retry p1/p3 transient 500
        ('110101', [4]),            # 东城: tail p4 (p5 already read)
        ('110102', [4]),            # 西城: tail p4 (p5 already read)
        ('110105', [6, 7, 8]),      # 朝阳: push past declared p5
        ('110106', [6]),            # 丰台: push past declared p5
        ('110112', [6]),            # 通州: push past declared p5
        ('110115', [4, 5, 6]),      # 大兴: retry p4/p5 + deeper
        ('110107', [3, 4, 5]),      # 石景山: retry p3-p5
        ('110111', [3, 4, 5]),      # 房山: retry p3-p5
        ('110113', [3, 4, 5]),      # 顺义: retry p3-p5
    ]
    for code, pages in LIZIHANG_F_BJ:
        for page in pages:
            lizi_f.append(_f('丽兹行', '北京', LIZIHANG_LABELS['北京'][code], '普通',
                             lizihang_page('北京', code, page), 'lizihang'))

    LIZIHANG_F_SH = [
        ('310101', [4, 5, 6, 7, 8]), ('310104', [4, 5, 6, 7, 8]),
        ('310105', [4, 5, 6, 7, 8]), ('310106', [3, 4, 5, 6, 7, 8]),
        ('310110', [3, 4, 5, 6, 7, 8]), ('310112', [4, 5, 6, 7, 8]),
        ('310113', [4, 5, 6, 7, 8]), ('310115', [4, 5, 6, 7, 8]),
        ('310107', [3, 4, 5]), ('310109', [3, 4, 5]), ('310117', [3, 4, 5]),
        ('310118', [3, 4, 5]),
    ]
    for code, pages in LIZIHANG_F_SH:
        for page in pages:
            lizi_f.append(_f('丽兹行', '上海', LIZIHANG_LABELS['上海'][code], '普通',
                             lizihang_page('上海', code, page), 'lizihang'))

    cent_f = []
    for slug in CENTANET_REAL_DISTRICTS:
        label = CENTANET_DISTRICT_LABELS[slug]
        for page in (4, 5, 6):                      # villa t2 tail past the read p1-p3
            cent_f.append(_f('中原', '上海', label, '别墅',
                             centanet_villa_page(slug, page), 'centanet'))
    for page in (7, 8):                             # 浦东 villa declares 8 pages
        cent_f.append(_f('中原', '上海', CENTANET_DISTRICT_LABELS['pudongxinqu'], '别墅',
                         centanet_villa_page('pudongxinqu', page), 'centanet'))
    for slug in CENTANET_REAL_DISTRICTS:            # 普通 mid pages where tail > p5
        if CENTANET_DECLARED_LAST[slug] <= 6:
            continue
        label = CENTANET_DISTRICT_LABELS[slug]
        for page in (6, 7):
            cent_f.append(_f('中原', '上海', label, '普通',
                             centanet_page(slug, page), 'centanet'))
    for board in CENTANET_BOARDS:                   # board mid pages where tail > p5
        if CENTANET_BOARD_DECLARED_LAST[board] <= 6:
            continue
        for page in (7, 8):
            cent_f.append(_f('中原', '上海', board, '普通',
                             centanet_page(board, page), 'centanet'))
    for page in (6, 7, 8):                          # city-wide tails
        cent_f.append(_f('中原', '上海', '全城', '别墅', centanet_all_villa_page(page),
                         'centanet'))
        cent_f.append(_f('中原', '上海', '全城', '普通', centanet_all_page(page),
                         'centanet'))

    for source, city, district, kind, url, parser in lizi_f + cent_f:
        add(source, city, district, kind, url, parser)
    return targets


PLAN = build_plan()


# ---------------------------------------------------------------------------
# small io helpers (atomic json / checkpoint)
# ---------------------------------------------------------------------------
def _atomic_json(path, obj):
    """Atomic replace so a crash never leaves a half-written artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def read_checkpoint(out_dir):
    return _read_json(Path(out_dir) / 'run_state' / 'checkpoint.json')


def _row_belongs(row, url):
    """A redirect hop belongs to the target whose URL started its chain."""
    if row.get('requested_url') == url:
        return True
    return any((hop or {}).get('url') == url for hop in (row.get('redirect_chain') or []))


def _target_read(target, rows):
    return any(r.get('status') == 200 and _row_belongs(r, target['url']) for r in rows)


def _snapshot_title(path):
    """Recover a page title from a saved body; used for verbatim block findings."""
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        doc = BeautifulSoup(raw, 'html.parser')
        return doc.title.get_text(' ', strip=True)[:120] if doc.title else None
    except Exception:
        return None


def _snapshot_challenge(row):
    """Re-verdict a 200 row against its saved body, or None when no snapshot is readable."""
    path = row.get('snapshot_path')
    if not path or not Path(path).exists():
        return None
    body = Path(path).read_bytes()
    doc = BeautifulSoup(body, 'html.parser')
    title = doc.title.get_text(' ', strip=True) if doc.title else ''
    text = doc.get_text(' ', strip=True)
    return bool(sp.is_challenge(title, text, bool(int(row.get('cards_parsed') or 0)),
                                final_url=row.get('final_url') or row.get('requested_url'),
                                redirect_chain=row.get('redirect_chain'), body=body))


def effective_failure_class(row):
    """Correct a 200 challenge/no_cards label from the saved body (the label may be stale)."""
    fc = row.get('failure_class')
    if row.get('status') == 200 and fc in ('challenge', 'no_cards'):
        verdict = _snapshot_challenge(row)
        if verdict is None:
            return fc
        if verdict:
            return 'challenge'
        return 'no_cards' if not int(row.get('cards_parsed') or 0) else None
    return fc


def _corrected_rows(trows):
    return [dict(r, failure_class=effective_failure_class(r)) for r in trows]


def _observed_id(obs):
    return obs.get('source_id') or obs.get('id')


def _snapshot_pages(trows):
    """Declared page count for a set of snapshots: 中原 共 N 个小区, 房天下 共 N 页, nav."""
    best = None
    for row in trows:
        path = row.get('snapshot_path')
        if not path:
            continue
        snapshot = Path(path)
        if not snapshot.exists():
            continue
        try:
            raw = snapshot.read_bytes()
        except OSError:
            continue
        text = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
        declared = sp.PAGE_DECLARED_RE.search(text)
        if declared:
            count = int(declared.group(1).replace(',', ''))
            best = max(best or 0, min(-(-count // CENTANET_PER_PAGE), NAV_CAP))
        pages = re.search(r'共\s*(\d+)\s*页', text)
        if pages:
            best = max(best or 0, min(int(pages.group(1)), NAV_CAP))
        nav = sp.parse_page_nav(raw, str(snapshot))
        if nav.get('max_page'):
            best = max(best or 0, min(nav['max_page'], NAV_CAP))
    return best


def _snapshot_declared_count(trows):
    for row in trows:
        path = row.get('snapshot_path')
        if not path:
            continue
        snapshot = Path(path)
        if not snapshot.exists():
            continue
        text = BeautifulSoup(snapshot.read_bytes(), 'html.parser').get_text(' ', strip=True)
        match = sp.PAGE_DECLARED_RE.search(text)
        if match:
            return int(match.group(1).replace(',', ''))
    return None


def _merge_observations(out_dir, cards):
    path = Path(out_dir) / 'observations.jsonl'
    write_jsonl(path, merge_observations(read_jsonl(path), cards))


def _fang_catalog(body, city, url, fetched_at, raw):
    """Import fang_parser lazily so an offline run never hard-fails on its deps."""
    import fang_parser
    return fang_parser.catalog(body, city, url, fetched_at, raw)


def _cards_for(target, body, row, url):
    """Parse a 200 body with the target's fixed parser; unsupported input parses empty."""
    kind = target.get('parser')
    source = target['source']
    city = target['city']
    if kind == 'fang':
        try:
            result = _fang_catalog(body, city, url, row['requested_at'],
                                   {'path': row.get('snapshot_path'),
                                    'sha256': row.get('sha256')})
            return list(result.get('profiles') or [])
        except Exception:
            return []
    parser = PARSERS.get(kind)
    if parser is None:
        return []
    try:
        return list(parser(body, url, city, source, row['requested_at'], 'http',
                           row.get('snapshot_path')))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def _snapshot_evidence(row):
    """challenge_evidence hits from a row's saved body; [] when no readable snapshot."""
    path = row.get('snapshot_path')
    if not path or not Path(path).exists():
        return []
    body = Path(path).read_bytes()
    doc = BeautifulSoup(body, 'html.parser')
    title = doc.title.get_text(' ', strip=True) if doc.title else ''
    text = doc.get_text(' ', strip=True)
    return sp.challenge_evidence(
        title, text, bool(int(row.get('cards_parsed') or 0)),
        row.get('final_url') or row.get('requested_url'),
        row.get('redirect_chain'), body)


def _block_marker(trows):
    """The row that marks a cell blocked: a block class, else a 202/transport row."""
    marker = next((r for r in trows if r.get('failure_class') in BLOCK_CLASSES), None)
    if marker is None:
        marker = next((r for r in trows
                       if r.get('failure_class') in ('http_202', 'transport_error')), None)
    return marker


def _block_evidence(trows):
    """challenge_evidence hits from a blocked cell's marker snapshot; [] when not blocked."""
    marker = _block_marker(trows)
    return _snapshot_evidence(marker) if marker is not None else []


def _cell_blocked(trows):
    marker = _block_marker(trows)
    if marker is None:
        return {'blocked': False, 'class': None, 'status': None, 'retry_after': None,
                'title': None}
    return {'blocked': True, 'class': marker.get('failure_class'),
            'status': marker.get('status'), 'retry_after': marker.get('retry_after'),
            'title': _snapshot_title(marker.get('snapshot_path'))}


def _cell_dict(source, city, district, catalog_kind, stage, url, status, reason,
               trows, declared_pages, pages_read, blocked):
    return {
        'source': source, 'city': city, 'district': district, 'catalog_kind': catalog_kind,
        'stage': stage, 'url': url, 'key': [source, city, district, catalog_kind],
        'status': status, 'reason': reason, 'requests': len(trows),
        'cards_parsed': sum(int(r.get('cards_parsed') or 0) for r in trows),
        'declared_pages': declared_pages, 'pages_read': pages_read,
        'snapshot_paths': sorted({r.get('snapshot_path') for r in trows
                                  if r.get('snapshot_path')}),
        'failure_classes': sorted({r.get('failure_class') for r in trows
                                   if r.get('failure_class')}),
        'blocked': blocked,
        'block_evidence': _block_evidence(trows),
    }


def _derive_status(trows, declared_pages, pages_read, city, scope):
    """fetch_status plus a 202/transport override so a shell page is never '空目录'."""
    report = fetch_status(trows, total_pages_expected=declared_pages, pages_read=pages_read,
                          city=city, scope=scope)
    state, reason = report['state'], report['reason']
    failure_classes = {r.get('failure_class') for r in trows}
    if 'http_202' in failure_classes:
        state, reason = '阻断', 'http_202'
    elif (state != '成功'
          and any(r.get('failure_class') == 'transport_error' and r.get('status') is None
                  for r in trows)):
        # a 500 probe error is recorded, not a block: never downgrade a fully-read (成功) cell
        state, reason = '阻断', 'transport_error'
    elif state == '截断' and sum(int(r.get('cards_parsed') or 0) for r in trows) == 0:
        # A normal page declaring more pages but parsing 0 cards is an empty directory.
        state, reason = '空目录', '解析0条'
    return state, reason


def _derive_entry_cells(entry_rows):
    """Re-derive entry cells from entry request rows via source_entry_probe's own logic."""
    try:
        import source_entry_probe as sep
        cells = []
        for target in sep.PLAN:
            trows = [r for r in entry_rows if _row_belongs(r, target['url'])]
            status, reason, blocked = sep._cell_state(target, trows, len(sep.PLAN), None,
                                                      [], {})
            declared = _snapshot_pages(trows)
            pages_read = 1 if _target_read(target, trows) else 0
            cells.append(_cell_dict(target['source'], target['city'], target['district'],
                                    target['catalog_kind'], ENTRY_STAGE, target['url'],
                                    status, reason, trows, declared, pages_read, blocked))
        return cells
    except Exception:
        return []


def _entry_cells(rows, existing_cells):
    """Entry-stage cells: re-derived from entry request rows, else preserved as-is."""
    entry_rows = [r for r in rows if r.get('stage') == ENTRY_STAGE]
    if entry_rows:
        derived = _derive_entry_cells(entry_rows)
        if derived:
            return derived
    preserved = []
    for cell in existing_cells:
        if cell.get('stage') != ENTRY_STAGE:
            continue
        preserved.append(_cell_dict(
            cell.get('source'), cell.get('city'), cell.get('district'),
            cell.get('catalog_kind'), ENTRY_STAGE, cell.get('url'), cell.get('status'),
            cell.get('reason', ''), [], cell.get('declared_pages'),
            cell.get('pages_read') or 0, cell.get('blocked')
            or {'blocked': False, 'class': None, 'status': None, 'retry_after': None,
                'title': None}))
    return preserved


def _stage_cells(plan, rows, corrected=True):
    groups = {}
    for target in plan:
        key = (target['source'], target['city'], target['district'], target['catalog_kind'])
        groups.setdefault(key, []).append(target)
    stage_rows = [r for r in rows if r.get('stage') == STAGE]
    cells = []
    for (source, city, district, catalog_kind), targets in groups.items():
        trows = [r for r in stage_rows if any(_row_belongs(r, t['url']) for t in targets)]
        if corrected:
            trows = _corrected_rows(trows)
        declared = _snapshot_pages(trows)
        pages_read = sum(1 for t in targets if _target_read(t, trows))
        state, reason = _derive_status(trows, declared, pages_read, city, catalog_kind)
        cells.append(_cell_dict(source, city, district, catalog_kind, STAGE,
                                targets[0]['url'], state, reason, trows, declared,
                                pages_read, _cell_blocked(trows)))
    return cells


def build_coverage(out_dir, *, plan=None, corrected=True):
    """Per-cell coverage over entry cells (preserved/re-derived) plus this stage's cells."""
    plan = list(plan) if plan is not None else PLAN
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    existing = _read_json(out_dir / 'coverage.json') or {}
    cells = _entry_cells(rows, existing.get('cells') or []) + \
        _stage_cells(plan, rows, corrected)
    stages = [ENTRY_STAGE, STAGE]
    summary = {state: sum(1 for c in cells if c['status'] == state) for state in FETCH_STATES}
    summary_by_stage = {}
    for stage in stages:
        summary_by_stage[stage] = {
            state: sum(1 for c in cells if c['stage'] == stage and c['status'] == state)
            for state in FETCH_STATES}
    return {'schema': COVERAGE_SCHEMA, 'generated_at': now_iso(), 'stages': stages,
            'cells': cells, 'summary': summary, 'summary_by_stage': summary_by_stage}


def _filter_coverage(coverage, stage):
    cells = [c for c in coverage['cells'] if c['stage'] == stage]
    summary = {state: sum(1 for c in cells if c['status'] == state) for state in FETCH_STATES}
    return {'schema': coverage['schema'], 'generated_at': coverage['generated_at'],
            'stages': [stage], 'cells': cells, 'summary': summary,
            'summary_by_stage': {stage: summary}}


def rebuild_coverage(out_dir):
    """Offline rebuild: corrected coverage written out, uncorrected summary kept as 'old'."""
    out_dir = Path(out_dir)
    coverage = build_coverage(out_dir, corrected=True)
    _atomic_json(out_dir / 'coverage.json', coverage)
    _atomic_json(out_dir / 'coverage_catalog_expand.json', _filter_coverage(coverage, STAGE))
    old_summary = build_coverage(out_dir, corrected=False)['summary']
    return {'old': old_summary, 'new': coverage['summary'], 'cells': len(coverage['cells'])}


def build_block_findings(rows):
    """Verbatim hard-block findings (status/url/retry_after/title snippet) from request rows."""
    findings = []
    for row in rows:
        failure_class = row.get('failure_class')
        if failure_class not in BLOCK_CLASSES:
            continue
        findings.append({
            'source': row.get('source'), 'city': row.get('city'),
            'url': row.get('final_url') or row.get('requested_url'),
            'status': row.get('status'), 'failure_class': failure_class,
            'retry_after': row.get('retry_after'),
            'title': _snapshot_title(row.get('snapshot_path')),
            'snapshot_path': row.get('snapshot_path'), 'sha256': row.get('sha256'),
            'requested_at': row.get('requested_at'),
        })
    return findings


BLOCK_REPORT_SCHEMA = 'source_block_report/v1'


def _block_report_classification(row):
    """The classification a request row advertises, before snapshot correction."""
    fc = row.get('failure_class')
    if fc in ('challenge', 'blocked_403', 'blocked_429', 'http_202'):
        return fc
    if row.get('status') is not None and 300 <= row['status'] < 400 and \
            sp.challenge_url_markers(row.get('final_url'), row.get('redirect_chain')):
        return 'redirect_to_challenge'
    if fc:
        return fc
    return 'no_cards'


def _block_report_corrected(row):
    """The corrected classification: snapshot evidence overrides a stale challenge/no_cards."""
    if effective_failure_class(row) == 'challenge' or \
            _block_report_classification(row) == 'redirect_to_challenge':
        return 'challenge'
    fc = row.get('failure_class')
    if fc == 'http_202':
        return '阻断'
    if fc == 'challenge' and row.get('status') == 200:
        return '空目录'
    return _block_report_classification(row)


def build_block_report(out_dir):
    """Offline report of block-classified rows with snapshot-corrected labels."""
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    entries = []
    for row in rows:
        classification = _block_report_classification(row)
        corrected = _block_report_corrected(row)
        if classification not in ('challenge', 'blocked_403', 'blocked_429', 'http_202',
                                  'redirect_to_challenge') and corrected != 'challenge':
            continue
        evidence = []
        if row.get('status') == 200 and row.get('snapshot_path') \
                and Path(row['snapshot_path']).exists():
            body = Path(row['snapshot_path']).read_bytes()
            doc = BeautifulSoup(body, 'html.parser')
            title = doc.title.get_text(' ', strip=True) if doc.title else ''
            text = doc.get_text(' ', strip=True)
            evidence = sp.challenge_evidence(
                title, text, bool(int(row.get('cards_parsed') or 0)),
                row.get('final_url') or row.get('requested_url'),
                row.get('redirect_chain'), body)
        entries.append({
            'source': row.get('source'), 'city': row.get('city'),
            'url': row.get('final_url') or row.get('requested_url'),
            'requested_url': row.get('requested_url'), 'final_url': row.get('final_url'),
            'redirect_chain': list(row.get('redirect_chain') or []),
            'status': row.get('status'), 'bytes': row.get('bytes'),
            'sha256': row.get('sha256'), 'snapshot_path': row.get('snapshot_path'),
            'classification': classification, 'corrected_classification': corrected,
            'evidence': evidence,
        })
    counts = Counter(e['corrected_classification'] for e in entries)
    corrections = [e for e in entries if e['classification'] != e['corrected_classification']]
    if rows:
        cell_status_totals = {
            'old': build_coverage(out_dir, corrected=False)['summary'],
            'new': build_coverage(out_dir, corrected=True)['summary'],
        }
    else:
        cell_status_totals = {}
    payload = {
        'schema': BLOCK_REPORT_SCHEMA, 'generated_at': now_iso(), 'trial_dir': str(out_dir),
        'entry_count': len(entries), 'counts_by_classification': dict(counts),
        'cell_status_totals': cell_status_totals,
        'corrections': corrections, 'entries': entries,
    }
    _atomic_json(out_dir / 'block_report.json', payload)
    return payload


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _centanet_pagination(plan, chunk_rows, observations):
    groups = {}
    for target in plan:
        if target['source'] != '中原':
            continue
        groups.setdefault(centanet_partition(target['url']), []).append(target)
    out = []
    for partition, targets in groups.items():
        trows = [r for r in chunk_rows if any(_row_belongs(r, t['url']) for t in targets)]
        pages_read = sum(1 for t in targets if _target_read(t, trows))
        paths = {r.get('snapshot_path') for r in trows if r.get('snapshot_path')}
        ids = set()
        for obs in observations:
            if obs.get('snapshot_path') in paths and obs.get('list_url') \
                    and centanet_partition(obs['list_url']) == partition:
                sid = _observed_id(obs)
                if sid:
                    ids.add(sid)
        out.append({'url': partition, 'declared_count': _snapshot_declared_count(trows),
                    'declared_pages': _snapshot_pages(trows), 'nav_cap': NAV_CAP,
                    'pages_read': pages_read, 'unique_ids': len(ids)})
    return out


def _fang_pagination(plan, chunk_rows):
    groups = {}
    for target in plan:
        if target['source'] != '房天下':
            continue
        groups.setdefault(fang_partition(target['url']), []).append(target)
    out = []
    for partition, targets in groups.items():
        trows = [r for r in chunk_rows if any(_row_belongs(r, t['url']) for t in targets)]
        out.append({'url': partition, 'declared_pages': _snapshot_pages(trows)})
    return out


def _hash_verification(chunk_rows):
    checked = ok = 0
    mismatched = []
    for row in chunk_rows:
        path = row.get('snapshot_path')
        if not path:
            continue
        checked += 1
        snapshot = Path(path)
        digest = row.get('sha256')
        if snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest:
            ok += 1
        else:
            mismatched.append({'request_id': row.get('request_id'),
                               'snapshot_path': path, 'expected': digest})
    return {'checked': checked, 'ok': ok, 'mismatched': mismatched}


def _build_report(out_dir, *, coverage, stop_reason, blocked_sources, suspended_sources,
                  signals, store, raw_dir, checkpoint_path, plan, next_index, clock,
                  chunk_id, pre_ids, chunk_ids):
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    chunk_rows = [r for r in rows if r.get('chunk_id') == chunk_id]
    observations = read_jsonl(out_dir / 'observations.jsonl')
    network_rows = [r for r in chunk_rows
                    if r.get('network_attempt') and r.get('request_id') is not None]
    per_source = {}
    for row in network_rows:
        per_source[row.get('source')] = per_source.get(row.get('source'), 0) + 1
    store_report = store.report(now=clock())
    today = datetime.fromtimestamp(clock(), timezone.utc).strftime('%Y-%m-%d')
    stage_cells = [c for c in coverage['cells'] if c['stage'] == STAGE]
    cells_touched = [
        {'source': c['source'], 'city': c['city'], 'district': c['district'],
         'catalog_kind': c['catalog_kind'], 'stage': c['stage'], 'status': c['status'],
         'reason': c['reason'], 'requests': c['requests'],
         'cards_parsed': c['cards_parsed'], 'declared_pages': c['declared_pages'],
         'pages_read': c['pages_read']}
        for c in stage_cells if c['requests'] > 0]
    distinct_source_ids = {}
    for source in sorted(set(pre_ids) | set(chunk_ids)):
        ids = chunk_ids.get(source, set())
        previous = pre_ids.get(source, set())
        distinct_source_ids[source] = {
            'distinct': len(ids), 'new_vs_pre_chunk': len(ids - previous),
            'already_seen': len(ids & previous)}
    gaps = [{'source': c['source'], 'city': c['city'], 'district': c['district'],
             'catalog_kind': c['catalog_kind'], 'status': c['status'], 'reason': c['reason']}
            for c in stage_cells
            if c['status'] in ('未测', '截断', '阻断', '预算未完成')]
    cursor = None
    if next_index < len(plan):
        cursor = {'source': plan[next_index]['source'], 'url': plan[next_index]['url']}
    return {
        'schema': REPORT_SCHEMA, 'generated_at': now_iso(), 'stage': STAGE,
        'chunk_id': chunk_id, 'stop_reason': stop_reason,
        'requests_used_this_chunk': {'total': len(network_rows), 'per_source': per_source},
        'requests_used_cumulative': {
            'stage': store_report['stages'].get(STAGE, {'used': 0, 'cap': 0, 'remaining': 0}),
            'trial': store_report['trial']},
        'cells_touched': cells_touched,
        'distinct_source_ids': distinct_source_ids,
        'pagination': _centanet_pagination(plan, chunk_rows, observations),
        'fang_pagination': _fang_pagination(plan, chunk_rows),
        'raw_snapshot_count': len(list(Path(raw_dir).glob('*.html'))),
        'hash_verification': _hash_verification(chunk_rows),
        'block_findings': build_block_findings(chunk_rows),
        'remaining_budget': {'per_stage': store_report['stages'],
                             'per_platform_today': store_report['platforms_daily'].get(today, {}),
                             'combined_today': store_report['combined_daily'].get(today, {})},
        'checkpoint_path': str(checkpoint_path),
        'next_cursor': cursor,
        'coverage_gaps': gaps,
        'suspended_sources': dict(suspended_sources),
        'blocked_sources': list(blocked_sources),
        'signals': signals,
    }


# ---------------------------------------------------------------------------
# stage runner
# ---------------------------------------------------------------------------
def run(out_dir, *, resume=False, transport=None, clock=None,
        max_requests=DEFAULT_MAX_REQUESTS, plan=None, store=None):
    """Execute the 目录扩大试爬 stage against fixed PLAN targets.

    transport/clock are injectable so tests exercise the loop with no network and no real
    sleep. Every real attempt is appended and checkpointed immediately. Returns 0 when the
    chunk ended cleanly (stop_reason None/'chunk_bound'/'plan_exhausted'), else 2.
    """
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or _now_epoch
    transport = transport or single_request
    plan = list(plan) if plan is not None else list(PLAN)
    if store is None:
        store = BudgetStore(run_state_dir, trial_total=TRIAL_TOTAL_CAP,
                            platform_overrides=load_platform_policy_overrides(), clock=clock)
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        print(json.dumps({'stage': STAGE, 'error': 'budget_lock', 'detail': str(exc)},
                         ensure_ascii=False))
        return 2

    checkpoint_path = run_state_dir / 'checkpoint.json'
    existing = _read_json(checkpoint_path)
    if existing and existing.get('stage') not in (None, STAGE):
        archive = run_state_dir / f"checkpoint_{existing['stage']}.json"
        if not archive.exists():
            _atomic_json(archive, existing)

    next_index = 0
    last_request_id = None
    cursor = None
    blocked_sources = []
    suspended_sources = {}
    stop_reason = None
    chunk_id = now_iso()
    chunk_requests = 0
    signals = {}
    if resume and existing and existing.get('stage') == STAGE:
        next_index = int(existing.get('next_index') or 0)
        last_request_id = existing.get('last_request_id')
        cursor = existing.get('cursor')
        blocked_sources = list(existing.get('blocked_sources') or [])
        suspended_sources = dict(existing.get('suspended_sources') or {})

    pre_ids = {}
    for obs in read_jsonl(out_dir / 'observations.jsonl'):
        sid = _observed_id(obs)
        if sid:
            pre_ids.setdefault(obs.get('source'), set()).add(sid)
    chunk_ids = {}
    seen_hosts = set()
    for row in read_jsonl(out_dir / 'requests.jsonl'):
        host = urlsplit(row.get('requested_url') or '').netloc
        if host:
            seen_hosts.add(host)

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA

    def checkpoint_now():
        _atomic_json(checkpoint_path, {
            'schema': CHECKPOINT_SCHEMA, 'stage': STAGE, 'updated_at': now_iso(),
            'plan_total': len(plan), 'next_index': next_index,
            'completed': [t['url'] for t in plan[:next_index]],
            'last_request_id': last_request_id, 'cursor': cursor,
            'counters': store.report(now=clock()),
            'blocked_sources': list(blocked_sources),
            'suspended_sources': dict(suspended_sources),
            'chunk_id': chunk_id, 'chunk_requests': chunk_requests,
            'stop_reason': stop_reason,
        })

    def base_row(target, url, request_id):
        source = target['source']
        return {
            'source': source, 'city': target['city'],
            'combo': combo_of(source, target['city']), 'requested_url': url,
            'final_url': url, 'requested_at': now_iso(), 'status': None,
            'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0,
            'body_state': None, 'request_id': request_id, 'stage': STAGE,
            'platform': source, 'network_attempt': True, 'chunk_id': chunk_id,
        }

    def emit(row):
        append_jsonl(out_dir / 'requests.jsonl', [row])
        if row.get('snapshot_path') and (row.get('failure_class')
                                         or row.get('status') != 200):
            copy_to_failed(raw_dir, row['snapshot_path'])
        checkpoint_now()

    def classify_success(target, body, row, url):
        cards = _cards_for(target, body, row, url)
        row['cards_parsed'] = len(cards)
        doc = BeautifulSoup(body, 'html.parser')
        title = doc.title.get_text(' ', strip=True) if doc.title else ''
        text = doc.get_text(' ', strip=True)
        if is_challenge(title, text, bool(cards), final_url=row.get('final_url') or url,
                        redirect_chain=row.get('redirect_chain'), body=body):
            row['failure_class'] = 'challenge'
        elif not cards:
            row['failure_class'] = 'no_cards'
        else:
            _merge_observations(out_dir, cards)
            for obs in cards:
                sid = _observed_id(obs)
                if sid:
                    chunk_ids.setdefault(obs.get('source') or target['source'], set()).add(sid)
            merge_signals(signals, target['source'], body,
                          snapshot_path=row.get('snapshot_path'), url=url,
                          city=target['city'])

    def reserve_attempt(target, url):
        """Sleep through interval gates; on any refusal emit a row and report the reason."""
        source = target['source']
        combo = combo_of(source, target['city'])
        while True:
            res = store.reserve(source, STAGE, combo, url, now=clock())
            if res['allowed']:
                return res['request_id']
            if res['reason'] == 'interval_not_elapsed':
                _sleep(res['wait_seconds'])
                continue
            store.record_exhausted(platform=source, stage=STAGE, combo=combo,
                                   requested_url=url, reason=res['reason'], now=clock())
            row = base_row(target, url, None)
            row['failure_class'] = 'budget_exhausted'
            row['network_attempt'] = False
            row['blocking_reason'] = res['reason']
            emit(row)
            return ('refusal', res['reason'])

    def fetch_hop(target, url, chain, request_id):
        row = base_row(target, url, request_id)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, TIMEOUT)
        row['elapsed_ms'] = outcome.get('elapsed_ms')
        response = outcome.get('response')
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome.get('error_type')
            row['body_state'] = 'no_body'
            emit(row)
            return row

        body = response.content
        status = response.status_code
        headers = response.headers
        row['status'] = status
        row['content_type'] = headers.get('Content-Type')
        row['retry_after'] = headers.get('Retry-After')
        row['bytes'] = len(body)
        row['sha256'] = hashlib.sha256(body).hexdigest()
        row['body_state'] = 'retained' if body else 'no_body'
        if body:
            row['snapshot_path'] = str(save_raw(
                raw_dir, combo_of(target['source'], target['city']), url, body,
                row['sha256']))
        if status == 403:
            row['failure_class'] = 'blocked_403'
        elif status == 429:
            row['failure_class'] = 'blocked_429'
        elif status == 202:
            row['failure_class'] = 'http_202'
        elif 300 <= status < 400 and headers.get('Location'):
            location = headers['Location']
            next_url = urljoin(url, location)
            row['final_url'] = next_url
            row['redirect_chain'] = list(chain) + [{'url': url, 'status': status,
                                                    'location': location, 'next': next_url}]
            if sp.challenge_url_markers(next_url, row['redirect_chain']):
                # Redirect into a known CAPTCHA host is a block, not just a hop.
                row['failure_class'] = 'challenge'
            elif len(chain) >= MAX_REDIRECT_HOPS:
                row['failure_class'] = 'transport_error'
                row['error_type'] = 'TooManyRedirects'
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        else:
            classify_success(target, body, row, url)
        emit(row)
        return row

    def process_target(target):
        """Follow one target's manual redirects; each hop reserves and logs one request."""
        nonlocal last_request_id, stop_reason, chunk_requests
        current = target['url']
        chain = []
        while True:
            outcome = reserve_attempt(target, current)
            if isinstance(outcome, tuple):
                return 'refusal', outcome[1], False
            last_request_id = outcome
            host = urlsplit(current).netloc
            host_was_seen = host in seen_hosts
            row = fetch_hop(target, current, chain, outcome)
            seen_hosts.add(host)
            chunk_requests += 1
            failure = row['failure_class']
            status = row['status']
            if failure in BLOCK_CLASSES:
                stop_reason = failure
                if target['source'] not in blocked_sources:
                    blocked_sources.append(target['source'])
                store.pause_platform(target['source'], failure, now=clock())
                suspended_sources[target['source']] = f'platform_paused:{failure}'
                return 'stop', failure, True
            if failure == 'http_202':
                if host_was_seen:
                    return 'ok', 'http_202', True
                stop_reason = 'http_202'
                return 'stop', 'http_202', True
            if failure == 'transport_error':
                # Record the 500 and continue; one transport error must not halt the chunk.
                return 'ok', 'transport_error', True
            is_redirect = (failure is None and status is not None and 300 <= status < 400
                           and row['redirect_chain']
                           and row['redirect_chain'][-1]['url'] == current)
            if is_redirect and chunk_requests < max_requests:
                chain = row['redirect_chain']
                current = chain[-1]['next']
                continue
            if chunk_requests >= max_requests:
                stop_reason = 'chunk_bound'
                return 'ok', 'chunk_bound', (not is_redirect)
            return 'ok', None, True

    try:
        for index, target in enumerate(plan):
            if index < next_index:
                continue
            if stop_reason is not None:
                break
            source = target['source']
            if source in blocked_sources or source in suspended_sources:
                next_index = index + 1
                checkpoint_now()
                continue
            cursor = {'source': source, 'url': target['url']}
            next_index = index
            checkpoint_now()
            outcome, reason, complete = process_target(target)
            if outcome == 'refusal' and reason in SUSPEND_REASONS:
                suspended_sources[source] = reason
                next_index = index + 1
                while next_index < len(plan) and plan[next_index]['source'] == source:
                    next_index += 1
                checkpoint_now()
                continue
            if outcome == 'refusal':
                stop_reason = reason
            next_index = index + 1 if complete else index
            checkpoint_now()
            if stop_reason is not None:
                break

        if stop_reason is None and next_index >= len(plan):
            stop_reason = 'plan_exhausted'
            checkpoint_now()

        coverage = build_coverage(out_dir, plan=plan)
        _atomic_json(out_dir / 'coverage.json', coverage)
        _atomic_json(out_dir / 'coverage_catalog_expand.json',
                     _filter_coverage(coverage, STAGE))
        report = _build_report(out_dir, coverage=coverage, stop_reason=stop_reason,
                               blocked_sources=blocked_sources,
                               suspended_sources=suspended_sources, signals=signals,
                               store=store, raw_dir=raw_dir,
                               checkpoint_path=checkpoint_path, plan=plan,
                               next_index=next_index, clock=clock, chunk_id=chunk_id,
                               pre_ids=pre_ids, chunk_ids=chunk_ids)
        _atomic_json(out_dir / 'catalog_expand_report.json', report)
    finally:
        store.release_lock()

    print(json.dumps({'stage': STAGE, 'chunk_id': chunk_id, 'stop_reason': stop_reason,
                      'requests_used_this_chunk': report['requests_used_this_chunk'],
                      'coverage': coverage['summary'],
                      'next_cursor': report['next_cursor']}, ensure_ascii=False))
    return 0 if stop_reason in (None, 'chunk_bound', 'plan_exhausted') else 2


# ---------------------------------------------------------------------------
# read-only commands
# ---------------------------------------------------------------------------
def _store_for(out_dir, clock):
    return BudgetStore(Path(out_dir) / 'run_state', trial_total=TRIAL_TOTAL_CAP,
                       platform_overrides=load_platform_policy_overrides(), clock=clock)


def plan_payload(out_dir, *, clock=None):
    """PLAN + persisted budget + checkpoint; read-only, ZERO network."""
    clock = clock or _now_epoch
    per_source = {}
    for target in PLAN:
        per_source[target['source']] = per_source.get(target['source'], 0) + 1
    return {'stage': STAGE, 'plan_total': len(PLAN), 'per_source': per_source,
            'first': PLAN[:3], 'last': PLAN[-3:],
            'budget': _store_for(out_dir, clock).report(now=clock()),
            'checkpoint': read_checkpoint(out_dir)}


def status_payload(out_dir, *, clock=None):
    clock = clock or _now_epoch
    return {'stage': STAGE, 'checkpoint': read_checkpoint(out_dir),
            'budget': _store_for(out_dir, clock).report(now=clock())}


def verify(out_dir):
    """Offline replay check: every snapshot still hashes to the row's sha256."""
    out_dir = Path(out_dir)
    rows = read_jsonl(out_dir / 'requests.jsonl')
    checked = ok = 0
    mismatches = []
    no_body = 0
    per_source = {}
    for row in rows:
        source = row.get('source')
        per_source[source] = per_source.get(source, 0) + 1
        path = row.get('snapshot_path')
        digest = row.get('sha256')
        if path:
            checked += 1
            snapshot = Path(path)
            if snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest:
                ok += 1
            else:
                mismatches.append({'request_id': row.get('request_id'),
                                   'snapshot_path': path, 'expected': digest})
        if row.get('body_state') == 'no_body':
            no_body += 1
    return {
        'schema': VERIFY_SCHEMA, 'rows': len(rows),
        'network_attempts': sum(1 for r in rows
                                if r.get('network_attempt') and r.get('request_id') is not None),
        'snapshots_checked': checked, 'hash_ok': ok, 'hash_mismatches': mismatches,
        'no_body_rows': no_body,
        'raw_html_count': len(list((out_dir / 'raw').glob('*.html'))),
        'per_source': per_source,
    }


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def _build_cli():
    parser = argparse.ArgumentParser(
        description='目录扩大试爬 stage: bounded, budget-locked catalog expansion trial.')
    parser.add_argument('command', nargs='?', default='run',
                        choices=['run', 'status', 'plan', 'verify', 'block-report', 'coverage'],
                        help="'run' (default, real; --resume), 'status', 'plan', 'verify', "
                             "'block-report' or 'coverage' (offline rebuild)")
    parser.add_argument('--out', default=DEFAULT_OUT,
                        help=f'experiment directory (default {DEFAULT_OUT})')
    parser.add_argument('--resume', action='store_true',
                        help='resume from checkpoint next_index')
    parser.add_argument('--dry-run', action='store_true',
                        help='alias for the plan command: zero network')
    parser.add_argument('--max-requests', type=int, default=DEFAULT_MAX_REQUESTS,
                        help='per-chunk network request cap (default 200)')
    return parser


def main(argv=None):
    args = _build_cli().parse_args(argv)
    out_dir = Path(args.out)
    if args.command == 'status':
        print(json.dumps(status_payload(out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'plan' or args.dry_run:
        print(json.dumps(plan_payload(out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'verify':
        print(json.dumps(verify(out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'block-report':
        print(json.dumps(build_block_report(out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'coverage':
        print(json.dumps(rebuild_coverage(out_dir), ensure_ascii=False, indent=2))
        return 0
    return run(out_dir, resume=args.resume, max_requests=args.max_requests)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
