"""
城市交集筛选器 v2
================
筛选"安居客可获取 + 统计局70城数据"同时包含的城市。
安居客覆盖626个城市，70城全部在范围内。

输出：
1. intersection_cities.json — 交集城市列表（含slug、坐标、等级等）
2. 控制台打印交集详情
"""

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANJUKE_DIR = PROJECT_ROOT / "output" / "anjuke"
STATS_DIR = PROJECT_ROOT / "output" / "stats_gov"
OUTPUT_DIR = PROJECT_ROOT / "data_processing" / "frontend"

# ============================================================
# 安居客 PC端 fangjia 页面 slug → 中文名 映射
# PC端URL: www.anjuke.com/fangjia/{slug}{year}/
# 部分城市用二级域名: xa.anjuke.com/fangjia/xa{year}/
# ============================================================
ANJUKE_PC_SLUG_MAP = {
    # 顶级 S
    "beijing": "北京", "shanghai": "上海", "shenzhen": "深圳",
    # 一线
    "guangzhou": "广州", "hangzhou": "杭州", "nanjing": "南京",
    "chengdu": "成都", "wuhan": "武汉", "tianjin": "天津",
    "xiamen": "厦门", "chongqing": "重庆",
    # 二线 - 有直接Anjuke数据的
    "xian": "西安",      # PC端用 xa.anjuke.com/fangjia/xa{year}/
    "changsha": "长沙", "zhengzhou": "郑州", "qingdao": "青岛",
    "dalian": "大连", "ningbo": "宁波", "jinan": "济南",
    "hefei": "合肥", "fuzhou": "福州", "kunming": "昆明",
    "wuxi": "无锡", "nanning": "南宁", "guiyang": "贵阳",
    "nanchang": "南昌", "taiyuan": "太原", "shijiazhuang": "石家庄",
    "haerbin": "哈尔滨", "changchun": "长春", "shenyang": "沈阳",
    "lanzhou": "兰州", "huhehaote": "呼和浩特", "haikou": "海口",
    "yinchuan": "银川", "xining": "西宁", "wulumuqi": "乌鲁木齐",
    # 二线/三线 - 70城中的其余城市
    "sanya": "三亚", "dandong": "丹东", "jiujiang": "九江",
    "baotou": "包头", "beihai": "北海", "nanchong": "南充",
    "jilin": "吉林", "tangshan": "唐山", "dali": "大理",
    "anqing": "安庆", "yichang": "宜昌", "yueyang": "岳阳",
    "changde": "常德", "pingdingshan": "平顶山",
    "xuzhou": "徐州", "huizhou": "惠州", "yangzhou": "扬州",
    "guilin": "桂林", "quanzhou": "泉州", "luzhou": "泸州",
    "luoyang": "洛阳", "jining": "济宁", "wenzhou": "温州",
    "zhanjiang": "湛江", "yantai": "烟台", "mudanjiang": "牡丹江",
    "qinhuangdao": "秦皇岛", "bengbu": "蚌埠", "xiangyang": "襄阳",
    "ganzhou": "赣州", "zunyi": "遵义", "jinhua": "金华",
    "jinzhou": "锦州", "shaoguan": "韶关",
    # 安居客有数据但不在统计局70城的(供参考)
    "suzhou": "苏州",
}

# 特殊二级域名城市
SUBDOMAIN_CITIES = {
    "xian": {"subdomain": "xa", "url_pattern": "xa.anjuke.com/fangjia/xa{year}/"},
}

# ============================================================
# 安居客省份 slug → 省份名 映射 (用于遍历省份页面获取城市列表)
# URL: www.anjuke.com/fangjia/{province_slug}/
# ============================================================
ANJUKE_PROVINCE_MAP = {
    "anhui": "安徽", "beijing": "北京", "chongqing": "重庆",
    "fujian": "福建", "gansu": "甘肃", "guangdong": "广东",
    "guangxi": "广西", "guizhou": "贵州", "hainan": "海南",
    "hebei": "河北", "heilongjiang": "黑龙江", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "jiangsu": "江苏",
    "jiangxi": "江西", "jilin": "吉林", "liaoning": "辽宁",
    "neimenggu": "内蒙古", "ningxia": "宁夏", "qinghai": "青海",
    "shaanxi": "陕西", "shandong": "山东", "shanghai": "上海",
    "shanxi": "山西", "sichuan": "四川", "tianjin": "天津",
    "xinjiang": "新疆", "xizang": "西藏", "yunnan": "云南",
    "zhejiang": "浙江",
    "yixianchengshi": "直辖市",
}

