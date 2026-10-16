"""Verify responsive layout and touch flows against the static release directory."""
import argparse
import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output/mobile-browser'
VIEWS = [('overview', '市场概览'), ('city', '城市分析'), ('cih', '中指数据'),
         ('releases', '统计局月报'), ('mortgage', '房贷计算')]


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def select_view(page, name):
    page.get_by_role('button', name=name, exact=True).click()
    page.wait_for_timeout(250)


def check_layout(page, label, compact, results):
    metrics = page.evaluate('''() => {
        const visible = e => e.getBoundingClientRect().height > 0;
        const controls = [...document.querySelectorAll('.hub-tabs button, main button, main select, main input, .ranking-toolbar select, .ranking-toolbar input, .income-controls select, .analysis-modes button, .policy-chip')].filter(visible);
        return {
            width:innerWidth, scroll:document.documentElement.scrollWidth,
            shortControls:controls.filter(e => e.type !== 'checkbox' && e.getBoundingClientRect().height < 43.5).map(e=>e.getAttribute('aria-label') || e.closest('label')?.textContent.slice(0,30) || e.textContent.slice(0,30)),
            smallInputs:controls.filter(e=>e.matches('input:not([type=checkbox]),select') && parseFloat(getComputedStyle(e).fontSize) < 16).length,
            chartMismatches:[...document.querySelectorAll('canvas')].filter(e=>e.closest('[_echarts_instance_]')).map(e=>{
                const host=e.closest('[_echarts_instance_]'), c=echarts.getInstanceByDom(host);
                return {host:host.clientWidth, chart:c.getWidth(),height:host.clientHeight,chartHeight:c.getHeight()};
            }).filter(c=>Math.abs(c.host-c.chart)>1 || Math.abs(c.height-c.chartHeight)>1)
        };
    }''')
    assert metrics['scroll'] <= metrics['width'] + 1, (label, 'document overflow', metrics)
    assert not metrics['chartMismatches'], (label, 'chart has stale dimensions', metrics)
    if compact:
        assert not metrics['shortControls'], (label, 'touch target too short', metrics['shortControls'])
        assert not metrics['smallInputs'], (label, 'input font may cause focus zoom')
    text = page.locator('body').inner_text()
    assert 'NaN' not in text and 'undefined' not in text, label
    results.append(dict(view=label, **metrics))
    page.screenshot(path=str(OUT / (label + '.png')))


