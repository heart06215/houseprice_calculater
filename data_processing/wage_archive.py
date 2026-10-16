"""Historical MoHRSS split tables and reviewed image transcriptions.

Image transcriptions are tied to exact official image bytes. Unknown or changed
images require review; an OCR result is never automatically published.
"""
import base64
import calendar
import hashlib
import html
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from data_store import ROOT, snapshot

REGIONS = '北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 深圳 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆'.split()
TABLES = Path(__file__).with_name('wage_archive_tables.json')
NINGXIA_URL = 'https://www.nx.gov.cn/zwgk/qzfwj/201801/t20180115_668505.html'
SPLIT_URLS = ['https://www.mohrss.gov.cn/SYrlzyhshbzb/laodongguanxi_/fwyd/201612/t20161213_'+n+'.html'
              for n in ('261789','261787')]
SPLIT_URLS += ['https://www.mohrss.gov.cn/ldgxs/LDGXqiyegongzi/LDGXzuidigongzibiaozhun/201503/t201503'+day+'_'+n+'.html'
              for day,n in (('05','153182'),('04','153104'))]
XLSX_URL='https://www.mohrss.gov.cn/ldgxs/LDGXqiyegongzi/LDGXzuidigongzibiaozhun/201506/t20150626_212497.html'


def period(title, published_at=None):
    match = re.search(r'(?:截至|截止)(20\d{2})年(\d{1,2})月(?:(\d{1,2})日)?', title)
    if match:
        year, month = int(match[1]), int(match[2])
        date(year, month, int(match[3] or 1))
        return f'{year:04}-{month:02}'+(f'-{int(match[3]):02}' if match[3] else ''), 'day' if match[3] else 'month'
    match = re.fullmatch(r'(20\d{2})年全国各地区(?:月|小时)最低工资标准情况', title)
    if match:
        return match[1], 'year'
    match=re.search(r'截至(\d{1,2})月(\d{1,2})日',title)
    if match and published_at:
        value=date(int(published_at[:4]),int(match[1]),int(match[2])).isoformat()
        if value>published_at:raise ValueError('工资统计期晚于原文发布日期')
        return value,'day'
    raise ValueError('历史工资表缺少明确统计期')


def cutoff(value):
    parts = [int(p) for p in value.split('-')]
    year = parts[0]
    month = parts[1] if len(parts)>1 else 12
    day = parts[2] if len(parts)>2 else calendar.monthrange(year,month)[1]
    return date(year, month, day).isoformat()


def checked_rows(rows, part, as_of):
    if len(rows)!=32 or {r['region'] for r in rows}!=set(REGIONS):
        raise ValueError('历史工资表地区不完整或重复')
    tiers = 8 if part=='combined' else len(rows[0]['values'])
    if part!='combined' and tiers not in (4,5):raise ValueError('历史工资档位只接受原表四档或五档')
    for row in rows:
        values = row['values']
        if len(values)!=tiers:
            raise ValueError('历史工资表档位数量不符')
        for key, numbers in [('monthly',values[:4]),('hourly',values[4:]) ] if part=='combined' else [(part,values)]:
            valid = [v for v in numbers if v is not None]
            low, high = (500,10000) if key=='monthly' else (1,200)
            if not valid or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not low<=v<=high for v in valid):
                raise ValueError('历史工资表数值异常')
            if key=='monthly' and any(v!=int(v) for v in valid):
                raise ValueError('月工资必须是原表整数元')
            known_conflict = part=='hourly' and row['region']=='宁夏' and numbers[:3]==[14,11.5,12] and all(v is None for v in numbers[3:])
            if (valid!=sorted(valid,reverse=True) and not known_conflict) or numbers[:len(valid)]!=valid:
                raise ValueError('历史工资档位错位或次序异常: '+row['region']+' '+str(numbers))
        effective = row.get('effective_from')
        if effective and (date.fromisoformat(effective).isoformat()!=effective or effective>cutoff(as_of)):
            raise ValueError('工资实施日期晚于统计期或格式异常')
    return rows


