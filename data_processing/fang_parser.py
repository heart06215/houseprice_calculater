"""Pure Fang adapters. No network/storage side effects; keep unlike price series separate."""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from community_attributes import year_value, market_types
from community_profile_crawler import parse_page, SOURCES

VERSION = 2  # raw_position now requires a numbered street token; forces replay re-parse


class ParseError(ValueError):
    pass


def source_id(url):
    p = urlparse(url)
    match = re.fullmatch(r'/loupan/(\d+)(?:\.htm|/(?:housedetail\.htm)?)', p.path)
    if not match or p.hostname not in {urlparse(u).hostname for u in SOURCES.values()}:
        raise ParseError('Unrecognized community URL')
    return f'fang:{p.hostname}:{match[1]}'


def allowed_url(url, city):
    p = urlparse(url)
    return (city in SOURCES and p.scheme == 'https' and p.netloc == urlparse(SOURCES[city]).netloc
            and not p.query and not p.fragment
            and bool(re.fullmatch(r'/housing/(?:[\w-]+/)?|/loupan/\d+(?:\.htm|/housedetail\.htm)', p.path)))


def clean_link(href, base, city):
    url = urljoin(base, href)
    p = urlparse(url)
    if p.scheme == 'http':url = p._replace(scheme='https').geturl()
    return url if allowed_url(url, city) else None


def numeric_price(text):
    m = re.fullmatch(r'\s*([\d,]+(?:\.\d+)?)\s*元\s*/\s*(?:㎡|平米|平方米|平|m²)\s*', text)
    if not m:return None
    value = float(m[1].replace(',', ''))
    return value if 100 <= value <= 1000000 else None


def signed_change(text):
    """Never infer sign from a colour or an unsigned number."""
    text = text.replace('％', '%').replace('−', '-')
    m = re.search(r'([↑↓+\-]|上涨|下降|下跌|上涨了|下跌了)\s*(\d+(?:\.\d+)?)\s*%', text)
    if m:
        value = float(m[2]) * (-1 if m[1] in ('↓', '-', '下降', '下跌', '下跌了') else 1)
        return value if -100 <= value <= 1000 else None
    if re.search(r'(?:^|\D)0(?:\.0+)?\s*%|持平', text):return 0.0
    return None


def quotation(profile_id, value, kind, stamp, url, raw, period=None, label=None,
              period_basis='not_provided', change_text='', change_period=None):
    item = dict(profile_id=profile_id, source='房天下', source_url=url, fetched_at=stamp,
                raw=raw, parser_version=VERSION, value=value, unit='元/㎡', kind=kind,
                period=period, period_label=label, period_basis=period_basis,
                change_pct=signed_change(change_text), change_text=change_text,
                change_period=change_period, price_basis='平台参考/展示价，非已核实成交价')
    item['id'] = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return item


def catalog(html, city, url, stamp, raw):
    from community_profile_bulk import page_info
    doc = BeautifulSoup(html, 'html.parser')
    cards = doc.select('dl.plotListwrap')
    if not cards:raise ParseError('Catalog card structure missing; preserve prior observations')
    legacy, next_url = parse_page(html, city, url, stamp, raw)
    profiles, quotes = [], []
    by_id = {}
    for card in cards:
        a = card.select_one('a.plotTit')
        if a:
            try:by_id[source_id(urljoin(url, a.get('href', '')))] = card
            except ParseError:pass
    for row in legacy:
        old_id = row['id']; row['id'] = source_id(row['source_url'])
        row.update(legacy_id=old_id, parser_version=VERSION, page_kind='catalog')
        card = by_id.get(row['id'])
        if card is None:raise ParseError('Card identity cannot be established')
        wrapper = card.find_parent('div', class_='list')
        # Price lives outside the dl, in its enclosing .list. Never zip two global lists.
        if wrapper and len(wrapper.select('dl.plotListwrap')) == 1:
            price_nodes = wrapper.select('.priceAverage')
            if len(price_nodes) == 1:
                price = numeric_price(price_nodes[0].get_text(' ', strip=True))
                ratio = wrapper.select_one('.ratio')
                ratio_text = ratio.get_text(' ', strip=True) if ratio else ''
                if price is not None:
                    quotes.append(quotation(row['id'], price, 'directory_reference', stamp, url, raw,
                                             label='目录展示价（未注明月份）', change_text=ratio_text,
                                             change_period='month' if '环比' in ratio_text else None))
        sale = next((li for li in card.select('li') if '套在售' in li.get_text()), None)
        count = re.search(r'(\d+)\s*套在售', sale.get_text(' ', strip=True)) if sale else None
        row['for_sale_count'] = int(count[1]) if count else None
        profiles.append(row)
    if cards and not profiles:
        raise ParseError('No in-scope communities parsed; review district/template changes')
    info = page_info(html, city, url)
    links = [dict(**p, kind='catalog') for p in info['partitions']]
    if next_url:links.append(dict(url=next_url, label='下一页', kind='catalog'))
    links += [dict(url=p['source_url'], label=p['name'], kind='detail', profile_id=p['id']) for p in profiles]
    return dict(profiles=profiles, quotations=quotes, links=links,
                quality=dict(cards=len(cards), accepted=len(profiles), excluded=len(cards)-len(profiles),
                             with_price=len(quotes), **info))


