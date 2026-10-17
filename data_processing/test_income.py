import json
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import income_crawler as I
from release_inputs import safe_path


class IncomeTests(unittest.TestCase):
    def test_median_supplement_retains_urban_rural_and_reparses_both_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def source(content,name,parser):
                path=root/('output/raw/'+name+'.html');path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(content.encode('utf-8'))
                raw=dict(path=path.relative_to(root).as_posix(),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                return parser(content,'https://www.stats.gov.cn/sj/zxfb/201501/t20150120_'+name+'.html','2026-10-09T08:00:00Z',raw)
            primary=source('<title>2014年国民经济和社会发展统计公报</title><p>全国居民人均可支配收入20167元；全国居民人均消费支出14491元；城镇居民人均可支配收入中位数为26635元；农村居民人均可支配收入中位数为9497元。</p>',
                           'primary',I.parse_communique)
            extra=source('<title>2014年国民经济在新常态下平稳运行</title><p>全年全国居民人均可支配收入20167元；全国居民人均可支配收入中位数17570元。</p>',
                         'extra',I.parse_annual_economy)
            combined=I.supplement_income(primary,extra,['national.disposable_median'],root)
            self.assertEqual(combined['metrics']['national']['disposable_median'],17570)
            self.assertEqual(combined['metrics']['urban']['disposable_median'],26635)
            self.assertEqual(combined['metrics']['rural']['disposable_median'],9497)
            self.assertEqual(combined['source_url'],primary['source_url'])
            self.assertEqual(I.validate_evidence(dict(income=[combined]),root),1)
            without=copy.deepcopy(combined);without.pop('supplements')
            with self.assertRaises(ValueError):I.validate_evidence(dict(income=[without]),root)
            wrong=copy.deepcopy(combined);wrong['supplements'][0]['year']=2015
            with self.assertRaises(ValueError):I.validate_evidence(dict(income=[wrong]),root)
            wrong=copy.deepcopy(combined);wrong['metrics']['national']['disposable_median']=17631
            with self.assertRaises(ValueError):I.validate_evidence(dict(income=[wrong]),root)
            wrong=copy.deepcopy(combined);wrong['supplements'][0]['fields']=['urban.disposable_median']
            with self.assertRaises(ValueError):I.validate_evidence(dict(income=[wrong]),root)
            (root/extra['raw']['path']).write_text('changed')
            with self.assertRaises(ValueError):I.validate_evidence(dict(income=[combined]),root)

    def test_annual_economy_rejects_quarter_future_year_and_income_group_median(self):
        for content in ('<title>2014年前三季度国民经济运行情况</title><p>全年全国居民人均可支配收入20167元；全国居民人均可支配收入中位数17570元。</p>',
                        '<title>2026年国民经济运行情况</title><p>全年全国居民人均可支配收入20167元；全国居民人均可支配收入中位数17570元。</p>',
                        '<title>2014年国民经济运行情况</title><p>全年全国居民人均可支配收入20167元；中等收入组人均可支配收入17631元。</p>'):
            with self.subTest(content=content[:40]),self.assertRaises(ValueError):
                I.parse_annual_economy(content,I.INCOME_SUPPLEMENT_SEEDS[2014],'2026-10-09T08:00:00Z',{})

    def test_income_and_wage_values_must_match_archived_official_tables(self):
        saved=I.read(I.OUT/'clean.json',{})
        self.assertTrue(saved.get('income') and saved.get('minimum_wages'))
        income=copy.deepcopy(saved['income'][-1])
        wage=copy.deepcopy(saved['minimum_wages'][-1])
        self.assertEqual(I.validate_evidence(dict(income=[income],minimum_wages=[wage])),2)
        income['metrics']['national']['disposable_mean']+=1
        with self.assertRaises(ValueError):I.validate_evidence(dict(income=[income]))
        wage['rows'][0]['monthly'][0]+=1
        with self.assertRaises(ValueError):I.validate_evidence(dict(minimum_wages=[wage]))

    def test_wage_report_period_unit_and_hash_cannot_be_changed(self):
        record=I.read(I.OUT/'clean.json',{})['minimum_wages'][-1]
        for field,value in [('as_of','2020-01-01'),('unit','万元/月')]:
            modified=copy.deepcopy(record);modified[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                I.validate_evidence(dict(minimum_wages=[modified]))
        modified=copy.deepcopy(record);modified['raw']['sha256']='wrong'
        with self.assertRaises(ValueError):I.validate_evidence(dict(minimum_wages=[modified]))

    def test_all_70_cities_have_wage_region(self):
        cities=I.read(I.ROOT/'data_processing/frontend/intersection_cities.json',{})['cities']
        data=I.projection()
        self.assertEqual(set(data['city_provinces']),{r['city'] for r in cities})
        self.assertEqual(len(data['city_provinces']),70)
        self.assertEqual(data['city_provinces']['石家庄'],'河北')
        self.assertEqual(data['city_provinces']['吉林'],'吉林')
        self.assertEqual(data['city_provinces']['深圳'],'广东')
        regions={r['region'] for r in data['minimum_wages'][-1]['rows']}
        self.assertTrue(set(data['city_provinces'].values())<=regions)
    def test_actual_nbs_income_units_year_and_scopes(self):
        saved=I.read(I.OUT/'clean.json',{})
        if not saved.get('income'):self.skipTest('No local archived sample')
        record=next(r for r in saved['income'] if r['year']==2025)
        raw=(I.ROOT/record['raw']['path']).read_bytes()
        p=I.parse_income(raw,record['source_url'],record['fetched_at'],record['raw'])
        self.assertEqual(p['year'],2025)
        self.assertEqual(p['metrics']['national']['disposable_mean'],43377)
        self.assertEqual(p['metrics']['national']['disposable_median'],36231)
        self.assertEqual(p['metrics']['urban']['disposable_median'],51115)
        self.assertEqual(p['metrics']['rural']['disposable_median'],20711)
        self.assertEqual(p['metrics']['national']['consumption'],29476)
        self.assertEqual(len(p['consumption_categories']),8)

    def test_historical_years_parse_independently(self):
        saved=I.read(I.OUT/'clean.json',{})
        expected={2021:35128,2022:36883,2023:39218,2024:41314}
        for record in saved.get('income',[]):
            if record['year'] not in expected:continue
            p=I.parse_income((I.ROOT/safe_path(record['raw']['path'])).read_bytes(),record['source_url'],record['fetched_at'],record['raw'])
            self.assertEqual(p['metrics']['national']['disposable_mean'],expected[p['year']])
            self.assertGreater(p['metrics']['national']['disposable_median'],0)

    def test_quarter_and_empty_verification_page_are_not_annual_income(self):
        for s in ('<title>2026年上半年居民收入和消费支出情况</title>','<script>document.cookie="token"</script>'):
            with self.assertRaises(ValueError):I.parse_income(s,I.NBS_SEED,'2026-09-19',{})
        with self.assertRaises(ValueError):I.parse_wages('<script>verification</script>',I.WAGE_SEED,'2026-09-19',{})

    def test_real_wage_table_preserves_missing_columns_and_shenzhen(self):
        p=I.OUT/'browser_mohrss_20260112.json'
        if not p.exists():self.skipTest('No local captured wage table')
        saved=json.loads(p.read_text(encoding='utf-8'))
        data=I.parse_wages('<title>'+saved['title']+'</title>'+''.join(saved['tables']),saved['url'],saved['fetched_at'],{})
        rows={r['region']:r for r in data['rows']}
        self.assertEqual(len(rows),32)
        self.assertEqual(rows['北京']['monthly'],[2540,None,None,None])
        self.assertEqual(rows['北京']['hourly'][0],27.7)
        self.assertEqual(rows['广东']['monthly'],[2500,2080,1850,1750])
        self.assertEqual(rows['深圳']['monthly'][0],2520)
        self.assertEqual(data['as_of'],'2026-01-01')
        with self.assertRaises(ValueError):I.parse_wages('<p>截至2026年1月1日 月最低工资标准</p><table></table>',saved['url'],saved['fetched_at'],{})

    def test_failed_refresh_keeps_existing_clean_data(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(I,'OUT',Path(temp)),patch.object(I,'fetch',side_effect=RuntimeError('offline')):
            old={'income':[{'year':2025}],'minimum_wages':[{'as_of':'2026-01-01'}]}
            I.atomic_json(I.OUT/'clean.json',old)
            self.assertEqual(I.crawl(1)['state'],'partial')
            self.assertEqual(I.read(I.OUT/'clean.json',{}),old)

    def test_browser_recovery_resolves_http_failure_without_hiding_attempt(self):
        def fetch(url):
            if url.startswith(I.WAGE_LIST):
                return '<script>EO_Bot()</script>',{}
            return '<a href="income.html">income</a>',{}
        with tempfile.TemporaryDirectory() as temp,patch.object(I,'OUT',Path(temp)),\
                patch.object(I,'fetch',side_effect=fetch),patch.object(I,'discover',return_value=[]),\
                patch.object(I,'parse_income',return_value={'year':2025}),\
                patch.object(I,'browser_collect_wage_links',return_value=[I.WAGE_SEED]),\
                patch.object(I,'browser_html',return_value='validated table'),\
                patch.object(I,'merge_browser_wage',return_value={'as_of':'2026-01-01'}):
            result=I.crawl(1)
            self.assertEqual(result['state'],'ok')
            failed=[e for e in result['events'] if e['state']=='failed']
            self.assertEqual(len(failed),1)
            self.assertTrue(failed[0]['recovered'])
            self.assertEqual(result['events'][-1]['capture_mode'],'browser_dom')
            self.assertEqual(I.read(I.OUT/'clean.json',{})['minimum_wages'],[{'as_of':'2026-01-01'}])

    def test_browser_failure_keeps_history_and_unresolved_status(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(I,'OUT',Path(temp)),\
                patch.object(I,'fetch',return_value=('<script>EO_Bot()</script>',{})),\
                patch.object(I,'parse_income',return_value={'year':2025}),\
                patch.object(I,'browser_collect_wage_links',side_effect=ValueError('验证码页')):
            old={'income':[{'year':2025}],'minimum_wages':[{'as_of':'2026-01-01'}]}
            I.atomic_json(I.OUT/'clean.json',old)
            result=I.crawl(1)
            self.assertEqual(result['state'],'partial')
            self.assertFalse(any(e.get('recovered') for e in result['events']))
            self.assertEqual(I.read(I.OUT/'clean.json',{})['minimum_wages'],old['minimum_wages'])

    def test_wage_candidates_refresh_latest_and_backfill_missing_with_a_bound(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(I,'OUT',Path(temp)):
            I.atomic_json(I.OUT/'clean.json',{'income':[], 'minimum_wages':[{'as_of':'2026-01-01','source_url':'known'}]})
            self.assertEqual(I.wage_candidates(['latest','known','missing-1','missing-2','missing-3','missing-4']),
                             ['latest','missing-1','missing-2','missing-3'])
            self.assertEqual(I.wage_candidates(['known','known']),['known'])
            self.assertEqual(I.wage_candidates([]),[])


if __name__=='__main__':unittest.main()
