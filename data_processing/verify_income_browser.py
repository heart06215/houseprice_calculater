"""Verify every recovered income year/wage table and all 70 city consumers in a real browser."""

import argparse
import json
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from chart_pipeline_audit import audit_charts
from income_dom_audit import audit_income_panel
from verify_dashboard_browser import Quiet

ROOT = Path(__file__).resolve().parent.parent
CAPTURE_STYLE = '.header, .hub-tabs {visibility:hidden!important;}'


def settle(page):
    page.evaluate('async () => { await Vue.nextTick(); await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); }')
    page.wait_for_timeout(80)


def record(page, data, checks, label, charts=False):
    settle(page)
    check = audit_income_panel(page, data)
    assert check is not None, label + ': income panel absent'
    dimensions = page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
    assert dimensions['scroll'] <= dimensions['width'] + 2, label + ': page overflow'
    checks.append(dict(view=label, **check, chart_pipeline=audit_charts(page, data) if charts else []))


def verify_rejection(page, data, selector, attribute=None):
    """A wrong rendered value or missing chart must fail the same publication audit."""
    element = page.locator(selector).first
    original = element.get_attribute(attribute) if attribute else element.text_content()
    if attribute:
        element.evaluate('(el, name)=>el.removeAttribute(name)', attribute)
    else:
        element.evaluate("el=>{el.textContent='17,571';}")
    try:
        try:
            audit_income_panel(page, data)
        except AssertionError:
            return 'rejected'
        raise AssertionError('income audit accepted a deliberately wrong DOM value/chart')
    finally:
        if attribute:
            element.evaluate('(el, item)=>el.setAttribute(item.name,item.value)', dict(name=attribute, value=original))
        else:
            element.evaluate('(el, text)=>{el.textContent=text;}', original)


