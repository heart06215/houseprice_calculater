"""Join source profiles to geographic communities only with address/phase identity evidence."""
import re
import unicodedata
import calendar
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta

from data_store import ROOT, atomic_json
from community_profile_crawler import read, name_key, district_key
from community_attributes import summarize_years, finalize_types
import community_catalog as catalog
from community_catalog import fang_source_id

OUT = ROOT/'output/fang'

CATALOG_RELATION='source_record'
PHASE_RE=re.compile(r'([一二三四五六七八九十百\d]+期)')
_CATALOG_DB=None
_CITY_CODES=None
DECISION_STATE={'reuse':'matched','auto_accept':'auto_accepted','accept_alias':'auto_accepted',
                'review':'pending_review','isolate':'quarantined','new_record':'created_communities',
                'pending_location':'pending_location'}
LEGACY_STATES=('needs_location','needs_address_phase_review','matched','ambiguous')


def _phase_of(profile):
    for text in (profile.get('phase'),profile.get('name'),profile.get('address')):
        m=PHASE_RE.search(text or '')
        if m:return m.group(1)
    return None


def _city_code(city):
    global _CITY_CODES
    if _CITY_CODES is None:
        try:
            from metro_proximity import _load_city_codes
            _CITY_CODES=_load_city_codes() or {}
        except Exception:
            _CITY_CODES={}
    return _CITY_CODES.get(city)


def _source_id(profile):
    # fang_source_id raises ParseError on malformed/missing URLs; fall back to the profile id.
    try:
        return fang_source_id(profile.get('source_url')) or profile['id']
    except Exception:
        return profile['id']


def _catalog_conn():
    conn=catalog.connect(_CATALOG_DB)
    catalog.create_schema(conn)
    return conn


def _observation(profile):
    address, address_source = merge_address(profile)
    source=catalog.normalize_source(profile.get('source') or 'fang')
    year=profile.get('year') if isinstance(profile.get('year'),dict) else {}
    forms=profile.get('residential_forms') or profile.get('housing_types') or []
    subtypes=profile.get('villa_subtypes') or []
    return dict(source=source,
                source_id=_source_id(profile),name=profile.get('name'),aliases=profile.get('aliases') or [],
                phase=_phase_of(profile),address=address,address_source=address_source,
                city_code=profile.get('city_code') or _city_code(profile.get('city')),
                district_code=profile.get('district_code'),
                longitude=profile.get('longitude'),latitude=profile.get('latitude'),
                coord_system=profile.get('coord_system') or 'wgs84',
                precision_level=profile.get('precision_level') or 'unknown',
                has_osm=profile.get('has_osm',False),
                year_start=year.get('start'),year_end=year.get('end'),year_precision=year.get('precision'),
                year_meaning='completion' if (year.get('start') is not None or year.get('end') is not None) else None,
                year_scope=year.get('scope'),year_label=year.get('label'),
                residential_forms=list(forms),villa_subtypes=list(subtypes),
                pure_villa=profile.get('pure_villa'))


def _missing_fields(profile):
    missing=[]
    if not profile.get('address') or profile.get('address_truncated'):missing.append('地址')
    if not _phase_of(profile):missing.append('期次')
    if profile.get('longitude') is None or profile.get('latitude') is None:missing.append('坐标')
    if not profile.get('district_code'):missing.append('区县')
    return missing


def address_key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC',value or '')).casefold()


def exact_address(a,b):
    a,b=address_key(a),address_key(b)
    return bool(a and a==b and re.search(r'\d+(?:号|弄|院)',a) and not re.search(r'\.\.|…',a))


_NUMBERED_ADDRESS_RE = re.compile(r'\d+\s*(?:号|弄|院)')


def merge_address(profile):
    """Pick the non-truncated, most specific address. Returns (value, source)."""
    card = (profile.get('address') or '').strip() or None
    position = profile.get('raw_position')
    raw = None
    if isinstance(position, dict):
        raw = (position.get('normalized') or position.get('raw') or '').strip() or None
    candidates = []
    if card and not profile.get('address_truncated'):
        candidates.append(('card', card))
    if raw:
        candidates.append(('raw_position', raw))
    if not candidates:
        return (card, 'card') if card else (None, None)

    def rank(item):
        relation, value = item
        return (bool(_NUMBERED_ADDRESS_RE.search(value)), len(value), relation == 'raw_position')
    relation, value = max(candidates, key=rank)
    return value, relation


