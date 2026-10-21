# -*- coding: utf-8 -*-
"""Local bounded community rotation (same collectors as the cloud workflow).

Uses the local desktop browser (Edge by default) for 58/Anjuke, plain HTTP for
5i5j/Fang. Nothing is imported from a browser profile or account; the local
runner is for development and evidence, not a substitute for the cloud run.

python data_processing/local_community_rotation.py --city 北京 \
    --sources 5i5j,58 --pages 2 --districts 2 --details 5
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The shared browser runtime rejects non-chromium only in cloud mode; locally Edge is fine.
os.environ.setdefault('CRAWLER_BROWSER_CHANNEL', 'msedge')

CITY_LIST = sorted(__import__('anjuke_province_crawler', fromlist=['CITY_SLUG_70']).CITY_SLUG_70)


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def run_source(source, city, *, pages, districts, details, interval, out):
    if source == '5i5j':
        import community_directory as D
        return D.collect('5i5j', city, pages=pages, details=details, interval=interval,
                         out=Path(out) / 'community_directory')
    if source == '58':
        import community_directory as D
        return D.collect('58', city, pages=pages, districts=districts, details=details,
                         interval=interval, out=Path(out) / 'community_directory')
    if source == 'anjuke':
        import community_directory as D
        return D.collect('anjuke', city, pages=pages, details=0, interval=interval,
                         out=Path(out) / 'community_directory')
    if source == 'fang':
        import fang_loupan as F
        result = F.crawl([city], max_pages=pages, max_details=details, interval=interval,
                         out=Path(out) / 'fang_loupan')
        return dict(source='fang', city=city, count=result.get('records', 0),
                    errors=[])
    raise ValueError(f'Unknown source: {source}')


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city', help='城市；留空按日期轮转')
    parser.add_argument('--sources', default='5i5j,58,anjuke,fang')
    parser.add_argument('--pages', type=int, default=2)
    parser.add_argument('--districts', type=int, default=2)
    parser.add_argument('--details', type=int, default=5)
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--out', type=Path, default=ROOT / 'output/local_community_rotation')
    args = parser.parse_args(argv)
    city = args.city or CITY_LIST[date.today().toordinal() % len(CITY_LIST)]
    sources = [s.strip() for s in args.sources.split(',') if s.strip()]
    summary = dict(schema='local_community_rotation/v1', at=now(), city=city, sources=[])
    for source in sources:
        started = time.monotonic()
        try:
            result = run_source(source, city, pages=args.pages, districts=args.districts,
                                details=args.details, interval=args.interval, out=args.out)
            summary['sources'].append(dict(source=source, ok=True,
                                           count=result.get('count', result.get('records', 0)),
                                           details=result.get('details', 0),
                                           errors=result.get('errors', [])[:5],
                                           seconds=round(time.monotonic() - started, 1)))
        except Exception as exc:
            summary['sources'].append(dict(source=source, ok=False,
                                           error=f'{type(exc).__name__}: {exc}'[:200],
                                           seconds=round(time.monotonic() - started, 1)))
        print(json.dumps(summary['sources'][-1], ensure_ascii=False), flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
    total = sum(s.get('count') or 0 for s in summary['sources'])
    print(json.dumps(dict(city=city, total=total, sources=summary['sources']),
                     ensure_ascii=False, indent=2))
    return 0 if total else 2


if __name__ == '__main__':
    raise SystemExit(main())