def phone_flows(page, width):
    select_view(page, '市场概览')
    national = page.locator('.national-analysis .analysis-chart').first
    verify_lpr(page, national)
    assert national.evaluate('e=>echarts.getInstanceByDom(e).getOption().dataZoom[1].height') == 44
    national.screenshot(path=str(OUT / f'{width}-national-chart.png'))
    if width == 390:
        national.scroll_into_view_if_needed()
        start = page.evaluate('scrollY')
        bounds = national.bounding_box()
        cdp = page.context.new_cdp_session(page)
        x,y = int(bounds['x']+bounds['width']/2),int(bounds['y']+bounds['height']/2)
        cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=x,y=y)]))
        for i in range(1,9):
            cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=x,y=y-i*20)]))
            page.wait_for_timeout(30)
        cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
        page.wait_for_timeout(150)
        cdp.detach()
        assert page.evaluate('scrollY') > start + 50, 'chart blocks vertical touch scrolling'
    page.locator('.ranking-jump').click()
    table = page.locator('.ranking-scroll')
    first = table.locator('tbody tr').first
    geometry = first.locator('.ranking-value').evaluate('''e=>({right:e.getBoundingClientRect().right,edge:e.closest('.ranking-scroll').getBoundingClientRect().right})''')
    assert geometry['right'] <= geometry['edge'] + 1, ('main ranking price off screen', width, geometry)
    expect(first.locator('.mobile-city-tier')).to_be_visible()
    nav = page.locator('.hub-tabs').bounding_box()
    assert abs(nav['y']) <= 1, ('navigation not sticky', nav)
    table.evaluate('e=>{e.scrollLeft=e.scrollWidth;e.scrollTop=150}')
    assert table.evaluate('e=>e.scrollLeft') > 0
    expect(first.locator('.ranking-source')).to_be_visible()
    city = first.locator('.rank-city-link')
    assert city.bounding_box()['x'] >= table.bounding_box()['x'] - 1
    table.screenshot(path=str(OUT / f'{width}-ranking-scrolled.png'))
    table.evaluate('e=>{e.scrollLeft=0;e.scrollTop=0}')
    city_name = city.inner_text()
    city.tap()
    expect(page.get_by_role('heading', name=city_name+' · 城市分析', exact=True)).to_be_visible()
    page.wait_for_timeout(250)
    page.screenshot(path=str(OUT / f'{width}-city-from-ranking.png'))
    assert abs(page.locator('.hub-tabs').bounding_box()['y']) <= 1, ('city navigation position',page.locator('.hub-tabs').bounding_box(),page.evaluate('scrollY'))
    page.locator('.city-section-links a').first.tap()
    page.wait_for_timeout(100)
    section = page.locator('#city-price').bounding_box()
    if section:
        assert section['y'] >= page.locator('.hub-tabs').bounding_box()['height'] - 2, 'section covered by navigation'
    for i, chart in enumerate(page.locator('.city-analysis .analysis-chart').all()[:2]):
        assert chart.evaluate('e=>echarts.getInstanceByDom(e).getOption().dataZoom[1].height') == 44
        chart.screenshot(path=str(OUT / f'{width}-city-chart-{i}.png'))

    select_view(page, '中指数据')
    verify_land_bid(page)
    page.locator('.cih-land-ratio').screenshot(path=str(OUT / f'{width}-cih-land-ratio.png'))
    page.get_by_role('button', name='新房 / 二手房', exact=True).tap()
    expect(page.locator('.cih-sample-panel[data-kind=new_house]')).to_be_visible()
    page.get_by_label('城市', exact=True).fill('北京')
    assert page.locator('.cih-sample-panel tbody tr').count() == 2
    page.get_by_label('城市', exact=True).fill('')
    page.locator('.cih-samples').screenshot(path=str(OUT / f'{width}-cih-samples.png'))
    chart = page.locator('[data-chart=priceRatio]')
    assert chart.evaluate('e=>echarts.getInstanceByDom(e).getOption().series[0].label.show') is False
    for name in ('shDist', 'rentRank', 'priceRatio', 'landBar'):
        page.locator(f'[data-chart={name}]').screenshot(path=str(OUT / f'{width}-cih-{name}.png'))
    page.set_viewport_size(dict(width=844, height=390))
    page.wait_for_timeout(250)
    assert chart.evaluate('e=>echarts.getInstanceByDom(e).getOption().series[0].label.show') is True
    page.set_viewport_size(dict(width=width, height=844))
    page.wait_for_timeout(250)
    assert chart.evaluate('e=>echarts.getInstanceByDom(e).getOption().series[0].label.show') is False
    # Container resizing can happen without a window resize (panels, font loading).
    original = chart.evaluate('e=>e.clientWidth')
    chart.evaluate('e=>e.style.width="80%"')
    page.wait_for_timeout(150)
    assert chart.evaluate('e=>echarts.getInstanceByDom(e).getWidth()') < original
    chart.evaluate('e=>e.style.width="100%"')
    page.wait_for_timeout(150)
    assert abs(chart.evaluate('e=>echarts.getInstanceByDom(e).getWidth()') - original) <= 1

    select_view(page, '统计局月报')
    assert page.locator('.release-grid article').count() == 70
    page.get_by_label('住宅类别').select_option('second_hand')
    page.get_by_label('城市', exact=True).fill('北京')
    expect(page.locator('.release-grid article')).to_have_count(1)
    page.get_by_label('城市', exact=True).fill('')
    page.locator('.release-grid').screenshot(path=str(OUT / f'{width}-release-cards.png'))

    select_view(page, '房贷计算')
    page.get_by_label('商业贷款金额（万元）', exact=True).fill('100')
    page.get_by_label('商业贷款年利率（%）', exact=True).fill('0')
    expect(page.locator('.mortgage-result')).to_contain_text('2,777.78')
    page.get_by_label('税前月工资（元）', exact=True).fill('15000')
    expect(page.locator('.fund-total')).to_contain_text('2,100.00')
    expect(page.locator('.fund-cash')).to_contain_text('677.78')
    equation = page.locator('.fund-equation')
    assert equation.evaluate('e=>e.scrollWidth<=e.clientWidth+1'), 'fund equation overflows'
    equation.screenshot(path=str(OUT / f'{width}-mortgage-equation.png'))
    comparison = page.locator('.mortgage-result .audit-table-wrap')
    assert comparison.evaluate('e=>e.scrollWidth<=e.clientWidth+1'), 'loan comparison off screen'
    page.get_by_label('贷款类型').select_option('combined')
    page.get_by_label('公积金贷款金额（万元）', exact=True).fill('50')
    page.get_by_label('还款方式').select_option('principal')
    page.locator('.fund-options summary').tap()
    page.get_by_label('使用实际缴存基数').check()
    expect(page.get_by_label('实际缴存基数（元/月）', exact=True)).to_be_visible()
    page.locator('.mortgage-result').screenshot(path=str(OUT / f'{width}-mortgage-result.png'))
    page.get_by_label('商业贷款金额（万元）', exact=True).fill('0')
    expect(page.get_by_role('alert')).to_be_visible()


