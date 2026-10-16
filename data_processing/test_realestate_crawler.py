import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import realestate_crawler as R

URL = 'https://www.stats.gov.cn/sj/zxfb/202609/t20260915_1.html'


def report(month='2026-08', published='2026/09/15', older=False):
    year, end = month.split('-')
    title = f'{year}年1—{int(end)}月份全国房地产' + ('开发投资和销售情况' if older else '市场基本情况')
    rows = [('房地产开发投资（亿元）', 47979, -19.9), ('其中：住宅', 37017, -19.7),
            ('房屋施工面积（万平方米）', 560770, -12.8),
            ('房屋新开工面积（万平方米）', 29894, -24.8), ('其中：住宅', 21840, -25.4),
            (('' if older else '新建') + '商品房销售面积（万平方米）', 49880, -12.1),
            (('' if older else '新建') + '商品房销售额（亿元）', 47470, -13)]
    return (f'<title>{title}</title><meta name="ArticleTitle" content="{title}">'
            f'<meta name="PubDate" content="{published} 10:00"><table>' +
            ''.join(f'<tr><td>{n}</td><td>{v:,}</td><td>{y}</td></tr>' for n, v, y in rows) + '</table>')


class RealestateTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(R, 'today', return_value=date(2026, 10, 1))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.archive = patch.object(R, 'ARCHIVE', {})
        self.archive.start()
        self.addCleanup(self.archive.stop)

    def test_total_area_and_investment_are_not_residential_subtotals(self):
        row = R.parse_report(report(), URL)
        self.assertEqual(row['items'][0]['value'], 47979)
        self.assertEqual(row['items'][2], dict(label='房屋新开工面积', value=29894, unit='万㎡', yoy=-24.8))
        self.assertEqual(row['published_at'], '2026-09-15')

    def test_old_title_and_sales_labels_and_combined_january_february(self):
        row = R.parse_report(report('2021-02', '2021/03/15', True), URL)
        self.assertEqual(row['period'], '2021年1-2月')
        self.assertEqual(row['items'][3]['label'], '新建商品房销售面积')
        self.assertNotIn('2021-01', R.expected_months('2021-03'))

    def test_half_year_full_year_and_old_visible_publication_heading(self):
        half = report('2023-06', '2023/07/17').replace('2023年1—6月份', '2023年上半年')
        half = half.replace('<meta name="PubDate" content="2023/07/17 10:00">', '<h2>2023/07/17 10:00 |</h2>')
        self.assertEqual(R.parse_report(half, URL)['month'], '2023-06')
        annual = report('2025-12', '2026/01/19').replace('2025年1—12月份', '2025年')
        self.assertEqual(R.parse_report(annual, URL)['month'], '2025-12')
        growth = report('2022-02', '2022/03/15').replace('市场基本情况', '开发投资增长3.7%')
        self.assertEqual(R.parse_report(growth, URL)['month'], '2022-02')
        with self.assertRaisesRegex(ValueError, 'publication date missing'):
            R.parse_report(half.replace('<h2>2023/07/17 10:00 |</h2>', ''), URL)

    def test_archive_uses_original_document_date_instead_of_migration_url(self):
        html = report('2021-06', '2021/07/15').replace('<title>2021年1—6月份全国房地产市场基本情况</title>', '<title>国家统计局信息公开</title><h2>2021年1—6月份全国房地产市场基本情况</h2>')
        html = html.replace('<meta name="PubDate" content="2021/07/15 10:00">', '成文日期 2021年07月15日')
        row = R.parse_report(html, 'https://www.stats.gov.cn/xxgk/202302/report.html')
        self.assertEqual(row['published_at'], '2021-07-15')

    def test_official_yoy_can_exceed_one_hundred_percent(self):
        row = R.parse_report(report('2021-02', '2021/03/15').replace('<td>-13</td>', '<td>133</td>'), URL)
        self.assertEqual(row['items'][-1]['yoy'], 133)

    def test_changed_unit_missing_total_invalid_number_or_duplicate_fails(self):
        html = report()
        for bad in (html.replace('新开工面积（万平方米）', '新开工面积（平方米）'),
                    html.replace('房屋新开工面积', '住宅新开工面积'),
                    html.replace('29,894', 'NaN'), html.replace('-24.8', 'inf'),
                    html.replace('</table>', '<tr><td>房屋新开工面积（万平方米）</td><td>100</td><td>0</td></tr></table>')):
            with self.subTest(bad=bad[-160:]), self.assertRaises(ValueError):
                R.parse_report(bad, URL)

    def test_future_month_publication_mismatch_or_foreign_source_fails(self):
        for html, url in ((report('2026-10'), URL), (report('2026-01'), URL),
                          (report(published='2026/10/19'), URL),
                          (report(published='2026/08/15'), URL), (report(), 'https://example.com/report.html')):
            with self.assertRaises(ValueError):
                R.parse_report(html, url)

    def test_october_first_keeps_august_until_september_is_actually_published(self):
        existing = R.parse_report(report(), URL)
        saved = dict(existing, history=[existing])
        directory = f'<a href="{URL}">2026年1—8月份全国房地产市场基本情况</a>'
        with patch.object(R, 'expected_months', return_value=['2026-08']), \
                patch.object(R, 'snapshot', return_value={'sha256': 'digest', 'path': 'output/raw.html'}):
            current, errors = R.collect(saved, lambda url, **kwargs: directory if 'index' in url else report())
        self.assertFalse(errors)
        self.assertEqual(current['month'], '2026-08')
        self.assertEqual(len(current['history']), 1)

    def test_new_release_appends_month_and_failure_retains_prior_history(self):
        existing = R.parse_report(report(), URL)
        saved = dict(existing, history=[existing])
        new_url = URL.replace('1.html', '2.html')
        directory = (f'<a href="{new_url}">2026年1—9月份全国房地产市场基本情况</a>'
                     f'<a href="{URL}">2026年1—8月份全国房地产市场基本情况</a>')
        with patch.object(R, 'today', return_value=date(2026, 10, 19)), \
                patch.object(R, 'expected_months', side_effect=lambda last: ['2026-08', '2026-09'] if last.endswith('09') else ['2026-08']), \
                patch.object(R, 'snapshot', return_value={'sha256': 'digest', 'path': 'output/raw.html'}):
            current, errors = R.collect(saved, lambda url, **kwargs: directory if 'index' in url else report('2026-09', '2026/10/19'))
            self.assertFalse(errors)
            self.assertEqual([r['month'] for r in current['history']], ['2026-08', '2026-09'])
            def failed(url, **kwargs):
                if 'index' in url:
                    return directory
                raise OSError('offline')
            current, errors = R.collect(saved, failed)
            self.assertEqual(current['month'], '2026-08')
            self.assertTrue(errors)

    def test_directory_article_month_must_match(self):
        directory = f'<a href="{URL}">2026年1—8月份全国房地产市场基本情况</a>'
        with self.assertRaisesRegex(ValueError, 'No validated'):
            R.collect({}, lambda url, **kwargs: directory if 'index' in url else report('2026-07'), max_pages=1)

    def test_snapshot_upgrade_reparses_raw_and_incremental_request_is_bounded(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); raw_file = root / 'output/raw.html'; raw_file.parent.mkdir()
            raw_file.write_text(report(), encoding='utf-8')
            raw = dict(path='output/raw.html', sha256=hashlib.sha256(raw_file.read_bytes()).hexdigest())
            saved = dict(month='2026-08', source_url=URL, raw=raw, fetched_at='2026-09-15')
            calls = []
            def fetch(url, **kwargs):
                calls.append(url)
                return f'<a href="{URL}">2026年1—8月份全国房地产市场基本情况</a>' if 'index' in url else report()
            with patch.object(R, 'ROOT', root), patch.object(R, 'expected_months', return_value=['2026-08']), \
                    patch.object(R, 'snapshot', return_value=raw):
                current, errors = R.collect(saved, fetch)
            self.assertFalse(errors)
            self.assertEqual(len(calls), 2)
            self.assertEqual(current['history'][0]['items'][2]['value'], 29894)

    def test_evidence_checks_every_historical_value_and_latest_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw_file = root / 'output/raw.html'
            raw_file.parent.mkdir(); raw_file.write_text(report(), encoding='utf-8')
            import hashlib
            row = dict(R.parse_report(report(), URL), raw={'path': 'output/raw.html', 'sha256': hashlib.sha256(raw_file.read_bytes()).hexdigest()})
            current = dict(row, history=[row])
            R.validate_evidence(current, root)
            bad = copy.deepcopy(current); bad['history'][0]['items'][2]['value'] = 21840
            with self.assertRaisesRegex(ValueError, 'value mismatch'):
                R.validate_evidence(bad, root)
            bad = copy.deepcopy(current); bad['month'] = '2026-09'
            with self.assertRaisesRegex(ValueError, 'latest/history'):
                R.validate_evidence(bad, root)
            with self.assertRaisesRegex(ValueError, 'periods lost'):
                R.validate_evidence(current, root, dict(month='2026-08', history=[dict(month='2026-07')]))

    def test_independent_narrative_audit_rejects_reversed_growth_sign_and_wrong_total(self):
        import hashlib
        from chart_pipeline_audit import audit_realestate_evidence
        html = report() + ('<p>全国房地产开发投资47979亿元，同比下降19.9%；其中住宅投资37017亿元。</p>'
                           '<p>房屋新开工面积29894万平方米，下降24.8%。其中住宅新开工面积21840万平方米。</p>')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw_file = root / 'output/raw.html'; raw_file.parent.mkdir()
            raw_file.write_text(html, encoding='utf-8')
            row = dict(R.parse_report(html, URL), raw=dict(path='output/raw.html', sha256=hashlib.sha256(raw_file.read_bytes()).hexdigest()))
            data = dict(information=dict(macro=dict(realestate=dict(history=[row]))))
            result = audit_realestate_evidence(data, root)
            self.assertEqual((result['reports'], result['metrics'], result['numeric_values']), (1, 2, 4))
            bad = copy.deepcopy(data); bad['information']['macro']['realestate']['history'][0]['items'][0]['yoy'] = 19.9
            with self.assertRaisesRegex(AssertionError, 'growth sign mismatch'):
                audit_realestate_evidence(bad, root)
            bad = copy.deepcopy(data); bad['information']['macro']['realestate']['history'][0]['items'][2]['value'] = 21840
            with self.assertRaisesRegex(AssertionError, 'growth sign mismatch'):
                audit_realestate_evidence(bad, root)


if __name__ == '__main__':
    unittest.main()
