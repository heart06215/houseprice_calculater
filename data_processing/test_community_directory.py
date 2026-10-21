# -*- coding: utf-8 -*-
"""小区目录采集器（58/安居客）的离线解析回归，不联网。"""
import json
import tempfile
import unittest
from pathlib import Path

import community_directory as D


FIFTY_EIGHT_ROW = '''<a href="https://bj.58.com/xiaoqu/meijingdongfang/" class="li-row">
<div class="li-info"><div class="li-title"><div class="li-community-title">美景东方</div></div>
<div class="props"><span class="year">2002年竣工</span><span class="split-line"></span>
<span>朝阳 - 华威桥 - 松榆南路38号</span></div>
<div class="prop-tags"><span class="prop-tag">近地铁</span><span class="prop-tag">配套齐全</span></div>
<div class="detail"><span class="detail-link"><a href="https://bj.58.com/xiaoqu/meijingdongfang/ershoufang/">二手房(64)</a></span></div>
</div></a>
<a href="https://bj.58.com/xiaoqu/bishuizhuangyuan/" class="li-row">
<div class="li-info"><div class="li-title"><div class="li-community-title">碧水庄园</div></div>
<div class="props"><span class="year">1994年竣工</span><span>昌平 - 沙河 - 定泗路</span></div>
<div class="prop-tags"><span class="prop-tag">别墅</span></div></div></a>'''


FIVEI5J_CARD = '''<div class="tz_a" data-href="https://m.5i5j.com/bj/xiaoqu/339050.html">
<div class="list_con"><div class="img_left"><img alt="杏花西里" src="x.jpg"></div>
<div class="xqimg_con"><div class="xq_gz" data-code="0" data-id="339050" data-name="杏花西里" data-price="13225.83"></div>
<h2>杏花西里</h2>
<p class="ting">房山燕山 <!-- 建成年代优化 --> / 约1982年建成 <!-- 楼型 --> / 板楼 </p>
<p class="sp_hui"> 在售<span class="sp_hei">103</span>套 <span class="dian_1">/</span>在租<span class="sp_hei">52</span>套 </p>
<p class="pricepm" data-code="1"><span><b>13225.83</b>元/㎡</span></p></div></div></div>'''

FIVEI5J_DETAIL = '''<html><body>
<div class="year_tan">建成年代仅供参考，实际房龄以政府信息为准 建成年代： 1982-2022年 小区楼型： 板楼</div>
<script>var Lng = "115.964401"; var Lat = "39.726601";</script>
</body></html>'''


FIFTY_EIGHT_DISTRICTS = '''<ul class="region-parents">
<li><a href="https://bj.58.com/xiaoqu/" class="active">不限</a></li>
<li><a href="https://bj.58.com/xiaoqu/1142/">朝阳</a></li>
<li><a href="https://bj.58.com/xiaoqu/1143/">海淀</a></li>
</ul><a class="li-row" href="https://bj.58.com/xiaoqu/meijingdongfang/">美景东方</a>'''


