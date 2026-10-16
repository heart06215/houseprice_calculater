# -*- coding: utf-8 -*-
"""前端数据完整性校验：schema、字段齐全性、坐标唯一性、关键断链检查。"""

import json
import re
import sys
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
RE_DIR = FRONTEND_DIR / "realestate"

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def check_housing_data():
    f = FRONTEND_DIR / "intersection_housing_data.json"
    if not f.exists():
        err(f"缺少 {f.name}（主看板 fetch 目标）")
        return
    data = json.loads(f.read_text(encoding="utf-8"))
    cities = data.get("cities", [])
    if not cities:
        err("intersection_housing_data.json 无 cities 数据")
        return
    required = ["city", "tier", "lat", "lon", "current_price", "max_price",
                "min_price", "peak_date", "peak_year", "drop_from_peak",
                "change_pct_12m", "monthly"]
    for c in cities:
        missing = [k for k in required if k not in c]
        if missing:
            err(f"城市 {c.get('city', '?')} 缺字段: {missing}")
            break
        if not c.get("monthly"):
            err(f"城市 {c['city']} monthly 为空")
            break
    meta = data.get("meta", {})
    print(f"  [OK] 主数据: {len(cities)} 城, 更新至 {meta.get('data_updated', '?')}")

    # 前端消费一致性：index.html 引用的 fetch 目标
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    if "intersection_housing_data.json" not in html and "window.__HOUSING_DATA__" not in (FRONTEND_DIR / "assets" / "js" / "shared" / "data-loader.js").read_text(encoding="utf-8"):
        err("主看板未引用 intersection_housing_data.json")


def check_properties():
    f = RE_DIR / "data" / "properties.json"
    if not f.exists():
        err("缺少 realestate/data/properties.json")
        return
    props = json.loads(f.read_text(encoding="utf-8"))
    required = ["id", "name", "city", "lat", "lng", "price_avg", "source", "precision", "type"]
    bad = 0
    for p in props:
        missing = [k for k in required if k not in p]
        if missing:
            bad += 1
            if bad <= 3:
                err(f"楼盘 {p.get('name', p.get('id', '?'))} 缺字段: {missing}")
        if not isinstance(p.get("lat"), (int, float)) or not isinstance(p.get("lng"), (int, float)):
            if p.get('precision') == 'unknown' and p.get('lat') is None and p.get('lng') is None and p.get('source_url'):
                continue  # Explicitly unlocated public projects are list-only, not bogus map pins.
            err(f"楼盘 {p.get('name', '?')} 坐标非法: {p.get('lat'), p.get('lng')}")
            bad += 1
    if bad == 0:
        print(f"  [OK] 楼盘数据: {len(props)} 条, schema 统一")

    # schema 混存回归检查（贝壳 latitude/longitude 旧字段不得回流）
    legacy = [p for p in props if "latitude" in p or "longitude" in p]
    if legacy:
        err(f"schema 回归: {len(legacy)} 条记录仍是旧版 latitude/longitude 字段")

    coords = {}
    for p in props:
        if p.get('lat') is None or p.get('lng') is None:
            continue
        key = (round(p["lat"], 4), round(p["lng"], 4))
        coords.setdefault(key, 0)
        coords[key] += 1
    stacked = sum(1 for v in coords.values() if v > 5)
    print(f"  [OK] 唯一坐标 {len(coords)} 个 / {len(props)} 条"
          + (f"，{stacked} 个坐标点超过 5 条堆叠（聚合展示兜底）" if stacked else ""))
    zero = sum(1 for p in props if p.get("price_avg", 0) <= 0)
    if zero:
        warn(f"{zero} 条楼盘无报价（前端显示'暂无报价'）")
    by_source = {}
    for p in props:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    print(f"  [OK] 来源分布: {by_source}")