def read_raw(ref, root):
    from release_inputs import safe_path
    body = (Path(root)/safe_path(ref['path'])).read_bytes()
    if hashlib.sha256(body).hexdigest()!=ref['sha256']:
        raise ValueError('历史工资证据哈希不符')
    return body


def spreadsheet_rows(body, part, year):
    """Read fixed numeric cells from the official OOXML workbook; no formulas."""
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    sheets=[]
    with zipfile.ZipFile(io.BytesIO(body)) as workbook:
        if sum(i.file_size for i in workbook.infolist())>5_000_000:raise ValueError('工资附件超过允许的解压大小')
        strings=[''.join(t.text or '' for t in cell.findall('.//x:t',ns))
                 for cell in ET.fromstring(workbook.read('xl/sharedStrings.xml')).findall('x:si',ns)]
        for name in workbook.namelist():
            if not re.fullmatch(r'xl/worksheets/sheet\d+\.xml',name):continue
            values=[]
            for row in ET.fromstring(workbook.read(name)).findall('.//x:row',ns):
                cells={}
                for cell in row.findall('x:c',ns):
                    if cell.find('x:f',ns) is not None:raise ValueError('工资附件含公式，不能把缓存值作为原始工资')
                    value=cell.find('x:v',ns)
                    value=value.text if value is not None else None
                    if value is not None and cell.get('t')=='s':value=strings[int(value)]
                    if value is None and cell.get('t')=='inlineStr':value=''.join(t.text or '' for t in cell.findall('.//x:t',ns))
                    cells[re.sub(r'\d','',cell.get('r',''))]=value
                values.append(cells)
            title=next((r.get('A') for r in values if '全国各地区' in (r.get('A') or '')),'')
            label='小时' if part=='hourly' else '月'
            if title==f'{year}年全国各地区{label}最低工资标准情况':sheets.append(values)
    if len(sheets)!=1:raise ValueError('工资附件工作表类型或年份不符')
    rows=[]
    for cells in sheets[0]:
        region=re.sub(r'^其中[:：]','',re.sub(r'\s+','',cells.get('A') or ''))
        if region not in REGIONS:continue
        when=re.fullmatch(r'(20\d{2})\.(\d{2})\.(\d{2})',cells.get('B') or '')
        # The 2015 attachment writes Hainan's date as "2015.0101".
        if not when:when=re.fullmatch(r'(20\d{2})\.(\d{2})(\d{2})',cells.get('B') or '')
        if not when:raise ValueError('工资附件实施日期不完整')
        numbers=[]
        for column in 'CDEF':
            text=cells.get(column)
            if text is None:numbers.append(None);continue
            if not re.fullmatch(r'\d+(?:\.\d+)?',text):raise ValueError('工资附件单元格不是原始数值')
            original=float(text);value=round(original,4)
            if abs(original-value)>1e-8:raise ValueError('工资数值精度异常')
            numbers.append(value)
        rows.append(dict(region=region,values=numbers,effective_from=date(*map(int,when.groups())).isoformat()))
    return rows


