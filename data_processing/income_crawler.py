"""Official income/consumption and minimum-wage releases, adapted from stats_gov_crawler.

Separate storage: never feed income tables into the 70-city price-index parser.
python data_processing/income_crawler.py --crawl
python data_processing/income_crawler.py --import-browser output/income/browser_mohrss_20260112.json
"""
import argparse
import html
import json
import re
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urljoin,urlparse

from bs4 import BeautifulSoup
from data_store import ROOT,atomic_json,snapshot
from stats_gov_crawler import http_get,ArticleLinks,parse_article_url_date,BASE,LIST_ITEM_RE
from city_administration import CITY_PROVINCES

OUT=ROOT/'output/income'
NBS_SEED='https://www.stats.gov.cn/sj/zxfb/202601/t20260119_1962321.html'
HISTORY_SEEDS=[
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899820.html',
    'https://www.stats.gov.cn/sj/zxfb/202501/t20250117_1958325.html',
    'https://www.stats.gov.cn/sj/zxfb/202401/t20240116_1946622.html',
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1901715.html',
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1901342.html',
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1900970.html',
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1900600.html',
    'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1900203.html',
]
WAGE_LIST='https://www.mohrss.gov.cn/SYrlzyhshbzb/laodongguanxi_/fwyd/'
WAGE_SEED=WAGE_LIST+'202601/t20260112_565296.html'
# 历史发布(脚本直连会被 EO_Bot 挑战页拦截,由 --wage-history 用真实浏览器采集)
WAGE_HISTORY_SEEDS=[
    'https://www.mohrss.gov.cn/SYrlzyhshbzb/laodongguanxi_/fwyd/202002/t20200210_359172.html',
]
PROVINCES=set('北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆'.split())


def now():return datetime.now(timezone.utc).isoformat()
def read(path,default):return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
def compact(s):return re.sub(r'\s+','',s)


def parse_income(content,url,stamp,raw):
    doc=BeautifulSoup(content,'html.parser');title=doc.title.get_text(' ',strip=True) if doc.title else ''
    # Only full calendar years may be used as an annual-income denominator.
    match=re.search(r'(20\d{2})年居民收入和消费支出情况',title)
    if not match:raise ValueError('未识别到全年居民收支标题，季度累计值不得乘4充当全年')
    year=int(match[1]);text=compact(doc.get_text(' ',strip=True));metrics={}
    if year>=datetime.fromisoformat(stamp.replace('Z','+00:00')).year:raise ValueError('尚未结束的年份不得作为全年收入')
    for scope,label in [('national','全国'),('urban','城镇'),('rural','农村')]:
        values={}
        for key,field in [('disposable_mean','可支配收入'),('disposable_median','可支配收入中位数'),('consumption','消费支出')]:
            m=re.search(label+'居民人均'+field+r'([\d,]+)元',text)
            if m:values[key]=int(m[1].replace(',',''))
        metrics[scope]=values
    for key,label in [('wage_income','工资性收入'),('business_income','经营净收入'),('property_income','财产净收入'),('transfer_income','转移净收入')]:
        m=re.search(r'(?:全国居民)?人均'+label+r'([\d,]+)元',text)
        if m:metrics['national'][key]=int(m[1].replace(',',''))
    categories={}
    for label in ('食品烟酒','衣着','居住','生活用品及服务','交通通信','教育文化娱乐','医疗保健','其他用品及服务'):
        m=re.search(r'(?:全国居民)?人均'+label+r'消费支出([\d,]+)元',text)
        if m:categories[label]=int(m[1].replace(',',''))
    if any(not metrics['national'].get(k) for k in ('disposable_mean','disposable_median','consumption')):
        raise ValueError('全国全年收入均值、中位数或消费字段缺失；保留旧资料')
    if any(not 1000<=v<=1000000 for values in metrics.values() for v in values.values()):raise ValueError('收入数值异常')
    return dict(year=year,period=f'{year}-01/{year}-12',scope='全国及分城乡居民',unit='元/人/年',metrics=metrics,
                consumption_categories=categories,source='国家统计局',source_url=url,published_at=publication_date(content,url),
                fetched_at=stamp,raw=raw,title=title.split(' - ')[0],parser_version=1)


