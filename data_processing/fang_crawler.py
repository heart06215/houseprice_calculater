"""Fang community pipeline: crawl / replay / export / status. crawl → replay → export → status 四段流水线。"""
import argparse
import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from data_store import ROOT, atomic_json, snapshot
from community_profile_crawler import SOURCES, read
from fang_store import Store, utcnow, later, normalize_kind, with_age_fields
import fang_parser as parser

OUT = ROOT/'output/fang'
CONFIG = Path(__file__).with_name('fang_config.json')


def config():
    cfg=read(CONFIG,{})
    if cfg.get('interval_seconds',0)<10 or not 1<=cfg.get('daily_request_limit',0)<=500 or not 0<=cfg.get('jitter_seconds',-1)<=30:
        raise ValueError('Fang interval >=10s, daily limit 1..500, jitter 0..30s required')
    if any(cfg.get(k,0)<1 for k in ('max_pages','max_frontier_jobs','catalog_refresh_days','detail_refresh_days','block_cooldown_hours','max_response_bytes','price_stale_days')):
        raise ValueError('Fang budgets and refresh periods must be positive')
    return cfg


def migrate(store,cfg):
    """Import legacy cursor and source-wide cooldown exactly once; never erase archives."""
    if store.get('legacy_migrated'):return
    bulk=read(ROOT/'output/community_profiles/bulk_state.json',{})
    prior=read(ROOT/'output/community_profiles/status.json',{})
    pauses=[s for s in (bulk,prior) if s.get('retry_after')]
    if pauses:
        latest=max(pauses,key=lambda s:s['retry_after'])
        store.put('pause',dict(until=latest['retry_after'],reason=latest.get('error'),kind='legacy'))
    for city, entry in bulk.get('cities',{}).items():
        if city not in SOURCES:continue
        for job in entry.get('queue',[]):
            if parser.allowed_url(job['url'],city):store.enqueue(city,job['url'],label=job.get('label',''),due_at=utcnow())
    store.put('legacy_migrated',True);store.db.commit()


def enqueue_links(store,city,links,cfg):
    for link in links:
        if not parser.allowed_url(link['url'],city):continue
        if store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]>=cfg['max_frontier_jobs']:
            store.put('frontier_truncated',True);break
        kind=normalize_kind(link['kind']);pid=link.get('profile_id')
        if kind=='attribute' and pid and not store.attribute_gap(pid):kind='price'
        store.enqueue(city,link['url'],kind,link.get('label',''),pid)


def parse_fetch(store,fetch,content,kind,evidence_format='html'):
    if normalize_kind(kind)=='discovery':return parser.catalog(content,fetch['city'],fetch['url'],fetch['fetched_at'],fetch['raw'])
    known=store.profile(parser.source_id(fetch['url']))
    return parser.detail(content,fetch['city'],fetch['url'],fetch['fetched_at'],fetch['raw'],known,evidence_format)


def ingest_fetch(store,fetch,content,kind,cfg,mode='http',evidence_format='html'):
    kind=normalize_kind(kind)
    parsed=parse_fetch(store,fetch,content,kind,evidence_format)
    clean=snapshot('fang_pages',dict(fetch=fetch,parser_version=parser.VERSION,**parsed))
    interval={'discovery':cfg['catalog_refresh_days'],'price':cfg['price_stale_days']}.get(kind,cfg['detail_refresh_days'])
    pid=None if kind=='discovery' else parser.source_id(fetch['url'])
    next_kind=kind if kind=='discovery' else ('attribute' if store.attribute_gap(pid) else 'price')
    with store.db:
        store.ingest(parsed)
        enqueue_links(store,fetch['city'],parsed['links'],cfg)
        store.enqueue(fetch['city'],fetch['url'],kind,profile_id=pid)
        # Replaying older evidence must not move a successfully refreshed cursor backward.
        store.db.execute('UPDATE jobs SET kind=?,due_at=MAX(due_at,?),last_success=MAX(COALESCE(last_success,\'\'),?),failures=0 WHERE url=?',
                         (next_kind,later(fetch['fetched_at'],interval*86400),fetch['fetched_at'],fetch['url']))
        store.record_fetch(dict(fetch,clean=clean,quality=parsed['quality']),mode,'ok')
        if mode=='http':store.put('last_success',fetch['fetched_at'])
    return parsed['quality']