def component(evidence, root=ROOT):
    from income_crawler import publication_date
    url = evidence['source_url']
    if urlparse(url).scheme!='https' or urlparse(url).hostname!='www.mohrss.gov.cn':
        raise ValueError('历史工资来源必须是人社部')
    content = read_raw(evidence['raw'],root).decode('utf-8')
    mode = evidence.get('capture_mode','http')
    if mode=='browser_dom':
        capture=json.loads(content)
        if capture['url']!=url:raise ValueError('历史工资浏览器来源不符')
        content='<title>'+html.escape(capture['title'])+'</title>'+''.join(capture['tables'])+'<p>'+html.escape(capture['text'])+'</p>'
    doc=BeautifulSoup(content,'html.parser')
    title=doc.title.get_text(' ',strip=True).split(' - ')[0]
    published_at=publication_date(content,url)
    as_of,precision=period(title,published_at)
    parser=evidence['parser']
    result=dict(source='人力资源社会保障部',source_url=url,title=title,
        as_of=as_of,period_precision=precision,fetched_at=evidence['fetched_at'],
        published_at=published_at,raw=evidence['raw'],capture_mode=mode,parser=parser)
    if parser=='wage_split_html':
        part='hourly' if '小时最低' in title else 'monthly' if '月最低' in title else None
        if part is None:raise ValueError('历史分表类型不符')
        tier_headers=[tr for tr in doc.select('table tr') if '第一档' in tr.get_text()]
        if len(tier_headers)!=1:raise ValueError('历史分表档位标题缺失或重复')
        tiers=5 if '第五档' in tier_headers[0].get_text() else 4
        rows=[]
        for tr in doc.select('table tr'):
            cells=[re.sub(r'\s+','',td.get_text()) for td in tr.find_all(['td','th'],recursive=False)]
            if len(cells)!=tiers+2:continue
            region=re.sub(r'^其中[:：]','',cells[0])
            if region not in REGIONS:continue
            when=re.fullmatch(r'(20\d{2})\.(\d{1,2})\.(\d{1,2})',cells[1])
            if not when:raise ValueError('历史分表缺少标准实施日期')
            effective=date(*map(int,when.groups())).isoformat()
            values=[]
            for cell in cells[2:]:
                if cell in ('','—','-'):values.append(None)
                elif re.fullmatch(r'\d+(?:\.\d+)?',cell):values.append(float(cell) if '.' in cell else int(cell))
                else:raise ValueError('历史工资单元格未识别')
            rows.append(dict(region=region,values=values,effective_from=effective))
    elif parser=='wage_xlsx':
        part=evidence.get('part')
        if part not in ('monthly','hourly'):raise ValueError('工资附件类型未知')
        attachment_url=evidence['attachment_url']
        urls={urljoin(url,node.get('href','')) for node in doc.select('a[href$=".xlsx"]')}
        if attachment_url not in urls or urlparse(attachment_url).hostname!='www.mohrss.gov.cn':raise ValueError('工资附件不属于原始来源')
        rows=spreadsheet_rows(read_raw(evidence['attachment_raw'],root),part,as_of[:4])
        result.update(attachment_url=attachment_url,attachment_raw=evidence['attachment_raw'])
    elif parser=='wage_reviewed_image':
        image=read_raw(evidence['image_raw'],root)
        if not image.startswith((b'\xff\xd8',b'\x89PNG')):raise ValueError('工资证据不是图片')
        image_url=evidence['image_url']
        urls={urljoin(url,node.get('src','')) for node in doc.select('img[src]')}
        if image_url not in urls:raise ValueError('工资原图不属于来源文章')
        tables=json.loads(TABLES.read_text(encoding='utf-8'))['tables']
        table=next((t for t in tables if t['source_url']==url and t['image_url']==image_url
                    and t['image_sha256']==evidence['image_raw']['sha256']),None)
        if table is None:raise ValueError('工资图片未经逐格核对或原图已变更，保留旧数据')
        if (table['as_of'],table['period_precision'])!=(as_of,precision):raise ValueError('工资转录统计期与官方标题不符')
        part=table['part']
        rows=[dict(region=r,values=v,**({'effective_from':d} if d else {})) for r,d,v in table['rows']]
        result.update(image_url=image_url,image_raw=evidence['image_raw'])
    else:raise ValueError('未知历史工资证据类型')
    result.update(part=part,rows=checked_rows(rows,part,as_of))
    return result


