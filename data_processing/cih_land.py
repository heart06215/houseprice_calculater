# -*- coding: utf-8 -*-
"""中指土地页（data/land.html）宅地数据采集：Playwright 读渲染 DOM，按周期增量。

页面为 Vue 单页应用，接口响应经 yjy-web-crypto 加密——直接读站点自行渲染的
表格（站点自身解密），不复制其密钥逻辑；站点更换密钥/加密方式不影响本模块。

刷新游标控制请求频率，与数据所属统计时期分别记录：
  - 周游标取页面的 ISO 周标签；月游标取最近检查的日历月。
  - 两个游标都在当前周期，且已有供求统计时期 → 零请求，沿用已存数据。
  - 任一游标过期 → 一次 Playwright 会话抓全部区块；区块级内容哈希比对，
    未变化的区块保留原样（不重写、不改时间戳）。
  - 前端使用页面原文的 period，不使用刷新游标表示统计时期。

证据：每次实际抓取把渲染表格 HTML 存 output/cih/raw/（哈希命名），不保存截图。
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, date
from pathlib import Path

from data_store import ROOT, snapshot, atomic_json

LAND_URL = 'https://www.cih-index.com/data/land.html'
OUT = ROOT / 'output/cih/land.json'
RAW_DIR = ROOT / 'output/cih/raw'
SECTIONS = ('weekly_300', 'supply_22', 'top_deals')


def _iso_week_label(now=None):
    """与站点口径核对：2026-09-21~09.27 = 2026年第39周（ISO 周一开周）。"""
    d = (now or datetime.now(timezone.utc)).date()
    return '%d年第%d周' % (d.isocalendar()[0], d.isocalendar()[1])


def _current_month_label(now=None):
    d = (now or datetime.now(timezone.utc)).date()
    return '%d-%02d' % (d.year, d.month)


def _fresh(stored, now=None):
    """刷新游标是否处于当前周期，且已取得统计时期；并非本月统计已发布。"""
    if not stored:
        return False
    periods = stored.get('periods') or {}
    supply = (stored.get('sections') or {}).get('supply_22') or {}
    return (bool(supply.get('period')) and periods.get('week') == _iso_week_label(now)
            and periods.get('month') == _current_month_label(now))


def _clean(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


_EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const headings = [...document.querySelectorAll('h2, h3, .head-tool-tit')];
  const byTitle = {};
  for (const h of headings) {
    const t = clean(h.textContent);
    if (t) byTitle[t] = byTitle[t] || h;
  }
  const rowsOf = (tb) => [...tb.querySelectorAll('tr')].map(tr =>
      [...tr.querySelectorAll('th,td')].map(c => clean(c.textContent)));
  // 全表枚举 + 各自最近的区块标题（供签名分类兜底）
  const tables = [...document.querySelectorAll('table')].map((tb) => {
    let node = tb, title = '';
    for (let k = 0; k < 8 && node; k++) {
      node = node.parentElement;
      if (!node) break;
      const h = [...node.querySelectorAll('h2, h3, .head-tool-tit')][0];
      if (h) { title = clean(h.textContent); break; }
    }
    return { title, rows: rowsOf(tb) };
  });
  const out = {};
  const supply = byTitle['22城宅地供求'];
  const detail = byTitle['22城宅地供求明细'];
  if (supply && detail) {
    const range = document.createRange();
    range.setStartBefore(supply); range.setEndBefore(detail);
    const wrap = document.createElement('div');
    wrap.appendChild(range.cloneContents());
    out.supply_22_text = clean(wrap.textContent).slice(0, 600);
  }
  const w = byTitle['300城·宅地成交数据'];
  if (w) {
    let node = w;
    for (let k = 0; k < 4 && node; k++) node = node.parentElement;
    out.weekly_300_text = node ? clean(node.innerText).slice(0, 600) : '';
  }
  out.top_tabs = [...document.querySelectorAll('.el-tabs__item')]
      .map(el => clean(el.textContent)).filter(Boolean);
  const hIdx = headings.findIndex(h => clean(h.textContent) === '全国地块成交TOP10');
  const nextH = headings.slice(hIdx + 1).find(h => clean(h.textContent) === '中指报告·土地');
  if (hIdx >= 0 && nextH) {
    const range = document.createRange();
    range.setStartBefore(headings[hIdx]); range.setEndBefore(nextH);
    const wrap = document.createElement('div');
    wrap.appendChild(range.cloneContents());
    out.top_leaves = [...wrap.querySelectorAll('*')]
      .filter(el => el.children.length === 0 && clean(el.textContent))
      .map(el => clean(el.textContent));
  }
  out.all_tables = tables;
  out.title_texts = Object.keys(byTitle).slice(0, 20);
  return out;
}
"""

