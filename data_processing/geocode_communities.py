# -*- coding: utf-8 -*-
"""
楼盘地理编码器(Photon/OSM, 免密钥)
====================================
把"城市中心估算"精度的楼盘通过 Photon(基于 OSM)地理编码到真实坐标。

设计:
- 查询: q="{城市} {楼盘名}", 取前8个候选, 双重校验后才采用:
    ① 候选的城市/州/县名(去"市/省"后缀)与楼盘城市一致
    ② 候选坐标落在楼盘城市中心 ±0.6° 范围内
  未通过校验的楼盘保持原精度(city), 由前端继续用聚合兜底展示。
- 坐标系: Photon 返回 WGS84, 高德瓦片底图需要 GCJ-02, 命中后做标准偏移转换。
- 缓存: output/geocode_cache.json 以 "城市|楼盘名" 为键, 命中与未命中都缓存,
  重复运行零查询; normalize_properties ④ 会把缓存应用到新数据。

用法:
  python geocode_communities.py            # 编码所有 city 精度楼盘
  python geocode_communities.py --limit 50 # 只处理前50个(测试用)
"""

import argparse
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROPS_FILE = PROJECT_ROOT / "data_processing" / "frontend" / "realestate" / "data" / "properties.json"
CACHE_FILE = PROJECT_ROOT / "output" / "geocode_cache.json"

PHOTON = "https://photon.komoot.io/api/?q={q}&limit=8&lang=default"
UA = "houseprice-calculator/1.0 (local demo project)"
QUERY_INTERVAL = 1.1  # Photon 礼貌限速
CITY_BBOX_DEG = 0.6   # 城市中心容差(度)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.cities import CITY_CENTERS  # noqa: E402
EXTRA_CENTERS = {
    "济南": (36.651, 117.001), "沈阳": (41.808, 123.432), "南宁": (22.817, 108.366),
    "宁波": (29.868, 121.544), "无锡": (31.491, 120.312),
}


def http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def norm_city(name):
    """OSM 城市名(北京市/广州市) → 平台城市名(北京/广州)。"""
    if not name:
        return ""
    return name.rstrip("市省自治区特别行政区") or name


def wgs84_to_gcj02(lat, lng):
    """WGS84 → GCJ-02(国测局火星坐标, 高德底图坐标系)。中国境外原样返回。"""
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lat, lng
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def pick_candidate(features, city, center):
    """双重校验(城市名一致 + 坐标在城市中心附近), 返回 (lat, lng) 或 None。"""
    for f in features:
        pr = f.get("properties", {})
        names = {norm_city(pr.get(k)) for k in ("city", "state", "county", "district")}
        geo = f.get("geometry", {}).get("coordinates")
        if not geo:
            continue
        lng, lat = float(geo[0]), float(geo[1])
        near = abs(lat - center[0]) <= CITY_BBOX_DEG and abs(lng - center[1]) <= CITY_BBOX_DEG
        if city in names and near:
            return lat, lng
    return None


def load_properties():
    return json.loads(PROPS_FILE.read_text(encoding="utf-8"))


def main():
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="楼盘地理编码器(Photon/OSM)")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个(0=全部)")
    args = parser.parse_args()

    props = load_properties()
    targets = [p for p in props
               if p.get("precision") == "city" and (p.get("community") or p.get("name"))]
    if args.limit:
        targets = targets[:args.limit]
    print(f"待地理编码: {len(targets)} 个 city 精度楼盘")

    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    hit = miss = 0
    try:
        for i, p in enumerate(targets, 1):
            city = p["city"]
            name = p.get("community") or p.get("name")
            key = f"{city}|{name}"
            center = CITY_CENTERS.get(city) or EXTRA_CENTERS.get(city)
            if not center:
                continue
            if key in cache:
                continue  # 已编码过(含未命中缓存)
            try:
                d = http_get_json(PHOTON.format(q=urllib.parse.quote(f"{city} {name}")))
                feats = d.get("features", [])
                res = pick_candidate(feats, city, center)
            except Exception as e:
                print(f"  [{i}/{len(targets)}] {key}: 请求失败 {str(e)[:60]}")
                time.sleep(3)
                continue
            if res:
                lat, lng = wgs84_to_gcj02(res[0], res[1])
                cache[key] = {"lat": round(lat, 6), "lng": round(lng, 6)}
                hit += 1
                print(f"  [{i}/{len(targets)}] ✓ {key} → ({lat:.4f}, {lng:.4f})")
            else:
                cache[key] = None
                miss += 1
            time.sleep(QUERY_INTERVAL)
    finally:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n缓存已保存: {CACHE_FILE.name} (命中 {hit}, 未命中 {miss}, 共 {len(cache)} 条)")
        print("运行 pipeline.py --skip-crawl 将缓存坐标应用到 properties.json")


if __name__ == "__main__":
    main()
