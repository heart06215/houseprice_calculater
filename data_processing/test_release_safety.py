"""Failure injection for scheduled publication and portable evidence snapshots."""
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import release_inputs as R
import update_data as U
from release_contract import official_month, validate


class SourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=sorted((R.ROOT/'output/stats_gov').glob('stats_gov_*.json'))[-1]
        cls.rows=json.loads(path.read_text(encoding='utf-8'))
        cls.month=path.stem[-7:]

    def test_complete_official_city_type_area_grid(self):
        self.assertEqual(len(official_month(self.rows,self.month)),560)

    def test_partial_or_duplicate_report_is_rejected(self):
        for rows in (self.rows[:-1],self.rows[:-1]+self.rows[:1]):
            with self.assertRaises(ValueError):official_month(rows,self.month)

    def test_wrong_report_month_is_rejected(self):
        rows=copy.deepcopy(self.rows);rows[0]['report_month']='2021-01'
        with self.assertRaises(ValueError):official_month(rows,self.month)

    def test_new_and_second_hand_fields_cannot_be_mixed(self):
        rows=copy.deepcopy(self.rows);rows[0]['second_hand_mom']=rows[0]['new_house_mom']
        with self.assertRaises(ValueError):official_month(rows,self.month)


class SnapshotTests(unittest.TestCase):
    def test_legacy_index_file_label_does_not_replace_canonical_evidence(self):
        source=dict(raw_file='output/raw/stats_gov/legacy.html',raw_sha256='legacy',
                    raw=dict(path='output/warehouse/raw/stats_gov/source.html',sha256='canonical'))
        self.assertEqual(list(R.references(source)),[source['raw']])

    def test_flat_market_evidence_survives_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            raw=root/'output/market/raw/source.html';raw.parent.mkdir(parents=True)
            raw.write_text('<title>大理房价</title>',encoding='utf-8')
            content=raw.read_bytes()
            source=root/'output/market/quotes.json'
            source.write_text(json.dumps([dict(raw_file=raw.relative_to(root).as_posix(),
                                              raw_sha256=hashlib.sha256(content).hexdigest())]),encoding='utf-8')
            R.capture(root)
            raw.unlink();source.unlink();R.restore(root)
            self.assertEqual(raw.read_bytes(),content)
            raw.write_text('changed')
            with self.assertRaises(ValueError):R.capture(root)

    def test_rotation_checkpoint_rejects_tampered_inputs_and_keeps_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/R.BUNDLE;target.parent.mkdir(parents=True)
            with zipfile.ZipFile(target,'w') as bundle:
                bundle.writestr('manifest.json',json.dumps({'output/income/clean.json':'wrong'}))
                bundle.writestr('output/income/clean.json','{}')
            before=target.read_bytes()
            cursor=root/'output/anjuke_recheck/update_rotation.json';cursor.parent.mkdir(parents=True)
            cursor.write_text('{}')
            with self.assertRaises(ValueError):R.checkpoint_rotation(root)
            self.assertEqual(target.read_bytes(),before)

    def test_fresh_checkout_restores_evidence_without_overwriting_newer_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'output/income/clean.json';path.parent.mkdir(parents=True)
            path.write_text('{"income":[]}',encoding='utf-8')
            R.capture(root)
            path.unlink();R.restore(root)
            self.assertEqual(json.loads(path.read_text()),{'income':[]})
            path.write_text('{"income":[2025]}',encoding='utf-8');R.restore(root)
            self.assertEqual(json.loads(path.read_text()),{'income':[2025]})

    def test_tampered_snapshot_and_path_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/R.BUNDLE;target.parent.mkdir(parents=True)
            with zipfile.ZipFile(target,'w') as bundle:
                bundle.writestr('manifest.json',json.dumps({'output/income/clean.json':'wrong'}))
                bundle.writestr('output/income/clean.json','{}')
            with self.assertRaises(ValueError):R.restore(root)
            self.assertFalse((root/'output/income/clean.json').exists())
        for value in ('../outside.json','output/../../outside.json','C:/outside.json','output/run.exe'):
            with self.assertRaises(ValueError):R.safe_path(value)


