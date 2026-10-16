import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from audit_public_release import audit, inspect_content


class PublicReleaseAuditTests(unittest.TestCase):
    def test_secret_report_never_contains_the_matched_value(self):
        secret = ('gh' + 'p_' + 'z' * 36).encode()
        findings = inspect_content('source.txt', b'line one\n' + secret)
        self.assertEqual(findings, [dict(path='source.txt', rule='github-token', line=2)])
        self.assertNotIn(secret.decode(), str(findings))

    def test_public_source_hash_is_not_mistaken_for_a_credential(self):
        self.assertEqual(inspect_content('source.json', b'{"sha256":"' + b'a' * 64 + b'"}'), [])

    def test_hidden_secret_in_snapshot_and_force_tracked_env_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('.env\n', encoding='utf-8')
            (root / '.env').write_text('EXAMPLE_SETTING=1\n', encoding='utf-8')
            subprocess.run(['git', 'add', '-f', '.env'], cwd=root, check=True)
            with zipfile.ZipFile(root / 'snapshot.zip', 'w') as archive:
                archive.writestr('output/evidence.txt', 'gh' + 'p_' + 'z' * 36)
            report = audit(root)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['archive_members_scanned'], 1)
            self.assertEqual({finding['rule'] for finding in report['findings']},
                             {'credential-or-session-file', 'github-token'})


if __name__ == '__main__':
    unittest.main()
