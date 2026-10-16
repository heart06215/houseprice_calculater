"""Transactional crawl frontier, immutable observation history and rebuildable JSON exports."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from data_store import atomic_json


def dumps(value):return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
def utcnow():return datetime.now(timezone.utc).isoformat()
def later(stamp, seconds):return (datetime.fromisoformat(stamp.replace('Z','+00:00'))+timedelta(seconds=seconds)).isoformat()

def _fact_key(field,value):
    """Compare the observed fact (object + event semantics + value), never source metadata."""
    if field=='year' and isinstance(value,dict):
        return (value.get('scope'),value.get('meaning'),value.get('start'),value.get('end'),value.get('precision'))
    return dumps(value)

def _platform(evidence):
    """Host identity, so two pages of one platform count as one independent source."""
    host=urlparse((evidence or {}).get('source_url') or '').hostname or ''
    host=host[4:] if host.startswith('www.') else host
    return host.lower()

SCHEMA_VERSION = 2
OBSERVED_FIELDS = ('year','housing_types','residential_forms','villa_subtypes','address','for_sale_count','detail_fields')
CONFLICT_FIELDS = ('year','housing_types','residential_forms')
KINDS = ('discovery', 'attribute', 'price')
KIND_ALIASES = {'catalog': 'discovery', 'detail': 'attribute'}
OVERDUE_SECONDS = 86400
OVERSIZED_FOR_SALE = 1000
YEAR_POLICY_VERSION = 'year-primary-1'
YEAR_POLICY_FIELDS = ('primary_source','policy_version','selected_source_id','year_raw',
                      'year_start','year_end','year_scope','selection_reason','cross_source_difference')


def select_publish_year(conflict_result,primary_source,policy_version=YEAR_POLICY_VERSION):
    """B10 selection layer over the raw year-conflict status; observations stay untouched.

    A cross-source difference (each platform contributes at most one differing fact group)
    selects the primary platform's own observed year. A same-platform genuine conflict (one
    platform contributes >=2 differing groups) or a missing primary year returns None, so the
    caller keeps blocking and never silently fills the year.
    """
    groups=conflict_result.get('values') or []
    by_platform={}
    for group in groups:
        for platform in {e.get('platform') for e in (group.get('evidence') or []) if e.get('platform')}:
            by_platform.setdefault(platform,[]).append(group)
    # Same-platform genuine conflict: one platform saw >=2 differing facts for one object.
    if any(len(platform_groups)>=2 for platform_groups in by_platform.values()):return None
    primary_groups=by_platform.get(primary_source) or []
    if len(primary_groups)!=1:return None
    selected=primary_groups[0]
    raw=selected.get('value') if isinstance(selected.get('value'),dict) else {}
    if raw.get('start') is None and raw.get('end') is None:return None
    # Other platforms' differing raw facts are kept as a hint, never averaged or voted on.
    others=[dict(platform=platform,value=group.get('value'))
            for platform,platform_groups in sorted(by_platform.items())
            for group in platform_groups if group is not selected]
    return {
        'primary_source':primary_source,
        'policy_version':policy_version,
        'selected_source_id':conflict_result.get('source_id'),
        'year_raw':raw,
        'year_start':raw.get('start'),
        'year_end':raw.get('end'),
        'year_scope':raw.get('scope'),
        'selection_reason':f'跨来源年份差异不阻断：采用主平台 {primary_source} 的观测年份，其他平台差异仅作提示',
        'cross_source_difference':others,
    }


def normalize_kind(kind):
    """Map legacy catalog/detail job kinds onto the explicit discovery/attribute/price vocabulary."""
    return KIND_ALIASES.get(kind, kind)


def with_age_fields(summary,at=None):
    """Derive human-relative ages from persisted absolute due stamps; never persisted so status.json stays byte-reproducible."""
    result=copy.deepcopy(summary)
    now_dt=datetime.fromisoformat((at or utcnow()).replace('Z','+00:00'))
    def age(entry):
        stamp=entry.get('oldest_due_at')
        if not stamp:return None
        try:return round((now_dt-datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds(),3)
        except ValueError:return None
    for entry in result.get('kinds',{}).values():entry['oldest_due_age_seconds']=age(entry)
    if 'backlog' in result:result['backlog']['oldest_due_age_seconds']=age(result['backlog'])
    return result


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder/'fang.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        # DELETE journal makes a closed DB a complete, portable backup (no missing WAL).
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1,SCHEMA_VERSION):raise ValueError(f'Unsupported Fang schema version: {version}')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                url TEXT PRIMARY KEY, city TEXT NOT NULL, kind TEXT NOT NULL, profile_id TEXT,
                label TEXT, due_at TEXT NOT NULL, last_success TEXT, attempts INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(enabled,city,kind,due_at);
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_versions (
                fingerprint TEXT PRIMARY KEY, profile_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quotations (
                id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quotes_profile ON quotations(profile_id,observed_at);
            CREATE INDEX IF NOT EXISTS quotes_series ON quotations(profile_id,json_extract(payload,'$.kind'),observed_at);
            CREATE TABLE IF NOT EXISTS fetches (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, at TEXT NOT NULL, status INTEGER,
                state TEXT NOT NULL, mode TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT '',
                field TEXT NOT NULL,
                value TEXT,
                raw_text TEXT,
                evidence TEXT,
                fetched_at TEXT NOT NULL,
                replay_at TEXT,
                raw_hash TEXT,
                created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observations_key ON observations(source_id,phase,field);
            CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations
            BEGIN SELECT RAISE(ABORT,'observations are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations
            BEGIN SELECT RAISE(ABORT,'observations are append-only'); END;
        ''')
        if version < SCHEMA_VERSION:self._migrate(version)
        self._migrate_job_kinds()

    def close(self):self.db.close()
    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def put(self,key,value):self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,dumps(value)))

    def _migrate(self,from_version):
        """Additive forward migration (v1->v2). No existing row is changed or removed.
        DB schema is v2; the export JSON (profiles/quotations/status) schema stays v1 and its
        shapes are unchanged - observations live only in SQLite and are never exported.
        Rollback: restore the pre-migration fang.sqlite3 backup, or run
        `DROP TABLE IF EXISTS observations; PRAGMA user_version=1; DELETE FROM meta WHERE key='store_migration';`
        Only newly appended observations are lost."""
        backfilled=self._backfill_observations()
        self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        self.put('store_migration',dict(version=SCHEMA_VERSION,from_version=from_version,at=utcnow(),observations_backfilled=backfilled))
        self.db.commit()

    def _migrate_job_kinds(self):
        """Rename legacy job kinds once; url stays the primary key so no job row is added or deleted."""
        renamed = 0
        for legacy, canonical in KIND_ALIASES.items():
            renamed += self.db.execute('UPDATE jobs SET kind=? WHERE kind=?', (canonical, legacy)).rowcount
        if renamed:
            self.put('job_kinds_migrated', dict(at=utcnow(), renamed=renamed))
            self.db.commit()
        return renamed

    def _backfill_observations(self):
        count=0
        for row in self.db.execute('SELECT id,payload FROM profiles').fetchall():
            p=json.loads(row[1]);phase=p.get('phase') or '';evidence=p.get('field_evidence',{})
            for field in OBSERVED_FIELDS:
                ev=evidence.get(field,{});value=ev.get('value')
                if value not in (None,'',[],{}):
                    stamp=ev.get('fetched_at') or p.get('last_seen') or p.get('fetched_at')
                    self.add_observation(p['id'],field,value,dict(source_url=ev.get('source_url'),raw=ev.get('raw'),field=field,backfilled=True),
                                         stamp,replay_at=stamp,phase=phase)
                    count+=1;continue
                value=p.get(field)
                if value not in (None,'',[],{}):
                    stamp=p.get('fetched_at') or p.get('last_seen')
                    self.add_observation(p['id'],field,value,
                                         dict(source_url=p.get('evidence_url') or p.get('source_url'),raw=p.get('raw'),field=field,backfilled=True),
                                         stamp,replay_at=stamp,phase=phase)
                    count+=1
        return count

    def add_observation(self,source_id,field,value,evidence,fetched_at,replay_at=None,phase='',raw_text=None):
        value_text=None if value is None else dumps(value)
        evidence_text=None if evidence is None else dumps(evidence)
        raw_hash=hashlib.sha256(f'{raw_text or ""}|{value_text or ""}'.encode()).hexdigest()
        cur=self.db.execute('INSERT INTO observations(source_id,phase,field,value,raw_text,evidence,fetched_at,replay_at,raw_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (source_id,phase,field,value_text,raw_text,evidence_text,fetched_at,replay_at or fetched_at,raw_hash,utcnow()))
        return cur.lastrowid

    def observations(self,source_id,field=None,phase=''):
        sql='SELECT * FROM observations WHERE source_id=? AND phase=?';args=[source_id,phase]
        if field is not None:sql+=' AND field=?';args.append(field)
        rows=self.db.execute(sql+' ORDER BY id',args).fetchall()
        return [dict(r,value=json.loads(r['value']) if r['value'] is not None else None,
                     evidence=json.loads(r['evidence']) if r['evidence'] is not None else None) for r in rows]

    def field_conflict(self,source_id,field,phase=''):
        rows=self.observations(source_id,field,phase);groups={}
        for row in rows:
            group=groups.setdefault(_fact_key(field,row['value']),dict(value=row['value'],evidence=[]))
            group['evidence'].append(dict(id=row['id'],evidence=row['evidence'],raw_text=row['raw_text'],
                                          fetched_at=row['fetched_at'],replay_at=row['replay_at'],raw_hash=row['raw_hash'],
                                          platform=_platform(row['evidence'])))
        status='unknown' if not groups else ('recorded' if len(groups)==1 else 'conflict')
        platforms={e['platform'] for group in groups.values() for e in group['evidence'] if e['platform']}
        return dict(source_id=source_id,field=field,phase=phase or '',status=status,distinct=len(groups),count=len(rows),
                    independent_source_count=len(platforms),values=list(groups.values()))

    def _primary_source(self,p):
        """Host identity of the profile's own platform; the platform whose year gets published."""
        return _platform({'source_url':p.get('source_url') or p.get('evidence_url')})

    def _year_blocked(self,p):
        """Blocked only when a raw conflict has no primary-platform year to select (same-platform)."""
        c=self.field_conflict(p['id'],'year')
        return c['status']=='conflict' and select_publish_year(c,self._primary_source(p)) is None

    def phases(self,source_id):
        return sorted(r[0] for r in self.db.execute("SELECT DISTINCT phase FROM observations WHERE source_id=? AND phase<>''",(source_id,)))

    def enqueue(self,city,url,kind='discovery',label='',profile_id=None,due_at=None):
        self.db.execute('INSERT OR IGNORE INTO jobs(url,city,kind,profile_id,label,due_at) VALUES(?,?,?,?,?,?)',
                        (url,city,normalize_kind(kind),profile_id,label,due_at or utcnow()))

    def attribute_gap(self,pid):
        """P0 signal: identity/attribute enrichment still missing for this profile."""
        if not pid:return True
        profile=self.profile(pid)
        if not profile:return True
        return not profile.get('year') or not (profile.get('housing_types') or profile.get('residential_forms'))

    def _needs_review(self,pid):
        """P1 signal: identity/field conflict, truncated address, or oversized estate."""
        if not pid:return False
        profile=self.profile(pid) or {}
        if profile.get('identity_changed') or profile.get('address_truncated'):return True
        if (profile.get('for_sale_count') or 0)>=OVERSIZED_FOR_SALE:return True
        return any(self.field_conflict(pid,field)['status']=='conflict' for field in CONFLICT_FIELDS)

    def _tier(self,kind,pid):
        if kind=='attribute':
            if self.attribute_gap(pid):return 0
            if self._needs_review(pid):return 1
            return 2
        if kind=='discovery':return 2
        if kind=='price':return 3
        return 4

    def next_job(self,cities,at):
        """Highest-priority due job: P0 attribute gaps, P1 review, P2 refresh, P3 price. Price never precedes attribute."""
        cities=list(cities)
        if not cities:return None
        tick=self.get('scheduler_tick',0)
        order=[cities[(tick+offset)%len(cities)] for offset in range(len(cities))]
        rotation={city:index for index,city in enumerate(order)}
        placeholders=','.join('?'*len(cities))
        rows=self.db.execute(f'SELECT * FROM jobs WHERE enabled=1 AND due_at<=? AND city IN ({placeholders})',tuple([at]+cities)).fetchall()
        best=None;best_key=None
        for row in rows:
            job=dict(row);key=(self._tier(normalize_kind(job['kind']),job['profile_id']),rotation.get(job['city'],len(cities)),job['due_at'],job['attempts'],job['url'])
            if best_key is None or key<best_key:best_key=key;best=job
        return best

    def _job_backlog(self,at=None):
        now=at or utcnow()
        now_dt=datetime.fromisoformat(now.replace('Z','+00:00'))
        kinds={kind:dict(queued=0,due=0,overdue=0,oldest_due_at=None) for kind in KINDS}
        oldest={}
        for row in self.db.execute('SELECT kind,due_at FROM jobs WHERE enabled=1'):
            kind=normalize_kind(row['kind'])
            entry=kinds.setdefault(kind,dict(queued=0,due=0,overdue=0,oldest_due_at=None))
            entry['queued']+=1
            try:due=datetime.fromisoformat(row['due_at'].replace('Z','+00:00'))
            except (AttributeError,ValueError):continue
            if due<=now_dt:
                entry['due']+=1
                if kind not in oldest or due<oldest[kind]:oldest[kind]=due;entry['oldest_due_at']=row['due_at']
                if (now_dt-due).total_seconds()>=OVERDUE_SECONDS:entry['overdue']+=1
        return kinds

    def profile(self,pid):
        row=self.db.execute('SELECT payload FROM profiles WHERE id=?',(pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def ingest(self,parsed,replay_at=None):
        replay_at=replay_at or utcnow()
        for p in parsed['profiles']:
            stamp=p['fetched_at'];payload=dumps(p);fingerprint=hashlib.sha256(payload.encode()).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO profile_versions VALUES(?,?,?,?)',(fingerprint,p['id'],stamp,payload))
            oldrow=self.db.execute('SELECT * FROM profiles WHERE id=?',(p['id'],)).fetchone()
            merged=dict(p)
            if oldrow:
                old=json.loads(oldrow['payload'])
                newer=stamp>=oldrow['last_seen']
                merged=dict(old if newer else p)
                for k,v in (p if newer else old).items():
                    # A missing field never erases earlier explicit evidence.
                    if v not in (None,'',[],{}):merged[k]=v
                merged['first_seen']=min(stamp,oldrow['first_seen'])
                merged['last_seen']=max(stamp,oldrow['last_seen'])
                if old.get('name')!=p['name']:merged['identity_changed']=True
            else:merged.update(first_seen=stamp,last_seen=stamp)
            # Preserve field-level observations; a carried value must not acquire today's timestamp.
            evidence=dict((json.loads(oldrow['payload']).get('field_evidence',{})) if oldrow else {})
            for key in ('year','housing_types','address','detail_fields','for_sale_count'):
                value=p.get(key)
                if value not in (None,'',[],{}) and stamp>=evidence.get(key,{}).get('fetched_at',''):
                    evidence[key]=dict(value=value,fetched_at=stamp,source_url=p['evidence_url'],raw=p['raw'])
            merged['field_evidence']=evidence
            self.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?)',
                            (p['id'],p['city'],merged['name'],merged['first_seen'],merged['last_seen'],dumps(merged)))
            self._observe_profile(p,replay_at)
        for q in parsed['quotations']:
            self.db.execute('INSERT OR IGNORE INTO quotations VALUES(?,?,?,?)',(q['id'],q['profile_id'],q['fetched_at'],dumps(q)))

    def _observe_profile(self,p,replay_at):
        phase=p.get('phase') or ''
        base=dict(source_url=p.get('evidence_url') or p.get('source_url'),raw=p.get('raw'),
                  page_kind=p.get('page_kind'),parser_version=p.get('parser_version'))
        for field in OBSERVED_FIELDS:
            value=p.get(field)
            if value not in (None,'',[],{}):
                self.add_observation(p['id'],field,value,dict(base,field=field,phase=phase),p['fetched_at'],replay_at,phase)
        for item in p.get('phase_observations') or []:
            child=item.get('phase') or ''
            if not child:continue
            stamp=item.get('fetched_at') or p['fetched_at']
            child_base=dict(source_url=item.get('source_url') or base['source_url'],raw=item.get('raw') or base['raw'],
                            page_kind=base['page_kind'],parser_version=base['parser_version'],child=True)
            for field in OBSERVED_FIELDS:
                value=item.get(field)
                if value not in (None,'',[],{}):
                    self.add_observation(p['id'],field,value,dict(child_base,field=field,phase=child),stamp,replay_at,child)

    def record_fetch(self,fetch,mode,state):
        self.db.execute('INSERT INTO fetches(url,at,status,state,mode,payload) VALUES(?,?,?,?,?,?)',
                        (fetch['url'],fetch['fetched_at'],fetch.get('status'),state,mode,dumps(fetch)))

    def summary(self):
        profiles=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM profiles')]
        quoted={r[0] for r in self.db.execute('SELECT DISTINCT profile_id FROM quotations')}
        by_city={}
        for p in profiles:
            e=by_city.setdefault(p['city'],dict(profiles=0,with_year=0,with_form=0,with_price=0))
            e['profiles']+=1;e['with_year']+=bool(p.get('year'));e['with_form']+=bool(p.get('housing_types'));e['with_price']+=p['id'] in quoted
        kinds=self._job_backlog()
        filterable=sum(1 for p in profiles if p.get('year') and (p.get('housing_types') or p.get('residential_forms')))
        publish_blocked=sum(1 for p in profiles if self._year_blocked(p))
        successful_requests=self.db.execute("SELECT COUNT(*) FROM fetches WHERE mode='http' AND state='ok'").fetchone()[0]
        ages=[entry['oldest_due_at'] for entry in kinds.values() if entry['oldest_due_at'] is not None]
        backlog=dict(queued=sum(entry['queued'] for entry in kinds.values()),due=sum(entry['due'] for entry in kinds.values()),
                     overdue=sum(entry['overdue'] for entry in kinds.values()),oldest_due_at=min(ages) if ages else None,
                     successful_requests=successful_requests,filterable=filterable,
                     filterable_per_100_success=round(filterable/successful_requests*100,2) if successful_requests else None)
        return dict(schema_version=1,profiles=len(profiles),quotations=self.db.execute('SELECT COUNT(*) FROM quotations').fetchone()[0],
                    with_price=len(quoted),by_city=by_city,jobs=self.db.execute('SELECT COUNT(*) FROM jobs WHERE enabled=1').fetchone()[0],
                    due_jobs=self.db.execute('SELECT COUNT(*) FROM jobs WHERE enabled=1 AND due_at<=?',(utcnow(),)).fetchone()[0],
                    pause=self.get('pause'),last_run=self.get('last_run'),last_success=self.get('last_success'),
                    daily_requests=self.get('daily'),frontier_truncated=self.get('frontier_truncated',False),last_replay=self.get('last_replay'),
                    publish_blocked=publish_blocked,year_conflicts=publish_blocked,kinds=kinds,backlog=backlog)

    def export(self):
        self.db.commit()
        profiles=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM profiles ORDER BY city,id')]
        # Publish gate: a same-platform year conflict blocks the year, but a cross-source
        # difference selects the primary platform's own year instead of demoting it.
        for p in profiles:
            c=self.field_conflict(p['id'],'year')
            policy=select_publish_year(c,self._primary_source(p)) if c['status']=='conflict' else None
            if c['status']=='conflict' and policy is None:
                p['year_status']='conflict';p['year']=None;p['year_publishable']=False
                p['publish_gate']={'status':'blocked','year':{'distinct':c['distinct'],
                    'independent_source_count':c['independent_source_count'],'values':[g['value'] for g in c['values']]}}
            else:
                if policy is not None:
                    p['year']=policy['year_raw'];p['year_policy']=policy;p['year_status']='recorded'
                else:
                    p['year_status']='recorded' if c['status']=='recorded' else 'unknown'
                p['year_publishable']=True;p['publish_gate']={'status':'open'}
        # Keep the frequently rebuilt frontend export bounded. All historical observations
        # remain in SQLite and immutable page snapshots, not in an ever-growing JS payload.
        quotes=[json.loads(r[0]) for r in self.db.execute('''
            SELECT q.payload FROM quotations q JOIN (
                SELECT profile_id,json_extract(payload,'$.kind') kind,MAX(observed_at) stamp
                FROM quotations GROUP BY profile_id,json_extract(payload,'$.kind')
            ) latest ON q.profile_id=latest.profile_id AND json_extract(q.payload,'$.kind')=latest.kind AND q.observed_at=latest.stamp
            ORDER BY q.observed_at,q.id
        ''')]
        atomic_json(self.folder/'profiles.json',dict(schema_version=1,records=profiles))
        atomic_json(self.folder/'quotations.json',dict(schema_version=1,scope='latest_per_profile_and_kind',history='fang.sqlite3 / quotations',records=quotes))
        summary=self.summary();atomic_json(self.folder/'status.json',summary)
        return summary
