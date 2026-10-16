"""Bounded official macro/LPR collection with dated, independently retained records."""
import json
from datetime import datetime, timezone

from data_store import ROOT, atomic_json
from stats_gov_crawler import http_get
from lpr_crawler import LPR_URL, parse_lpr, collect as collect_lpr
from realestate_crawler import parse_report as parse_macro, collect as collect_realestate

OUT=ROOT/'output/macro'


def crawl():
    path=OUT/'clean.json'
    saved=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    events=[]
    for kind in ('lpr','realestate'):
        try:
            errors=[]
            if kind=='lpr':
                record=collect_lpr(saved.get(kind,{}),http_get)
            else:
                record,errors=collect_realestate(saved.get(kind,{}),http_get)
            old=saved.get(kind,{})
            if (record.get('as_of') or record.get('month'))<(old.get('as_of') or old.get('month','')):
                raise ValueError('Source period moved backwards')
            saved[kind]=record;atomic_json(path,saved)
            events.append(dict(kind=kind,state='partial' if errors else 'ok',
                               period=record.get('as_of') or record.get('month'),
                               **({'error':'; '.join(errors)} if errors else {})))
        except Exception as exc:events.append(dict(kind=kind,state='failed',error=str(exc)))
    status=dict(checked_at=datetime.now(timezone.utc).isoformat(),events=events)
    atomic_json(OUT/'status.json',status)
    return status


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(crawl(),ensure_ascii=False))
