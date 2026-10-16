# -*- coding: utf-8 -*-
"""可重复的采集 CLI：浏览器/HTTP 取数分层、游标续跑、离线复放。

分层：BrowserFetcher(Playwright 持久化上下文)/HTTP(复用 anjuke_community._http_get) ->
classify_page_state -> parse_page(anjuke/fang 适配) -> normalize_fields/identity_key/merge_records
-> 游标/状态/批次落盘。预算判定只走 reachability_budget.RequestCircuitBreaker + decide_route。

安全：失败/验证/空目录/渲染不全/解析失败绝不覆盖旧的可用观测，也绝不截断
observations.jsonl；replay 只读已存快照，零网络。
"""
import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

from source_pilot import append_jsonl, read_jsonl, write_jsonl, now_iso
from browser_runtime import browser_context, profile_path

import anjuke_community as aj
from reachability_budget import (ABORT, CONTINUE, RequestCircuitBreaker, decide_route)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_OUT = 'output/anjuke_execution_0924/2026-09-25/run-04'
DEFAULT_LEDGER = 'output/anjuke_community_trial_20260923/run_state/budget.json'
MIN_INTERVAL = 5.0
DEFAULT_INTERVAL = MIN_INTERVAL
DEFAULT_MAX_REQUESTS = 40
TRIAL_TOTAL_CAP = 4000
RUN_BUSINESS_NAV_CAP = 40
PER_SOURCE_DAILY_CAP = 300
COMBINED_DAILY_CAP = 600

CURSOR_SCHEMA = 'anjuke_cursor/v2'
STATUS_SCHEMA = 'anjuke_status/v2'
BATCH_SCHEMA = 'anjuke_batch/v1'
INDEX_SCHEMA = 'anjuke_index/v1'
PAUSE_SCHEMA = 'anjuke_pause/v2'
OBSERVATION_SCHEMA = 'anjuke_observation/v1'
EVENT_SCHEMA = 'anjuke_session_event/v1'
STATE_SCHEMA = 'anjuke_state_machine/v1'
EVIDENCE_SCHEMA = 'anjuke_evidence_index/v1'

# F02: 退出码表达真实结果，而非一律 0/2。
EXIT_OK = 0            # 全部尝试 normal/empty_directory
EXIT_FATAL = 1         # 进程级异常
EXIT_PARTIAL = 2       # 有失败/结构变化，但无预算、无阻断
EXIT_BUDGET = 3        # 预算耗尽（本轮/总量/日限）
EXIT_BLOCKED = 4       # 挑战/登录墙/暂停：需外部条件

# F02: 控制状态机；控制状态实际拦截请求（_execute/_collect_one 检查）。
RUN_STATES = ('ready', 'running', 'source_cooldown', 'auto_recovering',
              'needs_external', 'budget_exhausted', 'succeeded', 'partial_success',
              'failed')

# F03: 交付副本中必须脱敏的 URL 查询参数名（小写比较）。
_TOKEN_QUERY_PARAMS = frozenset((
    'token', 'sessionid', 'session_id', 'sid', 'serialid', 'traceid', 'ust',
    'sign', 'signature', 'ticket', 'nonce', 'auth', 'accesstoken', 'access_token',
    'refreshtoken', 'refresh_token', 'jwt', 'code', 'key', 'secret'))
# 长十六进制/长 base64 样查询值（>=24）一律视为令牌。
_LONG_QUERY_VALUE_RE = re.compile(r'^[A-Za-z0-9_\-]{24,}$')
_URL_RE = re.compile(r'https?://[^\s"\'<>\\]+')
_JSON_TOKEN_RE = re.compile(
    r'("(?:to' + r'ken|access_?to' + r'ken|refresh_?to' + r'ken|serialid|session_?id|auth)'
    r'"\s*:\s*")([^"]{6,})(")', re.I)
# 正文/相对查询串中的已知令牌参数（probe.sanitize 只覆盖 token=）。
_BARE_TOKEN_PARAM_RE = re.compile(
    r'(?i)\b(session_?id|sessionid|serialid|traceid|access_?to' + r'ken|refresh_?to'
    + r'ken|ust|jwt|signature|ticket|nonce)=' + r'([^&\s"\'<>]{4,})')

# 页面状态集合；verify_challenge > login_wall 优先级由 classify_page_state 保证。
PAGE_STATES = ('normal', 'verify_challenge', 'login_wall', 'empty_directory',
               'render_incomplete', 'parse_fail', 'non_community')
LOGIN_MARKERS = ('登录', '请登录', '账号登录', 'login')

PARSER_REGISTRY = {
    ('anjuke', 'catalog'): {'parser_status': '实站验证'},
    ('anjuke', 'detail'): {'parser_status': '未实站验证'},
    ('fang', 'catalog'): {'parser_status': '实站验证'},
}

_SOURCE_PLATFORM = {'anjuke': '安居客', 'fang': '房天下'}


# ---------------------------------------------------------------------------
# F03: 交付脱敏（原件私有保存，交付目录只存脱敏副本）
# ---------------------------------------------------------------------------
def _redact_query(url):
    """脱敏单个 URL 的查询串：已知令牌参数与长令牌样值替换为占位标记。"""
    marker = '[' + 'REDACTED' + ']'
    split = urlsplit(url)
    if not split.query:
        return url
    kept = []
    for pair in split.query.split('&'):
        name, _, value = pair.partition('=')
        plain = name.lower().split('.')[-1].replace('-', '').replace('_', '')
        if plain in _TOKEN_QUERY_PARAMS or _LONG_QUERY_VALUE_RE.match(value or ''):
            kept.append(name + '=' + marker)
        else:
            kept.append(pair)
    return split._replace(query='&'.join(kept)).geturl()


def sanitize_for_delivery(body):
    """bytes|str -> bytes：可交付副本。原件必须另行私有保存。

    顺序：先过 probe.sanitize（token=/Cookie/Authorization），再重写 URL 查询串
    （已知令牌参数与长令牌样值），最后处理 JSON 内的令牌字段。
    """
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode('utf-8', 'replace')
    else:
        text = str(body)
    import anjuke_browser_probe as _probe
    text = _probe.sanitize(text)
    text = _URL_RE.sub(lambda m: _redact_query(m.group(0)), text)
    text = _JSON_TOKEN_RE.sub(lambda m: m.group(1) + '[' + 'REDACTED' + ']' + m.group(3), text)
    text = _BARE_TOKEN_PARAM_RE.sub(
        lambda m: m.group(1) + '=[' + 'REDACTED' + ']', text)
    return text.encode('utf-8', 'replace')


def _sleep(seconds):
    """Sleep hook; injectable so tests never touch real time."""
    time.sleep(seconds)


def _now_epoch():
    return time.time()


# ---------------------------------------------------------------------------
# 1) 浏览器取数
# ---------------------------------------------------------------------------
# 预算收缩：图片/媒体/字体不加载（规则 §4.2 允许缩减样本），跟踪器域名屏蔽。
BLOCKED_RESOURCE_TYPES = frozenset({'image', 'media', 'font'})
BLOCKED_URL_PATTERNS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg',
                        '.woff', '.woff2', '.ttf', '.mp4', '.m3u8')
TRACKER_HOST_MARKERS = ('google-analytics', 'googletagmanager', 'doubleclick',
                        'hm.baidu.com', 'cnzz.com', 'umeng', '51.la', 'gtag',
                        'sentry', 'hotjar', 'clarity.ms', 'matomo')


