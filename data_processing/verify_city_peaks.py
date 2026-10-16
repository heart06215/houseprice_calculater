"""Audit 70 individual peak labels, browser city switches and historical cutoffs."""

import argparse
import json
import math
import re
import sys
import threading
from collections import Counter
from datetime import datetime, timezone, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from release_contract import validate_city_extrema

ROOT = Path(__file__).resolve().parent.parent


def positive(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def peak_at(city, month):
    rows = [
        r for r in city["monthly"] if r["date"] <= month and positive(r.get("price"))
    ]
    return max((r["price"] for r in rows), default=None)


def money(price):
    return f"{math.floor(price + 0.5):,}" if price is not None else "--"


def percent(value):
    if value is None:
        return "--"
    if abs(value) < 0.005:
        return "0%"
    return ("+" if value > 0 else "") + f"{value:.2f}".rstrip("0").rstrip(".") + "%"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def audit_browser(directory, data, output):
    from playwright.sync_api import sync_playwright, expect

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Quiet, directory=str(directory))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cases, errors = [], []
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
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(
                    f"http://127.0.0.1:{server.server_port}", wait_until="networkidle"
                )
                # Check every row of both the present and historical peak rankings.
                page.get_by_label("排名指标").select_option("peak")
                for month in (
                    max(r["date"] for c in data["cities"] for r in c["monthly"]),
                    "2021-04",
                ):
                    page.get_by_label("排名月份").select_option(month)
                    expect(page.locator(".ranking-period strong")).to_have_text(month)
                    actual = page.locator(".ranking-panel tbody tr").evaluate_all(
                        "rows=>rows.map(r=>({city:r.querySelector('.rank-city-link').textContent.trim(),value:r.querySelector('.ranking-value strong').textContent.trim()}))"
                    )
                    assert len(actual) == 70, "Peak ranking must include all 70 cities"
                    for row in actual:
                        city = next(
                            c for c in data["cities"] if c["city"] == row["city"]
                        )
                        expected = money(peak_at(city, month))
                        numeric = re.search(r"[\d,]+", row["value"])
                        assert (numeric.group() if numeric else "--") == expected, (
                            f"{city['city']}: ranking peak mismatch at {month}"
                        )
                    cases.append(
                        dict(
                            width=width,
                            view="peak-ranking",
                            month=month,
                            cities=len(actual),
                        )
                    )
                page.get_by_role("button", name="城市分析", exact=True).click()
                select = page.get_by_label("分析城市")
                for city in data["cities"]:
                    select.select_option(label=city["city"])
                    expect(
                        page.get_by_role(
                            "heading", name=city["city"] + " · 城市分析", exact=True
                        )
                    ).to_be_visible()
                    latest = city["monthly"][-1]["date"]
                    before = [
                        r["date"]
                        for r in city["monthly"]
                        if positive(r.get("price"))
                        and city["peak_date"]
                        and r["date"] < city["peak_date"]
                    ]
                    months = [latest] + ([max(before)] if before else [])
                    for month in months:
                        page.get_by_label("观察月份").select_option(month)
                        peak = peak_at(city, month)
                        price = next(
                            r["price"] for r in city["monthly"] if r["date"] == month
                        )
                        drop = (
                            (price / peak - 1) * 100
                            if positive(price) and positive(peak)
                            else None
                        )
                        kpi = page.locator(".city-analysis > .city-kpis > div").last
                        expect(kpi.locator("small")).to_have_text(
                            "峰值 " + money(peak) + " 元/㎡"
                        )
                        expect(kpi.locator("strong")).to_have_text(percent(drop))
                        cases.append(
                            dict(
                                width=width,
                                view="city-peak",
                                city=city["city"],
                                month=month,
                                peak=peak,
                                drop=drop,
                            )
                        )
                    chart = page.get_by_role(
                        "img", name=city["city"] + "均价及环比走势", exact=True
                    )
                    option = chart.evaluate(
                        "el=>{const o=echarts.getInstanceByDom(el).getOption();return {months:o.xAxis[0].data,prices:o.series[0].data}}"
                    )
                    rows = {r["date"]: r["price"] for r in city["monthly"]}
                    assert set(rows).issubset(option["months"]), (
                        city["city"] + ": chart omitted historical months"
                    )
                    assert option["prices"] == [
                        rows.get(m) for m in option["months"]
                    ], city["city"] + ": chart contains another city price"
                    assert (
                        max(v for v in option["prices"] if positive(v))
                        == city["max_price"]
                    ), city["city"] + ": rendered chart peak mismatch"
                    if city["city"] in ("北京", "上海", "深圳"):
                        page.locator(".city-analysis > .city-kpis").screenshot(
                            path=str(output / f"{width}-{city['city']}-historical.png")
                        )
                    dimensions = page.evaluate(
                        "({width:innerWidth,scroll:document.documentElement.scrollWidth})"
                    )
                    assert dimensions["scroll"] <= width + 2, (
                        city["city"] + ": page overflow"
                    )
                page.close()
            browser.close()
        assert not errors, "Browser errors: " + str(errors)
        return dict(
            status="passed",
            cities_per_viewport=70,
            viewports=[1440, 390],
            cases=cases,
            errors=errors,
        )
    finally:
        server.shutdown()
        server.server_close()