def parse_wages(content,url,stamp,raw,mode='http'):
    doc=BeautifulSoup(content,'html.parser');text=compact(doc.get_text(' ',strip=True))
    m=re.search(r'截至(20\d{2})年(\d{1,2})月(\d{1,2})日',text)
    if not m or '月最低工资标准' not in text:raise ValueError('人社部未返回可解析的最低工资表（可能为脚本验证页），旧表保留')
    as_of=f'{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}';rows=[]
    for tr in doc.select('table tr'):
        cells=[compact(td.get_text(' ',strip=True)) for td in tr.find_all(['td','th'],recursive=False)]
        if len(cells)!=9:continue
        region=re.sub(r'^其中[:：]','',cells[0])
        if region not in PROVINCES|{'深圳'}:continue
        def number(s):return float(s) if re.fullmatch(r'\d+(?:\.\d+)?',s) else None
        monthly=[number(v) for v in cells[1:5]];hourly=[number(v) for v in cells[5:9]]
        if not any(monthly) or any(v is not None and not 500<=v<=10000 for v in monthly):raise ValueError('月最低工资字段异常')
        if any(v is not None and not 1<=v<=200 for v in hourly):raise ValueError('小时最低工资字段异常')
        rows.append(dict(region=region,monthly=monthly,hourly=hourly))
    if {r['region'] for r in rows if r['region']!='深圳'}!=PROVINCES or len(rows)!=len({r['region'] for r in rows}):
        raise ValueError('最低工资表31省区市不完整或重复；禁止替换')
    return dict(as_of=as_of,rows=rows,source='人力资源社会保障部',source_url=url,fetched_at=stamp,raw=raw,
                published_at=publication_date(content,url),capture_mode=mode,unit='元/月；元/小时',parser_version=1)


def merge(kind,record):
    saved=read(OUT/'clean.json',dict(income=[],minimum_wages=[]))
    key='year' if kind=='income' else 'as_of'
    records={r[key]:r for r in saved[kind]};records[record[key]]=record
    saved[kind]=sorted(records.values(),key=lambda r:r[key]);saved['updated_at']=now()
    snapshot('official_income_clean',record);atomic_json(OUT/'clean.json',saved)
    return saved


def fetch(url):
    if urlparse(url).hostname not in ('www.stats.gov.cn','www.mohrss.gov.cn'):raise ValueError('Unexpected official source')
    content=http_get(url,timeout=20,retries=0)
    raw=snapshot('official_income',content.encode('utf-8'),'raw','html')
    return content,raw


def discover(content,base,pattern):
    found=[]
    for href,title in LIST_ITEM_RE.findall(content):
        url=urljoin(base,href)
        if re.search(pattern,title) and urlparse(url).hostname==urlparse(base).hostname:found.append(url)
    return list(dict.fromkeys(found))


def wage_candidates(urls,limit=4):
    """Refresh the newest release and backfill missing official links within the request budget."""
    urls=list(dict.fromkeys(urls))
    saved={row.get('source_url') for row in read(OUT/'clean.json',dict(minimum_wages=[]))['minimum_wages']}
    return ([urls[0]]+[url for url in urls[1:] if url not in saved])[:limit] if urls else []