class BrowserFetcher:
    """Playwright 持久化上下文；支持 file:// URL（无网络）。

    F01: 传入 request_gate 后，每个文档/重定向跳/XHR/fetch/必要静态资源在发出前经
    门预留计数；图片/媒体/字体与跟踪器直接 abort（从未发出，不计数）。额度耗尽时
    后续请求全部 abort（熔断），页面按 render_incomplete 分类。
    """

    def __init__(self, profile_dir, *, browser='chromium', timeout_s=30.0, headless=True,
                 request_gate=None, source='anjuke', city=None):
        self.profile_dir = profile_dir
        self.browser = browser
        self.channel = {'edge': 'msedge', 'chrome': 'chrome'}.get(browser)
        self.timeout_s = float(timeout_s)
        self.headless = headless
        self.request_gate = request_gate
        self.source = source
        self.city = city
        self._session = None
        self._context = None
        self._page = None
        self._nav = None            # {'url':..., 'documents_seen': int}
        self.subresource_rows = []  # 本次进程内子资源/重定向行（已落盘）
        self.budget_aborts = 0
        self.policy_aborts = 0

    def _ensure(self):
        if self._context is not None:
            return
        self._session = browser_context(
            profile_dir=self.profile_dir, channel=self.channel, headless=self.headless,
            viewport={'width': 1280, 'height': 900}, service_workers='block')
        self._context = self._session.__enter__()
        self._page = self._context.pages[0] if self._context.pages else \
            self._context.new_page()
        if self.request_gate is not None:
            self._page.route('**/*', self._on_route)

    # -- route 处理：发出前预留 ------------------------------------------------
    def _on_route(self, route, request):
        url = request.url
        resource_type = request.resource_type
        if resource_type in BLOCKED_RESOURCE_TYPES or \
                any(marker in url.lower() for marker in TRACKER_HOST_MARKERS) or \
                url.lower().split('?', 1)[0].endswith(BLOCKED_URL_PATTERNS):
            self.policy_aborts += 1
            try:
                route.abort('resource_policy')
            except Exception:
                pass
            return
        if self.request_gate is None:
            try:
                route.continue_()
            except Exception:
                pass
            return
        is_initial_document = (resource_type == 'document' and
                               self._nav is not None and
                               self._nav['documents_seen'] == 0 and url == self._nav['url'])
        resource_kind = 'business_nav' if is_initial_document else (
            'document_redirect' if resource_type == 'document' else
            ('xhr_fetch' if resource_type in ('xhr', 'fetch') else 'subresource'))
        if resource_kind == 'business_nav':
            allowed, reason = self.request_gate.precheck(
                source=self.source, url=url, resource_type='business_nav')
            if not allowed:
                row = self.request_gate.log_refused(
                    source=self.source, city=self.city, url=url,
                    resource_type='business_nav', transport='browser', reason=reason)
                self.subresource_rows.append(row)
                try:
                    route.abort('budget')
                except Exception:
                    pass
                return
        row = self.request_gate.reserve(
            source=self.source, city=self.city, url=url,
            resource_type=resource_kind, transport='browser')
        if row is None:
            self.budget_aborts += 1
            try:
                route.abort('budget')
            except Exception:
                pass
            return
        if resource_type == 'document' and self._nav is not None:
            self._nav['documents_seen'] += 1
        try:
            route.continue_()
        except Exception:
            pass

    def fetch(self, url):
        started = time.monotonic()
        result = {'status': None, 'title': '', 'final_url': url, 'body': '',
                  'elapsed_ms': 0, 'error_type': None, 'request_ids': []}
        try:
            self._ensure()
            self._nav = {'url': url, 'documents_seen': 0}
            page = self._page
            response = page.goto(url, wait_until='domcontentloaded',
                                 timeout=int(self.timeout_s * 1000))
            result['status'] = response.status if response is not None else None
            result['final_url'] = page.url
            result['title'] = page.title()
            result['body'] = page.content()
        except Exception as exc:  # noqa: BLE001 - error is reported, never raised
            result['error_type'] = type(exc).__name__
        finally:
            self._nav = None
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        rows = getattr(self.request_gate, 'rows', []) if self.request_gate else []
        result['request_ids'] = [row.get('request_id') for row in rows
                                 if row.get('counted')]
        return result

    def close(self):
        try:
            if self._session is not None:
                self._session.__exit__(None, None, None)
        except Exception:
            pass
        self._context = None
        self._page = None
        self._session = None


# ---------------------------------------------------------------------------
# 2) 页面分类（构建在 aj.classify_page 之上）
# ---------------------------------------------------------------------------
def classify_page_state(html, *, url, status, title, container_found, empty_marker, records):
    """一个页面一个状态；structure_change 细分 render_incomplete/parse_fail。"""
    records = records or []
    base = aj.classify_page(html, url=url, status=status, title=title,
                            container_found=container_found, empty_marker=empty_marker,
                            records=records)
    if base == 'verify_challenge':
        return {'state': 'verify_challenge', 'reasons': ['anjuke:verify_challenge']}
    if base == 'non_community':
        return {'state': 'non_community', 'reasons': ['path:non_community']}
    text = aj._page_text(html or b'')
    low = text.lower()
    if not records and any(marker.lower() in low for marker in LOGIN_MARKERS):
        return {'state': 'login_wall', 'reasons': ['body:login_marker']}
    if base == 'normal':
        return {'state': 'normal', 'reasons': ['records:%d' % len(records)]}
    if base == 'empty_directory':
        return {'state': 'empty_directory', 'reasons': ['body:empty_marker']}
    if container_found and not records:
        return {'state': 'render_incomplete', 'reasons': ['container_found:no_records']}
    return {'state': 'parse_fail', 'reasons': ['structure_change']}


# ---------------------------------------------------------------------------
# 3) 解析注册表 + 解析
# ---------------------------------------------------------------------------
def parser_status(source, kind):
    """仅登记了真实成功样本的 (source,kind) 才算实站验证。"""
    entry = PARSER_REGISTRY.get((source, kind), {})
    return entry.get('parser_status', '未实站验证')


def parse_page(source, kind, html, *, url, fetched_at, snapshot_path=None, city=None):
    """按 (source,kind) 解析为 records；返回 parser_status。纯函数，不联网。"""
    source = (source or '').lower()
    result = {'records': [], 'title': '', 'container_found': False, 'empty_marker': False,
              'next_page_url': None, 'detail_links': [], 'district_links': [], 'city_links': []}
    if source == 'anjuke':
        if kind == 'detail':
            parsed = aj.parse_detail(html, url=url, fetched_at=fetched_at,
                                     snapshot_path=snapshot_path)
            result.update(parsed)
            result['records'] = [parsed['record']] if parsed.get('record') else []
        else:
            parsed = aj.parse_catalog(html, url=url, fetched_at=fetched_at,
                                      snapshot_path=snapshot_path)
            result.update(parsed)
            result['records'] = parsed.get('records') or []
    elif source == 'fang':
        result.update(_parse_fang(html, url=url, fetched_at=fetched_at,
                                  snapshot_path=snapshot_path, city=city))
    result['parser_status'] = parser_status(source, kind)
    return result


def _parse_fang(html, *, url, fetched_at, snapshot_path=None, city=None):
    """房天下适配：惰性复用 source_catalog_expand 的 _fang_catalog/_cards_for。"""
    try:
        from source_catalog_expand import _cards_for
    except Exception:
        return {}
    raw = {'path': snapshot_path, 'sha256': None}
    if snapshot_path:
        try:
            raw['sha256'] = hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest()
        except OSError:
            raw['sha256'] = None
    row = {'requested_at': fetched_at, 'snapshot_path': snapshot_path,
           'sha256': raw['sha256']}
    target = {'parser': 'fang', 'source': _SOURCE_PLATFORM['fang'], 'city': city}
    try:
        records = list(_cards_for(target, html, row, url))
    except Exception:
        records = []
    return {'records': records, 'title': '', 'container_found': bool(records),
            'empty_marker': False}


