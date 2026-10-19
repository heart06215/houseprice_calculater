"""Discover annual NBS unsold-area indicators by name, never guess indicator codes."""
import json
import math
import re

from data_store import snapshot
from gdp_crawler import EASYQUERY, EASYQUERY_PAGE, PROVINCES, REG_CODES, http_get


def discover_indicators():
    def children(code):
        raw = http_get(EASYQUERY, data=dict(m='getTree', dbcode='fsnd', wdcode='zb', id=code))
        nodes = json.loads(raw)
        if not isinstance(nodes, list):
            raise ValueError('国家数据指标目录不可解析')
        return nodes

    roots = children('zb')
    queue = [node for node in roots if '房地产' in node.get('name', '')]
    found = {}
    visited = set()
    while queue and len(visited) < 16:
        node = queue.pop(0)
        code, label = node['id'], re.sub(r'\s+', '', node.get('name', ''))
        if code in visited:
            continue
        visited.add(code)
        if '待售面积' in label:
            if '住宅' in label and not any(x in label for x in ('办公', '商业')):
                found['residential'] = code
            elif re.fullmatch(r'(?:房地产开发企业)?(?:新建)?商品房(?:屋)?待售面积(?:[（(]万平方米[）)])?', label):
                found['commercial'] = code
        if node.get('isParent') is True or node.get('isParent') == 'true':
            queue.extend(children(code))
    if not all(key in found for key in ('commercial', 'residential')):
        raise ValueError('年度库未找到可核对的商品房及住宅待售面积指标')
    return found


def parse_table(raw, year, indicators):
    data = json.loads(raw)
    payload = data.get('returndata') or {}
    units = {node['code']: node.get('unit') for dimension in payload.get('wdnodes', [])
             if dimension.get('wdcode') == 'zb' for node in dimension.get('nodes', [])}
    if any(units.get(code) != '万平方米' for code in indicators.values()):
        raise ValueError('待售面积单位缺失或不为万平方米')
    regions = {code: province for province, code in REG_CODES.items()}
    rows = {}
    for node in payload.get('datanodes', []):
        dims = {w['wdcode']: str(w.get('valuecode')) for w in node.get('wds', [])}
        value = node.get('data') or {}
        if dims.get('sj') != str(year) or dims.get('reg') not in regions or value.get('hasdata') is not True:
            continue
        kind = next((key for key, code in indicators.items() if dims.get('zb') == code), None)
        if not kind:
            continue
        number = float(value['data'])
        if not math.isfinite(number) or number < 0:
            raise ValueError('待售面积数值异常')
        province = regions[dims['reg']]
        row = rows.setdefault(province, {})
        if kind in row and row[kind] != number:
            raise ValueError('同省同年待售面积冲突')
        row[kind] = number
    result = {province: (row['commercial'], row['residential']) for province, row in rows.items()
              if all(key in row for key in ('commercial', 'residential'))}
    if not result:
        raise ValueError(f'国家数据未返回{year}年有效分省待售面积')
    if any(residential > commercial for commercial, residential in result.values()):
        raise ValueError('住宅待售面积超过商品房合计')
    return result


def fetch_table(year, indicators):
    raw = http_get(EASYQUERY, data=dict(m='QueryData', dbcode='fsnd', rowcode='reg', colcode='sj',
        wds='[]', dfwds=json.dumps([{'wdcode': 'zb', 'valuecode': code} for code in indicators.values()] +
                                 [{'wdcode': 'sj', 'valuecode': str(year)}])))
    rows = parse_table(raw, year, indicators)
    evidence = snapshot('official_provincial_unsold', raw, 'raw', 'json')
    return rows, dict(label=f'国家数据分省年度待售面积（{year}年末）', url=EASYQUERY_PAGE,
                      raw=evidence, indicators=indicators, unit='万平方米')