def pause_for(status,headers,failures,cfg,at):
    seconds=cfg['block_cooldown_hours']*3600 if status is not None and status<500 else min(21600,300*2**min(failures,6))
    retry=headers.get('Retry-After','')
    try:
        delta=int(retry) if retry.isdigit() else (parsedate_to_datetime(retry)-datetime.fromisoformat(at)).total_seconds()
        seconds=max(seconds,delta)
    except (ValueError,TypeError,OverflowError):pass
    return later(at,seconds)


def fetch_page(session,url,max_bytes):
    with session.get(url,timeout=(10,30),allow_redirects=False,stream=True) as response:
        body=bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body)>max_bytes:raise parser.ParseError('Response exceeds configured size limit')
        return response.status_code,requests.structures.CaseInsensitiveDict(response.headers),bytes(body)


def run(cities=None,max_pages=None,interval=None):
    cfg=config();cities=cfg['cities'] if cities is None else cities;max_pages=cfg['max_pages'] if max_pages is None else max_pages
    interval=cfg['interval_seconds'] if interval is None else interval
    if not cities or any(c not in SOURCES for c in cities) or not 1<=max_pages<=500 or interval<10:
        raise ValueError('Invalid Fang city/batch; minimum interval is 10 seconds')
    store=Store(OUT);session=None;success=0;attempts=0;state='idle';error=None
    try:
        migrate(store,cfg)
        for city in cities:store.enqueue(city,SOURCES[city],label='总目录')
        store.db.commit()
        pause=store.get('pause')
        if pause and pause['until']>utcnow():
            return dict(state='cooldown',retry_after=pause['until'],error=pause['reason'],**store.export())
        session=requests.Session();session.headers['User-Agent']=cfg['user_agent']
        for _ in range(max_pages):
            at=utcnow();day=at[:10];counter=store.get('daily',{})
            if counter.get('date')!=day:counter=dict(date=day,count=0)
            if counter['count']>=cfg['daily_request_limit']:state='daily_limit';break
            job=store.next_job(cities,at)
            if not job:state='not_due';break
            if not parser.allowed_url(job['url'],job['city']):raise ValueError('Unsafe persisted job URL')
            previous=store.get('last_request_at')
            wait=max(0,(datetime.fromisoformat(later(previous,interval))-datetime.now(timezone.utc)).total_seconds()) if previous else 0
            # Keep individual sleeps short enough for normal interruption and logs.
            wait+=random.uniform(0,cfg['jitter_seconds'])
            while wait>0:
                step=min(wait,5);time.sleep(step);wait-=step
            at=utcnow()
            if counter['date']!=at[:10]:counter=dict(date=at[:10],count=0)
            attempts+=1;counter['count']+=1
            with store.db:
                store.put('daily',counter);store.put('last_request_at',at)
                store.put('scheduler_tick',store.get('scheduler_tick',0)+1)
                store.db.execute('UPDATE jobs SET attempts=attempts+1 WHERE url=?',(job['url'],))
            fetch=dict(city=job['city'],url=job['url'],fetched_at=at);headers={};status=None
            try:
                status,headers,content=fetch_page(session,job['url'],cfg['max_response_bytes'])
                raw=snapshot('fang',content,'raw','html');fetch.update(status=status,raw=raw)
                if headers.get('Location'):
                    dest=urlparse(urljoin(job['url'],headers['Location']))
                    fetch['redirect_target']=dest._replace(query='',fragment='').geturl()
                if status!=200:raise parser.ParseError(f'HTTP {status}; source paused, cursor retained')
                quality=ingest_fetch(store,fetch,content,job['kind'],cfg)
                success+=1;state='batch_complete'
                with store.db:store.put('pause',None)
                print(json.dumps(dict(city=job['city'],kind=job['kind'],success=success,quality=quality),ensure_ascii=False),flush=True)
            except (requests.RequestException,parser.ParseError,ValueError) as exc:
                error=str(exc);state='partial' if success else 'failed'
                until=pause_for(status,headers,job['failures'],cfg,at)
                with store.db:
                    store.record_fetch(dict(fetch,error=error),'http','failed')
                    store.put('pause',dict(until=until,reason=error,kind='source_or_parser'))
                    store.db.execute('UPDATE jobs SET failures=failures+1 WHERE url=?',(job['url'],))
                break
    except KeyboardInterrupt:
        state='interrupted';error='Interrupted; committed pages retained'
    finally:
        if session:session.close()
        if state!='idle':
            with store.db:store.put('last_run',dict(state=state,checked_at=utcnow(),successful_pages=success,attempts=attempts,error=error))
        summary=store.export();store.close()
    return dict(state=state,error=error,successful_pages=success,attempts=attempts,**summary)


