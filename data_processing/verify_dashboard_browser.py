"""Exercise published dashboard controls on desktop/mobile and capture browser evidence."""

import argparse
import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from chart_pipeline_audit import audit_charts, audit_release_cards, audit_realestate_evidence
from income_dom_audit import audit_income_panel

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output/release-browser"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check(page, label, results, data):
    page.wait_for_timeout(350)
    text = page.locator("body").inner_text()
    assert "NaN" not in text and "undefined" not in text, (
        label + ": invalid displayed value"
    )
    assert "小区地图" not in text, label + ": map entry visible"
    dimensions = page.evaluate(
        "({width:innerWidth,scroll:document.documentElement.scrollWidth})"
    )
    assert dimensions["scroll"] <= dimensions["width"] + 2, (
        label + ": page overflow " + str(dimensions)
    )
    chart_checks = audit_charts(page, data)
    release_check = (
        audit_release_cards(page, data)
        if page.get_by_label("报告月份").count()
        else None
    )
    results.append(
        dict(
            view=label,
            **dimensions,
            charts=page.locator("canvas").count(),
            chart_pipeline=chart_checks,
            release_pipeline=release_check,
            income_pipeline=audit_income_panel(page, data),
        )
    )
    page.screenshot(path=str(OUT / (label + ".png")), full_page=True)
    page.screenshot(path=str(OUT / (label + "-screen.png")))
    if label.endswith("-cih"):
        page.locator("[aria-labelledby=cih-price-ratio]").screenshot(
            path=str(OUT / (label + "-price-chart.png"))
        )