# ============================================================
# 城市等级分类（70城全部纳入）
# ============================================================
TIER_CLASSIFICATION = {
    # S - 顶级
    "北京": {"tier": "S", "tier_name": "顶级", "color": "#FF4B4B",
             "lat": 39.9042, "lon": 116.4074, "anjuke_slug": "beijing",
             "province": "北京"},
    "上海": {"tier": "S", "tier_name": "顶级", "color": "#FF4B4B",
             "lat": 31.2304, "lon": 121.4737, "anjuke_slug": "shanghai",
             "province": "上海"},
    "深圳": {"tier": "S", "tier_name": "顶级", "color": "#FF4B4B",
             "lat": 22.5431, "lon": 114.0579, "anjuke_slug": "shenzhen",
             "province": "广东"},
    # Tier 1 - 一线
    "广州": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 23.1291, "lon": 113.2644, "anjuke_slug": "guangzhou",
             "province": "广东"},
    "杭州": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 30.2741, "lon": 120.1551, "anjuke_slug": "hangzhou",
             "province": "浙江"},
    "南京": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 32.0603, "lon": 118.7969, "anjuke_slug": "nanjing",
             "province": "江苏"},
    "武汉": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 30.5928, "lon": 114.3055, "anjuke_slug": "wuhan",
             "province": "湖北"},
    "天津": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 39.3434, "lon": 117.3616, "anjuke_slug": "tianjin",
             "province": "天津"},
    "厦门": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 24.4798, "lon": 118.0894, "anjuke_slug": "xiamen",
             "province": "福建"},
    "成都": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 30.5728, "lon": 104.0668, "anjuke_slug": "chengdu",
             "province": "四川"},
    "重庆": {"tier": "1", "tier_name": "一线", "color": "#FF9F43",
             "lat": 29.4316, "lon": 106.9123, "anjuke_slug": "chongqing",
             "province": "重庆"},
    # Tier 2 - 二线（70城中的二线城市）
    "西安": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 34.3416, "lon": 108.9398, "anjuke_slug": "xian",
             "province": "陕西"},
    "长沙": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 28.2282, "lon": 112.9388, "anjuke_slug": "changsha",
             "province": "湖南"},
    "郑州": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 34.7466, "lon": 113.6253, "anjuke_slug": "zhengzhou",
             "province": "河南"},
    "青岛": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 36.0671, "lon": 120.3826, "anjuke_slug": "qingdao",
             "province": "山东"},
    "宁波": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 29.8683, "lon": 121.5440, "anjuke_slug": "ningbo",
             "province": "浙江"},
    "大连": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 38.9140, "lon": 121.6147, "anjuke_slug": "dalian",
             "province": "辽宁"},
    "济南": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 36.6512, "lon": 116.9972, "anjuke_slug": "jinan",
             "province": "山东"},
    "合肥": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 31.8206, "lon": 117.2272, "anjuke_slug": "hefei",
             "province": "安徽"},
    "福州": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 26.0745, "lon": 119.2965, "anjuke_slug": "fuzhou",
             "province": "福建"},
    "昆明": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 25.0389, "lon": 102.7183, "anjuke_slug": "kunming",
             "province": "云南"},
    "无锡": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 31.4912, "lon": 120.3119, "anjuke_slug": "wuxi",
             "province": "江苏"},
    "南宁": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 22.8170, "lon": 108.3665, "anjuke_slug": "nanning",
             "province": "广西"},
    "贵阳": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 26.6470, "lon": 106.6302, "anjuke_slug": "guiyang",
             "province": "贵州"},
    "南昌": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 28.6820, "lon": 115.8579, "anjuke_slug": "nanchang",
             "province": "江西"},
    "太原": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 37.8706, "lon": 112.5489, "anjuke_slug": "taiyuan",
             "province": "山西"},
    "石家庄": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 38.0428, "lon": 114.5149, "anjuke_slug": "shijiazhuang",
             "province": "河北"},
    "哈尔滨": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 45.8038, "lon": 126.5350, "anjuke_slug": "haerbin",
             "province": "黑龙江"},
    "长春": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 43.8171, "lon": 125.3235, "anjuke_slug": "changchun",
             "province": "吉林"},
    "沈阳": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 41.8057, "lon": 123.4315, "anjuke_slug": "shenyang",
             "province": "辽宁"},
    "兰州": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 36.0611, "lon": 103.8343, "anjuke_slug": "lanzhou",
             "province": "甘肃"},
    "呼和浩特": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
                 "lat": 40.8427, "lon": 111.7490, "anjuke_slug": "huhehaote",
                 "province": "内蒙古"},
    "海口": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 20.0174, "lon": 110.3492, "anjuke_slug": "haikou",
             "province": "海南"},
    "银川": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 38.4872, "lon": 106.2309, "anjuke_slug": "yinchuan",
             "province": "宁夏"},
    "西宁": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
             "lat": 36.6171, "lon": 101.7782, "anjuke_slug": "xining",
             "province": "青海"},
    "乌鲁木齐": {"tier": "2", "tier_name": "二线", "color": "#54A0FF",
                 "lat": 43.8256, "lon": 87.6168, "anjuke_slug": "wulumuqi",
                 "province": "新疆"},
    # Tier 3 - 三线（70城中的三线城市）
    "三亚": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 18.2528, "lon": 109.5120, "anjuke_slug": "sanya",
             "province": "海南"},
    "丹东": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 40.0006, "lon": 124.3540, "anjuke_slug": "dandong",
             "province": "辽宁"},
    "九江": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 29.7053, "lon": 116.0020, "anjuke_slug": "jiujiang",
             "province": "江西"},
    "包头": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 40.6571, "lon": 109.8403, "anjuke_slug": "baotou",
             "province": "内蒙古"},
    "北海": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 21.4810, "lon": 109.1200, "anjuke_slug": "beihai",
             "province": "广西"},
    "南充": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 30.8378, "lon": 106.1107, "anjuke_slug": "nanchong",
             "province": "四川"},
    "吉林": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 43.8378, "lon": 126.5497, "anjuke_slug": "jilin",
             "province": "吉林"},
    "唐山": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 39.6309, "lon": 118.1802, "anjuke_slug": "tangshan",
             "province": "河北"},
    "大理": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 25.6065, "lon": 100.2679, "anjuke_slug": "dali",
             "province": "云南"},
    "安庆": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 30.5430, "lon": 117.0631, "anjuke_slug": "anqing",
             "province": "安徽"},
    "宜昌": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 30.6918, "lon": 111.2864, "anjuke_slug": "yichang",
             "province": "湖北"},
    "岳阳": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 29.3562, "lon": 113.1290, "anjuke_slug": "yueyang",
             "province": "湖南"},
    "常德": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 29.0318, "lon": 111.6983, "anjuke_slug": "changde",
             "province": "湖南"},
    "平顶山": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
              "lat": 33.7662, "lon": 113.1927, "anjuke_slug": "pingdingshan",
              "province": "河南"},
    "徐州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 34.2616, "lon": 117.1846, "anjuke_slug": "xuzhou",
             "province": "江苏"},
    "惠州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 23.1116, "lon": 114.4160, "anjuke_slug": "huizhou",
             "province": "广东"},
    "扬州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 32.3943, "lon": 119.4128, "anjuke_slug": "yangzhou",
             "province": "江苏"},
    "桂林": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 25.2740, "lon": 110.2900, "anjuke_slug": "guilin",
             "province": "广西"},
    "泉州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 24.8741, "lon": 118.6758, "anjuke_slug": "quanzhou",
             "province": "福建"},
    "泸州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 28.8717, "lon": 105.4425, "anjuke_slug": "luzhou",
             "province": "四川"},
    "洛阳": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 34.6197, "lon": 112.4540, "anjuke_slug": "luoyang",
             "province": "河南"},
    "济宁": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 35.4146, "lon": 116.5871, "anjuke_slug": "jining",
             "province": "山东"},
    "温州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 28.0006, "lon": 120.6720, "anjuke_slug": "wenzhou",
             "province": "浙江"},
    "湛江": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 21.2707, "lon": 110.3594, "anjuke_slug": "zhanjiang",
             "province": "广东"},
    "烟台": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 37.4638, "lon": 121.4479, "anjuke_slug": "yantai",
             "province": "山东"},
    "牡丹江": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
              "lat": 44.5520, "lon": 129.6320, "anjuke_slug": "mudanjiang",
              "province": "黑龙江"},
    "秦皇岛": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
              "lat": 39.9354, "lon": 119.5994, "anjuke_slug": "qinhuangdao",
              "province": "河北"},
    "蚌埠": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 32.9170, "lon": 117.3889, "anjuke_slug": "bengbu",
             "province": "安徽"},
    "襄阳": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 32.0090, "lon": 112.1232, "anjuke_slug": "xiangyang",
             "province": "湖北"},
    "赣州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 25.8310, "lon": 114.9330, "anjuke_slug": "ganzhou",
             "province": "江西"},
    "遵义": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 27.7256, "lon": 106.9274, "anjuke_slug": "zunyi",
             "province": "贵州"},
    "金华": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 29.0785, "lon": 119.6498, "anjuke_slug": "jinhua",
             "province": "浙江"},
    "锦州": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 41.1140, "lon": 121.1270, "anjuke_slug": "jinzhou",
             "province": "辽宁"},
    "韶关": {"tier": "3", "tier_name": "三线", "color": "#5F27CD",
             "lat": 24.8107, "lon": 113.5975, "anjuke_slug": "shaoguan",
             "province": "广东"},
}

