# -*- coding: utf-8 -*-
"""离线单测：anjuke_collect 采集 CLI（无网络、无真实 sleep、不碰共享账本）。

transport/clock/sleep 全部注入；每个测试在 tempfile.TemporaryDirectory() 内运行，`--out`
与 `--budget-ledger` 都指向临时路径，绝不读写 output/ 下的真实账本。
"""
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import requests  # noqa: E402  (import only; no request is ever sent)

import anjuke_collect as ac  # noqa: E402
import anjuke_community as aj  # noqa: E402
from source_pilot import read_jsonl  # noqa: E402

# ---------------------------------------------------------------------------
# 合成页面
# ---------------------------------------------------------------------------
CATALOG_HTML = (
    '<html><head><title>北京小区</title></head><body>'
    '<div class="list-wrap">共 2 个小区</div>'
    '<ul class="list-content">'
    '<li><a href="/bj/xiaoqu/111111/">甲小区</a></li>'
    '<li><a href="/bj/xiaoqu/222222/">乙小区</a></li>'
    '<li><a href="/bj/xiaoqu/p2/">下一页</a></li>'
    '<li><a href="/bj/xiaoqu/chaoyang/">朝阳</a></li>'
    '<li><a href="/sh/xiaoqu/">上海</a></li>'
    '</ul>'
    '<script>window.__INITIAL_STATE__ = {"data":{"list":[{"id":111111,"name":"甲小区",'
    '"url":"/bj/xiaoqu/111111/","district":"朝阳区","address":"朝阳路1号",'
    '"build_year":"2005年","type_tags":"公寓","lng":116.40,"lat":39.90,"on_sale":5}]}};</script>'
    '</body></html>')

DETAIL_HTML = (
    '<html><head><title>甲小区-安居客</title></head><body>'
    '<h1>甲小区</h1>'
    '<div>行政区：朝阳区 小区地址：朝阳路1号 建成于2005年 别墅 联排 在售5套 在租0套</div>'
    '<script>var info={"lng":116.40,"lat":39.90};</script>'
    '</body></html>')

CHALLENGE_HTML = (
    '<html><head><title>访问验证</title></head><body>请完成验证</body></html>')

LOGIN_HTML = (
    '<html><head><title>登录</title></head><body>请登录后继续</body></html>')

EMPTY_MARKER_HTML = (
    '<html><body><div class="list-content">暂无小区</div></body></html>')

RENDER_INCOMPLETE_HTML = (
    '<html><body><ul class="list-content"></ul></body></html>')

CATALOG_URL = 'https://m.anjuke.com/bj/xiaoqu/'
DETAIL_1 = 'https://m.anjuke.com/bj/xiaoqu/111111/'
DETAIL_2 = 'https://m.anjuke.com/bj/xiaoqu/222222/'


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self, start=1_800_000_000.0, step=6.0):
        self.now = start
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class FakeResponse:
    def __init__(self, status_code=200, body=b'', headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = dict(headers or {})


def ok(status=200, body=b'', headers=None):
    return {'response': FakeResponse(status, body, headers), 'error_type': None,
            'elapsed_ms': 1.0}


class RecordingTransport:
    """记录调用 URL；detail 链接返回详情页，其余返回目录页。"""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder

    def __call__(self, session, url, timeout):
        self.calls.append(url)
        if self.responder:
            return self.responder(url)
        if aj._is_community_detail_url(url):
            return ok(200, DETAIL_HTML.encode('utf-8'))
        return ok(200, CATALOG_HTML.encode('utf-8'))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / 'out'
        self.ledger = Path(self._tmp.name) / 'ledger.json'
        self.clock = FakeClock()
        self.sleep = lambda seconds: None

    def tearDown(self):
        self._tmp.cleanup()

    def phonebook(self):
        return read_jsonl(self.out / 'observations.jsonl')

    def first_collect(self, transport):
        return ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                          out=self.out, budget_ledger=self.ledger, transport=transport,
                          clock=self.clock, sleep=self.sleep)


