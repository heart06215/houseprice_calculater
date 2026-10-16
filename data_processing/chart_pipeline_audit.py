"""Compare every mounted dashboard chart against its published input facts."""

import hashlib
import math
import re
from bs4 import BeautifulSoup


def audit_realestate_evidence(data, root):
    """Check narrative totals and growth direction independently of the crawler's table parser."""
    from release_inputs import safe_path
    reports = data['information']['macro']['realestate']['history']
    checks = []
    for report in reports:
        raw = report['raw']
        content = (root / safe_path(raw['path'])).read_bytes()
        assert hashlib.sha256(content).hexdigest() == raw['sha256'], 'real-estate original evidence hash'
        soup = BeautifulSoup(content.decode('utf-8'), 'html.parser')
        paragraphs = [re.sub(r'\s+', '', p.get_text()) for p in soup.find_all('p')]
        facts = {}
        for label, phrase, prose_unit, unit in (
            ('全国房地产开发投资额', '全国房地产开发投资', '亿元', '亿元'),
            ('房屋新开工面积', '房屋新开工面积', '万平方米', '万㎡'),
        ):
            pattern = phrase + r'([\d,.]+)' + prose_unit + r'[^。；;]*?(增长|下降)([\d.]+)%'
            stated = {(float(m[1].replace(',', '')), float(m[3]) * (-1 if m[2] == '下降' else 1))
                      for text in paragraphs for m in re.finditer(pattern, text)}
            assert len(stated) == 1, f"{report['month']}/{label}: missing or conflicting official narrative"
            total, growth = stated.pop()
            items = [item for item in report['items'] if item['label'] == label]
            assert len(items) == 1 and items[0]['unit'] == unit, f"{report['month']}/{label}: metric/unit binding"
            assert (items[0]['value'], items[0]['yoy']) == (total, growth), f"{report['month']}/{label}: official narrative/amount/growth sign mismatch"
            facts[label] = dict(value=total, yoy=growth, unit=unit)
        checks.append(dict(month=report['month'], source_url=report['source_url'], metrics=facts))
    return dict(reports=len(reports), metrics=len(reports) * 2, numeric_values=len(reports) * 4, checks=checks)


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def value(point):
    return point.get("value") if isinstance(point, dict) else point


def equal(actual, expected, label):
    if isinstance(expected, list):
        assert len(actual) == len(expected), (
            f"{label}: length {len(actual)} != {len(expected)}"
        )
        for index, (a, b) in enumerate(zip(actual, expected)):
            equal(value(a), b, f"{label}[{index}]")
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and math.isclose(
            actual, expected, rel_tol=1e-9, abs_tol=1e-8
        ), f"{label}: {actual} != {expected}"
    else:
        assert actual == expected, f"{label}: {actual!r} != {expected!r}"


def income_for(income, month):
    rows = [r for r in income["income"] if r["year"] <= int(month[:4])]
    return max(rows, key=lambda r: r["year"])["metrics"] if rows else {}


def wages_for(income, month):
    from wage_archive import cutoff
    rows = [r for r in income["minimum_wages"] if cutoff(r["as_of"]) <= month + "-31"]
    return max(rows, key=lambda r: cutoff(r["as_of"]))["rows"] if rows else []


def ratio(price, denominator):
    return price * 90 / denominator if positive(denominator) else None


