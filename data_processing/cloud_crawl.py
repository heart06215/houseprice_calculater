"""Run a registered collector on a cloud runner without shell command strings."""

import argparse
import importlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / 'data_processing'
REPORT = ROOT / 'output/cloud_crawl/report.json'

# The main dashboard uses its existing isolated validation/publication pipeline.
# Individual collectors produce research artifacts, without publishing the site.
CRAWLERS = {
    'dashboard': ('update_data.py', '--dashboard-only'),
    'stats_gov': ('stats_gov_crawler.py',),
    'income': ('income_crawler.py', '--crawl'),
    'macro': ('macro_crawler.py',),
    'vacancy': ('vacancy_crawler.py', '--crawl'),
    'gdp': ('gdp_crawler.py', '--crawl'),
    'lpr': ('macro_crawler.py', '--kind', 'lpr'),
    'realestate': ('macro_crawler.py', '--kind', 'realestate'),
    'income_history': ('income_crawler.py', '--income-history'),
    'wage_history': ('income_crawler.py', '--wage-history'),
    'wage_archive': ('income_crawler.py', '--wage-archive'),
    'cih': ('cih_crawler.py',),
    'cih_land': ('cih_land.py',),
    'cih_rankings': ('cih_rankings.py', '--fetch'),
    'anjuke_prices': ('anjuke_province_crawler.py', '--crawl'),
    'anjuke_browser': ('anjuke_province_crawler.py', '--crawl-browser'),
    'anjuke_recheck': ('recheck_anjuke.py',),
    'anjuke_market': ('market_crawler.py',),
    'anjuke_anonymous': ('anjuke_anonymous.py', '--browser-only', '--headed', '--solve-slider',
                         '--targets', 'beijing_year,beijing_communities,beijing_properties,'
                                      'beijing_mobile_properties'),
    'anjuke_collect': ('anjuke_collect.py',),
    'anjuke_community': ('anjuke_community.py',),
    'anjuke_probe': ('anjuke_browser_probe.py',),
    'crawl_public': ('crawl_public.py',),
    'fang': ('fang_crawler.py',),
    'fang_loupan': ('fang_loupan.py',),
    'communities': ('community_crawler.py',),
    'community_directory': ('community_directory.py',),
    'community_profiles': ('community_profile_crawler.py',),
    'community_neighbourhoods': ('community_neighbourhood_crawler.py',),
    'hydrology': ('hydrology_crawler.py',),
    'public_locations': ('public_location_crawler.py',),
    'metro': ('public_location_crawler.py', 'metro'),
    'permits': ('public_location_crawler.py', 'permits'),
    'nanjing_projects': ('nanjing_project_crawler.py',),
    'geocode': ('geocode_communities.py',),
    'district_boundaries': ('generate_district_boundaries.py',),
    'city_regions': ('build_city_regions.py',),
    'beike': ('legacy/crawl_beike.py',),
    'source_pilot': ('source_pilot.py',),
    'source_entries': ('source_entry_probe.py',),
    'source_catalog': ('source_catalog_expand.py',),
    'external_candidates': ('external_candidate_probe.py',),
    'site_reachability': ('frontend/realestate/crawl_realestate.py',),
}


def parse_arguments(value, crawler=None):
    args = json.loads(value)
    if not isinstance(args, list) or any(not isinstance(a, str) or '\0' in a for a in args):
        raise ValueError('args-json must be a JSON array of strings')
    # All cloud jobs are unattended and must never inherit a local browser profile,
    # credential store or system-browser channel.
    desktop_flags = ('--headful', '--edge', '--manual', '--profile-dir', '--user-data-dir',
                     '--executable-path', '--channel', '--browser-profile', '--remote-debugging-port')
    for value in args:
        if value in desktop_flags or any(value.startswith(flag + '=') for flag in desktop_flags):
            raise ValueError('Cloud collectors require unattended Chromium with a fresh profile; '
                             'remove desktop-only/local-state flags')
    for index, value in enumerate(args):
        if value in ('--browser=edge', '--browser=chrome') or (
            value == '--browser' and args[index + 1:index + 2] in (['edge'], ['chrome'])
        ):
            raise ValueError('Use --browser chromium for cloud collection')
        if crawler == 'anjuke_probe' and (
            value.startswith('--path=') and value.split('=', 1)[1] in ('1', '2') or
            value == '--path' and args[index + 1:index + 2] in (['1'], ['2'])
        ):
            raise ValueError('Use --path 4 for a cloud browser, or --path 3 for offline snapshots')
    return args