class RetainedPublicationTests(unittest.TestCase):
    def test_checkpoint_error_still_marks_publication_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'data_processing';front=base/'frontend';front.mkdir(parents=True)
            source=front/'intersection_housing_data.json';source.write_text('{"old":"valid"}')
            status=front/'update_status.json';status.write_text('{"last_publish":"prior","sources":{}}')
            (base/'update_policy.json').write_text('{"sources":{"anjuke":{"hours":6}}}')
            def prepare(stage,dashboard_only=False):
                cursor=stage/'output/anjuke_recheck/update_rotation.json';cursor.parent.mkdir(parents=True)
                cursor.write_text('{}')
            with patch.multiple(U,ROOT=root,BASE=base,FRONT=front,STATUS=status), \
                 patch.object(U,'archive_project'),patch.object(U,'prepare_workspace',prepare), \
                 patch.object(U,'preserve_evidence'),patch('release_inputs.checkpoint_rotation',side_effect=ValueError('corrupt snapshot')), \
                 patch.object(U.subprocess,'run',return_value=subprocess.CompletedProcess([],2)):
                self.assertEqual(U.update(['anjuke'],force=True,dashboard_only=True),1)
            self.assertEqual(source.read_text(),'{"old":"valid"}')
            kept=json.loads(status.read_text(encoding='utf-8'))
            self.assertEqual(kept['state'],'retained');self.assertEqual(kept['last_publish'],'prior')
            self.assertIn('corrupt snapshot',kept['error'])

    def test_timeout_preserves_rotation_in_portable_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'data_processing';front=base/'frontend';front.mkdir(parents=True)
            source=front/'intersection_housing_data.json';source.write_text('{"old":"valid"}')
            status=front/'update_status.json';status.write_text('{"last_publish":"prior","sources":{}}')
            (base/'update_policy.json').write_text('{"sources":{"anjuke":{"hours":6}}}')
            cursor=root/'output/anjuke_recheck/update_rotation.json';cursor.parent.mkdir(parents=True)
            cursor.write_text('{}');R.capture(root)
            def prepare(stage,dashboard_only=False):
                path=stage/'output/anjuke_recheck/update_rotation.json';path.parent.mkdir(parents=True)
                path.write_text('{"北京":"attempted"}',encoding='utf-8')
            with patch.multiple(U,ROOT=root,BASE=base,FRONT=front,STATUS=status), \
                 patch.object(U,'archive_project'),patch.object(U,'prepare_workspace',prepare), \
                 patch.object(U,'preserve_evidence'), \
                 patch.object(U.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',2400)):
                self.assertEqual(U.update(['anjuke'],force=True,dashboard_only=True),1)
            self.assertEqual(source.read_text(),'{"old":"valid"}')
            cursor.unlink();R.restore(root)
            self.assertEqual(json.loads(cursor.read_text(encoding='utf-8')),{'北京':'attempted'})

    def test_failed_batch_restores_rotation_without_publishing_failed_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'data_processing';front=base/'frontend';front.mkdir(parents=True)
            source=front/'intersection_housing_data.json';source.write_text('{"old":"valid"}')
            status=front/'update_status.json';status.write_text('{"last_publish":"prior","sources":{}}')
            (base/'update_policy.json').write_text('{"sources":{"anjuke":{"hours":6}}}')
            clean=root/'output/income/clean.json';clean.parent.mkdir(parents=True)
            clean.write_text('{"income":[2025]}')
            rotation=root/'output/anjuke_recheck/update_rotation.json';rotation.parent.mkdir(parents=True)
            rotation.write_text('{"北京":"old"}',encoding='utf-8')
            R.capture(root)
            def prepare(stage,dashboard_only=False):
                dest=stage/'data_processing/frontend';dest.mkdir(parents=True)
                (dest/'intersection_housing_data.json').write_text('{"new":"invalid"}')
                (dest/'update_status.json').write_text('{"last_publish":"bad","sources":{}}')
                cursor=stage/'output/anjuke_recheck/update_rotation.json';cursor.parent.mkdir(parents=True)
                cursor.write_text('{"北京":"attempted"}',encoding='utf-8')
                candidate=stage/'output/income/clean.json';candidate.parent.mkdir(parents=True)
                candidate.write_text('{"income":[]}')
            with patch.multiple(U,ROOT=root,BASE=base,FRONT=front,STATUS=status), \
                 patch.object(U,'archive_project'),patch.object(U,'prepare_workspace',prepare), \
                 patch.object(U,'preserve_evidence'), \
                 patch.object(U.subprocess,'run',return_value=subprocess.CompletedProcess([],2)):
                self.assertEqual(U.update(['anjuke'],force=True,dashboard_only=True),2)
            self.assertEqual(source.read_text(),'{"old":"valid"}')
            self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['last_publish'],'prior')
            rotation.unlink();clean.unlink();R.restore(root)
            self.assertEqual(json.loads(rotation.read_text(encoding='utf-8')),{'北京':'attempted'})
            self.assertEqual(json.loads(clean.read_text()),{'income':[2025]})

    def test_failed_worker_and_timeout_leave_published_data_unchanged(self):
        for outcome in ('invalid_source','timeout'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);base=root/'data_processing';front=base/'frontend';front.mkdir(parents=True)
                source=front/'intersection_housing_data.json';source.write_text('{"old":"valid"}')
                status=front/'update_status.json';status.write_text('{"last_publish":"prior","sources":{}}')
                (base/'update_policy.json').write_text('{"sources":{"nbs":{"hours":12}}}')
                def prepare(stage,dashboard_only=False):
                    dest=stage/'data_processing/frontend';dest.mkdir(parents=True)
                    (dest/'intersection_housing_data.json').write_text('{"new":"invalid"}')
                    (dest/'update_status.json').write_text('{"last_publish":"bad","sources":{}}')
                def run(*args,**kwargs):
                    if outcome=='timeout':raise subprocess.TimeoutExpired('worker',2400)
                    return subprocess.CompletedProcess([],1)
                with patch.multiple(U,ROOT=root,BASE=base,FRONT=front,STATUS=status), \
                     patch.object(U,'archive_project'),patch.object(U,'prepare_workspace',prepare), \
                     patch.object(U,'preserve_evidence'),patch.object(U.subprocess,'run',run):
                    self.assertNotEqual(U.update(['nbs'],force=True,dashboard_only=True),0)
                self.assertEqual(source.read_text(),'{"old":"valid"}')
                kept=json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(kept['state'],'retained');self.assertEqual(kept['last_publish'],'prior')

    def test_all_failed_sources_skip_rebuild_and_keep_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            front=Path(folder);status=front/'update_status.json';path=front/'intersection_housing_data.json'
            status.write_text('{"sources":{}}');path.write_text('{"old":"valid"}')
            with patch.multiple(U,FRONT=front,STATUS=status),patch('release_inputs.restore'), \
                 patch.object(U,'source_job',side_effect=RuntimeError('empty response')), \
                 patch.object(U,'read',side_effect=lambda p,default=None: {'sources':{'nbs':{}}} if p.name=='update_policy.json' else json.loads(p.read_text())), \
                 patch.object(U,'run_script') as build:
                self.assertEqual(U.worker(['nbs'],dashboard_only=True),2)
                build.assert_not_called()
            self.assertEqual(path.read_text(),'{"old":"valid"}')


if __name__=='__main__':unittest.main()