def snapshot_tokens(text):
    """Import previously archived accessible page text, not a browser/network fallback."""
    tokens = []
    for line in text.splitlines():
        m = re.match(r'\s*- (?:generic|paragraph|text|emphasis):\s*(.+)', line)
        if not m:m = re.match(r'\s*- (?:heading|link) "([^"]+)"', line)
        if m:tokens.append(m[1].strip('"'))
    return tokens


def profile_fields(tokens):
    labels = {'小区地址', '小区位置', '建筑年代', '物业类型', '建筑类型', '产权描述', '房屋总数',
              '楼栋总数', '物业公司', '开发商', '绿化率', '容积率', '物业费', '停车位'}
    fields = {}
    for i, t in enumerate(tokens[:-1]):
        key = re.sub(r'\s+', '', t).rstrip('：:')
        if key in labels and re.sub(r'\s+', '', tokens[i+1]) not in labels:
            fields.setdefault(key, tokens[i+1])
    return fields


def _clauses(text):
    """Clauses with offsets so evidence offsets always point into the passed text."""
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r'[^。！？；\n\r，,]+', text)]


def _sentence_for(text, pos):
    for clause, start, end in _clauses(text):
        if start <= pos < end:
            return clause
    return text


def _villa_item(text, subtype, matched, rule):
    start = text.find(matched)
    return dict(subtype=subtype, raw_text=matched,
                evidence=dict(sentence=_sentence_for(text, start), matched=matched,
                              start=start, end=start + len(matched), rule=rule))


_VILLA_SYNONYMS = (('独栋', ('独栋别墅', '独栋')), ('双拼', ('双拼别墅', '双拼')),
                   ('联排', ('联排别墅', '联排')), ('叠拼', ('叠拼别墅', '叠拼')))


def villa_subtypes(text):
    text = text or ''
    found = []
    for subtype, terms in _VILLA_SYNONYMS:
        for term in terms:  # longest synonym first
            idx = text.find(term)
            if idx >= 0:
                found.append((idx, subtype, term))
                break
    if found:
        found.sort(key=lambda item: item[0])
        return [_villa_item(text, subtype, term, 'explicit_subtype_term')
                for _, subtype, term in found]
    if '别墅' in text:
        return [_villa_item(text, '别墅子型未明', '别墅', 'generic_villa_no_subtype')]
    return []


_POSITION_LABELS = ('小区地址', '小区位置', '楼盘地址', '地址', '位置')
_POSITION_SUFFIXES = ('大道', '公路', '路', '街', '道', '弄', '巷', '号', '镇', '村',
                      '苑', '园', '府', '湾', '里', '庭', '区', '市', '县')

# An address needs a numbering token (路/弄/号 next to a digit). A bare place-name
# suffix (园/湾/村...) also ends community names like 星标家园 or 上河村.
_ADDRESS_CANDIDATE_RE = re.compile(
    r'[\u4e00-\u9fa5A-Za-z0-9]{1,}\d{1,5}(?:号|弄|院|路|街|道|巷|里|村)')
_ADDRESS_DIGIT_RE = re.compile(r'\d+\s*(?:号|弄|院)|(?:路|街|道|巷|里|村)\s*\d+')