# ECharts 中国地图名称映射
CITY_NAME_MAP = {city: city for city in TIER_CLASSIFICATION}


def get_stats_cities():
    """从统计局数据中获取70城列表"""
    cities = set()
    if STATS_DIR.exists():
        for f in STATS_DIR.glob("*.json"):
            with open(f, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    for row in data:
                        city = row.get('city')
                        if city:
                            cities.add(city)
    return cities


def get_anjuke_crawled_cities():
    """从实际爬取数据中获取安居客城市列表"""
    cities = set()
    if ANJUKE_DIR.exists():
        for f in ANJUKE_DIR.glob("*.json"):
            with open(f, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    for row in data:
                        city = row.get('city')
                        if city:
                            cities.add(city)
    return cities


def compute_full_intersection():
    """计算完整交集：安居客(全626城) ∩ 统计局70城

    安居客覆盖626个城市，70城全在范围内。
    交集 = 统计局70城中有安居客slug映射的城市。
    """
    stats_cities = get_stats_cities()
    anjuke_crawled = get_anjuke_crawled_cities()

    # 安居客slug映射覆盖的城市
    anjuke_mapped = set(ANJUKE_PC_SLUG_MAP.values())

    # 完整交集 = 统计局70城 ∩ 安居客slug映射
    intersection = stats_cities & anjuke_mapped

    # 已爬取数据的城市
    crawled_in_intersection = anjuke_crawled & intersection

    # 未爬取但有slug的城市（需要逆推）
    not_crawled = intersection - anjuke_crawled

    # 不在交集中的统计局城市
    stats_only = stats_cities - anjuke_mapped

    # 不在交集中的安居客城市
    anjuke_only = anjuke_mapped - stats_cities

    return {
        "stats_cities": sorted(stats_cities),
        "anjuke_crawled": sorted(anjuke_crawled),
        "anjuke_mapped": sorted(anjuke_mapped),
        "intersection": sorted(intersection),
        "crawled_in_intersection": sorted(crawled_in_intersection),
        "not_crawled": sorted(not_crawled),
        "stats_only": sorted(stats_only),
        "anjuke_only": sorted(anjuke_only),
    }


def build_intersection_config(intersection_cities):
    """为交集城市构建前端配置"""
    config = []
    for city in intersection_cities:
        info = TIER_CLASSIFICATION.get(city)
        if not info:
            continue
        entry = {
            "city": city,
            "map_name": CITY_NAME_MAP.get(city, city),
            "tier": info["tier"],
            "tier_name": info["tier_name"],
            "color": info["color"],
            "lat": info["lat"],
            "lon": info["lon"],
            "anjuke_slug": info["anjuke_slug"],
            "province": info.get("province", ""),
        }
        # 二级域名标记
        if info["anjuke_slug"] in SUBDOMAIN_CITIES:
            entry["subdomain"] = SUBDOMAIN_CITIES[info["anjuke_slug"]]["subdomain"]
        config.append(entry)

    # 按等级+纬度排序
    tier_order = {"S": 0, "1": 1, "2": 2, "3": 3}
    config.sort(key=lambda x: (tier_order.get(x["tier"], 9), -x.get("lat", 0)))
    return config


def build_province_city_tree():
    """构建省份→城市→slug的树形结构（用于遍历安居客省份页面）"""
    tree = {}
    for city, info in TIER_CLASSIFICATION.items():
        province = info.get("province", "")
        if province not in tree:
            tree[province] = []
        tree[province].append({
            "city": city,
            "anjuke_slug": info["anjuke_slug"],
            "tier": info["tier"],
        })
    # 添加省份slug
    result = {}
    for province, cities in tree.items():
        province_slug = ""
        for slug, name in ANJUKE_PROVINCE_MAP.items():
            if name == province:
                province_slug = slug
                break
        result[province] = {
            "province_slug": province_slug,
            "cities": sorted(cities, key=lambda c: c["city"]),
        }
    return result


def main():
    result = compute_full_intersection()

    print("=" * 60)
    print("城市交集筛选结果 v2 (70城完整版)")
    print("=" * 60)
    print(f"统计局70城 ({len(result['stats_cities'])}): {len(result['stats_cities'])} 个")
    print(f"安居客slug映射 ({len(result['anjuke_mapped'])}): {len(result['anjuke_mapped'])} 个")
    print(f"完整交集 ({len(result['intersection'])}): {result['intersection']}")
    print(f"已爬取数据 ({len(result['crawled_in_intersection'])}): {result['crawled_in_intersection']}")
    print(f"未爬取需逆推 ({len(result['not_crawled'])}): {len(result['not_crawled'])} 个")
    print(f"仅在统计局 ({len(result['stats_only'])}): {result['stats_only']}")
    print(f"仅在安居客 ({len(result['anjuke_only'])}): {result['anjuke_only']}")

    # 构建交集配置
    intersection_config = build_intersection_config(result["intersection"])

    # 构建省份树
    province_tree = build_province_city_tree()

    # 保存交集配置
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "intersection_cities.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            "meta": {
                "description": "安居客(626城) ∩ 统计局70城 交集城市列表",
                "stats_count": len(result["stats_cities"]),
                "anjuke_mapped_count": len(result["anjuke_mapped"]),
                "intersection_count": len(result["intersection"]),
                "crawled_count": len(result["crawled_in_intersection"]),
                "not_crawled_count": len(result["not_crawled"]),
                "anjuke_total_cities": 626,
            },
            "cities": intersection_config,
            "province_tree": province_tree,
            "anjuke_province_map": ANJUKE_PROVINCE_MAP,
            "subdomain_cities": SUBDOMAIN_CITIES,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n交集配置保存到: {output_path}")
    print(f"交集城市数量: {len(intersection_config)}")

    # 按等级打印
    for tier_name in ["顶级", "一线", "二线", "三线"]:
        tier_cities = [c for c in intersection_config if c["tier_name"] == tier_name]
        print(f"\n  {tier_name} ({len(tier_cities)}):")
        for c in tier_cities:
            print(f"    {c['city']} slug={c['anjuke_slug']} "
                  f"({c['lat']}, {c['lon']}) {c.get('province','')}")


if __name__ == "__main__":
    main()