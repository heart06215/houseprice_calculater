"""Check rendered income tables and ratios independently of the frontend calculator."""

import math
import re

from chart_pipeline_audit import income_for, positive, ratio, wages_for
from wage_archive import cutoff


def numbers(text):
    return [float(v.replace(',', '')) for v in re.findall(r'-?\d[\d,]*(?:\.\d+)?', text)]


def check_number(text, expected, label, digits=0):
    actual = numbers(text)
    if expected is None:
        assert not actual and '暂无' in text, f'{label}: missing value shown as {text!r}'
    else:
        assert len(actual) == 1 and math.isclose(actual[0], expected, rel_tol=0,
            abs_tol=0.50001 * 10 ** -digits), f'{label}: {text!r} != {expected}'


def tiers_text(values, digits):
    def formatted(v):
        text = f'{v:,.{digits}f}'
        return text.rstrip('0').rstrip('.') if digits else text
    return ' / '.join('—' if v is None else formatted(v) for v in values)


def snapshot(income, month):
    records = [r for r in income['minimum_wages'] if cutoff(r['as_of']) <= month + '-31']
    return max(records, key=lambda r: cutoff(r['as_of'])) if records else None


def source_urls(record):
    if not record:
        return []
    return list(dict.fromkeys([record['source_url']] +
        [r['source_url'] for r in record.get('archive_sources', [])] +
        [r['source_url'] for r in record.get('corrections', [])]))


