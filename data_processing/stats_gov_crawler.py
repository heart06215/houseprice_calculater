# -*- coding: utf-8 -*-
"""
国家统计局 70 城房价数据自动爬取/更新器
========================================
职责: 保持 output/stats_gov/stats_gov_YYYY-MM.json 与统计局最新发布同步。

设计要点:
- 请求使用标准库，文章元信息解析使用 beautifulsoup4；访问国家统计局 www.stats.gov.cn。
  贝壳/安居客反爬极强且需人工验证码, 不在本脚本的自动范围内。
- 每月一期发布《XXXX年X月份70个大中城市商品住宅销售价格变动情况》,
  约 15-20 日发布上月数据。列表页 index.html / index_N.html 可翻页。
- 输出 schema 与历史文件完全兼容, inverse_price_engine 直接消费:
  每月一个数组文件, 记录含 city/report_month/table_type/area_type/环比指数等。
- 修复历史缺陷: 合计表每行含左右两个城市, 旧爬虫只解析左列(仅35城有合计,
  其余35城只能用分面积均值兜底); 本爬虫两列都解析, 全部 70 城有真实合计。
- 自动更新由 update_sources.py 调用；每次到期检查发布目录，失败保留旧数据。
  报告期取自文章标题和正文，不使用检查日期；尚未发布时下次仍检查目录。

用法:
  python stats_gov_crawler.py                  # 增量: 补齐所有缺失月份(默认)
  python stats_gov_crawler.py --check          # 只报告缺失月份, 不抓取
  python stats_gov_crawler.py --backfill       # 重爬全部已有月份(修复旧文件35城缺口)
  python stats_gov_crawler.py --months 2026-03,2026-04
  python stats_gov_crawler.py --max-pages 60
"""

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import hashlib


class ArticleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            a = dict(attrs)
            self.current = [a.get('href', ''), a.get('title', '')]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATS_DIR = PROJECT_ROOT / "output" / "stats_gov"

BASE = "https://www.stats.gov.cn/sj/zxfb/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 数据起点: 项目逆推锚点为 2021-04 基准价
FIRST_MONTH = (2021, 4)

# 文章标题 → (y, m)
TITLE_RE = re.compile(r"(\d{4})年(\d{1,2})月份70个大中城市商品住宅销售价格变动情况")

# 列表页文章链接: href="./202608/t20260817_1965050.html" title='...'
LIST_ITEM_RE = re.compile(r"href=\"(\./20\d{4}/t\d+_\d+\.html)\"[^>]*title='([^']+)'")

# 文章内 6 张表的标题(每张标题出现两次, 取第一处, 之后最近的 <table>)
TABLE_TITLES = [
    ("个大中城市新建商品住宅销售价格指数", "new_house", "total"),
    ("个大中城市二手住宅销售价格指数", "second_hand", "total"),
    ("个大中城市新建商品住宅销售价格分类指数（一）", "new_house_by_area", "area1"),
    ("个大中城市新建商品住宅销售价格分类指数（二）", "new_house_by_area", "area2"),
    ("个大中城市二手住宅销售价格分类指数（一）", "second_hand_by_area", "area1"),
    ("个大中城市二手住宅销售价格分类指数（二）", "second_hand_by_area", "area2"),
]
AREA_TYPES = ["90m2及以下", "90-144m2", "144m2以上"]


# ============================================================
# HTTP
# ============================================================
_HTTP_KW = {}  # quick_update 设置的临时超时/重试覆盖