def current_quote(quotes,at,stale_days=60):
    if not quotes:return None
    # Different definitions remain separate series. Conflicting same-fetch values are withheld.
    latest={}
    for q in quotes:
        if q.get('kind') not in ('directory_reference','monthly_reference'):continue
        if not isinstance(q.get('value'),(int,float)) or not 100<=q['value']<=1000000:continue
        if q.get('unit')!='元/㎡' or not q.get('raw') or not q.get('source_url'):continue
        key=q['kind'];old=latest.get(key,[])
        if not old or q['fetched_at']>old[0]['fetched_at']:latest[key]=[q]
        elif q['fetched_at']==old[0]['fetched_at']:old.append(q)
    valid=[qs[0] for qs in latest.values() if len({q['value'] for q in qs})==1]
    if not valid:return None
    q=dict(max(valid,key=lambda x:(x['fetched_at'],x['kind']=='monthly_reference')))
    fetched=datetime.fromisoformat(q['fetched_at'].replace('Z','+00:00'))
    if fetched>at+timedelta(minutes=5):return None
    expiry=fetched+timedelta(days=stale_days)
    if q.get('period'):
        try:
            year,month=map(int,q['period'].split('-'))
            end=datetime(year,month,calendar.monthrange(year,month)[1],tzinfo=timezone.utc)
            if q['period']>at.strftime('%Y-%m'):return None
            expiry=min(expiry,end+timedelta(days=stale_days))
        except (ValueError,TypeError):return None
    q['stale']=at>expiry
    q['stale_after']=expiry.isoformat()
    # Undated directory changes cannot become a monthly time series.
    if not q.get('change_period'):q['change_pct']=None
    return q