def main(directory, output, browser):
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads(
        (directory / "intersection_housing_data.json").read_text(encoding="utf-8")
    )
    cities = data["cities"]
    assert len(cities) == len({c["city"] for c in cities}) == 70
    rows = []
    for city in cities:
        peak = validate_city_extrema(city)
        record = (
            next(r for r in city["monthly"] if r["date"] == peak["date"])
            if peak["date"]
            else {}
        )
        peak.update(
            period_start=city["monthly"][0]["date"],
            period_end=city["monthly"][-1]["date"],
            current_price=city["current_price"],
            drop_from_peak=city["drop_from_peak"],
            source_url=record.get("evidence", {}).get("source_url")
            or record.get("evidence", {}).get("anchor", {}).get("source_url"),
            anchor_date=record.get("anchor_date"),
            chain_months=record.get("chain_months"),
        )
        rows.append(peak)
    report = dict(
        status="passed",
        checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        scope="highest price in each published city series, including verified observations and index estimates",
        cities=rows,
        peak_sources=dict(Counter(r["method"] for r in rows)),
    )
    if browser:
        report["browser"] = audit_browser(directory, data, output)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 70 城最高点核验",
        "",
        "按每个城市自己的已发布有效月度价格序列计算；同价并列时标注最早月份。以下为已收录序列的最高值，不代表全部住房的历史最高成交价。",
        "",
        "| 城市 | 最高价（元/㎡） | 首次最高月份 | 全部并列月份 | 价格依据 |",
        "| --- | ---: | --- | --- | --- |",
    ]
    methods = {
        "listing_observation": "安居客观测",
        "index_estimate": "统计局环比链推导",
    }
    lines.extend(
        f"| {r['city']} | {money(r['price'])} | {r['date'] or '--'} | {'、'.join(r['dates'])} | {methods.get(r['method'], '暂无')} |"
        for r in rows
    )
    if browser:
        lines += [
            "",
            f"桌面和 390px 手机均逐城切换，{len(report['browser']['cases'])} 项页面核对通过；同时核验价格曲线、当月回撤、历史截止月和 70 城峰值排名。",
        ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(
                status="passed",
                cities=len(rows),
                peak_sources=report["peak_sources"],
                browser_cases=len(report.get("browser", {}).get("cases", [])),
                report=str(output / "report.md"),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "data_processing/frontend"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "output/city-peaks")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Switch all 70 cities on desktop and mobile",
    )
    args = parser.parse_args()
    main(args.directory.resolve(), args.output.resolve(), args.browser)