def _fetch_dom(timeout_s=60.0):
    """Playwright 会话：等待宅地表格渲染后按标题锚点提取。

    预算口径：CIH 源不在 4000 试验账本内（由 update_policy 周期+max_pages 约束）；
    此处每次刷新=一次页面导航，会话内观测到的全部网络尝试（文档/子资源）
    逐类计数并写入 evidence，保证透明可审计。"""
    from playwright.sync_api import sync_playwright
    attempts = {'document': 0, 'xhr_fetch': 0, 'script_style': 0, 'other': 0}
    with sync_playwright() as p:
        options = {'channel': 'msedge'} if sys.platform == 'win32' else {}
        browser = p.chromium.launch(headless=True, **options)
        page = browser.new_page(viewport={'width': 1440, 'height': 2400})

        def _count(req):
            t = req.resource_type
            if t == 'document':
                attempts['document'] += 1
            elif t in ('xhr', 'fetch'):
                attempts['xhr_fetch'] += 1
            elif t in ('script', 'stylesheet'):
                attempts['script_style'] += 1
            else:
                attempts['other'] += 1
        page.on('request', _count)
        try:
            page.goto(LAND_URL, wait_until='domcontentloaded', timeout=int(timeout_s * 1000))
            page.wait_for_selector('table tr', timeout=int(timeout_s * 1000))
            page.wait_for_timeout(4000)   # 让 Vue 完成数据渲染
            dom = page.evaluate(_EXTRACT_JS)
            dom['rendered_html_sha256'] = hashlib.sha256(
                page.locator('body').inner_html().encode('utf-8')).hexdigest()
            dom['network_attempts'] = dict(attempts)
            return dom
        finally:
            browser.close()


def _num(s):
    m = re.search(r'-?[\d,]+(?:\.\d+)?', str(s or '').replace(',', ''))
    return float(m.group(0)) if m else None


