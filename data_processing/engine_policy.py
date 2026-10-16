"""Conservative per-city retrospective holdout gate; never changes archived observations."""
import hashlib
import json
from datetime import datetime, timezone, date
from functools import lru_cache
from data_store import ROOT, atomic_json

REPORT = ROOT/'output/engine_validation/price_policy.json'
VERSION = 2

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()

def fingerprint(observations, indices):
    return digest(dict(version=VERSION,observations=sorted((list(k),v,observations.records.get(k,{})) for k,v in observations.items()),indices=sorted((list(k),v) for k,v in indices.items())))

def official_verified():
    try:
        reports=json.loads((REPORT.parent/'index_sources.json').read_text(encoding='utf-8'))
        files=sorted((ROOT/'output/stats_gov').glob('stats_gov_*.json'))
        checks={r['month']:r for r in reports}
        return bool(files) and all(checks.get(p.stem[-7:],{}).get('status')=='passed' and checks[p.stem[-7:]].get('input_sha256')==digest(json.loads(p.read_text(encoding='utf-8'))) for p in files)
    except (OSError,ValueError,KeyError):return False

@lru_cache(maxsize=256)
def raw_rows(path, sha):
    from anjuke_province_crawler import extract_trend, collect_rows
    content=(ROOT/path.replace('\\','/')).read_bytes()
    if hashlib.sha256(content).hexdigest()!=sha:raise ValueError('raw hash mismatch')
    info=extract_trend(content.decode('utf-8'))
    now=date.today()
    return info['city'],collect_rows(info,now.year,now.month,set())

def observed_verified(city,ym,price,record):
    try:
        raw=record['raw'];name,rows=raw_rows(raw['path'],raw['sha256'])
        return name==city and rows.get(ym)==price
    except (OSError,ValueError,KeyError,TypeError):return False

def gate(errors, baseline, signed_errors=None):
    if len(errors)<6:return 'insufficient_samples'
    if sum(errors)/len(errors)>8 or max(errors)>20:return 'excess_error'
    if errors[-1]>5 or sum(errors[-3:])/3>5:return 'recent_drift'
    if signed_errors and abs(sum(signed_errors[-3:])/3)>3:return 'recent_bias'
    if sum(errors)>sum(baseline)+1e-9:return 'worse_than_constant_anchor'
    return 'passed'

def validate(observations,indices,cities):
    from inverse_price_engine import propagate_price
    latest=max((y,m) for c,y,m in indices)
    anchor=(latest[0]-1,latest[1]);a=anchor[0]*12+anchor[1]-1
    months=[divmod(a+i,12) for i in range(1,13)]
    months=[(y,m+1) for y,m in months]
    verified=official_verified();results={}
    for city in cities:
        price=observations.get((city,*anchor));errors=[];baseline=[];predictions={};comparisons=[];signed_errors=[]
        status='missing_verified_anchor'
        if price and observed_verified(city,anchor,price,observations.records.get((city,*anchor),{})):
            mom={(y,m):v for (c,y,m),v in indices.items() if c==city}
            for ym in months:
                estimate=propagate_price(price,anchor,ym,mom)
                if estimate is None:continue
                predictions['%04d-%02d'%ym]=estimate
                actual=observations.get((city,*ym))
                if actual and observed_verified(city,ym,actual,observations.records.get((city,*ym),{})):
                    error=abs(estimate/actual-1)*100
                    signed=(estimate/actual-1)*100
                    errors.append(error);baseline.append(abs(price/actual-1)*100)
                    signed_errors.append(signed)
                    comparisons.append(dict(month='%04d-%02d'%ym,observed=actual,estimated=estimate,error_pct=error,signed_error_pct=signed))
            status=gate(errors,baseline,signed_errors) if len(predictions)==12 else 'incomplete_chain'
            recent_months={'%04d-%02d'%ym for ym in months[-3:]}
            if status=='passed' and not recent_months.issubset({r['month'] for r in comparisons}):
                status='missing_recent_observations'
        if not verified:status='official_source_unverified'
        results[city]=dict(status=status,anchor_date='%04d-%02d'%anchor,anchor_price=price,samples=len(errors),mape_pct=sum(errors)/len(errors) if errors else None,max_error_pct=max(errors) if errors else None,latest_error_pct=errors[-1] if errors else None,recent_bias_pct=sum(signed_errors[-3:])/len(signed_errors[-3:]) if signed_errors else None,baseline_mape_pct=sum(baseline)/len(baseline) if baseline else None,predictions=predictions,comparisons=comparisons)
    report=dict(version=VERSION,input_fingerprint=fingerprint(observations,indices),created_at=datetime.now(timezone.utc).isoformat(),official_verified=verified,horizon='%04d-%02d'%latest,publication_policy='verified_observation_first; nearest_verified_anchor_for_gaps; fixed_anchor_model_is_comparison_only',criteria=dict(min_samples=6,max_mape_pct=8,max_error_pct=20,max_latest_error_pct=5,max_recent_mape_pct=5,max_recent_signed_bias_pct=3,required_recent_months=3,beat_constant_anchor=True),cities=results)
    atomic_json(REPORT,report)
    return report

def load_policy(observations,indices):
    try:
        report=json.loads(REPORT.read_text(encoding='utf-8'))
        if report.get('version')==VERSION and report.get('input_fingerprint')==fingerprint(observations,indices) and official_verified():return report
    except (OSError,ValueError):pass
    return {'cities':{}}

def overlay(city,derived,report,observations):
    """Attach the fixed-anchor model for comparison without replacing observations or newer anchors."""
    item=report.get('cities',{}).get(city,{})
    if not item.get('predictions') or not report.get('official_verified'):return
    ay,am=map(int,item['anchor_date'].split('-'))
    for month,price in item['predictions'].items():
        y,m=map(int,month.split('-'))
        if (y,m) not in derived:continue
        observed=observations.get((city,y,m))
        derived[y,m]['model_comparison']=dict(price=price,anchor_date=item['anchor_date'],chain_months=(y-ay)*12+m-am,
            observed_price=observed,deviation_pct=round((price/observed-1)*100,2) if observed else None,
            validation_status=item['status'],validation_id=report['input_fingerprint'],used_for_ranking=False)

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    from inverse_price_engine import load_anjuke_data,load_stats_mom_data,load_intersection_cities
    report=validate(load_anjuke_data(),load_stats_mom_data(),[r['city'] for r in load_intersection_cities()])
    print(json.dumps({c:r['status'] for c,r in report['cities'].items()},ensure_ascii=False))
