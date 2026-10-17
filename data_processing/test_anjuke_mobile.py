"""Mobile cards from the actual anonymous cloud response, not desktop fixtures."""
import unittest
from pathlib import Path
from market_crawler import parse_properties


class MobileListingsTests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).parent / 'fixtures/anjuke_mobile_list.html').read_text(encoding='utf8')
        self.url = 'https://m.anjuke.com/bj/sale/'

    def test_observed_mobile_fields_and_units(self):
        rows = parse_properties(self.html, '北京', self.url)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first['community'], '建投国贸·瑨上')
        self.assertEqual(first['price_avg'], 40000)
        self.assertEqual(first['area'], 75)
        self.assertEqual(first['listings'][0]['total'], 300)
        self.assertEqual(first['layout'], '2室2厅')
        self.assertEqual(first['district'], '通州')
        self.assertEqual(first['orientation'], '南北')
        self.assertNotIn('?', first['source_url'])

    def test_city_unit_and_detail_link_remain_required(self):
        self.assertEqual(parse_properties(self.html, '上海', self.url), [])
        self.assertEqual(parse_properties(self.html.replace('元/㎡', '万元'), '北京', self.url), [])
        self.assertEqual(parse_properties(self.html.replace('/sale/S', '/sale/filter-'), '北京', self.url), [])


if __name__ == '__main__':
    unittest.main()
