"""Shared reporting-year policy. Observation dates are never relabelled.

Default snapshots prefer the previous calendar year in Asia/Shanghai, then
earlier years. Full history remains available for explicit period selection.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import re

CHINA_TIME = timezone(timedelta(hours=8))
VERSION = 'previous-year-first-v1'


def target_year(today=None):
    today = today or datetime.now(CHINA_TIME)
    if isinstance(today, datetime) and today.tzinfo is not None:
        today = today.astimezone(CHINA_TIME)
    return today.year - 1


def period_year(period):
    match = re.match(r'^(\d{4})(?:$|[-./年Q])', str(period or ''))
    return int(match[1]) if match else None


def candidate_years(available=(), today=None):
    """Probe the target and two preceding years, plus all known older years."""
    target = target_year(today)
    known = {period_year(value) for value in available}
    return sorted({target, target - 1, target - 2} |
                  {year for year in known if year is not None and year <= target}, reverse=True)


def select_period(periods, today=None, ceiling=None):
    limit = min(target_year(today), ceiling) if ceiling is not None else target_year(today)
    valid = [value for value in periods if period_year(value) is not None and period_year(value) <= limit]
    return max(valid, key=lambda value: (period_year(value), str(value)), default=None)


def selection(periods, today=None, reason=None):
    periods = list(periods)
    selected = select_period(periods, today)
    target = target_year(today)
    actual = period_year(selected)
    fallback = actual is not None and actual < target
    return dict(target_year=target, selected_year=actual, selected_period=selected,
                fallback=fallback, lag_years=target - actual if actual is not None else None,
                state='missing' if selected is None else 'fallback' if fallback else 'preferred',
                reason=(reason or f'{target}年数据缺失或未通过完整性核验，采用最近可用历史期')
                if fallback else (f'暂无{target}年及更早的可用数据' if selected is None else None))


def apply_policy(data, today=None):
    """Publish defaults and an audit of actual years without pruning any history."""
    info = data.setdefault('information', {})
    income = info.get('income', {})
    gdp = info.get('gdp', {})
    vacancy = info.get('vacancy', {})
    macro = info.get('macro', {})
    sources = {
        'housing_prices': [r['date'] for c in data.get('cities', []) for r in c.get('monthly', [])
                           if isinstance(r.get('price'), (int, float)) and r['price'] > 0],
        'nbs': [r['month'] for r in info.get('releases', []) if r.get('rows')],
        'income': [r['year'] for r in income.get('income', []) if r.get('metrics')],
        'minimum_wages': [r['as_of'] for r in income.get('minimum_wages', []) if r.get('rows')],
        'gdp': [r.get('year', gdp.get('year')) for r in gdp.get('provinces', [])],
        'vacancy_unsold': [r.get('unsold_as_of') for r in vacancy.get('rows', [])],
        'vacancy_stock': [r.get('stock_as_of') for r in vacancy.get('rows', [])],
        'realestate': [r.get('month') for r in macro.get('realestate', {}).get('history', [])],
        'lpr': [r.get('as_of') for r in macro.get('lpr', {}).get('history', [])],
    }
    for kind in ('new_house', 'second_hand', 'rent', 'land'):
        sources['cih_' + kind] = [r['period'] for r in info.get('cih', {}).get('prices', [])
                                 if r.get('kind') == kind and r.get('price', 0) > 0]
    policies = {}
    for name, periods in sources.items():
        policies[name] = selection(periods, today)
        policies[name]['year_counts'] = dict(sorted(Counter(
            str(period_year(value)) for value in periods if period_year(value) is not None).items()))
    policies['vacancy_stock']['reason'] = '住宅存量为低频人口普查数据，保留普查原始时点，未推算为目标年存量'
    policies['vacancy_unsold']['scope'] = 'per_region'
    policies['vacancy_unsold']['fallback_regions'] = sum(
        int(count) for year, count in policies['vacancy_unsold']['year_counts'].items() if int(year) < target_year(today))
    policy = dict(version=VERSION, timezone='Asia/Shanghai', target_year=target_year(today),
                  rule='优先上一日历年，缺失或核验失败时逐年回退；保留真实统计期与完整历史', sources=policies)
    data.setdefault('meta', {})['data_period_policy'] = policy
    return data