def main(directory):
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Quiet, directory=str(directory))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    results = []
    errors = []
    bad_requests = []
    data = json.loads(
        (directory / "intersection_housing_data.json").read_text(encoding="utf-8")
    )
    realestate_evidence = audit_realestate_evidence(data, ROOT)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                **({"channel": "msedge"} if sys.platform == "win32" else {}),
            )
            for width in (1440, 390):
                page = browser.new_page(
                    viewport={"width": width, "height": 1000}, reduced_motion="reduce"
                )
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "response",
                    lambda r: (
                        bad_requests.append(dict(url=r.url, status=r.status))
                        if r.status >= 400
                        else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                page.get_by_role("button", name="房贷计算", exact=True).wait_for()
                page.get_by_role("button", name="具体城市分析", exact=True).count()
                assert page.locator(".ranking-panel tbody tr").count() > 0, (
                    "ranking empty"
                )
                ranking = page.locator(".ranking-panel")
                if ranking.count():
                    search = ranking.locator('input[type="search"]')
                    if search.count():
                        search.fill("北京")
                        expect(ranking.locator(".rank-city-link")).to_have_count(1)
                        search.fill("")
                    page.get_by_label("排序方向").select_option("asc")
                    page.get_by_label("城市能级").select_option("S")
                    expect(ranking.locator(".rank-city-link")).to_have_count(4)
                    page.get_by_role("button", name="重置筛选", exact=True).click()
                    page.get_by_label("收入年份").select_option("2014")
                    expect(page.locator(".income-panel>.income-controls")).to_contain_text(
                        "2014 年收入"
                    )
                    expect(page.locator('.income-ratios article').nth(1)).to_contain_text('17,570')
                    expect(page.locator('.income-source a[href$="t20230203_1898674.html"]')).to_be_visible()
                page.get_by_role("button", name="同比", exact=True).click()
                check(page, f"{width}-overview-yoy", results, data)
                page.get_by_role("button", name="环比", exact=True).click()
                check(page, f"{width}-overview", results, data)
                macro = page.locator('.realestate-analysis')
                assert page.get_by_label('开发指标口径').input_value() == 'yoy', 'Default market trend must use official cumulative YoY'
                expect(macro).to_contain_text(data['information']['macro']['realestate']['period'])
                has_gaps = macro.locator('[_echarts_instance_]').evaluate_all('(els)=>els.some(el=>echarts.getInstanceByDom(el).getOption().series[0].markLine?.data?.length)')
                expect(macro.locator('.realestate-gap-note')).to_have_count(1 if has_gaps else 0)
                panels = macro.locator('.realestate-chart-grid>article').evaluate_all('(els)=>els.map(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y}))')
                assert (abs(panels[0]['y'] - panels[1]['y']) < 2 and panels[1]['x'] > panels[0]['x']) if width > 700 else (panels[1]['y'] > panels[0]['y'] and abs(panels[0]['x'] - panels[1]['x']) < 2), 'real-estate paired responsive layout'
                macro.screenshot(path=str(OUT / f'{width}-realestate.png'),
                    style='.header, .hub-tabs {visibility:hidden!important;}')
                page.get_by_label('开发指标口径').select_option('value')
                check(page, f'{width}-realestate-total', results, data)
                expect(macro).to_contain_text('不能作为单月增减走势')
                page.get_by_label('开发指标口径').select_option('yoy')
                page.get_by_label('开发指标年份').select_option(data['information']['macro']['realestate']['history'][0]['month'][:4])
                check(page, f'{width}-realestate-year', results, data)
                page.get_by_label('开发指标年份').select_option('all')
                page.get_by_role("button", name="城市分析", exact=True).click()
                page.locator(".city-analysis-page,.city-analysis").first.wait_for()
                select = page.get_by_label("分析城市")
                select.select_option(label="上海")
                expect(
                    page.get_by_role("heading", name="上海 · 城市分析", exact=True)
                ).to_be_visible()
                check(page, f"{width}-city-latest", results, data)
                page.get_by_label("观察月份").select_option("2021-04")
                expect(page.locator(".city-analysis-controls")).to_contain_text(
                    "2021-04 月度快照"
                )
                expect(page.locator('.income-regional article').first).to_contain_text('2,480')
                expect(page.locator('.income-source')).to_contain_text('2020-03-31')
                check(page, f"{width}-city", results, data)
                page.get_by_role("button", name="中指数据", exact=True).click()
                page.get_by_role("heading", name="中指市场情报", exact=True).wait_for()
                assert page.locator("[data-chart=priceRatio] canvas").count() == 1, (
                    "paired price chart must render"
                )
                page.get_by_role("button", name="新房 / 二手房", exact=True).click()
                assert (
                    page.locator(".cih-sample-panel[data-kind=new_house]").count() == 1
                )
                check(page, f"{width}-cih", results, data)
                for select in page.locator("main select").all():
                    options = select.locator("option")
                    if options.count() > 1:
                        select.select_option(index=1)
                check(page, f"{width}-cih-filters", results, data)
                page.get_by_role("button", name="统计局月报", exact=True).click()
                page.get_by_role(
                    "heading", name="统计局月度发布", exact=True
                ).wait_for()
                assert page.locator(".release-grid article").count() == 70, (
                    "release grid must contain 70 cities"
                )
                check(page, f"{width}-releases-all", results, data)
                page.get_by_label("住宅类别").select_option("second_hand")
                page.get_by_label("面积类别").select_option(index=1)
                page.get_by_label("城市", exact=True).fill("北京")
                expect(page.locator(".release-grid article")).to_have_count(1)
                check(page, f"{width}-releases", results, data)
                page.get_by_role("button", name="房贷计算", exact=True).click()
                amount = page.get_by_label("商业贷款金额（万元）", exact=True)
                amount.fill("0")
                expect(page.get_by_role("alert")).to_be_visible()
                amount.fill("100")
                page.get_by_label("商业贷款年利率（%）", exact=True).fill("0")
                expect(page.locator(".mortgage-result")).to_contain_text("2,777.78")
                check(page, f"{width}-mortgage-zero", results, data)
                page.get_by_label("商业贷款年利率（%）", exact=True).fill("3.1")
                page.get_by_label("贷款类型").select_option("combined")
                page.get_by_label("税前月工资（元）", exact=True).fill("15000")
                expect(page.locator(".fund-total")).to_contain_text("2,100.00")
                page.get_by_label("公积金贷款期限（年）", exact=True).fill("20")
                check(page, f"{width}-mortgage-annuity", results, data)
                page.get_by_label("还款方式").select_option("principal")
                check(page, f"{width}-mortgage", results, data)
                page.get_by_role("button", name="市场概览", exact=True).click()
                page.keyboard.press("Tab")
                assert page.evaluate("document.activeElement.tagName") != "BODY"
                page.close()
            page = browser.new_page(
                viewport={"width": 390, "height": 1000}, reduced_motion="reduce"
            )
            waiting = dict(
                state="ready",
                checked_at="2026-10-01T10:00:00+00:00",
                sources=dict(
                    nbs=dict(
                        state="ok",
                        data_period="2026-08",
                        expected_period="2026-09",
                        availability="awaiting_release",
                        detail="等待 2026-09 月报发布；已核验至 2026-08，后续仍每日检查",
                    )
                ),
            )
            page.route(
                "**/update_status.json*", lambda route: route.fulfill(json=waiting)
            )
            page.goto(url, wait_until="networkidle")
            page.locator(".update-status summary").click()
            nbs_row = page.locator(".update-status tbody tr").filter(
                has_text="国家统计局"
            )
            expect(nbs_row).to_contain_text("2026-08")
            expect(nbs_row).to_contain_text("等待 2026-09 月报发布")
            expect(page.locator(".update-status")).to_contain_text(
                "检查成功不表示新一期数据已经发布"
            )
            check(page, "390-awaiting-release", results, data)
            page.get_by_role("button", name="统计局月报", exact=True).click()
            assert page.get_by_label("报告月份").input_value() == max(
                r["month"] for r in data["information"]["releases"]
            ), "Check date must not advance the official report month"
            page.close()
            page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion='reduce')
            import copy
            empty = copy.deepcopy(data)
            empty['information']['macro']['realestate']['history'] = []
            page.route('**/intersection_housing_data.json*', lambda route: route.fulfill(json=empty))
            page.goto(url, wait_until='networkidle')
            expect(page.locator('.realestate-analysis .analysis-empty')).to_have_count(2)
            assert not page.locator('.realestate-analysis canvas').count(), 'Empty real-estate data must not invent lines'
            check(page, '390-realestate-empty', results, empty)
            page.close()
            page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion='reduce')
            missing = copy.deepcopy(data)
            history = missing['information']['macro']['realestate']['history']
            absent = next(r['month'] for r in reversed(history) if r['month'].endswith('-03'))
            missing['information']['macro']['realestate']['history'] = [r for r in history if r['month'] != absent]
            page.route('**/intersection_housing_data.json*', lambda route: route.fulfill(json=missing))
            page.goto(url, wait_until='networkidle')
            page.get_by_label('开发指标年份').select_option(absent[:4])
            page.wait_for_timeout(350)
            gaps = page.locator('.realestate-analysis [_echarts_instance_]').evaluate_all('''(els)=>els.map(el=>{
                const opt=echarts.getInstanceByDom(el).getOption();
                return {months:opt.xAxis[0].data,values:opt.series[0].data,connect:opt.series[0].connectNulls,bridges:opt.series[0].markLine.data};
            })''')
            assert len(gaps) == 2 and all(r['values'][r['months'].index(absent)] is None and not r['connect'] and len(r['bridges']) == 1 for r in gaps), 'A missing report stays null and receives one red endpoint bridge'
            assert all(not any(m.endswith('-01') for m in r['months']) for r in gaps), 'Combined January-February reports must not produce January gaps'
            expect(page.locator('.realestate-gap-note')).to_contain_text(absent)
            check(page, '390-realestate-gap', results, missing)
            page.locator('.realestate-analysis').screenshot(path=str(OUT / '390-realestate-gap-preview.png'),
                style='.header,.hub-tabs {visibility:hidden!important;}')
            page.close()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route(
                "**/intersection_housing_data.json*",
                lambda route: route.fulfill(status=503, body="unavailable"),
            )
            page.goto(url, wait_until="networkidle")
            page.get_by_role("button", name="重新加载", exact=True).wait_for()
            check(page, "390-data-error", results, data)
            page.close()
            browser.close()
        assert not errors, "Browser errors: " + str(errors)
        assert not bad_requests, "Static request failures: " + str(bad_requests)
        report = dict(
            status="passed",
            checks=results,
            realestate_evidence=realestate_evidence,
            browser_errors=errors,
            failed_requests=bad_requests,
        )
        (OUT / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                dict(
                    status="passed",
                    views=len(results),
                    chart_checks=sum(len(r["chart_pipeline"]) for r in results),
                    points_checked=sum(
                        c["points"] for r in results for c in r["chart_pipeline"]
                    ),
                    realestate_reports_checked=realestate_evidence['reports'],
                    report=str(OUT / "report.json"),
                ),
                ensure_ascii=False,
            )
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist/pages")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUT,
        help="Browser report and screenshots directory",
    )
    args = parser.parse_args()
    OUT = args.output.resolve()
    main(args.directory.resolve())
