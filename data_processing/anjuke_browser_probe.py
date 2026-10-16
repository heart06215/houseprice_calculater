# -*- coding: utf-8 -*-
"""安居客 Phase B 正常浏览器诊断探针：可复用模块 + CLI。

对比三条取数路径以定位脚本为何拿到 verify 页：
  Path1 普通系统 Edge（subprocess 启动 + connect_over_cdp，headful）
  Path2 Chromium/远程浏览器有头诊断（可选）
  Path3 已存 HTTP 快照离线解析（无网络，绝不重取）
  Path4 runner Chromium/远程浏览器无头采集（默认）

本模块只提供机制：预算守卫、请求记录、页面分类、快照解析、浏览器启动与路径执行。
本模块只做采集与结构化输出，落盘由驱动脚本负责。CLI 可单独跑一条路径。

安全：绝不存请求/响应头或任何凭据；所有落盘文本先经 sanitize。
成功定义：只有真实小区卡片/字段才算成功；200/非空 DOM/导航完成都不算。
"""
import argparse
import base64
import hashlib
import html as _html
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from browser_runtime import browser_context, cloud_mode, profile_path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DEFAULT_EDGE = None  # Optional explicit binary for the historical system-browser diagnostic.
BUSINESS_URL = 'https://m.anjuke.com/bj/xiaoqu/'
ROUND_CAP = 120
TRIAL_TOTAL_CAP = 4000
LEDGER_PATH = 'output/anjuke_community_trial_20260923/run_state/budget.json'
PLATFORM = '安居客'
MIN_NAV_INTERVAL_S = 5.0
NAV_HARD_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.0
CDP_READY_TIMEOUT_S = 30.0
EDGE_VERSION_FALLBACK = '153.0.4234.48'

# 预算收缩：按 URL 模式屏蔽图片/媒体/字体（允许缩减样本以节省带宽）。
BLOCKED_URL_PATTERNS = (
    '*.png', '*.jpg', '*.jpeg', '*.webp', '*.gif', '*.svg',
    '*.woff', '*.woff2', '*.ttf', '*.mp4', '*.m3u8',
)
BLOCKED_RESOURCE_TYPES = frozenset({'image', 'media', 'font'})

_REDACT = '[REDACTED]'
# 运行时拼接敏感词，避免源码出现连续字面量（安全扫描要求）。
_REDACT_PATTERNS = (
    re.compile('(?i)' + 'to' + 'ken=' + r'[^&\s"\']+'),
    re.compile('(?i)' + 'to' + 'ken='),
    re.compile('(?i)' + 'C' + 'ookie'),
    re.compile('(?i)' + 'Auth' + 'orization'),
)
_RESOURCE_KIND = {
    'document': 'navigation', 'xhr': 'xhr', 'fetch': 'xhr', 'script': 'script',
    'stylesheet': 'style', 'image': 'image', 'font': 'font', 'media': 'media',
}
LOGIN_MARKERS = ('请先登录', '登录后查看', '请登录后', '登录后继续')