def verify_lpr(page, chart):
    options = chart.evaluate('e=>{const o=echarts.getInstanceByDom(e).getOption();return {months:o.xAxis[0].data,series:o.series.map(s=>({name:s.name,data:s.data,step:s.step})),zoom:o.dataZoom.map(z=>({start:z.start,end:z.end}))};}')
    history = page.evaluate('window.__HOUSING_DATA__?.information?.macro?.lpr?.history')
    if history is None:
        history = page.request.get(urljoin(page.url, 'intersection_housing_data.json')).json()['information']['macro']['lpr']['history']
    assert options['months'] == [r['as_of'][:7] for r in history], 'LPR chart month mismatch'
    for i, key in enumerate(('one_year', 'five_year')):
        assert options['series'][i]['data'] == [r[key] for r in history], 'LPR term/rate mismatch'
        assert options['series'][i]['step'] == 'end', 'LPR chart interpolates monthly rates'
    assert all(z['start'] == 0 and z['end'] == 100 for z in options['zoom']), 'LPR history initially truncated'
    body = page.locator('body').inner_text()
    assert '70城均价 · 全国样本汇总' not in body, 'removed sample chart still visible'
    assert '2026-09-20' not in page.locator('[aria-labelledby=lpr-trend]').inner_text(), 'removed LPR date still visible'
    assert 'LPR是银行贷款的参考利率' in page.locator('[aria-labelledby=lpr-trend]').inner_text(), 'LPR explanation missing'


def house_land_reference(cih):
    supply = cih['land']['sections']['supply_22']
    column = supply['headers'].index('成交楼面均价(元/㎡)')
    prices = [r for r in cih['prices'] if r['kind'] == 'new_house']
    period = max(r['period'] for r in prices)
    homes = {r['city']: r['price'] for r in prices if r['period'] == period}
    floors = {r['city']: r['values'][column] for r in supply['cities']}
    cities = homes.keys() & floors.keys()
    return dict(ratio=sum(homes[c] for c in cities) / sum(floors[c] for c in cities),
                land_share=100 * sum(floors[c] for c in cities) / sum(homes[c] for c in cities),
                house_price=sum(homes[c] for c in cities) / len(cities),
                land_price=sum(floors[c] for c in cities) / len(cities),
                count=len(cities), house_period=period, land_period=supply['period'])