def http_get(url, timeout=None, retries=None):
    """带 UA 与 SSL 宽松回退的 GET(仅用于公开政府数据站点)。"""
    timeout = timeout or _HTTP_KW.get('timeout', 25)
    retries = retries if retries is not None else _HTTP_KW.get('retries', 2)
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # stats.gov.cn 偶尔用 404 软性限流: 延迟后重试一次再定论
                if attempt < retries:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"HTTP 404: {url}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
        # SSL 证书校验失败时降级重试一次(公开数据站点, 不涉及敏感信息)
        if isinstance(last_err, urllib.error.URLError) and \
                isinstance(getattr(last_err, "reason", None), Exception) and \
                "CERTIFICATE_VERIFY_FAILED" in str(last_err.reason):
            try:
                ctx = ssl_unverified()
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except Exception as e2:
                last_err = e2
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def ssl_unverified():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ============================================================
# 列表页 → 文章索引
# ============================================================
def discover_articles(max_pages=40, stop_before=None, timeout=25, retries=2):
    """翻列表页收集 {(y, m): 绝对URL}。

    stop_before: (y, m) — 当列表中出现的 70 城文章早于该月时提前停止翻页。
    timeout/retries: 启动快速模式应传短超时, 避免离线时卡住打开流程。
    """
    found = {}
    current = datetime.now(timezone(timedelta(hours=8)))
    for page in range(max_pages):
        url = BASE + ("index.html" if page == 0 else f"index_{page}.html")
        try:
            html = http_get(url, timeout=timeout, retries=retries)
        except RuntimeError as e:
            if page == 0:
                raise
            if "404" in str(e):
                break  # 翻过最后一页
            continue  # 临时失败(超时/5xx): 跳过该页继续, 避免误判为列表结束
        parser = ArticleLinks()
        parser.feed(html)
        links = parser.links
        oldest = None
        for href, title in links:
            m = TITLE_RE.search(title)
            if not m:
                continue
            y, mo = int(m.group(1)), int(m.group(2))
            abs_url = urljoin(url, href)
            if urlparse(abs_url).hostname != 'www.stats.gov.cn':
                continue
            if not 1 <= mo <= 12 or (y, mo) >= (current.year, current.month):
                continue
            if (y, mo) not in found:
                found[(y, mo)] = abs_url
            if oldest is None or (y, mo) < oldest:
                oldest = (y, mo)
        if not links:
            break  # 整页无文章 → 已翻过最后一页
        if stop_before and oldest and oldest < stop_before:
            break
        time.sleep(0.3)
    return found


# ============================================================
# 文章解析
# ============================================================
def _clean_city(raw):
    """表格显示名 → 标准城市名(去全角/半角空格)。"""
    return re.sub(r"[\s\u3000]+", "", raw or "")


def _cells(tr_html):
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr_html, re.S)
    out = []
    for c in cells:
        c = re.sub(r"<[^>]+>", "", c)
        # 部分页面用 &nbsp; 实体分隔城市名(如 哈&nbsp;尔&nbsp;滨), 解码后由 _clean_city 去除
        out.append(html_lib.unescape(c).strip())
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _table_after(html, marker):
    """取 marker 标题之后最近的 <table>...</table>。"""
    pos = html.find(marker)
    if pos < 0:
        return None
    m = re.compile(r"<table[^>]*>.*?</table>", re.S).search(html, pos)
    return m.group(0) if m else None


def _rows(table_html):
    return re.findall(r"<tr[^>]*>(.*?)</tr>", table_html or "", re.S)


def _first_data_row_len(rows):
    """从第一条数据行推断整表列数(表头行有合并单元格, 不可靠)。

    返回 0 表示无数据行。
    """
    for tr in rows:
        c = _cells(tr)
        if _is_data_row(c):
            return len(c)
    return 0


def _is_data_row(c):
    """数据行: 首格是城市名、次格是数值(单格残行/表头行均不算)。"""
    return (len(c) >= 2 and bool(_clean_city(c[0]))
            and bool(re.match(r"^[\d.]+$", c[1] or "")))


