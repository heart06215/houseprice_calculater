"""70城价格引擎：历史导入与可追溯观测分开，缺失月按完整统计局环比链推导。
旧版所谓真实锚点缺少原始凭据，不能视为已核验实采或成交价格。
来源规则见 price_provenance.py。
"""

import json
import sys
import io
import math
from price_provenance import apply_provenance,listing_evidence
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANJUKE_DIR = PROJECT_ROOT / "output" / "anjuke"
STATS_DIR = PROJECT_ROOT / "output" / "stats_gov"
INTERSECTION_FILE = PROJECT_ROOT / "data_processing" / "frontend" / "intersection_cities.json"
OUTPUT_DIR = PROJECT_ROOT / "data_processing" / "frontend"

# ============================================================
# GYBrand 2025 城市分级 (基于综合实力, 非单纯GDP)
# 来源: https://www.10guoying.com/city/10917.html
# ============================================================
GYBRAND_TIERS = {
    # 一线城市 (Top 4)
    "S": ["上海", "北京", "深圳", "广州"],
    # 新一线 (准一线, rank 5-20)
    "1": ["杭州", "成都", "南京", "武汉", "重庆", "天津", "长沙", "青岛",
          "宁波", "无锡", "西安", "郑州", "合肥", "厦门"],
    # 二线 (rank 21-50)
    "2": ["济南", "福州", "昆明", "大连", "沈阳", "哈尔滨", "南昌", "泉州",
          "烟台", "温州", "贵阳", "南宁", "石家庄", "长春", "太原",
          "金华", "徐州", "惠州", "扬州", "乌鲁木齐", "兰州"],
    # 三线 (rank 51-100 或未在百强但属70城)
    "3": ["呼和浩特", "洛阳", "唐山", "海口", "宜昌", "银川", "济宁",
          "襄阳", "遵义", "赣州", "九江", "桂林", "湛江", "岳阳", "常德",
          # 以下不在GYBrand百强但属国家统计局70城
          "三亚", "丹东", "包头", "北海", "南充", "吉林", "大理",
          "安庆", "平顶山", "泸州", "牡丹江", "秦皇岛", "蚌埠",
          "锦州", "韶关", "西宁"],
}

# 构建城市→等级映射
CITY_TIER_MAP = {}
for tier, cities in GYBRAND_TIERS.items():
    for city in cities:
        CITY_TIER_MAP[city] = tier

TIER_COLORS = {"S": "#FF4B4B", "1": "#FF9F43", "2": "#54A0FF", "3": "#5F27CD"}
TIER_NAMES = {"S": "一线城市", "1": "新一线", "2": "二线城市", "3": "三线城市"}

# ============================================================
# 基准价格参考 (2021年4月)
# 旧版参考常数；原始校准依据缺失，未核验
# 仅供旧固定锚点模型对照；发布价格使用独立的观测凭据与指数链。
# ============================================================
BASE_PRICE_REF_2021Q1 = {
    # ===== 一线 S =====
    "北京": 62516, "上海": 53858, "深圳": 58303, "广州": 38051,

    # ===== 新一线 1 =====
    "杭州": 36570, "南京": 24679, "武汉": 16277, "成都": 16898,
    "重庆": 12085, "天津": 16495, "长沙": 10425, "青岛": 17606,
    "宁波": 23987, "无锡": 15960, "西安": 16015, "郑州": 12397,
    "合肥": 17343, "厦门": 48516,

    # ===== 二线 2 =====
    "济南": 16297, "福州": 27456, "昆明": 10297, "大连": 12158,
    "沈阳": 9093, "哈尔滨": 8854, "南昌": 11154, "泉州": 11382,
    "烟台": 10131,
    "温州": 17500,
    "贵阳": 9058, "南宁": 10979, "石家庄": 12907,
    "长春": 8404,
    "太原": 10160, "金华": 14752,
    "徐州": 10259, "惠州": 10453, "扬州": 13364,
    "乌鲁木齐": 8345, "兰州": 11725,

    # ===== 三线 3 =====
    "三亚": 22000,
    "唐山": 8911,
    "秦皇岛": 8500,
    "牡丹江": 4888,
    "丹东": 5537,
    "吉林": 5428,
    "锦州": 4562,
    "呼和浩特": 10328,
    "洛阳": 8398,
    "海口": 15550,
    "宜昌": 7839,
    "银川": 7240,
    "济宁": 7215,
    "襄阳": 8174,
    "遵义": 5888,
    "赣州": 8438,
    "九江": 7450,
    "桂林": 6420,
    "湛江": 7330,
    "岳阳": 6828,
    "常德": 5968,
    "大理": 11580,
    "包头": 6810,
    "北海": 6880,
    "南充": 6354,
    "安庆": 7142,
    "平顶山": 5371,
    "泸州": 6528,
    "蚌埠": 7318,
    "韶关": 5989,
    "西宁": 8503,
}

