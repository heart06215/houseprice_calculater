"""南京市房地产市场交易中心公开项目目录；分页限速、断点续采、保留原始证据。"""
import argparse
import json
import time
from public_location_crawler import DATA, OUT, fetch
from data_store import atomic_json, snapshot, archive_project

URL = 'https://www.njhouse.com.cn/jsmart/nj/web/webProjectSearch'


def normalize(rows):
    projects = {}
    for row in rows:
        name=row.get('projectName') or row.get('promotionName')
        if not row.get('prjId') or not name:
            raise ValueError('Missing public project identity')
        pid = 'gov-nanjing-' + str(row['prjId'])
        projects[pid] = dict(id=pid, name=name, community=name,
            name_basis='项目名称' if row.get('projectName') else '公开推广名称（项目名称未填）',
            city='南京', province='江苏', district=row.get('dist', ''), type='公示项目',
            source='government', source_name='南京市房地产市场交易中心',
            source_url=f"https://www.njhouse.com.cn/project/info/{row['prjId']}/homePage.html",
            address=row.get('location'), promotion_name=row.get('promotionName'),
            usage=row.get('usage'), planned_sale_date=row.get('saleDate'),
            latest_permit_number=row.get('permitNO'), evidence=row.get('_evidence'),
            lat=None, lng=None, precision='unknown', price_avg=0, area=0, listings=[],
            tags=['官方项目公示', row.get('usage') or '用途待核验', '无成交报价'],
            location_note='公开目录未提供经纬度；拟开盘时间不等于许可批准日期，项目包括住宅及非住宅。')
    return list(projects.values())


def crawl(max_pages=300, refresh=False):
    path = OUT/'nanjing_projects.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(records=[],pages={})
    records = {r['prjId']:r for r in saved['records']}
    pages = saved.get('pages',{})
    log = []
    for page in [1]+[p for p in range(2,max_pages+1) if refresh or str(p) not in pages]:
        try:
            response, evidence = fetch(URL,'government_nanjing',data={'page.pageNumber':page,'page.pageSize':10})
            if str(response.get('code'))!='0' or not isinstance(response.get('data'),list):
                raise ValueError('Unexpected public search response')
            pagination=response['page']
            if int(pagination['pageNumber'])!=page: raise ValueError('Pagination mismatch')
            batch=response['data']
            normalize(batch)  # Validate all rows before checkpointing.
            for row in batch: records[row['prjId']]=dict(row,_evidence=evidence)
            pages[str(page)]=evidence
            atomic_json(path,dict(records=list(records.values()),pages=pages,total_available=pagination['totalSize'],updated_at=evidence['fetched_at']))
            log.append(dict(page=page,status='ok',records=len(records)))
            if page==1 or page%20==0:print(json.dumps(log[-1]),flush=True)
            if page>=int(pagination['totalPageCount']):break
            time.sleep(.6)
        except Exception as exc:
            log.append(dict(page=page,status='failed',error=str(exc)))
            print(str(exc),flush=True)
            break
    atomic_json(OUT/'nanjing_run.json',log)
    projects=normalize(records.values())
    if projects:
        snapshot('government_nanjing_projects',projects)
        for filename in ['properties_government.json','properties.json']:
            dest=DATA/filename
            previous=json.loads(dest.read_text(encoding='utf-8')) if dest.exists() else []
            atomic_json(dest,[p for p in previous if not str(p.get('id','')).startswith('gov-nanjing-')]+projects)
    archive_project()
    print(f'Nanjing public projects: {len(projects)}',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-pages',type=int,default=300)
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args()
    if not 1<=args.max_pages<=500:parser.error('max-pages must be 1..500')
    crawl(args.max_pages,args.refresh)