def parse_article(html):
    """解析文章 HTML → 记录列表(schema 与历史文件一致)。

    列布局自适应(以首条数据行为准):
      合计表:   每城 [城市,环比,同比(,1-M月平均)], 双城并排 → 行长 6 或 8
      分类指数表: 每面积组 [环比,同比(,1-M月平均)] ×3 组 + 城市名 → 行长 7 或 10
      每年 1 月份发布没有"1-M月平均"列, 其余月份有。
    """
    tables = {}
    for marker, ttype, part in TABLE_TITLES:
        tbl = _table_after(html, marker)
        if tbl is None and "（" in marker:
            # 兼容半角括号的旧页面
            tbl = _table_after(html, marker.replace("（", "(").replace("）", ")"))
        tables[(ttype, part)] = tbl

    for key in [("new_house", "total"), ("second_hand", "total"),
                ("new_house_by_area", "area1"), ("second_hand_by_area", "area1")]:
        if not tables.get(key):
            raise ValueError(f"文章缺少表格: {key}")

    records = []  # 每项: (city, table_type, area_type, mom, yoy, annual, raw_cells)

    # —— 合计表: 每行并排两个城市 ——
    for ttype in ("new_house", "second_hand"):
        rows = _rows(tables[(ttype, "total")])
        n = _first_data_row_len(rows)
        if not n:
            raise ValueError(f"{ttype} 合计表无数据行")
        width = 4 if n >= 8 else 3  # 每城占 4 列(含平均) 或 3 列(仅环比/同比)
        for tr in rows:
            c = _cells(tr)
            if not _is_data_row(c):
                continue
            for half in range(2):
                off = half * width
                if len(c) < off + width:
                    continue
                city = _clean_city(c[off])
                mom = _f(c[off + 1])
                yoy = _f(c[off + 2])
                avg = _f(c[off + 3]) if width == 4 else None
                if city and mom is not None:
                    records.append((city, ttype, "合计", mom, yoy, avg, c[off:off + width]))

    # —— 分面积表: 两张拼齐 70 城 ——
    for ttype in ("new_house_by_area", "second_hand_by_area"):
        rows = _rows(tables[(ttype, "area1")]) + _rows(tables[(ttype, "area2")])
        n = _first_data_row_len(rows)
        if not n:
            raise ValueError(f"{ttype} 分类指数表无数据行")
        width = (n - 1) // 3  # 每面积组 3 列(含平均) 或 2 列(仅环比/同比)
        if width < 2 or width > 3:
            raise ValueError(f"{ttype} 分类指数表列数异常: {n}")
        for tr in rows:
            c = _cells(tr)
            if not _is_data_row(c):
                continue
            city = _clean_city(c[0])
            for i, area in enumerate(AREA_TYPES):
                off = 1 + i * width
                seg = c[off: off + width]
                if not seg:
                    continue
                mom = _f(seg[0])
                yoy = _f(seg[1]) if len(seg) > 1 else None
                avg = _f(seg[2]) if len(seg) > 2 else None
                if mom is None:
                    continue
                records.append((city, ttype, area, mom, yoy, avg, list(c)))

    return records


def parse_article_url_date(url):
    """从文章 URL 提取发布日期, 如 /202603/t20260316_x.html → 2026-03-16。"""
    m = re.search(r"/20(\d{2})(\d{2})/t20(\d{2})(\d{2})(\d{2})_", url)
    if m:
        return f"20{m.group(3)}-{m.group(4)}-{m.group(5)}"
    return ""


def build_month_records(y, m, url, html, crawl_time):
    month = f"{y}-{m:02d}"
    title_match = re.search(r"<title>([^<]*)</title>", html)
    title = (title_match.group(1).strip() if title_match else
             f"{y}年{m}月份70个大中城市商品住宅销售价格变动情况")
    publish_date = parse_article_url_date(url)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    meta = soup.find('meta', attrs={'name': 'PubDate'})
    if meta:
        candidate = meta.get('content', '')[:10].replace('/', '-')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', candidate):
            publish_date = candidate
    report = re.search(r'(\d{4})年\s*(\d{1,2})月份', title)
    if report and tuple(map(int, report.groups())) != (y, m):
        raise ValueError('Article report month differs from requested month')

    out = []
    for city, ttype, area, mom, yoy, avg, raw in parse_article(html):
        rec = {
            "title": title,
            "publish_date": publish_date,
            "url": url,
            "category": "70城房价指数",
            "report_month": month,
            "city": city,
            "table_type": ttype,
            "area_type": area,
            "content_text": "",
            "table_data": json.dumps(raw, ensure_ascii=False),
            "new_house_mom": None, "new_house_yoy": None, "new_house_annual": None,
            "second_hand_mom": None, "second_hand_yoy": None, "second_hand_annual": None,
            "crawl_time": crawl_time,
            "data_source": "stats_gov",
        }
        prefix = "new_house" if ttype.startswith("new_house") else "second_hand"
        rec[f"{prefix}_mom"] = mom
        if yoy is not None:
            rec[f"{prefix}_yoy"] = yoy
        if avg is not None:
            rec[f"{prefix}_annual"] = avg
        out.append(rec)
    return out


