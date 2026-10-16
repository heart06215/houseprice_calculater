"""Reproducible publication audit: preserve baseline, verify every quote and derived chain."""
import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from data_store import ROOT, atomic_json, snapshot
from inverse_price_engine import load_anjuke_data, load_stats_mom_data, propagate_price

OUT = ROOT / 'output/engine_validation'
PUBLISHED = ROOT / 'data_processing/frontend/intersection_housing_data.json'
BASELINE = OUT / 'publication_baseline.json'


def capture():
    content = PUBLISHED.read_bytes()
    atomic_json(BASELINE, dict(created_at=datetime.now(timezone.utc).isoformat(),
                              raw=snapshot('price_publication_before', content)))
    print('Archived the current publication as the comparison baseline.')


def audit():
    data = json.loads(PUBLISHED.read_text(encoding='utf-8'))
    pointer = json.loads(BASELINE.read_text(encoding='utf-8'))['raw']
    previous = (ROOT / pointer['path']).read_bytes()
    if hashlib.sha256(previous).hexdigest() != pointer['sha256']:
        raise ValueError('Baseline hash mismatch')
    before = json.loads(previous)
    previous_cities = {c['city']: c for c in before['cities']}
    observations = load_anjuke_data()
    indices = load_stats_mom_data()
    errors, changes = [], []
    counts = Counter()
    for city in data['cities']:
        name = city['city']
        mom = {(y, m): v for (c, y, m), v in indices.items() if c == name}
        for row in city['monthly']:
            ym = tuple(map(int, row['date'].split('-')))
            key = (name, *ym)
            counts[row['method']] += 1
            observed = observations.get(key)
            if observed is not None:
                if row['price'] != observed or row['method'] != 'listing_observation':
                    errors.append(dict(city=name, month=row['date'], error='verified_observation_replaced'))
            elif row['method'] == 'index_estimate':
                anchor = tuple(map(int, row['anchor_date'].split('-')))
                value = propagate_price(observations.get((name, *anchor)), anchor, ym, mom)
                if value is None or row['price'] != value:
                    errors.append(dict(city=name, month=row['date'], error='derived_chain_mismatch'))
            elif row['method'] != 'unavailable' or row['price'] is not None:
                errors.append(dict(city=name, month=row['date'], error='unsupported_absolute_price'))
        old = previous_cities.get(name, {})
        latest = city['monthly'][-1]
        old_row = next((r for r in old.get('monthly', []) if r['date'] == latest['date']), {})
        value, previous_value = latest['price'], old_row.get('price')
        changes.append(dict(city=name, month=latest['date'], before=previous_value, after=value,
            previous_overstatement_pct=round((previous_value/value-1)*100,2) if value and previous_value else None,
            method=latest['method'], model_comparison=latest.get('model_comparison')))
    changed = [r for r in changes if r['before'] != r['after']]
    latest_observed = [r for r in changes if r['method'] == 'listing_observation']
    result = dict(created_at=datetime.now(timezone.utc).isoformat(),baseline=pointer,
        status='failed' if errors else 'passed',errors=errors,cities=len(data['cities']),
        verified_input_rows=len(observations),method_counts=dict(counts),changed_latest_cities=len(changed),
        latest_observed_cities=len(latest_observed),
        old_latest_overstated_gt5pct=sum((r['previous_overstatement_pct'] or 0)>5 for r in latest_observed),
        city_comparisons=changes)
    atomic_json(OUT/'publication_audit.json', result)
    snapshot('price_publication_audit', result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('city_comparisons','baseline')},ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',action='store_true')
    args=parser.parse_args()
    if args.capture:
        capture()
    else:
        sys.exit(audit())
