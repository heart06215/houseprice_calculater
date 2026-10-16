# -*- coding: utf-8 -*-
"""
城市区划边界数据生成器
======================
为楼盘地图(realestate)的 Leaflet 城市视图生成区级分界线数据(如 海淀区/黄埔区)。

做的事:
1. 给 frontend/realestate/data/cities.json 每个城市补充 "adcode" 字段
   (阿里 DataV 行政区划代码, 区界数据的唯一来源标识)
2. 从阿里 DataV GeoAtlas 下载每个城市的区级边界 GeoJSON,
   精简后存到 frontend/realestate/data/districts/{城市名}.json

前端 city.js 优先读本地 districts/ 文件, 缺失时自动回退 DataV 在线接口,
因此本脚本只需在新增城市后跑一次; 不跑也不影响(在线回退)。

用法:
  python generate_district_boundaries.py            # 全部城市
  python generate_district_boundaries.py --city 北京  # 指定城市
"""

import argparse
import io
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REALESTATE_DATA = PROJECT_ROOT / "data_processing" / "frontend" / "realestate" / "data"
CITIES_FILE = REALESTATE_DATA / "cities.json"
DISTRICTS_DIR = REALESTATE_DATA / "districts"

DATAV_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 城市行政区划代码(地级市/直辖市)
CITY_ADCODE = {
    "北京": 110000, "上海": 310000, "天津": 120000, "重庆": 500000,
    "广州": 440100, "深圳": 440300, "成都": 510100, "杭州": 330100,
    "武汉": 420100, "南京": 320100, "苏州": 320500, "西安": 610100,
    "长沙": 430100, "郑州": 410100, "合肥": 340100, "福州": 350100,
    "济南": 370100, "昆明": 530100, "沈阳": 210100, "南宁": 450100,
    "青岛": 370200, "大连": 210200, "厦门": 350200, "宁波": 330200,
    "无锡": 320200,
}


def http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,*/*",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def slim_geojson(gj):
    """只保留 name 属性, 大幅减小文件体积。"""
    if not isinstance(gj, dict) or gj.get("type") != "FeatureCollection":
        raise ValueError("返回数据不是 FeatureCollection")
    feats = []
    for f in gj.get("features", []):
        props = f.get("properties") or {}
        feats.append({
            "type": "Feature",
            "properties": {"name": props.get("name") or props.get("NAME") or ""},
            "geometry": f.get("geometry"),
        })
    return {"type": "FeatureCollection", "features": feats}


def main():
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="城市区划边界数据生成器")
    parser.add_argument("--city", default="", help="只处理指定城市(默认全部)")
    args = parser.parse_args()

    cities = json.loads(CITIES_FILE.read_text(encoding="utf-8"))
    changed = False
    for name, cfg in cities.items():
        if name in CITY_ADCODE and cfg.get("adcode") != CITY_ADCODE[name]:
            cfg["adcode"] = CITY_ADCODE[name]
            changed = True
        elif name not in CITY_ADCODE and "adcode" not in cfg:
            print(f"[WARN] {name} 无 adcode 映射, 跳过(需在 CITY_ADCODE 中补充)")
    if changed:
        CITIES_FILE.write_text(json.dumps(cities, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        print("[OK] cities.json 已补充 adcode 字段")

    DISTRICTS_DIR.mkdir(parents=True, exist_ok=True)
    targets = [args.city] if args.city else list(cities.keys())
    ok, fail = 0, 0
    for name in targets:
        cfg = cities.get(name) or {}
        adcode = cfg.get("adcode")
        if not adcode:
            print(f"[WARN] {name} 无 adcode, 跳过")
            fail += 1
            continue
        try:
            gj = slim_geojson(http_get_json(DATAV_URL.format(adcode=adcode)))
            n = len(gj["features"])
            out = DISTRICTS_DIR / f"{name}.json"
            out.write_text(json.dumps(gj, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            size_kb = out.stat().st_size // 1024
            sample = ", ".join((f["properties"]["name"] for f in gj["features"][:3]))
            print(f"[OK] {name}({adcode}): {n} 个区县, {size_kb}KB — {sample}...")
            ok += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            fail += 1

    print(f"\n完成: 成功 {ok}, 失败 {fail}; 输出目录 {DISTRICTS_DIR}")


if __name__ == "__main__":
    main()
