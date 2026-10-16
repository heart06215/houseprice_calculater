import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import update_data as U
import sqlite3
import data_store
from update_sources import SourceTasks
from update_workspace import UpdateWorkspace, copy_artifact

def _make_db(path,marker,version=2):
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(path))
    try:
        conn.execute(f'PRAGMA user_version={version}')
        conn.execute('CREATE TABLE IF NOT EXISTS marker(value TEXT)')
        conn.execute('DELETE FROM marker')
        conn.execute('INSERT INTO marker VALUES(?)',(marker,))
        conn.commit()
    finally:conn.close()

def _marker(path):
    conn=sqlite3.connect(str(path))
    try:return conn.execute('SELECT value FROM marker').fetchone()[0]
    finally:conn.close()

class UpdateTests(unittest.TestCase):
    def test_due_time_is_not_data_month(self):
        at=datetime(2026,9,18,12,tzinfo=timezone.utc)
        self.assertTrue(U.due({},12,at))
        self.assertFalse(U.due({'checked_at':'2026-09-18T01:00:00+00:00','data_period':'2021-04'},12,at))
        self.assertTrue(U.due({'checked_at':'2026-09-18T00:00:00+00:00'},12,at))
    def test_partial_weekly_source_retries_at_next_daily_run(self):
        entry={'checked_at':'2026-09-17T12:00:00+00:00','state':'partial'}
        self.assertTrue(U.due(entry,168,datetime(2026,9,18,12,tzinfo=timezone.utc)))
        self.assertFalse(U.due(entry,168,datetime(2026,9,18,11,tzinfo=timezone.utc)))
        self.assertFalse(U.due(dict(entry,state='ok'),168,datetime(2026,9,18,12,tzinfo=timezone.utc)))
        self.assertTrue(U.due(dict(entry,state='failed'),6,datetime(2026,9,17,18,tzinfo=timezone.utc)))
    def test_failed_publication_rolls_back_prior_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'live';stage=Path(folder)/'stage';root.mkdir();stage.mkdir()
            for name in ['a.json','b.json']:
                (root/name).write_text('old');(stage/name).write_text('new')
            actual=U.atomic_copy
            def fail_second(src,dst):
                if src==stage/'b.json':raise OSError('simulated disk failure')
                return actual(src,dst)
            with patch.object(U,'ROOT',root),patch.object(U,'atomic_copy',fail_second):
                with self.assertRaises(OSError):U.publish(stage,[Path('a.json'),Path('b.json')])
            self.assertEqual((root/'a.json').read_text(),'old');self.assertEqual((root/'b.json').read_text(),'old')
    def test_cleanup_refuses_project_or_external_path(self):
        with self.assertRaises(ValueError):U.cleanup_workspace(U.ROOT)
        with self.assertRaises(ValueError):U.cleanup_workspace(U.ROOT/'output/update_work')
    def test_lock_prevents_concurrent_publishers(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(U,'ROOT',Path(folder)):
            with U.single_writer():
                with self.assertRaises(OSError):
                    with U.single_writer():pass
            with U.single_writer():pass
    def test_legacy_raw_evidence_survives_stage_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'live';stage=Path(folder)/'stage'
            paths=['output/raw/stats_gov/month_hash.html','output/market/raw/hash.html','output/warehouse/raw/cih/hash.html']
            for rel in paths:
                path=stage/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('evidence')
            with patch.object(U,'ROOT',root):U.preserve_evidence(stage)
            for rel in paths:self.assertEqual((root/rel).read_text(),'evidence')
    def test_sqlite_snapshot_is_transactionally_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            src=Path(folder)/'live.sqlite3';dst=Path(folder)/'copy.sqlite3'
            _make_db(src,'live',2)
            data_store.sqlite_snapshot(src,dst)
            conn=sqlite3.connect(str(dst))
            try:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
                self.assertEqual(conn.execute('SELECT value FROM marker').fetchone()[0],'live')
            finally:conn.close()
    def test_catalog_db_publish_and_rollback_restore_previous(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'live';stage=Path(folder)/'stage'
            (root/'output/catalog').mkdir(parents=True);(stage/'output/catalog').mkdir(parents=True)
            _make_db(root/'output/catalog/catalog.sqlite3','old',2)
            (root/'output/catalog/ingest_report.json').write_text('old',encoding='utf-8')
            _make_db(stage/'output/catalog/catalog.sqlite3','new',2)
            (stage/'output/catalog/ingest_report.json').write_text('new',encoding='utf-8')
            rels=[Path('output/catalog/catalog.sqlite3'),Path('output/catalog/ingest_report.json')]
            with patch.object(U,'ROOT',root):U.publish(stage,rels)
            self.assertEqual(_marker(root/'output/catalog/catalog.sqlite3'),'new')
            self.assertEqual((root/'output/catalog/ingest_report.json').read_text(),'new')
            _make_db(root/'output/catalog/catalog.sqlite3','old',2)
            (root/'output/catalog/ingest_report.json').write_text('old',encoding='utf-8')
            actual=U.copy_artifact
            def fail_json(src,dst):
                if Path(src).suffix=='.json' and str(stage) in str(src):raise OSError('simulated disk failure')
                return actual(src,dst)
            with patch.object(U,'ROOT',root),patch.object(U,'copy_artifact',fail_json):
                with self.assertRaises(OSError):U.publish(stage,rels)
            self.assertEqual(_marker(root/'output/catalog/catalog.sqlite3'),'old')
            self.assertEqual((root/'output/catalog/ingest_report.json').read_text(),'old')
    def test_preserve_evidence_keeps_previous_catalog_db_and_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'live';stage=Path(folder)/'stage'
            (root/'output/catalog').mkdir(parents=True);(stage/'output/catalog').mkdir(parents=True)
            _make_db(root/'output/catalog/catalog.sqlite3','old',2)
            (root/'output/catalog/ingest_report.json').write_text('old',encoding='utf-8')
            _make_db(stage/'output/catalog/catalog.sqlite3','new',2)
            (stage/'output/catalog/ingest_report.json').write_text('new',encoding='utf-8')
            with patch.object(U,'ROOT',root):U.preserve_evidence(stage)
            self.assertEqual(_marker(root/'output/catalog/catalog.sqlite3'),'old')
            self.assertEqual((root/'output/catalog/ingest_report.json').read_text(),'old')
    def test_preserve_evidence_seeds_missing_catalog_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'live';stage=Path(folder)/'stage'
            (stage/'output/catalog').mkdir(parents=True)
            _make_db(stage/'output/catalog/catalog.sqlite3','new',2)
            (stage/'output/catalog/ingest_report.json').write_text('new',encoding='utf-8')
            with patch.object(U,'ROOT',root):U.preserve_evidence(stage)
            self.assertEqual(_marker(root/'output/catalog/catalog.sqlite3'),'new')
            self.assertEqual((root/'output/catalog/ingest_report.json').read_text(),'new')
    def test_catalog_job_runs_offline_scripts_in_order(self):
        calls=[]
        with tempfile.TemporaryDirectory() as folder,patch.object(U,'ROOT',Path(folder)),patch.object(U,'run_script',lambda name,*a:calls.append(name)):
            info=U.source_job('catalog',{}, {'sources':{}})
        self.assertEqual(calls,['catalog_ingest.py','metro_proximity.py','build_city_index.py','directory_coverage.py'])
        self.assertIn('detail',info)
    def test_policy_registers_catalog_job(self):
        policy=U.read(U.BASE/'update_policy.json')
        self.assertIn('catalog',policy['sources'])
        self.assertIn('hours',policy['sources']['catalog'])


class ModuleContractTests(unittest.TestCase):
    def test_typed_market_quotes_refresh_when_history_page_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            run=root/'output/anjuke_recheck/run.json';run.parent.mkdir(parents=True)
            U.atomic_json(run,[{'city':'北京','observed_months':0}])
            with patch('anjuke_province_crawler.CITY_SLUG_70',{'北京':'beijing'}),\
                    patch('anjuke_province_crawler.CITY_DATA_BUG',set()),\
                    patch('recheck_anjuke.crawl'),\
                    patch('market_crawler.run',return_value=[dict(city='北京',status='parsed',quotes=1)]) as market:
                info=SourceTasks(root,lambda *args:None).run('anjuke',{'batch_cities':1})
            market.assert_called_once()
            self.assertEqual(market.call_args.args[0],['北京'])
            self.assertTrue(info['partial'])
            self.assertIn('市场分类页成功 1 页',info['detail'])

    def test_market_properties_cannot_claim_a_typed_quote_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / 'output/anjuke_recheck/run.json'
            run.parent.mkdir(parents=True)
            U.atomic_json(run, [{'city':'北京','observed_months':12}])
            with patch('anjuke_province_crawler.CITY_SLUG_70', {'北京':'beijing'}),\
                    patch('anjuke_province_crawler.CITY_DATA_BUG', set()),\
                    patch('recheck_anjuke.crawl'),\
                    patch('market_crawler.run', return_value=[dict(city='北京',status='parsed',quotes=0,properties=2)]):
                info = SourceTasks(root, lambda *args:None).run('anjuke',{'batch_cities':1})
            self.assertTrue(info['partial'])
            self.assertIn('市场分类页成功 0 页', info['detail'])
            self.assertIn('没有可核验的分类报价', info['error'])

    def test_source_registry_covers_update_policy(self):
        policy = U.read(U.BASE / 'update_policy.json')
        self.assertEqual(set(SourceTasks.NAMES), set(policy['sources']))
        with self.assertRaises(ValueError):
            SourceTasks(U.ROOT, U.run_script).run('__class__', {})

    def test_stats_period_uses_configured_project_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = root / 'output/stats_gov'
            history.mkdir(parents=True)
            for month in ('2024-01', '2024-03'):
                (history / f'stats_gov_{month}.json').write_text('{}')
            tasks = SourceTasks(root, U.run_script)
            with patch('stats_gov_crawler.quick_update', return_value='unchanged'):
                self.assertEqual(tasks.run('nbs', {})['data_period'], '2024-03')
            with patch('stats_gov_crawler.quick_update', return_value='partial'):
                with self.assertRaises(RuntimeError):
                    tasks.run('nbs', {})
            self.assertEqual(len(list(history.glob('*.json'))), 2)

    def test_dashboard_publication_scope_and_status_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = (
                'output/engine_validation/2026-08.json',
                'output/anjuke/beijing.json',
                'output/fang/profiles.json',
                'data_processing/frontend/intersection_housing_data.json',
                'data_processing/frontend/update_status.json',
                'data_processing/frontend/realestate/data/communities.json',
            )
            for rel in files:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            paths = UpdateWorkspace(root).publication_paths(root, dashboard_only=True)
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(paths[-1], Path(files[4]))
            self.assertIn(Path(files[0]), paths)
            self.assertIn(Path(files[1]), paths)
            self.assertIn(Path(files[3]), paths)
            self.assertNotIn(Path(files[2]), paths)
            self.assertNotIn(Path(files[5]), paths)

    def test_status_failure_restores_history_and_removes_new_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'live'
            stage = Path(folder) / 'stage'
            existing = Path('output/engine_validation/2026-08.json')
            created = Path('output/engine_validation/2026-09.json')
            status = Path('data_processing/frontend/update_status.json')
            for base, rel, content in (
                (root, existing, 'old'),
                (stage, existing, 'new'),
                (stage, created, 'new'),
                (root, status, 'ready'),
                (stage, status, 'ready-new'),
            ):
                path = base / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            workspace = UpdateWorkspace(root)
            paths = workspace.publication_paths(stage, dashboard_only=True)

            def fail_status(src, dst):
                if src == stage / status:
                    raise OSError('simulated status publication failure')
                return copy_artifact(src, dst)

            with self.assertRaises(OSError):
                workspace.publish(stage, paths, fail_status)
            self.assertEqual((root / existing).read_text(), 'old')
            self.assertFalse((root / created).exists())
            self.assertEqual((root / status).read_text(), 'ready')