def run(directory, out):
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads((directory / 'intersection_housing_data.json').read_text(encoding='utf-8'))
    clean = json.loads((ROOT / 'output/income/clean.json').read_text(encoding='utf-8'))
    income = data['information']['income']
    assert income['income'] == clean['income'], 'published annual income differs from verified input'
    assert income['minimum_wages'] == clean['minimum_wages'], 'published wages differ from verified input'
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks, errors, bad_requests, controls, rejection_checks = [], [], [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({'channel': 'msedge'} if sys.platform == 'win32' else {}))
            for width in (1440, 390):
                page = browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('response', lambda r: bad_requests.append({'url': r.url, 'status': r.status}) if r.status >= 400 else None)
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                panel = page.locator('.income-panel')
                panel.get_by_label('房价月份', exact=True).select_option('2026-08')
                summary = panel.locator('.income-more>summary')
                summary.focus()
                page.keyboard.press('Enter')
                assert panel.locator('.income-more').get_attribute('open') is not None, 'keyboard disclosure'
                for year in income['income']:
                    panel.get_by_label('收入年份', exact=True).select_option(str(year['year']))
                    record(page, data, checks, f'{width}-annual-{year["year"]}', charts=year['year'] in (2014, 2025))
                for wage in income['minimum_wages']:
                    panel.get_by_label('最低工资表期', exact=True).select_option(wage['as_of'])
                    record(page, data, checks, f'{width}-wage-table-{wage["as_of"]}')
                panel.get_by_label('收入年份', exact=True).select_option('2014')
                panel.get_by_label('最低工资表期', exact=True).select_option('2014')
                settle(page)
                rejection_checks.append(dict(width=width,
                    wrong_median=verify_rejection(page, data, '.income-ratios article:nth-child(2) p b'),
                    missing_chart=verify_rejection(page, data, '.income-panel .spark[_echarts_instance_]', '_echarts_instance_')))
                panel.screenshot(path=str(out / f'{width}-2014-income-and-wages.png'), style=CAPTURE_STYLE)
                selector = panel.get_by_label('最低工资表期', exact=True)
                selector.focus()
                page.keyboard.press('Home')
                page.keyboard.press('Enter')
                settle(page)
                assert selector.input_value() == 'auto', 'keyboard wage query reset'
                style = selector.evaluate('el=>{const s=getComputedStyle(el);return {height:el.getBoundingClientRect().height,font:s.fontSize,outline:s.outlineWidth,color:s.color,background:s.backgroundColor,focus:s.outlineColor};}')
                assert float(style['outline'][:-2]) >= 2, 'wage selector focus visibility'
                if width == 390:
                    assert style['height'] >= 44, 'mobile wage selector target size'
                controls.append(dict(width=width, keyboard='passed', **style))
                months = panel.get_by_label('房价月份', exact=True).locator('option').evaluate_all('(els)=>els.map(el=>el.value)')
                for month in sorted(months):
                    panel.get_by_label('房价月份', exact=True).select_option(month)
                    record(page, data, checks, f'{width}-national-month-{month}', charts=month in ('2014-12', '2016-12', '2026-08'))
                print(json.dumps({'width': width, 'annual_years': len(income['income']), 'wage_tables': len(income['minimum_wages']), 'national_months': len(months)}, ensure_ascii=False), flush=True)
                page.get_by_role('button', name='城市分析', exact=True).click()
                city_select = page.get_by_label('分析城市')
                for source in data['cities']:
                    city_select.select_option(label=source['city'])
                    settle(page)
                    available = page.get_by_label('观察月份').locator('option').evaluate_all('(els)=>els.map(el=>el.value)')
                    for month in dict.fromkeys([max(available), '2021-04']):
                        if month not in available:
                            continue
                        page.get_by_label('观察月份').select_option(month)
                        record(page, data, checks, f'{width}-city-{source["city"]}-{month}', charts=month == max(available))
                for city, month in (('福州', '2017-12'), ('厦门', '2017-12'), ('哈尔滨', '2016-12'), ('银川', '2016-12'), ('北京', '2014-12')):
                    city_select.select_option(label=city)
                    settle(page)
                    page.get_by_label('观察月份').select_option(month)
                    record(page, data, checks, f'{width}-historical-{city}-{month}', charts=True)
                    page.locator('.income-panel').screenshot(path=str(out / f'{width}-{city}-{month}.png'), style=CAPTURE_STYLE)
                city_select.select_option(label='福州')
                page.get_by_label('观察月份').select_option('2026-08')
                record(page, data, checks, f'{width}-retired-tier-福州', charts=True)
                page.locator('.income-panel').screenshot(path=str(out / f'{width}-福州-retired-tier.png'), style=CAPTURE_STYLE)
                city_select.select_option(label='上海')
                page.get_by_label('观察月份').select_option('2026-08')
                for year in income['income']:
                    page.get_by_label('收入年份', exact=True).select_option(str(year['year']))
                    record(page, data, checks, f'{width}-city-median-{year["year"]}')
                page.close()
            # The added table selector reuses the same layout at the remaining supported widths.
            for width in (320, 768, 1280, 1920):
                page = browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                page.locator('.income-more>summary').click()
                page.get_by_label('最低工资表期', exact=True).select_option('2016-12')
                record(page, data, checks, f'{width}-historical-table-layout')
                page.locator('.income-more').screenshot(path=str(out / f'{width}-wage-query-layout.png'), style=CAPTURE_STYLE)
                page.close()
            browser.close()
        assert not errors, 'browser script errors: ' + str(errors)
        assert not bad_requests, 'failed static requests: ' + str(bad_requests)
        chart_checks = sum(len(c['chart_pipeline']) for c in checks)
        points = sum(chart['points'] for c in checks for chart in c['chart_pipeline'])
        report = dict(status='passed', deployed=False, annual_years=len(income['income']), verified_median_fields=len(income['income']) * 3,
                      wage_tables=len(income['minimum_wages']), cities=len(data['cities']), views=len(checks),
                      chart_checks=chart_checks, chart_points=points, table_numeric_checks=sum(c['wage_numeric_fields'] for c in checks),
                      browser_errors=errors, failed_requests=bad_requests, keyboard_controls=controls,
                      validator_negative_cases=rejection_checks, checks=checks)
        path = out / 'report.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('status', 'annual_years', 'verified_median_fields', 'wage_tables', 'cities', 'views', 'chart_checks', 'chart_points', 'table_numeric_checks')}, ensure_ascii=False), flush=True)
        return report
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist/pages')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/income-consumer-browser')
    args = parser.parse_args()
    run(args.directory.resolve(), args.output.resolve())