def check_communities():
    from community_attributes import PROFILE_VERSION,excluded_residence
    f=RE_DIR/'data/communities.json'
    if not f.exists():err('缺少小区位置库 communities.json');return
    data=json.loads(f.read_text(encoding='utf-8'));rows=data.get('communities',[])
    if data.get('profile_version')!=PROFILE_VERSION:err('小区分类与年代口径过期')
    if not rows:err('小区位置库为空');return
    identities=set();counts={}
    for row in rows:
        key=(row.get('city'),row.get('id'))
        if key in identities:err('小区位置身份重复: '+str(key))
        identities.add(key);counts[row.get('city')]=counts.get(row.get('city'),0)+1
        if not row.get('name') or not row.get('source_url','').startswith('https://www.openstreetmap.org/'):err('小区缺少名称或有效来源')
        if row.get('coordinate_system')!='WGS84' or not isinstance(row.get('lat'),(int,float)) or not isinstance(row.get('lng'),(int,float)) or not (15<row['lat']<55 and 70<row['lng']<140):err('小区坐标异常: '+str(key))
        if any(k in row for k in ['price_avg','listings','current_price']):err('小区位置库混入房源报价')
        if excluded_residence(row.get('source_tags',{}),row.get('name','')) or '宿舍' in row.get('housing_types',[]):err('小区位置库混入宿舍: '+str(key))
        if row.get('type')=='纯别墅':err('别墅分类未合并: '+str(key))
        year=row.get('completion_year',{})
        if any(e.get('scope')!='小区' for e in year.get('evidence',[])):err('小区年份混入单栋日期: '+str(key))
        if year.get('status')=='recorded' and (not year.get('evidence') or not year.get('start') or year['end']<year['start']):err('小区年份缺少来源或范围无效: '+str(key))
        if '公寓' in row.get('housing_types',[]) and not any(e.get('kind')=='公寓' and e.get('field') not in ('building','residential') for e in row.get('type_evidence',[])):err('公寓分类仅凭多户住宅标签: '+str(key))
        if row.get('pure_villa') and not any('纯别墅' in e.get('value','') and e.get('confidence')=='source' for e in row.get('type_evidence',[])):err('纯别墅缺少明确证据: '+str(key))
    for city,count in counts.items():
        if data.get('cities',{}).get(city,{}).get('count')!=count:err('小区城市计数不一致: '+str(city))
    known=sum(r.get('completion_year',{}).get('status')=='recorded' for r in rows)
    if data.get('profile_stats',{}).get('year_known')!=known:err('小区年代覆盖计数不一致')
    print(f'  [OK] 小区位置 {len(rows)} 个，{len(counts)} 城，小区年代 {known} 个，无单栋年份或挂牌价格字段')


def check_water():
    from hydrology_crawler import FILTER_VERSION,MIN_WATER_AREA_KM2,MIN_RIVER_WIDTH_M,major_kind,river_width_m
    path=RE_DIR/'data/water.json'
    if not path.exists():err('缺少水系索引 water.json');return
    index=json.loads(path.read_text(encoding='utf-8'));total=0
    if index.get('filter_version')!=FILTER_VERSION:err('水系索引不是当前大型水体口径')
    for city,meta in index.get('cities',{}).items():
        path=RE_DIR/'data/water'/f'{city}.json'
        if not path.exists():err(f'{city}水系几何文件缺失');continue
        geo=json.loads(path.read_text(encoding='utf-8'));features=geo.get('features',[])
        if geo.get('filter_version')!=FILTER_VERSION:err(f'{city}水系清洗口径过期')
        if geo.get('city')!=city or geo.get('coordinate_system')!='WGS84' or len(features)!=meta['count']:err(f'{city}水系索引与几何不一致')
        if len({f['id'] for f in features})!=len(features):err(f'{city}水系ID重复')
        for f in features:
            props=f.get('properties',{});kind=props.get('kind');gtype=f.get('geometry',{}).get('type')
            if gtype not in ('LineString','Polygon','MultiPolygon') or not props.get('source_url','').startswith('https://www.openstreetmap.org/'):err(f'{city}水系几何或来源无效');break
            if kind!=major_kind(props.get('source_tags',{}),props.get('name','')) or kind is None:err(f'{city}水系混入非目标分类');break
            if gtype=='LineString' and kind=='江河' and (not props.get('name') or (river_width_m(props.get('source_tags',{})) or 0)<MIN_RIVER_WIDTH_M):err(f'{city}江河线缺少名称或达标宽度证据');break
            if gtype in ('Polygon','MultiPolygon') and kind!='海域' and props.get('area_km2',0)<MIN_WATER_AREA_KM2:err(f'{city}水系混入小面积水面');break
        total+=len(features)
    print(f"  [OK] 水系 {total} 处要素，{len(index.get('cities',{}))} 城，按需加载")