class CommunityDirectoryTests(unittest.TestCase):
    def test_split_location_variants(self):
        self.assertEqual(D.split_location('朝阳 - 华威桥 - 松榆南路38号'),
                         ('朝阳', '华威桥', '松榆南路38号'))
        self.assertEqual(D.split_location('昌平 - 沙河'), ('昌平', '沙河', None))
        self.assertEqual(D.split_location('定泗路'), (None, None, '定泗路'))
        self.assertEqual(D.split_location(''), (None, None, None))

    def test_year_from_text_is_bounded(self):
        self.assertEqual(D.year_from_text('2002年竣工'), 2002)
        self.assertIsNone(D.year_from_text('竣工时间未知'))
        self.assertIsNone(D.year_from_text('1200年'))

    def test_parse_58_list_keeps_year_location_tags_and_sale(self):
        records = D.parse_58_list(FIFTY_EIGHT_ROW, city='北京',
                                  url='https://bj.58.com/xiaoqu/',
                                  fetched_at='2026-10-18T00:00:00+00:00')
        self.assertEqual(len(records), 2)
        first = next(r for r in records if r['name'] == '美景东方')
        self.assertEqual(first['year'], 2002)
        self.assertEqual(first['district'], '朝阳')
        self.assertEqual(first['subarea'], '华威桥')
        self.assertEqual(first['address'], '松榆南路38号')
        self.assertIn('配套齐全', first['tags'])
        self.assertEqual(first['on_sale_count'], 64)
        self.assertFalse(first['pure_villa'])
        second = next(r for r in records if r['name'] == '碧水庄园')
        self.assertTrue(second['pure_villa'])

    def test_source_urls(self):
        self.assertEqual(D.list_url('58', 'bj', 1), 'https://bj.58.com/xiaoqu/')
        self.assertEqual(D.list_url('58', 'bj', 2), 'https://bj.58.com/xiaoqu/p2/')
        self.assertEqual(D.list_url('anjuke', 'bj', 1), 'https://m.anjuke.com/bj/xiaoqu/')
        self.assertEqual(D.list_url('anjuke', 'bj', 7), 'https://m.anjuke.com/bj/xiaoqu-p7/')
        self.assertEqual(D.list_url('5i5j', 'bj', 1), 'https://m.5i5j.com/bj/xiaoqu/')
        self.assertEqual(D.list_url('5i5j', 'bj', 3), 'https://m.5i5j.com/bj/xiaoqu/p3/')

    def test_finalize_accumulates_sources_without_clobbering(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            D.finalize(out, '5i5j', '北京', [dict(source_group='5i5j', detail_url='a', name='A')],
                       dict(source='5i5j', city='北京'))
            D.finalize(out, '58', '北京', [dict(source_group='58', detail_url='b', name='B')],
                       dict(source='58', city='北京'))
            records = json.loads((out / 'records.json').read_text(encoding='utf-8'))
            report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(records['count'], 2)
            self.assertEqual({row['name'] for row in records['records']}, {'A', 'B'})
            self.assertEqual(set(report['runs']), {'5i5j:北京', '58:北京'})
            self.assertTrue((out / 'report-5i5j-北京.json').exists())

    def test_parse_anjuke_detail_walks_nuxt_cycles_and_maps_fields(self):
        community = {'id': '803030', 'name': '温哥华森林', 'address': '立汤路',
                     'lng': '116.41872028885', 'lat': '40.139543421852',
                     'area_name': '昌平', 'block_name': '北七家', 'build_type_str': '低层',
                     'completion_time': '2018', 'tags': ['别墅', '绿化好']}
        nuxt = {'data': [{'props': {'community': community}}]}
        community['cycle'] = nuxt  # circular reference must not hang the walker
        parsed = D.parse_anjuke_detail(nuxt)
        self.assertEqual(parsed['name'], '温哥华森林')
        self.assertEqual(parsed['year'], 2018)
        self.assertEqual(parsed['district'], '昌平')
        self.assertEqual(parsed['subarea'], '北七家')
        self.assertEqual(parsed['building_type'], '低层')
        self.assertIn('别墅', parsed['tags'])
        self.assertAlmostEqual(parsed['lng'], 116.41872028885)
        self.assertEqual(D.parse_anjuke_detail(None), {})

    def test_parse_58_districts_keeps_only_numeric_region_links(self):
        districts = D.parse_58_districts(FIFTY_EIGHT_DISTRICTS, 'https://bj.58.com/xiaoqu/')
        self.assertEqual([d['name'] for d in districts], ['朝阳', '海淀'])
        self.assertEqual(D.district_page_url('https://bj.58.com/xiaoqu/1142/', 1),
                         'https://bj.58.com/xiaoqu/1142/')
        self.assertEqual(D.district_page_url('https://bj.58.com/xiaoqu/1142/', 2),
                         'https://bj.58.com/xiaoqu/1142/p2/')

    def test_source_city_slugs_are_per_source_and_allow_missing_cities(self):
        self.assertEqual(D.source_city_slug('5i5j', '北京'), 'bj')
        self.assertEqual(D.source_city_slug('5i5j', '上海'), 'sh')
        # 5i5j has no Guangzhou entry; the fallback must not invent one.
        self.assertIsNone(D.source_city_slug('5i5j', '广州'))
        self.assertEqual(D.source_city_slug('58', '北京'), 'beijing')
        self.assertEqual(D.source_city_slug('anjuke', '北京'), 'beijing')

    def test_parse_5i5j_list_keeps_year_building_price_and_counts(self):
        records = D.parse_5i5j_list(FIVEI5J_CARD, city='北京',
                                    url='https://m.5i5j.com/bj/xiaoqu/',
                                    fetched_at='2026-10-18T00:00:00+00:00')
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual(row['name'], '杏花西里')
        self.assertEqual(row['detail_url'], 'https://m.5i5j.com/bj/xiaoqu/339050.html')
        self.assertEqual(row['year'], 1982)
        self.assertEqual(row['building_type'], '板楼')
        self.assertEqual(row['location_text'], '房山燕山')
        self.assertEqual(row['on_sale_count'], 103)
        self.assertEqual(row['on_rent_count'], 52)
        self.assertAlmostEqual(row['price'], 13225.83)

    def test_parse_5i5j_detail_range_and_coordinates(self):
        detail = D.parse_5i5j_detail(FIVEI5J_DETAIL)
        self.assertEqual(detail['year'], 1982)
        self.assertEqual(detail['year_end'], 2022)
        self.assertEqual(detail['building_type'], '板楼')
        self.assertAlmostEqual(detail['lng'], 115.964401)
        self.assertAlmostEqual(detail['lat'], 39.726601)
        self.assertEqual(detail['coord_system'], 'bd09')
        self.assertIn('BD-09', detail['coord_system_evidence'])
        self.assertIn('OSM', detail['coord_system_evidence'])

    def test_merge_records_backfills_missing_fields_only(self):
        store = {}
        D.merge_records(store, [dict(detail_url='x', name='a', year=2001, district='朝阳')])
        D.merge_records(store, [dict(detail_url='x', name='a', year=1999, address='某路1号')])
        merged = store['x']
        self.assertEqual(merged['year'], 2001)
        self.assertEqual(merged['address'], '某路1号')


if __name__ == '__main__':
    unittest.main()
