"""
安居客省份城市爬虫
==================
从安居客房价总页面出发，遍历所有省份页面，
获取每个省份下的所有城市房价数据。

URL结构:
  总页面: https://www.anjuke.com/fangjia/
  省份页面: https://www.anjuke.com/fangjia/{province_slug}/
  直辖市: https://www.anjuke.com/fangjia/yixianchengshi/
  城市页面: https://www.anjuke.com/fangjia/{city_slug}/
  城市年份: https://www.anjuke.com/fangjia/{city_slug}{year}/
  二级域名城市: https://{sub}.anjuke.com/fangjia/{sub}{year}/

页面解析:
  SSR 的 window.__NUXT__ 载荷可能含 yearAreaData.trendList 月度均价。
  每次核对城市和实际月份；空响应、验证码及错误城市均不作为价格。
  历史年份页与滚动窗口分别解析，缺期不推断，失败保留已核验观测。

使用方式:
  1. 自动爬取模式(推荐, 需要本机装有 Node.js):
     python anjuke_province_crawler.py --crawl
  2. 手动模式(输出所有待访问URL，用户手动填写数据):
     python anjuke_province_crawler.py --manual
  3. Playwright模式(需要手动完成验证码):
     python anjuke_province_crawler.py --playwright
"""

import json
import sys
import io
import random
import tempfile
import time
import re
from datetime import date,datetime,timezone
from pathlib import Path
from browser_runtime import launch_browser

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "anjuke"
SOURCE_DIR = PROJECT_ROOT / "source"

