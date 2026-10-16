"""Bounded, resumable listing collection with raw evidence and a failure circuit."""
import argparse
import hashlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
import anjuke_province_crawler as crawler

ROOT = Path(__file__).resolve().parent.parent


def run(cities=None, start_year=2021, max_requests=120, delay=3, failure_limit=5):
    today = date.today()
    raw_dir = ROOT / 'output' / 'raw' / 'anjuke'
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'requests': 0,
              'added': 0, 'pages': [], 'stopped_reason': 'complete'}
    failures = 0
    stop = False
    for city, slug in sorted(crawler.CITY_SLUG_70.items()):
        if (cities and city not in cities) or city in crawler.CITY_DATA_BUG:
            continue
        have = crawler.existing_year_months(slug)
        for year in range(today.year, start_year - 1, -1):
            expected = today.month - 1 if year == today.year else 12
            if year != today.year and sum(y == year for y, m in have) >= expected:
                continue
            parsed_year = False
            for url in crawler.city_page_urls(city, slug, year):
                if report['requests'] >= max_requests or failures >= failure_limit:
                    report['stopped_reason'] = 'request_limit' if report['requests'] >= max_requests else 'consecutive_failures'
                    stop = True
                    break
                report['requests'] += 1
                captured = datetime.now(timezone.utc).isoformat()
                html = crawler.fetch_fangjia_page(url, retries=0)
                info = crawler.extract_trend(html) if html else None
                entry = {'city': city, 'url': url, 'fetched_at': captured, 'status': 'failed'}
                # An explicit city match prevents cross-city payload contamination.
                if info and info.get('city') == city and info.get('blocks'):
                    rows = crawler.collect_rows(info, today.year, today.month, set())
                    digest = hashlib.sha256(html.encode('utf-8')).hexdigest()
                    raw_path = raw_dir / f'{slug}_{year}_{digest[:12]}.html'
                    raw_path.write_text(html, encoding='utf-8')
                    evidence = {key: {'source_url': url, 'fetched_at': captured,
                                      'raw_sha256': digest, 'raw_file': str(raw_path.relative_to(ROOT)),
                                      'price_basis': 'listing'} for key in rows}
                    _, added, conflicts = crawler.merge_city_rows(slug, city, rows, evidence)
                    have.update(rows)
                    report['added'] += added
                    entry.update(status='parsed', added=added, conflicts=conflicts, raw_sha256=digest)
                    failures = 0
                    parsed_year = True
                else:
                    failures += 1
                report['pages'].append(entry)
                print(f"{city} {year}: {entry['status']}, +{entry.get('added', 0)}", flush=True)
                report_path = ROOT / 'output' / 'crawl_report.json'
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                time.sleep(delay)
                if entry['status'] == 'parsed':
                    break
            if stop:
                break
            if not parsed_year:
                break
        if stop:
            break
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    (ROOT / 'output' / 'crawl_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'pages'}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cities', default='')
    parser.add_argument('--from-year', type=int, default=2021)
    parser.add_argument('--max-requests', type=int, default=120)
    args = parser.parse_args()
    if not 2000 <= args.from_year <= date.today().year or args.max_requests < 1:
        parser.error('invalid year or request limit')
    run(set(args.cities.split(',')) if args.cities else None, args.from_year, args.max_requests)