def verify_land_bid(page):
    cih = page.request.get(urljoin(page.url, 'intersection_housing_data.json')).json()['information']['cih']
    metrics = cih['metrics']
    source = next(m for m in metrics if m['scope'] == '300城宅地供求' and m['label'] == '平均溢价率' and m['unit'] == '%')
    shown = page.locator('[data-land-bid-ratio]')
    expected = f"{100 + source['value']:,.2f}".rstrip('0').rstrip('.') + '%'
    assert shown.inner_text() == expected, 'land bid ratio has wrong percentage base or source population'
    assert shown.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') <= 16, 'bid ratio should be secondary text'
    assert source['period'] in page.locator('.cih-land-ratio').inner_text(), 'land ratio lost source period'
    assert page.locator('[data-land-bid-source]').get_attribute('href') == source['source_url'], 'land ratio source mismatch'
    ref = house_land_reference(cih)
    main = page.locator('[data-house-land-ratio]')
    expected = f"{ref['land_share']:,.2f}".rstrip('0').rstrip('.') + '%'
    assert main.inner_text() == expected, 'floor-land cost share city or price mismatch'
    assert main.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)') >= 36, 'main ratio should be emphasized'
    card = page.locator('.cih-land-ratio').inner_text()
    for text in (f"{ref['count']}个共同城市", ref['house_period'], ref['land_period'], '实际利润还需扣除建安、税费、融资等成本'):
        assert text in card, 'missing ratio basis: ' + text


def main(directory):
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results, errors, failures = [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({'channel':'msedge'} if sys.platform=='win32' else {}))
            for width, height in [(320,740), (360,800), (390,844), (430,932), (844,390),
                                  (768,1024), (1280,900), (1440,1000), (1920,1080)]:
                phone = width <= 430
                page = browser.new_page(viewport=dict(width=width,height=height), has_touch=width<1000,
                                        is_mobile=width<1000, device_scale_factor=1, reduced_motion='reduce')
                page.on('pageerror', lambda e:errors.append(str(e)))
                page.on('response', lambda r:failures.append(dict(url=r.url,status=r.status)) if r.status>=400 else None)
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                page.get_by_role('button', name='房贷计算', exact=True).wait_for()
                for view, name in VIEWS:
                    select_view(page, name)
                    page.locator('.hub-tabs').scroll_into_view_if_needed()
                    check_layout(page, f'{width}-{view}', phone or width==844, results)
                    if view == 'cih':
                        verify_land_bid(page)
                        if width == 1440:
                            page.locator('.cih-land-ratio').screenshot(path=str(OUT / '1440-cih-land-ratio.png'))
                if phone:
                    phone_flows(page, width)
                    print(f'{width}px: layout and touch flows passed', flush=True)
                elif width == 1440:
                    select_view(page, '市场概览')
                    national = page.locator('.national-analysis .analysis-chart').first
                    verify_lpr(page, national)
                    national.screenshot(path=str(OUT / '1440-lpr-chart.png'))
                    assert page.locator('.national-analysis .analysis-chart').first.evaluate('e=>echarts.getInstanceByDom(e).getOption().dataZoom[1].height') == 20
                    expect(page.locator('.mobile-city-tier').first).not_to_be_visible()
                page.close()
            page = browser.new_page(viewport=dict(width=390,height=844),has_touch=True,is_mobile=True,reduced_motion='no-preference')
            page.on('pageerror', lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}',wait_until='networkidle')
            select_view(page,'房贷计算')
            page.wait_for_timeout(500)
            assert abs(page.locator('.hub-tabs').bounding_box()['y']) <= 1, 'animated navigation scrolls to wrong position'
            page.screenshot(path=str(OUT/'390-normal-motion.png'))
            page.close()
            browser.close()
        assert not errors, errors
        assert not failures, failures
        report = dict(status='passed', checks=results, browser_errors=errors, failed_requests=failures,
                      touch_flow_widths=[320,360,390,430], browser='Edge / Chromium, touch emulation')
        (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'PASS: {len(results)} viewports/views, 4 complete touch flows, rotation and container resizing')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT/'dist/pages')
    main(parser.parse_args().directory.resolve())