def calendar(months):
    result = []
    if not months:
        return result
    year, month = map(int, min(months).split("-"))
    while f"{year:04d}-{month:02d}" <= max(months):
        result.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def mortgage_years(page):
    """Calculate annual repayments from form inputs with closed-form balances."""
    kind = page.get_by_label("贷款类型").input_value()
    method = page.get_by_label("还款方式").input_value()
    names = (
        ["商业贷款", "公积金贷款"]
        if kind == "combined"
        else ["公积金贷款" if kind == "fund" else "商业贷款"]
    )
    loans = []
    for name in names:
        loans.append(
            (
                float(
                    page.get_by_label(name + "金额（万元）", exact=True).input_value()
                )
                * 10000,
                float(page.get_by_label(name + "年利率（%）", exact=True).input_value())
                / 1200,
                int(page.get_by_label(name + "期限（年）", exact=True).input_value())
                * 12,
            )
        )
    annual = [[0.0, 0.0] for _ in range(max(n for _, _, n in loans) // 12)]
    for principal, rate, months in loans:
        payment = (
            principal / months
            if rate == 0
            else (principal * rate / -math.expm1(-months * math.log1p(rate)))
        )
        for index in range(months):
            if method == "principal" or rate == 0:
                balance = principal * (1 - index / months)
                capital = principal / months
            else:
                balance = (
                    principal
                    * -math.expm1((index - months) * math.log1p(rate))
                    / -math.expm1(-months * math.log1p(rate))
                )
                capital = balance if index == months - 1 else payment - balance * rate
            annual[index // 12][0] += capital
            annual[index // 12][1] += balance * rate
    return [[round(row[index] / 10000, 2) for row in annual] for index in (0, 1)]


def expected_spark(data, city, chart):
    income = data["information"]["income"]
    if city:
        source = next(c for c in data["cities"] if c["city"] == city)
        rows = sorted(
            (r for r in source["monthly"] if positive(r["price"])),
            key=lambda r: r["date"],
        )
        months, prices = [r["date"] for r in rows], [r["price"] for r in rows]
        if chart["aria"] == "均价逐月走势":
            return months, prices
        if chart["aria"] == "总价逐月走势":
            return months, [p * 90 / 10000 for p in prices]
        if "中位数" in chart["card"]:
            return months, [
                ratio(
                    p,
                    income_for(income, m).get("national", {}).get("disposable_median"),
                )
                for m, p in zip(months, prices)
            ]
        tier = int(re.search(r"第(\d+)档", chart["card"]).group(1)) - 1
        region = (
            city
            if city in ("北京", "上海", "天津", "重庆", "深圳")
            else income["city_provinces"][city]
        )

        def denominator(month):
            row = next(
                (r for r in wages_for(income, month) if r["region"] == region), {}
            )
            monthly = row.get("monthly", [])
            wage = monthly[tier] if tier < len(monthly) else None
            return wage * 12 if positive(wage) else None

        return months, [ratio(p, denominator(m)) for m, p in zip(months, prices)]
    grouped = {}
    for source in data["cities"]:
        for row in source["monthly"]:
            if positive(row["price"]):
                grouped.setdefault(row["date"], []).append(row["price"])
    months = sorted(m for m, prices in grouped.items() if len(prices) >= 30)
    prices = [sum(grouped[m]) / len(grouped[m]) for m in months]
    if chart["card_index"] < 3:
        scope, metric = (
            ("national", "disposable_mean"),
            ("national", "disposable_median"),
            ("urban", "disposable_median"),
        )[chart["card_index"]]
        return months, [
            ratio(p, income_for(income, m).get(scope, {}).get(metric))
            for m, p in zip(months, prices)
        ]

    def lowest_wage(month):
        values = [
            v for r in wages_for(income, month) for v in r["monthly"] if positive(v)
        ]
        return min(values) * 12 if values else None

    return months, [ratio(p, lowest_wage(m)) for m, p in zip(months, prices)]


def audit_charts(page, data):
    """Read rendered ECharts options and independently check labels and every point."""
    charts = page.evaluate("""() => [...document.querySelectorAll('[_echarts_instance_]')].filter(el=>el.getBoundingClientRect().height>0).map(el=>{
        const chart=echarts.getInstanceByDom(el), opt=chart.getOption(), card=el.closest('article');
        return {key:el.dataset.chart||'',aria:el.getAttribute('aria-label')||'',spark:el.classList.contains('spark'),
            card:card?.querySelector('span')?.textContent||'',card_index:card?Array.from(card.parentElement.children).indexOf(card):-1,
            x:(opt.xAxis||[]).map(a=>a.data||[]),y:(opt.yAxis||[]).map(a=>a.data||[]),y_names:(opt.yAxis||[]).map(a=>a.name||''),
            x_inverse:(opt.xAxis||[]).map(a=>Boolean(a.inverse)),y_inverse:(opt.yAxis||[]).map(a=>Boolean(a.inverse)),
            series:(opt.series||[]).map(s=>({name:s.name,data:s.data||[],connect_nulls:Boolean(s.connectNulls),
                line_style:s.lineStyle||{},gap_lines:s.markLine?.data||[],gap_style:s.markLine?.lineStyle||{}}))};
    })""")
    select = page.get_by_label("分析城市")
    city = select.input_value() if select.count() else None
    reports = []
    segments, info = data["housing_segments"], data["information"]
    for chart in charts:
        source, expected = None, []
        aria, key = chart["aria"], chart["key"]
        if aria == "每月上涨持平下跌及不可比城市数量":
            months = calendar(
                [
                    r["date"]
                    for c in data["cities"]
                    for r in c["monthly"]
                    if r["date"] >= "2021-04"
                ]
            )
            equal(chart["x"][0], months, "breadth months")
            maps = [{r["date"]: r for r in c["monthly"]} for c in data["cities"]]
            columns = []
            for index, month in enumerate(months):
                previous = months[index - 1] if index else None
                changes = [
                    rows[month]["change_pct"]
                    for rows in maps
                    if month in rows
                    and previous in rows
                    and positive(rows[month]["price"])
                    and positive(rows[previous]["price"])
                    and isinstance(rows[month].get("change_pct"), (int, float))
                ]
                up, down = (
                    sum(v > 0.05 for v in changes),
                    sum(v < -0.05 for v in changes),
                )
                columns.append(
                    [up, len(changes) - up - down, down, len(maps) - len(changes)]
                )
            expected = [list(values) for values in zip(*columns)]
            source = "cities.monthly adjacent-month comparable breadth"
        elif aria == "各能级价格走势和政策节点":
            for tier in ("S", "1", "2", "3"):
                cities = [c for c in data["cities"] if c["tier"] == tier]
                months = calendar(
                    [
                        r["date"]
                        for c in cities
                        for r in c["monthly"]
                        if r["date"] >= "2021-04"
                    ]
                )
                maps = [{r["date"]: r["price"] for r in c["monthly"]} for c in cities]
                cohort = [
                    rows
                    for rows in maps
                    if months and all(positive(rows.get(m)) for m in months)
                ]
                if not cohort:
                    continue
                base = sum(rows["2021-04"] for rows in cohort) / len(cohort)
                expected.append(
                    [
                        [
                            m,
                            math.floor(
                                sum(rows[m] for rows in cohort)
                                / len(cohort)
                                / base
                                * 1000
                                + 0.5
                            )
                            / 10,
                        ]
                        for m in months
                    ]
                )
            source = "cities.monthly fixed complete cohort by tier, base 2021-04"
        elif aria == "城市价格较各自峰值的回撤分布":
            buckets = [0] * 6
            for city_row in data["cities"]:
                drop = city_row.get("drop_from_peak")
                if isinstance(drop, (int, float)):
                    index = next(
                        (
                            i
                            for i, edge in enumerate((-10, -20, -30, -40, -50))
                            if drop > edge
                        ),
                        5,
                    )
                    buckets[index] += 1
            expected, source = [buckets], "cities.drop_from_peak"
        elif aria == "每年还款的本金和利息":
            expected = mortgage_years(page)
            equal(
                chart["x"][0],
                [f"第{i + 1}年" for i in range(len(expected[0]))],
                "mortgage years",
            )
            equal(
                [s["name"] for s in chart["series"]],
                ["偿还本金", "支付利息"],
                "mortgage series",
            )
            source = "loan form amounts/rates/terms and repayment method (computed)"
        elif chart["spark"]:
            months, values = expected_spark(data, city, chart)
            equal(chart["x"][0], months, f"income chart months/{city}/{chart['card']}")
            expected, source = [values], "cities.monthly + information.income"
        elif aria in ("全国房地产开发投资累计走势", "全国房屋新开工面积累计走势"):
            history = info['macro']['realestate']['history']
            year = page.get_by_label('开发指标年份').input_value()
            mode = page.get_by_label('开发指标口径').input_value()
            rows = [r for r in history if year == 'all' or r['month'].startswith(year)]
            months = calendar([r['month'] for r in rows])
            if mode == 'yoy':
                months = [m for m in months if not m.endswith('-01')]
            equal(chart['x'][0], months, 'real-estate report periods')
            assert all(not s['connect_nulls'] for s in chart['series']), 'Missing report values must remain null rather than enter the regular line'
            equal(chart['x_inverse'], [False], 'real-estate chronological direction')
            equal(chart['y_inverse'], [False], 'real-estate vertical direction')
            investment = aria == '全国房地产开发投资累计走势'
            label = '全国房地产开发投资额' if investment else '房屋新开工面积'
            unit = '亿元' if investment else '万㎡'
            by_month = {r['month']: next(m for m in r['items'] if m['label'] == label) for r in rows}
            assert all(r['unit'] == unit for r in by_month.values()), 'real-estate source unit'
            expected = [[by_month[m]['yoy' if mode == 'yoy' else 'value'] if m in by_month else None for m in months]]
            bridges = []
            valid = [i for i, value in enumerate(expected[0]) if value is not None]
            for left, right in zip(valid, valid[1:]):
                missing = [m for m in months[left + 1:right] if not m.endswith('-01')]
                if missing:
                    bridges.append([[months[left], expected[0][left]], [months[right], expected[0][right]]])
            series = chart['series'][0]
            token = '--trend-up' if investment else '--tech-blue'
            normal_color = page.evaluate("name=>getComputedStyle(document.documentElement).getPropertyValue(name).trim()", token)
            assert series['line_style'].get('color') == normal_color, 'Regular real-estate line must retain its metric color'
            equal([[endpoint['coord'] for endpoint in line] for line in series['gap_lines']], bridges, 'real-estate gap bridge endpoints')
            red = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--trend-up').trim()")
            assert series['gap_style'].get('color') == red and series['gap_style'].get('type') == 'solid', 'Missing reports must use a red straight line'
            equal(chart['y_names'], ['累计同比（%）' if mode == 'yoy' else f'累计（{unit}）'], 'real-estate chart unit')
            index = 0 if investment else 1
            panel = page.locator('.realestate-chart-grid>article').nth(index)
            assert ('累计同比' if mode == 'yoy' else '年初累计') in panel.locator('h3').inner_text(), 'real-estate heading must match chart measure'
            summary = panel.locator('.realestate-latest').inner_text()
            selected = by_month[rows[-1]['month']]
            shown = f"{selected['yoy' if mode == 'yoy' else 'value']:,.2f}".rstrip('0').rstrip('.')
            assert summary.startswith(shown + ('%' if mode == 'yoy' else f' {unit}')), 'real-estate summary must match chart measure'
            assert rows[-1]['period'] in summary, 'real-estate summary must follow selected year'
            source = 'information.macro.realestate.history / ' + label
        elif "贷款市场报价利率" in aria:
            history = info["macro"]["lpr"]["history"]
            equal(chart["x"][0], [r["as_of"][:7] for r in history], "LPR months")
            expected = [[r[k] for r in history] for k in ("one_year", "five_year")]
            source = "information.macro.lpr.history"
        elif aria == "全国新房与二手住宅涨跌幅走势":
            metric = (
                "yoy_pct"
                if page.get_by_role("button", name="同比", exact=True).get_attribute(
                    "aria-pressed"
                )
                == "true"
                else "mom_pct"
            )
            months = sorted(
                {
                    r["month"]
                    for segment in segments.values()
                    for c in segment["cities"]
                    for r in c["monthly"]
                }
            )
            equal(chart["x"][0], calendar(months), "official national months")
            for kind in ("new_house", "second_hand"):
                grouped = {}
                for c in segments[kind]["cities"]:
                    for row in c["monthly"]:
                        if isinstance(row.get(metric), (int, float)):
                            grouped.setdefault(row["month"], []).append(row[metric])
                expected.append(
                    [
                        sum(grouped[m]) / len(grouped[m]) if grouped.get(m) else None
                        for m in calendar(months)
                    ]
                )
            source = "housing_segments monthly official changes"
        elif aria.endswith("环比和同比走势"):
            kind = "new_house" if "新建商品住宅" in aria else "second_hand"
            rows = next(c for c in segments[kind]["cities"] if c["city"] == city)[
                "monthly"
            ]
            months = calendar([r["month"] for r in rows])
            lookup = {r["month"]: r for r in rows}
            equal(chart["x"][0], months, "official city months")
            expected = [
                [lookup.get(m, {}).get(k) for m in months]
                for k in ("mom_pct", "yoy_pct")
            ]
            source = f"housing_segments.{kind}.{city}"
        elif aria.endswith("均价及环比走势"):
            rows = next(c for c in data["cities"] if c["city"] == city)["monthly"]
            months = calendar([r["date"] for r in rows])
            lookup = {r["date"]: r for r in rows}
            equal(chart["x"][0], months, "city price months")
            expected = [
                [lookup.get(m, {}).get(k) for m in months]
                for k in ("price", "change_pct")
            ]
            source = f"cities.{city}.monthly"
        elif key in ("shDist", "rentRank"):
            kind, field, limit = (
                ("second_hand", "mom_pct", 30)
                if key == "shDist"
                else ("rent", "price", 25)
            )
            latest = max(
                r["period"] for r in info["cih"]["prices"] if r["kind"] == kind
            )
            rows = sorted(
                (
                    r
                    for r in info["cih"]["prices"]
                    if r["kind"] == kind
                    and r["period"] == latest
                    and isinstance(r.get(field), (int, float))
                ),
                key=lambda r: r[field],
                reverse=True,
            )[:limit][::-1]
            equal(chart["y"][0], [r["city"] for r in rows], f"{key} cities")
            expected = [
                [round(r[field], 2) if key == "shDist" else r[field] for r in rows]
            ]
            source = f"information.cih.prices.{kind}.{latest}"
        elif key == "priceRatio":
            latest = max(
                r["period"] for r in info["cih"]["prices"] if r["kind"] == "second_hand"
            )
            prices = {
                (r["city"], r["kind"]): r["price"]
                for r in info["cih"]["prices"]
                if r["period"] == latest
            }
            paired = sorted(
                (
                    (city_name, price / prices[(city_name, "new_house")])
                    for (city_name, kind), price in prices.items()
                    if kind == "second_hand"
                    and positive(prices.get((city_name, "new_house")))
                ),
                key=lambda r: r[1],
                reverse=True,
            )[:20]
            equal(chart["x"][0], [r[0] for r in paired], "paired CIH cities")
            expected, source = (
                [[round(r[1], 3) for r in paired]],
                f"information.cih.prices paired by city/month {latest}",
            )
        elif key == "landBar":
            land = info["cih"]["land"]["sections"]["supply_22"]
            aggregate = land.get("aggregate") or {}
            items = [
                (h, v)
                for h, v in zip(
                    aggregate.get("headers") or land["headers"],
                    aggregate.get("values", []),
                )
                if "万㎡" in h and isinstance(v, (int, float))
            ][::-1]
            equal(chart["y"][0], [r[0] for r in items], "land units and labels")
            expected, source = (
                [[r[1] for r in items]],
                "information.cih.land.sections.supply_22.aggregate",
            )
        assert source is not None, (
            f"Unmapped rendered chart: {aria or key or chart['card']}"
        )
        equal([s["data"] for s in chart["series"]], expected, source)
        reports.append(
            dict(
                chart=aria or key or chart["card"],
                source=source,
                series=len(expected),
                points=sum(len(r) for r in expected),
            )
        )
    return reports


def audit_release_cards(page, data):
    month = page.get_by_label("报告月份").input_value()
    kind = page.get_by_label("住宅类别").input_value()
    area = page.get_by_label("面积类别").input_value()
    query = page.get_by_label("城市", exact=True).input_value().strip()
    release = next(r for r in data["information"]["releases"] if r["month"] == month)
    expected = {
        r["city"]: r
        for r in release["rows"]
        if r["kind"] == kind and r["area"] == area and query in r["city"]
    }
    cards = page.locator(".release-grid article").evaluate_all(
        "els=>els.map(el=>({city:el.querySelector('h3').textContent,values:[...el.querySelectorAll('strong')].map(e=>e.textContent)}))"
    )
    assert {r["city"] for r in cards} == set(expected), (
        "Monthly report city coverage mismatch"
    )
    for card in cards:
        for text, metric in zip(card["values"], ("mom_pct", "yoy_pct")):
            match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
            actual = float(match.group()) if match else None
            equal(
                actual,
                expected[card["city"]][metric],
                f"{month}/{kind}/{area}/{card['city']}/{metric}",
            )
    return dict(
        month=month,
        kind=kind,
        area=area,
        cities=len(cards),
        source="information.releases",
    )
