import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import inverse_price_engine as engine
import anjuke_province_crawler as crawler
from data_quality import audit
from stats_gov_crawler import ArticleLinks


class PriceEngineTests(unittest.TestCase):
    def test_forward_and_backward(self):
        anchors = {('北京', 2025, 2): 10000}
        mom = {('北京', 2025, 1): 100, ('北京', 2025, 2): 125, ('北京', 2025, 3): 110}
        result = engine.derive_from_nearest_anchor('北京', anchors, mom)
        self.assertEqual(result[(2025, 1)]['price'], 8000)
        self.assertEqual(result[(2025, 3)]['price'], 11000)
        self.assertEqual(result[(2025, 2)]['price'], 10000)

    def test_missing_link_never_copies_anchor(self):
        result = engine.derive_from_nearest_anchor('北京', {('北京', 2025, 1): 10000}, {('北京', 2025, 3): 101})
        self.assertNotIn((2025, 3), result)

    def test_alternative_anchor_and_chain_limit(self):
        anchors = {('北京', 2025, 1): 10000, ('北京', 2025, 6): 12000}
        indices = {('北京', 2025, m): 100 for m in [3, 4, 5, 6]}
        indices[('北京', 2027, 1)] = 100
        result = engine.derive_from_nearest_anchor('北京', anchors, indices)
        self.assertEqual(result[(2025, 3)]['price'], 12000)
        self.assertNotIn((2027, 1), result)

    def test_base_never_jumps_missing_calendar_month(self):
        result = engine.derive_from_base_price('吉林', {('吉林', 2021, 4): 100, ('吉林', 2021, 6): 99})
        self.assertNotIn((2021, 6), result)

    def test_invalid_index_rejected(self):
        for index in [0, -100, float('nan'), float('inf')]:
            self.assertIsNone(engine.propagate_price(100, (2025, 1), (2025, 2), {(2025, 2): index}))

    def test_exact_year_comparison(self):
        city = dict(city='北京', tier='S', tier_name='一线', color='#000', lat=40, lon=116)
        derived = {(2025, m): dict(price=100+m, source='anjuke') for m in range(1, 13)}
        derived[(2026, 1)] = dict(price=202, source='anjuke')
        result = engine.build_frontend_data([city], {'北京': derived}, {}, {})['cities'][0]
        self.assertEqual(result['change_pct_12m'], 100)
        del derived[(2025, 1)]
        result = engine.build_frontend_data([city], {'北京': derived}, {}, {})['cities'][0]
        self.assertIsNone(result['change_pct_12m'])

    def test_missing_anchor_retains_city_without_rankable_price(self):
        city=dict(city='吉林',tier='3',tier_name='三线',color='#000',lat=43,lon=126)
        rows={(2026,m):dict(price=None,source='unavailable',method='unavailable',quality='unverified',anchor_date=None,chain_months=None) for m in range(1,9)}
        data=engine.build_frontend_data([city],{'吉林':rows},{},{})
        result=data['cities'][0]
        self.assertEqual(result['city'],'吉林')
        self.assertIsNone(result['current_price']);self.assertIsNone(result['max_price'])
        self.assertTrue(all(r['price'] is None and r['change_pct'] is None for r in result['monthly']))
        with tempfile.TemporaryDirectory() as directory:
            audit(data,Path(directory))
        self.assertIsNone(data['analytics']['market'][-1]['median_price'])

    def test_collection_filters_future_and_invalid_months(self):
        info = {'blocks': [{'year': 2026, 'months': [{'month': 8, 'price': 10}, {'month': 9, 'price': 11}, {'month': 13, 'price': 20}, {'month': 'x', 'price': 5}]}]}
        self.assertEqual(crawler.collect_rows(info, 2026, 9, set()), {(2026, 8): 10})

    def test_provenance_enrichment_preserves_conflicts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(crawler, 'OUTPUT_DIR', Path(folder)):
            crawler.merge_city_rows('beijing', '北京', {(2025, 1): 10000})
            result = crawler.merge_city_rows('beijing', '北京', {(2025, 1): 20000})
            self.assertEqual(result, (1, 0, 1))
            import json
            self.assertEqual(json.loads((Path(folder)/'beijing.json').read_text(encoding='utf-8'))[0]['avg_price'], 10000)

    def test_links_accept_quotes_and_nested_titles(self):
        parser = ArticleLinks()
        parser.feed("<a href='./article.html'><span>2026年8月份70个大中城市商品住宅销售价格变动情况</span></a>")
        self.assertEqual(parser.links[0][0], './article.html')
        self.assertIn('2026年8月', parser.links[0][1])

    def test_reference_compares_same_month_and_is_not_transaction(self):
        city = {'city': '北京', 'monthly': [{'date': '2025-06', 'price': 100, 'source': 'anjuke'}, {'date': '2025-07', 'price': 200, 'source': 'anjuke'}], 'fang_reference': {'month': '2025-06', 'price': 100}}
        with tempfile.TemporaryDirectory() as folder:
            audit({'cities': [city]}, Path(folder))
        self.assertEqual(city['fang_reference']['deviation_pct'], 0)
        self.assertFalse(city['audit']['transaction_verified'])
        self.assertEqual(city['audit']['traceable_months'], 0)


if __name__ == '__main__':
    unittest.main()