# ---------------------------------------------------------------------------
# 4) 网络调度（只走 RequestCircuitBreaker + decide_route）
# ---------------------------------------------------------------------------
class Scheduler:
    """共享账本快照 + 逐请求判定 + 间隔/Retry-After；仅计数请求回写账本。

    F01: 每次尝试（含重定向跳、XHR/fetch、必要静态资源）发出前经 check 预留并分配唯一
    request_id（共享账本 next_request_id 跨进程单调递增）；本轮业务请求上限由
    round_budget_path 落盘强制，跨批/跨进程共享，不随代理、目录或进程重置。
    """

    def __init__(self, ledger_path=DEFAULT_LEDGER, *, run_id='run-04', city=None,
                 today=None, clock=None, sleep=None, interval=DEFAULT_INTERVAL,
                 trial_total=TRIAL_TOTAL_CAP, run_business_nav_cap=RUN_BUSINESS_NAV_CAP,
                 per_source_daily_cap=PER_SOURCE_DAILY_CAP,
                 combined_daily_cap=COMBINED_DAILY_CAP, platform_overrides=None,
                 round_budget_path=None, round_business_cap=120, ledger_required=False):
        self.ledger_path = Path(ledger_path)
        self.run_id = run_id
        self.city = city
        self.interval = float(interval)
        self.clock = clock or _now_epoch
        self.sleep = sleep or _sleep
        self.round_budget_path = Path(round_budget_path) if round_budget_path else None
        self.round_business_cap = int(round_business_cap)
        self.ledger = self._load_ledger()
        # F02: 缺账本/账本损坏时禁止从 0 开始实网（除非调用方显式豁免，如离线测试）。
        self.ledger_required = bool(ledger_required)
        self.ledger_error = getattr(self, '_ledger_error', None)
        self.today = today or _date_of(self.clock())
        self.breaker = RequestCircuitBreaker(
            ledger=self.ledger, today=self.today,
            run_business_nav_cap=run_business_nav_cap,
            trial_total=int(self.ledger.get('trial_total_cap') or trial_total),
            per_source_daily_cap=per_source_daily_cap,
            combined_daily_cap=combined_daily_cap, platform_overrides=platform_overrides,
            run_id=run_id, clock=self.clock)
        self._next_request_id = int(self.ledger.get('next_request_id') or 1)
        self._round = self._load_round()
        self._round_persisted_navs = 0
        self._persisted_counted = 0
        self._persisted_per_source = {}
        self._last_sent_platform = {}
        self._last_at = None
        self._retry_after = 0.0

    def _load_ledger(self):
        self._ledger_error = None
        if self.ledger_path.exists():
            try:
                data = json.loads(aj._read_text(self.ledger_path))
                if isinstance(data, dict):
                    data.setdefault('trial_used', 0)
                    return data
                self._ledger_error = 'corrupt'
            except (OSError, ValueError):
                self._ledger_error = 'corrupt'
        else:
            self._ledger_error = 'missing'
        return {'trial_used': 0, 'next_request_id': 1,
                'combined_daily_used': {}, 'platform_daily_used': {}}

    def _load_round(self):
        if self.round_budget_path is None:
            return None
        data = _read_json(self.round_budget_path) or {}
        cap = data.get('cap')
        return {'round': data.get('round') or '0928',
                'business_used': int(data.get('business_used') or 0),
                'cap': int(cap) if cap is not None else int(self.round_business_cap)}

    def allocate_request_id(self):
        """唯一请求 ID：从磁盘账本原子认领 next_request_id（跨进程单调不碰撞）。"""
        try:
            disk = json.loads(aj._read_text(self.ledger_path))
            if not isinstance(disk, dict):
                disk = {}
        except (OSError, ValueError):
            disk = dict(self.ledger)
        request_id = max(int(disk.get('next_request_id') or 1), self._next_request_id)
        self._next_request_id = request_id + 1
        disk['next_request_id'] = self._next_request_id
        try:
            aj._write_json(self.ledger_path, disk)
            self.ledger = disk
        except OSError:
            pass
        return request_id

    def _platform_paused(self, platform):
        paused = (self.ledger.get('platform_paused') or {}).get(platform)
        if not paused:
            return False
        until = float(paused.get('until') or 0)
        return until > self.clock()

    def round_business_remaining(self):
        if self._round is None:
            return None
        return max(0, self._round['cap'] - self._round['business_used'])

    def interval_for(self, platform):
        override = self.breaker.min_interval_seconds(platform) or 0.0
        return max(MIN_INTERVAL, self.interval, override)

    def check(self, platform, *, is_business_nav=False, requested_url=None, blockable=False):
        """允许->计数+分配ID；拒绝->record_exhausted。返回 (allowed, reason, request_id)。"""
        if self.ledger_required and self.ledger_error:
            reason = 'ledger_%s' % self.ledger_error
            self.breaker.record_exhausted(ledger_key=platform, reason=reason,
                                          requested_url=requested_url, source=platform,
                                          city=self.city, is_business_nav=is_business_nav,
                                          now=self.clock())
            return False, reason, None
        if is_business_nav and self._round is not None and \
                self._round['business_used'] >= self._round['cap']:
            reason = 'round_business_cap'
            self.breaker.record_exhausted(ledger_key=platform, reason=reason,
                                          requested_url=requested_url, source=platform,
                                          city=self.city, is_business_nav=True,
                                          now=self.clock())
            return False, reason, None
        if self._platform_paused(platform):
            reason = 'platform_paused'
            self.breaker.record_exhausted(ledger_key=platform, reason=reason,
                                          requested_url=requested_url, source=platform,
                                          city=self.city, is_business_nav=is_business_nav,
                                          now=self.clock())
            return False, reason, None
        decision, reason = decide_route(blockable=blockable, breaker=self.breaker,
                                        ledger_key=platform, is_business_nav=is_business_nav)
        if decision == CONTINUE:
            return True, None, self.allocate_request_id()
        if decision == ABORT:
            self.breaker.record_exhausted(ledger_key=platform, reason=reason,
                                          requested_url=requested_url, source=platform,
                                          city=self.city, is_business_nav=is_business_nav,
                                          now=self.clock())
        return False, reason, None

    def wait_interval(self, platform, now=None):
        """跨进程业务间隔：取本进程与共享账本 last_request_at 的较新者计算等待。"""
        now = self.clock() if now is None else now
        last_ledger = (self.ledger.get('last_request_at') or {}).get(platform)
        candidates = [x for x in (self._last_at, last_ledger) if x is not None]
        if not candidates:
            return 0.0
        last = max(float(x) for x in candidates)
        wait = max(self.interval_for(platform) - (now - last), self._retry_after)
        if wait > 0:
            self.sleep(wait)
            self._retry_after = 0.0
        return wait

    def note_sent(self, platform=None, now=None):
        self._last_at = self.clock() if now is None else now
        if platform is not None:
            self._last_sent_platform[platform] = self._last_at

    def honour_retry_after(self, header):
        try:
            seconds = float(header)
        except (TypeError, ValueError):
            return
        if seconds > 0:
            self._retry_after = max(self._retry_after, seconds)

    def persist(self):
        """有计数请求时原子回写共享账本；可多次调用（增量安全）。

        两进程竞争：回写前重读磁盘账本，把本次自上次 persist 以来的增量累加到
        磁盘值上（而非用本进程快照绝对值覆盖），并发写互不丢账。
        """
        delta = self.breaker.counted_total - self._persisted_counted
        if delta <= 0 and self.breaker.counted_total <= 0:
            return False
        disk = self._load_ledger()
        state = json.loads(json.dumps(disk))
        state['trial_used'] = int(disk.get('trial_used') or 0) + max(0, delta)
        combined = state.setdefault('combined_daily_used', {})
        combined[self.today] = int(
            (disk.get('combined_daily_used') or {}).get(self.today) or 0) + max(0, delta)
        daily = state.setdefault('platform_daily_used', {})
        day = daily.setdefault(self.today, {})
        for key, used in self.breaker.per_source_run.items():
            persisted = self._persisted_per_source.get(key, 0)
            inc = used - persisted
            if inc > 0:
                day[key] = int(day.get(key) or 0) + inc
                self._persisted_per_source[key] = used
        state['next_request_id'] = max(int(disk.get('next_request_id') or 1),
                                       self._next_request_id)
        last_at = dict(disk.get('last_request_at') or {})
        for platform, epoch in self._last_sent_platform.items():
            last_at[platform] = max(float(last_at.get(platform) or 0), float(epoch))
        state['last_request_at'] = last_at
        paused = dict(disk.get('platform_paused') or {})
        paused.update(self.ledger.get('platform_paused') or {})
        state['platform_paused'] = paused
        state['updated_at'] = now_iso()
        aj._write_json(self.ledger_path, state)
        self.ledger = state
        self._persisted_counted = self.breaker.counted_total
        if self._round is not None:
            delta = self.breaker.business_navs - self._round_persisted_navs
            if delta:
                self._round['business_used'] += delta
                self._round_persisted_navs = self.breaker.business_navs
                aj._write_json(self.round_budget_path, {
                    'schema': 'round_business_budget/v1', 'round': self._round['round'],
                    'cap': self._round['cap'],
                    'business_used': self._round['business_used'],
                    'updated_at': now_iso(), 'updated_by_run': self.run_id})
        return True

    def report(self):
        return self.breaker.snapshot()