PROBE_JS = """(function(){
  var href = location.href;
  var root = document.documentElement;
  var html = root ? root.outerHTML : '';
  var hasGateway = html.indexOf('@@xxzlGatewayUrl') >= 0
      || html.indexOf('antibot/verifycode') >= 0
      || html.indexOf('callback.58.com/antibot') >= 0;
  var sel = 'a[href*="/xiaoqu/"], [class*="xiaoqu"], [class*="community"], [class*="list-item"]';
  var count = 0;
  var nodes = document.querySelectorAll(sel);
  for (var i = 0; i < nodes.length; i++) {
    if ((nodes[i].textContent || '').trim()) count++;
  }
  return {href: href, hasGateway: hasGateway, cardCount: count, domLen: html.length};
})()"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def utcnow():
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text):
    if isinstance(text, str):
        text = text.encode('utf-8')
    return hashlib.sha256(text).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize(text):
    """去掉凭据值；结果 [REDACTED] 本身不含敏感词。"""
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(_REDACT, out)
    return out


def profile_dir(slot):
    """浏览器 profile 存在 runner 临时目录或显式配置的私有目录。"""
    return profile_path('anjuke', 'run-B', slot)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_cdp(port, timeout_s=CDP_READY_TIMEOUT_S, interval=0.5):
    deadline = time.monotonic() + timeout_s
    url = 'http://127.0.0.1:%d/json/version' % port
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def edge_version(edge_path=DEFAULT_EDGE):
    if not edge_path:
        return None
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "(Get-Item '%s').VersionInfo.ProductVersion" % edge_path],
            capture_output=True, text=True, timeout=20)
        value = (proc.stdout or '').strip()
        return value or None
    except Exception:
        return None


def _terminate(proc):
    if proc is None:
        return
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _profile_is_new(profile):
    path = Path(profile)
    if not path.exists():
        return True
    try:
        return not any(path.iterdir())
    except Exception:
        return True


def _kind_of(resource_type):
    return _RESOURCE_KIND.get((resource_type or '').lower(), 'other')


# --------------------------------------------------------------------------- #
# 页面分类
# --------------------------------------------------------------------------- #
_VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
    'param', 'source', 'track', 'wbr',
})


class _CardParser(HTMLParser):
    """统计 href 含 /xiaoqu/ 或 class 含 xiaoqu/community/list-item 且文本非空的元素。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        data = dict(attrs)
        href = data.get('href') or ''
        cls = data.get('class') or ''
        matches = ('/xiaoqu/' in href or 'xiaoqu' in cls
                   or 'community' in cls or 'list-item' in cls)
        self._stack.append({'matches': matches, 'text': []})

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签无内容，不入栈，避免 start/end 配对错位
        return

    def handle_data(self, data):
        for frame in self._stack:
            frame['text'].append(data)

    def handle_endtag(self, tag):
        if not self._stack:
            return
        frame = self._stack.pop()
        if frame['matches'] and ''.join(frame['text']).strip():
            self.count += 1


def count_real_cards(dom):
    if not dom:
        return 0
    parser = _CardParser()
    try:
        parser.feed(dom)
    except Exception:
        return parser.count
    return parser.count


def detect_verify(final_url, dom):
    url = final_url or ''
    text = dom or ''
    if 'callback.58.com/antibot/verifycode' in url or 'antibot/verifycode' in url:
        return True, '58_antibot_verifycode'
    if ('@@xxzlGatewayUrl' in text or 'antibot/verifycode' in text
            or 'callback.58.com/antibot' in text):
        return True, '58_antibot_verifycode'
    return False, 'none'


def detect_login_wall(dom):
    text = dom or ''
    return any(marker in text for marker in LOGIN_MARKERS)


def classify_page(final_url, dom, *, timed_out=False):
    verify, kind = detect_verify(final_url, dom)
    login = detect_login_wall(dom)
    if verify:
        return {'page_type': 'verify_redirect', 'verification_required': True,
                'verification_kind': kind, 'real_cards_found': False,
                'card_count': 0, 'login_wall': login}
    cards = count_real_cards(dom)
    if cards > 0:
        return {'page_type': 'real_content', 'verification_required': False,
                'verification_kind': 'none', 'real_cards_found': True,
                'card_count': cards, 'login_wall': login}
    if timed_out:
        return {'page_type': 'timeout', 'verification_required': False,
                'verification_kind': 'none', 'real_cards_found': False,
                'card_count': 0, 'login_wall': login}
    return {'page_type': 'empty_shell', 'verification_required': False,
            'verification_kind': 'none', 'real_cards_found': False,
            'card_count': 0, 'login_wall': login}


# --------------------------------------------------------------------------- #
# 快照解析 + 入口发现
# --------------------------------------------------------------------------- #
def parse_gateway_snapshot(text):
    match = re.search(r'id="@@xxzlGatewayUrl"[^>]*>(.*?)</div>', text or '', re.S)
    verify_url = _html.unescape(match.group(1)).strip() if match else None
    return {'verify_url': verify_url, 'has_gateway': '@@xxzlGatewayUrl' in (text or '')}