def crawl(max_pages=2):
    events=[];targets={'income':[NBS_SEED],'minimum_wages':[WAGE_SEED]}
    for kind,base,pattern in [('income',BASE,r'20\d{2}年居民收入和消费支出情况'),('minimum_wages',WAGE_LIST,'最低工资标准情况')]:
        for page in range(max_pages if kind=='income' else 1):
            url=base+('index.html' if page==0 else f'index_{page}.html')
            try:
                content,raw=fetch(url)
                if not BeautifulSoup(content,'html.parser').select('a[href]'):raise ValueError('目录未返回正文链接')
                targets[kind]=discover(content,url,pattern)+targets[kind]
            except Exception as exc:
                events.append(dict(source=kind,url=url,state='failed',error=str(exc)));break
        # Only follow a small bounded set of actual annual-release links per run.
        selected=wage_candidates(targets[kind]) if kind=='minimum_wages' else list(dict.fromkeys(targets[kind]))[:3]
        for url in selected:
            # If the MoHRSS list is a script challenge, avoid immediate same-host retries.
            if kind=='minimum_wages' and any(e['source']==kind and e['state']=='failed' for e in events):break
            try:
                content,raw=fetch(url);stamp=now()
                record=(parse_income if kind=='income' else parse_wages)(content,url,stamp,raw)
                merge(kind,record);events.append(dict(source=kind,url=url,state='ok',period=record.get('year') or record.get('as_of')))
            except Exception as exc:events.append(dict(source=kind,url=url,state='failed',error=str(exc)));break
    # A bounded normal browser fallback handles script-rendered public wage tables.
    # CAPTCHA or unrecognized tables stop the attempt; previously parsed tables remain intact.
    script_blocked=any(e['source']=='minimum_wages' and e['state']=='failed'
        and any(marker in e.get('error','') for marker in ('未返回可解析','目录未返回正文链接','EO_Bot')) for e in events)
    if script_blocked and not any(e['source']=='minimum_wages' and e['state']=='ok' for e in events):
        try:
            links=browser_collect_wage_links(max_pages=1)
            if not links:raise ValueError('浏览器目录未返回最低工资发布链接')
            for url in wage_candidates(links):
                record=merge_browser_wage(url,browser_html(url))
                merge('minimum_wages',record)
                events.append(dict(source='minimum_wages',url=url,state='ok',period=record['as_of'],capture_mode='browser_dom'))
            for event in events:
                if event['source']=='minimum_wages' and event['state']=='failed':
                    event['recovered']=True
        except Exception as exc:
            events.append(dict(source='minimum_wages',url=WAGE_LIST,state='failed',capture_mode='browser_dom',error=str(exc)))
    events+=backfill_income_supplements()
    # Keep failed HTTP attempts in the log; successful browser recovery resolves their status.
    result=dict(checked_at=now(),state='partial' if any(e['state']=='failed' and not e.get('recovered') for e in events) else 'ok',events=events)
    atomic_json(OUT/'status.json',result);return result


def import_browser(path):
    saved=read(Path(path),{})
    if urlparse(saved['url']).hostname!='www.mohrss.gov.cn':raise ValueError('Browser capture is not MoHRSS')
    raw=snapshot('official_income_browser',saved)
    content='<title>'+html.escape(saved['title'])+'</title>'+''.join(saved['tables'])+'<p>'+html.escape(saved['text'])+'</p>'
    record=parse_wages(content,saved['url'],saved['fetched_at'],raw,'browser_dom')
    return merge('minimum_wages',record)


def backfill(urls=None):
    """Fetch discovered full-year releases; archive and merge, never infer annual values."""
    events=[]
    for url in urls or HISTORY_SEEDS:
        try:
            content,raw=fetch(url)
            record=parse_income(content,url,now(),raw)
            merge('income',record)
            events.append(dict(url=url,year=record['year'],state='ok'))
        except Exception as exc:
            events.append(dict(url=url,state='failed',error=str(exc)))
        time.sleep(.7)
    atomic_json(OUT/'backfill_status.json',events)
    return events


def projection():
    data=read(OUT/'clean.json',dict(income=[],minimum_wages=[]))
    return dict(**data,city_provinces=dict(CITY_PROVINCES),status=read(OUT/'status.json',{}))


def publication_date(content,url):
    doc=BeautifulSoup(content,'html.parser')
    meta=doc.find('meta',attrs={'name':'PubDate'})
    if meta:
        candidate=meta.get('content','')[:10].replace('/','-')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',candidate):return candidate
    return parse_article_url_date(url)


