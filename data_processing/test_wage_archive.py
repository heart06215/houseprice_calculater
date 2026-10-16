"""Regression checks against archived official wage evidence and its dates."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import income_crawler as I
import wage_archive as W
import release_inputs as R


class WageArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records={r['as_of']:r for r in I.read(I.OUT/'clean.json',{})['minimum_wages']}

    def test_all_restored_periods_reparse_their_original_evidence(self):
        periods=I.read(I.OUT/'archive_audit.json',{})['recovered_periods']
        self.assertEqual(len(periods),14)
        self.assertEqual(I.validate_evidence(dict(minimum_wages=[self.records[p] for p in periods])),14)
        r=self.records['2017'];fujian=next(row for row in r['rows'] if row['region']=='福建')
        self.assertEqual(fujian['monthly'],[1700,1650,1500,1380,1280])
        self.assertEqual(fujian['hourly'],[18,17.5,16,14.6,13.6])
        self.assertEqual(r['period_precision'],'year')

    def test_ningxia_conflict_requires_replayed_provincial_notice(self):
        r=self.records['2016-12'];row=next(row for row in r['rows'] if row['region']=='宁夏')
        self.assertEqual(row['hourly'],[14,13,12,None,None])
        self.assertEqual(row['monthly_effective_from'],'2015-11-01')
        raw=next(s for s in r['archive_sources'] if s['part']=='hourly')
        self.assertEqual(next(row for row in raw['rows'] if row['region']=='宁夏')['values'],[14,11.5,12,None,None])
        modified=copy.deepcopy(r);modified.pop('corrections')
        with self.assertRaises(ValueError):I.validate_evidence(dict(minimum_wages=[modified]))
        modified=copy.deepcopy(r);modified['corrections'][0]['source_url']='https://example.com/notice'
        with self.assertRaises(ValueError):I.validate_evidence(dict(minimum_wages=[modified]))

    def test_future_effective_dates_in_2015_september_tables_are_rejected(self):
        sources=I.read(I.OUT/'archive_audit.json',{})['quarantined']
        self.assertEqual(len(sources),2)
        for source in sources:
            with self.subTest(url=source['source_url']),self.assertRaisesRegex(ValueError,'实施日期晚于统计期'):
                W.component(source)
        self.assertNotIn('2015-09-28',self.records)

    def test_split_pair_cannot_mix_periods_or_drop_hourly_table(self):
        sources=copy.deepcopy(self.records['2017']['archive_sources'])
        with self.assertRaises(ValueError):W.compose(sources[:1])
        sources[0]['as_of']='2018-03'
        with self.assertRaises(ValueError):W.compose(sources)

    def test_image_values_and_provenance_cannot_be_changed(self):
        r=self.records['2023-01-01']
        self.assertEqual(next(row for row in r['rows'] if row['region']=='湖北')['hourly'][1],18)
        for change in ('value','metadata','image','url'):
            modified=copy.deepcopy(r)
            if change=='value':modified['rows'][0]['monthly'][0]+=1
            elif change=='metadata':modified['period_precision']='year'
            elif change=='image':modified['archive_sources'][0]['image_raw']['sha256']='wrong'
            else:modified['archive_sources'][0]['image_url']='https://example.com/table.jpg'
            with self.subTest(change=change),self.assertRaises(ValueError):
                I.validate_evidence(dict(minimum_wages=[modified]))

    def test_xlsx_reads_both_sheets_and_rejects_formula_cache(self):
        r=self.records['2015-06'];row=next(row for row in r['rows'] if row['region']=='海南')
        self.assertEqual(row['monthly'],[1270,1170,1120,None])
        self.assertEqual(row['hourly_effective_from'],'2015-01-01')
        evidence=r['archive_sources'][0]
        body=W.read_raw(evidence['attachment_raw'],I.ROOT)
        modified=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(body)) as source,zipfile.ZipFile(modified,'w') as target:
            for name in source.namelist():
                value=source.read(name)
                if name=='xl/worksheets/sheet1.xml':value=value.replace(b'<v>1720</v>',b'<f>1700+20</f><v>1720</v>',1)
                target.writestr(name,value)
        with self.assertRaisesRegex(ValueError,'含公式'):W.spreadsheet_rows(modified.getvalue(),'monthly','2015')

    def test_binary_source_evidence_survives_portable_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);proofs=[]
            for name in ('official_wage_image/table.jpg','official_wage_attachment/table.xlsx'):
                path=root/'output/warehouse/raw'/name;path.parent.mkdir(parents=True,exist_ok=True)
                content=b'original binary '+name.encode();path.write_bytes(content)
                proofs.append(dict(path=path.relative_to(root).as_posix(),sha256=hashlib.sha256(content).hexdigest()))
            source=root/'output/income/clean.json';source.parent.mkdir(parents=True)
            source.write_text(json.dumps(dict(proofs=proofs)),encoding='utf-8')
            R.capture(root)
            for proof in proofs:(root/proof['path']).unlink()
            R.restore(root)
            for proof in proofs:self.assertEqual(hashlib.sha256((root/proof['path']).read_bytes()).hexdigest(),proof['sha256'])
            for name in ('output/image.jpg','output/warehouse/raw/unrelated/table.xlsx','output/warehouse/raw/official_wage_image/file.exe'):
                with self.assertRaises(ValueError):R.safe_path(name)


if __name__=='__main__':unittest.main()
