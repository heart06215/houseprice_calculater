"""Cloud dispatch contracts; no network, browser installation or real collection."""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cloud_crawl as C
import macro_crawler as M
import geocode_communities as G


class DispatchTests(unittest.TestCase):
    def test_every_registered_entry_exists_and_workflow_choices_match(self):
        workflow = (C.ROOT / '.github/workflows/crawl.yml').read_text(encoding='utf-8')
        options = workflow.split('        options:\n', 1)[1].split('      args:', 1)[0]
        self.assertEqual(set(re.findall(r'^          - (\w+)$', options, re.M)), set(C.CRAWLERS))
        for name in C.CRAWLERS:
            argv = C.command(name, ['--example', '北京; $(touch injected)'])
            self.assertEqual(argv[0], sys.executable)
            self.assertTrue(Path(argv[1]).is_file(), name)
            self.assertEqual(argv[-1], '北京; $(touch injected)')

    def test_arguments_are_json_strings_and_desktop_modes_are_rejected(self):
        for value in ('{}', 'null', '[1]', '[null]', '["\\u0000"]', 'broken'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                C.parse_arguments(value)
        for args in (['--headful'], ['--headful=true'], ['--edge'], ['--edge=x'],
                     ['--manual'], ['--browser=edge'], ['--browser', 'chrome']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                C.parse_arguments(json.dumps(args))
        self.assertEqual(C.parse_arguments('["--browser", "chromium"]'), ['--browser', 'chromium'])
        for args in (['--path', '1'], ['--path=2']):
            with self.assertRaises(ValueError):
                C.parse_arguments(json.dumps(args), 'anjuke_probe')
        self.assertEqual(C.parse_arguments('["--path", "4"]', 'anjuke_probe'), ['--path', '4'])

    def test_success_failure_and_timeout_write_machine_readable_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'report.json'
            summary = Path(folder) / 'summary.md'
            for result, expected, state in ((0, 0, 'completed'), (2, 2, 'failed'),
                                            (subprocess.TimeoutExpired('crawler', 1), 124, 'timed_out'),
                                            (OSError('spawn failed'), 1, 'failed')):
                runner = dict(side_effect=result) if isinstance(result, Exception) else dict(return_value=result)
                with self.subTest(state=state), mock.patch.object(C, 'REPORT', report), \
                     mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}), \
                     mock.patch.object(C, 'run_collector', **runner) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(C.main(['--crawler', 'stats_gov']), expected)
                    saved = json.loads(report.read_text(encoding='utf-8'))
                    self.assertEqual((saved['state'], saved['exit_code']), (state, expected))
                    self.assertIn('finished_at', saved)
                    environment = run.call_args.args[1]
                    self.assertEqual(environment['CRAWLER_CLOUD'], '1')
                    self.assertEqual(environment['CRAWLER_BROWSER_CHANNEL'], 'chromium')
            self.assertIn('timed_out', summary.read_text(encoding='utf-8'))

    def test_dry_run_never_starts_collection(self):
        with mock.patch.object(C, 'run_collector') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(C.main(['--crawler', 'beike', '--dry-run']), 0)
        run.assert_not_called()

    def test_timeout_reaps_process(self):
        process = mock.MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired('crawler', 1), 0]
        process.pid = 12345
        manager = mock.MagicMock()
        manager.__enter__.return_value = process
        with mock.patch.object(C.subprocess, 'Popen', return_value=manager) as launch, \
             mock.patch.object(C.os, 'killpg', create=True) as kill_group:
            with self.assertRaises(subprocess.TimeoutExpired):
                C.run_collector(['python', 'collector.py'], {}, 1)
            self.assertEqual(launch.call_args.kwargs['stdin'], subprocess.DEVNULL)
            if os.name == 'posix':
                kill_group.assert_called_once_with(process.pid, C.signal.SIGKILL)
            else:
                process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)


class MacroCloudTests(unittest.TestCase):
    def test_one_source_preserves_other_source_and_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            previous = {'lpr': {'as_of': '2026-09-20'}, 'realestate': {'month': '2026-08'}}
            (out / 'clean.json').write_text(json.dumps(previous), encoding='utf-8')
            with mock.patch.object(M, 'OUT', out), \
                 mock.patch.object(M, 'collect_lpr', side_effect=OSError('blocked')), \
                 mock.patch.object(M, 'collect_realestate') as other, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(M.main(['--kind', 'lpr']), 2)
                other.assert_not_called()
            self.assertEqual(json.loads((out / 'clean.json').read_text(encoding='utf-8')), previous)
            self.assertEqual(json.loads((out / 'status.json').read_text(encoding='utf-8'))['events'][0]['state'], 'failed')

    def test_geocoder_accepts_previous_cloud_result_as_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'properties.json'
            properties = [{'city': '北京', 'community': '示例小区', 'precision': 'city'}]
            source.write_text(json.dumps(properties), encoding='utf-8')
            self.assertEqual(G.load_properties(source), properties)


if __name__ == '__main__':
    unittest.main()