def ningxia_correction(evidence, root=ROOT):
    if evidence.get('source_url')!=NINGXIA_URL or evidence.get('parser')!='ningxia_2015':
        raise ValueError('宁夏工资校正来源不符')
    content=read_raw(evidence['raw'],root).decode('utf-8')
    text=re.sub(r'\s+','',BeautifulSoup(content,'html.parser').get_text())
    required=['宁政发〔2015〕97号','2015年11月1日起执行','由每人每月1300元调整为1480元',
              '由每人每月1220元调整为1390元','由每人每月1150元调整为1320元',
              '一类区由12.5元调整为14元','二类区由11.5元调整为13元','三类区由10.5元调整为12元']
    if any(s not in text for s in required):raise ValueError('宁夏工资原始通知字段不完整或已变更')
    return dict(source_url=NINGXIA_URL,parser='ningxia_2015',raw=evidence['raw'],
                fetched_at=evidence['fetched_at'],label='宁夏工资与实施日期按自治区原始通知校正')


def compose(sources, corrections=()):
    parts={s['part']:s for s in sources}
    if len(parts)!=len(sources) or set(parts) not in ({'monthly','hourly'},{'combined'}):
        raise ValueError('月工资与小时工资分表缺失、重复或类型不符')
    primary=parts.get('monthly') or parts['combined']
    if any((s['as_of'],s['period_precision'])!=(primary['as_of'],primary['period_precision']) for s in sources):
        raise ValueError('月工资与小时工资统计期不一致')
    rows=[]
    secondary={r['region']:r for r in parts.get('hourly',{}).get('rows',[])}
    for row in primary['rows']:
        if primary['part']=='combined':
            rows.append(dict(region=row['region'],monthly=row['values'][:4],hourly=row['values'][4:]))
        else:
            other=secondary[row['region']]
            rows.append(dict(region=row['region'],monthly=row['values'],hourly=other['values'],
                monthly_effective_from=row['effective_from'],hourly_effective_from=other['effective_from']))
    has_conflict = any(r['region']=='宁夏' and r['hourly'][:3]==[14,11.5,12] for r in rows)
    if has_conflict:
        if len(corrections)!=1:raise ValueError('宁夏汇总表存在冲突，必须提供原始通知校正证据')
        row=next(r for r in rows if r['region']=='宁夏')
        if row['monthly'][:3]!=[1480,1390,1320]:raise ValueError('宁夏待校正工资与已知原表不符')
        after=cutoff(primary['as_of'])>='2015-11-01'
        row.update(monthly=([1480,1390,1320] if after else [1300,1220,1150])+[None]*(len(row['monthly'])-3),
                   hourly=([14,13,12] if after else [12.5,11.5,10.5])+[None]*(len(row['hourly'])-3),
                   monthly_effective_from='2015-11-01' if after else None,
                   hourly_effective_from='2015-11-01' if after else None)
    elif corrections:raise ValueError('无对应冲突的工资记录不得附加校正')
    result = dict(as_of=primary['as_of'],period_precision=primary['period_precision'],rows=rows,
        source=primary['source'],source_url=primary['source_url'],raw=primary['raw'],
        published_at=primary['published_at'],fetched_at=max(s['fetched_at'] for s in sources),
        capture_mode=primary['capture_mode'],unit='元/月；元/小时',parser='wage_archive',
        parser_version=1,archive_sources=sorted(sources,key=lambda s:s['part']))
    if corrections:result['corrections']=list(corrections)
    return result


def reconstruct(record, root=ROOT):
    sources=[]
    for evidence in record.get('archive_sources',[]):
        parsed=component(evidence,root)
        if parsed!=evidence:raise ValueError('历史工资分表或转录元数据与原始证据不符')
        sources.append(parsed)
    corrections=[ningxia_correction(e,root) for e in record.get('corrections',[])]
    if corrections!=record.get('corrections',[]):raise ValueError('工资校正元数据与原始通知不符')
    result=compose(sources,corrections)
    for key in ('source_url','raw','fetched_at','capture_mode','period_precision'):
        if record.get(key)!=result.get(key):raise ValueError('历史工资主记录与分表证据不符')
    return result


def browser_bytes(page, url):
    encoded=page.evaluate('async u=>{const b=new Uint8Array(await (await fetch(u)).arrayBuffer());let s="";b.forEach(x=>s+=String.fromCharCode(x));return btoa(s)}',url)
    return base64.b64decode(encoded)