def parse_communique(content,url,stamp,raw):
    """年度统计公报：按全国、城镇和农村分别提取原文实际公布的收入指标。"""
    doc=BeautifulSoup(content,'html.parser')
    title=doc.title.get_text(' ',strip=True) if doc.title else ''
    m=re.search(r'(?:中华人民共和国)?(20\d{2})年国民经济和社会发展统计公报',title)
    if not m:raise ValueError('未识别到年度统计公报标题')
    year=int(m[1]);text=compact(doc.get_text(' ',strip=True))
    # 公报数字前可能带脚注标记(如 中位数[43]20883元), 须容忍
    FN=r'(?:\[\d+\])?'
    def grab(pat):
        mm=re.search(pat,text)
        return int(mm[1].replace(',','')) if mm else None
    # 2014 公报在"中位数"后还有"为"字(中位数[35]为26635元), 一并容忍
    national=dict(disposable_mean=grab(r'全国居民人均可支配收入'+FN+r'([\d,]+)元'),
                  disposable_median=grab(r'全国居民人均可支配收入中位数'+FN+r'为?([\d,]+)元'),
                  consumption=grab(r'全国居民人均消费支出'+FN+r'([\d,]+)元'))
    urban=dict(disposable_mean=grab(r'城镇居民人均可支配收入'+FN+r'([\d,]+)元'),
               disposable_median=grab(r'城镇居民人均可支配收入中位数'+FN+r'为?([\d,]+)元'),
               consumption=grab(r'城镇居民人均消费支出'+FN+r'([\d,]+)元'))
    rural=dict(disposable_mean=grab(r'农村居民人均可支配收入'+FN+r'([\d,]+)元'),
               disposable_median=grab(r'农村居民人均可支配收入中位数'+FN+r'为?([\d,]+)元'),
               consumption=grab(r'农村居民人均消费支出'+FN+r'([\d,]+)元'))
    if not national['disposable_mean']:
        raise ValueError('公报缺少全国居民收入均值；保留旧资料')
    for values in (national,urban,rural):
        if any(v is not None and not 1000<=v<=1000000 for v in values.values()):
            raise ValueError('公报收入数值异常')
    return dict(year=year,period=f'{year}-01/{year}-12',scope='全国及分城乡居民',unit='元/人/年',
                metrics=dict(national=national,urban=urban,rural=rural),
                consumption_categories={},source='国家统计局年度统计公报',source_url=url,
                published_at=publication_date(content,url),fetched_at=stamp,raw=raw,
                title=title.split(' - ')[0],parser='communique',parser_version=1)


def parse_annual_economy(content,url,stamp,raw):
    """年度经济运行稿：只取正文明确说明全年口径的全国收入与中位数。"""
    doc=BeautifulSoup(content,'html.parser')
    title=doc.title.get_text(' ',strip=True) if doc.title else ''
    match=re.match(r'(20\d{2})年国民经济',title)
    if not match or re.search(r'季度|上半年|前\d+月|前[一二三四]季度',title):
        raise ValueError('非全年国民经济运行报告')
    year=int(match[1])
    if year>=datetime.fromisoformat(stamp.replace('Z','+00:00')).year:
        raise ValueError('尚未结束的年份不得作为全年收入')
    text=compact(doc.get_text(' ',strip=True))
    mean=re.search(r'全年全国居民人均可支配收入([\d,]+)元',text)
    median=re.search(r'全国居民人均可支配收入中位数(?:为)?([\d,]+)元',text)
    if not mean or not median:raise ValueError('全年全国收入或中位数缺少明确正文证据')
    values=dict(disposable_mean=int(mean[1].replace(',','')),disposable_median=int(median[1].replace(',','')))
    if any(not 1000<=v<=1000000 for v in values.values()):raise ValueError('全年收入数值异常')
    return dict(year=year,period=f'{year}-01/{year}-12',scope='全国及分城乡居民',unit='元/人/年',
                metrics=dict(national=values,urban={},rural={}),consumption_categories={},
                source='国家统计局年度经济运行报告',source_url=url,published_at=publication_date(content,url),
                fetched_at=stamp,raw=raw,title=title.split(' - ')[0],parser='annual_economy',parser_version=1)


INCOME_SUPPLEMENT_SEEDS={2014:'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1898674.html'}


def supplement_income(record,source,fields,root=ROOT):
    """保留主报告，附上逐字段补充证据；同年同口径冲突禁止覆盖。"""
    import copy
    result=copy.deepcopy(record)
    metadata={key:source[key] for key in ('year','period','unit','source','source_url','published_at','fetched_at','raw','title','parser','parser_version')}
    metadata.update(fields=fields,label='全国收入中位数补充原文')
    supplements={s['source_url']:s for s in result.get('supplements',[])}
    supplements[metadata['source_url']]=metadata
    result['supplements']=list(supplements.values())
    return record_from_evidence(result,'income',root)


