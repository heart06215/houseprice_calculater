"""Main dashboard refresh: isolated crawl, evidence checks, replay and safe publication.

python data_processing/pipeline.py
python data_processing/pipeline.py --skip-crawl
"""
import argparse
import sys
from update_data import update


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-crawl', action='store_true', help='Offline replay from verified saved inputs')
    parser.add_argument('--sources', help='Optional subset: nbs,income,anjuke,cih,macro')
    parser.add_argument('--full', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--skip-calibrate', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--crawl-cih', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    names = args.sources.split(',') if args.sources else None
    return update(names=names, force=True, offline=args.skip_crawl, dashboard_only=True)


if __name__ == '__main__':
    sys.exit(main())