def fetch_components(url):
    """Read the official page and its original image in a normal browser session."""
    from playwright.sync_api import sync_playwright
    from income_crawler import now
    with sync_playwright() as p:
        from browser_runtime import launch_browser
        browser=launch_browser(p)
        try:
            context=browser.new_context(locale='zh-CN')
            page=context.new_page();page.goto(url,timeout=45000);page.wait_for_timeout(4000)
            content=page.content()
            if 'EO_Bot' in content[:2000] or '请输入验证码' in content:raise ValueError('工资来源验证页未通过')
            evidence=dict(source_url=url,fetched_at=now(),capture_mode='http',
                raw=snapshot('official_wage_archive',content.encode('utf-8'),'raw','html'))
            doc=BeautifulSoup(content,'html.parser')
            attachment=doc.select_one('a[href$=".xlsx"]')
            if attachment:
                target=urljoin(url,attachment['href'])
                if urlparse(target).hostname!='www.mohrss.gov.cn':raise ValueError('工资附件来源异常')
                evidence.update(parser='wage_xlsx',attachment_url=target,
                    attachment_raw=snapshot('official_wage_attachment',browser_bytes(page,target),'raw','xlsx'))
                return [component(dict(evidence,part=part)) for part in ('monthly','hourly')]
            elif any(re.sub(r'\s+','',td.get_text()) in REGIONS for td in doc.select('table td')):
                evidence['parser']='wage_split_html'
            else:
                urls=page.locator('img').evaluate_all('(items)=>items.map(i=>i.src).filter(u=>u.includes("W020"))')
                if len(urls)!=1 or urlparse(urls[0]).hostname!='www.mohrss.gov.cn':raise ValueError('工资图片缺失或来源异常')
                body=browser_bytes(page,urls[0])
                suffix='jpg' if body.startswith(b'\xff\xd8') else 'png'
                evidence.update(parser='wage_reviewed_image',image_url=urls[0],image_raw=snapshot('official_wage_image',body,'raw',suffix))
            return [component(evidence)]
        finally:browser.close()


def backfill():
    """Recover missing reviewed archive sources; keep partial pairs unpublished."""
    from collections import defaultdict
    from income_crawler import OUT, read, merge, now, http_get, validate_evidence
    from data_store import atomic_json
    saved=read(OUT/'clean.json',dict(minimum_wages=[]))['minimum_wages']
    seen={s['source_url'] for r in saved for s in r.get('archive_sources',[])}
    urls=SPLIT_URLS+[XLSX_URL]+[t['source_url'] for t in json.loads(TABLES.read_text(encoding='utf-8'))['tables']]
    periods=defaultdict(list);events=[]
    for url in dict.fromkeys(urls):
        if url in seen:continue
        try:
            for source in fetch_components(url):periods[source['as_of']].append(source)
            events.append(dict(url=url,state='captured'))
        except Exception as exc:events.append(dict(url=url,state='failed',error=str(exc)))
    for as_of,sources in periods.items():
        try:
            corrections=[]
            if any(r['region']=='宁夏' and r['values'][:3]==[14,11.5,12]
                   for s in sources if s['part']=='hourly' for r in s['rows']):
                content=http_get(NINGXIA_URL)
                corrections=[ningxia_correction(dict(source_url=NINGXIA_URL,parser='ningxia_2015',fetched_at=now(),
                    raw=snapshot('official_wage_correction',content.encode('utf-8'),'raw','html')))]
            record=compose(sources,corrections)
            validate_evidence(dict(minimum_wages=[record]))
            merge('minimum_wages',record)
            events.append(dict(as_of=as_of,state='ok'))
        except Exception as exc:events.append(dict(as_of=as_of,state='failed',error=str(exc)))
    atomic_json(OUT/'wage_archive_status.json',dict(checked_at=now(),events=events))
    return events
