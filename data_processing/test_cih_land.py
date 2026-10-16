# -*- coding: utf-8 -*-
"""cih_land 离线单测：周期检测、签名分类、区块增量（无网络、无浏览器）。"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cih_land  # noqa: E402


def make_dom():
    return {
        'supply_22_text': '22城宅地供求 (2026年01-10月)',
        'weekly_300_text': '300城·宅地成交数据 (2026年第39周 09.21-09.27) 成交宗数 143宗 '
                           '规划建筑面积 922.62万㎡ 成交楼面价 4,981元/㎡',
        'all_tables': [
            {'title': '22城宅地供求', 'rows': [['22城', '推出土地宗数(宗)', '推出规划建面(万㎡)', '推出楼面均价(元/㎡)', '成交土地宗数(宗)', '成交规划建面(万㎡)', '成交楼面均价(元/㎡)', '土地出让金(亿元)', '平均溢价率(%)']]},
            {'title': '22城宅地供求', 'rows': [['绝对值', '865', '5380.09', '11878', '587', '3701.66', '15588', '5770', '13.46']]},
            {'title': '22城宅地供求', 'rows': [['22城明细', '推出土地宗数(宗)', '推出规划建面(万㎡)', '推出楼面均价(元/㎡)', '成交土地宗数(宗)', '成交规划建面(万㎡)', '成交楼面均价(元/㎡)', '土地出让金(万元)', '平均溢价率(%)']]},
            {'title': '22城宅地供求', 'rows': [['南京', '153', '903.65', '8133', '62', '363.94', '8698', '3165694', '1.88'],
                                              ['成都', '96', '578.95', '7925', '56', '311.93', '9526', '2971343.63', '3.76']]},
        ],
        'top_tabs': ['总价', '单价'],
        'top_leaves': ['全国地块成交TOP10', '2026年1-10月', '总价', '单价',
                       '排名', '地块名称', '出让金(万元)', '成交楼面价(元/㎡)'],
        'title_texts': ['22城宅地供求'],
    }


class PeriodTest(unittest.TestCase):
    def test_iso_week_label_matches_site_convention(self):
        # 站点 2026年第39周 = 09.21-09.27（周一开周）；9/24 应判为第39周
        d = datetime(2026, 9, 24, tzinfo=timezone.utc)
        self.assertEqual(cih_land._iso_week_label(d), '2026年第39周')
        d2 = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.assertEqual(cih_land._iso_week_label(d2), '2026年第40周')

    def test_month_label(self):
        d = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(cih_land._current_month_label(d), '2026-10')


class BuildSectionsTest(unittest.TestCase):
    def setUp(self):
        self.sections = cih_land.build_sections(make_dom())

    def test_weekly_section(self):
        w = self.sections['weekly_300']
        self.assertEqual(w['period'], '2026年第39周 09.21-09.27')
        self.assertEqual(w['stats']['成交宗数_宗'], 143.0)
        self.assertEqual(w['stats']['规划建筑面积_万㎡'], 922.62)
        self.assertEqual(w['stats']['成交楼面价_元㎡'], 4981.0)

    def test_supply_22_section(self):
        s = self.sections['supply_22']
        self.assertEqual(s['period'], '2026年01-10月')
        self.assertEqual(s['aggregate']['values'][:2], [865.0, 5380.09])
        self.assertEqual(len(s['aggregate']['values']), 8)
        self.assertEqual([c['city'] for c in s['cities']], ['南京', '成都'])
        self.assertEqual(s['cities'][0]['values'][0], 153.0)
        self.assertTrue(s['headers'])
        self.assertEqual(s['aggregate']['headers'][6], '土地出让金(亿元)')
        self.assertEqual(s['headers'][6], '土地出让金(万元)')

    def test_period_is_not_refresh_month_or_other_section(self):
        dom = make_dom()
        dom.pop('supply_22_text')
        self.assertIsNone(cih_land.build_sections(dom)['supply_22']['period'])
        dom['supply_22_text'] = '22城宅地供求 (2026年10-01月)'
        self.assertIsNone(cih_land.build_sections(dom)['supply_22']['period'])

    def test_top_deals_honest_empty(self):
        # 数据行未渲染 → tables 留空 + note，表头/时期选项如实记录
        t = self.sections['top_deals']
        self.assertEqual(t['tables'], [])
        self.assertIn('地块名称', t['headers'])
        self.assertEqual(t['period_options'][0], '2026年1-10月')
        self.assertTrue(t['note'])


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(cih_land, 'OUT',
                                    Path(self._tmp.name) / 'land.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_fetch_then_freshness_skip(self):
        result = cih_land.refresh(fetch=make_dom,
                                  now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['periods'], {'week': '2026年第40周', 'month': '2026-10'})
        self.assertTrue(Path(result['evidence']['raw_path']).exists())
        cih_land.validate_evidence(result)
        changed = json.loads(json.dumps(result))
        changed['sections']['supply_22']['period'] = '2026年10月'
        with self.assertRaisesRegex(ValueError, 'period'):
            cih_land.validate_evidence(changed)
        changed = json.loads(json.dumps(result))
        changed['sections']['supply_22']['cities'][0]['values'][5] += 1
        with self.assertRaisesRegex(ValueError, 'cities'):
            cih_land.validate_evidence(changed)
        # 同周同月 → 零请求（fetch 被调用即失败）
        with mock.patch.object(cih_land, '_fetch_dom',
                               side_effect=AssertionError('must not fetch')):
            second = cih_land.refresh(now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(second['state'], 'unchanged')

    def test_period_rollover_triggers_refetch_and_dedup(self):
        cih_land.refresh(fetch=make_dom, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        # 下一个月 → 需要重抓；内容与上期相同 → unchanged 且不重写文件
        before = cih_land.OUT.read_bytes()
        result = cih_land.refresh(fetch=make_dom,
                                  now=datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(result['state'], 'unchanged')
        # 周期内数据变化 → ok 并重写
        dom = make_dom()
        dom['all_tables'][3]['rows'][0] = ['南京', '200', '903.65', '8133', '62', '363.94', '8698', '3165694', '1.88']
        result2 = cih_land.refresh(fetch=lambda: dom,
                                   now=datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(result2['state'], 'ok')
        self.assertIn('supply_22', result2['changed_sections'])
        self.assertNotEqual(cih_land.OUT.read_bytes(), before)

    def test_failed_fetch_keeps_old(self):
        cih_land.refresh(fetch=make_dom, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        old = cih_land.OUT.read_bytes()
        result = cih_land.refresh(fetch=lambda: {'all_tables': []},
                                  now=datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(cih_land.OUT.read_bytes(), old)

    def test_partial_render_keeps_previous_complete_supply(self):
        cih_land.refresh(fetch=make_dom, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        old = cih_land.OUT.read_bytes()
        for key in ('supply_22_text', 'all_tables'):
            dom = make_dom()
            if key == 'all_tables':
                dom[key] = dom[key][:3]
            else:
                dom.pop(key)
            result = cih_land.refresh(force=True, fetch=lambda: dom)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(cih_land.OUT.read_bytes(), old)

    def test_build_sections_survives_garbage(self):
        # 空表/坏输入不抛出，字段保持空
        sections = cih_land.build_sections({'all_tables': [], 'weekly_300_text': ''})
        self.assertEqual(sections['supply_22']['cities'], [])
        self.assertIsNone(sections['weekly_300']['period'])


if __name__ == '__main__':
    unittest.main()
