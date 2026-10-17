"""Live acceptance of public Anjuke data in fresh, account-free cloud sessions."""

import argparse
import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from anjuke_browser_probe import detect_verify, detect_login_wall, sanitize
from anjuke_community import parse_catalog
from anjuke_province_crawler import collect_rows, extract_trend
from data_store import ROOT, atomic_json
from market_crawler import parse_properties

OUT = ROOT / 'output/anjuke_anonymous'


def targets():
    year = date.today().year
    return [
        dict(key='beijing_year', city='北京', kind='prices', url=f'https://www.anjuke.com/fangjia/beijing{year}/'),
        dict(key='beijing_market', city='北京', kind='prices', url='https://beijing.anjuke.com/market/'),
        dict(key='shanghai_market', city='上海', kind='prices', url='https://shanghai.anjuke.com/market/'),
        dict(key='beijing_communities', city='北京', kind='communities', url='https://m.anjuke.com/bj/xiaoqu/'),
        dict(key='beijing_properties', city='北京', kind='properties', url='https://bj.anjuke.com/sale/'),
        dict(key='beijing_mobile_properties', city='北京', kind='properties', url='https://m.anjuke.com/bj/sale/'),
        dict(key='beijing_mobile_home', city='北京', kind='properties', url='https://m.anjuke.com/bj/'),
        dict(key='beijing_mobile_trend', city='北京', kind='properties', url='https://m.anjuke.com/bj/trendency/sale/'),
    ]


def public_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))


def parse_response(target, body, final_url, status):
    """Fresh response facts only: existing records and fallback data are never read."""
    challenge, challenge_kind = detect_verify(final_url, body)
    login = detect_login_wall(body)
    result = dict(state='unparsed', records=[], challenge=challenge,
                  challenge_kind=challenge_kind, login_wall=login)
    if 'captcha' in urlsplit(final_url).path.lower():
        result.update(challenge=True, challenge_kind='captcha_redirect')
        challenge = True
    if challenge:
        result['state'] = 'challenge'
        return result
    if status != 200:
        result['state'] = 'rate_limited' if status == 429 else 'http_error'
        return result
    if target['kind'] == 'prices':
        info = extract_trend(body) or {}
        if info.get('city') == target['city']:
            now = date.today()
            rows = collect_rows(info, now.year, now.month, set())
            result['records'] = [dict(city=target['city'], month=f'{y:04d}-{m:02d}', price=price)
                                 for (y, m), price in sorted(rows.items())]
    elif target['kind'] == 'communities':
        parsed = parse_catalog(body, url=final_url, fetched_at=datetime.now(timezone.utc).isoformat())
        result['records'] = parsed['records']
    else:
        result['records'] = parse_properties(body, target['city'], final_url)
    if result['records']:
        result['state'] = 'observed'
    elif login:
        result['state'] = 'login_required'
    return result


def retain(target, transport, body, final_url, status, *, out=OUT, error=None, network=None):
    parsed = parse_response(target, body, final_url, status)
    content = sanitize(body).encode('utf-8')
    digest = hashlib.sha256(content).hexdigest()
    path = out / 'responses' / f'{target["key"]}-{transport}.html'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    # Retain the delivered scripts for offline REA analysis; never execute them here.
    soup = BeautifulSoup(sanitize(body), 'html.parser')
    scripts = []
    for index, script in enumerate(soup.find_all('script')):
        if script.get('src'):
            scripts.append(dict(url=public_url(urljoin(final_url, script['src']))))
        elif script.string and script.string.strip():
            source = script.string
            dest = out / 'scripts' / f'{target["key"]}-{transport}-{index}.js'
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source, encoding='utf-8')
            scripts.append(dict(path=dest.relative_to(out).as_posix()))
    return dict(target=target['key'], city=target['city'], kind=target['kind'],
                transport=transport, url=target['url'], final_url=public_url(final_url),
                status=status, bytes=len(content), sha256=digest,
                snapshot=path.relative_to(out).as_posix(), scripts=scripts,
                error=error, network=network or [], **parsed)


def http_probe(target, out):
    # This session has no browser profile, supplied cookies, auth, or environment proxy.
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({'Accept-Language': 'zh-CN,zh;q=0.9'})
        try:
            response = session.get(target['url'], timeout=(15, 30), allow_redirects=False)
            final_url = urljoin(target['url'], response.headers.get('Location') or target['url'])
            return retain(target, 'http', response.text, final_url, response.status_code, out=out)
        except requests.RequestException as exc:
            return retain(target, 'http', '', target['url'], 0, out=out, error=type(exc).__name__)