def backfill_income_supplements():
    events=[]
    saved={r['year']:r for r in read(OUT/'clean.json',dict(income=[]))['income']}
    for year,url in INCOME_SUPPLEMENT_SEEDS.items():
        record=saved.get(year)
        if not record or record.get('metrics',{}).get('national',{}).get('disposable_median') is not None:
            continue
        try:
            content,raw=fetch(url)
            source=parse_annual_economy(content,url,now(),raw)
            updated=supplement_income(record,source,['national.disposable_median'])
            merge('income',updated)
            events.append(dict(source='income',url=url,state='ok',period=year,purpose='median_backfill'))
        except Exception as exc:
            events.append(dict(source='income',url=url,state='failed',error=str(exc),purpose='median_backfill'))
    return events


COMMUNIQUE_SEEDS={
    2016:'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899428.html',
    2015:'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899041.html',
    2014:'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1898704.html',
}


def backfill_communique():
    """回填早期年份的年度统计公报(2014-2016)。只补缺, 不覆盖既有专题发布。"""
    events=[]
    have={r['year'] for r in read(OUT/'clean.json',dict(income=[]))['income']}
    for year,seed in sorted(COMMUNIQUE_SEEDS.items()):
        if year in have:
            events.append(dict(year=year,state='already'));continue
        try:
            content,raw=fetch(seed)
            record=parse_communique(content,seed,now(),raw)
            saved=merge('income',record)
            have.add(year)
            events.append(dict(year=year,state='ok',
                               median=record['metrics']['national']['disposable_median']))
            print(f'[communique] ✓ {year}年: 全国中位数 {record["metrics"]["national"]["disposable_median"]}')
        except Exception as exc:
            events.append(dict(year=year,state='failed',error=str(exc)[:120]))
            print(f'[communique] ✗ {year}: {str(exc)[:80]}')
        time.sleep(.7)
    atomic_json(OUT/'communique_status.json',dict(checked_at=now(),events=events))
    return events


def reparse():
    data=read(OUT/'clean.json',dict(income=[],minimum_wages=[]))
    for kind in ('income','minimum_wages'):
        for old in data[kind]:
            merge(kind,record_from_evidence(old,kind))
    return projection()


def record_from_evidence(record,kind,root=ROOT):
    """Reconstruct the official table from hashed HTML or its browser capture."""
    import hashlib
    from release_inputs import safe_path
    expected='www.stats.gov.cn' if kind=='income' else 'www.mohrss.gov.cn'
    if kind not in ('income','minimum_wages') or urlparse(record['source_url']).hostname!=expected:
        raise ValueError('收入或工资来源与类型不符')
    if kind=='minimum_wages' and record.get('parser')=='wage_archive':
        from wage_archive import reconstruct
        return reconstruct(record,root)
    raw=record['raw']
    content=(Path(root)/safe_path(raw['path'])).read_bytes()
    if hashlib.sha256(content).hexdigest()!=raw['sha256']:raise ValueError('原始证据哈希不符')
    if record.get('capture_mode')=='browser_dom':
        capture=json.loads(content)
        if capture['url']!=record['source_url']:raise ValueError('浏览器捕获与来源链接不符')
        content='<title>'+html.escape(capture['title'])+'</title>'+''.join(capture['tables'])+'<p>'+html.escape(capture['text'])+'</p>'
    args=(content,record['source_url'],record['fetched_at'],raw)
    if kind=='income':
        parsers={None:parse_income,'communique':parse_communique,'annual_economy':parse_annual_economy}
        if record.get('parser') not in parsers:raise ValueError('未知收入报告解析类型')
        parsed=parsers[record.get('parser')](*args)
        for evidence in record.get('supplements',[]):
            if evidence.get('supplements'):raise ValueError('补充证据不得嵌套')
            supplement=record_from_evidence(evidence,'income',root)
            if any(evidence.get(k)!=supplement.get(k) for k in ('year','period','unit','published_at','title')):
                raise ValueError('补充收入证据元数据与原文不符')
            if any(supplement.get(k)!=parsed.get(k) for k in ('year','period','unit')):
                raise ValueError('补充收入证据年份或单位不匹配')
            for scope,values in parsed['metrics'].items():
                for key,current in values.items():
                    other=supplement['metrics'][scope].get(key)
                    if current is not None and other is not None and current!=other:
                        raise ValueError('同年收入报告的已公布指标相互冲突')
            if not evidence.get('fields'):raise ValueError('补充收入证据未指定字段')
            for field in evidence['fields']:
                if field not in ('national.disposable_median','urban.disposable_median','rural.disposable_median'):
                    raise ValueError('未知补充收入字段')
                scope,key=field.split('.')
                value=supplement['metrics'][scope].get(key)
                current=parsed['metrics'][scope].get(key)
                if value is None or (current is not None and current!=value):
                    raise ValueError('补充收入字段缺少原文证据或与主报告冲突')
                parsed['metrics'][scope][key]=value
        if 'supplements' in record:parsed['supplements']=record['supplements']
        return parsed
    return parse_wages(*args,mode=record.get('capture_mode','http'))