# ---------------------------------------------------------------------------
# 1) 分类：5+ 状态
# ---------------------------------------------------------------------------
class ClassifyStateTest(unittest.TestCase):
    def _state(self, html, *, url=CATALOG_URL, status=200, container=False, empty=False,
               records=None, title=''):
        return ac.classify_page_state(html, url=url, status=status, title=title,
                                      container_found=container, empty_marker=empty,
                                      records=records or [])['state']

    def test_all_states_including_login_wall(self):
        observed = {
            self._state(CATALOG_HTML, container=True, records=[{'name': '甲'}]): None,
            self._state(CHALLENGE_HTML): None,
            self._state(LOGIN_HTML): None,
            self._state(EMPTY_MARKER_HTML, container=True, empty=True): None,
            self._state(RENDER_INCOMPLETE_HTML, container=True): None,
            self._state(b''): None,
            self._state('<html>ok</html>', url='https://m.anjuke.com/bj/fangjia/1/'): None,
        }
        self.assertEqual(observed, {
            'normal': None, 'verify_challenge': None, 'login_wall': None,
            'empty_directory': None, 'render_incomplete': None, 'parse_fail': None,
            'non_community': None})
        self.assertEqual(set(observed), set(ac.PAGE_STATES))

    def test_verify_challenge_wins_over_login_marker(self):
        html = '<html><body>请先登录 请完成验证</body></html>'
        self.assertEqual(self._state(html), 'verify_challenge')


# ---------------------------------------------------------------------------
# 2) parser_status 注册表
# ---------------------------------------------------------------------------
class ParserStatusTest(unittest.TestCase):
    def test_anjuke_detail_unverified_and_catalogs_verified(self):
        self.assertEqual(ac.parser_status('anjuke', 'detail'), '未实站验证')
        self.assertEqual(ac.parser_status('anjuke', 'catalog'), '实站验证')
        self.assertEqual(ac.parser_status('fang', 'catalog'), '实站验证')
        self.assertEqual(ac.parser_status('fang', 'detail'), '未实站验证')


# ---------------------------------------------------------------------------
# 3) 调度器：拒绝超上限（F01 反例：check 返回 (allowed, reason, request_id)）
# ---------------------------------------------------------------------------
class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / 'ledger.json'

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_business_nav_over_cap(self):
        scheduler = ac.Scheduler(self.ledger, run_id='t', run_business_nav_cap=1,
                                 clock=lambda: 1_800_000_000.0, sleep=lambda s: None)
        allowed, reason, request_id = scheduler.check('安居客', is_business_nav=True)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(request_id, 1)
        allowed, reason, _ = scheduler.check('安居客', is_business_nav=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'run_business_nav_cap')
        self.assertEqual(scheduler.breaker.counted_total, 1)

    def test_refuses_per_source_over_cap(self):
        today = ac._date_of(1_800_000_000.0)
        self.ledger.write_text(
            json.dumps({'trial_used': 0, 'platform_daily_used': {today: {'安居客': 299}}}),
            encoding='utf-8')
        scheduler = ac.Scheduler(self.ledger, run_id='t', per_source_daily_cap=300,
                                 clock=lambda: 1_800_000_000.0, sleep=lambda s: None)
        self.assertTrue(scheduler.check('安居客')[0])
        allowed, reason, _ = scheduler.check('安居客')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'per_source_daily_cap')

    def test_request_ids_unique_and_persisted(self):
        # F01 反例：修复前 request_id=null；修复后每次尝试唯一且回写 next_request_id。
        scheduler = ac.Scheduler(self.ledger, run_id='t',
                                 clock=lambda: 1_800_000_000.0, sleep=lambda s: None)
        ids = [scheduler.check('安居客')[2] for _ in range(3)]
        self.assertEqual(ids, [1, 2, 3])
        scheduler.persist()
        data = json.loads(self.ledger.read_text(encoding='utf-8'))
        self.assertEqual(data['next_request_id'], 4)

    def test_round_budget_enforced_across_instances(self):
        # F01/F02 反例：本轮业务请求上限跨进程共享，不随进程重置。
        round_path = Path(self._tmp.name) / 'round_budget.json'
        first = ac.Scheduler(self.ledger, run_id='t1', round_budget_path=round_path,
                             round_business_cap=2, clock=lambda: 1_800_000_000.0,
                             sleep=lambda s: None)
        for _ in range(2):
            self.assertTrue(first.check('安居客', is_business_nav=True)[0])
        first.persist()
        second = ac.Scheduler(self.ledger, run_id='t2', round_budget_path=round_path,
                              round_business_cap=2, clock=lambda: 1_800_000_000.0,
                              sleep=lambda s: None)
        allowed, reason, _ = second.check('安居客', is_business_nav=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'round_business_cap')

    def test_platform_paused_blocks_check(self):
        # F02 反例：账本 platform_paused 未到期时 check 直接拒绝。
        self.ledger.write_text(json.dumps({
            'trial_used': 0,
            'platform_paused': {'安居客': {'reason': 'verify_challenge',
                                          'until': 1_800_000_100.0}}}), encoding='utf-8')
        scheduler = ac.Scheduler(self.ledger, run_id='t',
                                 clock=lambda: 1_800_000_050.0, sleep=lambda s: None)
        allowed, reason, _ = scheduler.check('安居客')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'platform_paused')