def build_sections(dom, now=None):
    """全表枚举 → 按内容签名分类（表头/数值行特征），解析失败保持 None 不补造。"""
    sections = {}
    week_label = _iso_week_label(now)
    month_label = _current_month_label(now)
    # 周度：300城宅地成交
    text = dom.get('weekly_300_text') or ''
    period_m = re.search(r'(\d{4}年第\d+周\s*[\d.]{5,12}-[\d.]{5,12})', text)
    stats = {}
    for key, pat in (('成交宗数_宗', r'成交宗数\s*([\d,]+)宗'),
                     ('规划建筑面积_万㎡', r'规划建筑面积\s*([\d,.]+)万㎡'),
                     ('成交楼面价_元㎡', r'成交楼面价\s*([\d,]+)元/㎡')):
        m = re.search(pat, text)
        stats[key] = _num(m.group(1)) if m else None
    sections['weekly_300'] = {
        'period': period_m.group(1) if period_m else None,
        'week_label': week_label,
        'stats': stats, 'raw_text': text[:400],
    }
    # 表签名分类
    agg_head = agg_value = det_head = det_data = None
    top_tables = []
    for t in dom.get('all_tables') or []:
        rows = [r for r in (t.get('rows') or []) if r]
        if not rows:
            continue
        joined = ''.join(r[0] or '' for r in rows[:2])
        full = ''.join(''.join(r) for r in rows[:2])
        if '推出土地宗数' in full and '22城明细' in full:
            det_head = rows
        elif '推出土地宗数' in full and '22城' in full and '绝对值' not in full:
            agg_head = rows
        elif rows[0][0] == '绝对值':
            agg_value = rows
        elif '地块名称' in full and '排名' in full:
            top_tables.append(rows)
        elif (len(rows) >= 2 and rows[0][0] != '绝对值'
              and re.match(r'^[一-龥]{2,4}$', rows[0][0] or '')):
            det_data = rows if det_data is None or len(rows) > len(det_data) else det_data
    agg_row = next((r for r in (agg_value or []) if r and '绝对值' in r[0]), None)
    head_row = (det_head or [None])[0] or (agg_head or [None])[0]
    cities = []
    for r in (det_data or []):
        if r and len(r) >= 9 and r[0] and not re.search(r'\d', r[0][:2]):
            cities.append({'city': r[0], 'values': [_num(x) for x in r[1:9]]})
    # 原始区块的统计时期与刷新游标 month 分开；不能用当前月份冒充累计时期。
    supply_text = dom.get('supply_22_text') or ''
    supply_period = re.search(r'(\d{4}年(0?[1-9]|1[0-2])(?:-(0?[1-9]|1[0-2]))?月)', supply_text)
    period = supply_period.group(1) if supply_period else None
    if supply_period and supply_period.group(3) and int(supply_period.group(3)) < int(supply_period.group(2)):
        period = None
    sections['supply_22'] = {
        'month': month_label,
        'period': period, 'source_url': LAND_URL,
        'headers': [_clean(x) for x in (head_row or [])[1:9]],
        'aggregate': {'label': (agg_row or ['绝对值'])[0],
                      'headers': [_clean(x) for x in ((agg_head or [[]])[0])[1:9]],
                      'values': [_num(x) for x in (agg_row or [])[1:9]]},
        'cities': cities,
    }
    tops = []
    for rows in top_tables:
        head_idx = next((k for k, r in enumerate(rows) if r and '排名' in ''.join(r)), 0)
        headers = [re.sub(r'\s+', '', x) for x in rows[head_idx]]
        entries = []
        for r in rows[head_idx + 1:]:
            if not r or not r[0]:
                continue
            entries.append({headers[i] if i < len(headers) else ('col%d' % i): v
                            for i, v in enumerate(r)})
        if entries:
            tops.append({'rows': entries})
    if not tops:
        # 组件数据行未随页面渲染（站点行为）：只记录表头与时期选项，rows 留空不补造
        leaves = dom.get('top_leaves') or []
        headers = [x for x in leaves if x in ('排名', '地块名称')
                   or '出让金' in x or '楼面价' in x]
        periods = [x for x in leaves
                   if re.match(r'^\d{4}年\d{1,2}(-\d{1,2})?月$', x)]
        sections['top_deals'] = {'tables': [], 'tabs': dom.get('top_tabs') or [],
                                 'headers': headers, 'period_options': periods,
                                 'note': '数据行未随页面渲染,未取得明细' if periods else None}
        return sections
    sections['top_deals'] = {'tables': tops[:2], 'tabs': dom.get('top_tabs') or []}
    return sections