def _date_of(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# 4b) 逐请求门（F01）：任何传输的每次尝试发出前预留，日志可还原账本
# ---------------------------------------------------------------------------
class RequestGate:
    """浏览器/HTTP/重定向跳/XHR/静态资源共用：发出前预留额度，逐行落盘。

    reserve() 返回行（含唯一 request_id）；额度拒绝也落盘（counted=False，
    blocking_reason=原因）。settle() 补齐结果字段并追加到 requests.jsonl。
    """

    def __init__(self, scheduler, out, *, batch_id, stage):
        self.scheduler = scheduler
        self.out = Path(out)
        self.batch_id = batch_id
        self.stage = stage
        self.rows = []
        self.last_refusal_reason = None

    def _base_row(self, *, platform, source, city, url, resource_type, transport):
        allowed, reason, request_id = self.scheduler.check(
            platform, is_business_nav=(resource_type == 'business_nav'),
            requested_url=url)
        row = {'source': source, 'city': city, 'platform': platform,
               'requested_url': url, 'final_url': url, 'requested_at': now_iso(),
               'status': None, 'redirect_chain': [], 'failure_class': None,
               'error_type': None, 'snapshot_path': None, 'sha256': None, 'bytes': None,
               'content_type': None, 'retry_after': None, 'elapsed_ms': None,
               'records_parsed': 0, 'page_state': None, 'network_attempt': bool(allowed),
               'request_id': request_id, 'run_id': self.scheduler.run_id,
               'batch_id': self.batch_id, 'stage': self.stage, 'transport': transport,
               'resource_type': resource_type, 'counted': bool(allowed),
               'cache': 'miss', 'blocking_reason': reason}
        self.rows.append(row)
        if not allowed:
            self.last_refusal_reason = reason
        return row if allowed else None

    def reserve(self, *, source, city, url, resource_type='business_nav',
                transport='http'):
        return self._base_row(platform=_platform_of(source), source=source, city=city,
                              url=url, resource_type=resource_type, transport=transport)

    def precheck(self, *, source, url, resource_type='business_nav'):
        """浏览器导航预检：不计数不占ID，只判断额度是否允许发出首个文档请求。"""
        platform = _platform_of(source)
        if self.scheduler._platform_paused(platform):
            return False, 'platform_paused'
        if resource_type == 'business_nav' and self.scheduler.round_business_remaining() == 0:
            return False, 'round_business_cap'
        if resource_type == 'business_nav' and \
                self.scheduler.breaker.business_navs >= self.scheduler.breaker.run_business_nav_cap:
            return False, 'run_business_nav_cap'
        allowed, reason = self.scheduler.breaker.check(
            platform, is_business_nav=(resource_type == 'business_nav'))
        return (True, None) if allowed else (False, reason)

    def settle(self, row, **fields):
        row = dict(row)
        row.update(fields)
        append_jsonl(self.out / 'requests.jsonl', [row])
        return row

    def log_refused(self, *, source, city, url, resource_type='business_nav',
                    transport='http', reason='refused'):
        """补记一行不计额度的拒绝（额度拒绝已由 _base_row 落盘）。"""
        return self.settle({
            'source': source, 'city': city, 'platform': _platform_of(source),
            'requested_url': url, 'final_url': url, 'requested_at': now_iso(),
            'status': None, 'redirect_chain': [], 'failure_class': 'refused',
            'error_type': None, 'network_attempt': False, 'request_id': None,
            'run_id': self.scheduler.run_id, 'batch_id': self.batch_id,
            'stage': self.stage, 'transport': transport,
            'resource_type': resource_type, 'counted': False,
            'cache': 'miss', 'blocking_reason': reason})


# ---------------------------------------------------------------------------
# 5) 文件与观测小工具
# ---------------------------------------------------------------------------
def _read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(aj._read_text(path))
    except (OSError, ValueError):
        return default


def _default_ledger():
    return {'trial_used': 0, 'next_request_id': 1,
            'combined_daily_used': {}, 'platform_daily_used': {}}


def _platform_of(source):
    return _SOURCE_PLATFORM.get(source, source)


def _merge_class(state):
    if state == 'normal':
        return 'normal'
    if state == 'empty_directory':
        return 'empty_directory'
    return 'structure_change'


def _new_cursor(run_id, source, city, batch_id):
    return {'schema': CURSOR_SCHEMA, 'run_id': run_id, 'source': source, 'city': city,
            'current_url': None, 'url_class': None, 'next_unvisited': [], 'visited': [],
            'retry_queue': [], 'batch_id': batch_id, 'updated_at': now_iso()}


def _load_cursor(out):
    return _read_json(Path(out) / 'run_state' / 'cursor.json')


def _save_cursor(out, cursor):
    cursor['updated_at'] = now_iso()
    aj._write_json(Path(out) / 'run_state' / 'cursor.json', cursor)


def _save_snapshot(snaps_dir, body, sha, *, private_dir=None, out=None):
    """F03 双写：原件存私有目录（交付清单之外），交付目录只存脱敏副本。

    返回 (original_path|None, sanitized_path, evidence_row)。
    """
    payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
    snaps_dir.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_for_delivery(payload)
    san_sha = hashlib.sha256(sanitized).hexdigest()
    san_path = snaps_dir / f'{san_sha}.html'
    san_path.write_bytes(sanitized)
    original_path = None
    evidence = {'original_sha256': sha, 'sanitized_sha256': san_sha,
                'bytes_original': len(payload), 'bytes_sanitized': len(sanitized),
                'at': now_iso()}
    if private_dir is not None:
        private_dir = Path(private_dir)
        private_dir.mkdir(parents=True, exist_ok=True)
        original_path = private_dir / f'{sha}.html'
        original_path.write_bytes(payload)
        evidence['original_path'] = str(original_path)
    evidence['sanitized_path'] = str(san_path)
    if out is not None:
        append_jsonl(Path(out) / 'evidence_index.jsonl', [evidence])
    (snaps_dir / f'{san_sha}.sha256').write_text(san_sha, encoding='utf-8')
    return original_path, san_path, evidence


def _evidence_parse_path(row):
    """解析/复算优先私有原件；缺失时回退交付副本。"""
    original = row.get('original_path') or row.get('snapshot_path')
    if original and Path(original).exists():
        return original
    sanitized = row.get('sanitized_path') or row.get('delivery_snapshot_path')
    if sanitized and Path(sanitized).exists():
        return sanitized
    return original or sanitized


def _discovered_links(parsed):
    links = []
    for link in parsed.get('detail_links') or []:
        links.append(link)
    for link in parsed.get('district_links') or []:
        links.append((link or {}).get('url'))
    for link in parsed.get('city_links') or []:
        links.append((link or {}).get('url'))
    if parsed.get('next_page_url'):
        links.append(parsed['next_page_url'])
    return [link for link in links if link]


def _advance_cursor(cursor, url, url_class, parsed, *, success=True):
    """F02: 只有成功（normal/empty_directory）才标 visited；失败进入 retry_queue 待重试。"""
    visited = list(cursor.get('visited') or [])
    next_unvisited = list(cursor.get('next_unvisited') or [])
    retry_queue = list(cursor.get('retry_queue') or [])
    if url in next_unvisited:
        next_unvisited.remove(url)
    if success:
        if url and url not in visited:
            visited.append(url)
        if url in retry_queue:
            retry_queue.remove(url)
        for link in _discovered_links(parsed):
            if link not in visited and link not in next_unvisited and link not in retry_queue:
                next_unvisited.append(link)
    else:
        if url and url not in retry_queue:
            retry_queue.append(url)
    cursor.update({'current_url': url, 'url_class': url_class, 'visited': visited,
                   'next_unvisited': next_unvisited, 'retry_queue': retry_queue})
    return cursor


def _enrich(record, *, source, city, url_class, parser_status_value, state, snapshot_path,
            sha, requested_url, final_url, observed_at, sanitized_path=None,
            sanitized_sha256=None):
    row = dict(record or {})
    row.update({
        'schema': OBSERVATION_SCHEMA, 'source': source, 'city': city, 'url_class': url_class,
        'parser_status': parser_status_value, 'page_state': state,
        'snapshot_path': snapshot_path, 'sha256': sha,
        'delivery_snapshot_path': sanitized_path, 'sanitized_sha256': sanitized_sha256,
        'requested_url': requested_url, 'final_url': final_url, 'observed_at': observed_at,
        'normalized': aj.normalize_fields(record),
    })
    return row


def _ingest(existing_obs, cursor, *, source, city, url, final_url, url_class, parsed, state,
            snapshot_path, sha, fetched_at, success=None, sanitized_path=None,
            sanitized_sha256=None):
    """归一化 + 身份合并；非 normal 状态只保留旧观测。返回 (merged, changes, dup_keys)。"""
    if success is None:
        success = state in ('normal', 'empty_directory')
    incoming = [_enrich(record, source=source, city=city, url_class=url_class,
                        parser_status_value=parsed.get('parser_status') or '未实站验证',
                        state=state, snapshot_path=snapshot_path, sha=sha,
                        requested_url=url, final_url=final_url, observed_at=fetched_at,
                        sanitized_path=sanitized_path, sanitized_sha256=sanitized_sha256)
                for record in (parsed.get('records') or [])]
    prior_by_key = {}
    for record in existing_obs:
        prior_by_key.setdefault(aj.identity_key(record), record)
    changes = {}
    for record in incoming:
        key = aj.identity_key(record)
        old = prior_by_key.get(key)
        if old is None:
            continue
        old_norm = old.get('normalized') or aj.normalize_fields(old)
        new_norm = record.get('normalized') or aj.normalize_fields(record)
        delta = {field: {'old': old_norm.get(field, {}).get('value'), 'new': info['value']}
                 for field, info in new_norm.items()
                 if info['state'] == 'observed'
                 and old_norm.get(field, {}).get('value') != info['value']}
        if delta:
            changes[key] = delta
    merged = aj.merge_records(existing_obs, incoming, _merge_class(state))
    dup_keys = sorted(set(prior_by_key) & {aj.identity_key(record) for record in incoming})
    _advance_cursor(cursor, url, url_class, parsed, success=success)
    return merged, changes, dup_keys


def _append_event(out, event):
    payload = dict(event)
    payload.setdefault('schema', EVENT_SCHEMA)
    payload.setdefault('at', now_iso())
    append_jsonl(Path(out) / 'session_events.jsonl', [payload])


def _update_index(out, url, snapshot_path, sha, body):
    path = Path(out) / 'index.json'
    data = _read_json(path, {'schema': INDEX_SCHEMA, 'snapshots': []}) or {}
    rows = data.setdefault('snapshots', [])
    if not any(row.get('sha256') == sha for row in rows):
        payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
        rows.append({'sha256': sha, 'bytes': len(payload), 'path': str(snapshot_path),
                     'url': url, 'requested_at': now_iso()})
    data['schema'] = INDEX_SCHEMA
    data['updated_at'] = now_iso()
    aj._write_json(path, data)


def _record_field_changes(out, changes):
    if not changes:
        return
    path = Path(out) / 'field_changes.json'
    data = _read_json(path, {'schema': 'anjuke_field_changes/v1', 'changes': []}) or {}
    entries = data.setdefault('changes', [])
    for key, delta in changes.items():
        entries.append({'identity_key': key, 'changes': delta, 'at': now_iso()})
    aj._write_json(path, data)


def _record_duplicates(out, dup_keys):
    path = Path(out) / 'duplicates.json'
    data = _read_json(path, {'schema': 'anjuke_duplicates/v1', 'identity_keys': []}) or {}
    keys = set(data.get('identity_keys') or [])
    keys.update(dup_keys)
    data['identity_keys'] = sorted(keys)
    data['updated_at'] = now_iso()
    aj._write_json(path, data)


def _request_row(*, source, city, url, url_class, run_id, batch_id, request_id):
    return {'source': source, 'city': city, 'platform': _platform_of(source),
            'requested_url': url, 'final_url': url, 'requested_at': now_iso(), 'status': None,
            'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'records_parsed': 0, 'page_state': None,
            'parser_status': parser_status(source, _kind_of(url_class)),
            'network_attempt': True, 'request_id': request_id, 'run_id': run_id,
            'batch_id': batch_id, 'url_class': url_class}


def _kind_of(url_class):
    return 'detail' if url_class == 'detail' else 'catalog'


def _failure_class(state):
    return None if state == 'normal' else state


# ---------------------------------------------------------------------------
# 6) 单 URL 采集
# ---------------------------------------------------------------------------
BUDGET_REFUSALS = frozenset(('round_business_cap', 'run_business_nav_cap', 'trial_total_cap',
                             'per_source_daily_cap', 'combined_daily_cap'))
BLOCKING_STATES = frozenset(('verify_challenge', 'login_wall'))
CHALLENGE_COOLDOWN_S = 3600.0


def _pause_active(out, clock=None):
    pause = _read_json(Path(out) / 'run_state' / 'pause.json')
    if not pause or not pause.get('paused'):
        return False, None
    until = pause.get('until')
    if until is not None and clock is not None and clock() >= float(until):
        return False, None  # 冷却到期：暂停失效，可恢复（恢复后 recheck 重新验证）
    return True, pause


def _collect_one(*, source, city, url, url_class, out, snaps, scheduler, gate,
                 batch_id, run_id, stage, browser, profile_dir, transport, fetcher,
                 session, timeout, headless=True, private_dir=None):
    platform = _platform_of(source)
    is_local = url.startswith('file://')
    main_row = None
    sub_rows = []
    gate_start = len(gate.rows) if gate is not None else 0

    # F02: 暂停状态实际拦截请求（不只是写日志）。
    if not is_local:
        paused, pause_row = _pause_active(out, scheduler.clock)
        if paused:
            if gate is not None:
                gate.log_refused(source=source, city=city, url=url,
                                 resource_type='business_nav',
                                 transport='browser' if browser and browser != 'none'
                                 else 'http', reason='paused')
            return {'url': url, 'url_class': url_class, 'fetched': False, 'refused': True,
                    'reason': 'paused', 'pause': pause_row, 'page_state': None,
                    'records_parsed': 0}
        if browser and browser != 'none':
            # 浏览器模式：首个文档请求在 route 回调中发出前预留；此处仅预检。
            allowed, reason = gate.precheck(source=source, url=url,
                                            resource_type='business_nav')
            if not allowed:
                gate.log_refused(source=source, city=city, url=url,
                                 resource_type='business_nav', transport='browser',
                                 reason=reason)
                return {'url': url, 'url_class': url_class, 'fetched': False,
                        'refused': True, 'reason': reason, 'page_state': None,
                        'records_parsed': 0}
            scheduler.wait_interval(platform)
        else:
            main_row = gate.reserve(source=source, city=city, url=url,
                                    resource_type='business_nav', transport='http')
            if main_row is None:
                return {'url': url, 'url_class': url_class, 'fetched': False,
                        'refused': True, 'reason': gate.last_refusal_reason,
                        'page_state': None, 'records_parsed': 0}
            scheduler.wait_interval(platform)

    fetched = _fetch(url, browser=browser, profile_dir=profile_dir, transport=transport,
                     fetcher=fetcher, session=session, timeout=timeout,
                     follow_redirects=(source == 'fang'), headless=headless,
                     hop_reserve=(None if is_local else
                                  (lambda hop_url: gate.reserve(
                                      source=source, city=city, url=hop_url,
                                      resource_type='http_redirect_hop',
                                      transport='http'))))
    if not is_local:
        scheduler.note_sent(platform)
        if main_row is not None:
            scheduler.persist()   # F02: 逐请求落账，进程异常退出不丢账
    new_rows = gate.rows[gate_start:] if gate is not None else []
    if main_row is None:
        main_row = next((r for r in new_rows
                         if r.get('resource_type') == 'business_nav'
                         and r.get('requested_url') == url), None)
    sub_rows = [r for r in new_rows if r is not main_row]

    if fetched.get('error_type'):
        if main_row is not None:
            gate.settle(main_row, failure_class='transport_error',
                        error_type=fetched['error_type'], body_state='no_body',
                        elapsed_ms=fetched.get('elapsed_ms'))
            main_row['_settled'] = True
        else:
            # 预检通过但连接未建立（如 DNS/连接拒绝）：补记一行失败尝试。
            gate.log_refused(source=source, city=city, url=url,
                             resource_type='business_nav',
                             transport='browser' if browser and browser != 'none'
                             else 'http', reason='transport_error')
        for row in sub_rows:
            if not row.get('_settled'):
                gate.settle(row, settlement='sent')
                row['_settled'] = True
        _append_event(out, {'event': 'collect', 'url': url, 'state': 'transport_error'})
        return {'url': url, 'url_class': url_class, 'fetched': False, 'refused': False,
                'reason': 'transport_error', 'page_state': 'transport_error',
                'records_parsed': 0}

    body = fetched['body']
    payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
    sha = hashlib.sha256(payload).hexdigest()
    final_url = fetched.get('final_url') or url
    original_path, san_path, evidence = _save_snapshot(
        snaps, body, sha, private_dir=private_dir, out=out)
    for row in sub_rows:
        if not row.get('_settled'):
            gate.settle(row, settlement='sent')
            row['_settled'] = True

    kind = _kind_of(url_class)
    parsed = parse_page(source, kind, body, url=final_url, fetched_at=evidence['at'],
                        snapshot_path=str(original_path or san_path), city=city)
    state = classify_page_state(body, url=final_url, status=fetched.get('status'),
                                title=fetched.get('title') or parsed.get('title') or '',
                                container_found=parsed.get('container_found', False),
                                empty_marker=parsed.get('empty_marker', False),
                                records=parsed.get('records') or [])
    http_status = fetched.get('status')
    if main_row is not None:
        main_row.update({'final_url': final_url, 'status': http_status,
                         'bytes': len(payload), 'sha256': sha,
                         'elapsed_ms': fetched.get('elapsed_ms'),
                         'body_state': 'retained',
                         'retry_after': fetched.get('retry_after'),
                         'redirect_chain': fetched.get('redirect_chain') or [],
                         'snapshot_path': str(original_path) if original_path else None,
                         'delivery_snapshot_path': str(san_path),
                         'sanitized_sha256': evidence['sanitized_sha256']})
        gate.settle(main_row, page_state=state['state'],
                    failure_class=_failure_class(state['state']),
                    records_parsed=len(parsed.get('records') or []))
        main_row['_settled'] = True
    scheduler.honour_retry_after(fetched.get('retry_after'))

    # F02: 挑战/限流停止当前来源 —— 自动写入暂停与账本 platform_paused，等待冷却或外部恢复。
    auto_paused = False
    if not is_local and (state['state'] in BLOCKING_STATES or
                         (isinstance(http_status, int) and http_status in (403, 429))):
        reason = state['state'] if state['state'] in BLOCKING_STATES else \
            'http_%s' % http_status
        cooldown = CHALLENGE_COOLDOWN_S if state['state'] in BLOCKING_STATES else \
            max(900.0, float(fetched.get('retry_after') or 0) or 900.0)
        pause(out=out, reason=reason, url=url, auto=True, until=scheduler.clock() + cooldown)
        ledger = scheduler.ledger
        ledger.setdefault('platform_paused', {})[platform] = {
            'reason': reason, 'until': scheduler.clock() + cooldown,
            'paused_at': now_iso(), 'run_id': run_id}
        scheduler.persist()
        auto_paused = True

    existing = read_jsonl(Path(out) / 'observations.jsonl')
    cursor = _load_cursor(out) or _new_cursor(run_id, source, city, batch_id)
    merged, changes, dup_keys = _ingest(
        existing, cursor, source=source, city=city, url=url, final_url=final_url,
        url_class=url_class, parsed=parsed, state=state['state'],
        snapshot_path=str(original_path) if original_path else str(san_path), sha=sha,
        fetched_at=evidence['at'], sanitized_path=str(san_path),
        sanitized_sha256=evidence['sanitized_sha256'])
    write_jsonl(Path(out) / 'observations.jsonl', merged)
    _save_cursor(out, cursor)
    _update_index(out, final_url, original_path or san_path, sha, payload)
    _record_field_changes(out, changes)
    _record_duplicates(out, dup_keys)
    _append_event(out, {'event': 'collect', 'url': url, 'url_class': url_class,
                        'state': state['state'], 'reasons': state['reasons'],
                        'records': len(parsed.get('records') or [])})
    return {'url': url, 'url_class': url_class, 'fetched': True, 'refused': False,
            'reason': None, 'page_state': state['state'], 'reasons': state['reasons'],
            'records_parsed': len(parsed.get('records') or []), 'sha256': sha,
            'snapshot_path': str(original_path) if original_path else str(san_path),
            'delivery_snapshot_path': str(san_path),
            'auto_paused': auto_paused}


MAX_REDIRECT_HOPS = 4


def _fetch(url, *, browser, profile_dir, transport, fetcher, session, timeout,
           follow_redirects=False, headless=True, hop_reserve=None):
    """取一个 URL。F01: HTTP 重定向改为手动逐跳，每跳发出前经 hop_reserve 预留。

    hop_reserve(url) -> 行|None：None 表示额度拒绝，立即停止跟随（熔断）。
    """
    if browser and browser != 'none':
        if fetcher is None:
            fetcher = BrowserFetcher(profile_dir or _default_profile_dir(), browser=browser,
                                     headless=headless)
        fetched = fetcher.fetch(url)
        fetched.setdefault('retry_after', None)
        return fetched
    if url.startswith('file://'):
        try:
            raw = url2pathname(unquote(urlsplit(url).path))
            return {'status': None, 'title': '', 'final_url': url,
                    'body': Path(raw).read_bytes(), 'elapsed_ms': 0, 'error_type': None,
                    'retry_after': None, 'redirect_chain': []}
        except OSError as exc:
            return {'status': None, 'title': '', 'final_url': url, 'body': '',
                    'elapsed_ms': 0, 'error_type': type(exc).__name__, 'retry_after': None,
                    'redirect_chain': []}
    if follow_redirects and transport is None:
        import requests
        started = time.monotonic()
        current = url
        chain = []
        response = None
        error_type = None
        try:
            for hop in range(MAX_REDIRECT_HOPS + 1):
                if hop > 0:
                    if hop_reserve is None:
                        error_type = 'redirect_budget_unavailable'
                        break
                    row = hop_reserve(current)
                    if row is None:
                        error_type = 'redirect_budget_exhausted'
                        break
                response = session.get(current, timeout=(timeout, timeout),
                                       allow_redirects=False)
                if 300 <= response.status_code < 400 and response.headers.get('Location'):
                    chain.append(current)
                    current = urljoin(current, response.headers['Location'])
                    continue
                break
        except requests.RequestException as exc:
            return {'status': None, 'title': '', 'final_url': url, 'body': '',
                    'elapsed_ms': round((time.monotonic() - started) * 1000),
                    'error_type': type(exc).__name__, 'retry_after': None,
                    'redirect_chain': chain}
        if error_type is not None:
            return {'status': None, 'title': '', 'final_url': current, 'body': '',
                    'elapsed_ms': round((time.monotonic() - started) * 1000),
                    'error_type': error_type, 'retry_after': None,
                    'redirect_chain': chain}
        return {'status': response.status_code, 'title': '', 'final_url': current,
                'body': response.content,
                'elapsed_ms': round((time.monotonic() - started) * 1000),
                'error_type': None, 'retry_after': response.headers.get('Retry-After'),
                'redirect_chain': chain}

    getter = transport or aj._http_get
    outcome = getter(session, url, timeout)
    response = outcome.get('response')
    if response is None:
        return {'status': None, 'title': '', 'final_url': url, 'body': '',
                'elapsed_ms': outcome.get('elapsed_ms'),
                'error_type': outcome.get('error_type'), 'retry_after': None,
                'redirect_chain': []}
    return {'status': response.status_code, 'title': '',
            'final_url': url, 'body': response.content,
            'elapsed_ms': outcome.get('elapsed_ms'), 'error_type': None,
            'retry_after': response.headers.get('Retry-After'),
            'redirect_chain': []}


def _default_profile_dir():
    import os
    return profile_path('anjuke', 'run-04')


def _guess_url_class(url):
    return 'detail' if aj._is_community_detail_url(url) else 'catalog'


# ---------------------------------------------------------------------------
# 7) 执行器（collect/resume 共用）
# ---------------------------------------------------------------------------
class RunStateMachine:
    """F02: 控制状态机。状态写入 run_state/state_machine.json，并实际拦截请求。"""

    def __init__(self, out, run_id):
        self.out = Path(out)
        self.run_id = run_id
        self.history = []

    @property
    def state(self):
        return self.history[-1]['state'] if self.history else 'ready'

    def transition(self, state, reason=None, **details):
        assert state in RUN_STATES, state
        row = {'state': state, 'reason': reason, 'at': now_iso(),
               'run_id': self.run_id}
        row.update(details)
        self.history.append(row)
        aj._write_json(self.out / 'run_state' / 'state_machine.json', {
            'schema': STATE_SCHEMA, 'current': state, 'history': self.history,
            'updated_at': now_iso()})
        return row


def _exit_code_of(entries, stop_reason):
    """F02: 退出码表达真实结果：0 成功 / 2 部分 / 3 预算 / 4 阻断。"""
    states = [entry.get('page_state') for entry in entries]
    reasons = [entry.get('reason') for entry in entries if entry.get('refused')]
    if any(reason in BUDGET_REFUSALS or reason in ('paused', 'platform_paused')
           for reason in reasons):
        return EXIT_BUDGET if any(r in BUDGET_REFUSALS for r in reasons) else EXIT_BLOCKED
    if stop_reason and stop_reason in BUDGET_REFUSALS:
        return EXIT_BUDGET
    if any(state in BLOCKING_STATES for state in states):
        return EXIT_BLOCKED
    if stop_reason in ('verify_challenge', 'login_wall', 'paused', 'platform_paused'):
        return EXIT_BLOCKED
    if stop_reason:
        return EXIT_BUDGET if stop_reason in BUDGET_REFUSALS else EXIT_PARTIAL
    if states and all(state in ('normal', 'empty_directory') for state in states):
        return EXIT_OK
    if any(state in ('transport_error',) for state in states):
        return EXIT_PARTIAL
    return EXIT_PARTIAL if states else EXIT_FATAL


def _execute(*, mode, source, city, urls, out, profile_dir, budget_ledger, interval,
             max_requests, batch_id, stage, browser, run_id, timeout, clock, sleep,
             transport, fetcher, headless=True, round_budget_path=None,
             round_business_cap=120, private_dir=None, ledger_required=False):
    out = Path(out)
    run_state = out / 'run_state'
    snaps = out / 'snapshots'
    run_state.mkdir(parents=True, exist_ok=True)
    snaps.mkdir(parents=True, exist_ok=True)
    clock = clock or _now_epoch
    sleep = sleep or _sleep
    run_id = run_id or f'{mode}-{uuid.uuid4().hex[:12]}'
    batch_id = batch_id or run_id
    if private_dir is None:
        private_dir = str(out.parent / (out.name + '-private'))
    machine = RunStateMachine(out, run_id)
    machine.transition('running', reason='run_start', mode=mode)
    ledger_path = Path(budget_ledger)
    ledger_before = _read_json(ledger_path, _default_ledger())
    aj._write_json(run_state / 'ledger_before.json', ledger_before)
    scheduler = Scheduler(ledger_path, run_id=run_id, city=city, clock=clock, sleep=sleep,
                          interval=interval, round_budget_path=round_budget_path,
                          round_business_cap=round_business_cap,
                          ledger_required=ledger_required)
    gate = RequestGate(scheduler, out, batch_id=batch_id, stage=stage)

    cursor = _load_cursor(out) or _new_cursor(run_id, source, city, batch_id)
    if mode == 'collect' and urls:
        cursor['source'] = source
        cursor['city'] = city
        cursor['current_url'] = urls[0][0]
        cursor['url_class'] = urls[0][1]
        _save_cursor(out, cursor)

    recheck = _recheck(out, limit=5) if mode == 'resume' else []
    if mode == 'collect':
        queue = list(urls)
    else:
        # F02: 恢复先重试失败队列，再走未访问队列；恢复前重新验证内容与状态。
        retry_queue = list(cursor.get('retry_queue') or [])
        next_unvisited = list(cursor.get('next_unvisited') or [])
        queue = [(link, _guess_url_class(link))
                 for link in (retry_queue + next_unvisited)[:max_requests]]

    session = None
    if browser in (None, 'none'):
        import requests
        session = requests.Session()
        session.trust_env = False
        session.headers['User-Agent'] = aj.UA

    fetcher_obj = fetcher
    owned_fetcher = None
    if browser and browser != 'none' and fetcher_obj is None:
        owned_fetcher = BrowserFetcher(
            profile_dir or _default_profile_dir(), browser=browser, headless=headless,
            request_gate=gate, source=source, city=city)
        fetcher_obj = owned_fetcher

    entries = []
    stop_reason = None
    try:
        for url, url_class in queue:
            # F02: 暂停激活时不再发出任何请求（pause() 后续跑不发请求）。
            paused, _row = _pause_active(out, clock)
            if paused:
                entries.append({'url': url, 'url_class': url_class, 'fetched': False,
                                'refused': True, 'reason': 'paused', 'page_state': None,
                                'records_parsed': 0})
                stop_reason = 'paused'
                machine.transition('needs_external', reason='pause_active', url=url)
                break
            event = _collect_one(source=source, city=city, url=url, url_class=url_class,
                                 out=out, snaps=snaps, scheduler=scheduler, gate=gate,
                                 batch_id=batch_id, run_id=run_id, stage=stage,
                                 browser=browser, profile_dir=profile_dir,
                                 transport=transport, fetcher=fetcher_obj,
                                 session=session, timeout=timeout, headless=headless,
                                 private_dir=private_dir)
            entries.append(event)
            if event.get('refused'):
                stop_reason = event.get('reason') or 'refused'
                if stop_reason in BUDGET_REFUSALS:
                    machine.transition('budget_exhausted', reason=stop_reason, url=url)
                else:
                    machine.transition('needs_external', reason=stop_reason, url=url)
                break
            if event.get('page_state') in BLOCKING_STATES:
                stop_reason = event.get('page_state')
                machine.transition('needs_external', reason=stop_reason, url=url,
                                   auto_paused=True)
                break
            if event.get('auto_paused'):
                stop_reason = 'source_cooldown'
                machine.transition('source_cooldown', reason='auto_paused', url=url)
                break
    finally:
        # F02: 挑战/暂停后保留自建窗口与会话现场供诊断；正常结束才关闭。
        if owned_fetcher is not None and stop_reason not in \
                ('verify_challenge', 'login_wall', 'paused', 'source_cooldown'):
            owned_fetcher.close()

    scheduler.persist()
    ledger_after = _read_json(ledger_path, ledger_before)
    aj._write_json(run_state / 'ledger_after.json', ledger_after)
    cursor = _load_cursor(out) or cursor
    _write_batch(out, mode=mode, run_id=run_id, batch_id=batch_id, source=source, city=city,
                 entries=entries, recheck=recheck, stop_reason=stop_reason)
    budget = scheduler.report()
    exit_code = _exit_code_of(entries, stop_reason)
    if exit_code == EXIT_OK:
        machine.transition('succeeded', reason='all_normal')
    elif exit_code == EXIT_BUDGET:
        machine.transition('budget_exhausted', reason=stop_reason or 'budget')
    elif exit_code == EXIT_BLOCKED:
        machine.transition('needs_external', reason=stop_reason or 'blocked')
    elif exit_code == EXIT_PARTIAL:
        machine.transition('partial_success', reason=stop_reason or 'partial')
    else:
        machine.transition('failed', reason=stop_reason or 'fatal')
    aj._write_json(run_state / 'status.json', {
        'schema': STATUS_SCHEMA, 'run_id': run_id, 'source': source, 'city': city,
        'state': machine.state,
        'last_url': (entries[-1]['url'] if entries else cursor.get('current_url')),
        'batch_id': batch_id, 'requests_this_run': budget.get('counted_total'),
        'business_navs_this_run': budget.get('business_navs'),
        'round_business_remaining': scheduler.round_business_remaining(),
        'stop_reason': stop_reason, 'exit_code': exit_code, 'budget': budget,
        'browser_kept_open': bool(owned_fetcher is not None and stop_reason in
                                  ('verify_challenge', 'login_wall', 'paused',
                                   'source_cooldown')),
        'updated_at': now_iso()})

    return {'mode': mode, 'out': str(out), 'run_id': run_id, 'batch_id': batch_id,
            'source': source, 'city': city, 'entries': entries, 'recheck': recheck,
            'stop_reason': stop_reason, 'counted_total': budget.get('counted_total'),
            'business_navs': budget.get('business_navs'),
            'state': machine.state, 'exit_code': exit_code}


def _write_batch(out, *, mode, run_id, batch_id, source, city, entries, recheck, stop_reason):
    aj._write_json(Path(out) / 'run_state' / 'batch.json', {
        'schema': BATCH_SCHEMA, 'mode': mode, 'run_id': run_id, 'batch_id': batch_id,
        'source': source, 'city': city, 'entries': entries, 'recheck': recheck,
        'stop_reason': stop_reason, 'updated_at': now_iso()})


def _recheck(out, *, limit=5):
    """离线复查已有观测：重解析已存快照，核对身份键稳定；不发任何网络请求。

    F02: 恢复前重新验证内容与状态 —— 私有原件优先，缺失回退脱敏副本。
    """
    observations = read_jsonl(Path(out) / 'observations.jsonl')
    records = []
    for obs in observations[:max(0, limit)]:
        path_str = _evidence_parse_path(obs)
        row = {'url': obs.get('final_url') or obs.get('requested_url'),
               'url_class': obs.get('url_class'),
               'identity_key': aj.identity_key(obs), 'stable': False, 'page_state': None}
        if path_str:
            snap = Path(path_str)
            if snap.exists():
                body = snap.read_bytes()
                sha = hashlib.sha256(body).hexdigest()
                row['sha256_matches'] = (sha == obs.get('sha256'))
                parsed = parse_page(obs.get('source') or 'anjuke',
                                    _kind_of(obs.get('url_class')), body,
                                    url=row['url'], fetched_at=now_iso(),
                                    snapshot_path=str(snap), city=obs.get('city'))
                state = classify_page_state(
                    body, url=row['url'], status=200, title=parsed.get('title') or '',
                    container_found=parsed.get('container_found', False),
                    empty_marker=parsed.get('empty_marker', False),
                    records=parsed.get('records') or [])
                row['page_state'] = state['state']
                row['stable'] = any(aj.identity_key(rec) == row['identity_key']
                                    for rec in (parsed.get('records') or []))
        records.append(row)
    return records


# ---------------------------------------------------------------------------
# 8) 离线复放（零网络）
# ---------------------------------------------------------------------------
def replay(*, out, evidence_manifest, city, run_id=None, clock=None):
    """只读已存文件重放：不建 session、不发请求、不改账本。"""
    out = Path(out)
    run_state = out / 'run_state'
    run_state.mkdir(parents=True, exist_ok=True)
    clock = clock or _now_epoch
    run_id = run_id or f'replay-{uuid.uuid4().hex[:12]}'
    items = _read_json(evidence_manifest, []) or []
    existing = read_jsonl(out / 'observations.jsonl')
    cursor = _load_cursor(out) or _new_cursor(
        run_id, (items[0].get('source') if items else 'anjuke'), city, run_id)
    entries = []
    for item in items:
        if city and item.get('city') and item.get('city') != city:
            continue
        path_str = item.get('path')
        url = item.get('url')
        source = (item.get('source') or 'anjuke').lower()
        url_class = item.get('url_class') or _guess_url_class(url or '')
        kind = item.get('kind') or _kind_of(url_class)
        entry = {'city': item.get('city'), 'source': source, 'url': url,
                 'url_class': url_class, 'replayed': False, 'page_state': None}
        if path_str and Path(path_str).exists():
            body = Path(path_str).read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            parsed = parse_page(source, kind, body, url=url, fetched_at=now_iso(),
                                snapshot_path=path_str, city=city)
            state = classify_page_state(body, url=url, status=200,
                                        title=parsed.get('title') or '',
                                        container_found=parsed.get('container_found', False),
                                        empty_marker=parsed.get('empty_marker', False),
                                        records=parsed.get('records') or [])
            merged, changes, dup_keys = _ingest(
                existing, cursor, source=source, city=city, url=url, final_url=url,
                url_class=url_class, parsed=parsed, state=state['state'],
                snapshot_path=path_str, sha=sha, fetched_at=now_iso())
            existing = merged
            _record_field_changes(out, changes)
            _record_duplicates(out, dup_keys)
            entry.update({'replayed': True, 'page_state': state['state'],
                          'reasons': state['reasons'], 'records_parsed': len(merged),
                          'parser_status': parsed.get('parser_status'), 'sha256': sha})
        entries.append(entry)
    write_jsonl(out / 'observations.jsonl', existing)
    _save_cursor(out, cursor)
    _write_batch(out, mode='replay', run_id=run_id, batch_id=run_id,
                 source=(entries[0]['source'] if entries else 'anjuke'), city=city,
                 entries=entries, recheck=[], stop_reason=None)
    _append_event(out, {'event': 'replay', 'city': city, 'entries': len(entries)})
    return {'mode': 'replay', 'out': str(out), 'city': city, 'entries': entries,
            'observations': len(existing), 'exit_code': 0}


# ---------------------------------------------------------------------------
# 9) CLI
# ---------------------------------------------------------------------------
def collect(*, source, city, url, url_class, out=DEFAULT_OUT, profile_dir=None,
            budget_ledger=DEFAULT_LEDGER, interval=DEFAULT_INTERVAL,
            max_requests=DEFAULT_MAX_REQUESTS, batch_id=None, stage='入口探测',
            browser='none', run_id=None, timeout=30.0, clock=None, sleep=None,
            transport=None, fetcher=None, headless=True, round_budget_path=None,
            round_business_cap=120, private_dir=None, ledger_required=False):
    return _execute(mode='collect', source=source, city=city, urls=[(url, url_class)],
                    out=out, profile_dir=profile_dir, budget_ledger=budget_ledger,
                    interval=interval, max_requests=max_requests, batch_id=batch_id,
                    stage=stage, browser=browser, run_id=run_id, timeout=timeout,
                    clock=clock, sleep=sleep, transport=transport, fetcher=fetcher,
                    headless=headless, round_budget_path=round_budget_path,
                    round_business_cap=round_business_cap, private_dir=private_dir,
                    ledger_required=ledger_required)


def resume(*, out=DEFAULT_OUT, budget_ledger=DEFAULT_LEDGER, interval=DEFAULT_INTERVAL,
           max_requests=DEFAULT_MAX_REQUESTS, browser='none', profile_dir=None,
           timeout=30.0, run_id=None, clock=None, sleep=None, transport=None, fetcher=None,
           round_budget_path=None, round_business_cap=120, private_dir=None,
           ledger_required=False):
    cursor = _load_cursor(out)
    source = (cursor or {}).get('source') or 'anjuke'
    city = (cursor or {}).get('city') or '北京'
    return _execute(mode='resume', source=source, city=city, urls=[], out=out,
                    profile_dir=profile_dir, budget_ledger=budget_ledger, interval=interval,
                    max_requests=max_requests, batch_id=(cursor or {}).get('batch_id'),
                    stage='续跑复查', browser=browser, run_id=run_id, timeout=timeout,
                    clock=clock, sleep=sleep, transport=transport, fetcher=fetcher,
                    round_budget_path=round_budget_path,
                    round_business_cap=round_business_cap, private_dir=private_dir,
                    ledger_required=ledger_required)


def pause(*, out, reason, url=None, clock=None, auto=False, until=None):
    """F02: 暂停写入 run_state/pause.json；激活期间 _execute/_collect_one 拒绝发请求。

    auto=True 表示挑战/限流自动暂停；until 为 epoch 秒，冷却到期后暂停自动失效。
    """
    out = Path(out)
    run_state = out / 'run_state'
    run_state.mkdir(parents=True, exist_ok=True)
    record = {'schema': PAUSE_SCHEMA, 'paused': True, 'reason': reason, 'url': url,
              'auto': bool(auto), 'until': until, 'at': now_iso()}
    aj._write_json(run_state / 'pause.json', record)
    _append_event(out, {'event': 'pause', 'reason': reason, 'url': url, 'auto': bool(auto)})
    if not auto:
        existing = _read_json(out / 'human_interventions.json', []) or []
        if not isinstance(existing, list):
            existing = []
        existing.append(record)
        aj._write_json(out / 'human_interventions.json', existing)
    return record


def status(*, out):
    out = Path(out)
    run_state = out / 'run_state'
    cursor = _read_json(run_state / 'cursor.json', {}) or {}
    return {'schema': STATUS_SCHEMA, 'out': str(out),
            'cursor': cursor,
            'status': _read_json(run_state / 'status.json', {}) or {},
            'pause': _read_json(run_state / 'pause.json', None),
            'batch': _read_json(run_state / 'batch.json', {}) or {},
            'requests': len(read_jsonl(out / 'requests.jsonl')),
            'observations': len(read_jsonl(out / 'observations.jsonl')),
            'updated_at': now_iso()}


def budget_report(*, budget_ledger=DEFAULT_LEDGER):
    ledger = _read_json(Path(budget_ledger), _default_ledger()) or _default_ledger()
    return {'ledger_path': str(budget_ledger), 'ledger': ledger}


def build_parser():
    parser = argparse.ArgumentParser(
        prog='anjuke_collect',
        description='可重复采集 CLI：浏览器/HTTP 分层、游标续跑、离线复放。')
    sub = parser.add_subparsers(dest='command', required=True)

    p_collect = sub.add_parser('collect', help='采集一个 URL：分类/解析/合并/写游标')
    p_collect.add_argument('--source', choices=['anjuke', 'fang'], required=True)
    p_collect.add_argument('--city', required=True)
    p_collect.add_argument('--url', required=True)
    p_collect.add_argument('--url-class', required=True)
    p_collect.add_argument('--out', default=DEFAULT_OUT)
    p_collect.add_argument('--profile-dir', default=None)
    p_collect.add_argument('--budget-ledger', default=DEFAULT_LEDGER)
    p_collect.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    p_collect.add_argument('--max-requests', type=int, default=DEFAULT_MAX_REQUESTS)
    p_collect.add_argument('--batch-id', default=None)
    p_collect.add_argument('--stage', default='入口探测')
    p_collect.add_argument('--browser', choices=['chromium', 'edge', 'chrome', 'none'], default='none')
    p_collect.add_argument('--headful', action='store_true',
                           help='有头浏览器；房天下挑战页需可见 Edge')
    p_collect.add_argument('--round-budget', default=None,
                           help='本轮业务请求预算文件（跨批共享，cap 强制）')
    p_collect.add_argument('--round-cap', type=int, default=120)
    p_collect.add_argument('--allow-missing-ledger', action='store_true',
                           help='允许缺失账本启动（仅离线诊断；实网默认禁止）')

    p_resume = sub.add_parser('resume', help='从 cursor 重试队列/未访问队列续跑并复查观测')
    p_resume.add_argument('--out', default=DEFAULT_OUT)
    p_resume.add_argument('--budget-ledger', default=DEFAULT_LEDGER)
    p_resume.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    p_resume.add_argument('--max-requests', type=int, default=DEFAULT_MAX_REQUESTS)
    p_resume.add_argument('--browser', choices=['chromium', 'edge', 'chrome', 'none'], default='none')
    p_resume.add_argument('--round-budget', default=None)
    p_resume.add_argument('--round-cap', type=int, default=120)
    p_resume.add_argument('--allow-missing-ledger', action='store_true')

    p_pause = sub.add_parser('pause', help='暂停：写入 run_state/pause.json')
    p_pause.add_argument('--out', default=DEFAULT_OUT)
    p_pause.add_argument('--reason', required=True)
    p_pause.add_argument('--url', default=None)

    p_status = sub.add_parser('status', help='打印运行状态')
    p_status.add_argument('--out', default=DEFAULT_OUT)

    p_replay = sub.add_parser('replay', help='离线复放已存快照（零网络）')
    p_replay.add_argument('--out', default=DEFAULT_OUT)
    p_replay.add_argument('--evidence-manifest', required=True)
    p_replay.add_argument('--city', required=True)

    p_budget = sub.add_parser('budget', help='打印共享预算账本')
    p_budget.add_argument('--budget-ledger', default=DEFAULT_LEDGER)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == 'collect':
        summary = collect(source=args.source, city=args.city, url=args.url,
                          url_class=args.url_class, out=args.out,
                          profile_dir=args.profile_dir, budget_ledger=args.budget_ledger,
                          interval=args.interval, max_requests=args.max_requests,
                          batch_id=args.batch_id, stage=args.stage, browser=args.browser,
                          headless=not args.headful, round_budget_path=args.round_budget,
                          round_business_cap=args.round_cap,
                          ledger_required=not args.allow_missing_ledger)
    elif args.command == 'resume':
        summary = resume(out=args.out, budget_ledger=args.budget_ledger,
                         interval=args.interval, max_requests=args.max_requests,
                         browser=args.browser, round_budget_path=args.round_budget,
                         round_business_cap=args.round_cap,
                         ledger_required=not args.allow_missing_ledger)
    elif args.command == 'pause':
        summary = pause(out=args.out, reason=args.reason, url=args.url)
    elif args.command == 'status':
        summary = status(out=args.out)
    elif args.command == 'replay':
        summary = replay(out=args.out, evidence_manifest=args.evidence_manifest,
                         city=args.city)
    else:
        summary = budget_report(budget_ledger=args.budget_ledger)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(summary.get('exit_code', 0))


if __name__ == '__main__':
    sys.exit(main())