# ============================================================
# 本地文件
# ============================================================
def month_file(y, m):
    return STATS_DIR / f"stats_gov_{y}-{m:02d}.json"


def existing_months():
    months = set()
    if STATS_DIR.exists():
        for f in STATS_DIR.glob("stats_gov_*.json"):
            m = re.match(r"stats_gov_(\d{4})-(\d{2})\.json$", f.name)
            if m:
                months.add((int(m.group(1)), int(m.group(2))))
    return months


def validate_records(records):
    """返回两类住宅合计覆盖数；完整 560 行闸门由 official_month 检查。"""
    sh = {r["city"] for r in records
          if r["table_type"] == "second_hand" and r["area_type"] == "合计"
          and r["second_hand_mom"] is not None}
    nh = {r["city"] for r in records
          if r["table_type"] == "new_house" and r["area_type"] == "合计"
          and r["new_house_mom"] is not None}
    return len(sh), len(nh)


def old_file_is_defective(old_records):
    """检测旧爬虫缺陷: 新房/二手表数值完全相同。

    旧爬虫只解析了"新建商品住宅"两张表, 却把同一份数据贴了 new_house 与
    second_hand 两个标签(真正二手住宅环比从未入库)。逆推引擎消费
    second_hand_mom, 因此该缺陷会让历史逆推使用新房环比。检测到即允许
    回填覆盖修正。
    """
    nh, sh = {}, {}
    for r in old_records:
        if not r.get("city"):
            continue
        v = r.get("new_house_mom")
        if v is not None:
            nh[(r["city"], r.get("area_type"))] = float(v)
        v = r.get("second_hand_mom")
        if v is not None:
            sh[(r["city"], r.get("area_type"))] = float(v)
    common = set(nh) & set(sh)
    if not common:
        return False
    same = sum(1 for k in common if abs(nh[k] - sh[k]) < 0.001)
    return same >= len(common) * 0.95


def values_overlap_conflict(new_records, old_records):
    """合计值重叠比对(安全闸门): 有冲突返回 True。

    只比对"合计"(逆推引擎实际消费的数据)。分面积列不比对: 2021-2022 年旧文件
    存在已知列错位缺陷(把上一面积组的"平均"列读成下一组的"环比"), 那部分旧值
    本来就是错的, 不应作为覆盖的保护依据。
    """
    old_map = {}
    for r in old_records:
        if not r.get("city") or r.get("area_type") != "合计":
            continue
        for pfx in ("new_house", "second_hand"):
            v = r.get(f"{pfx}_mom")
            if v is not None:
                old_map[(r["city"], r.get("table_type"), r.get("area_type"))] = float(v)
    conflicts = 0
    checked = 0
    for r in new_records:
        if r.get("area_type") != "合计":
            continue
        for pfx in ("new_house", "second_hand"):
            v = r.get(f"{pfx}_mom")
            key = (r["city"], r["table_type"], r["area_type"])
            if r.get("city") and v is not None and key in old_map:
                checked += 1
                if abs(float(v) - old_map[key]) > 0.001:
                    conflicts += 1
    return checked > 0 and conflicts > 0