def command(name, args):
    entry, *defaults = CRAWLERS[name]
    return [sys.executable, str(BASE / entry), *defaults, *args]


def check_runtime():
    """Exercise HTTP/parser imports, Node evaluation and an actual Chromium DOM."""
    print('Checking cloud collector imports...', flush=True)
    for module in ('requests', 'bs4', 'geographiclib.geodesic', 'playwright.sync_api'):
        importlib.import_module(module)
    # These parser modules underpin the registered collectors and their evidence.
    for module in ('stats_gov_crawler', 'income_crawler', 'macro_crawler', 'vacancy_crawler', 'gdp_crawler', 'cih_land',
                   'anjuke_collect', 'fang_crawler', 'hydrology_crawler', 'public_location_crawler'):
        importlib.import_module(module)
    subprocess.run(['node', '-e', 'if (JSON.parse("{\\"ok\\":true}").ok !== true) process.exit(1)'],
                   check=True, timeout=20)
    print('HTTP/parser imports and Node ready; checking Chromium DOM...', flush=True)
    from browser_runtime import browser_context

    with browser_context() as context:
        page = context.new_page()
        page.set_content('<title>Cloud crawler</title><table><tr><td>70</td></tr></table>')
        if page.title() != 'Cloud crawler' or page.locator('td').inner_text() != '70':
            raise RuntimeError('Browser DOM smoke check failed')
    for entry, *_ in CRAWLERS.values():
        path = BASE / entry
        if not path.is_file():
            raise RuntimeError('Missing registered crawler: ' + entry)
        compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec')
    print('Cloud crawler runtime ready', flush=True)


def run_collector(argv, environment, timeout):
    """Stop the entire runner process group on timeout, including Node/Chromium."""
    with subprocess.Popen(argv, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                          start_new_session=os.name == 'posix') as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crawler', choices=sorted(CRAWLERS), default='dashboard')
    parser.add_argument('--args-json', default='[]')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--timeout', type=int, default=3300)
    options = parser.parse_args(argv)
    if options.list:
        print(json.dumps(CRAWLERS, ensure_ascii=False, indent=2))
        return 0
    try:
        args = parse_arguments(options.args_json, options.crawler)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if not 1 <= options.timeout <= 21600:
        parser.error('timeout must be between 1 and 21600 seconds')
    if options.check:
        check_runtime()
        return 0
    if options.dry_run:
        print(f'Validated cloud entry: {options.crawler}, {len(args)} additional arguments')
        return 0
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1',
                       CRAWLER_CLOUD='1', CRAWLER_BROWSER_CHANNEL='chromium')
    environment['PYTHONPATH'] = str(BASE) + os.pathsep + environment.get('PYTHONPATH', '')
    report = dict(crawler=options.crawler, started_at=datetime.now(timezone.utc).isoformat())
    try:
        code = run_collector(command(options.crawler, args), environment, options.timeout)
        report.update(state='completed' if code == 0 else 'failed', exit_code=code)
    except subprocess.TimeoutExpired:
        code = 124
        report.update(state='timed_out', exit_code=code)
    except OSError:
        code = 1
        report.update(state='failed', exit_code=code, error='Unable to start the crawler process')
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    from data_store import atomic_json

    atomic_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(f'### Cloud collection: {options.crawler}\n\n'
                         f'State: `{report["state"]}` · Exit code: `{code}`\n\n'
                         'Download the crawl artifact for data, evidence and logs.\n')
    return code


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
