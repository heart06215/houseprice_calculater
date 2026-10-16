"""Check the land indicator's real, zero, missing and conflicting browser states."""
import copy
import json
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from verify_mobile_browser import Quiet, select_view, house_land_reference

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output/land-ratio-browser'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    directory = ROOT / 'dist/pages'
    original = json.loads((directory / 'intersection_housing_data.json').read_text(encoding='utf-8'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks, errors = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({'channel': 'msedge'} if sys.platform == 'win32' else {}))
            for width, state in [(320, 'actual'), (390, 'actual'), (1440, 'actual'),
                                 (390, 'zero'), (390, 'missing'), (390, 'conflicting'),
                                 (390, 'missing-floor'), (390, 'zero-floor'), (390, 'wrong-unit'),
                                 (390, 'conflicting-house'), (390, 'missing-period')]:
                data = copy.deepcopy(original)
                rows = data['information']['cih']['metrics']
                is_premium = lambda r: r['scope'] == '300城宅地供求' and r['label'] == '平均溢价率'
                source = next(r for r in rows if is_premium(r))
                if state == 'zero':
                    source['value'] = 0
                elif state == 'missing':
                    data['information']['cih']['metrics'] = [r for r in rows if not is_premium(r)]
                elif state == 'conflicting':
                    rows.append(dict(source, value=source['value'] + 1))
                cih = data['information']['cih']
                supply = cih['land']['sections']['supply_22']
                if state == 'missing-floor':
                    supply['cities'] = []
                elif state == 'zero-floor':
                    for row in supply['cities']:
                        row['values'][supply['headers'].index('成交楼面均价(元/㎡)')] = 0
                elif state == 'wrong-unit':
                    supply['headers'][supply['headers'].index('成交楼面均价(元/㎡)')] = '成交土地单价(元/㎡)'
                elif state == 'conflicting-house':
                    month = max(r['period'] for r in cih['prices'] if r['kind'] == 'new_house')
                    home = next(r for r in cih['prices'] if r['kind'] == 'new_house' and r['period'] == month and r['city'] in {c['city'] for c in supply['cities']})
                    cih['prices'].append(dict(home, price=home['price'] + 1))
                elif state == 'missing-period':
                    supply.pop('period')
                page = browser.new_page(viewport=dict(width=width, height=900), has_touch=width < 700,
                                        is_mobile=width < 700, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/intersection_housing_data.json*', lambda route: route.fulfill(
                    status=200, content_type='application/json', body=json.dumps(data)))
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                lpr = page.locator('[aria-labelledby=lpr-trend]')
                expect(lpr).to_contain_text('LPR是银行贷款的参考利率')
                if state == 'actual' and width in (390, 1440):
                    lpr.screenshot(path=str(OUT / f'{width}-lpr-explanation.png'))
                select_view(page, '中指数据')
                card = page.locator('.cih-land-ratio')
                expect(card).to_contain_text('土地成本占新房售价')
                if state in ('missing', 'conflicting'):
                    expect(card.locator('[data-land-bid-ratio]')).to_have_count(0)
                    expect(card).to_contain_text('暂无可用于换算的300城平均溢价率')
                else:
                    expected = f"{100 + source['value']:,.2f}".rstrip('0').rstrip('.') + '%'
                    expect(card.locator('[data-land-bid-ratio]')).to_have_text(expected)
                    expect(card).to_contain_text(source['period'])
                    assert card.locator('[data-land-bid-ratio]').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') <= 16
                if state in ('missing-floor', 'zero-floor', 'wrong-unit', 'conflicting-house', 'missing-period'):
                    expect(card.locator('[data-house-land-ratio]')).to_have_count(0)
                    expect(card).to_contain_text('暂无城市、时期与面积口径可核对')
                else:
                    ref = house_land_reference(cih)
                    expected = f"{ref['land_share']:,.2f}".rstrip('0').rstrip('.') + '%'
                    expect(card.locator('[data-house-land-ratio]')).to_have_text(expected)
                    expect(card).to_contain_text(f"{ref['count']}个共同城市")
                    expect(card).to_contain_text(ref['house_period'])
                    expect(card).to_contain_text(ref['land_period'])
                    expect(card).to_contain_text('实际利润还需扣除建安、税费、融资等成本')
                    assert card.locator('[data-house-land-ratio]').evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') >= 36
                    card.locator('summary').click()
                    expect(card.locator('details')).to_have_attribute('open', '')
                for link in card.locator('a').all():
                    assert link.bounding_box()['height'] >= 44, 'source link touch target too short'
                assert page.locator('[data-chart=priceRatio] canvas').count() == 1, 'existing price chart disappeared'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'page overflow'
                card.screenshot(path=str(OUT / f'{width}-{state}.png'))
                checks.append(dict(width=width, state=state, passed=True))
                page.close()
            browser.close()
        assert not errors, errors
        (OUT / 'report.json').write_text(json.dumps(dict(checks=checks, browser_errors=errors), indent=2), encoding='utf-8')
        print('PASS: 11 sale/land and bid indicator states, LPR explanation, touch targets and existing chart')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