def write_month(y, m, url, html, crawl_time, overwrite=False):
    """抓取并写单月文件; 返回 (status, n_records)。status: written/kept/failed"""
    records = build_month_records(y, m, url, html, crawl_time)
    if not records:
        return "failed", 0
    n_sh, n_nh = validate_records(records)
    from release_contract import official_month
    try:
        official_month(records, f'{y}-{m:02d}')
    except ValueError as exc:
        print(f"  [WARN] {y}-{m:02d} 解析不完整(二手合计{n_sh}城/新房合计{n_nh}城), 保留原文件")
        print('  ' + str(exc))
        return "failed", len(records)

    target = month_file(y, m)
    if target.exists():
        if not overwrite:
            return "kept", len(records)
        try:
            old = json.load(open(target, encoding="utf-8"))
        except Exception:
            old = []
        if old_file_is_defective(old):
            print(f"  [修复] {y}-{m:02d} 旧文件存在 新房/二手 表混淆缺陷, 用正确解析覆盖")
        elif values_overlap_conflict(records, old):
            print(f"  [WARN] {y}-{m:02d} 与历史数据存在数值冲突, 保留原文件")
            return "kept", len(records)

    STATS_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir = STATS_DIR.parent / 'raw' / 'stats_gov'
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(html.encode('utf-8')).hexdigest()
    raw_path = raw_dir / f'{y}-{m:02d}_{digest[:12]}.html'
    raw_path.write_text(html, encoding='utf-8')
    for row in records:
        row['raw_sha256'] = digest
        row['raw_file'] = raw_path.relative_to(PROJECT_ROOT).as_posix()
    temporary = target.with_suffix('.json.tmp')
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    temporary.replace(target)
    return "written", len(records)


# ============================================================
# 入口
# ============================================================
def quick_update(max_pages=4, timeout=8, retries=0, details=False):
    """每次检查发布目录并补采新报告；details 返回报告期与等待发布状态。

    短超时与有限翻页限制单次检查；失败交给更新器保留历史并记录错误。
    """
    global _HTTP_KW
    _HTTP_KW = {"timeout": timeout, "retries": retries}
    try:
        return _run(max_pages=max_pages, backfill=False, details=details)
    finally:
        _HTTP_KW = {}