def _looks_like_address(value):
    return bool(value and _ADDRESS_DIGIT_RE.search(value))


def _normalize_position(raw, label):
    value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', raw))
    if label:
        value = re.sub(r'^' + re.escape(label) + r'[：:，,、\s]*', '', value)
    value = re.sub(r'^(?:位于|地处|坐落于|坐落)[：:，,、\s]*', '', value)
    return value.strip('：:，,、')


def raw_position(text):
    text = text or ''
    m = re.search(r'(' + '|'.join(_POSITION_LABELS) + r')\s*[：:]\s*([^。！？；\n\r，,]*)', text)
    if m and m[2].strip():
        raw, label = m[2].strip(), m[1]
        normalized = _normalize_position(raw, label)
        if normalized:
            return dict(raw=raw, normalized=normalized, label=label)
    m = re.search(r'(位于|地处|坐落于|坐落)\s*([^。！？；\n\r，,]*)', text)
    if m and m[2].strip() and _looks_like_address(m[2]):
        raw = m[2].strip()
        return dict(raw=raw, normalized=_normalize_position(raw, None), label=None)
    # Unlabeled text: only a numbered street token counts. A community name such as
    # 真源小区 / 星标家园 / 华润橡树湾 must never be returned as an address.
    m = _ADDRESS_CANDIDATE_RE.search(text)
    if m:
        raw = m[0]
        return dict(raw=raw, normalized=_normalize_position(raw, None), label=None)
    return None


_NEGATION_RE = re.compile(r'(?:非|不是|不属于|不含|并不含|无|没有|并无|并非|未含|未包含)\s*'
                          r'(?:别墅|独栋|双拼|联排|叠拼)')
_NEGATION_WORDS = ('非', '不是', '不属于', '不含', '并不含', '无', '没有', '并无', '并非', '未含', '未包含')
_RECOMMENDATION_TERMS = ('附近', '周边', '推荐', '热门', '广告', '转载', '邻盘', '临近楼盘',
                         '相邻楼盘', '猜你喜欢', '相关楼盘', '相似楼盘')


def negation_or_recommendation(text):
    text = text or ''
    spans, negated, recommended = [], [], []
    for m in _NEGATION_RE.finditer(text):
        negated.append(m.group())
        spans.append(dict(start=m.start(), end=m.end(), kind='negation', term=m.group()))
    for term in _RECOMMENDATION_TERMS:
        for m in re.finditer(re.escape(term), text):
            recommended.append(term)
            spans.append(dict(start=m.start(), end=m.end(), kind='recommendation', term=term))
    spans.sort(key=lambda span: span['start'])
    return dict(negated=bool(negated), negation_terms=negated,
                recommended=bool(recommended), recommendation_terms=recommended, spans=spans)


_HINT_TERMS = ('洋房', '复式', '跃层', '一楼带院', '低密', '大平层', '欧式', '独门独院')


def _hints(text):
    found = sorted((text.find(term), term) for term in _HINT_TERMS if term in text)
    return [dict(hint=term, raw_text=term,
                 evidence=dict(sentence=_sentence_for(text, idx), matched=term,
                               start=idx, end=idx + len(term), rule='hint_only'))
            for idx, term in found]


def extract_forms(text):
    text = text or ''
    dirty_terms = _NEGATION_WORDS + _RECOMMENDATION_TERMS
    spans = negation_or_recommendation(text)['spans']
    clean = []
    for clause, start, end in _clauses(text):
        dirty = any(start < span['end'] and span['start'] < end for span in spans) \
            or any(term in clause for term in dirty_terms)
        if not dirty:
            clean.append(clause)
    positive = ' '.join(clause.strip() for clause in clean).strip()
    if not positive:
        return dict(residential_forms=[], villa_subtypes=[], hints=[])
    kinds = []
    for kind in market_types(positive)[0]:
        if kind not in kinds:
            kinds.append(kind)
    return dict(residential_forms=kinds, villa_subtypes=villa_subtypes(positive),
                hints=_hints(positive))


