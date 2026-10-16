import unittest
from cih_rankings import parse_rankings


class RankingTests(unittest.TestCase):
    def test_amount_company_is_not_area_company(self):
        source = 'enterPriseRankRes:[{indexName:"2025年1-10月",areaAndSaleRankingDtoList:[{enterpriseNameAmount:"金额企业",amount:12.5,enterpriseNameArea:"面积企业",area:99}]}]'
        result = parse_rankings(source, 'https://example.com/data.js')[0]
        self.assertEqual(result['rows'], [dict(rank=1, enterprise='金额企业', amount=12.5)])
        self.assertEqual(result['period'], '2025年1-10月')
        self.assertEqual(result['unit'], '亿元')

    def test_missing_is_not_fabricated(self):
        self.assertEqual(parse_rankings('销售TOP100 无数据', 'url'), [])

    def test_truncated_does_not_publish(self):
        with self.assertRaises(ValueError):
            parse_rankings('landRankRes:[{', 'url')

    def test_untrusted_code_is_not_executed(self):
        with self.assertRaises(ValueError):
            parse_rankings('landRankRes:[alert("x")]', 'url')
