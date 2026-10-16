"""Offline unit tests for the 安居客 community adapter (data_processing/anjuke_community.py).

No network and no real sleep: ``transport`` and ``clock`` are injected stubs and the module's
``_sleep`` hook is patched to a no-op, so no call ever waits or sends a request. ``GLOBAL_LEDGER``
is redirected to a temp ledger so the real shared budget is never read or written, and every test
works inside ``tempfile.TemporaryDirectory()`` (never under output/anjuke_community_trial_20260923).
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import anjuke_community as aj
import source_pilot as sp


# ---------------------------------------------------------------------------
# synthetic pages
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

DETAIL_HTML_RANGE = (
    '<html><head><title>丙小区</title></head><body>'
    '<h1>丙小区</h1>'
    '<div>建成2005-2010年 别墅 联排</div>'
    '</body></html>')

DETAIL_HTML_NO_COORD = (
    '<html><head><title>乙小区</title></head><body>'
    '<h1>乙小区</h1>'
    '<div>行政区：海淀区 小区地址：海淀路2号 2010年建成 公寓</div>'
    '</body></html>')

CHALLENGE_HTML = (
    '<html><head><title>访问验证</title></head><body>请完成验证</body></html>')

EMPTY_MARKER_HTML = (
    '<html><body><div class="list-content">暂无小区</div></body></html>')

# The 58/安居客 anti-bot gateway shell: hidden callback URL plus a JS redirect.
GATEWAY_SHELL = (
    '<html><head><title>正在验证</title></head><body>'
    '<div id="@@xxzlGatewayUrl">'
    'https://callback.58.com/antibot/verifycode?serialId=abc123'
    '&namespace=cloud_ajk_tw_usersite_home'
    '&url=https%3A%2F%2Fm.anjuke.com%2Fbj%2Fxiaoqu%2F'
    '</div>'
    '<script>var u = document.getElementById("@@xxzlGatewayUrl").innerText;'
    'window.location.assign(u);</script>'
    '</body></html>')


def catalog_or_detail(url):
    if re.search(r'/xiaoqu/\d{3,}/', url):
        return ok(200, DETAIL_HTML.encode('utf-8'))
    return ok(200, CATALOG_HTML.encode('utf-8'))


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeClock:
    """Monotonic fake clock; each call advances past the 5s interval so no wait is needed."""

    def __init__(self, start=1_800_000_000.0, step=6.0):
        self.now = start
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds):
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code=200, body=b'', headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = dict(headers or {})


def ok(status=200, body=b'', headers=None):
    return {'response': FakeResponse(status, body, headers), 'error_type': None,
            'elapsed_ms': 1.0}


def redirect(location, status=302):
    return {'response': FakeResponse(status, b'', {'Location': location}),
            'error_type': None, 'elapsed_ms': 1.0}


def err(kind='ConnectionError'):
    return {'response': None, 'error_type': kind, 'elapsed_ms': 1.0}


class MapTransport:
    """Pops queued outcomes first, then falls back to a URL-aware handler."""

    def __init__(self, outcomes=None, handler=None):
        self.outcomes = list(outcomes or [])
        self.handler = handler
        self.calls = []

    def __call__(self, session, url, timeout):
        self.calls.append(url)
        if self.outcomes:
            return self.outcomes.pop(0)
        if self.handler:
            return self.handler(url)
        return ok(200, CATALOG_HTML.encode('utf-8'))


class AnjukeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / 'out'
        self.clock = FakeClock()
        self._orig_sleep = aj._sleep
        aj._sleep = lambda seconds: None
        self.ledger = Path(self._tmp.name) / 'ledger.json'
        self.ledger.write_text(json.dumps({'trial_used': 597, 'next_request_id': 598}),
                               encoding='utf-8')
        patcher = mock.patch.object(aj, 'GLOBAL_LEDGER', str(self.ledger))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        aj._sleep = self._orig_sleep
        self._tmp.cleanup()

    def rows(self):
        return sp.read_jsonl(self.out / 'requests.jsonl')

    def entry(self):
        return json.loads((self.out / 'entry_probe.json').read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# 1-6) pure parsers
# ---------------------------------------------------------------------------
class CatalogParseTest(unittest.TestCase):
    def test_parse_catalog_embedded_records_and_nav(self):
        result = aj.parse_catalog(CATALOG_HTML, url='https://m.anjuke.com/bj/xiaoqu/',
                                  fetched_at='t', snapshot_path='s')
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['stable_id'], '111111')
        self.assertEqual(result['records'][0]['name'], '甲小区')
        self.assertEqual(result['declared_count'], 2)
        self.assertTrue(result['container_found'])
        self.assertFalse(result['empty_marker'])
        self.assertEqual(result['next_page_url'], 'https://m.anjuke.com/bj/xiaoqu/p2/')
        self.assertIn('https://m.anjuke.com/sh/xiaoqu/',
                      [c['url'] for c in result['city_links']])
        self.assertIn('https://m.anjuke.com/bj/xiaoqu/chaoyang/',
                      [d['url'] for d in result['district_links']])
        self.assertEqual(sorted(result['detail_links']),
                         ['https://m.anjuke.com/bj/xiaoqu/111111/',
                          'https://m.anjuke.com/bj/xiaoqu/222222/'])
        for link in result['detail_links']:
            self.assertRegex(link, r'/xiaoqu/\d+/$')

    def test_parse_catalog_dom_list_fallback(self):
        body = re.sub(r'<script.*?</script>', '', CATALOG_HTML, flags=re.S)
        result = aj.parse_catalog(body, url='https://m.anjuke.com/bj/xiaoqu/', fetched_at='t')
        self.assertEqual({r['name'] for r in result['records']}, {'甲小区', '乙小区'})
        self.assertTrue(result['container_found'])
        self.assertEqual(result['declared_count'], 2)

    def test_detail_link_strictness_and_identity_key(self):
        strict_html = (
            '<html><body><ul class="list-content">'
            '<a href="/bj/xiaoqu/p2/">p2</a>'
            '<a href="/bj/xiaoqu/chaoyang/">chaoyang</a>'
            '<a href="/bj/xiaoqu/">root</a>'
            '<a href="/community/123456/">community</a>'
            '<a href="/bj/xiaoqu/123456/">xiaoqu</a>'
            '<a href="/fangjia/123456/">fangjia</a>'
            '<a href="/esf/123456/">esf</a>'
            '<a href="/loupan/123456/">loupan</a>'
            '</ul></body></html>')
        result = aj.parse_catalog(strict_html, url='https://m.anjuke.com/bj/xiaoqu/',
                                  fetched_at='t')
        self.assertEqual(sorted(result['detail_links']),
                         ['https://m.anjuke.com/bj/xiaoqu/123456/',
                          'https://m.anjuke.com/community/123456/'])
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/bj/xiaoqu/p2/'))
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/bj/xiaoqu/chaoyang/'))
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/bj/xiaoqu/'))
        self.assertTrue(aj._is_community_detail_url('https://m.anjuke.com/community/123456/'))
        self.assertTrue(aj._is_community_detail_url('https://m.anjuke.com/bj/xiaoqu/123456/'))
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/fangjia/123456/'))
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/esf/123456/'))
        self.assertFalse(aj._is_community_detail_url('https://m.anjuke.com/loupan/123456/'))
        self.assertEqual(
            aj.identity_key({'detail_url': 'https://m.anjuke.com/bj/xiaoqu/123456/'}),
            '123456')
        self.assertEqual(aj.identity_key({'name': '测试 花园（一期）'}),
                         'missing-id:测试花园一期')

    def test_parse_detail_fields_and_zero_count_kept(self):
        result = aj.parse_detail(DETAIL_HTML, url='https://m.anjuke.com/bj/xiaoqu/123456/',
                                 fetched_at='t')
        record = result['record']
        self.assertEqual(record['name'], '甲小区')
        self.assertEqual(record['stable_id'], '123456')
        self.assertEqual(record['year_text'], '2005年')
        self.assertEqual(record['year_scope'], 'single')
        self.assertIn('别墅', record['type_tags'])
        self.assertIn('联排', record['type_tags'])
        self.assertEqual(record['villa_proof'], '别墅')
        self.assertEqual(record['on_sale_count'], 5)
        self.assertEqual(record['on_rent_count'], 0)
        self.assertEqual(record['lng'], '116.40')
        self.assertEqual(record['lat'], '39.90')
        self.assertEqual(record['coord_system_evidence'], aj.DETAIL_COORD_NOTE)
        self.assertEqual(record['district_text'], '朝阳区')
        self.assertEqual(record['address_text'], '朝阳路1号')

    def test_parse_detail_year_range(self):
        result = aj.parse_detail(DETAIL_HTML_RANGE, url='https://m.anjuke.com/bj/xiaoqu/999/',
                                 fetched_at='t')
        record = result['record']
        self.assertEqual(record['year_text'], '2005-2010年')
        self.assertEqual(record['year_scope'], 'range')

    def test_parse_detail_without_coords_has_no_evidence(self):
        result = aj.parse_detail(DETAIL_HTML_NO_COORD,
                                 url='https://m.anjuke.com/bj/xiaoqu/888/', fetched_at='t')
        record = result['record']
        self.assertIsNone(record['lng'])
        self.assertIsNone(record['lat'])
        self.assertIsNone(record['coord_system_evidence'])

    def test_normalize_fields_missing_not_zero(self):
        normalized = aj.normalize_fields({'name': '甲'})
        self.assertEqual(normalized['on_sale_count']['state'], 'missing')
        self.assertIsNone(normalized['on_sale_count']['value'])
        self.assertEqual(normalized['on_rent_count']['state'], 'missing')
        zero = aj.normalize_fields({'on_sale_count': 0})
        self.assertEqual(zero['on_sale_count']['state'], 'observed')
        self.assertEqual(zero['on_sale_count']['value'], 0)

    def test_normalize_fields_coord_and_year_partial(self):
        one_axis = aj.normalize_fields({'lng': 116.4})
        self.assertEqual(one_axis['lng_raw']['state'], 'partial')
        self.assertEqual(one_axis['lat_raw']['state'], 'partial')
        self.assertIsNone(one_axis['lat_raw']['value'])
        unknown_year = aj.normalize_fields({'year_text': '2005'})
        self.assertEqual(unknown_year['year_raw']['state'], 'partial')
        self.assertEqual(unknown_year['year_raw']['value'], '2005')
        self.assertEqual(aj.normalize_fields({})['lng_raw']['state'], 'missing')

    def test_classify_page_verdicts(self):
        url = 'https://m.anjuke.com/bj/xiaoqu/'
        challenge = aj.classify_page(CHALLENGE_HTML.encode('utf-8'), url=url, status=200,
                                     title='', container_found=False, empty_marker=False,
                                     records=[])
        self.assertEqual(challenge, 'verify_challenge')
        self.assertEqual(aj.classify_page(b'<html></html>', url=url, status=403, title='',
                                          container_found=False, empty_marker=False, records=[]),
                         'verify_challenge')
        self.assertEqual(aj.classify_page(b'<html></html>', url=url, status=429, title='',
                                          container_found=False, empty_marker=False, records=[]),
                         'verify_challenge')
        self.assertEqual(aj.classify_page(b'<html>ok</html>', url=url, status=200, title='',
                                          container_found=True, empty_marker=False,
                                          records=[{'name': 'x'}]), 'normal')
        empty_dir = aj.classify_page(EMPTY_MARKER_HTML.encode('utf-8'), url=url, status=200,
                                     title='', container_found=True, empty_marker=True,
                                     records=[])
        self.assertEqual(empty_dir, 'empty_directory')
        change = aj.classify_page(b'<html>hi</html>', url=url, status=200, title='',
                                  container_found=False, empty_marker=False, records=[])
        self.assertEqual(change, 'structure_change')
        self.assertEqual(aj.classify_page(b'<html>queued</html>', url=url, status=202, title='',
                                          container_found=False, empty_marker=False, records=[]),
                         'structure_change')
        self.assertEqual(aj.classify_page(b'', url=url, status=200, title='',
                                          container_found=False, empty_marker=False, records=[]),
                         'structure_change')
        self.assertNotEqual(challenge, empty_dir)
        self.assertNotEqual(change, empty_dir)

    def test_classify_page_antibot_gateway_shell(self):
        verdict = aj.classify_page(GATEWAY_SHELL.encode('utf-8'),
                                   url='https://m.anjuke.com/bj/xiaoqu/', status=200,
                                   title='正在验证', container_found=False, empty_marker=False,
                                   records=[])
        self.assertEqual(verdict, 'verify_challenge')
        self.assertNotEqual(verdict, 'structure_change')
        self.assertNotEqual(verdict, 'empty_directory')


# ---------------------------------------------------------------------------
# 7) merge contract
# ---------------------------------------------------------------------------
class MergeRecordsTest(unittest.TestCase):
    def _prior(self):
        return [
            {'detail_url': 'https://m.anjuke.com/bj/xiaoqu/111/', 'name': '甲'},
            {'detail_url': 'https://m.anjuke.com/bj/xiaoqu/222/', 'name': '乙'},
        ]

    def test_merge_keeps_prior_on_failure_and_empty(self):
        incoming = [{'detail_url': 'https://m.anjuke.com/bj/xiaoqu/333/', 'name': '丙'}]
        for page_class in ('structure_change', 'verify_challenge'):
            prior = self._prior()
            snapshot = json.dumps(prior, ensure_ascii=False, sort_keys=True)
            merged = aj.merge_records(prior, incoming, page_class)
            self.assertEqual([aj.identity_key(r) for r in merged], ['111', '222'])
            self.assertEqual(json.dumps(prior, ensure_ascii=False, sort_keys=True), snapshot)
        prior = self._prior()
        self.assertEqual(len(aj.merge_records(prior, [], 'normal')), 2)
        self.assertEqual(
            [aj.identity_key(r) for r in aj.merge_records(prior, incoming, 'empty_directory')],
            ['111', '222'])

    def test_merge_normal_incoming_wins_and_mutates_nothing(self):
        prior = self._prior()
        incoming = [{'detail_url': 'https://m.anjuke.com/bj/xiaoqu/111/', 'name': '甲新'}]
        prior_snapshot = json.dumps(prior, ensure_ascii=False, sort_keys=True)
        incoming_snapshot = json.dumps(incoming, ensure_ascii=False, sort_keys=True)
        merged = aj.merge_records(prior, incoming, 'normal')
        self.assertEqual([aj.identity_key(r) for r in merged], ['111', '222'])
        self.assertIs(merged[0], incoming[0])
        self.assertIs(merged[1], prior[1])
        self.assertEqual(json.dumps(prior, ensure_ascii=False, sort_keys=True), prior_snapshot)
        self.assertEqual(json.dumps(incoming, ensure_ascii=False, sort_keys=True),
                         incoming_snapshot)


# ---------------------------------------------------------------------------
# 8) budget store
# ---------------------------------------------------------------------------
class BudgetStoreTest(AnjukeTestBase):
    def test_budget_store_seeds_and_persists(self):
        run_state = self.out / 'run_state'
        store = aj.AnjukeBudgetStore(run_state, clock=self.clock)
        state = store.load()
        self.assertEqual(state['trial_used'], 597)
        self.assertEqual(state['next_request_id'], 598)
        self.assertEqual(store.reconciliation()['effective_remaining'], 800)
        second = aj.AnjukeBudgetStore(run_state, clock=self.clock)
        self.assertEqual(second.load()['trial_used'], 597)
        result = second.reserve(aj.PLATFORM, '入口探测')
        self.assertTrue(result['allowed'])
        self.assertEqual(second.load()['next_request_id'], 599)

    def test_budget_reserve_hits_round_cap(self):
        run_state = self.out / 'capped'
        capped = aj.SEED_BASE_TRIAL_USED + aj.ANJUKE_ROUND_CAP
        ledger = Path(self._tmp.name) / 'capped_ledger.json'
        ledger.write_text(json.dumps({'trial_used': capped, 'next_request_id': 1398}),
                          encoding='utf-8')
        with mock.patch.object(aj, 'GLOBAL_LEDGER', str(ledger)):
            store = aj.AnjukeBudgetStore(run_state, clock=self.clock)
            result = store.reserve(aj.PLATFORM, '入口探测')
            self.assertFalse(result['allowed'])
            self.assertEqual(result['reason'], 'anjuke_round_cap')
            state = store.load()
            self.assertEqual(state['trial_used'], capped)
            self.assertEqual(state['next_request_id'], 1398)


# ---------------------------------------------------------------------------
# 9) offline entry probe
# ---------------------------------------------------------------------------
class RunProbeTest(AnjukeTestBase):
    def test_run_probe_writes_artifacts_and_request_counts(self):
        aj.run_probe(self.out, transport=MapTransport(handler=catalog_or_detail),
                     clock=self.clock)
        self.assertTrue((self.out / 'requests.jsonl').exists())
        self.assertTrue((self.out / 'run_config.json').exists())
        self.assertTrue((self.out / 'entry_probe.json').exists())
        rows = self.rows()
        entry = self.entry()
        self.assertEqual(entry['requests_used']['total'],
                         len([r for r in rows if r.get('request_id') is not None]))
        self.assertEqual(entry['requests_used']['total'], 5)
        self.assertEqual(entry['requests_used']['by_url_class'],
                         {'bj_mobile_catalog': 1, 'sh_mobile_catalog': 1, 'pagination': 1,
                          'district_catalog': 1, 'detail': 1})

    def test_run_probe_redirect_hop_counts_extra_request(self):
        transport = MapTransport(outcomes=[
            redirect('https://m.anjuke.com/bj/xiaoqu/next/'),
            ok(200, CATALOG_HTML.encode('utf-8'))])
        aj.run_probe(self.out, budget=2, transport=transport, clock=self.clock)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['status'] for r in rows], [302, 200])
        self.assertEqual(rows[0]['requested_url'], aj.MOBILE_BJ_CATALOG)
        self.assertEqual(rows[1]['requested_url'], 'https://m.anjuke.com/bj/xiaoqu/next/')
        ids = [r['request_id'] for r in rows]
        self.assertTrue(all(i is not None for i in ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.entry()['requests_used']['total'], 2)
        self.assertEqual(self.entry()['requests_used']['by_url_class'],
                         {'bj_mobile_catalog': 2})

    def test_run_probe_transport_failure_no_body(self):
        summary = aj.run_probe(self.out, transport=MapTransport(outcomes=[err()]),
                               clock=self.clock)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['failure_class'], 'transport_error')
        self.assertEqual(rows[0]['body_state'], 'no_body')
        self.assertIsNone(rows[0]['snapshot_path'])
        self.assertEqual(summary['exit_code'], 1)

    def test_run_probe_403_blocks_and_stops(self):
        body = b'<html><head><title>Forbidden</title></head><body>no</body></html>'
        summary = aj.run_probe(self.out, transport=MapTransport(outcomes=[ok(403, body)]),
                               clock=self.clock)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['failure_class'], 'blocked_403')
        self.assertTrue(summary['blocked'])
        self.assertEqual(summary['exit_code'], 2)
        entry = self.entry()
        self.assertEqual(len(entry['blocks']), 1)
        self.assertEqual(entry['blocks'][0]['status'], 403)
        # a 403 page is a challenge verdict for the classifier, never an empty directory
        self.assertEqual(aj.classify_page(body, url=aj.MOBILE_BJ_CATALOG, status=403, title='',
                                          container_found=False, empty_marker=False, records=[]),
                         'verify_challenge')

    def test_run_probe_gateway_shell_blocks_and_stops(self):
        summary = aj.run_probe(
            self.out, transport=MapTransport(outcomes=[ok(200, GATEWAY_SHELL.encode('utf-8'))]),
            clock=self.clock)
        rows = self.rows()
        self.assertEqual(rows[0]['page_class'], 'verify_challenge')
        self.assertTrue(summary['blocked'])
        self.assertEqual(summary['exit_code'], 2)
        self.assertEqual(len(self.entry()['blocks']), 1)


# ---------------------------------------------------------------------------
# 10-11) artifacts + replay
# ---------------------------------------------------------------------------
class ArtifactReplayTest(AnjukeTestBase):
    def _probe(self, budget=40):
        transport = MapTransport(handler=catalog_or_detail)
        return aj.run_probe(self.out, budget=budget, transport=transport, clock=self.clock)

    def test_hash_artifacts_and_manifest(self):
        self._probe()
        hash_ver = self.entry()['hash_verification']
        self.assertGreater(hash_ver['checked'], 0)
        self.assertEqual(hash_ver['matched'], hash_ver['checked'])
        sidecars = sorted((self.out / 'raw').glob('*.sha256'))
        self.assertTrue(sidecars)
        for sidecar in sidecars:
            html = sidecar.with_suffix('.html')
            self.assertTrue(html.exists())
            self.assertEqual(sidecar.read_text(encoding='utf-8').strip(),
                             hashlib.sha256(html.read_bytes()).hexdigest())
        manifest_path = self.out / 'manifest.json'
        manifest_path.write_text(json.dumps({'preexisting': {'keep': True}}), encoding='utf-8')
        aj.run_replay(self.out)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['preexisting'], {'keep': True})
        self.assertIn('anjuke_probe', manifest)
        replayed = self.entry()['hash_verification']
        self.assertEqual(replayed['matched'], replayed['checked'])

    def test_replay_reparses_every_snapshot_and_detects_tamper(self):
        self._probe()
        rows = self.rows()
        with_file = [r for r in rows if r.get('snapshot_path')
                     and Path(r['snapshot_path']).exists()]
        self.assertTrue(with_file)
        self.assertEqual(aj.run_replay(self.out)['snapshots_replayed'], len(with_file))
        victim = with_file[0]
        victim_path = Path(victim['snapshot_path'])
        victim_path.write_text('tampered-body', encoding='utf-8')
        summary = aj.run_replay(self.out)
        self.assertEqual(summary['snapshots_replayed'], len(with_file) - 1)
        replay_rows = {item['snapshot_path']: item for item in self.entry()['replay']}
        item = replay_rows[str(victim_path)]
        self.assertFalse(item['file_ok'])
        self.assertEqual(item['replayed_records'], 0)
        self.assertNotEqual(item['page_class'], 'empty_directory')


if __name__ == '__main__':
    unittest.main()