# ---------------------------------------------------------------------------
# 4) 采集 / 失败不覆盖 / 身份合并
# ---------------------------------------------------------------------------
class CollectTest(Base):
    def test_failure_never_overwrites_prior_valid_observation(self):
        self.first_collect(RecordingTransport())
        prior = self.phonebook()
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]['name'], '甲小区')

        def refuse(url):
            return ok(403, CHALLENGE_HTML.encode('utf-8'))

        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out, budget_ledger=self.ledger,
                             transport=RecordingTransport(responder=refuse),
                             clock=self.clock, sleep=self.sleep)
        after = self.phonebook()
        self.assertEqual(summary['entries'][0]['page_state'], 'verify_challenge')
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['name'], '甲小区')
        self.assertEqual(after[0]['page_state'], 'normal')

    def test_identity_merge_keeps_stable_id(self):
        self.first_collect(RecordingTransport())
        self.first_collect(RecordingTransport())
        observations = self.phonebook()
        self.assertEqual(len(observations), 1)
        self.assertEqual(aj.identity_key(observations[0]), '111111')


# ---------------------------------------------------------------------------
# 4b) F01 反例：fang 重定向逐跳手动跟随并逐跳预留；anjuke 路径保持不跟随
# ---------------------------------------------------------------------------
class _RedirectResponse:
    def __init__(self, status_code=200, body=b'', headers=None, url='', location=None):
        self.status_code = status_code
        self.content = body
        self.headers = dict(headers or {})
        if location:
            self.headers['Location'] = location
        self.url = url