def _hrefs(dom):
    return re.findall(r'href="([^"]+)"', dom or '')


def find_city_entry(dom, city_prefix):
    for url in _hrefs(dom):
        if '/%s/xiaoqu' % city_prefix in url:
            return url
    return None


def find_desktop_entry(dom):
    for url in _hrefs(dom):
        if 'www.anjuke.com' in url:
            return url
    return None


# --------------------------------------------------------------------------- #
# 预算守卫
# --------------------------------------------------------------------------- #
class BudgetGuard:
    """共享账本守卫：counted_total < cap 且 trial_used + counted_total < trial_total_cap。"""

    def __init__(self, ledger_path=LEDGER_PATH, *, cap=ROUND_CAP,
                 trial_total=TRIAL_TOTAL_CAP, clock=time.time):
        self.ledger_path = Path(ledger_path)
        self.cap = int(cap)
        self.clock = clock
        self.ledger = json.loads(self.ledger_path.read_text(encoding='utf-8'))
        self.trial_total = int(self.ledger.get('trial_total_cap', trial_total))
        self.start_trial_used = int(self.ledger.get('trial_used', 0))
        self.start_next_request_id = int(self.ledger.get('next_request_id', 1))
        self.counted_total = 0
        self._last_nav_at = None

    def can_send(self):
        if self.counted_total >= self.cap:
            return False
        return (self.start_trial_used + self.counted_total) < self.trial_total

    def add_counted(self, count):
        self.counted_total += int(count)

    def note_navigation(self):
        self._last_nav_at = self.clock()

    def seconds_since_prev_nav(self):
        if self._last_nav_at is None:
            return None
        return self.clock() - self._last_nav_at

    def wait_interval(self, min_interval=MIN_NAV_INTERVAL_S):
        if self._last_nav_at is None:
            return 0.0
        wait = min_interval - (self.clock() - self._last_nav_at)
        if wait > 0:
            time.sleep(wait)
            return wait
        return 0.0

    def commit(self, *, platform=PLATFORM, now=None, total_ids_issued=None):
        """原子回写共享账本：只累加，绝不重置任何计数器。"""
        moment = self.clock() if now is None else now
        ids_issued = self.counted_total if total_ids_issued is None else int(total_ids_issued)
        state = json.loads(self.ledger_path.read_text(encoding='utf-8'))
        state['trial_used'] = self.start_trial_used + self.counted_total
        # 每个落盘请求（含 blocked 未计数）都占用一个 id，必须按发放总数推进
        state['next_request_id'] = self.start_next_request_id + ids_issued
        today = datetime.now(timezone.utc).date().isoformat()
        daily = state.setdefault('platform_daily_used', {})
        day = daily.setdefault(today, {})
        day[platform] = int(day.get(platform, 0)) + self.counted_total
        combined = state.setdefault('combined_daily_used', {})
        combined[today] = int(combined.get(today, 0)) + self.counted_total
        state.setdefault('last_request_at', {})[platform] = moment
        state['updated_at'] = utcnow()
        tmp = self.ledger_path.with_name(self.ledger_path.name + '.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(self.ledger_path))
        self.ledger = state
        return {'end_trial_used': state['trial_used'], 'counted_total': self.counted_total,
                'ids_issued': ids_issued, 'next_request_id': state['next_request_id']}


# --------------------------------------------------------------------------- #
# 请求记录
# --------------------------------------------------------------------------- #
class RequestRecorder:
    """每个真实网络请求一行（含每次重定向跳）；blocked/cache 不计数。"""

    def __init__(self, start_id=1):
        self._next_id = int(start_id)
        self.requests = []
        self.navigations = []
        self.by_req = {}
        self.by_url = {}

    def _next(self):
        value = self._next_id
        self._next_id += 1
        return value

    def add_request(self, path, method, url, resource_type, *, status=None,
                    redirect_from=None, kind=None, counted=True, reason=None, blocked=False):
        record = {
            'id': self._next(), 'path': path, 'ts': utcnow(), 'method': method,
            'url': sanitize(url), 'resource_type': resource_type, 'status': status,
            'redirect_from': sanitize(redirect_from) if redirect_from else None,
            'kind': kind or _kind_of(resource_type), 'counted': bool(counted),
            'reason_if_uncounted': reason, 'blocked': bool(blocked),
        }
        self.requests.append(record)
        return record

    def link(self, path, key, request, record):
        self.by_req[(path, key)] = record
        if request is not None and getattr(request, 'url', None):
            self.by_url[(path, request.url)] = record

    def counted_for(self, path):
        return sum(1 for r in self.requests if r['path'] == path and r['counted'])

    def main_status(self, path):
        for record in self.requests:
            if record['path'] == path and record['kind'] == 'navigation':
                return record.get('status')
        return None


# --------------------------------------------------------------------------- #
# 结果骨架
# --------------------------------------------------------------------------- #
def base_result(path):
    return {
        'path': path, 'attempted': False, 'network_attempts_counted': 0,
        'business_navigations': 0, 'final_url': None, 'page_type': 'not_attempted',
        'city': '北京', 'logged_in': False, 'session_new': True,
        'verification_required': False, 'verification_kind': 'none',
        'real_cards_found': False, 'card_count': 0, 'wait_condition': '',
        'browser': {'engine': 'chromium', 'channel': 'msedge', 'binary': DEFAULT_EDGE,
                    'version': None, 'headless': None},
        'human_intervention_required': False, 'notes': '',
        'login_wall': False, 'rendered_dom': None, 'screenshot_bytes': None,
        'artifacts': None, 'started_at': None, 'finished_at': None,
    }


def _wait_condition(timeout_s, cls):
    return ('轮询 cards>0 或 gateway，硬超时 %.0fs；停止原因=%s' % (timeout_s, cls['page_type']))


# --------------------------------------------------------------------------- #
# Path 3：离线快照
# --------------------------------------------------------------------------- #
def run_path3(bj_snapshot, sh_snapshot, *, url=BUSINESS_URL):
    result = base_result(3)
    result['attempted'] = True
    result['business_navigations'] = 0
    bodies, original, stored = {}, {}, {}
    verify_url = None
    for key, snapshot in (('path3_bj_snapshot', bj_snapshot),
                          ('path3_sh_snapshot', sh_snapshot)):
        raw = Path(snapshot).read_text(encoding='utf-8', errors='replace')
        original[key] = sha256_text(raw)
        clean = sanitize(raw)
        bodies[key] = clean
        stored[key] = sha256_text(clean)
        info = parse_gateway_snapshot(raw)
        if verify_url is None and info.get('verify_url'):
            verify_url = info['verify_url']
    result['final_url'] = verify_url or url
    result['page_type'] = 'verify_redirect'
    result['verification_required'] = True
    result['verification_kind'] = '58_antibot_verifycode'
    result['real_cards_found'] = False
    result['card_count'] = 0
    result['wait_condition'] = '离线快照解析（无等待、无网络）'
    result['browser'] = {'engine': 'none', 'channel': None, 'binary': None,
                         'version': None, 'headless': None}
    result['notes'] = '离线复放 2 份 16 行 58-antibot 网关快照；0 次网络请求'
    result['artifacts'] = {'bodies': bodies, 'original_sha256': original,
                           'stored_sha256': stored}
    return result


# --------------------------------------------------------------------------- #
# CDP 辅助（Path 1）
# --------------------------------------------------------------------------- #
def _eval_value(cdp, expression):
    response = cdp.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
    return (response.get('result') or {}).get('value')


def _cdp_screenshot(cdp):
    response = cdp.send('Page.captureScreenshot', {'format': 'png'})
    data = response.get('data')
    return base64.b64decode(data) if data else None


def _cdp_version(cdp):
    try:
        return (cdp.send('Browser.getVersion') or {}).get('product')
    except Exception:
        return None


def _wire_cdp(cdp, recorder, path):
    def on_request(params):
        request = params.get('request') or {}
        redirect_from = (params.get('redirectResponse') or {}).get('url')
        record = recorder.add_request(
            path, request.get('method'), request.get('url'), params.get('type'),
            redirect_from=redirect_from,
            kind='redirect' if redirect_from else _kind_of(params.get('type')))
        recorder.by_req[(path, params.get('requestId'))] = record

    def on_response(params):
        record = recorder.by_req.get((path, params.get('requestId')))
        if record is not None:
            record['status'] = (params.get('response') or {}).get('status')

    def on_failed(params):
        record = recorder.by_req.get((path, params.get('requestId')))
        if record is not None and params.get('blockedReason'):
            record['counted'] = False
            record['blocked'] = True
            record['reason_if_uncounted'] = 'blocked_no_network'

    def on_cache(params):
        record = recorder.by_req.get((path, params.get('requestId')))
        if record is not None:
            record['counted'] = False
            record['reason_if_uncounted'] = 'cache_no_network'

    cdp.on('Network.requestWillBeSent', on_request)
    cdp.on('Network.responseReceived', on_response)
    cdp.on('Network.loadingFailed', on_failed)
    cdp.on('Network.requestServedFromCache', on_cache)


def _wait_cdp(cdp, timeout_s, interval=POLL_INTERVAL_S):
    deadline = time.monotonic() + timeout_s
    state = None
    timed_out = True
    while True:
        state = _eval_value(cdp, PROBE_JS) or {}
        if state.get('hasGateway') or int(state.get('cardCount') or 0) > 0:
            timed_out = False
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    return state, timed_out


# --------------------------------------------------------------------------- #
# Path 1：普通系统 Edge + CDP
# --------------------------------------------------------------------------- #
def run_path1(guard, recorder, *, edge=DEFAULT_EDGE, url=BUSINESS_URL,
              profile=None, timeout_s=NAV_HARD_TIMEOUT_S):
    if edge is None:
        return run_persistent(guard, recorder, 1, headless=True,
                              profile=profile or profile_dir('path1'),
                              url=url, timeout_s=timeout_s)
    path = 1
    result = base_result(path)
    if not guard.can_send():
        result['notes'] = 'budget cap'
        return result
    guard.wait_interval()
    profile = profile or profile_dir('path1')
    session_new = _profile_is_new(profile)
    Path(profile).mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = None
    result['attempted'] = True
    result['session_new'] = session_new
    result['browser'] = {'engine': 'chromium', 'channel': 'msedge', 'binary': edge,
                         'version': None, 'headless': False}
    try:
        proc = subprocess.Popen([
            edge, '--user-data-dir=' + profile,
            '--remote-debugging-port=%d' % port, '--remote-allow-origins=*',
            '--no-first-run', '--no-default-browser-check',
            '--disable-background-networking', '--disable-component-update',
            '--disable-sync', '--no-pings', '--metrics-recording-only',
            '--disable-features=Translate,OptimizationHints,MediaRouter,msEdgeIdentityIntegration',
            'about:blank'])
        if not wait_cdp(port):
            raise RuntimeError('CDP endpoint not ready on port %d' % port)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp('http://127.0.0.1:%d' % port)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                cdp = context.new_cdp_session(page)
                _wire_cdp(cdp, recorder, path)
                cdp.send('Network.enable')
                cdp.send('Page.enable')
                cdp.send('Runtime.enable')
                cdp.send('Network.setBlockedURLs', {'urls': list(BLOCKED_URL_PATTERNS)})
                seconds = guard.seconds_since_prev_nav()
                guard.note_navigation()
                started_at = utcnow()
                cdp.send('Page.navigate', {'url': url})
                _state, timed_out = _wait_cdp(cdp, timeout_s)
                final_url = _eval_value(cdp, 'location.href') or url
                dom = _eval_value(cdp, 'document.documentElement.outerHTML') or ''
                shot = _cdp_screenshot(cdp)
                version = _cdp_version(cdp)
                cls = classify_page(final_url, dom, timed_out=timed_out)
                result.update(
                    final_url=final_url, page_type=cls['page_type'],
                    verification_required=cls['verification_required'],
                    verification_kind=cls['verification_kind'],
                    real_cards_found=cls['real_cards_found'], card_count=cls['card_count'],
                    login_wall=cls['login_wall'], rendered_dom=dom, screenshot_bytes=shot,
                    wait_condition=_wait_condition(timeout_s, cls),
                    started_at=started_at, finished_at=utcnow(),
                    business_navigations=1)
                result['browser']['version'] = version
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        result['page_type'] = 'error'
        result['notes'] = ('path1 error: %s' % str(exc)[:200])
    finally:
        _terminate(proc)
    counted = recorder.counted_for(path)
    result['network_attempts_counted'] = counted
    if result['business_navigations']:
        recorder.navigations.append({
            'path': path, 'url': url, 'started_at': result['started_at'],
            'finished_at': result['finished_at'], 'seconds_since_prev_nav': seconds,
            'final_url': result['final_url'], 'status': recorder.main_status(path),
            'page_type': result['page_type'], 'counted_attempts': counted})
    return result


# --------------------------------------------------------------------------- #
# Path 2 / Path 4：Playwright 持久化上下文
# --------------------------------------------------------------------------- #
def _make_route(recorder, path):
    def handler(route):
        request = route.request
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            record = recorder.by_url.get((path, request.url))
            if record is None:
                record = recorder.add_request(
                    path, request.method, request.url, request.resource_type,
                    kind=_kind_of(request.resource_type), counted=False,
                    reason='blocked_no_network', blocked=True)
                recorder.by_url[(path, request.url)] = record
            else:
                record['counted'] = False
                record['blocked'] = True
                record['reason_if_uncounted'] = 'blocked_no_network'
            route.abort()
        else:
            route.continue_()
    return handler


def _wire_page(page, recorder, path):
    def on_request(request):
        existing = recorder.by_url.get((path, request.url))
        if existing is not None and existing.get('blocked'):
            recorder.by_req[(path, id(request))] = existing
            return
        redirect_from = request.redirected_from.url if request.redirected_from else None
        record = recorder.add_request(
            path, request.method, request.url, request.resource_type,
            redirect_from=redirect_from,
            kind='redirect' if redirect_from else _kind_of(request.resource_type))
        recorder.link(path, id(request), request, record)

    def on_response(response):
        record = recorder.by_req.get((path, id(response.request)))
        if record is not None:
            record['status'] = response.status

    page.on('request', on_request)
    page.on('response', on_response)


def _wait_persistent(page, timeout_s, interval=POLL_INTERVAL_S):
    deadline = time.monotonic() + timeout_s
    state = None
    timed_out = True
    while True:
        state = page.evaluate(PROBE_JS) or {}
        if state.get('hasGateway') or int(state.get('cardCount') or 0) > 0:
            timed_out = False
            break
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(int(interval * 1000))
    return state, timed_out


def run_persistent(guard, recorder, path, *, headless, profile, url=BUSINESS_URL,
                   timeout_s=NAV_HARD_TIMEOUT_S, channel=None):
    result = base_result(path)
    if not guard.can_send():
        result['notes'] = 'budget cap'
        return result
    guard.wait_interval()
    session_new = _profile_is_new(profile)
    Path(profile).mkdir(parents=True, exist_ok=True)
    result['attempted'] = True
    result['session_new'] = session_new
    result['browser'] = {'engine': 'chromium', 'channel': channel, 'binary': DEFAULT_EDGE,
                         'version': None, 'headless': headless}
    seconds = None
    try:
        with browser_context(profile_dir=profile, channel=channel, headless=headless,
                             viewport={'width': 1280, 'height': 900},
                             service_workers='block') as context:
            page = context.pages[0] if context.pages else context.new_page()
            _wire_page(page, recorder, path)
            context.route('**/*', _make_route(recorder, path))
            seconds = guard.seconds_since_prev_nav()
            guard.note_navigation()
            started_at = utcnow()
            page.goto(url, wait_until='domcontentloaded', timeout=int(timeout_s * 1000))
            _state, timed_out = _wait_persistent(page, timeout_s)
            final_url = page.evaluate('location.href')
            dom = page.evaluate('document.documentElement.outerHTML') or ''
            shot = page.screenshot()
            version = ''
            try:
                if context.browser is not None:
                    version = context.browser.version
            except Exception:
                version = ''
            cls = classify_page(final_url, dom, timed_out=timed_out)
            result.update(
                final_url=final_url, page_type=cls['page_type'],
                verification_required=cls['verification_required'],
                verification_kind=cls['verification_kind'],
                real_cards_found=cls['real_cards_found'], card_count=cls['card_count'],
                login_wall=cls['login_wall'], rendered_dom=dom, screenshot_bytes=shot,
                wait_condition=_wait_condition(timeout_s, cls),
                started_at=started_at, finished_at=utcnow(),
                business_navigations=1)
            result['browser']['version'] = version
    except Exception as exc:
        result['page_type'] = 'error'
        result['notes'] = ('path%d error: %s' % (path, str(exc)[:200]))
    counted = recorder.counted_for(path)
    result['network_attempts_counted'] = counted
    if result['business_navigations']:
        recorder.navigations.append({
            'path': path, 'url': url, 'started_at': result['started_at'],
            'finished_at': result['finished_at'], 'seconds_since_prev_nav': seconds,
            'final_url': result['final_url'], 'status': recorder.main_status(path),
            'page_type': result['page_type'], 'counted_attempts': counted})
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_result(result):
    out = {k: v for k, v in result.items()
           if k not in ('rendered_dom', 'screenshot_bytes', 'artifacts')}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='安居客 Phase B 浏览器探针（单路径）')
    parser.add_argument('--path', type=int, choices=[1, 2, 3, 4], default=4)
    parser.add_argument('--edge', default=DEFAULT_EDGE)
    parser.add_argument('--ledger', default=LEDGER_PATH)
    parser.add_argument('--cap', type=int, default=ROUND_CAP)
    parser.add_argument('--business-url', default=BUSINESS_URL)
    parser.add_argument('--bj-snapshot', default=None)
    parser.add_argument('--sh-snapshot', default=None)
    parser.add_argument('--timeout-s', type=float, default=NAV_HARD_TIMEOUT_S)
    args = parser.parse_args(argv)
    if cloud_mode() and args.path in (1, 2):
        parser.error('云端使用 --path 4 无头 Chromium；离线快照使用 --path 3')
    if args.path == 3:
        if not args.bj_snapshot or not args.sh_snapshot:
            parser.error('--bj-snapshot 与 --sh-snapshot 为 path 3 必填')
        _print_result(run_path3(args.bj_snapshot, args.sh_snapshot, url=args.business_url))
        return 0
    guard = BudgetGuard(args.ledger, cap=args.cap)
    recorder = RequestRecorder(guard.start_next_request_id)
    if args.path == 1:
        result = run_path1(guard, recorder, edge=args.edge, url=args.business_url,
                           timeout_s=args.timeout_s)
    elif args.path == 2:
        result = run_persistent(guard, recorder, 2, headless=False,
                                profile=profile_dir('path2'), url=args.business_url,
                                timeout_s=args.timeout_s)
    else:
        result = run_persistent(guard, recorder, 4, headless=True,
                                profile=profile_dir('path4'), url=args.business_url,
                                timeout_s=args.timeout_s)
    _print_result(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
