"""Shared Chromium sessions for cloud runners and optional remote browsers.

The default uses Playwright's bundled Chromium, never a machine's Edge install.
Remote sessions use a fresh context; local profiles and login state are not sent
to a provider. Request routing and evidence collection remain with each crawler.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def profile_path(*parts):
    root = os.environ.get('CRAWLER_PROFILE_ROOT')
    return str((Path(root) if root else Path(tempfile.gettempdir()) /
                'housing-crawler-profiles').joinpath(*parts))


def cloud_mode():
    return (os.environ.get('CRAWLER_CLOUD') == '1' or
            os.environ.get('GITHUB_ACTIONS', '').lower() == 'true')


def browser_channel(channel=None):
    value = channel or os.environ.get('CRAWLER_BROWSER_CHANNEL', 'chromium')
    if cloud_mode() and value != 'chromium':
        raise ValueError('Cloud collection uses bundled Chromium; remove the system browser channel')
    return None if value == 'chromium' else value


def remote_endpoint():
    ws = os.environ.get('CRAWLER_BROWSER_WS_ENDPOINT', '').strip()
    cdp = os.environ.get('CRAWLER_BROWSER_CDP_ENDPOINT', '').strip()
    if ws and cdp:
        raise ValueError('Configure only one remote browser endpoint')
    return ('playwright', ws) if ws else ('cdp', cdp) if cdp else (None, None)


def launch_browser(playwright, *, headless=True, channel=None):
    if cloud_mode() and not headless:
        raise ValueError('Cloud collection requires a headless browser')
    selected = browser_channel(channel)
    protocol, endpoint = remote_endpoint()
    if endpoint:
        connect = (playwright.chromium.connect if protocol == 'playwright'
                   else playwright.chromium.connect_over_cdp)
        try:
            return connect(endpoint, timeout=30000)
        except Exception:
            # Provider URLs can contain credentials; keep them out of run logs.
            raise RuntimeError('Remote browser connection failed; check the endpoint secret and provider status') from None
    options = {'headless': headless}
    if selected:
        options['channel'] = selected
    return playwright.chromium.launch(**options)


@contextmanager
def browser_context(*, profile_dir=None, channel=None, headless=True, **options):
    """Own and clean up one context, including partial startup failures."""
    if cloud_mode() and not headless:
        raise ValueError('Cloud collection requires a headless browser')
    browser_channel(channel)
    from playwright.sync_api import sync_playwright

    browser = context = None
    with sync_playwright() as playwright:
        try:
            if profile_dir and not remote_endpoint()[1]:
                launch = dict(options, user_data_dir=str(profile_dir), headless=headless)
                selected = browser_channel(channel)
                if selected:
                    launch['channel'] = selected
                context = playwright.chromium.launch_persistent_context(**launch)
            else:
                browser = launch_browser(playwright, headless=headless, channel=channel)
                context = browser.new_context(**options)
            yield context
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()
