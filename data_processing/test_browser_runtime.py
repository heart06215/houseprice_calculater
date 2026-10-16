"""Browser ownership and cloud portability without starting a real browser."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import browser_runtime as B


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.playwright = mock.MagicMock()

    def test_default_launch_uses_bundled_chromium(self):
        B.launch_browser(self.playwright)
        self.playwright.chromium.launch.assert_called_once_with(headless=True)

    def test_cloud_rejects_headful_and_machine_browser_before_connecting(self):
        for env in ({'GITHUB_ACTIONS': 'true'}, {'CRAWLER_CLOUD': '1'}):
            with mock.patch.dict(os.environ, env):
                for kwargs in ({'headless': False}, {'channel': 'msedge'}, {'channel': 'chrome'}):
                    with self.assertRaises(ValueError):
                        B.launch_browser(self.playwright, **kwargs)
                with self.assertRaises(ValueError):
                    with B.browser_context(headless=False):
                        self.fail('Cloud must reject headed contexts')
        self.playwright.chromium.launch.assert_not_called()

    def test_remote_protocol_and_conflicting_endpoints(self):
        for key, method in (('CRAWLER_BROWSER_WS_ENDPOINT', 'connect'),
                            ('CRAWLER_BROWSER_CDP_ENDPOINT', 'connect_over_cdp')):
            with mock.patch.dict(os.environ, {key: 'wss://browser.example/session'}):
                B.launch_browser(self.playwright)
                getattr(self.playwright.chromium, method).assert_called_once_with(
                    'wss://browser.example/session', timeout=30000)
        self.playwright.chromium.launch.assert_not_called()
        with mock.patch.dict(os.environ, {'CRAWLER_BROWSER_WS_ENDPOINT': 'a',
                                          'CRAWLER_BROWSER_CDP_ENDPOINT': 'b'}):
            with self.assertRaises(ValueError):
                B.remote_endpoint()

    def test_connection_errors_do_not_print_endpoint_credentials(self):
        secret = 'wss://user:password@browser.example/session?token=secret'
        with mock.patch.dict(os.environ, {'CRAWLER_BROWSER_WS_ENDPOINT': secret}):
            self.playwright.chromium.connect.side_effect = RuntimeError(secret)
            with self.assertRaises(RuntimeError) as caught:
                B.launch_browser(self.playwright)
            self.assertNotIn('password', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def api(self):
        factory = mock.MagicMock()
        factory.return_value.__enter__.return_value = self.playwright
        return mock.patch.dict(sys.modules, {'playwright.sync_api': SimpleNamespace(sync_playwright=factory)})

    def test_partial_context_startup_still_disconnects_remote_browser(self):
        with self.api(), mock.patch.dict(os.environ, {'CRAWLER_BROWSER_WS_ENDPOINT': 'wss://browser.example'}):
            browser = self.playwright.chromium.connect.return_value
            browser.new_context.side_effect = RuntimeError('context unavailable')
            with self.assertRaises(RuntimeError):
                with B.browser_context(profile_dir='/unused-profile'):
                    pass
            browser.close.assert_called_once()

    def test_remote_context_never_reuses_personal_profile(self):
        with self.api(), mock.patch.dict(os.environ, {'CRAWLER_BROWSER_CDP_ENDPOINT': 'https://browser.example'}):
            browser = self.playwright.chromium.connect_over_cdp.return_value
            with B.browser_context(profile_dir='/unused-profile', locale='zh-CN') as context:
                self.assertIs(context, browser.new_context.return_value)
            self.playwright.chromium.launch_persistent_context.assert_not_called()
            browser.new_context.assert_called_once_with(locale='zh-CN')
            context.close.assert_called_once()
            browser.close.assert_called_once()

    def test_persistent_context_is_closed_even_when_collection_fails(self):
        with self.api():
            context = self.playwright.chromium.launch_persistent_context.return_value
            with self.assertRaises(RuntimeError):
                with B.browser_context(profile_dir='/runner-profile'):
                    raise RuntimeError('parser failed')
            context.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
