import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compact_storage
from snapshot_archive import SnapshotArchive


class CompactPermissionsTests(unittest.TestCase):
    def deny_scandir(self, denied):
        original = os.scandir
        def scan(path):
            if Path(path) == denied:
                raise PermissionError(errno.EACCES, 'Access denied', str(denied))
            return original(path)
        return patch('compact_storage.os.scandir', side_effect=scan)

    def make_tree(self, root, name):
        tree = root / 'output/release-checkouts' / name
        tree.mkdir(parents=True)
        (tree / 'evidence.txt').write_bytes(b'original evidence')
        return tree

    def test_unreadable_root_cannot_be_reported_as_an_empty_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-denied')
            with SnapshotArchive(root) as archive, self.deny_scandir(tree):
                with self.assertRaises(PermissionError):
                    compact_storage.archive_tree(archive, tree)
                self.assertEqual(archive.db.execute('SELECT count(*) FROM members').fetchone()[0], 0)
            self.assertEqual((tree / 'evidence.txt').read_bytes(), b'original evidence')

    def test_late_unreadable_subtree_does_not_delete_already_archived_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-partial')
            hidden = tree / 'hidden'
            hidden.mkdir()
            (hidden / 'unique.txt').write_bytes(b'not yet archived')
            with SnapshotArchive(root) as archive, self.deny_scandir(hidden):
                with self.assertRaises(PermissionError):
                    compact_storage.archive_tree(archive, tree, remove_files=True)
                self.assertEqual(archive.db.execute('SELECT count(*) FROM members').fetchone()[0], 1)
            self.assertEqual((tree / 'evidence.txt').read_bytes(), b'original evidence')
            self.assertEqual((hidden / 'unique.txt').read_bytes(), b'not yet archived')

    def test_measure_labels_partial_statistics_and_records_denied_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-denied')
            (root / 'visible.txt').write_bytes(b'visible')
            with self.deny_scandir(tree):
                measured = compact_storage.measure(root)
            self.assertFalse(measured['complete'])
            self.assertEqual(measured['scope'], 'observed-files-only')
            self.assertEqual(measured['logical_bytes'], len(b'visible'))
            self.assertEqual(measured['files'], 1)
            self.assertEqual(measured['unreadable_paths'][0]['path'], tree.relative_to(root).as_posix())
            self.assertEqual(measured['unreadable_paths'][0]['error'], 'PermissionError')

    def test_compact_skips_unreadable_tree_and_still_archives_accessible_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            denied = self.make_tree(root, 'verified-denied')
            readable = self.make_tree(root, 'verified-readable')
            original_remove = compact_storage.shutil.rmtree
            with self.deny_scandir(denied), \
                    patch('compact_storage.shutil.rmtree', wraps=original_remove) as remove, \
                    contextlib.redirect_stdout(io.StringIO()):
                report = compact_storage.compact(root)
            self.assertTrue(denied.exists())
            self.assertFalse(readable.exists())
            self.assertEqual([call.args[0] for call in remove.call_args_list], [readable])
            self.assertEqual(report['status'], 'compacted_with_skips')
            self.assertEqual(report['skipped'][0]['directory'], denied.relative_to(root).as_posix())
            self.assertEqual(report['skipped'][0]['phase'], 'archive')
            self.assertEqual(report['skipped'][0]['error'], 'PermissionError')
            self.assertFalse(report['before']['complete'])
            self.assertFalse(report['after']['complete'])
            self.assertEqual(report['archived_directories'][0]['files'], 1)
            saved = json.loads((root / 'output/storage-maintenance/report.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['skipped'], report['skipped'])
            with SnapshotArchive(root, readonly=True) as archive:
                restored = archive.restore((readable / 'evidence.txt').relative_to(root).as_posix())
                self.assertEqual(restored.read_bytes(), b'original evidence')

    def test_integrity_failure_is_not_skipped_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-corrupt')
            with patch('compact_storage.SnapshotArchive.put_file', side_effect=ValueError('Corrupt snapshot object')), \
                    patch('compact_storage.shutil.rmtree') as remove, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'Corrupt snapshot object'):
                    compact_storage.compact(root)
            remove.assert_not_called()
            self.assertEqual((tree / 'evidence.txt').read_bytes(), b'original evidence')

    def test_compact_does_not_remove_tree_when_only_one_child_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-partial')
            hidden = tree / 'hidden'
            hidden.mkdir()
            (hidden / 'unique.txt').write_bytes(b'unarchived evidence')
            with self.deny_scandir(hidden), \
                    patch('compact_storage.shutil.rmtree') as remove, \
                    contextlib.redirect_stdout(io.StringIO()):
                report = compact_storage.compact(root)
            remove.assert_not_called()
            self.assertEqual(report['archived_directories'], [])
            self.assertEqual(report['skipped'][0]['path'], hidden.relative_to(root).as_posix())
            self.assertEqual((tree / 'evidence.txt').read_bytes(), b'original evidence')
            self.assertEqual((hidden / 'unique.txt').read_bytes(), b'unarchived evidence')

    def test_complete_directory_keeps_normal_cleanup_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = self.make_tree(root, 'verified-readable')
            with contextlib.redirect_stdout(io.StringIO()):
                report = compact_storage.compact(root)
            self.assertEqual(report['status'], 'compacted')
            self.assertEqual(report['skipped'], [])
            self.assertTrue(report['before']['complete'])
            self.assertTrue(report['after']['complete'])
            self.assertFalse(tree.exists())
            with SnapshotArchive(root, readonly=True) as archive:
                restored = archive.restore((tree / 'evidence.txt').relative_to(root).as_posix())
                self.assertEqual(restored.read_bytes(), b'original evidence')


if __name__ == '__main__':
    unittest.main()
