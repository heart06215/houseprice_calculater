import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import data_store
from compact_storage import archive_tree, compact_clean
from snapshot_archive import ARCHIVE, CHUNK_SIZE, SnapshotArchive, read_snapshot, safe_member
from update_workspace import UpdateWorkspace


class SnapshotArchiveTests(unittest.TestCase):
    def test_byte_exact_roundtrip_and_cross_version_chunk_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            block = os.urandom(CHUNK_SIZE)
            first = block * 5 + b'first version'
            second = block * 5 + b'second version'
            with SnapshotArchive(root) as archive:
                a = archive.put_bytes(first, 'output/old.db')
                b = archive.put_bytes(second, 'output/new.db')
                self.assertEqual(archive.read_bytes(a['sha256']), first)
                self.assertEqual(archive.read_bytes(b['sha256']), second)
                self.assertEqual(archive.db.execute('SELECT count(*) FROM chunks').fetchone()[0], 3)
                self.assertEqual(archive.restore('output/old.db').read_bytes(), first)
                (root / 'output/old.db').write_bytes(b'newer data')
                with self.assertRaises(ValueError):
                    archive.restore('output/old.db')

    def test_corruption_and_traversal_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with SnapshotArchive(root) as archive:
                item = archive.put_bytes(b'original evidence', 'output/evidence.json')
                archive.db.execute("UPDATE chunks SET content=?", (b'corrupt',))
                archive.db.commit()
                with self.assertRaises(Exception):
                    archive.restore('output/evidence.json')
                self.assertFalse((root / 'output/evidence.json').exists())
                self.assertFalse((root / 'output/evidence.json.restore.tmp').exists())
            for member in ('../outside', 'D:/outside', '/outside', 'output/../../outside'):
                with self.assertRaises(ValueError):
                    safe_member(root, member)

    def test_failed_hash_validation_rolls_back_new_archive_rows(self):
        with tempfile.TemporaryDirectory() as folder, SnapshotArchive(Path(folder)) as archive:
            with self.assertRaises(ValueError):
                archive.put_stream(io.BytesIO(b'wrong bytes'), 'output/a.json', expected='wrong')
            self.assertEqual(archive.db.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
            self.assertEqual(archive.db.execute('SELECT count(*) FROM objects').fetchone()[0], 0)

    def test_current_clean_refs_are_pinned_and_old_history_stays_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = b'{"exact_coordinate":12.123456789,"fetched_at":"original"}'
            sha = hashlib.sha256(payload).hexdigest()
            rel = f'output/warehouse/clean/cih/{sha}.json'
            path = root / rel;path.parent.mkdir(parents=True);path.write_bytes(payload)
            front = root / 'data_processing/frontend';front.mkdir(parents=True)
            current = front / 'input.json';current.write_text(json.dumps({'evidence': {'path': rel, 'sha256': sha}}))
            with SnapshotArchive(root) as archive:
                self.assertEqual(compact_clean(archive, root)['files'], 0)
                self.assertTrue(path.exists())
                current.write_text('{}')
                self.assertEqual(compact_clean(archive, root)['files'], 1)
                self.assertFalse(path.exists())
            self.assertEqual(read_snapshot({'path': rel, 'sha256': sha}, root), payload)

    def test_project_history_is_deduplicated_and_manifest_keeps_original_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'output/anjuke/city.json';path.parent.mkdir(parents=True);path.write_bytes(b'{"price":1234}')
            with patch.multiple(data_store, ROOT=root, STORE=root / 'output/warehouse'):
                first = data_store.archive_project()
                second = data_store.archive_project()
                self.assertEqual(data_store.read_snapshot(first['files'][0]), path.read_bytes())
                self.assertEqual(first['files'][0]['sha256'], second['files'][0]['sha256'])
            self.assertFalse((root / 'output/warehouse/clean/project').exists())
            with SnapshotArchive(root, readonly=True) as archive:
                self.assertEqual(archive.db.execute('SELECT count(*) FROM objects').fetchone()[0], 1)

    def test_worker_archive_is_not_hardlinked_and_new_evidence_is_merged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'live';root.mkdir()
            base = root / 'data_processing';base.mkdir()
            stage = root / 'output/update_work/run-test';stage.mkdir(parents=True)
            with SnapshotArchive(root) as archive:
                original = archive.put_bytes(b'original')
            UpdateWorkspace(root).prepare_workspace(stage, dashboard_only=True)
            self.assertFalse((stage / ARCHIVE).exists())
            with SnapshotArchive(stage) as archive:
                added = archive.put_bytes(b'new evidence', 'output/warehouse/clean/new.json')
            UpdateWorkspace(root).preserve_evidence(stage)
            with SnapshotArchive(root, readonly=True) as archive:
                self.assertEqual(archive.read_bytes(original['sha256']), b'original')
                self.assertEqual(archive.read_bytes(added['sha256']), b'new evidence')

    def test_stale_worker_cleanup_preserves_diagnostics_and_retained_dev_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stale = root / 'output/update_work/run-old';stale.mkdir(parents=True)
            marker = stale / '.update-workspace.json';marker.write_text('{}')
            prior = time.time() - 2 * 86400;os.utime(marker, (prior, prior))
            evidence = stale / 'unpublished.txt';evidence.write_bytes(b'unique failed crawl')
            fresh = root / 'output/update_work/run-fresh';fresh.mkdir();(fresh / '.update-workspace.json').write_text('{}')
            dev = root / 'output/update_work/crawler-audit-retained';dev.mkdir()
            workspace = UpdateWorkspace(root)
            with workspace.single_writer():
                workspace.reap_stale_workspaces()
            self.assertFalse(stale.exists());self.assertTrue(fresh.exists());self.assertTrue(dev.exists())
            with SnapshotArchive(root, readonly=True) as archive:
                self.assertEqual(archive.restore(evidence.relative_to(root).as_posix()).read_bytes(), b'unique failed crawl')

    def test_unreadable_stale_worker_is_preserved_without_blocking_new_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stale = root / 'output/update_work/run-denied';stale.mkdir(parents=True)
            marker = stale / '.update-workspace.json';marker.write_text('{}')
            prior = time.time() - 2 * 86400;os.utime(marker, (prior, prior))
            error = PermissionError(13, 'Access denied', str(stale))
            with patch('compact_storage.archive_tree', side_effect=error), \
                    patch.object(UpdateWorkspace, 'cleanup_workspace') as cleanup, \
                    patch('builtins.print') as warning:
                with UpdateWorkspace(root).single_writer():
                    UpdateWorkspace(root).reap_stale_workspaces()
            cleanup.assert_not_called()
            self.assertTrue(stale.is_dir())
            self.assertEqual(json.loads(warning.call_args.args[0])['event'], 'stale-workspace-preserved')


if __name__ == '__main__':
    unittest.main()