# 安居客省份 slug 列表 (从 website_resources.txt 和推断)
PROVINCES = {
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

# 70城完整slug映射 (PC端fangjia页面)
CITY_SLUG_70 = {
    "北京": "beijing", "上海": "shanghai", "深圳": "shenzhen",
    "广州": "guangzhou", "杭州": "hangzhou", "南京": "nanjing",
    "成都": "chengdu", "武汉": "wuhan", "天津": "tianjin",
    "厦门": "xiamen", "重庆": "chongqing",
    "西安": "xian", "长沙": "changsha", "郑州": "zhengzhou",
    "青岛": "qingdao", "大连": "dalian", "宁波": "ningbo",
    "苏州": "suzhou", "济南": "jinan", "合肥": "hefei", "福州": "fuzhou",
    "昆明": "kunming", "无锡": "wuxi", "南宁": "nanning",
    "贵阳": "guiyang", "南昌": "nanchang", "太原": "taiyuan",
    "石家庄": "shijiazhuang", "哈尔滨": "haerbin", "长春": "changchun",
    "沈阳": "shenyang", "兰州": "lanzhou", "呼和浩特": "huhehaote",
    "海口": "haikou", "银川": "yinchuan", "西宁": "xining",
    "乌鲁木齐": "wulumuqi",
    "三亚": "sanya", "丹东": "dandong", "九江": "jiujiang",
    "包头": "baotou", "北海": "beihai", "南充": "nanchong",
    "吉林": "jilin", "唐山": "tangshan", "大理": "dali",
    "安庆": "anqing", "宜昌": "yichang", "岳阳": "yueyang",
    "常德": "changde", "平顶山": "pingdingshan",
    "徐州": "xuzhou", "惠州": "huizhou", "扬州": "yangzhou",
    "桂林": "guilin", "泉州": "quanzhou", "泸州": "luzhou",
    "洛阳": "luoyang", "济宁": "jining", "温州": "wenzhou",
    "湛江": "zhanjiang", "烟台": "yantai", "牡丹江": "mudanjiang",
    "秦皇岛": "qinhuangdao", "蚌埠": "bengbu", "襄阳": "xiangyang",
    "赣州": "ganzhou", "遵义": "zunyi", "金华": "jinhua",
    "锦州": "jinzhou", "韶关": "shaoguan",
}

# 二级域名城市 (PC端fangjia使用子域名而非主域名)
SUBDOMAIN_CITIES = {
    "西安": {"subdomain": "xa", "url": "xa.anjuke.com/fangjia/xa{year}/"},
    "北京": {"subdomain": "bj", "url": "bj.anjuke.com/fangjia/bj{year}/"},
    "上海": {"subdomain": "sh", "url": "sh.anjuke.com/fangjia/sh{year}/"},
    "广州": {"subdomain": "gz", "url": "gz.anjuke.com/fangjia/gz{year}/"},
    "深圳": {"subdomain": "sz", "url": "sz.anjuke.com/fangjia/sz{year}/"},
    "成都": {"subdomain": "cd", "url": "cd.anjuke.com/fangjia/cd{year}/"},
    "杭州": {"subdomain": "hz", "url": "hz.anjuke.com/fangjia/hz{year}/"},
    "南京": {"subdomain": "nj", "url": "nj.anjuke.com/fangjia/nj{year}/"},
    "武汉": {"subdomain": "wh", "url": "wh.anjuke.com/fangjia/wh{year}/"},
    "天津": {"subdomain": "tj", "url": "tj.anjuke.com/fangjia/tj{year}/"},
    "重庆": {"subdomain": "cq", "url": "cq.anjuke.com/fangjia/cq{year}/"},
    "厦门": {"subdomain": "xm", "url": "xm.anjuke.com/fangjia/xm{year}/"},
}

# 2021年4月基准价格参考 (用于无Anjuke数据的城市逆推)
# 来源: 安居客历史数据 + 公开市场数据
# 单位: 元/㎡
BASE_PRICE_REF_2021Q1 = {
    # S级
    "北京": 62516, "上海": 53858, "深圳": 58303,
    # 一线
    "广州": 38051, "杭州": 36570, "南京": 24679,
    "武汉": 16277, "天津": 16495, "厦门": 48516,
    "成都": 16898, "重庆": 12085,
    # 二线
    "西安": 16015, "长沙": 10425, "郑州": 12397,
    "青岛": 17606, "大连": 12158, "宁波": 23987,
    "济南": 16297, "合肥": 17343, "福州": 27456,
    "昆明": 10297, "无锡": 15960, "南宁": 10979,
    "贵阳": 9058, "南昌": 11154, "太原": 10160,
    "石家庄": 12907, "哈尔滨": 8854, "长春": 8404,
    "沈阳": 9093, "兰州": 11725, "呼和浩特": 10328,
    "海口": 15550, "银川": 7240, "西宁": 8503,
    "乌鲁木齐": 8345,
    # 三线(估算 - 基于公开数据)
    "三亚": 33278, "丹东": 5537, "九江": 7450,
    "包头": 6810, "北海": 6880, "南充": 6354,
    "吉林": 5428, "唐山": 8911, "大理": 11580,
    "安庆": 7142, "宜昌": 7839, "岳阳": 6828,
    "常德": 5968, "平顶山": 5371, "徐州": 10259,
    "惠州": 10453, "扬州": 13364, "桂林": 6420,
    "泉州": 11382, "泸州": 6528, "洛阳": 8398,
    "济宁": 7215, "温州": 20656, "湛江": 7330,
    "烟台": 10131, "牡丹江": 4888, "秦皇岛": 10320,
    "蚌埠": 7318, "襄阳": 8174, "赣州": 8438,
    "遵义": 5888, "金华": 14752, "锦州": 4562,
    "韶关": 5989,
}


def try_urllib_fetch(url, timeout=10):
    """使用urllib尝试获取页面"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://www.anjuke.com/',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None


def extract_city_links(html, province_slug):
    """从省份页面HTML中提取城市链接"""
    links = []
    # 安居客省份页面中的城市链接格式:
    # <a href="/fangjia/{city_slug}/">{城市名}</a>
    pattern = r'<a[^>]*href="(/fangjia/([^/]+)/)"[^>]*>([^<]+)</a>'
    matches = re.findall(pattern, html)
    for href, slug, name in matches:
        if slug != province_slug and slug != 'yixianchengshi':
            links.append({"slug": slug, "name": name.strip(), "url": f"https://www.anjuke.com{href}"})
    return links


def extract_price_data(html):
    """从城市年份页面HTML中提取价格数据

    兼容千分位写法("62,516元/㎡"), 跨标签/换行匹配月份与价格。
    """
    prices = []
    # 安居客价格页面中的月度价格格式
    # 格式可能是: <td>{month}月</td><td>{price}元/㎡ 或 62,516元/㎡
    price_pattern = r'(\d{1,2})月[^0-9]{0,40}(\d{3,6}(?:,\d{3})*)\s*元/㎡'
    matches = re.findall(price_pattern, html, re.S)
    for month, price in matches:
        prices.append({"month": int(month), "price": int(price.replace(',', ''))})
    # 去重(同月取最后一次出现), 按月排序
    dedup = {p["month"]: p["price"] for p in prices}
    return [{"month": m, "price": v} for m, v in sorted(dedup.items())]


# 抓取年份范围 (与安居客 fangjia 页面年份一致)
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


# ============================================================
# --crawl 自动爬取模式 (NUXT 载荷提取，挑战页停止并保留旧数据)
# ============================================================
import html as html_lib
import subprocess
import urllib.request

NUXT_EXTRACT_JS = r"""
const fs = require('fs');
const vm = require('node:vm');
const script = new vm.Script('(' + fs.readFileSync(process.argv[2], 'utf-8') + ')');
const obj = script.runInNewContext(Object.create(null), {timeout: 1000, contextCodeGeneration: {strings: false, wasm: false}});
const data = (obj && obj.data) || {};
// Market report: use city-wide monthly series only, never weekly/district curves.
const market = data[0]?.priceTrendReportData;
if (market) {
  const root=data[0], city=market.cityName;
  const valid=market.type==='1' && market.id===market.cityId && market.name===city && root.city_info_vo?.name===city;
  const byYear={};
  if(valid) for(const trend of market.priceTrend||[]) {
    if(String(trend.trendType)!=='3')continue;
    for(const row of trend.city||[]) {
      const m=/^(\d{2}|20\d{2})-(0[1-9]|1[0-2])$/.exec(String(row.date||''));
      const price=Number(row.price);
      if(!m||!Number.isFinite(price)||price<=0||price>1000000)continue;
      const year=m[1].length===2?2000+Number(m[1]):Number(m[1]);
      (byYear[year] ||= []).push({month:Number(m[2]),price});
    }
  }
  console.log(JSON.stringify({city:valid?city:null,blocks:Object.entries(byYear).map(([year,months])=>({year:Number(year),months})),schema:'market_monthly'}));
  process.exit(0);
}
const d2 = data['2'] || data[2] || {};
const d1 = data['1'] || data[1] || {};
const info = d2.infoList || d1.infoList || {};
const city = info.cityName || info.name || null;
const blocks = [];

function pushBlock(year, list, fromTitles) {
  if (!year || !Array.isArray(list)) return;
  const months = [];
  for (const m of list) {
    if (!m) continue;
    const t = String(m.title || '');
    const p = m.avgPrice;
    if (p === undefined || p === null || String(p) === '' || +p <= 0) continue;
    if (fromTitles) {
      const mm = /^(\d{1,2})月$/.exec(t);
      if (mm) months.push({month: +mm[1], price: +p});
    } else {
      const ym = /^(\d{4})年(\d{1,2})月/.exec(t);
      if (ym) months.push({month: +ym[2], price: +p});
    }
  }
  if (months.length) blocks.push({year: +year, months});
}

// schema A (城市年份页): data[2].yearAreaData.trendList, title="N月", 年份=页面year
if (d2.yearAreaData && d2.yearAreaData.trendList && d2.year) {
  pushBlock(d2.year, d2.yearAreaData.trendList, true);
}
// schema B (无年份页): data[2].avgPriceData=[{title:"YYYY年XX房价", priceVOList:[{title:"YYYY年M月房价", avgPrice}]}]
if (Array.isArray(d2.avgPriceData)) {
  for (const b of d2.avgPriceData) {
    const t = String((b && b.title) || '');
    let ym = /^(\d{4})年/.exec(t);
    if (!ym && b && b.actionUrl) {
      ym = /fangjia\/[a-z]+(\d{4})\//.exec(String(b.actionUrl));
    }
    if (ym) pushBlock(+ym[1], (b && b.priceVOList) || [], false);
  }
}
console.log(JSON.stringify({city, blocks}));
"""

FETCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/126.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.anjuke.com/',
}

# 城市新 url_mark(起始页 fangjia/ NUXT 省市树提取, 2026-09):
# 这些城市的主域旧 slug 年份页已 404, 站点改用短标记, 形态如:
#   www.anjuke.com/fangjia/heb2025/ (带年份) / www.anjuke.com/fangjia/heb/ (无年份滚动窗口)
#   {mark}.anjuke.com/fangjia/{mark}{year}/ (子域兜底)
CITY_URL_MARKS = {
    "南昌": "nc", "厦门": "xm", "合肥": "hf", "哈尔滨": "heb", "太原": "ty",
    "宁波": "nb", "昆明": "km", "沈阳": "sy", "石家庄": "sjz", "福州": "fz",
    "西安": "xa", "贵阳": "gy", "长春": "cc", "长沙": "cs", "青岛": "qd",
    "烟台": "yt", "平顶山": "pingdingsha",
}


def city_page_urls(city, slug, year):
    """一个城市年份页的候选URL(按可靠性排序)。

    新mark城市跳过注定404的旧slug页, 优先 {mark}{year};
    无年份聚合页(每年仅近5个月)作兜底, 子域最后。
    """
    mark = CITY_URL_MARKS.get(city, slug)
    urls = [f"https://www.anjuke.com/fangjia/{mark}{year}/",
            f"https://www.anjuke.com/fangjia/{mark}/"]
    if mark != slug:
        urls.append(f"https://www.anjuke.com/fangjia/{slug}{year}/")
        urls.append(f"https://{mark}.anjuke.com/fangjia/{mark}{year}/")
    urls.append(f'https://{mark}.anjuke.com/market/')
    return urls


def fetch_fangjia_page(url, retries=2):
    """抓取页面 HTML; 失败/被拦截返回 None。

    低频请求仍可能收到空页、网关或验证码；不能以 HTTP 200 判断成功。
    数据是否有效由后续城市和月份解析校验决定。
    """
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=FETCH_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                disp = r.headers.get('Sec-Disp-Code', '')
                html = r.read().decode('utf-8', errors='replace')
            if len(html) < 2000 or disp.startswith('CZ') or '请输入验证码' in html:
                last = RuntimeError(f'WAF拦截({len(html)}B, disp={disp or "-"})')
            else:
                return html
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # 该年份页面不存在
            last = e
        except Exception as e:
            last = e
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    return None


def extract_trend(payload_js):
    """用 Node 求值 NUXT 载荷, 返回 {year, city, months:[{title,avgPrice}]} 或 None。"""
    if 'window.__NUXT__' not in payload_js and 'yearAreaData' not in payload_js:
        return None
    start = payload_js.find('window.__NUXT__=')
    end = payload_js.find('</script>', start)
    if start < 0 or end < 0:
        return None
    js = payload_js[start + len('window.__NUXT__='):end].strip().rstrip(';')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run(['node', '-e', NUXT_EXTRACT_JS, 'x', tmp],
                           capture_output=True, text=True, encoding='utf-8', timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.strip())
    except Exception:
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def month_num(title):
    m = re.match(r'(\d{1,2})月', title or '')
    return int(m.group(1)) if m else None


def merge_city_rows(slug, city, new_rows, evidence=None):
    """把 {(y,m): price} 以"只增不改"方式合并进 per-city 引擎 schema 文件。

    已存在的 (年,月) 记录不覆盖(保护人工校验过的历史锚点), 仅补缺失月份。
    返回 (总记录数, 新增数, 冲突数)。
    """
    out_path = OUTPUT_DIR / f"{slug}.json"
    existing = {}
    if out_path.exists():
        try:
            for r in json.loads(out_path.read_text(encoding='utf-8')):
                existing[(int(r.get('year')), int(r.get('month')))] = r
        except Exception as exc:
            raise ValueError(f'无法读取历史数据，停止覆盖: {out_path}') from exc
    added = conflicts = 0
    for (y, m), price in sorted(new_rows.items()):
        if not 1 <= m <= 12 or (y, m) >= (date.today().year, date.today().month) or price <= 0:
            continue
        old_row = existing.get((y, m))
        if old_row and old_row.get('avg_price', 0) > 0:
            old = old_row['avg_price']
            if abs(int(old) - int(price)) > max(2, price * 0.005):
                conflicts += 1  # 安居客口径修订, 保留旧值仅计数
            elif int(old) == int(price) and evidence and (y, m) in evidence:
                old_row.update(evidence[(y, m)])
            continue
        if old_row:
            print(f"    [修复] {y}-{m:02d} 旧记录为0值, 用真实值覆盖")
        existing[(y, m)] = {
            "city": city, "city_pinyin": slug, "year": y, "month": m,
            "avg_price": int(price), "data_source": "anjuke",
        }
        existing[(y, m)].update((evidence or {}).get((y, m), {}))
        added += 1
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [existing[k] for k in sorted(existing)]
    temporary = out_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(out_path)
    return len(rows), added, conflicts


def existing_year_months(slug):
    """读取 per-city 文件已有月份集合 {(y,m)}(用于增量: 已满12个月的旧年份跳过)。"""
    out_path = OUTPUT_DIR / f"{slug}.json"
    have = set()
    if out_path.exists():
        try:
            for r in json.loads(out_path.read_text(encoding='utf-8')):
                if r.get('avg_price', 0) > 0:  # 0 值为历史脏数据, 不算已覆盖
                    have.add((int(r.get('year')), int(r.get('month'))))
        except Exception:
            pass
    return have


def browser_extract(url, wait_ms=6000):
    """用 runner Chromium 或远程浏览器加载并返回渲染后的 HTML。

    浏览器及依赖由云端工作流准备，不依赖系统 Edge。
    返回 HTML 或 None(加载失败/验证码页)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[错误] 浏览器模式需要 playwright: pip install playwright")
        return None
    with sync_playwright() as p:
        browser = launch_browser(p)
        ctx = browser.new_context(
            user_agent=FETCH_HEADERS['User-Agent'], locale='zh-CN')
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        try:
            page.goto(url, timeout=45000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
        except Exception as e:
            print(f"    [WARN] {url} 加载失败: {str(e)[:80]}")
            html = None
        finally:
            browser.close()
    if html and ('请输入验证码' in html or len(html) < 5000):
        return None
    return html


def crawl_browser_mode(years_from=2021, only_cities=None, sleep_s=20):
    """浏览器兜底模式: 使用独立浏览器上下文尝试公开页面，不保证通过反爬。

    仅手动运行, 不接入启动流程。城市默认取 CITY_URL_MARKS 中仍缺当年数据的。
    候选URL与 crawl_mode 相同(主域新mark带年份→无年份→子域)。
    """
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)
    today = date.today()
    cur_year, cur_month = today.year, today.month

    candidates = CITY_SLUG_70 if only_cities else CITY_URL_MARKS
    targets = [c for c in candidates
               if not only_cities or c in only_cities]
    if not targets:
        print("没有需要补爬的城市")
        return

    print("=" * 70)
    print(f"浏览器兜底模式 ({len(targets)} 城, 间隔{sleep_s}s)")
    print("=" * 70)

    total_added = 0
    for i, city in enumerate(sorted(targets), 1):
        if city in CITY_DATA_BUG:
            print(f"[{i:>2}/{len(targets)}] {city}: 跳过 ({CITY_DATA_BUG[city]})")
            continue
        slug = CITY_SLUG_70.get(city, '')
        mark = CITY_URL_MARKS.get(city, slug)
        have = existing_year_months(slug)
        rows = {}
        for y in range(years_from, cur_year + 1):
            want_months = (cur_month - 1) if y == cur_year else 12
            if y < cur_year and \
                    sum(1 for (yy, _) in have if yy == y) >= want_months:
                continue  # 历史年份已齐
            html = None
            info = None
            for url in [f"https://www.anjuke.com/fangjia/{mark}{y}/",
                        f"https://www.anjuke.com/fangjia/{mark}/",
                        f"https://{mark}.anjuke.com/fangjia/{mark}{y}/"]:
                html = browser_extract(url)
                info = extract_trend(html) if html else None
                if info and info.get('blocks'):
                    break
                time.sleep(sleep_s)
            got = collect_rows(info or {}, cur_year, cur_month, have)
            if info and info.get('city') and info['city'] != city:
                print(f"    [WARN] {city} 页面城市为 {info['city']}, 跳过")
                got = {}
            rows.update(got)
            time.sleep(sleep_s)
        if not rows:
            print(f"[{i:>2}/{len(targets)}] {city}: 无新增")
            continue
        total, added, conflicts = merge_city_rows(slug, city, rows)
        total_added += added
        print(f"[{i:>2}/{len(targets)}] {city}: 新增 {added} 条, 共 {total} 条")

    print(f"\n完成: 共新增 {total_added} 条")


# 不再按城市永久跳过。省级年份榜单不是月度序列；逐页检查城市身份，
# 缺失月度块时通过 market 的城市级月度曲线补采，不使用预设参考价填空。
CITY_DATA_BUG = {}  # Validate each response's city/schema; market monthly fallback covers 吉林.


def collect_rows(info, cur_year, cur_month, have):
    """把 extract_trend 的 blocks 结果折叠为 {(y,m): price}。

    规则: 已有记录(have)不重复; 当年只收已完成月份(当月为预估值不落库);
    未来年份丢弃。
    """
    rows = {}
    for block in info.get('blocks', []):
        try:
            y = int(block['year'])
        except (KeyError, TypeError, ValueError):
            continue
        if y > cur_year:
            continue
        for item in block.get('months', []):
            try:
                m, price = int(item['month']), int(item['price'])
            except (KeyError, TypeError, ValueError):
                continue
            if not 1 <= m <= 12:
                continue
            if price <= 0:
                continue  # 站点空值/无效数据
            if (y, m) in have:
                continue
            if y == cur_year and m >= cur_month:
                continue
            rows[(y, m)] = price
    return rows


def crawl_mode(years_from=2021, only_cities=None, sleep_s=3.0, full=False,
               block_wait_s=600, max_block_cycles=6):
    """全量自动爬取: 主域 fangjia/{slug}{year}/ → NUXT trendList → per-city 文件。

    限流自保护:
    - 每请求间隔 sleep_s + 随机抖动(默认3s)，被拦截时退避
    - 命中挑战页时整体暂停 block_wait_s 再续(最多 max_block_cycles 轮),
      进度按城市落盘, 中断后重跑只补缺失月份(只增不改)
    - 增量语义: 旧年份(12个月齐全)默认跳过, 只爬当前年滚动窗口; --full 强制全量
    """
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    today = date.today()
    cur_year, cur_month = today.year, today.month
    targets = {k: v for k, v in CITY_SLUG_70.items()
               if not only_cities or k in only_cities}
    years = list(range(years_from, cur_year + 1))

    print("=" * 70)
    print(f"安居客70城自动爬取 ({len(targets)}城 × {years[0]}-{years[-1]} 年页, 间隔{sleep_s}s)")
    print("策略: 低频访问; 命中挑战页自动退避; 只增不改, 可断点续爬")
    print("=" * 70)

    total_added = total_conflicts = fails = 0
    block_cycles = 0
    skipped_pages = 0
    for i, (city, slug) in enumerate(sorted(targets.items()), 1):
        if city in CITY_DATA_BUG:
            print(f"[{i:>2}/{len(targets)}] {city}: 跳过 ({CITY_DATA_BUG[city]})")
            continue
        have = existing_year_months(slug)

        # 该次要抓的年份页: 旧年份已有12个月则跳过(增量), 当前年始终刷新
        fetch_years = []
        for y in years:
            if y < cur_year and not full and \
                    sum(1 for (yy, _) in have if yy == y) >= 12:
                skipped_pages += 1
                continue
            fetch_years.append(y)

        year_pages = {}
        page_evidence = {}
        blocked_now = False
        for y in fetch_years:
            got = {}
            # 候选URL依次尝试(主域年份页 → 无年份聚合页 → 子域),
            # 直到拿到有效价格行: 部分城市主域年份页是全 0 的空壳(如 上海/成都),
            # 真实数据在无年份聚合页(每年约5个月)或子域
            for url in city_page_urls(city, slug, y):
                html = fetch_fangjia_page(url)
                if html is None:
                    time.sleep(sleep_s * 0.5)
                    continue
                info = extract_trend(html)
                if info is None:
                    # 小页/挑战页漏网: 退避后重试一次
                    blocked_now = True
                    break
                got = collect_rows(info, cur_year, cur_month, have)
                if info.get('city') and info['city'] != city:
                    print(f"    [WARN] {url.rsplit('/', 2)[-2]} 页面城市为 {info['city']}, 与目标 {city} 不符, 跳过该页")
                    got = {}
                if got:
                    from data_store import snapshot
                    raw=snapshot('anjuke_pages',html.encode('utf-8'),'raw','html')
                    captured=datetime.now(timezone.utc).isoformat()
                    page_evidence.update({key:dict(source_url=url,fetched_at=captured,raw=raw) for key in got})
                    break
                time.sleep(sleep_s + random.uniform(0, 1.5))
            year_pages[y] = got

        if blocked_now:
            block_cycles += 1
            if block_cycles > max_block_cycles:
                print(f"[{i:>2}/{len(targets)}] {city}: 拦截轮次达上限, 暂停退出"
                      f"(已入库存进度, 稍后重跑即可续爬)")
                fails += 1
                break
            wait = min(block_wait_s * block_cycles, 1800)
            print(f"[{i:>2}/{len(targets)}] {city}: 命中WAF挑战页, "
                  f"退避 {wait}s 后继续 (第{block_cycles}/{max_block_cycles}轮)")
            time.sleep(wait)
            # 退避后重抓当前城市一轮
            year_pages = {}
            for y in fetch_years:
                html = fetch_city_year(city, slug, y)
                if html is None:
                    continue
                info = extract_trend(html)
                if info is None:
                    continue
                year_pages[y] = collect_rows(info, cur_year, cur_month, have)
                if info.get('city') and info['city'] != city:
                    year_pages[y] = {}
                time.sleep(sleep_s + random.uniform(0, 1.5))

        rows = {}
        for y in years:
            rows.update(year_pages.get(y) or {})
        if not rows and not year_pages:
            print(f"[{i:>2}/{len(targets)}] {city}: 无新增数据")
            fails += 1
            continue
        total, added, conflicts = merge_city_rows(slug, city, rows, page_evidence)
        total_added += added
        total_conflicts += conflicts
        new_latest = max(rows) if rows else None
        print(f"[{i:>2}/{len(targets)}] {city}: 新增 {added} 条 (冲突保留 {conflicts}), "
              f"共 {total} 条" + (f", 至 {new_latest[0]}-{new_latest[1]:02d}" if new_latest else ""))

    print(f"\n完成: 新增 {total_added} 条, 口径冲突保留 {total_conflicts} 条, "
          f"跳过已完成年页 {skipped_pages} 个, 无数据城市 {fails} 个")
    print(f"输出: {OUTPUT_DIR}/<城市slug>.json (与逆推引擎 schema 一致)")


def generate_urls():
    """生成所有70城的安居客URL列表"""
    urls = {}
    for city, slug in CITY_SLUG_70.items():
        # 检查是否是二级域名城市
        if city in SUBDOMAIN_CITIES:
            sub = SUBDOMAIN_CITIES[city]["subdomain"]
            for year in YEARS:
                urls[f"{city}_{year}"] = {
                    "city": city, "slug": slug, "year": year,
                    "url": f"https://{sub}.anjuke.com/fangjia/{sub}{year}/",
                    "type": "subdomain",
                }
        else:
            for year in YEARS:
                urls[f"{city}_{year}"] = {
                    "city": city, "slug": slug, "year": year,
                    "url": f"https://www.anjuke.com/fangjia/{slug}{year}/",
                    "type": "normal",
                }
    return urls


def manual_mode():
    """手动模式: 生成所有URL供用户手动浏览"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    urls = generate_urls()

    # 按城市分组
    print("=" * 80)
    print("安居客70城房价URL列表 (手动浏览模式)")
    print("=" * 80)
    print(f"总计: {len(urls)} 个URL, {len(CITY_SLUG_70)} 个城市")
    print(f"每个城市需要访问5个年份页面 (2021-2025)")
    print()

    # 输出URL清单文件
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    url_list_path = OUTPUT_DIR / "anjuke_url_list.json"
    with open(url_list_path, 'w', encoding='utf-8') as f:
        json.dump(urls, f, ensure_ascii=False, indent=2)
    print(f"URL清单保存到: {url_list_path}")

    # 输出基准价格参考
    ref_path = OUTPUT_DIR / "base_price_reference.json"
    with open(ref_path, 'w', encoding='utf-8') as f:
        json.dump(BASE_PRICE_REF_2021Q1, f, ensure_ascii=False, indent=2)
    print(f"基准价格参考保存到: {ref_path}")

    # 精简输出: 每个城市一行, 所有年份URL
    for city, slug in sorted(CITY_SLUG_70.items()):
        if city in SUBDOMAIN_CITIES:
            sub = SUBDOMAIN_CITIES[city]["subdomain"]
            url_template = f"https://{sub}.anjuke.com/fangjia/{sub}"
        else:
            url_template = f"https://www.anjuke.com/fangjia/{slug}"
        print(f"  {city}: {url_template}{{year}}/  (2021-2025)")

    # 输出省份URL
    print("\n省份导航页面:")
    for slug, name in sorted(PROVINCES.items()):
        print(f"  {name}: https://www.anjuke.com/fangjia/{slug}/")

    # 生成省份页面URL文件
    province_urls = {}
    for slug, name in PROVINCES.items():
        province_urls[slug] = {
            "name": name,
            "url": f"https://www.anjuke.com/fangjia/{slug}/",
        }
    province_path = OUTPUT_DIR / "province_urls.json"
    with open(province_path, 'w', encoding='utf-8') as f:
        json.dump(province_urls, f, ensure_ascii=False, indent=2)
    print(f"省份URL保存到: {province_path}")


def auto_mode():
    """自动模式: 尝试自动爬取 (可能被WAF拦截)"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

    print("=" * 80)
    print("安居客省份城市爬虫 (自动模式)")
    print("=" * 80)

    success_count = 0
    waf_count = 0
    error_count = 0

    # 先尝试总页面
    print("\n[1] 尝试获取总页面...")
    html = try_urllib_fetch("https://www.anjuke.com/fangjia/")
    if html and "antibot" not in html.lower():
        print("  成功! 提取省份链接...")
        province_links = extract_city_links(html, "")
        for link in province_links:
            print(f"  省份: {link['name']} → {link['url']}")
        success_count += 1
    else:
        print("  WAF拦截! 无法自动获取总页面")
        print("  建议: 切换到手动模式或Playwright模式")
        waf_count += 1

    # 尝试省份页面
    print("\n[2] 尝试获取省份页面...")
    for slug, name in list(PROVINCES.items())[:3]:  # 只测试前3个
        url = f"https://www.anjuke.com/fangjia/{slug}/"
        print(f"  尝试: {name} ({url})")
        html = try_urllib_fetch(url)
        if html and "antibot" not in html.lower():
            city_links = extract_city_links(html, slug)
            print(f"  成功! 发现 {len(city_links)} 个城市")
            for link in city_links:
                print(f"    {link['name']} → {link['slug']}")
            success_count += 1
        else:
            print(f"  WAF拦截")
            waf_count += 1
        time.sleep(2)

    # 尝试城市年份页面
    print("\n[3] 尝试获取城市价格页面...")
    test_cities = [("深圳", "shenzhen"), ("广州", "guangzhou"), ("温州", "wenzhou")]
    for city, slug in test_cities:
        url = f"https://www.anjuke.com/fangjia/{slug}2025/"
        print(f"  尝试: {city} 2025 ({url})")
        html = try_urllib_fetch(url)
        if html and "antibot" not in html.lower():
            prices = extract_price_data(html)
            print(f"  成功! 发现 {len(prices)} 条价格数据")
            success_count += 1
        else:
            print(f"  WAF拦截")
            waf_count += 1
        time.sleep(2)

    print(f"\n结果: 成功={success_count}, WAF拦截={waf_count}, 错误={error_count}")
    if waf_count > success_count:
        print("WAF拦截过多，建议使用手动模式:")
        print("  python anjuke_province_crawler.py --manual")


def playwright_mode():
    """兼容旧入口：无头采集，遇到验证页保留旧数据并结束。"""
    return crawl_browser_mode()


def convert_to_city_files(collected_data):
    """把 playwright 采集的 {city_year: {city, slug, year, prices[]}}
    合并转换为逆推引擎读取的 per-city 文件格式。返回写出的文件数。"""
    merged = {}  # slug -> {"city":…, "rows": {(year,month): price}}
    for key, block in collected_data.items():
        city, slug, year = block["city"], block["slug"], block["year"]
        entry = merged.setdefault(slug, {"city": city, "rows": {}})
        for p in block.get("prices", []):
            entry["rows"][(year, int(p["month"]))] = int(p["price"])

    written = 0
    for slug, entry in merged.items():
        out_path = OUTPUT_DIR / f"{slug}.json"
        existing = {}
        if out_path.exists():
            try:
                for r in json.loads(out_path.read_text(encoding='utf-8')):
                    existing[(int(r.get("year")), int(r.get("month")))] = r
            except Exception:
                existing = {}
        for (y, m), price in entry["rows"].items():
            existing[(y, m)] = {
                "city": entry["city"], "city_pinyin": slug,
                "year": y, "month": m, "avg_price": price,
                "data_source": "anjuke",
            }
        rows = [existing[k] for k in sorted(existing)]
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        written += 1
    return written


def main():
    args = sys.argv[1:]
    if not args:
        print("用法:")
        print("  python anjuke_province_crawler.py --crawl                # 自动爬取70城(需 Node.js，失败保留旧值)")
        print("  python anjuke_province_crawler.py --crawl-browser      # 浏览器兜底(子域城市, 云端 Chromium/远程浏览器)")
        print("  python anjuke_province_crawler.py --crawl --city 北京,温州  # 只爬指定城市")
        print("  python anjuke_province_crawler.py --crawl --from-year 2023 # 起始年份(默认2021)")
        print("  python anjuke_province_crawler.py --manual               # 手动模式(输出URL列表)")
        print("  python anjuke_province_crawler.py --playwright           # 无头浏览器模式(验证页停止)")
        return

    mode = args[0]
    if mode == "--crawl":
        only = None
        from_year = 2021
        full = False
        if "--city" in args:
            only = set(args[args.index("--city") + 1].split(","))
        if "--from-year" in args:
            from_year = int(args[args.index("--from-year") + 1])
        full = "--full" in args
        crawl_mode(years_from=from_year, only_cities=only, full=full)
    elif mode in ("--crawl-browser", "--playwright"):
        only = None
        from_year = 2021
        if "--city" in args:
            only = set(args[args.index("--city") + 1].split(","))
        if "--from-year" in args:
            from_year = int(args[args.index("--from-year") + 1])
        crawl_browser_mode(years_from=from_year, only_cities=only)
    elif mode == "--auto":
        auto_mode()
    elif mode == "--manual":
        manual_mode()
    else:
        print(f"未知模式: {mode}")
        print("可选: --crawl, --auto, --manual, --playwright")


if __name__ == "__main__":
    main()