class _FakeRedirectSession:
    """按队列返回响应；记录每次 get 的 allow_redirects 参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class RedirectFetchTest(unittest.TestCase):
    def test_fang_redirect_hops_manually_with_gate(self):
        # F01 反例：修复前 allow_redirects=True 自动跟随、整次只计一次；
        # 修复后每跳发出前经 hop_reserve 预留并各自计数。
        session = _FakeRedirectSession([
            _RedirectResponse(status_code=302, location='https://sh.esf.fang.com/housing/b/'),
            _RedirectResponse(status_code=200, body=b'<html>fang</html>',
                              headers={'Retry-After': '7'}),
        ])
        hops = []

        def hop_reserve(url):
            hops.append(url)
            return {'request_id': 100 + len(hops), 'requested_url': url}

        fetched = ac._fetch('https://sh.esf.fang.com/housing/', browser='none',
                            profile_dir=None, transport=None, fetcher=None,
                            session=session, timeout=10.0, follow_redirects=True,
                            hop_reserve=hop_reserve)
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(session.calls[0][1].get('allow_redirects'))
        self.assertEqual(hops, ['https://sh.esf.fang.com/housing/b/'])
        self.assertEqual(fetched['final_url'], 'https://sh.esf.fang.com/housing/b/')
        self.assertEqual(fetched['status'], 200)
        self.assertEqual(fetched['body'], b'<html>fang</html>')
        self.assertEqual(fetched['retry_after'], '7')
        self.assertEqual(fetched['redirect_chain'],
                         ['https://sh.esf.fang.com/housing/'])
        self.assertIsNone(fetched['error_type'])

    def test_redirect_stops_when_budget_refuses_hop(self):
        # F01 反例：额度拒绝后不再发出下一跳请求（熔断）。
        session = _FakeRedirectSession([
            _RedirectResponse(status_code=302, location='https://sh.esf.fang.com/b/'),
        ])

        fetched = ac._fetch('https://sh.esf.fang.com/housing/', browser='none',
                            profile_dir=None, transport=None, fetcher=None,
                            session=session, timeout=10.0, follow_redirects=True,
                            hop_reserve=lambda url: None)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(fetched['error_type'], 'redirect_budget_exhausted')
        self.assertEqual(fetched['redirect_chain'], ['https://sh.esf.fang.com/housing/'])

    def test_anjuke_path_still_no_redirect_follow(self):
        session = _FakeRedirectSession([
            _RedirectResponse(status_code=302, body=b'nginx redirect',
                              location='https://m.anjuke.com/bj/xiaoqu/'),
        ])
        ac._fetch('https://m.anjuke.com/bj/xiaoqu/', browser='none', profile_dir=None,
                  transport=None, fetcher=None, session=session, timeout=10.0,
                  follow_redirects=False)
        self.assertEqual(len(session.calls), 1)


# ---------------------------------------------------------------------------
# 5) 游标写 + resume 跳过 visited
# ---------------------------------------------------------------------------
class CursorResumeTest(Base):
    def test_cursor_write_then_resume_skips_visited(self):
        self.first_collect(RecordingTransport())
        cursor_path = self.out / 'run_state' / 'cursor.json'
        cursor = json.loads(cursor_path.read_text(encoding='utf-8'))
        self.assertIn(CATALOG_URL, cursor['visited'])
        self.assertIn(DETAIL_1, cursor['next_unvisited'])

        transport = RecordingTransport()
        summary = ac.resume(out=self.out, budget_ledger=self.ledger, max_requests=2,
                            transport=transport, clock=self.clock, sleep=self.sleep)
        self.assertNotIn(CATALOG_URL, transport.calls)
        self.assertEqual(transport.calls, [DETAIL_1, DETAIL_2])
        self.assertEqual(summary['exit_code'], 0)
        self.assertTrue(summary['recheck'])

        cursor = json.loads(cursor_path.read_text(encoding='utf-8'))
        self.assertIn(DETAIL_1, cursor['visited'])
        self.assertIn(DETAIL_2, cursor['visited'])
        self.assertNotIn(DETAIL_1, cursor['next_unvisited'])
        self.assertNotIn(DETAIL_2, cursor['next_unvisited'])

        batch = json.loads((self.out / 'run_state' / 'batch.json').read_text(encoding='utf-8'))
        self.assertEqual(batch['mode'], 'resume')
        self.assertIn('recheck', batch)


# ---------------------------------------------------------------------------
# 6) 离线复放：零网络
# ---------------------------------------------------------------------------
class ReplayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / 'out'
        snaps = self.out / 'snapshots'
        snaps.mkdir(parents=True)
        sha = hashlib.sha256(CATALOG_HTML.encode('utf-8')).hexdigest()
        self.snap = snaps / f'{sha}.html'
        self.snap.write_text(CATALOG_HTML, encoding='utf-8')
        self.manifest = Path(self._tmp.name) / 'manifest.json'
        self.manifest.write_text(json.dumps([
            {'city': '北京', 'source': 'anjuke', 'url': CATALOG_URL,
             'url_class': 'catalog', 'kind': 'catalog', 'path': str(self.snap)},
        ], ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def test_replay_makes_zero_network_calls(self):
        raising = AssertionError('network attempted during offline replay')
        with mock.patch('socket.socket', side_effect=raising), \
                mock.patch.object(requests, 'get', side_effect=raising), \
                mock.patch.object(requests.Session, 'get', side_effect=raising):
            summary = ac.replay(out=self.out, evidence_manifest=self.manifest, city='北京')
        self.assertEqual(summary['exit_code'], 0)
        self.assertEqual(len(read_jsonl(self.out / 'observations.jsonl')), 1)
        self.assertTrue((self.out / 'run_state' / 'cursor.json').exists())


# ---------------------------------------------------------------------------
# 7) pause / status
# ---------------------------------------------------------------------------
class PauseStatusTest(Base):
    def test_pause_and_status_artifacts(self):
        ac.pause(out=self.out, reason='verify_challenge', url=CATALOG_URL)
        self.assertTrue((self.out / 'run_state' / 'pause.json').exists())
        self.assertTrue((self.out / 'human_interventions.json').exists())
        report = ac.status(out=self.out)
        self.assertTrue(report['pause']['paused'])
        self.assertEqual(report['pause']['reason'], 'verify_challenge')


# ---------------------------------------------------------------------------
# 8) CLI headful / browser 透传（不启动真实浏览器）
# ---------------------------------------------------------------------------
class HeadfulCliTest(Base):
    def test_collect_headful_parses_and_default_stays_headless(self):
        parser = ac.build_parser()
        base = ['collect', '--source', 'fang', '--city', '上海',
                '--url', 'https://sh.esf.fang.com/housing/', '--url-class', 'catalog']
        self.assertFalse(parser.parse_args(base).headful)
        self.assertTrue(parser.parse_args(base + ['--headful']).headful)
        resume_args = parser.parse_args(['resume'])
        self.assertEqual(resume_args.browser, 'none')
        self.assertEqual(parser.parse_args(['resume', '--browser', 'edge']).browser, 'edge')

    def test_collect_threads_headless_to_browserfetcher(self):
        captured = {}

        class FakeFetcher:
            def __init__(self, profile_dir, *, browser='edge', timeout_s=30.0, headless=True,
                         request_gate=None, source=None, city=None):
                captured['headless'] = headless

            def fetch(self, url):
                return {'status': 200, 'title': '', 'final_url': url,
                        'body': CATALOG_HTML.encode('utf-8'), 'elapsed_ms': 1,
                        'error_type': None, 'retry_after': None}

            def close(self):
                pass

        with mock.patch.object(ac, 'BrowserFetcher', FakeFetcher):
            ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                       out=self.out, budget_ledger=self.ledger, browser='edge',
                       clock=self.clock, sleep=self.sleep)
            self.assertTrue(captured['headless'])
            ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                       out=self.out, budget_ledger=self.ledger, browser='edge',
                       headless=False, clock=self.clock, sleep=self.sleep)
            self.assertFalse(captured['headless'])


# ---------------------------------------------------------------------------
# 9) F02 反例：挑战停源、暂停拦截、失败URL重试、退出码、冷却到期恢复
# ---------------------------------------------------------------------------
class ChallengeStopTest(Base):
    def _challenge_then_ok(self):
        return RecordingTransport(responder=lambda url: (
            ok(200, CHALLENGE_HTML.encode('utf-8'))
            if url == CATALOG_URL else ok(200, DETAIL_HTML.encode('utf-8'))))

    def test_challenge_stops_source_and_auto_pauses(self):
        # 修复前：429/挑战后仍连续访问；修复后：挑战即停当前来源并自动暂停。
        transport = self._challenge_then_ok()
        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out,
                             budget_ledger=self.ledger, transport=transport,
                             clock=self.clock, sleep=self.sleep)
        self.assertEqual(summary['entries'][0]['page_state'], 'verify_challenge')
        self.assertEqual(summary['exit_code'], ac.EXIT_BLOCKED)
        self.assertEqual(summary['state'], 'needs_external')
        pause = json.loads((self.out / 'run_state' / 'pause.json').read_text('utf-8'))
        self.assertTrue(pause['paused'])
        self.assertTrue(pause['auto'])
        ledger = json.loads(self.ledger.read_text('utf-8'))
        self.assertIn('安居客', ledger.get('platform_paused', {}))
        # 暂停后同一批中的下一个 URL 不再发出网络请求。
        self.assertEqual(len(transport.calls), 1)

    def test_pause_blocks_subsequent_runs(self):
        # 修复前：pause() 后续跑仍发请求；修复后：暂停激活时拒绝并计 0 次。
        ac.pause(out=self.out, reason='verify_challenge')
        transport = RecordingTransport()
        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out,
                             budget_ledger=self.ledger, transport=transport,
                             clock=self.clock, sleep=self.sleep)
        self.assertEqual(summary['entries'][0]['refused'], True)
        self.assertEqual(summary['entries'][0]['reason'], 'paused')
        self.assertEqual(transport.calls, [])
        self.assertEqual(summary['exit_code'], ac.EXIT_BLOCKED)

    def test_cooldown_expiry_resumes(self):
        # 自动暂停带冷却期；到期后新一批可正常发请求。
        ac.pause(out=self.out, reason='verify_challenge', auto=True, until=100.0)
        self.clock.now = 200.0  # FakeClock：让 clock() 返回 200 > until
        transport = RecordingTransport()
        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out,
                             budget_ledger=self.ledger, transport=transport,
                             clock=self.clock, sleep=self.sleep)
        self.assertEqual(summary['entries'][0]['page_state'], 'normal')

    def test_failed_url_goes_to_retry_queue_not_visited(self):
        # 修复前：失败URL被标为 visited；修复后：失败进入 retry_queue，可续跑重试。
        ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                   out=self.out, budget_ledger=self.ledger,
                   transport=RecordingTransport(responder=lambda url: ok(
                       403, CHALLENGE_HTML.encode('utf-8'))),
                   clock=self.clock, sleep=self.sleep)
        cursor = json.loads((self.out / 'run_state' / 'cursor.json').read_text('utf-8'))
        self.assertNotIn(CATALOG_URL, cursor['visited'])
        self.assertIn(CATALOG_URL, cursor['retry_queue'])
        # 挑战也自动暂停；清除暂停后 resume 从 retry_queue 重试成功并移出。
        (self.out / 'run_state' / 'pause.json').unlink()
        ledger = json.loads(self.ledger.read_text('utf-8'))
        ledger.pop('platform_paused', None)
        self.ledger.write_text(json.dumps(ledger), encoding='utf-8')
        self.clock.now += 7200.0
        summary = ac.resume(out=self.out, budget_ledger=self.ledger,
                            transport=RecordingTransport(), clock=self.clock,
                            sleep=self.sleep)
        self.assertEqual(summary['entries'][0]['page_state'], 'normal')
        cursor = json.loads((self.out / 'run_state' / 'cursor.json').read_text('utf-8'))
        self.assertIn(CATALOG_URL, cursor['visited'])
        self.assertNotIn(CATALOG_URL, cursor['retry_queue'])


class ExitCodeTest(Base):
    def test_exit_codes_express_real_outcomes(self):
        # 全部正常 -> 0
        summary = self.first_collect(RecordingTransport())
        self.assertEqual(summary['exit_code'], ac.EXIT_OK)
        # 传输失败 -> 2（部分）
        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out,
                             budget_ledger=self.ledger,
                             transport=RecordingTransport(responder=lambda url: {
                                 'response': None, 'error_type': 'ConnectTimeout',
                                 'elapsed_ms': 1}),
                             clock=self.clock, sleep=self.sleep)
        self.assertEqual(summary['exit_code'], ac.EXIT_PARTIAL)
        # 本轮预算耗尽 -> 3
        round_path = self.out.parent / 'round.json'
        round_path.write_text(json.dumps({'round': 't', 'cap': 0, 'business_used': 0}),
                              encoding='utf-8')
        summary = ac.collect(source='anjuke', city='北京', url=CATALOG_URL,
                             url_class='catalog', out=self.out,
                             budget_ledger=self.ledger, transport=RecordingTransport(),
                             clock=self.clock, sleep=self.sleep,
                             round_budget_path=str(round_path))
        self.assertEqual(summary['exit_code'], ac.EXIT_BUDGET)


class LedgerReconcileTest(Base):
    def test_request_log_reconstructs_ledger(self):
        # F01 反例：请求日志能还原账本 —— counted 行数与账本增量一致，ID 唯一。
        self.first_collect(RecordingTransport())
        rows = read_jsonl(self.out / 'requests.jsonl')
        counted = [row for row in rows if row.get('counted')]
        self.assertTrue(counted)
        ledger = json.loads(self.ledger.read_text('utf-8'))
        before = json.loads((self.out / 'run_state' / 'ledger_before.json')
                            .read_text('utf-8'))['trial_used']
        self.assertEqual(ledger['trial_used'] - before, len(counted))
        ids = [row['request_id'] for row in counted]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i is not None for i in ids))


# ---------------------------------------------------------------------------
# 10) F03 反例：合成令牌页面 —— 交付副本脱敏、私有原件保留、双哈希记录
# ---------------------------------------------------------------------------
TOKEN_PAGE = ('<html><head><title>t</title></head><body>'
              '<a href="https://m.anjuke.com/bj/xiaoqu/111111/">'
              '?token=abc123secret&sessionid=XYZ987&ok=1</a>'
              '<script>var cfg={"access_token":"SkJw9xSecretValue123","k":1};</script>'
              '</body></html>')


class SanitizeDeliveryTest(Base):
    def test_delivery_copy_sanitized_original_private(self):
        transport = RecordingTransport(responder=lambda url: ok(
            200, TOKEN_PAGE.encode('utf-8')))
        ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                   out=self.out, budget_ledger=self.ledger, transport=transport,
                   clock=self.clock, sleep=self.sleep)
        snaps = sorted((self.out / 'snapshots').glob('*.html'))
        self.assertTrue(snaps)
        delivered = snaps[0].read_text('utf-8')
        self.assertNotIn('abc123secret', delivered)
        self.assertNotIn('XYZ987', delivered)
        self.assertNotIn('SkJw9xSecretValue123', delivered)
        self.assertIn('111111', delivered)  # 业务内容保留
        # 私有原件在本目录之外（out.name + '-private'）
        private_dir = self.out.parent / (self.out.name + '-private')
        originals = sorted(private_dir.glob('*.html'))
        self.assertTrue(originals)
        original = originals[0].read_text('utf-8')
        self.assertIn('abc123secret', original)
        # 证据索引记录双哈希且不相等
        evidence = read_jsonl(self.out / 'evidence_index.jsonl')
        self.assertTrue(evidence)
        row = evidence[-1]
        self.assertNotEqual(row['original_sha256'], row['sanitized_sha256'])
        self.assertEqual(row['bytes_original'], len(TOKEN_PAGE.encode('utf-8')))

    def test_redact_query_known_params(self):
        url = 'https://m.anjuke.com/path/?token=SECRET1&serialid=SER2&page=2&q=abc'
        out = ac._redact_query(url)
        self.assertNotIn('SECRET1', out)
        self.assertNotIn('SER2', out)
        self.assertIn('page=2', out)
        self.assertIn('q=abc', out)

    def test_delivery_tree_has_no_token_marker(self):
        transport = RecordingTransport(responder=lambda url: ok(
            200, TOKEN_PAGE.encode('utf-8')))
        ac.collect(source='anjuke', city='北京', url=CATALOG_URL, url_class='catalog',
                   out=self.out, budget_ledger=self.ledger, transport=transport,
                   clock=self.clock, sleep=self.sleep)
        marker = 'SkJw9xSecretValue123'
        for path in self.out.rglob('*'):
            if path.is_file() and path.suffix in ('.html', '.json', '.jsonl'):
                self.assertNotIn(marker, path.read_text('utf-8', errors='replace'),
                                 str(path))


# ---------------------------------------------------------------------------
# 11) F01 反例：浏览器 route 门 —— 子资源逐请求预留与策略屏蔽
# ---------------------------------------------------------------------------
class FakeRouteRequest:
    def __init__(self, url, resource_type):
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self):
        self.aborted = []
        self.continued = []

    def abort(self, reason=None):
        self.aborted.append(reason)

    def continue_(self):
        self.continued.append(True)


class BrowserRouteGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / 'ledger.json'
        self.out = Path(self._tmp.name) / 'out'
        self.scheduler = ac.Scheduler(self.ledger, run_id='t', clock=lambda: 100.0,
                                      sleep=lambda s: None)
        self.gate = ac.RequestGate(self.scheduler, self.out, batch_id='b', stage='s')

    def tearDown(self):
        self._tmp.cleanup()

    def _fetcher(self):
        return ac.BrowserFetcher('profile', request_gate=self.gate, source='anjuke',
                                 city='北京')

    def test_image_aborted_without_counting(self):
        fetcher = self._fetcher()
        route, request = FakeRoute(), FakeRouteRequest('https://x/a.png', 'image')
        fetcher._on_route(route, request)
        self.assertEqual(route.aborted, ['resource_policy'])
        self.assertEqual(self.scheduler.breaker.counted_total, 0)
        self.assertEqual(self.gate.rows, [])

    def test_subresource_counted_before_send(self):
        fetcher = self._fetcher()
        route, request = FakeRouteRequest('https://m.anjuke.com/bj/xiaoqu/api.js', 'script'), None
        route = FakeRoute()
        fetcher._nav = {'url': 'https://m.anjuke.com/bj/xiaoqu/', 'documents_seen': 1}
        fetcher._on_route(route, FakeRouteRequest('https://m.anjuke.com/api.js', 'script'))
        self.assertEqual(route.continued, [True])
        self.assertEqual(self.scheduler.breaker.counted_total, 1)
        row = self.gate.rows[0]
        self.assertEqual(row['resource_type'], 'subresource')
        self.assertTrue(row['counted'])
        self.assertIsNotNone(row['request_id'])

    def test_budget_exhaustion_aborts_subresources(self):
        # F01 反例：子资源额度耗尽后熔断 —— 后续请求全部 abort 且不计数。
        today = ac._date_of(100.0)
        self.ledger.write_text(json.dumps({
            'trial_used': 0, 'platform_daily_used': {today: {'安居客': 300}}}),
            encoding='utf-8')
        scheduler = ac.Scheduler(self.ledger, run_id='t', clock=lambda: 100.0,
                                 sleep=lambda s: None)
        gate = ac.RequestGate(scheduler, self.out, batch_id='b', stage='s')
        fetcher = ac.BrowserFetcher('profile', request_gate=gate, source='anjuke',
                                    city='北京')
        fetcher._nav = {'url': 'https://m.anjuke.com/bj/xiaoqu/', 'documents_seen': 1}
        route = FakeRoute()
        fetcher._on_route(route, FakeRouteRequest('https://m.anjuke.com/api.js', 'xhr'))
        self.assertEqual(route.aborted, ['budget'])
        self.assertEqual(route.continued, [])
        self.assertEqual(scheduler.breaker.counted_total, 0)

    def test_tracker_blocked(self):
        fetcher = self._fetcher()
        route = FakeRoute()
        fetcher._on_route(route, FakeRouteRequest(
            'https://hm.baidu.com/hm.js?x', 'script'))
        self.assertEqual(route.aborted, ['resource_policy'])


class LedgerIntegrityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / 'ledger.json'
        self.out = Path(self._tmp.name) / 'out'

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_ledger_refuses_network_when_required(self):
        # F02 反例：缺账本时禁止从 0 开始实网。
        scheduler = ac.Scheduler(self.ledger, run_id='t', clock=lambda: 100.0,
                                 sleep=lambda s: None, ledger_required=True)
        allowed, reason, _ = scheduler.check('安居客')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'ledger_missing')

    def test_corrupt_ledger_refuses_network_when_required(self):
        # F02 反例：账本损坏时同样拒绝实网。
        self.ledger.write_text('{not json', encoding='utf-8')
        scheduler = ac.Scheduler(self.ledger, run_id='t', clock=lambda: 100.0,
                                 sleep=lambda s: None, ledger_required=True)
        allowed, reason, _ = scheduler.check('安居客')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'ledger_corrupt')

    def test_missing_ledger_allowed_when_not_required(self):
        scheduler = ac.Scheduler(self.ledger, run_id='t', clock=lambda: 100.0,
                                 sleep=lambda s: None)
        self.assertTrue(scheduler.check('安居客')[0])


class TwoProcessRaceTest(unittest.TestCase):
    def test_concurrent_persists_do_not_lose_counts(self):
        # 6.2 反例：两个进程竞争 —— 先后 persist 的计数取单调 max，不互相覆盖。
        tmp = tempfile.TemporaryDirectory()
        try:
            ledger = Path(tmp.name) / 'ledger.json'
            a = ac.Scheduler(ledger, run_id='a', clock=lambda: 100.0, sleep=lambda s: None)
            b = ac.Scheduler(ledger, run_id='b', clock=lambda: 100.0, sleep=lambda s: None)
            # 两个进程各自计数（基于同一基线快照）
            a.check('安居客')
            b.check('安居客')
            b.persist()
            a.persist()
            data = json.loads(ledger.read_text('utf-8'))
            self.assertEqual(data['trial_used'], 2)
            self.assertEqual(data['next_request_id'], 3)
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
