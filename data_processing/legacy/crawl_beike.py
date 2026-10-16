# -*- coding: utf-8 -*-
"""
贝壳找房爬虫 - 二手房 + 新房楼盘数据采集
使用 requests + BeautifulSoup，无需浏览器
云端研究输出: output/beike/properties_beike.json
"""

import requests
import json
import os
import sys
import time
import random
import re
import hashlib
from pathlib import Path
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from config.cities import DISTRICT_COORDS, CITY_CENTERS, guess_district
from data_store import atomic_json

# ===================== 配置 =====================

# 抓取城市
ERSHOUFANG_CITIES = {
    "北京": {"code": "bj", "pages": 5},
    "上海": {"code": "sh", "pages": 5},
    "广州": {"code": "gz", "pages": 5},
    "深圳": {"code": "sz", "pages": 5},
    "成都": {"code": "cd", "pages": 4},
    "杭州": {"code": "hz", "pages": 4},
    "武汉": {"code": "wh", "pages": 4},
    "南京": {"code": "nj", "pages": 4},
    "天津": {"code": "tj", "pages": 3},
    "苏州": {"code": "su", "pages": 3},
    "西安": {"code": "xa", "pages": 3},
    "重庆": {"code": "cq", "pages": 3},
    "长沙": {"code": "cs", "pages": 2},
    "青岛": {"code": "qd", "pages": 2},
    "郑州": {"code": "zz", "pages": 2},
    "大连": {"code": "dl", "pages": 2},
    "厦门": {"code": "xm", "pages": 2},
    "合肥": {"code": "hf", "pages": 2},
    "福州": {"code": "fz", "pages": 2},
    "昆明": {"code": "km", "pages": 2},
}

LOUPAN_CITIES = {
    "上海": {"code": "sh", "pages": 3},
    "北京": {"code": "bj", "pages": 3},
    "广州": {"code": "gz", "pages": 3},
    "深圳": {"code": "sz", "pages": 3},
    "成都": {"code": "cd", "pages": 3},
    "杭州": {"code": "hz", "pages": 3},
    "武汉": {"code": "wh", "pages": 2},
    "南京": {"code": "nj", "pages": 2},
    "苏州": {"code": "su", "pages": 2},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

OUTPUT_DIR = str(BASE.parent / 'output' / 'beike')
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "properties_beike.json")

# ===================== 坐标近似 =====================
# DISTRICT_COORDS / CITY_CENTERS 统一维护在 data_processing/config/cities.py


def _stable_jitter(seed, span_deg):
    """基于 seed 的确定性抖动，保证重爬时坐标可复现。"""
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    lat_j = ((h >> 8) % 1000 / 1000 - 0.5) * span_deg
    lng_j = ((h >> 20) % 1000 / 1000 - 0.5) * span_deg
    return lat_j, lng_j


def get_coords(city, district="", name=""):
    """获取近似坐标，返回 (lat, lng, precision)。

    precision: district=区县级估算 / city=城市级估算。
    """
    city_districts = DISTRICT_COORDS.get(city, {})
    dkey = ""
    if district:
        for dname in city_districts:
            if dname in district or district in dname:
                dkey = dname
                break
    elif name:
        dkey = guess_district(city, name)
    if dkey:
        lat, lng = city_districts[dkey]
        lat_j, lng_j = _stable_jitter(f"{city}{dkey}{name}", 0.016)
        return round(lat + lat_j, 6), round(lng + lng_j, 6), "district"
    lat, lng = CITY_CENTERS.get(city, (39.9, 116.4))
    return round(lat, 6), round(lng, 6), "city"


def random_delay():
    time.sleep(random.uniform(1.0, 2.5))


# ===================== 二手房爬取 =====================

