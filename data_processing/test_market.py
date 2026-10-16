import unittest
import tempfile
import json
import copy
import hashlib
from datetime import date
from pathlib import Path
from unittest.mock import patch
from market_crawler import parse_market, parse_properties
import market_crawler as M
from housing_segments import build
from market_evidence import validate_projection


SUMMARY = ('<title>大理房价网,2026大理房价走势图,大理二手房价格-大理安居客</title>'
           '<nav>新房 二手房</nav><div class="price-box"><div class="price-item line">'
           '<div class="average_title">8806<span>元/㎡</span></div><div class="title">10月挂牌均价</div>'
           '</div><div class="price-item"><div class="average_title">0.22%</div>'
           '<div class="title">比上月</div></div></div>'
           '<div class="property">其他小区 30000元/㎡</div>')
SUMMARY_URL = 'https://dali.anjuke.com/market/'


class MarketTests(unittest.TestCase):
    def test_known_short_city_domain_and_archived_capture_time(self):
        html=SUMMARY.replace('大理','烟台').replace('</title>','</title>\n')+'\n'
        short_url='https://yt.anjuke.com/market/'
        self.assertEqual(M.market_hosts('烟台'),{'yt.anjuke.com','yantai.anjuke.com'})
        self.assertEqual(parse_market(html,'烟台',short_url,date(2026,10,9))[0]['price'],8806)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.multiple(M,ROOT=root,OUT=root/'output/market',PROPS=root/'properties.json'):
                count,_=M.ingest(html,'烟台',short_url,fetched_at='2026-10-04T12:11:01Z')
            self.assertEqual(count,1)
            saved=json.loads((root/'output/market/quotes.json').read_text(encoding='utf-8'))[0]
            self.assertEqual(saved['fetched_at'],'2026-10-04T12:11:01Z')
            self.assertEqual(saved['month'],'2026-10')
            raw=root/saved['raw_file']
            self.assertEqual(raw.read_bytes(),html.encode('utf-8'))
            self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),saved['raw_sha256'])

    def test_city_summary_uses_its_title_type_and_explicit_month_unit(self):
        rows = parse_market(SUMMARY, '大理', SUMMARY_URL, date(2026, 10, 9))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['city'], rows[0]['kind'], rows[0]['month'], rows[0]['price']),
                         ('大理', 'second_hand', '2026-10', 8806))
        self.assertTrue(rows[0]['provisional'])

    def test_summary_rejects_ambiguous_type_unit_date_city_and_district(self):
        fixtures = [SUMMARY.replace('二手房价格', '新房二手房价格'),
                    SUMMARY.replace('元/㎡', '万元'), SUMMARY.replace('2026', '2027'),
                    SUMMARY.replace('10月挂牌均价', '13月挂牌均价'),
                    SUMMARY.replace('2026', '2026年2025'), SUMMARY.replace('大理', '北京')]
        for html in fixtures:
            with self.subTest(html=html[:100]):
                self.assertEqual(parse_market(html, '大理', SUMMARY_URL, date(2026, 10, 9)), [])
        for url in (SUMMARY_URL+'?area=123', SUMMARY_URL+'district/', 'https://beijing.anjuke.com/market/'):
            self.assertEqual(parse_market(SUMMARY, '大理', url, date(2026, 10, 9)), [])

    def test_conflicting_city_summary_does_not_replace_a_typed_quote(self):
        html = SUMMARY + '<li>2026年10月 二手房 9900元/㎡</li>'
        with self.assertRaises(ValueError):
            parse_market(html, '大理', SUMMARY_URL, date(2026, 10, 9))

    def test_typed_projection_requires_original_evidence_and_correct_type(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root/'output/market/raw/source.html'
            raw.parent.mkdir(parents=True)
            raw.write_text(SUMMARY, encoding='utf-8')
            quote = parse_market(SUMMARY, '大理', SUMMARY_URL, date(2026, 10, 9))[0]
            quote.update(raw_file=raw.relative_to(root).as_posix(),
                         raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), fetched_at='2026-10-09T08:00:00Z')
            source = root/'output/market/quotes.json'
            source.write_text(json.dumps([quote]), encoding='utf-8')
            row = dict(month=quote['month'], price=quote['price'], price_source=SUMMARY_URL,
                       fetched_at=quote['fetched_at'], provisional=True)
            data = dict(housing_segments=dict(second_hand=dict(cities=[dict(city='大理', monthly=[row])]),
                                             new_house=dict(cities=[])))
            self.assertEqual(validate_projection(data, root), 1)
            wrong_type = copy.deepcopy(data)
            wrong_type['housing_segments']['new_house']['cities'] = wrong_type['housing_segments']['second_hand'].pop('cities')
            with self.assertRaises(ValueError): validate_projection(wrong_type, root)
            for field, value in [('month', '2026-09'), ('price', 9000), ('price_source', SUMMARY_URL+'district/')]:
                broken = copy.deepcopy(data)
                broken['housing_segments']['second_hand']['cities'][0]['monthly'][0][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError): validate_projection(broken, root)
            source.write_text(json.dumps([dict(quote, kind='new_house')]), encoding='utf-8')
            with self.assertRaises(ValueError): validate_projection(data, root)
            source.write_text(json.dumps([quote]), encoding='utf-8')
            raw.write_text(SUMMARY.replace('8806', '9000'), encoding='utf-8')
            with self.assertRaises(ValueError): validate_projection(data, root)

    def test_types_dates_units_and_city(self):
        html='<title>北京房价</title><ul><li>2026年8月 新房 50000元/㎡</li><li>2026年8月 二手房 40000元/㎡</li><li>2026年8月 新房 二手房 30000元/㎡</li><li>2026年8月 新房 总价300万元</li></ul>'
        rows=parse_market(html,'北京','https://beijing.anjuke.com/market/')
        self.assertEqual({r['kind']:r['price'] for r in rows},{'new_house':50000,'second_hand':40000})
        self.assertEqual(parse_market(html,'上海','https://shanghai.anjuke.com/market/'),[])

    def test_json_typed_quotes(self):
        html='<title>北京房价</title><script type="application/json">{"newHouse":[{"date":"2026年8月","avgPrice":50000,"unit":"元/㎡"}],"secondHand":[{"month":"2026-08","avgPrice":40000,"unit":"元/㎡"}]}</script>'
        self.assertEqual(len(parse_market(html,'北京','https://beijing.anjuke.com/market/')),2)

    def test_listing_identity_and_price(self):
        html='<title>北京二手房</title><div class="property"><a class="property-ex" href="/prop/view/A123?track=1"><span class="property-content-info-comm-name">测试小区</span></a><p class="property-price-average">40,000元/㎡</p><p>89.5㎡ 2室1厅</p><p class="property-price-total">358万</p></div>'
        r=parse_properties(html,'北京','https://beijing.anjuke.com/sale/')[0]
        self.assertEqual(r['price_avg'],40000)
        self.assertEqual(r['area'],89.5)
        self.assertEqual(r['type'],'二手房')
        self.assertIsNone(r['lat'])
        self.assertEqual(r['id'],parse_properties(html.replace('track=1','track=2'),'北京','https://beijing.anjuke.com/sale/')[0]['id'])

    def test_captcha_and_wrong_city_not_data(self):
        self.assertEqual(parse_market('<title>验证码</title>','北京','https://beijing.anjuke.com'),[])
        self.assertEqual(parse_properties('<title>上海二手房</title>','北京','https://beijing.anjuke.com'),[])

    def test_segments_never_reuse_other_type_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'output/stats_gov').mkdir(parents=True)
            (root/'output/market').mkdir()
            rows=[dict(city='北京',report_month='2025-01',area_type='合计',table_type=k,**{k+'_mom':v,k+'_yoy':98}) for k,v in [('new_house',101),('second_hand',99)]]
            (root/'output/stats_gov/month.json').write_text(json.dumps(rows),encoding='utf-8')
            (root/'output/market/quotes.json').write_text(json.dumps([dict(city='北京',kind='new_house',month='2025-01',price=50000,source_url='https://beijing.anjuke.com/market/')]),encoding='utf-8')
            with patch('housing_segments.ROOT',root):
                data=build({'cities':[dict(city='北京',tier='S',current_price=40000)]})
            new=data['housing_segments']['new_house']['cities'][0]['monthly'][0]
            old=data['housing_segments']['second_hand']['cities'][0]['monthly'][0]
            self.assertEqual(new['price'],50000)
            self.assertIsNone(old['price'])
            self.assertEqual(new['mom_pct'],1)
            self.assertEqual(old['mom_pct'],-1)
            self.assertEqual(data['research']['groups'][0]['gap_pp'],2)


if __name__=='__main__':
    unittest.main()
