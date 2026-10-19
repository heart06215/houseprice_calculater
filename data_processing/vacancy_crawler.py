# -*- coding: utf-8 -*-
"""Provinces: estimated unsold commercial housing over census residential stock.

Metric title (UI): 省份估算未售出面积占居住总面积
NOT official 空置率. Numerator = NBS provincial 商品房待售面积 (prefer 住宅);
denominator = 七普表1-12 人均住房建筑面积 × 家庭户人数 (ordinary residential 建筑面积).

python data_processing/vacancy_crawler.py
python data_processing/vacancy_crawler.py --crawl
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from data_store import ROOT, atomic_json, snapshot
from data_period_policy import target_year, candidate_years, select_period, selection
from provincial_unsold import discover_indicators, fetch_table

OUT = ROOT / 'output/vacancy'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

PROVINCES = (
    '北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江',
    '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北',
    '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏',
    '陕西', '甘肃', '青海', '宁夏', '新疆',
)

# 七普年鉴表1-12（2020-11-01）：居住在普通住宅的家庭户。
# Source image: https://www.stats.gov.cn/sj/pcsj/rkpc/7rp/zk/html/A0112.jpg
# Fields: households, household_persons, avg_area_per_hh, per_capita_building_sqm
CENSUS_A0112 = {
    '全国': (465241711, 1238552246, 111.18, 41.76),
    '北京': (7770769, 18102211, 81.28, 34.89),
    '天津': (4587490, 11069740, 85.54, 35.45),
    '河北': (24554501, 68088601, 105.60, 38.08),
    '山西': (12064669, 30883721, 92.57, 36.16),
    '内蒙古': (8817048, 21081773, 83.58, 34.96),
    '辽宁': (16571689, 38339479, 80.28, 34.70),
    '吉林': (8724943, 20787478, 80.63, 33.84),
    '黑龙江': (11685918, 26455448, 76.86, 33.95),
    '上海': (9095041, 21402382, 75.96, 32.28),
    '江苏': (28069197, 74167282, 123.67, 46.80),
    '浙江': (22921213, 55252198, 111.27, 46.16),
    '安徽': (20750408, 55125661, 122.42, 46.08),
    '福建': (13350262, 36614006, 133.63, 48.72),
    '江西': (13331484, 39896314, 164.48, 54.96),
    '山东': (33915789, 92192894, 106.36, 39.13),
    '河南': (30557811, 88228166, 134.26, 46.50),
    '湖北': (18760549, 50668219, 127.77, 47.31),
    '湖南': (21561143, 58609484, 129.18, 47.52),
    '广东': (38953156, 104999009, 91.21, 33.84),
    '广西': (15517021, 45197752, 136.56, 46.88),
    '海南': (2747332, 8571760, 106.01, 33.98),
    '重庆': (11415050, 28406859, 104.43, 41.96),
    '四川': (29098226, 74210524, 111.28, 43.63),
    '贵州': (12004270, 34394601, 123.49, 43.10),
    '云南': (14304259, 42141148, 125.53, 42.61),
    '西藏': (793704, 2814919, 159.16, 44.88),
    '陕西': (13350429, 34324885, 111.69, 43.44),
    '甘肃': (7936539, 22421260, 96.48, 34.15),
    '青海': (1791200, 5173085, 102.85, 35.61),
    '宁夏': (2361757, 6371085, 97.38, 36.10),
    '新疆': (7878844, 22560302, 93.11, 32.52),
}

# 《中国统计摘要2024》/年鉴：2023年末分地区新建商品房待售面积（万平方米）
# Prefer residential (#住宅); also keep commercial total for audit.
UNSOLD_2023 = {
    # province: (commercial_total, residential)
    '全国': (67295, 33119),
    '北京': (2950, 1064),
    '天津': (1226, 724),
    '河北': (1240, 956),
    '山西': (895, 621),
    '内蒙古': (1181, 732),
    '辽宁': (3220, 2090),
    '吉林': (1345, 893),
    '黑龙江': (1558, 918),
    '上海': (2920, 790),
    '江苏': (4208, 2166),
    '浙江': (2813, 1297),
    '安徽': (2429, 1169),
    '福建': (2341, 891),
    '江西': (751, 372),
    '山东': (3545, 2233),
    '河南': (2703, 1836),
    '湖北': (2303, 1466),
    '湖南': (1297, 702),
    '广东': (10547, 4774),
    '广西': (2468, 1280),
    '海南': (1118, 703),
    '重庆': (3064, 621),
    '四川': (2821, 745),
    '贵州': (1187, 562),
    '云南': (2502, 1160),
    '西藏': (97, 57),
    '陕西': (1090, 578),
    '甘肃': (658, 384),
    '青海': (196, 126),
    '宁夏': (749, 282),
    '新疆': (1873, 928),
}

CENSUS_URL = 'https://www.stats.gov.cn/sj/pcsj/rkpc/7rp/zk/html/A0112.jpg'
NBS_2024_REALESTATE = 'https://www.stats.gov.cn/sj/zxfb/202501/t20250117_1958328.html'
UNSOLD_ABSTRACT_NOTE = (
    '分省待售来自《中国统计摘要2024》'
    '「分地区房地产开发企业新建商品房销售面积、销售额和待售面积」(2023年末)；'
    '住宅列优先作为分子。'
)


def now():
    return datetime.now(timezone.utc).isoformat()


def http_get(url, timeout=25):
    host = urlparse(url).hostname or ''
    if not host.endswith('stats.gov.cn'):
        raise ValueError('Unexpected host: ' + host)
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,image/jpeg,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def stock_wan_sqm(persons, per_capita):
    return round(persons * per_capita / 10000.0, 2)


def build_rows(unsold_map, unsold_year, unsold_meta):
    rows = []
    for province in PROVINCES:
        if province not in unsold_map:
            continue
        hh, persons, avg_hh, per_capita = CENSUS_A0112[province]
        commercial, residential = unsold_map[province]
        use_residential = residential is not None and residential >= 0
        unsold = residential if use_residential else commercial
        kind = '住宅待售面积' if use_residential else '商品房待售面积'
        stock = stock_wan_sqm(persons, per_capita)
        ratio = round(unsold / stock * 100.0, 4) if stock > 0 else None
        rows.append(dict(
            province=province,
            unsold_wan_sqm=unsold,
            unsold_commercial_wan_sqm=commercial,
            unsold_residential_wan_sqm=residential,
            unsold_kind=kind,
            stock_wan_sqm=stock,
            census_households=hh,
            census_household_persons=persons,
            census_avg_area_per_hh=avg_hh,
            census_per_capita_building_sqm=per_capita,
            ratio_pct=ratio,
            unsold_as_of=str(unsold_year),
            stock_as_of='2020-11-01',
            basis=('分子：国家统计局分省' + kind + '（万平方米，年末）；'
                   '分母：七普表1-12 人均住房建筑面积×家庭户人数'
                   '（普通住宅家庭户建筑面积，换算为万平方米）'),
            sources=[
                dict(label='七普表1-12', url=CENSUS_URL),
                dict(label=unsold_meta.get('label', '待售面积'),
                     url=unsold_meta.get('url') or NBS_2024_REALESTATE),
            ],
        ))
    return rows


def national_row(unsold_map, unsold_year, rows):
    hh, persons, avg_hh, per_capita = CENSUS_A0112['全国']
    commercial, residential = unsold_map['全国']
    stock = stock_wan_sqm(persons, per_capita)
    unsold = residential if residential is not None else commercial
    return dict(
        province='全国',
        unsold_wan_sqm=unsold,
        unsold_commercial_wan_sqm=commercial,
        unsold_residential_wan_sqm=residential,
        unsold_kind='住宅待售面积',
        stock_wan_sqm=stock,
        census_households=hh,
        census_household_persons=persons,
        census_avg_area_per_hh=avg_hh,
        census_per_capita_building_sqm=per_capita,
        ratio_pct=round(unsold / stock * 100.0, 4),
        unsold_as_of=str(unsold_year),
        stock_as_of='2020-11-01',
        province_sum_residential=round(sum(r['unsold_residential_wan_sqm'] or 0 for r in rows), 2),
        province_sum_commercial=round(sum(r['unsold_commercial_wan_sqm'] or 0 for r in rows), 2),
    )


def verify_census():
    for name, (hh, persons, avg_hh, per_capita) in CENSUS_A0112.items():
        if name == '全国':
            continue
        if not (hh > 0 and persons > 0 and 20 <= per_capita <= 80 and 40 <= avg_hh <= 200):
            raise ValueError('Census row out of range: ' + name)
        implied = avg_hh * hh / persons
        if abs(implied - per_capita) > 0.08:
            raise ValueError(f'Census consistency fail {name}: {implied:.3f} vs {per_capita}')


def try_refresh_national_unsold():
    """Live-check national 待售 from latest NBS real-estate bulletin; provincial stays yearbook."""
    events = []
    try:
        raw = http_get(NBS_2024_REALESTATE)
        text = re.sub(r'<[^>]+>', '', raw.decode('utf-8', 'replace'))
        text = re.sub(r'\s+', '', text)
        national_total = national_res = None
        m_total = re.search(r'商品房待售面积(\d+)万平方米', text)
        if m_total:
            national_total = int(m_total.group(1))
        m2 = re.search(r'商品住宅待售面积(\d+)万平方米', text)
        if m2:
            national_res = int(m2.group(1))
        m3 = re.search(r'年末新建商品房待售面积(\d+)万平方米，其中商品住宅待售面积(\d+)万平方米', text)
        if m3:
            national_total, national_res = int(m3.group(1)), int(m3.group(2))
        if national_res is None:
            m_table = re.search(
                r'商品房待售面积（万平方米）(\d+\.\d+)其中：住宅(\d+\.\d+)办公楼(\d+\.\d+)商业营业用房(\d+\.\d+)',
                text,
            )
            if m_table:
                def split_cell(blob, abs_digits):
                    match = re.match(r'^(\d{%d})(-?\d+\.\d+)$' % abs_digits, blob)
                    return (int(match.group(1)), float(match.group(2))) if match else (None, None)
                total, _ = split_cell(m_table.group(1), 5)
                residential, _ = split_cell(m_table.group(2), 5)
                if total:
                    national_total = national_total or total
                if residential and 1000 <= residential <= 200000:
                    national_res = residential
        evidence = snapshot('official_vacancy_nbs', raw, 'raw', 'html')
        events.append(dict(
            source='nbs_national_bulletin', url=NBS_2024_REALESTATE, state='ok',
            national_commercial=national_total, national_residential=national_res,
            evidence=evidence,
        ))
        return events, national_total, national_res
    except Exception as exc:
        events.append(dict(source='nbs_national_bulletin', url=NBS_2024_REALESTATE,
                           state='failed', error=str(exc)[:240]))
        return events, None, None


def try_fetch_census_image():
    try:
        raw = http_get(CENSUS_URL)
        if raw[:3] != b'\xff\xd8\xff':
            raise ValueError('Census table is not JPEG')
        evidence = snapshot('official_vacancy_census', raw, 'raw', 'jpg')
        return dict(source='census_a0112', url=CENSUS_URL, state='ok', evidence=evidence)
    except Exception as exc:
        return dict(source='census_a0112', url=CENSUS_URL, state='failed', error=str(exc)[:240])


def cached_tables(saved):
    """Recover all actual annual observations, including the pre-policy format."""
    tables = {int(r['year']): r for r in saved.get('annual_history', [])}
    for row in saved.get('rows', []):
        year = int(row['unsold_as_of'][:4])
        table = tables.setdefault(year, dict(year=year, values={}, meta=saved.get('unsold_meta', {})))
        table['values'].setdefault(row['province'], [row.get('unsold_commercial_wan_sqm'), row.get('unsold_residential_wan_sqm')])
    seed = tables.setdefault(2023, dict(year=2023, values={}, meta=dict(
        label='中国统计摘要2024·分地区待售面积(2023年末)', url=NBS_2024_REALESTATE,
        capture='recoverable_official_table', note=UNSOLD_ABSTRACT_NOTE)))
    for name, values in UNSOLD_2023.items():
        seed['values'].setdefault(name, list(values))
    return tables


def national_from_archive():
    """Reparse stored official December reports; never substitute a partial year."""
    import hashlib
    from bs4 import BeautifulSoup
    path = ROOT / 'output/macro/clean.json'
    reports = json.loads(path.read_text(encoding='utf-8')).get('realestate', {}).get('history', []) if path.exists() else []
    for month in sorted((r['month'] for r in reports if r.get('month', '').endswith('-12')
                         and int(r['month'][:4]) <= target_year()), reverse=True):
        report = next(r for r in reports if r['month'] == month)
        raw = report.get('raw', {})
        file = (ROOT / raw.get('path', '')).resolve()
        if ROOT.resolve() not in file.parents or not file.is_file():
            continue
        content = file.read_bytes()
        if hashlib.sha256(content).hexdigest() != raw.get('sha256'):
            continue
        soup = BeautifulSoup(content, 'html.parser')
        title = soup.title.get_text() if soup.title else ''
        year = int(month[:4])
        if not re.search(rf'{year}年(?:全国|1[—\-－至]12月)', title):
            continue
        for table in soup.select('table'):
            rows = table.select('tr')
            for index, tr in enumerate(rows[:-1]):
                cells = [''.join(td.stripped_strings) for td in tr.find_all(['td', 'th'])]
                if cells and re.fullmatch(r'(?:新建)?商品房待售面积[（(]万平方米[）)]', cells[0]):
                    following = [''.join(td.stripped_strings) for td in rows[index + 1].find_all(['td', 'th'])]
                    if len(cells) > 1 and len(following) > 1 and '住宅' in following[0]:
                        try:
                            total, residential = float(cells[1].replace(',', '')), float(following[1].replace(',', ''))
                        except ValueError:
                            continue
                        if 0 <= residential <= total:
                            return year, (total, residential), dict(label=f'国家统计局{year}年全国房地产市场报告',
                                url=report['source_url'], raw=raw)
    return None


def assemble(prefer_live=True):
    verify_census()
    events = []
    if prefer_live:
        events.append(try_fetch_census_image())
    path = OUT / 'clean.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    tables = cached_tables(saved)
    indicators, discovery_error = None, None
    if prefer_live:
        try:
            indicators = discover_indicators()
        except Exception as exc:
            discovery_error = str(exc)[:240]
    rows_by_province = {}
    for year in candidate_years(tables):
        table = tables.get(year, dict(year=year, values={}, meta={}))
        if prefer_live and indicators:
            try:
                values, meta = fetch_table(year, indicators)
                table = dict(year=year, values={**table['values'], **values}, meta=meta)
                tables[year] = table
                events.append(dict(source='nbs_provincial_unsold', state='ok', year=year, count=len(values)))
            except Exception as exc:
                events.append(dict(source='nbs_provincial_unsold', state='failed', year=year, error=str(exc)[:240]))
        elif not table['values']:
            events.append(dict(source='nbs_provincial_unsold', state='unavailable', year=year,
                               error=discovery_error or '本地未保存该年分省表'))
        for row in build_rows(table['values'], year, table['meta']):
            if row['province'] not in rows_by_province:
                row['year_policy'] = selection([year])
                rows_by_province[row['province']] = row
        if len(rows_by_province) == len(PROVINCES):
            break
    rows = list(rows_by_province.values())
    unsold_meta = dict(target_year=target_year(), note='分省逐年择优；全国数据独立选择，不以全国数据替代分省缺值',
                       year_counts={str(y): sum(r['unsold_as_of'] == str(y) for r in rows) for y in sorted(tables, reverse=True)})
    archived = national_from_archive()
    national_years = [year for year, table in tables.items() if '全国' in table['values']]
    if archived:
        year, values, meta = archived
        tables[year] = dict(tables.get(year, dict(year=year, values={}, meta=meta)))
        tables[year]['values']['全国'] = list(values)
        national_years.append(year)
    national_year = select_period(national_years)
    national = national_row(tables[national_year]['values'], national_year, rows) if national_year else None
    if national:
        national['year_policy'] = selection([national_year])
        national['sources'] = [meta] if archived and national_year == archived[0] else [tables[national_year]['meta']]
    payload = dict(
        metric_title='省份估算未售出面积占居住总面积',
        not_vacancy_rate=True,
        updated_at=now(),
        method=dict(
            numerator='国家统计局分省新建商品房待售面积（优先住宅待售，万平方米）',
            denominator=('第七次全国人口普查年鉴表1-12：人均住房建筑面积×家庭户人数，'
                         '仅普通住宅家庭户，口径为住房建筑面积'),
            ratio='未售出面积 / 估算居住（住宅）总面积 × 100%',
            caveats=[
                '分子为开发企业已竣工未售（待售）库存，'
                '分母为普查时点全部普通住宅家庭户建筑面积存量估算。',
                '分子与分母年份不同（待售多为年末库存，分母为2020-11-01普查时点）。',
                '分母不含集体户、非普通住宅；农村自建与城镇商品房混在同一建筑面积口径中。',
                '若改用商品房（含办公/商业）待售作分子，占比会系统性偏高，表中另列对照列。',
            ],
            urban_rural_note=(
                '分母采用七普表1-12全省统一建筑面积口径，避免建筑面积与居住面积混算。'
            ),
        ),
        national=national,
        rows=sorted(rows, key=lambda r: (-(r['ratio_pct'] or -1), r['province'])),
        unsold_meta=unsold_meta,
        live_national_check=None,
        year_policy=selection(r['unsold_as_of'] for r in rows),
        annual_history=[tables[y] for y in sorted(tables)],
        audit_note=(
            '待售面积来自房地产开发企业统计，'
            '估算总面积来自七普普通住宅家庭户建筑面积，二者口径与时点均不同，仅供对照扫读。'
        ),
        events=events,
    )
    return payload


def crawl(prefer_live=True):
    OUT.mkdir(parents=True, exist_ok=True)
    payload = assemble(prefer_live=prefer_live)
    snapshot('official_vacancy_clean', payload)
    atomic_json(OUT / 'clean.json', payload)
    status = dict(
        checked_at=now(),
        state='ok' if any(e.get('state') == 'ok' for e in payload['events']) or payload['rows'] else 'failed',
        events=payload['events'],
        rows=len(payload['rows']),
        metric_title=payload['metric_title'],
    )
    # If only recoverable tables were used, mark partial when live fetches failed.
    if payload['year_policy']['fallback'] or all(e.get('state') != 'ok' for e in payload['events']):
        status['state'] = 'partial'
        status['detail'] = 'live fetch failed; served recoverable official tables'
    elif any(e.get('state') == 'failed' for e in payload['events']):
        status['state'] = 'partial'
    atomic_json(OUT / 'status.json', status)
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
    parser.add_argument('--crawl', action='store_true', help='Refresh outputs (default action)')
    parser.add_argument('--offline', action='store_true', help='Skip live HTTP; use recoverable tables only')
    args = parser.parse_args(argv)
    result = crawl(prefer_live=not args.offline)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('state') in ('ok', 'partial') else 2


if __name__ == '__main__':
    raise SystemExit(main())