def publication_state(articles, have, written=(), at=None):
    """检查时间只确定等待月份，数据时期始终来自实际发布及已存报告。"""
    today = (at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    expected = (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    def period(month):
        return f'{month[0]:04d}-{month[1]:02d}'
    available = period(max(articles)) if articles else None
    stored = period(max(have)) if have else None
    waiting = stored is not None and available is not None and stored < expected and available <= stored
    availability = 'awaiting_release' if waiting else 'available'
    if available is None:
        availability = 'unknown'
    elif stored is None or stored < available:
        availability = 'pending_collection'
    return dict(data_period=stored, published_period=available,
                expected_period=expected, availability=availability,
                data_changed=bool(written), added_periods=list(written))


def _run(max_pages=40, backfill=False, months=None, check_only=False, details=False):
    have = existing_months()
    earliest_need = min(have) if have else FIRST_MONTH
    need_backfill = sorted(have) if backfill else []
    crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if months:
        targets = []
        for s in months:
            mm = re.match(r"(\d{4})-(\d{1,2})$", s.strip())
            if mm:
                targets.append((int(mm.group(1)), int(mm.group(2))))
        if backfill:
            need_backfill = list(targets)  # --months --backfill: 只重爬指定月份
        if check_only:
            print("[stats-gov] 待抓取: " + ", ".join(
                f"{y}-{m:02d}" for y, m in targets if (y, m) not in have))
            return "check"
        articles = discover_articles(max_pages=max_pages)
    else:
        print(f"[stats-gov] 正在检查国家统计局最新发布 (已存 {len(have)} 期)...")
        articles = discover_articles(max_pages=max_pages, timeout=_HTTP_KW.get('timeout', 25), retries=_HTTP_KW.get('retries', 2),
                                     stop_before=None if backfill else earliest_need)
        targets = sorted(a for a in articles if a not in have and a >= FIRST_MONTH)
        if backfill:
            targets = sorted(set(targets) | set(need_backfill))

        # 提示列表页前 max_pages 页没有覆盖到的更早缺失月份(可手动 --months 补)
        if have:
            end = max(set(targets) | have) if targets else max(have)
            full_range = []
            y, m = min(have)
            while (y, m) <= end:
                full_range.append((y, m))
                m += 1
                if m > 12:
                    y, m = y + 1, 1
            missing_inner = [mm for mm in full_range if mm not in have and mm not in targets]
            if missing_inner:
                print(f"[stats-gov] [WARN] 前{max_pages}页列表未覆盖的更早缺失月份: "
                      + ", ".join(f"{y}-{m:02d}" for y, m in missing_inner))

        if check_only:
            if not targets:
                latest = max(have) if have else None
                print(f"[stats-gov] 数据已是最新 (最新一期: "
                      f"{latest[0]}-{latest[1]:02d})" if latest
                      else "[stats-gov] [WARN] 未发现任何统计局数据")
            else:
                print(f"[stats-gov] 缺失 {len(targets)} 期: "
                      + ", ".join(f"{y}-{m:02d}" for y, m in targets))
            return "check"

    if not articles:
        raise RuntimeError('统计局目录未返回可识别的月报，不能判定已是最新；保留旧数据')
    if not targets:
        latest = max(have) if have else None
        print(f"[stats-gov] 已存目录最新发布 (报告期: {latest[0]}-{latest[1]:02d})" if latest
              else "[stats-gov] [WARN] 未发现任何统计局数据")
        report = publication_state(articles, have)
        return dict(report, result='up-to-date') if details else "up-to-date"

    print(f"[stats-gov] 发现 {len(targets)} 期待处理: "
          + ", ".join(f"{y}-{m:02d}" for y, m in targets))

    written, kept, failed = [], [], []
    for (y, m) in targets:
        url = articles.get((y, m))
        if not url:
            failed.append((y, m, "列表中未找到该期文章(可能过旧或尚未发布)"))
            continue
        try:
            html = http_get(url)
            status, n = write_month(y, m, url, html, crawl_time,
                                    overwrite=(y, m) in need_backfill)
            tag = f"{y}-{m:02d}"
            if status == "written":
                written.append(tag)
                print(f"  [完成] {tag}: 新写入 {n} 条记录 ← {url}")
            elif status == "kept":
                kept.append(tag)
            else:
                failed.append((y, m, '解析未通过完整性检查'))
            time.sleep(0.8)
        except Exception as e:
            failed.append((y, m, str(e)[:100]))

    have = existing_months()
    latest = max(have) if have else None
    if written:
        print(f"[stats-gov] 本轮新增 {len(written)} 期: {', '.join(written)}; "
              f"数据现已覆盖至 {latest[0]}-{latest[1]:02d}")
    elif kept and not failed:
        print(f"[stats-gov] 数据已是最新 (最新一期: {latest[0]}-{latest[1]:02d})")
    if failed:
        for (y, m), why in [(f[:2], f[2]) for f in failed]:
            print(f"  [WARN] {y}-{m:02d} 抓取失败: {why}")
        print(f"[stats-gov] [WARN] {len(failed)} 期处理失败, 已有数据不受影响")
    result = "partial" if failed else "ok"
    return dict(publication_state(articles, have, written), result=result) if details else result


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="国家统计局70城房价数据自动爬取/更新器")
    parser.add_argument("--check", action="store_true", help="只检查缺失, 不抓取")
    parser.add_argument("--backfill", action="store_true",
                        help="重爬全部已有月份, 修复旧文件只解析35城合计的缺陷")
    parser.add_argument("--months", type=str, default="",
                        help="指定月份, 逗号分隔, 如 2026-03,2026-04")
    parser.add_argument("--max-pages", type=int, default=40, help="列表页最多翻页数")
    args = parser.parse_args()

    print("=" * 60)
    print("  国家统计局 70 城房价数据 · 自动更新")
    print("=" * 60)
    months = [s for s in args.months.split(",") if s.strip()] or None
    _run(max_pages=args.max_pages, backfill=args.backfill,
         months=months, check_only=args.check)


if __name__ == "__main__":
    main()