def detail(content, city, url, stamp, raw, known, evidence_format='html'):
    pid = source_id(url)
    if not known or known['id'] != pid:raise ParseError('Detail requires a known catalog identity')
    if evidence_format == 'browser_snapshot':
        text = content.decode('utf-8') if isinstance(content, bytes) else content
        tokens = snapshot_tokens(text)
        hrefs = re.findall(r'- /url: ([^\s]+)', text)
        headings = re.findall(r'- heading "([^"]+)"', text)
    else:
        doc = BeautifulSoup(content, 'html.parser')
        title = doc.title.get_text() if doc.title else ''
        if re.search(r'验证码|安全验证|访问已被拦截', title):raise ParseError('Verification page')
        for node in doc.select('script, style, [hidden], [style*="display:none"], [style*="display: none"]'):node.decompose()
        tokens = list(doc.stripped_strings)
        hrefs = [a['href'] for a in doc.select('a[href]')]
        headings = [h.get_text(' ', strip=True) for h in doc.select('h1,h2,h3')]
    if known['name'] not in headings:raise ParseError('Detail heading does not match catalog identity')
    # Exclude recommendations, individual listings and historical opening-price tables.
    start = tokens.index(known['name']) if known['name'] in tokens else 0
    end = next((i for i in range(start+1, len(tokens)) if re.search(r'就近楼群|历史开盘信息|二手房源推荐|成交记录', tokens[i])), len(tokens))
    scoped = tokens[start:end]
    fields = profile_fields(scoped)
    quotes = []
    joined = ' '.join(scoped)
    # Inline spans commonly separate amount and unit; bind both to the explicit price label.
    pattern = r'((?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*参考均价|本月均价)\s*([\d,]+(?:\.\d+)?\s*元\s*/\s*(?:平方米|平米|㎡|平|m²))'
    for label in re.finditer(pattern, joined):
        token = re.sub(r'\s+', '', label[1])
        value = numeric_price(label[4])
        if value is None:continue
        period, basis = None, 'month_only_year_unknown'
        if token == '本月均价':
            period = datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m')
            basis = 'relative_label_at_fetch'
        elif label[2] and 1 <= int(label[3]) <= 12:
            period, basis = f'{label[2]}-{int(label[3]):02}', 'explicit'
        elif label[3] and not 1 <= int(label[3]) <= 12:continue
        match = re.match(r'\s*(环比上月\s*(?:[↑↓+\-]|上涨|下降|下跌)?\s*\d+(?:\.\d+)?\s*%)', joined[label.end():])
        change = match[1] if match else ''
        quotes.append(quotation(pid, value, 'monthly_reference', stamp, url, raw, period, token, basis, change, 'month' if change else None))
    if not fields and not quotes:raise ParseError('Recognized community but no supported fields; review template')
    row = dict({k:known[k] for k in ('id','city','name','district','source','source_url')},
               evidence_url=url, fetched_at=stamp, raw=raw, identity_verified=False,
               page_kind='detail', parser_version=VERSION, detail_fields=fields, evidence_format=evidence_format)
    value = fields.get('建筑年代', '').removesuffix('建成')
    year = year_value(value)
    if year:row.update(year=year|dict(scope='小区', source='房天下', source_url=url, field='小区建筑年代', value=value, fetched_at=stamp), year_text=value)
    address = fields.get('小区地址') or fields.get('小区位置')
    if address:row.update(address=address, address_truncated=bool(re.search(r'\.\.|…', address)))
    type_text = ' '.join(fields.get(k, '') for k in ('物业类型','建筑类型','产权描述')).strip()
    kinds, pure = market_types(type_text)
    if kinds:row.update(type_text=type_text, housing_types=kinds, pure_villa=pure)
    forms = extract_forms(joined)
    row['residential_forms'] = forms['residential_forms']
    row['villa_subtypes'] = forms['villa_subtypes']
    row['form_hints'] = forms['hints']
    position = raw_position(joined)
    if position is None and address:
        label = '小区地址' if fields.get('小区地址') else '小区位置'
        position = dict(raw=address, normalized=_normalize_position(address, label), label=label)
    row['raw_position'] = position
    links = []
    for href in hrefs:
        target = clean_link(href.strip('"'), url, city)
        if target and '/loupan/' in target and source_id(target) == pid and target != url:
            links.append(dict(url=target, kind='detail', label=known['name'], profile_id=pid))
    return dict(profiles=[row], quotations=quotes, links=links,
                quality=dict(fields=len(fields), with_price=len(quotes), evidence_format=evidence_format))
