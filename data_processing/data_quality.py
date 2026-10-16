"""Evidence audit and comparable calendar-month analytics (no truth probabilities)."""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from price_provenance import apply_provenance

ROOT = Path(__file__).resolve().parent.parent


def month_id(value):
    y, m = map(int, value.split('-'))
    return y * 12 + m - 1


def audit(data, root=ROOT, indices=None):
    observations = {}
    for path in sorted((root / 'output' / 'anjuke').glob('*.json')):
        rows = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get('year') and row.get('month'):
                observations[(row.get('city'), f"{int(row['year']):04d}-{int(row['month']):02d}")] = row
    now = date.today()
    monthly_market = {}
    for city in data['cities']:
        rows = city['monthly']
        by_date = {r['date']: r for r in rows}
        issues, traced, direct = [], 0, 0
        for r in rows:
            raw = observations.get((city['city'], r['date']), {})
            apply_provenance(r,raw,observations.get((city['city'],r.get('anchor_date')),{}))
            url = raw.get('source_url') or raw.get('url')
            captured = raw.get('crawl_time') or raw.get('fetched_at')
            is_direct = r.get('source') == 'anjuke'
            direct += int(is_direct)
            traced += int(is_direct and bool(url and captured))
            if r.get('method')=='legacy_unverified':
                issues.append({'date':r['date'],'type':'unverified_legacy','detail':'历史导入值没有可追溯来源，不得宣称安居客实采'})
            if r.get('method')=='unavailable':
                issues.append({'date':r['date'],'type':'missing_verified_anchor','detail':'没有可核对的价格锚点，绝对价格留空'})
            if r.get('change_pct') is not None and abs(r['change_pct']) > 10:
                issues.append({'date': r['date'], 'type': 'price_jump',
                               'detail': f"月度变化 {r['change_pct']:+.2f}%"})
            monthly_market.setdefault(r['date'], []).append(r)
        city['source_type']=rows[-1]['source']
        city['anjuke_count']=direct
        city['legacy_count']=sum(r.get('method')=='legacy_unverified' for r in rows)
        city['stats_count']=sum(r.get('method') in ('index_estimate','reference_estimate') for r in rows)
        span = month_id(rows[-1]['date']) - month_id(rows[0]['date']) + 1
        missing = span - len(rows)
        if missing:
            issues.append({'type': 'missing_months', 'detail': f'区间缺失 {missing} 个月'})
        if direct > traced:
            issues.append({'type': 'missing_provenance', 'detail': f'{direct-traced} 条挂牌记录缺少原始链接或采集时间'})
        ref = city.get('fang_reference')
        if ref:
            comparable = by_date.get(ref['month'])
            ref['comparison_month'] = ref['month'] if comparable else None
            ref['deviation_pct'] = round((comparable['price'] / ref['price'] - 1) * 100, 2) if comparable and comparable.get('price') and ref.get('price') else None
            ref['evidence_status'] = 'unverified_reference'
            if ref['deviation_pct'] is not None and abs(ref['deviation_pct']) > 20:
                issues.append({'type': 'reference_divergence', 'detail': f"同月参考价偏差 {ref['deviation_pct']:+.2f}%"})
        # Leave-one-out evaluation: hide each observed price and predict from the previous one.
        from inverse_price_engine import propagate_price
        city_indices = {(y, m): v for (c, y, m), v in (indices or {}).items() if c == city['city']}
        observed = [r for r in rows if r['source'] == 'anjuke']
        errors = []
        for previous, target in zip(observed, observed[1:]):
            if month_id(target['date']) - month_id(previous['date']) > 12:
                continue
            estimate = propagate_price(previous['price'], tuple(map(int, previous['date'].split('-'))),
                                       tuple(map(int, target['date'].split('-'))), city_indices)
            if estimate is not None:
                errors.append(abs(estimate / target['price'] - 1) * 100)
        lag = now.year * 12 + now.month - 1 - month_id(rows[-1]['date'])
        city['audit'] = {'status': 'needs_review' if issues else 'limited_evidence',
                         'verdict': '待核验' if issues else '证据有限',
                         'latest_month': rows[-1]['date'], 'lag_months': lag,
                         'observed_months': direct, 'estimated_months': city['stats_count'], 'unverified_months':city['legacy_count'],
                         'traceable_months': traced, 'missing_months': missing,
                         'unavailable_months':sum(r.get('price') is None for r in rows),
                         'coverage_pct': round(len(rows)/span*100, 1),
                         'backtest_n': len(errors),
                         'backtest_mape': round(sum(errors)/len(errors), 2) if errors else None,
                         'backtest_median_ape': round(median(errors), 2) if errors else None,
                         'issues': issues, 'transaction_verified': False}
    market = []
    for month, rows in sorted(monthly_market.items()):
        changes = [r['change_pct'] for r in rows if r.get('change_pct') is not None]
        prices = [r['price'] for r in rows if r.get('price') is not None and r['price']>0]
        market.append({'month': month, 'cities': len(rows),
                       'median_price': round(median(prices)) if prices else None,
                       'rising': sum(v > 0 for v in changes), 'falling': sum(v < 0 for v in changes),
                       'flat': sum(v == 0 for v in changes), 'comparable': len(changes),
                       'observed': sum(r['source'] == 'anjuke' for r in rows)})
    data.setdefault('meta',{}).update(method='原始快照核对通过的安居客数据优先；缺月按近距离观测锚点与完整统计局环比链计算；旧固定锚点模型仅作对照',price_source='安居客数据 / 逆推结构',data_quality='公开挂牌价格与指数推导，不等于核验成交价；无核验锚点的绝对价格留空')
    data['analytics'] = {'market': market, 'generated_at': datetime.now(timezone.utc).isoformat(),
                         'price_basis': '城市二手住宅挂牌参考价及指数推导价，不代表真实成交价',
                         'backtest_basis': '以前一挂牌观测值和完整环比链预测下一观测值；误差衡量两种口径一致性，不代表成交误差',
                         'truth_statement': '统计局发布价格指数而非元/平方米价格；缺少成交凭证，不能确认房价真实性。'}
    return data


def main():
    from inverse_price_engine import load_stats_mom_data
    indices = load_stats_mom_data()
    target = ROOT / 'data_processing' / 'frontend' / 'intersection_housing_data.json'
    data = audit(json.loads(target.read_text(encoding='utf-8')), indices=indices)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f"[audit] {len(data['cities'])} cities audited")


if __name__ == '__main__':
    main()