def check_links(dashboard_only=False):
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    comp = (FRONTEND_DIR / "assets" / "js" / "dashboard" / "components.js").read_text(encoding="utf-8")
    if "city.html" in html or "city.html" in comp:
        err("主看板仍存在 city.html 断链（应为 realestate/map.html?city=）")
    if dashboard_only:
        app = (FRONTEND_DIR/'assets/js/dashboard/app.js').read_text(encoding='utf-8')
        city = (FRONTEND_DIR/'assets/js/dashboard/city_analysis.js').read_text(encoding='utf-8')
        if any(token in app+city+comp for token in ('propsmap', 'openStreetMap', 'realestate/map.html')):
            err('主看板仍含小区地图入口')
        for tag in re.findall(r'(?:src|href)="([^"]+)"', html):
            local = tag.split('?')[0]
            if not local.startswith(('http', '#')) and not (FRONTEND_DIR/local).is_file():
                err('缺少页面静态资源: '+local)
        print('  [OK] 主看板静态资源与入口检查完成')
        return
    if "realestate/map.html?city=" not in comp:
        err("主看板缺少指向 realestate/map.html?city= 的入口")
    else:
        print("  [OK] 主看板 → 楼盘地图链接正常 (realestate/map.html?city=)")
    info_tab=(FRONTEND_DIR/"assets"/"js"/"dashboard"/"information_tab.js").read_text(encoding="utf-8")
    if "数据来源:安居客" not in info_tab:
        warn("市场概览缺少数据来源标注(安居客/中指网/国家统计局)")
    else:
        print("  [OK] 市场概览含数据来源标注")
    for js in ["app.js", "community.js", "community-map.js", "community-list.js", "loader.js", "nation.js", "contours.js"]:
        if not (RE_DIR / "assets" / "js" / js).exists():
            err(f"缺少 realestate/assets/js/{js}")
    for f in ["map.html", "index.html"]:
        if not (RE_DIR / f).exists():
            err(f"缺少 realestate/{f}")
    # 旧版断链回归
    for name in ["index.html", "map.html"]:
        txt = (RE_DIR / name).read_text(encoding="utf-8")
        if "list.html" in txt:
            err(f"realestate/{name} 仍引用不存在的 list.html")


def check_vendor(dashboard_only=False):
    vendor = FRONTEND_DIR / "assets" / "vendor"
    need = ["vue.global.prod.js", "echarts.min.js", "china.geo.json"]
    if not dashboard_only:need += ["leaflet.js", "leaflet.css"]
    for n in need:
        p = vendor / n
        if not p.exists():
            err(f"缺少本地依赖 assets/vendor/{n}")
        elif p.stat().st_size < 1000:
            err(f"本地依赖疑似下载失败(过小): {n}")
    if not errors or all("vendor" not in e for e in errors):
        print(f"  [OK] 本地依赖齐全: {len(need)} 项")


def check_singlefile_freshness(dashboard_only=False):
    """单文件版(map_inline/standalone)内嵌数据, 数据更新后必须重新构建,
    否则用户双击打开的离线版仍是旧数据(首页数据陈旧事故的回归检测)。"""
    import time
    data_files = [
        FRONTEND_DIR / "intersection_housing_data.json",
        RE_DIR / "data" / "communities.json",
        RE_DIR / "data" / "water.json",
    ]
    builds = [
        BASE_DIR / "standalone_dashboard.html",
        RE_DIR / "map_inline.html",
    ]
    if dashboard_only:
        data_files=data_files[:1]
        builds=builds[:1]
    newest_data = max((p.stat().st_mtime for p in data_files if p.exists()), default=0)
    stale = []
    for b in builds:
        if not b.exists():
            warn(f"缺少单文件版 {b.name} (python build_standalone.py / build_inline.py)")
            continue
        if b.stat().st_mtime < newest_data:
            stale.append(b.name)
    if stale:
        warn(f"单文件版数据过期: {', '.join(stale)} — 请重新构建后再分发")
    else:
        print("  [OK] 单文件版与数据同步")


def main(dashboard_only=True):
    sys.stdout.reconfigure(encoding="utf-8")
    errors.clear()
    warnings.clear()
    print("  --- 单文件版时效 ---")
    check_singlefile_freshness(dashboard_only)
    print("  --- 主数据 ---")
    check_housing_data()
    if dashboard_only:
        try:
            from release_contract import validate
            checks=validate(json.loads((FRONTEND_DIR/'intersection_housing_data.json').read_text(encoding='utf-8')))
            print('  [OK] 城市、月份、住宅类别、价格证据和指数转换:', checks)
        except (ValueError,KeyError,OSError,TypeError) as exc:
            err('发布口径校验失败: '+str(exc))
    else:
        print("  --- 小区位置数据 ---")
        check_communities()
        check_water()
    print("  --- 页面与链接 ---")
    check_links(dashboard_only)
    check_vendor(dashboard_only)

    print()
    for w in warnings:
        print(f"  [WARN] {w}")
    if errors:
        print(f"\n[FAIL] {len(errors)} 个错误:")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print("[PASS] 全部校验通过" + (f"（{len(warnings)} 个警告）" if warnings else ""))
    return 0


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashboard-only',action='store_true',help='兼容参数；默认只校验主看板')
    parser.add_argument('--include-map',action='store_true',help='额外校验本地小区地图资料')
    args=parser.parse_args()
    sys.exit(main(not args.include_map))