def crawl_ershoufang(city, code, pages):
    """爬取贝壳二手房"""
    results = []
    print(f"\n  [HOUSE] {city} 二手房 (bj.ke.com/{code}/) - 目标{40*pages}条")

    for pg in range(1, pages + 1):
        if pg == 1:
            url = f"https://{code}.ke.com/ershoufang/"
        else:
            url = f"https://{code}.ke.com/ershoufang/pg{pg}/"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"    [WARN] pg{pg}: HTTP {resp.status_code}, skip")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".sellListContent li")
            if not items:
                items = soup.select("ul.sellListContent li")
            if not items:
                items = soup.select(".content__list li")

            city_list = []
            for li in items:
                try:
                    title_el = li.select_one(".title a")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)

                    # 提取小区名和户型
                    name_match = re.match(r'^(.+?) (\d+室\d+厅)', title)
                    if name_match:
                        name = name_match.group(1)
                        layout = name_match.group(2)
                    else:
                        name = title.split()[0] if title.split() else title
                        layout = ""

                    # 房屋信息: 楼层/年代/户型/面积/朝向
                    house_info = li.select_one(".houseInfo")
                    info_text = house_info.get_text(strip=True) if house_info else ""
                    parts = info_text.split("|")

                    area_val = 0
                    orientation = ""
                    floor_info = ""
                    for p in parts:
                        p = p.strip()
                        if "平米" in p or "㎡" in p or "m²" in p:
                            m = re.search(r'([\d.]+)', p)
                            if m:
                                area_val = float(m.group(1))
                        elif p in ["东", "南", "西", "北", "东南", "东北", "西南", "西北", "南北"]:
                            orientation = p
                        elif "楼层" in p or "层" in p:
                            floor_info = p

                    # 地址: 小区名 | 区域
                    addr_el = li.select_one(".positionInfo, .address")
                    addr_text = addr_el.get_text(strip=True) if addr_el else ""
                    district = ""
                    for a_tag in (addr_el.find_all("a") if addr_el else []):
                        txt = a_tag.get_text(strip=True)
                        if txt and "区" in txt:
                            district = txt
                            break

                    # 总价
                    total_price = 0
                    price_el = li.select_one(".totalPrice span, .priceInfo .total")
                    if price_el:
                        m = re.search(r'([\d.]+)', price_el.get_text(strip=True))
                        if m:
                            total_price = float(m.group(1))

                    # 单价
                    unit_price = 0
                    unit_el = li.select_one(".unitPrice span, .unitPrice")
                    if unit_el:
                        m = re.search(r'([\d,]+)', unit_el.get_text(strip=True))
                        if m:
                            unit_price = float(m.group(1).replace(",", ""))

                    # 标签
                    tags = []
                    tag_els = li.select(".tag span, .houseTag span")
                    for t in tag_els:
                        txt = t.get_text(strip=True)
                        if txt:
                            tags.append(txt)

                    if not name or not area_val:
                        continue

                    lat, lng, precision = get_coords(city, district, name)

                    prop = {
                        "name": name,
                        "city": city,
                        "district": district,
                        "type": "二手房",
                        "price_total": total_price,
                        "price_unit": unit_price,
                        "area": area_val,
                        "layout": layout,
                        "orientation": orientation,
                        "floor": floor_info,
                        "latitude": round(lat, 6),
                        "longitude": round(lng, 6),
                        "precision": precision,
                        "tags": tags,
                        "source": "beike",
                    }
                    city_list.append(prop)

                except Exception:
                    continue

            results.extend(city_list)
            print(f"    pg{pg}: {len(city_list)}条", end="")
            random_delay()

        except requests.RequestException as e:
            print(f"    [WARN] pg{pg}: {e}")
            continue

    print(f" → 共{len(results)}条")
    return results


# ===================== 新房爬取 =====================

