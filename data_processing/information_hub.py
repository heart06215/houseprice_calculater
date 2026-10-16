"""Build display projections without mixing source populations or price bases."""
import json
import re
from datetime import date
from data_store import ROOT, atomic_json, snapshot
from stats_gov_crawler import parse_article_url_date
from cih_rankings import build_rankings


def build(data):
    path=ROOT/'output/cih/clean.json'
    cih=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'pages':[]}
    prices, daily, articles, metrics = {}, {}, {}, []
    for row in cih.get('daily_observations',[]):daily[(row['city'],row['date'])]=row
    for row in cih.get('observations',[]):
        prices[(row['city'],row['kind'],row['period'])]=row
    pages=[]
    for page in cih['pages']:
        url,text=page['url'],page['text']
        for row in page.get('prices',[]):
            prices[(row['city'],row['kind'],row['period'])]=dict(row,fetched_at=page['fetched_at'])
        for row in page.get('daily',[]):
            daily[(row['city'],row['date'])]=row
        if '/news/' in url:
            # Retain short searchable excerpt and provenance, not a claim of independent verification.
            articles[url]=dict(title=page['title'].split('|')[0].strip(),url=url,excerpt=(page.get('summary') or page.get('article_text') or '')[:650],fetched_at=page['fetched_at'])
        # 来源列表保留承载指标的页面；新闻索引的摘录在文章区独立展示。
        if '/news/' not in url:
            pages.append(dict(title=page['title'],url=url,fetched_at=page['fetched_at'],
                              status='公开页面' if '/mall/' not in url else '公开目录，未取得明细'))
        patterns=[('300城宅地供求',r'300城·\s*宅地供求情况\s*\(([^)]+)\)(.*?)(?=政府收购|22城)',
                   [('推出规划建面','万㎡'),('成交规划建面','万㎡'),('成交楼面均价','元/㎡'),('平均溢价率','%')]),
                  ('重点50城商品住宅',r'重点50城·\s*月度成交及推出\s*\(商品住宅\s*([^)]*)\)(.*?)(?=成交面积走势|DATA)',
                   [('成交套数','套'),('成交面积','万㎡'),('上市套数','套'),('上市面积','万㎡')])]
        for scope,pattern,fields in patterns:
            match=re.search(pattern,text,re.S)
            if match:
                for label,unit in fields:
                    value=re.search(re.escape(label)+r'\s*([\d.]+)\s*'+re.escape(unit),match[2])
                    if value:
                        metrics.append(dict(scope=scope,period=match[1],label=label,value=float(value[1]),unit=unit,source_url=url))
    releases={}
    for path in sorted((ROOT/'output/stats_gov').glob('*.json')):
        for row in json.loads(path.read_text(encoding='utf-8')):
            month=row.get('report_month','')
            if not month or month>=date.today().strftime('%Y-%m'):
                continue
            release=releases.setdefault(month,dict(month=month,title=row.get('title'),publish_date=row.get('publish_date') or parse_article_url_date(row.get('url','')),source_url=row.get('url'),rows=[]))
            for kind in ('new_house','second_hand'):
                mom,yoy=row.get(kind+'_mom'),row.get(kind+'_yoy')
                if isinstance(mom,(int,float)):
                    release['rows'].append(dict(city=row['city'],kind=kind,area=row.get('area_type','合计'),
                        mom_pct=round(mom-100,2),yoy_pct=round(yoy-100,2) if isinstance(yoy,(int,float)) else None))
    land={}
    try:
        import cih_land
        land=cih_land.load() or {}
    except Exception:
        land={}
    data['information']=dict(cih=dict(prices=list(prices.values()),daily=list(daily.values()),
        articles=list(articles.values()),metrics=metrics,pages=pages,updated_at=cih.get('updated_at'),
        land=land,
        rankings=build_rankings(), ranking_reports=list({link['url']:link for page in cih['pages'] for link in page.get('links',[]) if '/report/detail/' in link['url'] and any(word in link['title'] for word in ('拿地榜','销售业绩'))}.values())),
        releases=sorted(releases.values(),key=lambda r:r['month'],reverse=True))
    for kind, segment in data.get('housing_segments',{}).items():
        for city in segment['cities']:
            for point in city['monthly']:
                row=prices.get((city['city'],kind,point['month']))
                if row:
                    point.update(cih_price=row['price'],cih_mom_pct=row['mom_pct'],cih_source_url=row['source_url'])
    data['meta']['platform_name']='70城房价信息平台'
    policy_path=ROOT/'output/policies/clean.json'
    data['information']['policies']=json.loads(policy_path.read_text(encoding='utf-8')) if policy_path.exists() else []
    from income_crawler import projection as income_projection
    data['information']['income']=income_projection()
    macro_path=ROOT/'output/macro/clean.json'
    data['information']['macro']=json.loads(macro_path.read_text(encoding='utf-8')) if macro_path.exists() else {}
    annual_path=ROOT/'output/anjuke_recheck/annual_quotes.json'
    annual=json.loads(annual_path.read_text(encoding='utf-8')) if annual_path.exists() else []
    for city in data.get('cities',[]):
        city['annual_quotes']=[r for r in annual if r['city']==city['city']]
    snapshot('information',data['information'])
    return data


def main():
    target=ROOT/'data_processing/frontend/intersection_housing_data.json'
    atomic_json(target,build(json.loads(target.read_text(encoding='utf-8'))))


if __name__=='__main__':
    main()
