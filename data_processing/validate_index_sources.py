"""Compare every local official index with an independently parsed official HTML table."""
import json,re,time,math
from datetime import datetime,timezone,date
from bs4 import BeautifulSoup
from data_store import ROOT,atomic_json,snapshot,archive_project
from stats_gov_crawler import http_get, parse_article
from release_contract import official_month

OUT=ROOT/'output/engine_validation'


def independent_totals(html,month):
    soup=BeautifulSoup(html,'html.parser')
    title=soup.find('meta',attrs={'name':'ArticleTitle'})
    title=title.get('content','') if title else soup.title.get_text()
    match=re.search(r'(\d{4})年\s*(\d{1,2})月份',title)
    if not match or '%04d-%02d'%tuple(map(int,match.groups()))!=month:raise ValueError('Report month mismatch: '+title)
    result={}
    # Infer headers per table. Do not reuse the production regex parser.
    for table in soup.find_all('table'):
        text=re.sub(r'\s+','',table.get_text())
        if '销售价格' not in text:
            for heading in table.find_all_previous(['p','h2','h3']):
                candidate=re.sub(r'\s+','',heading.get_text())
                if '销售价格' in candidate and '指数' in candidate:
                    text=candidate;break
        kind='second_hand' if '二手住宅销售价格' in text else 'new_house' if '新建商品住宅销售价格' in text else None
        if not kind or '分类' in text:continue
        for tr in table.find_all('tr'):
            cells=[re.sub(r'\s+','',td.get_text()) for td in tr.find_all(['td','th'],recursive=False)]
            if len(cells) not in (6,8):continue
            width=len(cells)//2
            for i in (0,width):
                city=cells[i]
                try:values=[float(v) for v in cells[i+1:i+width]]
                except ValueError:continue
                if not city or any(not math.isfinite(v) for v in values):continue
                key=(kind,city)
                if key in result and result[key]!=values:raise ValueError('Conflicting table rows')
                result[key]=values
    if len(result)!=140:raise ValueError(f'Expected 140 total rows, got {len(result)}')
    meta=soup.find('meta',attrs={'name':'PubDate'})
    published=(meta.get('content','')[:10].replace('/','-') if meta else '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',published):published=None
    return result,published


def validate():
    archive_project();results=[]
    for path in sorted((ROOT/'output/stats_gov').glob('stats_gov_*.json')):
        month=path.stem[-7:];rows=json.loads(path.read_text(encoding='utf-8'));url=rows[0]['url']
        cache=OUT/(month+'.json')
        try:
            official_month(rows, month)
            saved=json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
            evidence = saved.get('raw') or rows[0].get('validated_source',{}).get('raw')
            if evidence:
                import hashlib
                content=(ROOT/evidence['path'].replace('\\','/')).read_bytes()
                if hashlib.sha256(content).hexdigest()!=evidence['sha256']:raise ValueError('Official HTML hash mismatch')
                html=content.decode('utf-8')
            else:
                html=http_get(url,timeout=15,retries=0)
            raw=snapshot('validated_stats_indices',html.encode('utf-8'),'raw','html')
            official,published=independent_totals(html,month)
            parsed={(c,k,a):(mom,yoy,avg) for c,k,a,mom,yoy,avg,_ in parse_article(html)}
            if len(parsed)!=560:raise ValueError('Official area grid incomplete')
            for row in rows:
                kind=row['table_type'];prefix='new_house' if kind.startswith('new_house') else 'second_hand'
                actual=tuple(row.get(prefix+'_'+field) for field in ('mom','yoy','annual'))
                if parsed.get((row['city'],kind,row['area_type']))!=actual:
                    raise ValueError('Official full-table mismatch: '+str((row['city'],kind,row['area_type'])))
            diffs=[];local={}
            for r in rows:
                if r.get('area_type')!='合计' or r.get('table_type') not in ('new_house','second_hand'):continue
                k=(r['table_type'],r['city'])
                if k in local:raise ValueError('Duplicate local total')
                values=[r.get(k[0]+'_'+f) for f in ['mom','yoy']]
                local[k]=values
                if official.get(k,[])[:2]!=values:diffs.append(dict(kind=k[0],city=k[1],local=values,official=official.get(k)))
            if set(local)!=set(official):raise ValueError('Local city coverage mismatch')
            status='passed' if not diffs else 'mismatch'
            result=dict(month=month,url=url,status=status,differences=diffs,raw=raw,publish_date=published,
                checked_at=datetime.now(timezone.utc).isoformat(),checked_values=len(local)*2)
            # Correct metadata only after all month/city/type/value comparisons pass.
            if status=='passed':
                for row in rows:
                    if published:row['publish_date']=published
                    row['validated_source']=dict(raw=raw,checked_at=result['checked_at'])
                atomic_json(path,rows)
                result['input_sha256']=snapshot('validated_stats_clean',rows)['sha256']
            atomic_json(cache,result)
        except Exception as exc:result=dict(month=month,url=url,status='failed',error=str(exc))
        results.append(result);atomic_json(OUT/'index_sources.json',results)
        print(json.dumps({k:v for k,v in result.items() if k in ('month','status','error')},ensure_ascii=False),flush=True)
        time.sleep(.4)
    return results


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8');validate()
