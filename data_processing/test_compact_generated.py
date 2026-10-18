"""Generated preview cleanup must preserve exact recovery and active inputs."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compact_storage import GENERATED_OUTPUTS, archive_tree, compact_generated, restore_tree
from snapshot_archive import SnapshotArchive


class CompactGeneratedTests(unittest.TestCase):
    def test_cleanup_and_full_directory_restore_preserve_every_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preview = root / GENERATED_OUTPUTS[2]
            original = {'index.html': b'<html>historical preview</html>',
                        'data/source.json': b'{"coordinate":12.123456789,"timestamp":"original"}'}
            for name, content in original.items():
                path = preview / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            active = root / 'output/stats_gov/current.json'
            active.parent.mkdir(parents=True)
            active.write_bytes(b'current evidence')
            with contextlib.redirect_stdout(io.StringIO()):
                report = compact_generated(root)
            self.assertEqual(report['archived'][0]['files'], 2)
            self.assertFalse(preview.exists())
            self.assertEqual(active.read_bytes(), b'current evidence')
            with SnapshotArchive(root, readonly=True) as archive:
                self.assertEqual(restore_tree(archive, GENERATED_OUTPUTS[2]), 2)
                for name, content in original.items():
                    self.assertEqual((preview / name).read_bytes(), content)
                (preview / 'index.html').write_bytes(b'new version')
                with self.assertRaisesRegex(ValueError, 'overwrite'):
                    restore_tree(archive, GENERATED_OUTPUTS[2])

    def test_modified_source_aborts_cleanup_without_removing_other_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'output/preview'
            folder.mkdir(parents=True)
            first, second = folder / 'a.html', folder / 'b.html'
            first.write_bytes(b'original a')
            second.write_bytes(b'original b')
            with SnapshotArchive(root) as archive:
                put_file = archive.put_file
                def changing_put(path):
                    # Change the entry just archived so the post-archive check catches it
                    # regardless of the directory-listing order.
                    result = put_file(path)
                    Path(path).write_bytes(b'changed during archive')
                    return result
                with patch.object(archive, 'put_file', side_effect=changing_put):
                    with self.assertRaisesRegex(ValueError, 'changed before archive cleanup'):
                        archive_tree(archive, folder, remove_files=True)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())


if __name__ == '__main__':
    unittest.main()