def _content_hash(sections):
    """只对数据体哈希：剔除周期元数据（week_label/month），否则每期必判“变更”。"""
    normalized = {}
    for key, value in sections.items():
        if not isinstance(value, dict):
            normalized[key] = value
            continue
        cleaned = {k: v for k, v in value.items()
                   if k not in ('week_label', 'month')}
        normalized[key] = cleaned
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def refresh(*, force=False, now=None, fetch=None):
    """按周期检测：两者都新鲜且未 force → 零请求；否则一次抓取并区块级增量。

    返回 dict(state=ok|unchanged|failed, sections=..., evidence=...)。
    解析失败的字段保持 None 并如实返回 error；不覆盖已存好数据。
    """
    stored = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else None
    if not force and _fresh(stored, now=now):
        return {'state': 'unchanged', 'reason': 'stored periods cover current week/month',
                'periods': stored.get('periods'), 'path': str(OUT)}
    dom = (fetch or _fetch_dom)()
    tables = dom.get('all_tables') or []
    has_supply = any('推出土地宗数' in ''.join(''.join(r) for r in (t.get('rows') or [])[:2])
                     for t in tables)
    if not dom or not tables or not has_supply:
        return {'state': 'failed',
                'error': 'land page did not render expected tables',
                'table_count': len(tables), 'titles': dom.get('title_texts')}
    sections = build_sections(dom, now=now)
    supply = sections['supply_22']
    if (not supply['period'] or not supply['cities']
            or '成交楼面均价(元/㎡)' not in supply['headers']):
        return {'state': 'failed', 'error': 'land supply period or floor-price table missing; kept previous data'}
    now_iso = datetime.now(timezone.utc).isoformat()
    prev = (stored or {}).get('sections') or {}
    changed, details = [], {}
    for key in SECTIONS:
        new_hash = _content_hash({key: sections[key]})
        old_hash = _content_hash({key: prev[key]}) if key in prev else None
        if new_hash != old_hash:
            changed.append(key)
        details[key] = new_hash[:12]
    result = {'schema': 'cih_land/v1', 'fetched_at': now_iso,
              'periods': {'week': sections['weekly_300']['week_label'],
                          'month': sections['supply_22']['month']},
              'sections': sections, 'changed_sections': changed,
              'section_hashes': details}
    if changed:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(json.dumps(dom, ensure_ascii=False, sort_keys=True)
                             .encode('utf-8')).hexdigest()
        raw_path = RAW_DIR / f'land_dom_{sha[:16]}.json'
        raw_path.write_text(json.dumps(dom, ensure_ascii=False, indent=1), encoding='utf-8')
        result['evidence'] = {'raw_path': raw_path.relative_to(ROOT).as_posix(),
                              'raw_sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                              'raw': {'path': raw_path.relative_to(ROOT).as_posix(),
                                      'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest()},
                              'rendered_html_sha256': dom.get('rendered_html_sha256'),
                              'network_attempts': dom.get('network_attempts')}
        atomic_json(OUT, result)
        result['state'] = 'ok'
    else:
        # 内容未变：保留原文件与原时间戳（不重复发布）
        result['state'] = 'unchanged'
        result['path'] = str(OUT)
    return result


def load():
    return json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else None


def validate_evidence(record, root=ROOT):
    """复解析原始渲染表，核对累计时期、城市楼面价及聚合/明细单位。"""
    from release_inputs import safe_path
    evidence = record.get('evidence') or {}
    raw_path = Path(root) / safe_path(evidence.get('raw_path', ''))
    if not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != evidence.get('raw_sha256'):
        raise ValueError('CIH land source evidence missing or changed')
    parsed = build_sections(json.loads(raw_path.read_text(encoding='utf-8')))['supply_22']
    projected = (record.get('sections') or {}).get('supply_22') or {}
    for field in ('period', 'source_url', 'headers', 'cities', 'aggregate'):
        if projected.get(field) != parsed.get(field):
            raise ValueError('CIH land source/projection mismatch: ' + field)
    if not projected.get('period'):
        raise ValueError('CIH land statistical period missing')


def main(argv=None):
    result = refresh(force=bool(argv and '--force' in argv))
    print(json.dumps({k: v for k, v in result.items() if k != 'sections'},
                     ensure_ascii=False, indent=1))
    return 0 if result['state'] in ('ok', 'unchanged') else 2


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv[1:]))