def crawl_loupan(city, code, pages):
    """爬取贝壳新房楼盘"""
    results = []
    print(f"\n  [{city}] 新房 {code}.fang.ke.com/loupan/ - 目标{10*pages}条")

    for pg in range(1, pages + 1):
        if pg == 1:
            url = f"https://{code}.fang.ke.com/loupan/"
        else:
            url = f"https://{code}.fang.ke.com/loupan/pg{pg}/"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"    [WARN] pg{pg}: HTTP {resp.status_code}, skip")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".resblock-list-wrapper li, .resblock-list li")

            city_list = []
            for li in items:
                try:
                    name_el = li.select_one(".resblock-name .name, .name a")
                    if not name_el:
                        name_el = li.select_one(".resblock-name a")
                    if not name_el:
                        continue
                    name = name_el.get_text(strip=True)

                    # 类型
                    prop_type = "新房"
                    type_el = li.select_one(".resblock-type span")
                    if type_el:
                        t = type_el.get_text(strip=True)
                        if "别墅" in t:
                            prop_type = "别墅"
                        elif "住宅" in t:
                            prop_type = "新房"

                    # 地址
                    addr_el = li.select_one(".resblock-location span, .resblock-location")
                    addr_text = addr_el.get_text(strip=True) if addr_el else ""
                    district = ""
                    for part in addr_text.split("/"):
                        part = part.strip()
                        if "区" in part and len(part) <= 4:
                            district = part
                            break

                    # 户型
                    layout = ""
                    layout_el = li.select_one(".resblock-room span")
                    if layout_el:
                        layout = layout_el.get_text(strip=True)

                    # 面积
                    area_val = 0
                    area_el = li.select_one(".resblock-area span")
                    if area_el:
                        area_text = area_el.get_text(strip=True)
                        m = re.search(r'(\d+)-(\d+)', area_text)
                        if m:
                            area_val = (float(m.group(1)) + float(m.group(2))) / 2
                        else:
                            m = re.search(r'([\d.]+)', area_text)
                            if m:
                                area_val = float(m.group(1))

                    # 均价
                    unit_price = 0
                    price_el = li.select_one(".resblock-price .number, .main-price .number")
                    if price_el:
                        m = re.search(r'([\d,]+)', price_el.get_text(strip=True))
                        if m:
                            unit_price = float(m.group(1).replace(",", ""))

                    # 总价
                    total_price = 0
                    total_el = li.select_one(".resblock-price .second, .second-price")
                    if total_el:
                        total_text = total_el.get_text(strip=True)
                        m = re.search(r'(\d+)-(\d+)', total_text)
                        if m:
                            total_price = (float(m.group(1)) + float(m.group(2))) / 2
                        else:
                            m = re.search(r'([\d.]+)', total_text)
                            if m:
                                total_price = float(m.group(1))

                    if unit_price:
                        total_price = total_price or unit_price * (area_val or 90) / 10000

                    # 标签
                    tags = []
                    tag_els = li.select(".resblock-tag span, .tag-item")
                    for t in tag_els:
                        txt = t.get_text(strip=True)
                        if txt:
                            tags.append(txt)

                    lat, lng, precision = get_coords(city, district, name)

                    prop = {
                        "name": name,
                        "city": city,
                        "district": district,
                        "type": prop_type,
                        "price_total": round(total_price, 1),
                        "price_unit": int(unit_price),
                        "area": area_val,
                        "layout": layout,
                        "latitude": round(lat, 6),
                        "longitude": round(lng, 6),
                        "precision": precision,
                        "tags": tags,
                        "source": "beike",
                    }
                    city_list.append(prop)

                except Exception:
                    continue

            results.extend(city_list)
            print(f"    pg{pg}: {len(city_list)}条", end="")
            random_delay()

        except requests.RequestException as e:
            print(f"    [WARN] pg{pg}: {e}")
            continue

    print(f" → 共{len(results)}条")
    return results


# ===================== 主流程 =====================

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cities', help='逗号分隔的城市；默认全部配置城市')
    parser.add_argument('--pages', type=int, choices=range(1, 11),
                        help='每城市每类最多页数（1..10）')
    args = parser.parse_args(argv)
    cities = set(args.cities.split(',')) if args.cities else None
    if cities and cities - (ERSHOUFANG_CITIES.keys() | LOUPAN_CITIES.keys()):
        parser.error('Unknown city')
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print("=" * 60)
    print("  贝壳找房数据采集")
    print("=" * 60)

    all_properties = []
    pid = 1

    # 二手房
    print("\n[二手房屋爬取]")
    for city, cfg in ERSHOUFANG_CITIES.items():
        if cities and city not in cities:
            continue
        props = crawl_ershoufang(city, cfg["code"], args.pages or cfg["pages"])
        for p in props:
            p["id"] = f"ershou_{pid}"
            pid += 1
        all_properties.extend(props)
        if city == "北京":
            time.sleep(3)

    # 新房
    print("\n[新房楼盘爬取]")
    for city, cfg in LOUPAN_CITIES.items():
        if cities and city not in cities:
            continue
        props = crawl_loupan(city, cfg["code"], args.pages or cfg["pages"])
        for p in props:
            p["id"] = f"loupan_{pid}"
            pid += 1
        all_properties.extend(props)
        time.sleep(2)

    # 保存
    if not all_properties:
        print('未取得有效房源，保留已有数据。', flush=True)
        return 2
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    atomic_json(Path(OUTPUT_FILE), all_properties)

    ershou = sum(1 for p in all_properties if p["type"] == "二手房")
    loupan = sum(1 for p in all_properties if p["type"] == "新房")
    bieshu = sum(1 for p in all_properties if p["type"] == "别墅")
    cities = len(set(p["city"] for p in all_properties))

    print(f"\n{'='*60}")
    print(f"  采集完成!")
    print(f"  总计: {len(all_properties)}条 ({ershou}二手房 + {loupan}新房 + {bieshu}别墅)")
    print(f"  覆盖: {cities}个城市")
    print(f"  输出: {OUTPUT_FILE}")
    print(f"{'='*60}")


if __name__ == "__main__":
    sys.exit(main())