# 城市名映射(用于前端显示)
CITY_NAME_MAP = {city: city for city in BASE_PRICE_REF_2021Q1}

# 旧模型的历史质量标签，不构成已核验观测或成交价证明。
# 发布数据质量由 price_provenance 的原始证据校验决定。
DATA_QUALITY = {
    "三亚": "good",
    "唐山": "good",
    "秦皇岛": "good",
    "温州": "good",
    "牡丹江": "good",
    "丹东": "good",
    "济南": "good",
    "长春": "good",
    "锦州": "uncertain",
    "韶关": "uncertain",
    "平顶山": "uncertain",
    "吉林": "uncertain",
}
# 默认: cities not listed = "ok"


def load_intersection_cities():
    if INTERSECTION_FILE.exists():
        with open(INTERSECTION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data["cities"]
    return []


def load_stats_mom_data():
    """Load official second-hand totals only; area bands cannot replace weighted totals."""
    mom_data = {}

    if not STATS_DIR.exists():
        return mom_data

    for f in STATS_DIR.glob("*.json"):
        with open(f, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
            if isinstance(data, list):
                for row in data:
                    mom_val = row.get("second_hand_mom")
                    if mom_val is None:
                        continue
                    city = row.get("city")
                    rm = row.get("report_month", "")
                    at = row.get("area_type", "")
                    try:
                        parts = rm.split("-")
                        y, m = int(parts[0]), int(parts[1])
                        key = (city, y, m)
                        value = float(mom_val)
                        if not math.isfinite(value) or not 50 <= value <= 150 or not 1 <= m <= 12:
                            continue
                        if (y, m) >= (date.today().year, date.today().month):
                            continue
                        if at == "合计" and row.get('table_type') == 'second_hand':
                            mom_data[key] = float(mom_val)
                    except:
                        continue

    print(f"[stats] {len(mom_data)} 条二手住宅合计环比(不使用未加权面积均值), "
          f"{len(set(c for c,_,_ in mom_data))} 城市")
    return mom_data


class PriceInputs(dict):
    def __init__(self):
        super().__init__()
        self.records={}


def load_anjuke_data():
    """加载安居客价格数据"""
    from engine_policy import observed_verified
    anjuke_data = PriceInputs()
    if not ANJUKE_DIR.exists():
        return anjuke_data

    for f in ANJUKE_DIR.glob("*.json"):
        with open(f, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
            if isinstance(data, list):
                for row in data:
                    city = row.get('city')
                    # Keep unsupported legacy values on disk, but never use them as observed anchors.
                    if listing_evidence(row)['status']!='traceable_listing':
                        continue
                    year, month, price = row.get('year'), row.get('month'), row.get('avg_price')
                    if (city and isinstance(price, (int, float)) and math.isfinite(price)
                            and price > 0 and year and month and 1 <= int(month) <= 12
                            and (int(year), int(month)) < (date.today().year, date.today().month)):
                        if not observed_verified(city,(int(year),int(month)),price,row):
                            continue
                        anjuke_data[(city, int(year), int(month))] = int(price)
                        anjuke_data.records[(city,int(year),int(month))]=row

    print(f"[anjuke] {len(anjuke_data)} 条价格数据")
    return anjuke_data


def derive_from_base_price(city, mom_data, base_year=2021, base_month=4):
    """基准价格推导策略 (用于无Anjuke数据的城市)

    从2021-04基准价格出发，用统计局环比逐月推导。
    推导范围: 2021-04 → 最新月份
    数据质量标注基于DATA_QUALITY表。
    """
    base_price = BASE_PRICE_REF_2021Q1.get(city)
    if not base_price:
        return {}

    city_mom = {}
    for (c, y, m), val in mom_data.items():
        if c == city:
            city_mom[(y, m)] = val

    all_mom_months = sorted(city_mom.keys())
    if not all_mom_months:
        return {}

    anchor_ym = (base_year, base_month)

    results = {}
    for target in sorted(set(all_mom_months)|{anchor_ym}):
        value=propagate_price(base_price,anchor_ym,target,city_mom)
        if value is not None:
            results[target]=dict(price=value,source='reference_estimate',quality='uncertain',
                method='reference_estimate',anchor_date='%04d-%02d'%anchor_ym,
                chain_months=abs(month_number(target)-month_number(anchor_ym)))

    return results


def month_number(ym):
    return ym[0] * 12 + ym[1] - 1


def propagate_price(price, anchor, target, city_mom):
    """Only complete calendar-month chains can produce a price."""
    if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:
        return None
    start, end = month_number(anchor), month_number(target)
    for n in range(min(start, end) + 1, max(start, end) + 1):
        index = city_mom.get((n // 12, n % 12 + 1))
        if isinstance(index,bool) or not isinstance(index,(int,float)) or not math.isfinite(index) or not 50 <= index <= 150:
            return None
        price = price * index / 100 if end > start else price / (index / 100)
    return round(price) if math.isfinite(price) and price>0 else None


def derive_from_nearest_anchor(city, anjuke_data, mom_data):
    anchors = {(y, m): p for (c, y, m), p in anjuke_data.items() if c == city}
    indices = {(y, m): v for (c, y, m), v in mom_data.items() if c == city}
    results = {}
    for target in sorted(set(anchors) | set(indices)):
        if target in anchors:
            results[target] = {"price": anchors[target], "source": "anjuke",
                               "quality": "ok", "method": "listing_observation",
                               "anchor_date": "%04d-%02d" % target, "chain_months": 0}
            continue
        # Prefer a prior observation throughout a missing interval. Do not switch to a future
        # observation halfway through a gap merely because its calendar distance is shorter.
        candidates = sorted(anchors, key=lambda a: (a>target,abs(month_number(a)-month_number(target)),a))
        for anchor in candidates:
            distance = abs(month_number(anchor) - month_number(target))
            if distance > 12:
                continue
            price = propagate_price(anchors[anchor], anchor, target, indices)
            if price is not None:
                results[target] = {"price": price, "source": "国家统计局",
                                   "quality": "ok" if distance <= 3 else "uncertain",
                                   "method": "index_estimate", "anchor_date": "%04d-%02d" % anchor,
                                   "chain_months": distance}
                break
    records=getattr(anjuke_data,'records',{})
    for (y,m),point in results.items():
        ay,am=map(int,point['anchor_date'].split('-'))
        apply_provenance(point,records.get((city,y,m),{}),records.get((city,ay,am),{}))
    return results




def build_frontend_data(intersection_cities, all_derived, mom_data, anjuke_data):
    """构建前端数据"""
    city_stats = {}
    for city_info in intersection_cities:
        city = city_info["city"]
        info = city_info

        derived = all_derived.get(city, {})
        if not derived or len(derived) < 6:
            print(f"  [skip] {city}: 数据不足({len(derived)}条)")
            continue

        sorted_ym = sorted(derived.keys())
        sorted_prices = [derived[ym]["price"] for ym in sorted_ym]
        sorted_sources = [derived[ym]["source"] for ym in sorted_ym]
        sorted_quality = [derived[ym].get("quality", "ok") for ym in sorted_ym]

        valid_prices = [p for p in sorted_prices if isinstance(p,(int,float)) and p>0]
        max_price = max(valid_prices) if valid_prices else None
        min_price = min(valid_prices) if valid_prices else None
        peak_ym = sorted_ym[sorted_prices.index(max_price)] if valid_prices else None
        latest_price = sorted_prices[-1]
        drop_from_peak = round((latest_price - max_price) / max_price * 100, 1) if latest_price and max_price else None

        previous_year = derived.get((sorted_ym[-1][0] - 1, sorted_ym[-1][1]))
        if latest_price and previous_year and previous_year.get('price'):
            change_12m = round(latest_price - previous_year["price"])
            change_pct_12m = round(change_12m / previous_year["price"] * 100, 1)
        else:
            change_12m = None
            change_pct_12m = None

        anjuke_count = sum(1 for s in sorted_sources if s == "anjuke")
        stats_count = sum(1 for s in sorted_sources if s in ("国家统计局","index_estimate","reference_estimate"))

        # source_type: anjuke/mixed/国家统计局
        source_type = sorted_sources[-1]

        # 整体数据质量
        worst_quality = "good"
        for q in sorted_quality:
            if q == 'unverified':
                worst_quality = 'unverified'
                break
            if q == "uncertain":
                worst_quality = "uncertain"
            elif q == "ok" and worst_quality == "good":
                worst_quality = "ok"

        monthly = []
        for i, ym in enumerate(sorted_ym):
            price = sorted_prices[i]
            source = sorted_sources[i]
            quality = sorted_quality[i]

            change_pct = None
            if i > 0 and month_number(ym) - month_number(sorted_ym[i - 1]) == 1:
                prev = sorted_prices[i - 1]
                if price is not None and prev is not None and prev > 0:
                    change_pct = round((price - prev) / prev * 100, 2)

            monthly.append({
                "date": f"{ym[0]}-{ym[1]:02d}",
                "price": int(price) if price is not None else None,
                "change_pct": change_pct,
                "source": source,
                "quality": quality,
                "method": derived[ym].get("method", "reference_estimate"),
                "anchor_date": derived[ym].get("anchor_date", "2021-04"),
                "chain_months": derived[ym].get("chain_months", abs(month_number(ym) - month_number((2021, 4)))),
                "evidence": derived[ym].get('evidence',{}),
                "observed_price": derived[ym].get('observed_price'),
                "validation_id": derived[ym].get('validation_id'),
                "validation_mape_pct": derived[ym].get('validation_mape_pct'),
                "model_comparison": derived[ym].get('model_comparison'),
            })

        city_stats[city] = {
            "city": city,
            "map_name": CITY_NAME_MAP.get(city, city),
            "tier": info["tier"],
            "tier_name": info["tier_name"],
            "color": info["color"],
            "lat": info["lat"],
            "lon": info["lon"],
            "current_price": int(latest_price) if latest_price is not None else None,
            "max_price": max_price,
            "min_price": min_price,
            "peak_date": f"{peak_ym[0]}-{peak_ym[1]:02d}" if peak_ym else None,
            "peak_year": peak_ym[0] if peak_ym else None,
            "price_range": int(max_price - min_price) if max_price is not None else None,
            "drop_from_peak": drop_from_peak,
            "change_12m": change_12m,
            "change_pct_12m": change_pct_12m,
            "source_type": source_type,
            "anjuke_count": anjuke_count,
            "stats_count": stats_count,
            "data_count": len(sorted_ym),
            "data_quality": worst_quality,
            "monthly": monthly,
        }

    tier_order = {"S": 0, "1": 1, "2": 2, "3": 3}
    city_list = sorted(
        city_stats.values(),
        key=lambda x: (tier_order.get(x["tier"], 9), -(x["current_price"] or 0))
    )

    tier_avg = {}
    for tier in ["S", "1", "2", "3"]:
        prices = [
            c["monthly"][-1]["price"]
            for c in city_list if c["tier"] == tier and c['current_price'] is not None
        ]
        if prices:
            tier_avg[tier] = round(sum(prices) / len(prices))

    tier_count = {}
    for tier in ["S", "1", "2", "3"]:
        tier_count[tier] = sum(1 for c in city_list if c["tier"] == tier)

    # 数据截止月份动态计算(不再硬编码)
    all_ym = [(y, m) for (_, y, m) in mom_data.keys()] + \
             [(y, m) for (_, y, m) in anjuke_data.keys()]
    if all_ym:
        latest_ym = max(all_ym)
        data_updated = f"{latest_ym[0]}-{latest_ym[1]:02d}"
    else:
        data_updated = "2026-06"

    return {
        "meta": {
            "total_cities": len(city_list),
            "data_updated": data_updated,
            "tier_mean_prices": tier_avg,
            "tier_counts": tier_count,
            "tier_names": TIER_NAMES,
            "intersection_count": len(intersection_cities),
            "method": "原始快照核对通过的安居客当月观测优先；缺月才按近距离锚点推导；固定旧锚点模型只作对照。非成交均价。",
            "tier_source": "GYBrand 2025城市综合实力排行榜",
            "data_quality": "公开挂牌参考和指数推导；无核验锚点不发布绝对价格，不代表成交核验",
        },
        "cities": city_list,
        "tier_colors": TIER_COLORS,
    }


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    print("=" * 60)
    print("价格引擎 v6 — 原文核对观测优先，完整环比链补缺")
    print("=" * 60)

    intersection_cities = load_intersection_cities()
    print(f"\n[交集] {len(intersection_cities)} 城市")

    # 检查并修正城市等级(确保使用GYBrand分级)
    for ci in intersection_cities:
        city = ci["city"]
        gybrand_tier = CITY_TIER_MAP.get(city)
        if gybrand_tier:
            ci["tier"] = gybrand_tier
            ci["tier_name"] = TIER_NAMES.get(gybrand_tier, "未知")
            ci["color"] = TIER_COLORS.get(gybrand_tier, "#999")
        else:
            print(f"  [warn] {city} 不在GYBrand分级表中, 使用原分级 {ci.get('tier', '?')}")

    mom_data = load_stats_mom_data()
    anjuke_data = load_anjuke_data()
    from engine_policy import load_policy, overlay
    policy = load_policy(anjuke_data, mom_data)

    cities_with_anjuke = set()
    for (c, y, m), price in anjuke_data.items():
        cities_with_anjuke.add(c)
    print(f"[anjuke] 有直接数据的城市: {len(cities_with_anjuke)} 个")
    print(f"[quality] good={sum(1 for v in DATA_QUALITY.values() if v=='good')} "
          f"uncertain={sum(1 for v in DATA_QUALITY.values() if v=='uncertain')}")

    all_derived = {}
    anjuke_city_count = 0
    reference_city_count = 0

    for city_info in intersection_cities:
        city = city_info["city"]
        print(f"\n--- {city} [{city_info['tier_name']}] ---")

        # Keep every official city/month visible without inventing an absolute price.
        derived = {(y,m):dict(price=None,source='unavailable',method='unavailable',quality='unverified',
                    anchor_date=None,chain_months=None) for c,y,m in mom_data if c==city}
        if city in cities_with_anjuke:
            derived.update(derive_from_nearest_anchor(city, anjuke_data, mom_data))
            anjuke_city_count += 1
            strategy = "anchor"
        else:
            reference_city_count += 1
            strategy = "missing_verified_anchor"

        overlay(city, derived, policy, anjuke_data)
        all_derived[city] = derived

        anjuke_direct = sum(1 for v in derived.values() if v["source"] == "anjuke")
        stats_fill = sum(1 for v in derived.values() if v.get('method') == 'index_estimate')
        quality_set = set(v.get("quality", "ok") for v in derived.values())

        print(f"  [strategy] {strategy} | 质量: {','.join(sorted(quality_set))}")
        print(f"  [result] {len(derived)} 条: 安={anjuke_direct}, 统={stats_fill}")

        if derived:
            latest = max(derived.keys())
            print(f"  [range] {min(derived.keys())} → {latest}")
            print(f"  [latest] {latest[0]}-{latest[1]:02d}: "
                  f"{derived[latest]['price']}元 ({derived[latest]['source']})")

    print(f"\n[策略统计] 观测锚点: {anjuke_city_count}城, 无可核验锚点: {reference_city_count}城")

    result = build_frontend_data(intersection_cities, all_derived, mom_data, anjuke_data)
    result['meta']['engine_validation'] = dict(passed_cities=sum(r['status']=='passed' for r in policy['cities'].values()), horizon=policy.get('horizon'), input_fingerprint=policy.get('input_fingerprint'),publication_policy='observed_first',model_use='comparison_only')
    from data_quality import audit
    audit(result,indices=mom_data)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "intersection_housing_data.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"v5 最终结果:")
    print(f"  城市数: {result['meta']['total_cities']}")
    print(f"  分级来源: {result['meta']['tier_source']}")
    print(f"  等级分布: S={result['meta']['tier_counts'].get('S',0)} "
          f"新一线={result['meta']['tier_counts'].get('1',0)} "
          f"二线={result['meta']['tier_counts'].get('2',0)} "
          f"三线={result['meta']['tier_counts'].get('3',0)}")
    print(f"  等级均价: S={result['meta']['tier_mean_prices'].get('S','?')} "
          f"新一线={result['meta']['tier_mean_prices'].get('1','?')} "
          f"二线={result['meta']['tier_mean_prices'].get('2','?')} "
          f"三线={result['meta']['tier_mean_prices'].get('3','?')}")
    print(f"  保存: {output_path}")

    # 打印关键城市对比
    print(f"\n[价格对比] 修正前→修正后 (v4→v5):")
    for c in result["cities"]:
        if c["data_quality"] == "good" and c["source_type"] == "国家统计局":
            print(f"  {c['city']} [{c['tier_name']}]: {c['current_price']}元 "
                  f"(quality={c['data_quality']}, {c['data_count']}条)")


if __name__ == "__main__":
    main()