SDK_HOSTS = {'cstaticdun.126.net', 'cstaticdun1.126.net', 'cstatic.dun.163yun.com'}


def sdk_loader(url):
    parsed = urlsplit(url)
    return parsed.hostname in SDK_HOSTS and parsed.path == '/load.min.js'


def defense_result(body):
    """Retain result shapes/codes, never validation values or session identifiers."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return dict(json=False)
    if not isinstance(data, dict):
        return dict(json=True, shape=type(data).__name__)
    return dict(json=True, code=data.get('code') if isinstance(data.get('code'), (int, str)) else None,
                data_shape=type(data.get('data')).__name__,
                captcha_config=isinstance(data.get('data'), dict) and
                bool(data['data'].get('captchaId') or data['data'].get('id')),
                redirect=isinstance(data.get('msg'), str) and
                data['msg'].startswith(('https://', 'http://')))


def browser_probe(browser, target, out, sdk_fallback=False, activate_challenge=False, solve_slider=False):
    # Every target starts with a new anonymous context, never a persistent profile.
    with browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai',
                             viewport={'width': 1440, 'height': 1000},
                             service_workers='block') as context:
        if context.cookies():
            raise RuntimeError('Anonymous acceptance started with non-empty cookies')
        page = context.new_page()
        network = []
        script_files = []
        actions = []
        slider_result = None
        requests_sent = 0
        blocked_sdk = 0
        transport = 'chromium-sdk-fallback' if sdk_fallback else 'chromium'

        def route_request(route):
            nonlocal requests_sent, blocked_sdk
            request = route.request
            if sdk_fallback and sdk_loader(request.url):
                blocked_sdk += 1
                route.abort('failed')
            elif request.resource_type in ('media', 'font') or requests_sent >= 160:
                route.abort()
            else:
                requests_sent += 1
                route.continue_()

        page.route('**/*', route_request)
        def received(response):
            parsed_url = urlsplit(response.url)
            row = dict(url=public_url(response.url), status=response.status,
                       resource_type=response.request.resource_type)
            network.append(row)
            try:
                if parsed_url.hostname == 'callback.58.com' and parsed_url.path in (
                        '/antibot/yidun/register.do', '/antibot/yidun/checkcode.do'):
                    row['result'] = defense_result(response.text())
                if parsed_url.hostname == 'c.dun.163.com' and parsed_url.path.startswith('/api/'):
                    # JSONP is inspected as text, never evaluated.
                    source = response.text()
                    payload = json.loads(source[source.index('{'):source.rindex('}') + 1])
                    data = payload.get('data') or {}
                    row['challenge_result'] = dict(error=payload.get('error'),
                                                   type=data.get('type') if isinstance(data, dict) else None,
                                                   verified=data.get('result') if isinstance(data, dict)
                                                   and isinstance(data.get('result'), bool) else None)
                if response.request.resource_type == 'script' and (
                        parsed_url.hostname == 'j1.58cdn.com.cn' or
                        parsed_url.hostname in SDK_HOSTS) and len(script_files) < 8:
                    body = response.body()
                    if len(body) <= 1000000:
                        dest = out / 'external_scripts' / f'{target["key"]}-{transport}-{len(script_files)}.js'
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(body)
                        script_files.append(dict(path=dest.relative_to(out).as_posix(),
                                                 url=public_url(response.url),
                                                 sha256=hashlib.sha256(body).hexdigest()))
            except Exception as exc:
                row['capture_error'] = type(exc).__name__

        page.on('response', received)
        error = None
        status = 0
        try:
            for navigation in range(3 if sdk_fallback else 1):
                response = (page.reload(wait_until='domcontentloaded', timeout=45000) if navigation else
                            page.goto(target['url'], wait_until='domcontentloaded', timeout=45000))
                status = response.status if response else 0
                # The delivered wrapper persists its own anonymous error count.
                # Let register/init/check callbacks finish even on a challenge DOM.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    body = page.content()
                    parsed = parse_response(target, body, page.url, status)
                    if parsed['state'] in ('observed', 'login_required'):
                        break
                    if activate_challenge and not sdk_fallback and not actions and \
                            urlsplit(page.url).hostname == 'callback.58.com' and \
                            any('/core-optimi.' in n['url'] for n in network):
                        button = page.locator('#btnSubmit')
                        if button.count() and button.is_visible():
                            button.click(timeout=3000)
                            actions.append('open_site_challenge')
                    if solve_slider and slider_result is None and \
                            page.locator('img.yidun_jigsaw:visible').count():
                        from anjuke_slider_probe import try_slider
                        try:
                            slider_result = try_slider(page, context, out, target['key'])
                        except Exception as exc:
                            slider_result = dict(state='error', error=type(exc).__name__)
                        if slider_result['state'] == 'not_ready':
                            slider_result = None
                        else:
                            deadline = max(deadline, time.monotonic() + 10)
                    page.wait_for_timeout(500)
                if parsed['state'] in ('observed', 'login_required'):
                    break
        except Exception as exc:
            error = type(exc).__name__
        try:
            body, final_url = page.content(), page.url
        except Exception:
            body, final_url = '', target['url']
        result = retain(target, transport, body, final_url, status, out=out,
                        error=error, network=network)
        result.update(external_scripts=script_files, blocked_sdk_requests=blocked_sdk, actions=actions)
        result['slider_result'] = slider_result
        if result['state'] == 'challenge':
            screenshot = out / 'screenshots' / f'{target["key"]}-{transport}.png'
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(screenshot), timeout=5000)
                result['screenshot'] = screenshot.relative_to(out).as_posix()
            except Exception as exc:
                result['screenshot_error'] = type(exc).__name__
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--browser-only', action='store_true')
    parser.add_argument('--headed', action='store_true', help='Cloud Xvfb diagnostic only')
    parser.add_argument('--sdk-fallback', action='store_true', help='Compare observed SDK loader error fallback')
    parser.add_argument('--activate-challenge', action='store_true', help='Open the site-provided verification UI')
    parser.add_argument('--solve-slider', action='store_true', help='Try one image-matched drag per target')
    parser.add_argument('--targets', help='Comma-separated target keys; omitted means all')
    args = parser.parse_args(argv)
    if args.solve_slider:
        args.activate_challenge = True
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Live acceptance must run on GitHub Actions; local results are not cloud proof')
    from playwright.sync_api import sync_playwright
    selected = targets()
    if args.targets:
        keys = set(args.targets.split(','))
        unknown = keys - {t['key'] for t in selected}
        if unknown:
            parser.error('Unknown target keys: ' + ','.join(sorted(unknown)))
        selected = [t for t in selected if t['key'] in keys]
    report = dict(schema='anjuke_anonymous/v1', started_at=datetime.now(timezone.utc).isoformat(),
                  run_id=os.environ.get('GITHUB_RUN_ID'), commit=os.environ.get('GITHUB_SHA'),
                  account_used=False, imported_storage=False, restored_cache=False,
                  browser_only=args.browser_only, headed=args.headed,
                  sdk_fallback=args.sdk_fallback,
                  activate_challenge=args.activate_challenge,
                  solve_slider=args.solve_slider,
                  browser_args=['--disable-blink-features=AutomationControlled'], attempts=[])
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        with p.chromium.launch(headless=not args.headed, args=report['browser_args']) as browser:
            for target in selected:
                fetchers = [lambda target, out: browser_probe(browser, target, out,
                                                             activate_challenge=args.activate_challenge,
                                                             solve_slider=args.solve_slider)]
                if not args.browser_only:
                    fetchers.insert(0, http_probe)
                if args.sdk_fallback:
                    fetchers.append(lambda target, out: browser_probe(browser, target, out, sdk_fallback=True))
                for fetch in fetchers:
                    attempt = fetch(target, args.out)
                    report['attempts'].append(attempt)
                    atomic_json(args.out / 'report.json', report)
                    print(json.dumps({k: attempt[k] for k in ('target', 'transport', 'status', 'state')}
                                     | {'records': len(attempt['records'])}), flush=True)
                    time.sleep(8)
                    if attempt['state'] == 'observed':
                        break
    observed = {a['kind'] for a in report['attempts'] if a['state'] == 'observed'}
    report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                  observed_kinds=sorted(observed),
                  accepted=observed == {'prices', 'communities', 'properties'})
    atomic_json(args.out / 'report.json', report)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('## Anonymous Anjuke live acceptance\n\n'
                         f'Account: none. Imported state/cache: none. Accepted: {report["accepted"]}.\n\n'
                         '| Target | Transport | State | Fresh records |\n| --- | --- | --- | --- |\n')
            for a in report['attempts']:
                stream.write(f'| {a["target"]} | {a["transport"]} | {a["state"]} | {len(a["records"])} |\n')
    return 0 if report['accepted'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