def enrich(rows,at=None):
    at=at or datetime.now(timezone.utc)
    profiles=read(OUT/'profiles.json',{}).get('records',[])
    quotes=defaultdict(list)
    for q in read(OUT/'quotations.json',{}).get('records',[]):quotes[q['profile_id']].append(q)
    reviews=defaultdict(list)
    for link in read(OUT/'verified_links.json',{}).get('links',[]):reviews[link.get('profile_id')].append(link)
    conn=None
    try:
        conn=_catalog_conn()
        by_key=defaultdict(list);by_id={}
        for r in rows:
            # Regeneration/revocation cannot leave a previously published Fang reference attached.
            if r.pop('fang_profile',None):
                r['completion_year']=summarize_years([e for e in r.get('completion_year',{}).get('evidence',[]) if e.get('source')!='房天下'])
                prior=[e for e in r.get('type_evidence',[]) if e.get('source')!='房天下']
                r.update(finalize_types([e['kind'] for e in prior],prior))
            r.pop('fang_reference',None)
            by_key[(r['city'],name_key(r['name']),district_key(r.get('district')))].append(r)
            by_id[(r['city'],r['id'])]=r
        pending=[];matches=[]
        for p in profiles:
            candidates=by_key[(p['city'],name_key(p['name']),district_key(p.get('district')))]
            entry=dict(profile_id=p['id'],city=p['city'],name=p['name'],source_address=p.get('address'),
                       map_ids=[r['id'] for r in candidates],state='needs_address_phase_review' if len(candidates)==1 else 'ambiguous' if candidates else 'needs_location')
            chosen=None;basis=None
            if not p.get('identity_changed') and len(candidates)==1 and not p.get('address_truncated') and exact_address(p.get('address'),candidates[0].get('address')):
                chosen=candidates[0];basis='exact_name_district_full_address'
            approvals=reviews[p['id']]
            if len(approvals)==1:
                link=approvals[0];row=by_id.get((p['city'],link.get('map_id')))
                if (row and row in candidates and link.get('verified') is True and link.get('phase_checked') is True
                    and link.get('checked_at') and link.get('evidence_urls') and link.get('reason')
                    and not p.get('identity_changed')):
                    chosen=row;basis='reviewed_address_and_phase'
            if chosen:
                entry.update(state='matched',map_id=chosen['id'],basis=basis)
                matches.append((p,chosen,entry))
            observation=_observation(p)
            result=catalog.decide_match(conn,observation)
            entry.update(fang_source_id=observation['source_id'],decision=result['action'],
                         decision_evidence=result.get('reason'),community_id=result.get('community_id'),
                         relation=None,missing_fields=_missing_fields(p),
                         review_task_id=result.get('review_task_id'),audit_required=result.get('audit_required'),
                         pending_location=result['action']=='pending_location')
            if chosen is None:
                entry['state']=DECISION_STATE.get(result['action'],entry['state'])
            community_id=result.get('community_id')
            if community_id and result['action'] in ('reuse','auto_accept','accept_alias','new_record'):
                entity=catalog.upsert_source_entity(conn,observation['source'],observation['source_id'],
                                                    observation.get('name'),{'source_url':p.get('source_url')})
                exists=conn.execute("SELECT 1 FROM identity_links WHERE community_id=? AND source_entity_id=? "
                                    "AND relation=? AND revoked_at IS NULL",
                                    (community_id,entity['id'],CATALOG_RELATION)).fetchone()
                if not exists:
                    catalog.link_identity(conn,community_id,entity['id'],CATALOG_RELATION,
                                          {'via':'fang_publish','profile_id':p['id']},'fang_publish')
                entry['relation']=CATALOG_RELATION
            pending.append(entry)
        counts=Counter((r['city'],r['id']) for _,r,_ in matches);published=0;price_count=0
        for p,row,entry in matches:
            if counts[(row['city'],row['id'])]>1:
                entry['state']='multiple_profiles_for_map';continue
            published+=1
            evidence=p.get('field_evidence',{})
            year=p.get('year')
            if year and year.get('scope')=='小区':
                row['completion_year']=summarize_years(row.get('completion_year',{}).get('evidence',[])+[year])
            types=p.get('housing_types',[])
            if types:
                e=evidence.get('housing_types',{})
                prior=[x for x in row.get('type_evidence',[]) if x.get('confidence')!='name_hint']
                additions=[dict(kind=k,source='房天下',source_url=e.get('source_url',p['evidence_url']),field='小区住宅形态',
                                value=p.get('type_text',''),fetched_at=e.get('fetched_at',p['fetched_at']),confidence='source') for k in types]
                row.update(finalize_types([x['kind'] for x in prior]+types,prior+additions,p.get('pure_villa',False)))
            row['fang_profile']=dict(id=p['id'],source_url=p['source_url'],identity_basis=entry['basis'])
            quote=current_quote(quotes[p['id']],at,read(ROOT/'data_processing/fang_config.json',{}).get('price_stale_days',60))
            if quote:
                row['fang_reference']=quote;price_count+=1
        state_counts=Counter(p['state'] for p in pending)
        for key in LEGACY_STATES+('auto_accepted','quarantined','pending_location','pending_review','created_communities'):
            state_counts.setdefault(key,0)
        states=dict(state_counts)
        stats=dict(profiles=len(profiles),with_price=len(quotes),matched=published,published_prices=price_count,
                   map_communities=len(rows),states=states,
                   note='来源目录记录数量不等于物理小区数量；仅身份匹配项附加参考价，不推算缺失小区价格')
        atomic_json(OUT/'matching_candidates.json',dict(records=pending))
        atomic_json(OUT/'coverage.json',stats)
        return stats
    finally:
        if conn is not None:conn.close()


def main(argv=None):
    import argparse,json,sys
    parser=argparse.ArgumentParser(description='Publish Fang profiles onto map communities (offline).')
    parser.add_argument('--communities',help='local map communities JSON path')
    parser.add_argument('--at',help='ISO-8601 UTC timestamp for price staleness (default: now)')
    args=parser.parse_args(argv)
    if hasattr(sys.stdout,'reconfigure'):
        try:sys.stdout.reconfigure(encoding='utf-8',errors='replace')
        except Exception:pass
    path=args.communities or (ROOT/'data_processing'/'frontend'/'realestate'/'data'/'communities.json')
    rows=read(path,{}).get('communities',[])
    at=datetime.fromisoformat(args.at.replace('Z','+00:00')) if args.at else None
    stats=enrich(rows,at)
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
