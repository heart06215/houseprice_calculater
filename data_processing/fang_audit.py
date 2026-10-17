"""Offline integrity/quality audit for the persistent Fang dataset."""
import hashlib
import json
from collections import Counter
from datetime import datetime,timezone

from data_store import ROOT,atomic_json
from fang_store import Store,utcnow
from fang_parser import source_id,allowed_url


def audit(folder=None):
    folder=folder or ROOT/'output/fang'
    store=Store(folder);errors=[];checked=set()
    def check_raw(raw):
        if not raw or not raw.get('path') or not raw.get('sha256'):
            errors.append('Missing raw evidence reference');return
        key=(raw['path'],raw['sha256'])
        if key in checked:return
        checked.add(key);path=(ROOT/raw['path'].replace('\\', '/')).resolve()
        if ROOT.resolve() not in path.parents or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=raw['sha256']:
            errors.append('Evidence hash/path failed: '+raw['path'])
    try:
        if store.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':errors.append('SQLite integrity check failed')
        profiles={r['id']:json.loads(r['payload']) for r in store.db.execute('SELECT id,payload FROM profiles')}
        quotes=[json.loads(r[0]) for r in store.db.execute('SELECT payload FROM quotations')]
        for p in profiles.values():
            if source_id(p['source_url'])!=p['id']:errors.append('Source identity mismatch: '+p['id'])
            check_raw(p.get('raw'))
            for e in p.get('field_evidence',{}).values():check_raw(e.get('raw'))
            y=p.get('year')
            if y and not (y.get('scope')=='小区' and 1600<=y['start']<=y['end']<=datetime.now(timezone.utc).year):
                errors.append('Invalid community date: '+p['id'])
        for q in quotes:
            check_raw(q.get('raw'))
            if q['profile_id'] not in profiles or q.get('unit')!='元/㎡' or not 100<=q['value']<=1000000:
                errors.append('Invalid price: '+q['id'])
            if q['kind']=='directory_reference' and q.get('period'):
                errors.append('Directory observation incorrectly assigned a month: '+q['id'])
        for job in store.db.execute('SELECT city,url FROM jobs'):
            if not allowed_url(job['url'],job['city']):errors.append('Invalid job: '+job['url'])
        result=dict(checked_at=utcnow(),state='passed' if not errors else 'failed',errors=errors,
                    evidence_objects_checked=len(checked),**store.summary(),
                    quotation_kinds=dict(Counter(q['kind'] for q in quotes)),
                    quotations_with_signed_change=sum(q.get('change_pct') is not None for q in quotes))
        atomic_json(folder/'audit.json',result);return result
    finally:store.close()