def audit_income_panel(page, data):
    panel = page.locator('.income-panel')
    if not panel.count():
        return None
    rendered = panel.evaluate("""el => {
        const text=s=>el.querySelector(s)?.textContent.trim()||'';
        const rows=s=>[...el.querySelectorAll(s+' tbody tr')].map(tr=>[...tr.cells].map(c=>c.textContent.trim()));
        const cards=s=>[...el.querySelectorAll(s)].map(a=>({label:a.querySelector('span')?.textContent.trim(),
            income:a.querySelector('p b')?.textContent.trim()||'',body:a.querySelector('p')?.textContent.trim()||'',
            value:a.querySelector('strong')?.textContent.trim()||''}));
        return {summary:cards(':scope>.income-summary>article'),cards:cards(':scope>.income-ratios>article'),
            charts:[...el.querySelectorAll('.spark[_echarts_instance_]')].map(c=>c.getAttribute('aria-label')),
            extras:cards('.income-more>.income-summary>article'),surplus:text('.income-more>p .income-red'),
            annual_rows:rows('[aria-label="居民收支年度指标"]'),wage_rows:rows('.income-wages'),
            wage_headers:[...el.querySelectorAll('.income-wages thead th')].map(th=>th.textContent.trim()),
            sources:[...el.querySelectorAll(':scope>.income-source a')].map(a=>a.getAttribute('href')),
            wage_sources:[...el.querySelectorAll('.income-wage-table-source a')].map(a=>a.getAttribute('href')),
            source_text:text(':scope>.income-source')};
    }""")
    city_select = page.get_by_label('分析城市')
    city = city_select.input_value() if city_select.count() else ''
    month = panel.get_by_label('房价月份', exact=True).input_value()
    assert re.fullmatch(r'\d{4}-\d{2}', month), 'income month selector blank or invalid'
    if city:
        assert month == page.get_by_label('观察月份').input_value(), 'income/city observation month mismatch'
    year = int(panel.get_by_label('收入年份', exact=True).input_value())
    income = data['information']['income']
    annual = next(r for r in income['income'] if r['year'] == year)
    national = annual['metrics']['national']
    cities = [c for c in data['cities'] if not city or c['city'] == city]
    prices = [r['price'] for c in cities for r in c['monthly']
              if r['date'] == month and positive(r['price'])]
    price = sum(prices) / len(prices) if prices else None
    cost = price * 90 if positive(price) else None
    comparable = year <= int(month[:4])
    wage = snapshot(income, month)
    region = city if city in ('北京', '上海', '天津', '重庆', '深圳') else income['city_provinces'].get(city)
    wage_rows = [r for r in wage['rows'] if not city or r['region'] == region] if wage else []
    wage_values = [v for r in wage_rows for v in r['monthly'] if positive(v)]
    wage_min, wage_max = (min(wage_values), max(wage_values)) if wage_values else (None, None)
    summary = rendered['summary']
    assert len(summary) == 3
    check_number(summary[0]['value'], price, 'selected-month price')
    check_number(summary[1]['value'], cost / 10000 if cost is not None else None, '90 sqm cost', 2)
    expected_range = ([wage_min, wage_max] if city and wage_min != wage_max else
                      [wage_min] if wage_min is not None else [])
    assert numbers(summary[2]['value']) == expected_range, 'monthly minimum-wage summary'
    expected_sources = [annual['source_url']] + [s['source_url'] for s in annual.get('supplements', [])]
    if wage:
        expected_sources += [wage['source_url']]
        expected_sources += [s['source_url'] for s in wage.get('archive_sources', [])
                             if s['part'] == 'hourly' and s['source_url'] != wage['source_url']]
        expected_sources += [s['source_url'] for s in wage.get('corrections', [])]
        assert wage['as_of'] in rendered['source_text'], 'wage period provenance'
    assert rendered['sources'] == expected_sources, 'income/card source links'
    cards = rendered['cards']
    if city:
        months = [r['date'] for r in cities[0]['monthly'] if positive(r['price'])]
        all_tiers = {i + 1 for mm in months for r in wages_for(income, mm) if r['region'] == region
                     for i, v in enumerate(r['monthly']) if positive(v)}
        expected_tiers = sorted(all_tiers | {i + 1 for r in wage_rows for i, v in enumerate(r['monthly']) if positive(v)})
        expected_charts = []
        if len(months) >= 2:
            expected_charts += ['均价逐月走势', '总价逐月走势']
            if all_tiers:
                expected_charts += ['逐月倍数走势'] * len(expected_tiers)
            if any(positive(income_for(income, mm).get('national', {}).get('disposable_median')) for mm in months):
                expected_charts += ['逐月倍数走势']
        assert rendered['charts'] == expected_charts, f'{city}: expected income chart missing'
        assert len(cards) == len(expected_tiers) + 1, 'historical wage tier card omitted'
        row = wage_rows[0] if wage_rows else {}
        monthly = row.get('monthly', [])
        for card, tier in zip(cards[:-1], expected_tiers):
            assert card['label'] == f'{region} · 月最低工资第{tier}档', 'city/tier binding'
            v = monthly[tier - 1] if tier <= len(monthly) else None
            if positive(v):
                check_number(card['income'], v, f'{city} tier {tier} wage')
                assert numbers(card['body']) == [v, v * 12], 'monthly/annual wage units'
            else:
                assert not card['income'] and '所选月份暂无此档标准' in card['body'], 'retired tier current value'
            check_number(card['value'], ratio(price, v * 12) if positive(v) and price else None,
                         f'{city} tier {tier} ratio', 2)
        median = national['disposable_median']
        assert cards[-1]['label'] == '收入中位数 · 全国参考', 'local median scope'
        check_number(cards[-1]['income'], median, 'city national-reference median')
        check_number(cards[-1]['value'], ratio(price, median) if comparable and price else None, 'city median ratio', 2)
        table_record, table_rows = wage, wage_rows
    else:
        grouped = {}
        for source in data['cities']:
            for point in source['monthly']:
                if positive(point['price']):
                    grouped[point['date']] = grouped.get(point['date'], 0) + 1
        spark_months = [mm for mm, count in grouped.items() if count >= 30]
        expected_chart_count = 0
        if len(spark_months) >= 2:
            expected_chart_count = 3 + int(any(wages_for(income, mm) for mm in spark_months))
        assert rendered['charts'] == ['逐月倍数走势'] * expected_chart_count, 'national income chart missing'
        assert len(cards) == 4
        for card, (scope, metric) in zip(cards[:3], (
            ('national', 'disposable_mean'), ('national', 'disposable_median'), ('urban', 'disposable_median'))):
            denominator = annual['metrics'][scope].get(metric)
            check_number(card['income'], denominator, f'{year}/{scope}/{metric}')
            check_number(card['value'], ratio(price, denominator) if comparable and price else None,
                         f'{year}/{scope}/{metric} ratio', 2)
        check_number(cards[3]['value'], ratio(price, wage_min * 12) if positive(wage_min) and price else None,
                     'national lowest-tier ratio', 1)
        expected_rows = [[label] + [tiers_text([annual['metrics'][scope].get(metric)], 0)
                                  if annual['metrics'][scope].get(metric) is not None else '暂无'
                                  for metric in ('disposable_mean', 'disposable_median', 'consumption')]
                         for scope, label in (('national', '全国居民'), ('urban', '城镇居民'), ('rural', '农村居民'))]
        assert rendered['annual_rows'] == expected_rows, f'{year} annual income table'
        surplus = national['disposable_mean'] - national['consumption']
        for card, v in zip(rendered['extras'], (national.get('wage_income'), national.get('consumption'), surplus)):
            check_number(card['value'], v, f'{year} income/consumption summary')
        check_number(rendered['surplus'], ratio(price, surplus) if comparable and price else None, 'surplus ratio', 1)
        period = panel.get_by_label('最低工资表期', exact=True).input_value()
        table_record = wage if period == 'auto' else next(r for r in income['minimum_wages'] if r['as_of'] == period)
        table_rows = table_record['rows'] if table_record else []
        assert rendered['wage_sources'] == source_urls(table_record), 'historical wage table source links'
    # Each wage tier has its own column in the current public table. Check that
    # every reported tier is present and each numeric cell keeps its source value.
    columns = [(kind, i) for kind in ('monthly', 'hourly')
               for i in sorted({i for row in table_rows for i, value in enumerate(row[kind])
                                if isinstance(value, (int, float)) and math.isfinite(value)})]
    expected_headers = [('月' if kind == 'monthly' else '时') + str(i + 1) + '档' for kind, i in columns]
    if table_rows:
        assert rendered['wage_headers'][1:] == expected_headers, 'wage tier columns'
    expected_wages = [[r['region']] + [tiers_text([r[kind][i] if i < len(r[kind]) else None],
                       0 if kind == 'monthly' else 1) for kind, i in columns] for r in table_rows]
    assert rendered['wage_rows'] == expected_wages, f'{city or "national"}/{month} wage table values'
    return dict(city=city or '全国样本', month=month, year=year, price_cities=len(prices),
                wage_period=wage['as_of'] if wage else None,
                table_period=table_record['as_of'] if table_record else None,
                annual_median_fields=3 if not city else 1,
                wage_regions=len(table_rows), wage_numeric_fields=sum(
                    v is not None for r in table_rows for key in ('monthly', 'hourly') for v in r[key]),
                ratio_cards=len(cards), income_charts=len(rendered['charts']),
                sources_verified=len(expected_sources) + len(rendered['wage_sources']))
