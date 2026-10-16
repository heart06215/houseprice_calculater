import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from bs4 import BeautifulSoup

import lpr_crawler as L
import release_inputs as R


class LprTests(unittest.TestCase):
    def setUp(self):
        self.days = ['2019-08-20', '2019-09-20']
        self.rates = {'2019-08-20': (4.25, 4.85), '2019-09-20': (4.2, 4.85)}
        self.calls = []
        self.contents = {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        day = self.days[-1]
        if url == L.LPR_URL:
            one, five = self.rates[day]
            result = json.dumps(dict(data=dict(showDateCN=day), records=[
                dict(termCode='5Y', shibor=str(five)), dict(termCode='1Y', shibor=str(one))]))
        elif url == L.PBC_DIRECTORY:
            result = ''.join(f'<a istitle="true" href="{L.PBC_DIRECTORY[:-10]}{d}/index.html" title="{d[:4]}年{int(d[5:7])}月{int(d[8:])}日全国银行间同业拆借中心受权公布贷款市场报价利率（LPR）公告">公告</a>' for d in self.days)
        elif urlparse(url).netloc == 'www.pbc.gov.cn':
            day = url.split('/')[-2]
            one, five = self.rates[day]
            result = f'<p>中国人民银行授权全国银行间同业拆借中心公布，{day[:4]}年{int(day[5:7])}月{int(day[8:])}日贷款市场报价利率（LPR）为：1年期LPR为{one}%，5年期以上LPR为{five}%。</p>'
        else:
            query = parse_qs(urlparse(url).query)
            start, end = query['strStartDate'][0], query['strEndDate'][0]
            result = json.dumps(dict(head=dict(rep_code='200'), data=dict(message='', startDateCN=start,
                endDateCN=end, baseCurveCfgList=['1Y', '5Y']), records=[
                {'showDateCN': d, '1Y': str(self.rates[d][0]), '5Y': str(self.rates[d][1])}
                for d in reversed(self.days) if start <= d <= end]))
        self.contents[url] = result
        return result

    def collect(self, previous=None, getter=None):
        def raw(source, content, layer, suffix):
            digest = hashlib.sha256(content).hexdigest()
            return dict(path=f'output/warehouse/raw/official_lpr/{digest}.{suffix}', sha256=digest)
        with patch.object(L, 'snapshot', side_effect=raw):
            return L.collect(previous or {}, getter or self.get, gap=0)

    def test_bootstrap_pairs_actual_terms_and_orders_all_months(self):
        record = self.collect()
        self.assertEqual([r['as_of'] for r in record['history']], self.days)
        self.assertEqual(record['history'][0]['one_year'], 4.25)
        self.assertEqual(record['history'][0]['five_year'], 4.85)
        self.assertEqual(record['unit'], '%')
        self.assertEqual(record['verification']['as_of'], record['as_of'])

    def test_unchanged_publication_avoids_reloading_history(self):
        previous = self.collect(); self.calls.clear()
        current = self.collect(previous)
        self.assertEqual(previous['history'], current['history'])
        self.assertFalse(any(url.startswith(L.HISTORY_URL) for url in self.calls))

    def test_new_month_refetches_overlap_without_losing_old_months(self):
        previous = self.collect()
        self.days.append('2019-10-21'); self.rates[self.days[-1]] = (4.2, 4.85)
        record = self.collect(previous)
        self.assertEqual(len(record['history']), 3)
        query = parse_qs(urlparse(self.calls[-1]).query)
        self.assertEqual(query['strStartDate'], ['2019-09-01'])
        self.assertEqual(record['history'][0], previous['history'][0])

    def test_bootstrap_splits_requests_at_year_boundaries(self):
        self.days += ['2019-10-21', '2019-11-20', '2019-12-20', '2020-01-20']
        self.rates.update({d: (4.15, 4.8) for d in self.days[2:]})
        self.assertEqual(len(self.collect()['history']), 6)
        windows = [parse_qs(urlparse(u).query) for u in self.calls if u.startswith(L.HISTORY_URL)]
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]['strEndDate'], ['2019-12-31'])
        self.assertEqual(windows[1]['strStartDate'], ['2020-01-01'])

    def test_network_failure_uses_pbc_announcements(self):
        def blocked(url, **kwargs):
            if url == L.LPR_URL:
                raise RuntimeError('request blocked')
            return self.get(url, **kwargs)
        record = self.collect(getter=blocked)
        self.assertEqual(record['source'], '中国人民银行')
        self.assertEqual([r['one_year'] for r in record['history']], [4.25, 4.2])

    def test_history_api_network_failure_uses_pbc_monthly_fallback(self):
        def blocked(url, **kwargs):
            if url.startswith(L.HISTORY_URL):
                raise RuntimeError('history endpoint blocked')
            return self.get(url, **kwargs)
        record = self.collect(getter=blocked)
        self.assertEqual(record['source'], '全国银行间同业拆借中心')
        self.assertTrue(all(r['source'] == '中国人民银行' for r in record['history']))
        self.assertEqual(len(record['history']), 2)

    def test_pbc_fallback_follows_published_pagination_for_old_months(self):
        next_url = L.PBC_DIRECTORY.rsplit('/', 1)[0] + '/published-page-2.html'
        def paged(url, **kwargs):
            if url == L.LPR_URL:
                raise RuntimeError('request blocked')
            if url in (L.PBC_DIRECTORY, next_url):
                html = self.get(L.PBC_DIRECTORY)
                links = BeautifulSoup(html, 'html.parser').find_all('a')
                return (str(links[1]) + f'<a tagname="{next_url}">下一页</a>') if url == L.PBC_DIRECTORY else str(links[0])
            return self.get(url, **kwargs)
        record = self.collect(getter=paged)
        self.assertEqual(len(record['history']), 2)
        self.assertTrue(all(r['source'] == '中国人民银行' for r in record['history']))

    def test_conflicting_official_rates_reject_entire_attempt(self):
        def conflicting(url, **kwargs):
            text = self.get(url, **kwargs)
            return text.replace('1年期LPR为4.2%', '1年期LPR为3.2%') if 'pbc.gov.cn' in url else text
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.collect(getter=conflicting)

    def test_month_gap_duplicate_or_changed_prior_values_are_rejected(self):
        record = self.collect()
        for mutate in [lambda r: r['history'].pop(0), lambda r: r['history'].append(r['history'][0]),
                       lambda r: r['history'][0].update(one_year=1.0)]:
            bad = copy.deepcopy(record); mutate(bad)
            with self.assertRaises(ValueError): L.validate_history(bad, record)

    def test_api_error_empty_duplicates_and_out_of_range_are_rejected(self):
        url = L.HISTORY_URL + '?lang=CN&strStartDate=2019-08-20&strEndDate=2019-09-20'
        payload = json.loads(self.get(url))
        mutations = [lambda p: p['data'].update(message='只提供一年历史数据查询与下载'),
                     lambda p: p.update(records=[]), lambda p: p['records'].append(p['records'][0]),
                     lambda p: p['records'][0].update(showDateCN='2019-10-20'),
                     lambda p: p['records'][0].update({'5Y': 'nan'}),
                     lambda p: p['data'].update(baseCurveCfgList=['1Y'])]
        for mutate in mutations:
            bad = copy.deepcopy(payload); mutate(bad)
            with self.assertRaises(ValueError): L.parse_history(json.dumps(bad), url)
        latest = json.loads(self.get(L.LPR_URL)); latest['records'].append(latest['records'][0])
        with self.assertRaises(ValueError): L.parse_lpr(json.dumps(latest))
        for day in ('2013-01-20', '2099-01-20'):
            with self.assertRaises(ValueError): L.point(day, 3, 3.5, L.LPR_URL)

    def test_evidence_and_portable_snapshot_detect_wrong_rates_and_tampering(self):
        record = self.collect()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for row in [record, record['verification']] + record['history']:
                path = root / row['raw']['path']; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.contents[row['source_url']], encoding='utf-8')
            L.validate_evidence(record, root)
            bad = copy.deepcopy(record); bad['history'][0]['five_year'] = 4.25
            with self.assertRaisesRegex(ValueError, 'evidence'):
                L.validate_evidence(bad, root)
            source = root / 'output/macro/clean.json'; source.parent.mkdir(parents=True)
            source.write_text(json.dumps({'lpr': record}), encoding='utf-8')
            R.capture(root)
            target = root / record['history'][0]['raw']['path']; target.unlink()
            R.restore(root); L.validate_evidence(record, root)
            target.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'hash'):
                L.validate_evidence(record, root)


if __name__ == '__main__':
    unittest.main()