def validate_evidence(data,root=ROOT):
    """Reject table values, report periods or units differing from the source evidence."""
    fields={'income':('year','period','scope','unit','metrics','consumption_categories','published_at'),
            'minimum_wages':('as_of','rows','unit','published_at')}
    count=0
    for kind,keys in fields.items():
        for record in data.get(kind,[]):
            parsed=record_from_evidence(record,kind,root)
            if any(record.get(key)!=parsed.get(key) for key in keys):
                raise ValueError('收入或工资统计期/数值/单位与原文不符: '+str(record.get('year') or record.get('as_of')))
            count+=1
    return count


def browser_html(url,wait_ms=3500):
    """真实浏览器(无头 Edge)取渲染后 HTML:人社部 EO_Bot 挑战需执行 JS。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        options={'channel':'msedge'} if sys.platform=='win32' else {}
        browser=p.chromium.launch(headless=True,**options)
        ctx=browser.new_context(locale='zh-CN',user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')
        page=ctx.new_page()
        try:
            page.goto(url,timeout=45000)
            page.wait_for_timeout(wait_ms)
            html=page.content()
        finally:
            browser.close()
    if '请输入验证码' in html or 'EO_Bot' in html[:2000]:
        raise ValueError('人社部挑战未通过(验证码页)')
    return html


def browser_collect_wage_links(max_pages=8):
    """遍历 fwyd 列表页,收集各期"月最低工资标准情况"文档链接。"""
    urls=[]
    for page in range(max_pages):
        url=WAGE_LIST+('index.html' if page==0 else f'index_{page}.html')
        try:
            content=browser_html(url)
        except Exception as exc:
            events_log=dict(url=url,state='failed',error=str(exc))
            print(f'[wage-history] 列表页失败 {url}: {exc}')
            break
        doc=BeautifulSoup(content,'html.parser')
        found=[]
        for a in doc.select('a[href]'):
            href=a.get('href') or ''
            text=compact(a.get_text(' ',strip=True))
            if '最低工资标准' in text:
                full=urljoin(url,href)
                if urlparse(full).hostname=='www.mohrss.gov.cn' and re.search(r'/20\d{4}/t\d+_\d+\.html',full):
                    found.append(full)
        new=[u for u in found if u not in urls]
        urls+=new
        print(f'[wage-history] 列表第{page}页: +{len(new)} 篇')
        if not new:break
        time.sleep(1.0)
    return list(dict.fromkeys(urls))


def merge_browser_wage(url,html):
    """把浏览器渲染的页面按既有 browser_dom 通道解析入库。"""
    stamp=now()
    doc=BeautifulSoup(html,'html.parser')
    title=doc.title.get_text(' ',strip=True) if doc.title else ''
    tables=[t.decode() for t in doc.select('table')]
    capture=dict(url=url,title=title,tables=tables,text=compact(doc.get_text(' ',strip=True))[:5000],fetched_at=stamp)
    return import_capture(capture,stamp)


def import_capture(capture,stamp):
    saved_url=capture['url']
    if urlparse(saved_url).hostname!='www.mohrss.gov.cn':raise ValueError('Browser capture is not MoHRSS')
    raw=snapshot('official_income_browser',capture)
    content='<title>'+html.escape(capture['title'])+'</title>'+''.join(capture['tables'])+'<p>'+html.escape(capture['text'])+'</p>'
    return parse_wages(content,saved_url,stamp,raw,'browser_dom')


def wage_history(max_pages=8,max_docs=15):
    """回填人社部历史最低工资标准(EO_Bot 挑战 → 无头浏览器)。按 as_of 合并,不覆盖已有期。"""
    events=[]
    seen=list(WAGE_HISTORY_SEEDS)+[WAGE_SEED]
    urls=list(WAGE_HISTORY_SEEDS)
    try:
        discovered=browser_collect_wage_links(max_pages)
        urls+= [u for u in discovered if u not in seen]
    except Exception as exc:
        events.append(dict(state='failed',stage='discover',error=str(exc)))
    print(f'[wage-history] 待解析文档 {len(urls)} 篇(上限 {max_docs})')
    done=0
    for url in urls:
        if done>=max_docs:break
        try:
            content=browser_html(url)
            record=merge_browser_wage(url,content)
            merge('minimum_wages',record)
            events.append(dict(url=url,state='ok',as_of=record['as_of']));done+=1
            print(f'[wage-history] ✓ {record["as_of"]} ← {url}')
        except Exception as exc:
            events.append(dict(url=url,state='failed',error=str(exc)[:120]))
            print(f'[wage-history] ✗ {url}: {str(exc)[:80]}')
        time.sleep(1.2)
    atomic_json(OUT/'wage_history_status.json',dict(checked_at=now(),events=events))
    return events


def income_history(max_pages=240,max_docs=12):
    """深翻统计局发布列表,回填更早年份的全年居民收支(2013 起)。

    列表按时间倒序,目标文章每年仅出现一次,中间大量页面无匹配属正常:
    只有翻过最后一页(HTTP 404)或达到页数上限才停止。
    """
    events=[];have={r['year'] for r in read(OUT/'clean.json',dict(income=[]))['income']}
    urls=[];seen=set(HISTORY_SEEDS+[NBS_SEED]);reached=0
    for page in range(max_pages):
        url=BASE+('index.html' if page==0 else f'index_{page}.html')
        try:
            content,_=fetch(url)
        except Exception as exc:
            events.append(dict(url=url,state='archive_end' if '404' in str(exc) else 'failed',error=str(exc)[:100]))
            print(f'[income-history] 列表终止于第 {page} 页({str(exc)[:60]})')
            break
        reached=page
        found=[u for u in discover(content,url,r'20\d{2}年居民收入和消费支出情况') if u not in seen]
        seen.update(found);urls+=found
        if page%20==0:print(f'[income-history] 已翻到第 {page} 页,累计发现 {len(urls)} 篇')
        time.sleep(.4)
    print(f'[income-history] 翻页完成(到达第 {reached} 页),发现 {len(urls)} 篇')
    print(f'[income-history] 列表发现 {len(urls)} 篇')
    done=0
    for url in urls:
        if done>=max_docs:break
        try:
            content,raw=fetch(url)
            record=parse_income(content,url,now(),raw)
            if record['year'] in have:continue
            merge('income',record);have.add(record['year']);done+=1
            events.append(dict(url=url,state='ok',year=record['year']))
            print(f'[income-history] ✓ {record["year"]}年')
        except Exception as exc:
            events.append(dict(url=url,state='failed',error=str(exc)[:120]))
        time.sleep(.7)
    atomic_json(OUT/'income_history_status.json',dict(checked_at=now(),events=events))
    return events


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--crawl',action='store_true');p.add_argument('--import-browser');p.add_argument('--reparse',action='store_true');p.add_argument('--backfill',action='store_true');p.add_argument('--max-pages',type=int,default=2);p.add_argument('--wage-history',action='store_true',help='无头浏览器回填人社部历史最低工资各期');p.add_argument('--wage-archive',action='store_true',help='恢复已核对的历史工资分表、原图和Excel附件');p.add_argument('--income-history',action='store_true',help='深翻统计局列表回填更早年份全年收支');p.add_argument('--communique-income',action='store_true',help='回填2014-2016年度公报中的全国居民收支(含中位数)')
    args=p.parse_args()
    if not 1<=args.max_pages<=10:p.error('max-pages must be 1..10')
    from update_data import single_writer
    with single_writer():
        if args.wage_archive:
            from wage_archive import backfill as recover_wages
            result=recover_wages()
        elif args.wage_history:result=wage_history()
        elif args.income_history:result=income_history()
        elif args.communique_income:result=backfill_communique()

        else:result=import_browser(args.import_browser) if args.import_browser else backfill() if args.backfill else reparse() if args.reparse else crawl(args.max_pages) if args.crawl else projection()
    print(json.dumps(result,ensure_ascii=False,indent=2))
