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


def browser_probe(browser, target, out):
    # Every target starts with a new anonymous context, never a persistent profile.
    with browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai',
                             viewport={'width': 1440, 'height': 1000},
                             service_workers='block') as context:
        if context.cookies():
            raise RuntimeError('Anonymous acceptance started with non-empty cookies')
        page = context.new_page()
        network = []
        requests_sent = 0

        def route_request(route):
            nonlocal requests_sent
            request = route.request
            if request.resource_type in ('image', 'media', 'font') or requests_sent >= 80:
                route.abort()
            else:
                requests_sent += 1
                route.continue_()

        page.route('**/*', route_request)
        page.on('response', lambda r: network.append(dict(
            url=public_url(r.url), status=r.status, resource_type=r.request.resource_type)))
        error = None
        status = 0
        try:
            response = page.goto(target['url'], wait_until='domcontentloaded', timeout=45000)
            status = response.status if response else 0
            # Allow normal browser JavaScript and its anonymous session to settle.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                body = page.content()
                parsed = parse_response(target, body, page.url, status)
                if parsed['state'] in ('observed', 'challenge', 'login_required'):
                    break
                page.wait_for_timeout(1000)
        except Exception as exc:
            error = type(exc).__name__
        try:
            body, final_url = page.content(), page.url
        except Exception:
            body, final_url = '', target['url']
        return retain(target, 'chromium', body, final_url, status, out=out,
                      error=error, network=network)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Live acceptance must run on GitHub Actions; local results are not cloud proof')
    from playwright.sync_api import sync_playwright
    report = dict(schema='anjuke_anonymous/v1', started_at=datetime.now(timezone.utc).isoformat(),
                  run_id=os.environ.get('GITHUB_RUN_ID'), commit=os.environ.get('GITHUB_SHA'),
                  account_used=False, imported_storage=False, restored_cache=False, attempts=[])
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        with p.chromium.launch(headless=True) as browser:
            for target in targets():
                for fetch in (http_probe, lambda target, out: browser_probe(browser, target, out)):
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
