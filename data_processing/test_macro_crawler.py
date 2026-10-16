import json
import unittest
from unittest.mock import patch

import macro_crawler as M
from income_crawler import publication_date


class MacroTests(unittest.TestCase):
    def test_complete_lpr_terms_and_no_future_date(self):
        data=dict(data=dict(showDateCN='2026-09-20'),records=[dict(termCode='1Y',shibor='3.00'),dict(termCode='5Y',shibor='3.50')])
        self.assertEqual(M.parse_lpr(json.dumps(data))['five_year'],3.5)
        data['records'].pop()
        with self.assertRaises(ValueError):M.parse_lpr(json.dumps(data))
        data['data']['showDateCN']='2099-01-01'
        with self.assertRaises(ValueError):M.parse_lpr(json.dumps(data))

    def test_cumulative_macro_period_units_and_columns(self):
        table='<title>2026年1—8月份全国房地产市场基本情况</title><meta name="PubDate" content="2026/09/15 10:00"><table>'
        for name,value,yoy in [('房地产开发投资（亿元）',47979,-19.9),('其中：住宅',37017,-19.7),('房屋新开工面积（万平方米）',29894,-24.8),('其中：住宅',21840,-25.4),('新建商品房销售面积（万平方米）',49880,-12.1),('新建商品房销售额（亿元）',47470,-13)]:
            table+=f'<tr><td>{name}</td><td>{value}</td><td>{yoy}</td></tr>'
        table+='</table>'
        record=M.parse_macro(table,'https://www.stats.gov.cn/report.html')
        self.assertEqual(record['month'],'2026-08')
        self.assertEqual([r['unit'] for r in record['items']],['亿元','亿元','万㎡','万㎡','亿元'])
        self.assertEqual(record['items'][2]['value'],29894)
        for bad in (table.replace('1—8','2099'),table.replace('<td>47470</td>','<td>--</td>')):
            with self.assertRaises(ValueError):M.parse_macro(bad,'https://www.stats.gov.cn/report.html')

    def test_archive_migration_date_does_not_replace_publication_metadata(self):
        self.assertEqual(publication_date('<meta name="PubDate" content="2015-02-26 10:00:00">','https://www.stats.gov.cn/202302/t20230203_1.html'),'2015-02-26')

    def test_failed_macro_requests_preserve_both_valid_records(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);saved={'lpr':{'as_of':'2026-09-20'},'realestate':{'month':'2026-08'}}
            (path/'clean.json').write_text(json.dumps(saved),encoding='utf-8')
            with patch.object(M,'OUT',path),patch.object(M,'http_get',side_effect=OSError('blocked')):
                self.assertTrue(all(e['state']=='failed' for e in M.crawl()['events']))
            self.assertEqual(json.loads((path/'clean.json').read_text()),saved)


if __name__=='__main__':unittest.main()
