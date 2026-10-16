"""Bounded source collection with independent failure and refresh policies."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from data_store import atomic_json

DASHBOARD_SOURCES = ("nbs", "income", "anjuke", "cih", "macro")


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {} if default is None else default


def due(entry, hours, at=None):
    if entry.get('state') in ('failed', 'partial'):
        hours = min(hours, 24)
    try:
        return datetime.fromisoformat(entry["checked_at"]) + timedelta(hours=hours) <= (
            at or datetime.now(timezone.utc)
        )
    except (KeyError, ValueError, TypeError):
        return True


class SourceTasks:
    """Dispatch configured sources while retaining each collector's data contract."""

    NAMES = (
        "macro",
        "income",
        "nbs",
        "cih",
        "anjuke",
        "communities",
        "community_profiles",
        "community_coverage",
        "metro",
        "hydrology",
        "catalog",
    )

    def __init__(self, root, run_script):
        self.root = Path(root)
        self.front_data = self.root / "data_processing/frontend/realestate/data"
        self.run_script = run_script

    def run(self, name, options):
        if name not in self.NAMES:
            raise ValueError("Unknown source " + name)
        return getattr(self, "_" + name)(options)

    def _macro(self, options):
        import macro_crawler

        result = macro_crawler.crawl()
        good = sum((e["state"] == "ok" for e in result["events"]))
        if not good:
            raise RuntimeError("官方宏观与 LPR 采集失败，旧记录保留")
        return dict(
            detail="统计局开发投资、新开工面积等累计历史与 LPR 分别记录来源及时期",
            partial=good < len(result["events"]),
            error="；".join(
                (
                    e["kind"] + ": " + e["error"][:120]
                    for e in result["events"]
                    if e["state"] != "ok"
                )
            )
            or None,
        )

    def _income(self, options):
        import income_crawler

        result = income_crawler.crawl(max_pages=options.get("max_pages", 2))
        if not any((e["state"] == "ok" for e in result["events"])):
            raise RuntimeError("官方收入来源未解析成功，历史数据保留")
        browser_recovered = any(
            e["source"] == "minimum_wages"
            and e["state"] == "ok"
            and e.get("capture_mode") == "browser_dom"
            for e in result["events"]
        )
        detail = "全年居民收入、中位数与消费；最低工资独立口径"
        if browser_recovered:
            detail += "；最低工资已通过正常浏览器渲染核验"
        if result["state"] == "partial":
            detail += "；未成功部分保留已核对旧资料"
        return dict(
            detail=detail,
            partial=result["state"] == "partial",
            error="；".join(
                (
                    e["source"] + ": " + e["error"].splitlines()[0][:120]
                    for e in result["events"]
                    if e["state"] == "failed" and not e.get("recovered")
                )
            )
            or None,
        )

    def _nbs(self, options):
        import stats_gov_crawler

        result = stats_gov_crawler.quick_update(details=True)
        if isinstance(result, str):
            result = {"result": result}
        if result["result"] == "partial":
            raise RuntimeError("部分月报采集失败，已保留旧月份")
        report = {key: value for key, value in result.items() if key != "result"}
        report.setdefault(
            "data_period",
            max(
                (
                    p.stem[-7:]
                    for p in (self.root / "output/stats_gov").glob("stats_gov_*.json")
                ),
                default=None,
            ),
        )
        if report.get("availability") == "awaiting_release":
            detail = f"等待 {report['expected_period']} 月报发布；已核验至 {report['data_period']}，后续仍每日检查"
        else:
            detail = "目录检查完成；报告期取自实际发布，没有新增时保留当前月报"
        return dict(report, detail=detail)

    def _cih(self, options):
        import cih_crawler

        result = cih_crawler.crawl(max_pages=options.get("max_pages", 12))
        good = sum((r["status"] == "ok" for r in result["last_run"]))
        if not good:
            raise RuntimeError("中指公开页未成功解析，旧数据保留")
        ranking_note = ""
        ranking_failed = False
        land_failed = False
        if any(
            (
                r["url"] == cih_crawler.SEEDS[0] and r["status"] == "ok"
                for r in result["last_run"]
            )
        ):
            try:
                from cih_rankings import fetch_public_bundle

                fetch_public_bundle()
                ranking_note = "；公开榜单脚本已复核（时期独立标注）"
            except Exception:
                ranking_note = "；榜单入口未取得新明细，保留历史榜单"
                ranking_failed = True
        months = [
            r.get("period", "")
            for r in result["observations"]
            if len(r.get("period", "")) == 7 and r.get("period", "")[4] == "-"
        ]
        land_note = ""
        try:
            import cih_land

            land = cih_land.refresh()
            land_failed = land.get("state") not in ("ok", "unchanged")
            land_note = {
                "ok": "；宅地数据已更新",
                "unchanged": "；宅地数据周期内无更新（沿用）",
            }.get(
                land.get("state"),
                "；宅地采集未成功（" + str(land.get("error"))[:60] + "，沿用旧数据）",
            )
        except Exception as land_exc:
            land_failed = True
            land_note = "；宅地采集异常（" + str(land_exc)[:60] + "，沿用旧数据）"
        return dict(
            detail=f"{good}/{len(result['last_run'])} 个公开页面解析成功"
            + ranking_note
            + land_note,
            partial=good < len(result["last_run"]) or ranking_failed or land_failed,
            data_period=max(months, default=""),
            error="；".join(
                (
                    r.get("error", "")[:120]
                    for r in result["last_run"]
                    if r["status"] != "ok"
                )
            )
            or None,
        )

    def _anjuke(self, options):
        from anjuke_province_crawler import CITY_SLUG_70, CITY_DATA_BUG
        import recheck_anjuke, market_crawler

        path = self.root / "output/anjuke_recheck/update_rotation.json"
        rotation = read(path)
        names = [c for c in CITY_SLUG_70 if c not in CITY_DATA_BUG and c != "苏州"]
        selected = sorted(names, key=lambda c: rotation.get(c, ""))[
            : options.get("batch_cities", 4)
        ]
        recheck_anjuke.crawl(
            selected, interval_seconds=options.get("interval_seconds", 30)
        )
        log = read(self.root / "output/anjuke_recheck/run.json", [])
        for c in selected:
            rotation[c] = now()
        atomic_json(path, rotation)
        good = sum(
            (r.get("observed_months", 0) > 0 for r in log if r.get("city") in selected)
        )
        market = market_crawler.run(
            selected,
            pages=1,
            mode="market",
            delay=options.get("interval_seconds", 30),
        )
        typed_pages = sum(
            r.get("status") == "parsed" and r.get("quotes", 0) > 0 for r in market
        )
        if not good and not typed_pages:
            raise RuntimeError("本批均价页和分类市场页没有可核对报价；保留旧数据")
        issues = [
            r for r in market if r.get("status") != "parsed" or not r.get("quotes")
        ]
        reasons = {
            "verification_required": "验证页，已停止本轮分类采集",
            "schema_unrecognized": "页面未取得可核验的分类报价",
            "parsed": "没有可核验的分类报价",
        }
        return dict(
            detail=f"{good}/{len(selected)} 城均价复核成功；市场分类页成功 {typed_pages} 页",
            partial=good < len(selected) or typed_pages < len(selected) or bool(issues),
            cities=selected,
            error="；".join(
                r["city"]
                + ": "
                + reasons.get(r.get("status"), r.get("error") or "采集未成功")[:120]
                for r in issues
            )
            or None,
        )

    def _communities(self, options):
        import community_crawler

        log = community_crawler.crawl(max_cities=options.get("batch_cities", 2))
        good = sum((r["status"] == "ok" for r in log))
        if log and (not good):
            raise RuntimeError(log[-1].get("error", "小区采集失败"))
        return dict(
            detail=f"本批更新 {good} 城；完整覆盖未作保证",
            partial=good < len(log),
            count=len(community_crawler.build().get("communities", [])),
        )

    def _community_profiles(self, options):
        import fang_crawler

        result = fang_crawler.run(
            cities=options.get("cities"),
            max_pages=options.get("max_pages", 12),
            interval=options.get("interval_seconds", 30),
        )
        if result.get("state") == "failed":
            raise RuntimeError(result.get("error") or "小区资料来源暂不可用")
        return dict(
            detail=f"房天下 {result.get('profiles', 0)} 条资料、{result.get('with_price', 0)} 条有参考价；身份核对后接入地图",
            partial=result.get("state") == "partial",
            skipped=not result.get("successful_pages"),
            state=result.get("state"),
            retry_after=result.get("retry_after")
            or (result.get("pause") or {}).get("until"),
            last_success=result.get("last_success"),
        )

    def _community_coverage(self, options):
        import community_neighbourhood_crawler

        log = community_neighbourhood_crawler.crawl()
        good = sum((e["status"] == "ok" for e in log))
        if log and (not good):
            raise RuntimeError(log[-1].get("error", "北京上海住宅片区补采失败"))
        return dict(
            detail=f"北京/上海住宅片区点位补查：{good}城成功；行政区界验证后按ID合并",
            partial=good < len(log),
        )

    def _metro(self, options):
        import public_location_crawler

        public_location_crawler.crawl_metro()
        log = read(self.root / "output/public_locations/metro_run.json").get(
            "results", []
        )
        if not any((r["status"] == "ok" for r in log)):
            raise RuntimeError("地铁站点没有成功更新")
        return dict(
            detail=f"{sum((r['status'] == 'ok' for r in log))}/{len(log)} 城站点刷新成功",
            partial=any((r["status"] != "ok" for r in log)),
        )

    def _hydrology(self, options):
        import hydrology_crawler

        community_status = read(self.root / "output/communities/status.json")
        latest = max(
            list(community_status.values())
            + list(read(self.root / "output/hydrology/status.json").values()),
            key=lambda e: e.get("checked_at", ""),
            default={},
        )
        if (
            latest.get("status") == "failed"
            and "429" in (latest.get("error") or "")
            and (not due(latest, 24))
        ):
            raise RuntimeError("Overpass 限流冷却中，保留水系数据")
        log = hydrology_crawler.crawl(max_cities=options.get("batch_cities", 1))
        good = sum((r["status"] == "ok" for r in log))
        if log and (not good):
            raise RuntimeError(log[-1].get("error", "水系采集失败"))
        return dict(
            detail=f"本批更新 {good} 城江河与大型水体；排除小溪沟渠，非水源保护区数据",
            partial=good < len(log),
        )

    def _catalog(self, options):
        for script in (
            "catalog_ingest.py",
            "metro_proximity.py",
            "build_city_index.py",
            "directory_coverage.py",
        ):
            self.run_script(script)
        report = read(self.root / "output/catalog/ingest_report.json")
        index = read(self.front_data / "communities/index.json")
        return dict(
            detail=f"主目录离线重建：入库 {report.get('total_communities', '?')} 条小区、生成 {len(index.get('cities', {}))} 城索引；目录覆盖矩阵已刷新，未发送网络请求"
        )
