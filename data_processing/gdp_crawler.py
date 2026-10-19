# -*- coding: utf-8 -*-
"""Provincial (and optional city) annual GDP from NBS / provincial bulletins.

Target year = calendar year before "now" (2026 run → 2025 全年).
Primary live source: data.stats.gov.cn easyquery fsnd A020101 (地区生产总值).
Offline seed keeps a complete 31-province preliminary table when WAF blocks the API.

python data_processing/gdp_crawler.py
python data_processing/gdp_crawler.py --crawl
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from data_store import ROOT, atomic_json, snapshot
from data_period_policy import target_year, candidate_years, selection

OUT = ROOT / 'output/gdp'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

PROVINCES = (
    '北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江',
    '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北',
    '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏',
    '陕西', '甘肃', '青海', '宁夏', '新疆',
)

# NBS region codes for fsnd (valuecode under wdcode=reg)
REG_CODES = {
    '北京': '110000', '天津': '120000', '河北': '130000', '山西': '140000',
    '内蒙古': '150000', '辽宁': '210000', '吉林': '220000', '黑龙江': '230000',
    '上海': '310000', '江苏': '320000', '浙江': '330000', '安徽': '340000',
    '福建': '350000', '江西': '360000', '山东': '370000', '河南': '410000',
    '湖北': '420000', '湖南': '430000', '广东': '440000', '广西': '450000',
    '海南': '460000', '重庆': '500000', '四川': '510000', '贵州': '520000',
    '云南': '530000', '西藏': '540000', '陕西': '610000', '甘肃': '620000',
    '青海': '630000', '宁夏': '640000', '新疆': '650000',
}

EASYQUERY = 'https://data.stats.gov.cn/easyquery.htm'
EASYQUERY_PAGE = 'https://data.stats.gov.cn/easyquery.htm?cn=E0103'
# 地区生产总值（亿元）— 分省年度
ZB_GDP = 'A020101'

# 2025 全年初步数（亿元 / 实际同比%）。与各省统计局公报及国家统计局口径新闻汇总对齐；
# 云端 easyquery 成功后会被覆盖。青海用公报常见 4121.8；新闻偶见 3951 为旧口径勿用。
SEED_2025 = {
    '广东': (145846.8, 3.9), '江苏': (142351.5, 5.3), '山东': (103197.0, 5.5),
    '浙江': (94545.0, 5.5), '四川': (67665.3, 5.5), '河南': (66632.8, 5.6),
    '湖北': (62660.9, 5.5), '福建': (60199.5, 5.0), '上海': (56708.7, 5.4),
    '湖南': (55308.7, 4.8), '安徽': (52989.0, 5.5), '北京': (52073.4, 5.4),
    '河北': (49305.2, 5.6), '陕西': (36551.1, 5.1), '江西': (36020.0, 5.2),
    '重庆': (33757.9, 5.3), '辽宁': (33182.9, 3.7), '云南': (32765.8, 4.1),
    '广西': (29727.5, 5.1), '内蒙古': (26710.0, 4.7), '山西': (25495.7, 4.0),
    '贵州': (23562.2, 4.9), '新疆': (21462.1, 5.5), '天津': (18539.8, 4.8),
    '黑龙江': (16878.0, 4.2), '吉林': (14973.9, 5.0), '甘肃': (13697.5, 5.8),
    '海南': (8108.9, 4.0), '宁夏': (5696.5, 5.3), '青海': (4121.8, 4.1),
    '西藏': (3031.9, 7.0),
}

SEED_NOTE = (
    '2025年分省地区生产总值初步数（亿元）及实际同比；优先国家数据分省年度库。'
)


def now():
    return datetime.now(timezone.utc).isoformat()


def http_get(url, timeout=30, data=None, headers=None):
    host = urllib.parse.urlparse(url).hostname or ''
    if not (host.endswith('stats.gov.cn') or host == 'data.stats.gov.cn'):
        raise ValueError('Unexpected host: ' + host)
    hdr = {
        'User-Agent': UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': EASYQUERY_PAGE,
        'X-Requested-With': 'XMLHttpRequest',
    }
    if headers:
        hdr.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode('utf-8')
        hdr['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
    req = urllib.request.Request(url, data=body, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def seed_rows(year):
    if year != 2025:
        # 非种子年时仍返回空，等待 live；避免把 2025 误标为其它年
        return [], dict(mode='empty', note='无该年离线种子，需 live easyquery')
    rows = []
    for province in PROVINCES:
        gdp, yoy = SEED_2025[province]
        rows.append(dict(
            province=province,
            gdp_yi=gdp,
            yoy_pct=yoy,
            year=year,
            source='seed_preliminary',
            source_url=EASYQUERY_PAGE,
        ))
    return rows, dict(mode='seed', note=SEED_NOTE, page=EASYQUERY_PAGE)


def parse_easyquery(payload, year):
    """Parse NBS QueryData JSON into province rows."""
    if isinstance(payload, (bytes, bytearray)):
        text = payload.decode('utf-8', 'replace')
    else:
        text = payload
    if '403 Forbidden' in text or text.lstrip().startswith('<!'):
        raise RuntimeError('easyquery blocked or returned HTML')
    data = json.loads(text)
    returndata = data.get('returndata') or {}
    nodes = returndata.get('datanodes') or []
    wdnodes = {w.get('wdcode'): w for w in (returndata.get('wdnodes') or [])}
    reg_names = {}
    for node in (wdnodes.get('reg') or {}).get('nodes') or []:
        reg_names[node.get('code')] = node.get('cname') or node.get('name')
    # code map reverse
    code_to_province = {code: name for name, code in REG_CODES.items()}
    by_province = {}
    for node in nodes:
        wds = {w['wdcode']: w.get('valuecode') for w in node.get('wds') or []}
        if wds.get('sj') not in (str(year), year):
            continue
        data_obj = node.get('data') or {}
        if not data_obj.get('hasdata'):
            continue
        try:
            value = float(data_obj.get('data'))
        except (TypeError, ValueError):
            continue
        reg = wds.get('reg')
        province = code_to_province.get(reg) or reg_names.get(reg)
        if not province:
            continue
        # normalize full names like 广东省 → 广东
        for short in PROVINCES:
            if province.startswith(short):
                province = short
                break
        if province not in PROVINCES:
            continue
        by_province[province] = value
    rows = []
    for province in PROVINCES:
        if province not in by_province:
            continue
        rows.append(dict(
            province=province,
            gdp_yi=round(by_province[province], 2),
            yoy_pct=None,
            year=year,
            source='nbs_easyquery_fsnd',
            source_url=EASYQUERY_PAGE,
        ))
    if len(rows) < 28:
        raise RuntimeError('easyquery returned too few provinces: %d' % len(rows))
    return rows


def fetch_easyquery(year):
    # warm-up (may 403)
    try:
        http_get(EASYQUERY_PAGE, timeout=20)
    except Exception:
        pass
    k1 = str(int(time.time() * 1000))
    dfwds = json.dumps(
        [{'wdcode': 'zb', 'valuecode': ZB_GDP},
         {'wdcode': 'sj', 'valuecode': str(year)}],
        separators=(',', ':'),
        ensure_ascii=False,
    )
    raw = http_get(EASYQUERY, data={
        'm': 'QueryData',
        'dbcode': 'fsnd',
        'rowcode': 'reg',
        'colcode': 'sj',
        'wds': '[]',
        'dfwds': dfwds,
        'k1': k1,
        'h': '1',
    })
    return parse_easyquery(raw, year)


def fetch_city_easyquery(year):
    """Best-effort 主要城市年度 GDP（csnd）。失败则返回空列表。"""
    k1 = str(int(time.time() * 1000))
    # 城市地区生产总值常见 zb；若指标码变动则整段失败并忽略
    for zb in ('A010101', 'A020101'):
        try:
            dfwds = json.dumps(
                [{'wdcode': 'zb', 'valuecode': zb},
                 {'wdcode': 'sj', 'valuecode': str(year)}],
                separators=(',', ':'),
                ensure_ascii=False,
            )
            raw = http_get(EASYQUERY, data={
                'm': 'QueryData',
                'dbcode': 'csnd',
                'rowcode': 'reg',
                'colcode': 'sj',
                'wds': '[]',
                'dfwds': dfwds,
                'k1': k1,
                'h': '1',
            })
            text = raw.decode('utf-8', 'replace')
            if '403' in text[:200] or text.lstrip().startswith('<'):
                continue
            data = json.loads(text)
            nodes = ((data.get('returndata') or {}).get('datanodes')) or []
            wdnodes = {w.get('wdcode'): w for w in ((data.get('returndata') or {}).get('wdnodes') or [])}
            names = {n.get('code'): (n.get('cname') or n.get('name'))
                     for n in ((wdnodes.get('reg') or {}).get('nodes') or [])}
            cities = []
            for node in nodes:
                wds = {w['wdcode']: w.get('valuecode') for w in node.get('wds') or []}
                if wds.get('sj') not in (str(year), year):
                    continue
                data_obj = node.get('data') or {}
                if not data_obj.get('hasdata'):
                    continue
                try:
                    value = float(data_obj.get('data'))
                except (TypeError, ValueError):
                    continue
                name = names.get(wds.get('reg')) or ''
                name = re.sub(r'(市|地区|州)$', '', name)
                if not name:
                    continue
                cities.append(dict(
                    city=name, gdp_yi=round(value, 2), year=year,
                    source='nbs_easyquery_csnd', zb=zb,
                    source_url=EASYQUERY_PAGE,
                ))
            if cities:
                return cities
        except Exception:
            continue
    return []


def merge_seed_growth(rows, year):
    """Keep seed yoy when live API omits growth."""
    if year != 2025:
        return rows
    out = []
    for row in rows:
        item = dict(row)
        if item.get('yoy_pct') is None and item['province'] in SEED_2025:
            item['yoy_pct'] = SEED_2025[item['province']][1]
        out.append(item)
    return out


def assemble(prefer_live=True, year=None):
    requested = year or target_year()
    if requested > target_year():
        raise ValueError('GDP年度不得晚于上一日历年')
    saved_path = OUT / 'clean.json'
    saved = json.loads(saved_path.read_text(encoding='utf-8')) if saved_path.exists() else {}
    tables = {item['year']: item for item in saved.get('history', []) if item.get('year')}
    if saved.get('year') and saved.get('provinces'):
        tables[saved['year']] = {k: v for k, v in saved.items() if k not in ('history', 'status')}
    events = []
    provinces = []
    cities = []
    meta = dict(zb=ZB_GDP, dbcode='fsnd', page=EASYQUERY_PAGE)
    selected_year = None
    for candidate in candidate_years([*tables, 2025]):
        if candidate > requested:
            continue
        cached = tables.get(candidate, {})
        seed, seed_meta = seed_rows(candidate)
        have = {r['province']: dict(r) for r in seed}
        have.update({r['province']: dict(r) for r in cached.get('provinces', [])
                     if r.get('year', candidate) == candidate and r.get('gdp_yi', 0) > 0})
        mode = cached.get('meta', {}).get('mode') or seed_meta['mode']
        if prefer_live:
            try:
                live = merge_seed_growth(fetch_easyquery(candidate), candidate)
                have.update({r['province']: dict(r) for r in live})
                mode = 'live_easyquery' if len(live) == len(PROVINCES) else 'live_partial_cached'
                events.append(dict(kind='easyquery_fsnd', state='ok', count=len(live), year=candidate))
            except Exception as exc:
                events.append(dict(kind='easyquery_fsnd', state='failed', error=str(exc)[:240], year=candidate))
        if not all(p in have for p in PROVINCES):
            events.append(dict(kind='annual_coverage', state='failed', year=candidate,
                               error='分省年度表不完整，继续检查前一年', count=len(have)))
            continue
        selected_year = candidate
        provinces = [dict(have[p], year=candidate) for p in PROVINCES]
        cities = cached.get('cities', [])
        meta.update(year=candidate, mode=mode)
        if prefer_live:
            try:
                live_cities = fetch_city_easyquery(candidate)
                if live_cities:
                    cities = live_cities
            except Exception as exc:
                events.append(dict(kind='easyquery_csnd', state='failed', error=str(exc)[:240], year=candidate))
        events.append(dict(kind='annual_selection', state='ok', year=candidate, count=len(provinces)))
        break

    provinces = sorted(
        provinces,
        key=lambda r: (-(r.get('gdp_yi') or 0), r['province']),
    )
    for i, row in enumerate(provinces, 1):
        row['rank'] = i

    policy = selection([selected_year] if selected_year else [])
    state = 'failed' if not provinces else 'partial' if policy['fallback'] or any(e['state'] == 'failed' for e in events) else 'ok'
    payload = dict(
        metric_title='分省地区生产总值（年度）',
        year=selected_year,
        unit='亿元',
        updated_at=now(),
        provinces=provinces,
        cities=cities,
        meta=meta,
        audit_note=(
            f'{selected_year or "暂无可用年份"}年分省地区生产总值（亿元，现价）。排序按GDP降序。'
            '优先国家数据分省年度库；城市GDP在主要城市年度库可用时附带。'
        ),
        events=events,
        state=state,
        year_policy=policy,
        history=[tables[key] for key in sorted(tables)],
    )
    return payload


def crawl(prefer_live=True, year=None):
    OUT.mkdir(parents=True, exist_ok=True)
    data = assemble(prefer_live=prefer_live, year=year)
    if data['state'] != 'failed':
        atomic_json(OUT / 'clean.json', data)
    status = dict(
        checked_at=now(),
        state=data['state'],
        year=data['year'],
        province_count=len(data['provinces']),
        city_count=len(data['cities']),
        events=data['events'],
    )
    atomic_json(OUT / 'status.json', status)
    if data['state'] != 'failed':
        snapshot('official_gdp', data)
    return status


def projection():
    path = OUT / 'clean.json'
    if not path.exists():
        crawl(prefer_live=False)
    data = assemble(prefer_live=False)
    status = {}
    sp = OUT / 'status.json'
    if sp.exists():
        status = json.loads(sp.read_text(encoding='utf-8'))
    return dict(**data, status=status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crawl', action='store_true', help='Refresh GDP bundle')
    parser.add_argument('--offline', action='store_true', help='Seed only, skip live')
    parser.add_argument('--year', type=int, default=None, help='Override target year')
    args = parser.parse_args(argv)
    if args.crawl or args.offline or args.year:
        result = crawl(prefer_live=not args.offline, year=args.year)
    else:
        result = crawl(prefer_live=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('state') in ('ok', 'partial') else 2


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
