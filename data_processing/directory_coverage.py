"""目录覆盖台账：按城市/区县/来源分别记录目录发现、分页、身份、属性、地铁与查询口径。

队列耗尽≠全城完整；各来源口径不同，禁止求和；缺失值一律 null 附原因。
仅标准库与仓库内已有模块，离线、确定性。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import community_catalog
import data_store

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
CATALOG_DIR = OUTPUT_DIR / "catalog"
DOCS_DIR = ROOT / "docs"
DOC_NAME = "目录覆盖矩阵20260920.md"
CITY_CODES = {"北京": "110000", "上海": "310000", "广州": "440100"}
TARGET_CITIES = ["北京", "上海", "广州"]

# 相对输入路径，统一以 build_ledger(root=...) 注入的 root 解析，便于测试替换。
INPUTS = {
    "fang_status": "output/fang/status.json",
    "fang_coverage": "output/fang/coverage.json",
    "fang_profiles": "output/fang/profiles.json",
    "bulk_state": "output/community_profiles/bulk_state.json",
    "profiles_status": "output/community_profiles/status.json",
    "communities": "data_processing/frontend/realestate/data/communities.json",
    "metro_operational": "data_processing/frontend/realestate/data/metro_operational.json",
    "baseline_report": "output/community_baseline/baseline_report.json",
}
DISTRICTS_REL = "data_processing/frontend/realestate/data/districts/{city}.json"

SAMPLE_QUERY = {
    "city_code": "110000",
    "year_start": 2000,
    "year_end": 2020,
    "forms": ["楼房"],
    "max_metro_distance_m": 1000,
    "station_operational_scope": "operational",
    "include_pending": False,
    "data_version": 1,
}

NOTES = [
    "not_additive=true：各来源、各维度口径不同，禁止把原始来源总数相加得出“全城总量”。",
    "转载≠独立证据：同一小区被转载不增加独立证据数。",
    "队列耗尽≠全城完整：来源分页取尽只反映本轮抓取进度，不代表该城已被完整收录。",
    "缺失数据一律记为 null 并附原因，绝不以 0 代替；计算得到的真实空集才可为 0。",
    "样本查询固定为 SAMPLE_QUERY，仅用于验证目录查询口径，不参与覆盖统计。",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load(root, rel):
    """Parse one input file; missing or malformed returns None, never raises."""
    try:
        return json.loads((Path(root) / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _dig(obj, *keys):
    cur = obj
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return None
    return cur


def _cell(value):
    return "—" if value is None else value


def _atomic_text(path, text):
    # markdown 不是 JSON，沿用 atomic_json 的 tmp+replace 语义保证落盘完整。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------- #
# baseline indexes
# --------------------------------------------------------------------------- #

def _city_rows(baseline):
    rows = _dig(baseline, "totals", "cities") or []
    return {row["city"]: row for row in rows if isinstance(row, dict) and row.get("city")}


def _district_rows(baseline):
    rows = _dig(baseline, "totals", "districts") or []
    index = {}
    for row in rows:
        if isinstance(row, dict) and row.get("city") and row.get("district"):
            index[(row["city"], row["district"])] = row
    return index


def _district_names(root, city, district_index):
    names = {district for (name, district) in district_index if name == city}
    doc = _load(root, DISTRICTS_REL.format(city=city))
    for feature in _dig(doc, "features") or []:
        name = _dig(feature, "properties", "name")
        if name:
            names.add(name)
    return sorted(names)


# --------------------------------------------------------------------------- #
# per-group builders (mutate `reasons` with null explanations)
# --------------------------------------------------------------------------- #

def _city_discovery(city, records, status, reasons):
    group = {"source_id_count": None, "declared_count": None, "declared_reason": None}
    if records is None:
        reasons["discovery.source_id_count"] = "缺少 output/fang/profiles.json"
    else:
        group["source_id_count"] = sum(
            1 for rec in records if isinstance(rec, dict) and rec.get("city") == city)
    declared = _dig(status, "by_city", city, "profiles")
    if declared is None:
        reasons["discovery.declared_count"] = "fang 状态未提供该城市宣称档案数"
    else:
        group["declared_count"] = declared
    return group


def _fang_source_discovery(records, status, reasons):
    group = {"source_id_count": None, "declared_count": None, "declared_reason": None}
    if records is None:
        reasons["discovery.source_id_count"] = "缺少 output/fang/profiles.json"
    else:
        group["source_id_count"] = len(records)
    declared = status.get("profiles") if isinstance(status, dict) else None
    if declared is None:
        reasons["discovery.declared_count"] = "fang 状态缺少 profiles 宣称数"
    else:
        group["declared_count"] = declared
    return group


def _is_osm(rec):
    if not isinstance(rec, dict):
        return False
    if rec.get("source") == "osm" or rec.get("related_osm_ids"):
        return True
    return any(str(tag).lower() == "osm" for tag in (rec.get("source_tags") or []))


def _map_source_discovery(name, communities, reasons):
    group = {"source_id_count": None, "declared_count": None,
             "declared_reason": "来源未宣称目录总数"}
    if communities is None:
        reasons["discovery.source_id_count"] = "缺少 communities.json"
        return group
    if name == "osm":
        group["source_id_count"] = sum(1 for rec in communities if _is_osm(rec))
    else:
        group["source_id_count"] = sum(1 for rec in communities if not _is_osm(rec))
    return group


def _identity_group():
    return {"dedup_communities": None, "map_located": None,
            "unknown_location": None, "identity_pending": None}


def _city_identity(city, communities, communities_doc, records, row, reasons):
    group = _identity_group()
    dedup = _dig(communities_doc, "cities", city, "count")
    if dedup is None:
        reasons["identity.dedup_communities"] = "communities.json 缺少该城市小区计数"
    else:
        group["dedup_communities"] = dedup
    if communities is None:
        reasons["identity.map_located"] = "缺少 communities.json"
    else:
        group["map_located"] = sum(
            1 for rec in communities
            if isinstance(rec, dict) and rec.get("city") == city
            and rec.get("lat") is not None and rec.get("lng") is not None)
    if row is not None and row.get("unknown_location") is not None:
        group["unknown_location"] = row.get("unknown_location")
    else:
        reasons["identity.unknown_location"] = "缺少基线统计 unknown_location"
    if not isinstance(records, list):
        reasons["identity.identity_pending"] = "缺少 fang/profiles.json，无法按城市统计身份待核"
    else:
        # 逐城统计，避免把全局 states 合计重复计入每个城市
        group["identity_pending"] = sum(
            1 for rec in records
            if isinstance(rec, dict) and rec.get("city") == city
            and not rec.get("identity_verified"))
    return group


def _district_identity(city, district, communities, row, reasons):
    group = _identity_group()
    if row is not None and row.get("total") is not None:
        group["dedup_communities"] = row.get("total")
    else:
        reasons["identity.dedup_communities"] = "缺少基线统计 total"
    if communities is None:
        reasons["identity.map_located"] = "缺少 communities.json"
    else:
        group["map_located"] = sum(
            1 for rec in communities
            if isinstance(rec, dict) and rec.get("city") == city and rec.get("district") == district
            and rec.get("lat") is not None and rec.get("lng") is not None)
    if row is not None and row.get("unknown_location") is not None:
        group["unknown_location"] = row.get("unknown_location")
    else:
        reasons["identity.unknown_location"] = "缺少基线统计 unknown_location"
    reasons["identity.identity_pending"] = "区县级缺少待核分项"
    return group


def _fang_source_identity(records, states_sum, reasons):
    group = _identity_group()
    if records is None:
        reasons["identity.dedup_communities"] = "缺少 output/fang/profiles.json"
    else:
        group["dedup_communities"] = len(records)
    reasons["identity.map_located"] = "来源台账未记录坐标，见城市维度"
    reasons["identity.unknown_location"] = "来源台账未记录位置，见城市维度"
    if states_sum is None:
        reasons["identity.identity_pending"] = "fang coverage.json 缺少 states"
    else:
        group["identity_pending"] = states_sum
    return group


def _attributes(row, reasons):
    group = {"year_known": None, "year_denominator": None,
             "form_known": None, "form_denominator": None,
             "villa_subtype_known": None, "villa_denominator": None}
    if row is None:
        for field in group:
            reasons[f"attributes.{field}"] = "缺少基线统计（baseline_report.totals）"
        return group
    total = row.get("total")
    group["year_known"] = row.get("year_known")
    group["year_denominator"] = total
    group["form_known"] = row.get("form_known")
    group["form_denominator"] = total
    group["villa_subtype_known"] = row.get("villa_subtype_known")
    group["villa_denominator"] = total
    if total is None:
        for field in ("year_denominator", "form_denominator", "villa_denominator"):
            reasons[f"attributes.{field}"] = "基线统计缺少 total"
    return group


def _complete_records(row, reasons):
    group = {"year_and_villa_and_location": None, "denominator": None}
    if row is None:
        group_reason = "缺少基线统计（baseline_report.totals）"
        reasons["complete_records.year_and_villa_and_location"] = group_reason
        reasons["complete_records.denominator"] = group_reason
        return group
    group["year_and_villa_and_location"] = row.get("all_three_known")
    group["denominator"] = row.get("total")
    if row.get("all_three_known") is None:
        reasons["complete_records.year_and_villa_and_location"] = "基线统计缺少 all_three_known"
    if row.get("total") is None:
        reasons["complete_records.denominator"] = "基线统计缺少 total"
    return group


def _cross_city_adjacent(metro):
    stations = metro.get("stations") if isinstance(metro, dict) else None
    cities_with_coords = set()
    if isinstance(stations, list):
        for station in stations:
            if (isinstance(station, dict) and station.get("lat") is not None
                    and station.get("lng") is not None):
                cities_with_coords.add(station.get("city") or station.get("city_code"))
    capable = len(cities_with_coords) >= 2
    return {
        "capability": capable,
        "performed": False,
        "reason": ("站点坐标覆盖多个城市，具备跨城相邻判定能力，但按设计不做 O(n^2) 计算"
                   if capable else "站点坐标覆盖不足两个城市，无法做跨城相邻判定"),
    }


def _metro(metro, city, cross, reasons):
    group = {"stations": None, "lines": None, "verification_state": None,
             "coverage_incomplete": None, "cross_city_adjacent": cross}
    entry = _dig(metro, "coverage", "cities", city)
    if not isinstance(entry, dict):
        for field in ("stations", "lines", "verification_state", "coverage_incomplete"):
            reasons[f"metro.{field}"] = "缺少 metro_operational 城市条目"
        return group
    group["stations"] = entry.get("station_count")
    group["lines"] = entry.get("line_count")
    group["verification_state"] = entry.get("verification_state")
    group["coverage_incomplete"] = entry.get("coverage_incomplete")
    return group


def _city_query_stub(reasons):
    reasons["query"] = "见 sample_query"
    return {"query": None, "supported": None, "pending": None, "mismatch": None,
            "uncovered": None, "conflict": None, "coverage_note": None}


def _directory_pagination(status, bulk, profiles_status, has_status=False, has_bulk=False):
    group = {"reachable_pages": None, "successful_pages": None, "records_fetched": None,
             "reported_pages_total": None, "last_page_state": None,
             "truncated": None, "failure_count": None, "last_success_time": None,
             "pause_reason": None, "pause_until": None, "retry_after": None,
             "frontier_truncated": None, "capped": None, "bulk_state": None, "bulk_error": None}
    reasons = {}
    pages = bulk.get("pages") if isinstance(bulk, dict) else None
    if not isinstance(pages, list):
        pages = None
    if pages is None:
        reasons["directory_pagination.reachable_pages"] = "缺少 bulk_state.pages"
        reasons["directory_pagination.successful_pages"] = "缺少 bulk_state.pages"
    else:
        entries = [p for p in pages if isinstance(p, dict)]
        reachable = bulk.get("run_pages") if isinstance(bulk, dict) else None
        if not isinstance(reachable, int):
            reachable = len(entries)
        # reachable/successful 统一以页为单位，count 为记录数，勿混算
        group["reachable_pages"] = reachable
        group["successful_pages"] = sum(1 for p in entries if not p.get("capped"))
        group["records_fetched"] = sum(
            p.get("count") for p in entries if isinstance(p.get("count"), int))
        group["reported_pages_total"] = sum(
            p.get("reported_pages") for p in entries
            if isinstance(p.get("reported_pages"), int))
        group["capped"] = any(bool(p.get("capped")) for p in entries)
    state = _dig(profiles_status, "state") or _dig(bulk, "state")
    if state is None:
        reasons["directory_pagination.last_page_state"] = "缺少 community_profiles 状态"
    else:
        group["last_page_state"] = state
    group["frontier_truncated"] = status.get("frontier_truncated") if isinstance(status, dict) else None
    if group["frontier_truncated"] is None and group["capped"] is None:
        reasons["directory_pagination.truncated"] = "缺少 bulk_state.pages 与 frontier_truncated"
    else:
        group["truncated"] = bool(group["frontier_truncated"]) or bool(group["capped"])
    last_replay = _dig(status, "last_replay")
    if not has_status and not has_bulk:
        reasons["directory_pagination.failure_count"] = "缺少 last_replay 与 last_failed_fetch"
    else:
        failures = last_replay.get("failures") if isinstance(last_replay, dict) else None
        failures = failures if isinstance(failures, list) else []
        failed_fetch = bulk.get("last_failed_fetch") if isinstance(bulk, dict) else None
        group["failure_count"] = len(failures) + (1 if failed_fetch else 0)
    last_success = status.get("last_success") if isinstance(status, dict) else None
    if last_success is None:
        reasons["directory_pagination.last_success_time"] = "缺少 fang last_success"
    else:
        group["last_success_time"] = last_success
    group["pause_reason"] = _dig(status, "pause", "reason")
    group["pause_until"] = _dig(status, "pause", "until")
    group["retry_after"] = ((bulk.get("retry_after") if isinstance(bulk, dict) else None)
                            or _dig(profiles_status, "retry_after"))
    group["bulk_state"] = bulk.get("state") if isinstance(bulk, dict) else None
    group["bulk_error"] = bulk.get("error") if isinstance(bulk, dict) else None
    if group["pause_reason"] is None:
        reasons["directory_pagination.pause_reason"] = "fang 状态无暂停记录"
    if group["pause_until"] is None:
        reasons["directory_pagination.pause_until"] = "fang 状态无暂停截止"
    if group["retry_after"] is None:
        reasons["directory_pagination.retry_after"] = "无重试时间"
    return group, reasons


def _states_sum(coverage):
    states = _dig(coverage, "states")
    if not isinstance(states, dict) or not states:
        return None
    return sum(value for value in states.values() if isinstance(value, int))


def _sample_query(conn):
    blank = {"query": None, "supported": None, "pending": None, "mismatch": None,
             "uncovered": None, "conflict": None, "coverage_note": None}
    if conn is None:
        blank["reason"] = "no catalog connection"
        return blank
    try:
        resp = community_catalog.snapshot_query(conn, dict(SAMPLE_QUERY))
    except Exception as exc:  # 只读台账不得因目录查询失败而中断
        blank["reason"] = f"目录查询失败：{exc}"
        return blank
    counts = resp.get("status_counts") or {}
    items = resp.get("items") or []
    return {
        "query": resp.get("query"),
        "supported": counts.get("supported"),
        "pending": counts.get("unknown"),
        "mismatch": counts.get("excluded"),
        "uncovered": sum(1 for item in items if item.get("missing_fields")),
        "conflict": counts.get("conflict"),
        "coverage_note": resp.get("coverage_note"),
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def build_ledger(root=ROOT, conn=None, generated_at=None):
    root = Path(root)
    data = {name: _load(root, rel) for name, rel in INPUTS.items()}
    status = data["fang_status"] if isinstance(data["fang_status"], dict) else {}
    coverage = data["fang_coverage"] if isinstance(data["fang_coverage"], dict) else {}
    profiles_doc = data["fang_profiles"] if isinstance(data["fang_profiles"], dict) else {}
    bulk = data["bulk_state"] if isinstance(data["bulk_state"], dict) else {}
    profiles_status = data["profiles_status"] if isinstance(data["profiles_status"], dict) else {}
    communities_doc = data["communities"] if isinstance(data["communities"], dict) else {}
    metro = data["metro_operational"] if isinstance(data["metro_operational"], dict) else {}
    baseline = data["baseline_report"] if isinstance(data["baseline_report"], dict) else {}

    records = profiles_doc.get("records") if data["fang_profiles"] is not None else None
    communities = communities_doc.get("communities") if data["communities"] is not None else None

    city_rows = _city_rows(baseline)
    district_index = _district_rows(baseline)
    cross_city = _cross_city_adjacent(metro)
    fang_pagination, pagination_reasons = _directory_pagination(
        status, bulk, profiles_status,
        has_status=data["fang_status"] is not None,
        has_bulk=data["bulk_state"] is not None)
    states_sum = _states_sum(coverage)

    cities = {}
    for city in sorted(TARGET_CITIES):
        reasons = {}
        bucket = {
            "discovery": _city_discovery(city, records, status, reasons),
            "identity": _city_identity(city, communities, communities_doc, records,
                                       city_rows.get(city), reasons),
            "attributes": _attributes(city_rows.get(city), reasons),
            "complete_records": _complete_records(city_rows.get(city), reasons),
            "metro": _metro(metro, city, cross_city, reasons),
        }
        bucket["query"] = _city_query_stub(reasons)
        bucket["reasons"] = reasons
        cities[city] = bucket

    districts = {}
    for city in sorted(TARGET_CITIES):
        for district in _district_names(root, city, district_index):
            reasons = {}
            row = district_index.get((city, district))
            bucket = {
                "identity": _district_identity(city, district, communities, row, reasons),
                "attributes": _attributes(row, reasons),
                "complete_records": _complete_records(row, reasons),
            }
            bucket["reasons"] = reasons
            districts[f"{city}|{district}"] = bucket

    sources = {}
    for name in ("fang", "osm", "other"):
        reasons = {}
        if name == "fang":
            bucket = {
                "discovery": _fang_source_discovery(records, status, reasons),
                "directory_pagination": fang_pagination,
                "identity": _fang_source_identity(records, states_sum, reasons),
            }
            reasons.update(pagination_reasons)
        else:
            bucket = {"discovery": _map_source_discovery(name, communities, reasons)}
        bucket["reasons"] = reasons
        sources[name] = bucket

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utcnow(),
        "not_additive": True,
        "notes": list(NOTES),
        "cities": cities,
        "districts": {key: districts[key] for key in sorted(districts)},
        "sources": {name: sources[name] for name in sorted(sources)},
        "sample_query": _sample_query(conn),
    }


def _fang_pagination(ledger):
    return _dig(ledger, "sources", "fang", "directory_pagination") or {}


def render_markdown(ledger):
    lines = [
        "# 目录覆盖矩阵",
        "",
        f"- 生成时间：{ledger['generated_at']}",
        f"- schema_version：{ledger['schema_version']}",
        f"- not_additive：{ledger['not_additive']}",
        "",
        "> 队列耗尽≠全城完整；各来源口径不同，禁止求和。缺失值为 null 并附原因。",
        "",
        "## 城市覆盖矩阵",
        "",
        "| 城市 | 地图小区数 | 年代已知 | 类型已知 | 别墅子型 | 三项齐全 | 未知位置 | 身份待核 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for city in sorted(ledger.get("cities") or {}):
        bucket = ledger["cities"][city]
        identity = bucket.get("identity") or {}
        attributes = bucket.get("attributes") or {}
        complete = bucket.get("complete_records") or {}
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            city, _cell(identity.get("dedup_communities")),
            _cell(attributes.get("year_known")), _cell(attributes.get("form_known")),
            _cell(attributes.get("villa_subtype_known")),
            _cell(complete.get("year_and_villa_and_location")),
            _cell(identity.get("unknown_location")), _cell(identity.get("identity_pending"))))

    lines += ["", "## 区县覆盖矩阵", "",
              "| 城市 | 区县 | 总数 | 年代已知 | 类型已知 | 未知位置 |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for key in sorted(ledger.get("districts") or {}):
        city, district = key.split("|", 1)
        bucket = ledger["districts"][key]
        attributes = bucket.get("attributes") or {}
        identity = bucket.get("identity") or {}
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            city, district, _cell(attributes.get("year_denominator")),
            _cell(attributes.get("year_known")), _cell(attributes.get("form_known")),
            _cell(identity.get("unknown_location"))))

    lines += ["", "## 来源覆盖", "",
              "| 来源 | 发现ID数 | 宣称数 | 说明 |",
              "| --- | ---: | ---: | --- |"]
    for name in sorted(ledger.get("sources") or {}):
        discovery = ledger["sources"][name].get("discovery") or {}
        lines.append("| {} | {} | {} | {} |".format(
            name, _cell(discovery.get("source_id_count")),
            _cell(discovery.get("declared_count")), _cell(discovery.get("declared_reason"))))

    pagination = _fang_pagination(ledger)
    lines += ["", "## 截断与访问限制清单", ""]
    for label, value in (
        ("暂停原因", pagination.get("pause_reason")),
        ("暂停截止", pagination.get("pause_until")),
        ("重试时间", pagination.get("retry_after")),
        ("前沿截断(frontier_truncated)", pagination.get("frontier_truncated")),
        ("分页命中上限(capped)", pagination.get("capped")),
        ("采集状态", pagination.get("bulk_state")),
        ("采集错误", pagination.get("bulk_error")),
    ):
        lines.append(f"- {label}：{_cell(value)}")

    sample = ledger.get("sample_query") or {}
    location_values = [value for bucket in (ledger.get("cities") or {}).values()
                       for value in [_dig(bucket, "identity", "unknown_location")]
                       if value is not None]
    lines += ["", "## 覆盖缺口说明", "",
              f"- 样本查询口径：supported={_cell(sample.get('supported'))}、"
              f"pending={_cell(sample.get('pending'))}、mismatch={_cell(sample.get('mismatch'))}、"
              f"conflict={_cell(sample.get('conflict'))}、uncovered={_cell(sample.get('uncovered'))}。",
              f"- 未知位置：城市维度合计 {_cell(sum(location_values) if location_values else None)}（仅统计已计算项）。",
              f"- 身份待核：城市维度按 fang/profiles.json 的 identity_verified 逐城统计；"
              f"来源级合计 {_cell(_dig(ledger, 'sources', 'fang', 'identity', 'identity_pending'))}"
              "为全局口径（不区分城市，避免重复计数）。",
              "- 以上缺口按“未采集 / 来源没有 / 解析失败 / 资料冲突”四类解释，详见下方说明。"]

    lines += ["", "## 队列耗尽≠全城完整", "",
              "来源队列（分页）耗尽只说明本轮把来源给出的列表翻到了底，并不代表全城小区都已进入目录：",
              "来源本身可能未收录、分页可能被截断、记录可能因解析失败或资料冲突而未入册。",
              "因此本台账按城市/区县/来源分别记账，绝不以任何单一来源的队列进度推断“全城完整”。"]

    lines += ["", "## 四分类说明", "",
              "- 未采集：本轮尚未抓取到的记录（队列未覆盖）。",
              "- 来源没有：来源目录本身未收录该小区。",
              "- 解析失败：抓取到但无法解析出可用字段。",
              "- 资料冲突：同名但区县或坐标冲突，被隔离且不覆盖地图。"]

    lines += ["", "## 未知值原因", "", "| 维度 | 字段 | 原因 |", "| --- | --- | --- |"]
    reason_rows = 0
    for dimension, buckets in (("城市", ledger.get("cities") or {}),
                               ("区县", ledger.get("districts") or {}),
                               ("来源", ledger.get("sources") or {})):
        for name in sorted(buckets):
            for field in sorted(buckets[name].get("reasons") or {}):
                lines.append(f"| {dimension}·{name} | {field} | {buckets[name]['reasons'][field]} |")
                reason_rows += 1
    if not reason_rows:
        lines.append("| — | — | 无 |")
    lines.append("")
    return "\n".join(lines)


def _dedup_count(bucket):
    dedup = _dig(bucket, "identity", "dedup_communities")
    if dedup is not None:
        return dedup
    return _dig(bucket, "discovery", "source_id_count")


def _pagination_fields(bucket):
    group = bucket.get("directory_pagination") or {}
    truncated = group.get("truncated")
    return (group.get("reachable_pages"),
            None if truncated is None else (1 if truncated else 0),
            group.get("last_page_state"),
            group.get("last_success_time"))


def _write_row(conn, city_code, district_code, section, bucket):
    pages, truncated, status, fetched_at = _pagination_fields(bucket)
    conn.execute(
        "DELETE FROM directory_coverage WHERE city_code IS ? AND district_code IS ? AND directory_section=?",
        (city_code, district_code, section))
    conn.execute(
        "INSERT INTO directory_coverage (city_code, district_code, directory_section, pages, truncated,"
        " status, dedup_count, fetched_at, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (city_code, district_code, section, pages, truncated, status, _dedup_count(bucket), fetched_at,
         json.dumps(bucket, ensure_ascii=False, allow_nan=False)))
    return 1


def upsert_rows(conn, ledger):
    count = 0
    for city in sorted(ledger.get("cities") or {}):
        count += _write_row(conn, CITY_CODES.get(city), None, "summary", ledger["cities"][city])
    for key in sorted(ledger.get("districts") or {}):
        city, district = key.split("|", 1)
        count += _write_row(conn, CITY_CODES.get(city), district, "district", ledger["districts"][key])
    for name in sorted(ledger.get("sources") or {}):
        count += _write_row(conn, None, None, f"source:{name}", ledger["sources"][name])
    conn.commit()
    return count


def write_outputs(ledger, root=ROOT):
    root = Path(root)
    json_path = root / "output" / "catalog" / "directory_coverage.json"
    md_path = root / "docs" / DOC_NAME
    data_store.atomic_json(json_path, ledger)
    _atomic_text(md_path, render_markdown(ledger))
    return json_path, md_path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = community_catalog.connect()
    try:
        community_catalog.create_schema(conn)
        ledger = build_ledger(ROOT, conn=conn)
        rows = upsert_rows(conn, ledger)
    finally:
        conn.close()
    json_path, md_path = write_outputs(ledger, ROOT)
    print(f"json: {json_path}")
    print(f"markdown: {md_path}")
    print(f"cities={len(ledger['cities'])} districts={len(ledger['districts'])} "
          f"sources={len(ledger['sources'])} rows={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
