# -*- coding: utf-8 -*-
"""房天下楼盘库解析与深分页 cookie 的离线回归（不联网）。"""
import hashlib
import json
import unittest
from datetime import datetime, timezone

import fang_loupan as L


LIST_PAGE = '''<html><head><title>北京楼盘</title></head><body>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList",
"numberOfItems":575,"itemListElement":[
{"@type":"ListItem","position":1,"item":{"@type":"Product","name":"金融街武夷·融御三期",
 "description":"石景山 二居/三居","url":"https://newhouse.fang.com/loupan/1010203883.htm"}},
{"@type":"ListItem","position":2,"item":{"@type":"Product","name":"北京城建·龙樾璟序",
 "description":"通州 九棵树","url":"https://newhouse.fang.com/loupan/1010203635.htm"}}]}</script>
<div class="otherpage"><a href="/house/s/b929/">尾页</a></div>
</body></html>'''

DETAIL_PAGE = '''<html><head><title>壹品联投未来元著-北京壹品联投未来元著楼盘详情-北京房天下</title></head><body>
<script type="application/ld+json">{"@context":"https://schema.org","@type":["RealEstateListing","Place"],
"@id":"https://newhouse.fang.com/loupan/1010203883.htm",
"name":"壹品联投未来元著-北京大兴西红门-55000元/平方米-三居、四居-普通住宅,洋房-北京房天下",
"keywords":"壹品联投未来元著, 北京壹品联投未来元著，壹品联投未来元著楼盘详情",
"address":{"@type":"PostalAddress","addressCountry":"CN","addressRegion":"北京","addressLocality":"大兴","streetAddress":"宏业东路与欣合街交会处 "},
"containedInPlace":{"@type":"Place","name":"西红门"},
"geo":{"@type":"GeoCoordinates","latitude":39.78920364379883,"longitude":116.36245727539062}}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"name":"壹品联投未来元著-北京大兴西红门-55000元/平方米-三居、四居-普通住宅,洋房-北京房天下",
"brand":{"@type":"Brand","name":"北京京麓文璟房地产开发有限公司"},
"offers":{"@type":"AggregateOffer","price":55000,"unitText":"per平方米"},
"additionalProperty":[{"@type":"PropertyValue","name":"开盘时间","value":"2026-09-27"},
{"@type":"PropertyValue","name":"物业类别","value":"普通住宅,洋房"},
{"@type":"PropertyValue","name":"建筑类别","value":"板楼"},
{"@type":"PropertyValue","name":"开发商","value":"北京京麓文璟房地产开发有限公司"},
{"@type":"PropertyValue","name":"所属区域","value":"大兴"},
{"@type":"PropertyValue","name":"所属板块","value":"西红门"},
{"@type":"PropertyValue","name":"楼盘地址","value":"宏业东路与欣合街交会处 "}]}</script>
</body></html>'''

CHECK_PAGE = ('<html><head><script src="https://js.soufunimg.com/fangue/libs/check/checkyzm.min.js">'
              '</script></head><body><div class="slider"></div></body></html>')


class FangLoupanTests(unittest.TestCase):
    def test_otherid_matches_the_hourly_salt(self):
        moment = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
        expected = hashlib.md5('ETFio#dr2026101803lzkrZlt'.encode()).hexdigest()
        self.assertEqual(L.otherid(moment), expected)
        self.assertRegex(L.otherid(moment), r'^[0-9a-f]{32}$')

    def test_deep_pagination_urls_use_the_city_subdomain(self):
        self.assertEqual(L.list_url('bj', 1), 'https://bj.newhouse.fang.com/house/s/')
        self.assertEqual(L.list_url('bj', 2), 'https://bj.newhouse.fang.com/house/s/b92/')
        self.assertEqual(L.list_url('sh', 29), 'https://sh.newhouse.fang.com/house/s/b929/')
        self.assertEqual(L.detail_url('bj', '1010203883'),
                         'https://bj.newhouse.fang.com/loupan/1010203883.htm')

    def test_city_host_uses_the_measured_slug_map(self):
        self.assertEqual(L.city_host('北京'), 'bj')
        self.assertEqual(L.city_host('哈尔滨'), 'hrb')
        self.assertEqual(L.city_host('大连'), 'dl')
        self.assertIsNone(L.city_host('不存在的城市'))

    def test_parse_list_reads_jsonld_and_last_page(self):
        parsed = L.parse_list(LIST_PAGE)
        self.assertEqual(parsed['number_of_items'], 575)
        self.assertEqual(parsed['last_page'], 29)
        self.assertEqual([i['loupan_id'] for i in parsed['items']], ['1010203883', '1010203635'])

    def test_detail_reads_name_address_geo_and_open_year(self):
        record = L.extract_detail(DETAIL_PAGE, host='bj.newhouse.fang.com', loupan_id='1010203883',
                                  source_url='https://bj.newhouse.fang.com/loupan/1010203883.htm',
                                  fetched_at='2026-10-18T00:00:00+00:00', raw=None, city='北京')
        self.assertEqual(record['name'], '壹品联投未来元著')
        self.assertEqual(record['district'], '大兴')
        self.assertEqual(record['area'], '西红门')
        self.assertEqual(record['year_opening'], '2026-09-27')
        self.assertEqual(record['year_meaning'], 'opening')
        self.assertEqual(record['property_category'], '普通住宅,洋房')
        self.assertAlmostEqual(record['latitude'], 39.78920364379883)
        self.assertAlmostEqual(record['longitude'], 116.36245727539062)
        self.assertEqual(record['source_id'], 'fang:bj.newhouse.fang.com:1010203883')
        self.assertEqual(record['coord_system'], 'unknown')

    def test_check_page_is_never_parsed_as_data(self):
        self.assertTrue(L.is_check_page('https://check.3g.fang.com/check.html?backurl=x', CHECK_PAGE))
        self.assertFalse(L.is_check_page('https://bj.newhouse.fang.com/house/s/', LIST_PAGE))

    def test_main_rejects_unknown_city_without_network(self):
        with self.assertRaises(SystemExit):
            L.main(['--cities', '不存在的城市'])


if __name__ == '__main__':
    unittest.main()