def replay():
    """Reparse local original responses, then saved detail text; no HTTP access."""
    cfg=config();store=Store(OUT);count=0;failures=[]
    try:
        migrate(store,cfg)
        pages=list(read(ROOT/'output/community_profiles/bulk_state.json',{}).get('pages',[]))
        successes=[p['fetched_at'] for p in pages]
        successes += [r[0] for r in store.db.execute("SELECT at FROM fetches WHERE mode='http' AND state='ok'")]
        # Include new pipeline responses, including HTTP-200 parser failures after an adapter fix.
        pages += [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM fetches WHERE mode='http' AND status=200")]
        if successes:
            with store.db:store.put('last_success',max(successes+[store.get('last_success') or '']))
        for fetch in sorted(pages,key=lambda p:p['fetched_at']):
            marker=f"replay:{parser.VERSION}:{fetch['url']}:{fetch['fetched_at']}:{fetch['raw']['sha256']}"
            if store.get(marker):continue
            try:
                content=(ROOT/fetch['raw']['path']).read_bytes()
                if __import__('hashlib').sha256(content).hexdigest()!=fetch['raw']['sha256']:raise ValueError('Raw hash mismatch')
                kind='discovery' if '/housing/' in fetch['url'] else 'attribute'
                ingest_fetch(store,fetch,content,kind,cfg,'replay')
                with store.db:store.put(marker,True)
                count+=1
            except ValueError as exc:failures.append(dict(url=fetch['url'],error=str(exc)))
        probe=ROOT/'output/feasibility_probe_20260919'
        for event in read(probe/'browser_events.json',[]):
            if '/loupan/' not in event['url'] or not event.get('snapshot') or event.get('blocked'):continue
            url=event['url'];city=next((c for c in SOURCES if parser.allowed_url(url,c)),None)
            if not city:continue
            pid=parser.source_id(url)
            if not store.profile(pid):continue
            content=(probe/event['snapshot']).read_bytes();raw=snapshot('fang_browser_evidence',content,'raw','txt')
            marker=f"replay:{parser.VERSION}:{raw['sha256']}"
            if store.get(marker):continue
            fetch=dict(city=city,url=url,fetched_at=event['started_at'],raw=raw,status=200)
            try:
                ingest_fetch(store,fetch,content,'attribute',cfg,'replay','browser_snapshot')
                with store.db:store.put(marker,True)
                count+=1
            except ValueError as exc:failures.append(dict(url=url,error=str(exc)))
        with store.db:store.put('last_replay',dict(at=utcnow(),pages=count,failures=failures,parser_version=parser.VERSION))
        return dict(state='partial' if failures else 'replayed',replayed_pages=count,failures=failures,**store.export())
    finally:store.close()


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['crawl','replay','export','status','audit'])
    p.add_argument('--cities');p.add_argument('--max-pages',type=int);p.add_argument('--interval',type=float)
    args=p.parse_args()
    from update_data import single_writer
    # Share the updater's OS lock: staging publication cannot race a standalone collector.
    with single_writer():
        if args.command=='crawl':result=run(args.cities.split(',') if args.cities else None,args.max_pages,args.interval)
        elif args.command=='replay':result=replay()
        elif args.command=='audit':
            from fang_audit import audit
            result=audit(OUT)
        else:
            store=Store(OUT)
            try:result=store.export() if args.command=='export' else with_age_fields(store.summary())
            finally:store.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('state') in ('failed','partial','interrupted'):sys.exit(2)


if __name__=='__main__':main()
