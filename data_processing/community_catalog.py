"""Cross-source stable community catalog.

Keeps a platform-owned ``community_id`` separate from each source object, so a
verified community can enter the map catalog without an existing OSM row while
records without a reliable position become 待定位 candidates. Append-only
observations, versioned geometries and revocable identity links preserve
evidence. Standard library + existing repo modules only; no network.
"""
import hashlib
import json
import math
import re
import sqlite3

import judgement
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

# Repo root pattern shared by data_store / geocode_communities. Sibling imports
# stay flat (data_processing is the import root when run via the project entry).
ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / 'output' / 'catalog'
DEFAULT_DB = CATALOG_DIR / 'catalog.sqlite3'

SCHEMA_VERSION = 2

COORD_SYSTEMS = ('wgs84', 'gcj02', 'bd09', 'other')
PRECISION_LEVELS = ('point', 'street', 'district', 'unknown')
RELATION_TYPES = ('phase', 'subgroup')
MATCH_STATUSES = ('supported', 'excluded', 'unknown', 'conflict')
LINK_STATUSES = ('accepted', 'rejected', 'pending')
METRO_SCOPE = 'metro_lrt_operational'
METRO_THRESHOLD_M = 2000.0
LOCATION_MATCH_M = 200.0
CONFLICT_DISTANCE_M = 1000.0
# F3: 行政区/城市 only narrows recall; a candidate must ALSO carry a substantive
# identity signal. Name similarity uses this documented SequenceMatcher ratio.
NAME_SIMILARITY_THRESHOLD = 0.6
# Substantive proximity recall radius: both sides need comparable WGS84 coords.
PROXIMITY_RECALL_M = LOCATION_MATCH_M  # 200.0
# Spatial grid cell (~1.1 km/deg) so a 200 m radius only scans 3x3 neighbour cells.
PROXIMITY_CELL_DEG = 0.01
# Substantive identity signals; 行政区/城市 is deliberately NOT one.
SUBSTANTIVE_SIGNALS = ('来源映射', '名称一致', '别名一致', '名称相似', '地址', '邻近位置')

# One physical source object must never split across aliases (房天下 vs fang).
SOURCE_ALIASES = {'房天下': 'fang'}


def normalize_source(source):
    if source is None:
        return source
    return SOURCE_ALIASES.get(str(source), str(source))


_DDL = '''
CREATE TABLE IF NOT EXISTS communities (
    community_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT DEFAULT '[]',
    phase TEXT,
    parent_id TEXT,
    relation_type TEXT CHECK (relation_type IN ('phase','subgroup') OR relation_type IS NULL),
    district_code TEXT,
    city_code TEXT,
    address_raw TEXT,
    address_normalized TEXT,
    addr_house_no TEXT,
    addr_house_nos TEXT DEFAULT '[]',
    addr_lane TEXT,
    addr_courtyard TEXT,
    addr_phase_token TEXT,
    addr_subgroup_token TEXT,
    year_raw_text TEXT,
    year_start INTEGER,
    year_end INTEGER,
    year_precision TEXT,
    year_meaning TEXT,
    year_scope TEXT,
    residential_forms TEXT DEFAULT '[]',
    villa_subtypes TEXT DEFAULT '[]',
    identity_status TEXT DEFAULT 'unknown',
    admin_area_status TEXT DEFAULT 'unknown',
    year_status TEXT DEFAULT 'unknown',
    villa_type_status TEXT DEFAULT 'unknown',
    location_status TEXT DEFAULT 'unknown',
    metro_status TEXT DEFAULT 'unknown',
    coverage_note TEXT,
    source_fetched_at TEXT,
    has_osm INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS source_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT,
    source_url TEXT,
    payload TEXT,
    community_id TEXT,
    match_status TEXT DEFAULT 'unknown',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id TEXT,
    field TEXT NOT NULL,
    raw_text TEXT,
    value_json TEXT,
    evidence TEXT,
    fetched_at TEXT,
    replay_at TEXT,
    raw_hash TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS geometries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id TEXT NOT NULL,
    coord_system TEXT NOT NULL,
    coord_src_lon REAL,
    coord_src_lat REAL,
    coord_wgs84_lon REAL,
    coord_wgs84_lat REAL,
    precision_level TEXT NOT NULL DEFAULT 'unknown',
    accuracy_m REAL,
    source TEXT,
    evidence TEXT,
    transform_version TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    is_current INTEGER NOT NULL DEFAULT 1,
    location_note TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS identity_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id TEXT NOT NULL,
    source_entity_id INTEGER NOT NULL,
    relation TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    evidence TEXT,
    decided_by TEXT,
    decided_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS directory_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_code TEXT,
    district_code TEXT,
    directory_section TEXT,
    pages INTEGER,
    truncated INTEGER,
    status TEXT,
    dedup_count INTEGER,
    fetched_at TEXT,
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS metro_stations (
    station_id TEXT PRIMARY KEY,
    name TEXT,
    city_code TEXT,
    operational_status TEXT,
    line_info TEXT,
    lng REAL,
    lat REAL,
    coord_system TEXT,
    station_registry_version TEXT,
    evidence TEXT,
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS proximity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id TEXT,
    geometry_version INTEGER,
    station_registry_version TEXT,
    nearest_station_id TEXT,
    distance_m REAL,
    distance_lower_m REAL,
    distance_upper_m REAL,
    distance_basis TEXT DEFAULT 'straight_line',
    threshold_m REAL,
    boundary_judgement TEXT,
    computed_at TEXT
);
CREATE TABLE IF NOT EXISTS review_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id TEXT,
    reason TEXT,
    priority INTEGER DEFAULT 1,
    evidence TEXT,
    status TEXT DEFAULT 'open',
    result TEXT,
    history TEXT DEFAULT '[]',
    created_at TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS query_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_json TEXT,
    data_version TEXT,
    result_counts TEXT,
    sample TEXT,
    missing_eval TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS source_entities_community ON source_entities(community_id);
CREATE INDEX IF NOT EXISTS observations_community_field ON observations(community_id, field);
CREATE INDEX IF NOT EXISTS geometries_community_current ON geometries(community_id, is_current);
CREATE INDEX IF NOT EXISTS identity_links_community ON identity_links(community_id);
CREATE INDEX IF NOT EXISTS review_tasks_status ON review_tasks(status);
CREATE INDEX IF NOT EXISTS proximity_community ON proximity(community_id);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
'''


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _json_load(s, default=None):
    if s is None:
        return default
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def _row(row):
    return dict(row) if row is not None else None


def _as_float(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_wgs84_to_gcj02():
    """Lazy sibling import; unavailable conversion must not break the catalog."""
    try:
        from geocode_communities import wgs84_to_gcj02
    except ImportError:
        return None
    return wgs84_to_gcj02


def fang_source_id(url):
    """Optional wrapper delegating to fang_parser.source_id (lazy, flat import)."""
    try:
        from fang_parser import source_id
    except ImportError:
        return None
    return source_id(url)


def _distance_m(lat1, lng1, lat2, lng2):
    """Haversine straight-line distance in metres. Never used for mixed systems."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _tokens(s):
    """CJK runs and digit runs; used as a secondary name-overlap signal."""
    return set(re.findall(r'[\u4e00-\u9fff]+|\d+', s or ''))


_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20


def normalize_address(value):
    """Trim, fold full-width chars, collapse whitespace.

    Deliberately does not delete or merge 门牌号 / 弄 / 院 / 期次 / 组团 tokens,
    so "XX路100号、102号" keeps both house numbers and 别墅区/公寓区/一期/二期
    survive matching.
    """
    if value is None:
        return None
    text = str(value).translate(_FULLWIDTH)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or None


# --------------------------------------------------------------------------- #
# connection + schema
# --------------------------------------------------------------------------- #

def connect(path=None):
    """Open the catalog DB (default DEFAULT_DB), creating the parent folder."""
    db_path = Path(path) if path is not None else DEFAULT_DB
    if str(db_path) != ':memory:':
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    # DELETE journal keeps a closed DB a complete portable backup (no missing WAL).
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def create_schema(conn):
    """Create the 11 catalog tables and stamp PRAGMA user_version."""
    version = get_schema_version(conn)
    if version not in (0, 1, SCHEMA_VERSION):
        raise ValueError(f'Unsupported catalog schema version: {version}')
    conn.executescript('BEGIN;\n' + _DDL + f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;')
    conn.commit()
    return SCHEMA_VERSION


def get_schema_version(conn):
    return int(conn.execute('PRAGMA user_version').fetchone()[0])


# --------------------------------------------------------------------------- #
# source objects + observations
# --------------------------------------------------------------------------- #

def upsert_source_entity(conn, source, source_id, name, payload):
    """Idempotent by (source, source_id); a source object stays its own row.

    Payload is merged, not replaced: incoming keys override while stored evidence
    markers (e.g. catalog_ingest_state) survive a partial update. A None payload
    keeps the stored payload so callers cannot accidentally strip markers.
    """
    source = normalize_source(source)
    now = _utcnow()
    source_url = payload.get('source_url') if isinstance(payload, dict) else None
    existing = conn.execute(
        'SELECT payload FROM source_entities WHERE source=? AND source_id=?',
        (source, source_id)).fetchone()
    if payload is None:
        payload_text = existing['payload'] if existing is not None else None
    else:
        existing_payload = _json_load(existing['payload'], {}) if existing is not None else {}
        merged = dict(existing_payload) if isinstance(existing_payload, dict) else {}
        merged.update(payload)
        payload_text = _json_dump(merged)
    conn.execute(
        '''INSERT INTO source_entities (source, source_id, name, source_url, payload, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, source_id) DO UPDATE SET
               name = excluded.name,
               source_url = COALESCE(excluded.source_url, source_entities.source_url),
               payload = excluded.payload,
               updated_at = excluded.updated_at''',
        (source, source_id, name, source_url, payload_text, now, now))
    conn.commit()
    return _row(conn.execute(
        'SELECT * FROM source_entities WHERE source=? AND source_id=?', (source, source_id)).fetchone())


def merge_duplicate_sources(conn):
    """Collapse alias source rows onto the canonical source; never deletes.

    Alias rows without a canonical twin are renamed in place. A twin (same
    (canonical, source_id)) keeps its id while the alias row is soft-marked
    'merged' and its identity links repointed, so history survives.
    """
    now = _utcnow()
    merged = 0
    for alias, canonical in SOURCE_ALIASES.items():
        for raw in conn.execute('SELECT * FROM source_entities WHERE source=?', (alias,)).fetchall():
            twin = conn.execute('SELECT * FROM source_entities WHERE source=? AND source_id=?',
                                (canonical, raw['source_id'])).fetchone()
            if twin is None:
                conn.execute('UPDATE source_entities SET source=?, updated_at=? WHERE id=?',
                             (canonical, now, raw['id']))
                merged += 1
                continue
            payload = _json_load(raw['payload'], {}) or {}
            payload['superseded_by'] = twin['id']
            payload['merged_into'] = canonical
            conn.execute('UPDATE identity_links SET source_entity_id=? WHERE source_entity_id=?',
                         (twin['id'], raw['id']))
            conn.execute(
                "UPDATE source_entities SET community_id=NULL, match_status='merged', payload=?, "
                "updated_at=? WHERE id=?",
                (_json_dump(payload), now, raw['id']))
            merged += 1
    conn.commit()
    return merged


def add_observation(conn, community_id, field, raw_text, value_json, evidence, fetched_at, replay_at):
    """Append-only: never upserts, never overwrites earlier evidence."""
    now = _utcnow()
    value_text = None if value_json is None else _json_dump(value_json)
    evidence_text = None if evidence is None else _json_dump(evidence)
    raw_hash = hashlib.sha256(f'{raw_text or ""}|{value_text or ""}'.encode('utf-8')).hexdigest()
    cur = conn.execute(
        '''INSERT INTO observations
           (community_id, field, raw_text, value_json, evidence, fetched_at, replay_at, raw_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (community_id, field, raw_text, value_text, evidence_text, fetched_at, replay_at, raw_hash, now))
    conn.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# geometries
# --------------------------------------------------------------------------- #

def set_geometry(conn, community_id, lng, lat, coord_system, precision_level,
                 source, evidence, accuracy_m=None):
    """Insert a new geometry version and retire the previous current row.

    coord_wgs84_* keeps the WGS84 value used for distance; for wgs84 sources it is
    the identity of the raw pair and transform_version records the lazily probed
    display transform. gcj02/bd09 cannot be normalised with the forward-only
    helper, so coord_wgs84 stays NULL with an 'unsupported:<system>' note.
    """
    if coord_system not in COORD_SYSTEMS:
        raise ValueError(f'Invalid coord_system: {coord_system!r}')
    if precision_level not in PRECISION_LEVELS:
        raise ValueError(f'Invalid precision_level: {precision_level!r}')
    src_lon, src_lat = _as_float(lng), _as_float(lat)
    wgs_lon = wgs_lat = None
    location_note = None
    if src_lon is None or src_lat is None:
        transform_version = 'passthrough'
        location_note = '缺少坐标，未记录 WGS84'
    elif coord_system == 'wgs84':
        # Probe conversion once so downstream display knows the transform version.
        transform_version = 'wgs84->gcj02:v1' if _get_wgs84_to_gcj02() else 'passthrough'
        wgs_lon, wgs_lat = src_lon, src_lat
    else:
        transform_version = f'unsupported:{coord_system}'
        location_note = f'coord_system={coord_system} 无可用转换，coord_wgs84 留空'

    row = conn.execute(
        'SELECT MAX(version) AS v FROM geometries WHERE community_id=?', (community_id,)).fetchone()
    version = (row['v'] or 0) + 1
    conn.execute('UPDATE geometries SET is_current=0 WHERE community_id=?', (community_id,))
    cur = conn.execute(
        '''INSERT INTO geometries
           (community_id, coord_system, coord_src_lon, coord_src_lat, coord_wgs84_lon, coord_wgs84_lat,
            precision_level, accuracy_m, source, evidence, transform_version, version, is_current,
            location_note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)''',
        (community_id, coord_system, src_lon, src_lat, wgs_lon, wgs_lat,
         precision_level, _as_float(accuracy_m), source, None if evidence is None else _json_dump(evidence),
         transform_version, version, location_note, _utcnow()))
    conn.execute('UPDATE communities SET location_status=?, updated_at=? WHERE community_id=?',
                 ('known' if wgs_lon is not None else 'unknown', _utcnow(), community_id))
    conn.commit()
    return _row(conn.execute('SELECT * FROM geometries WHERE id=?', (cur.lastrowid,)).fetchone())


def _current_geometry(conn, community_id):
    return _row(conn.execute(
        'SELECT * FROM geometries WHERE community_id=? AND is_current=1 ORDER BY version DESC LIMIT 1',
        (community_id,)).fetchone())


def _current_wgs_pair(conn, community_id):
    geom = _current_geometry(conn, community_id)
    if not geom:
        return None
    lon, lat = geom.get('coord_wgs84_lon'), geom.get('coord_wgs84_lat')
    if lon is None or lat is None:
        return None
    return (lat, lon)


# --------------------------------------------------------------------------- #
# identity links (revocable, never hard-deleted)
# --------------------------------------------------------------------------- #

def link_identity(conn, community_id, source_entity_id, relation, evidence, decided_by):
    if source_entity_id is None:
        raise ValueError('source_entity_id is required')
    relation = relation or 'pending'
    now = _utcnow()
    cur = conn.execute(
        '''INSERT INTO identity_links
           (community_id, source_entity_id, relation, status, evidence, decided_by, decided_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (community_id, source_entity_id, relation, relation,
         None if evidence is None else _json_dump(evidence), decided_by, now, now))
    conn.commit()
    return _row(conn.execute('SELECT * FROM identity_links WHERE id=?', (cur.lastrowid,)).fetchone())


def revoke_identity(conn, link_id, reason):
    row = conn.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone()
    if row is None:
        raise ValueError(f'identity link not found: {link_id}')
    conn.execute('UPDATE identity_links SET status=?, revoked_at=?, revoke_reason=? WHERE id=?',
                 ('revoked', _utcnow(), reason, link_id))
    conn.commit()
    return _row(conn.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone())


def _accepted_link(conn, source, source_id):
    if not source or not source_id:
        return None
    return _row(conn.execute(
        '''SELECT il.* FROM identity_links il
           JOIN source_entities se ON se.id = il.source_entity_id
           WHERE se.source=? AND se.source_id=? AND il.status='accepted'
           ORDER BY il.id DESC LIMIT 1''', (source, source_id)).fetchone())


# --------------------------------------------------------------------------- #
# community ids + community rows
# --------------------------------------------------------------------------- #

def _city_slug(city_code):
    # ASSUMPTION: the repo exposes no city pinyin map, so the slug is the
    # lowercased alnum form of city_code; codes without alnum fall back to
    # 'unknown' (may share a sequence across such cities).
    slug = re.sub(r'[^0-9a-z]', '', (city_code or '').lower())
    return slug or 'unknown'


def allocate_community_id(conn, city_code):
    """Permanent monotonic id ``comm:<slug>:<8位序号>``; ids are never reused."""
    slug = _city_slug(city_code)
    prefix = f'comm:{slug}:'
    counter = f'community_seq:{slug}'
    row = conn.execute('SELECT value FROM counters WHERE name = ?', (counter,)).fetchone()
    if row is None:
        seed = 0
        for item in conn.execute('SELECT community_id FROM communities WHERE community_id LIKE ?',
                                 (prefix + '%',)).fetchall():
            tail = item['community_id'][len(prefix):]
            if tail.isdigit():
                seed = max(seed, int(tail))
    else:
        seed = int(row['value'])
    seq = seed + 1
    conn.execute('INSERT OR REPLACE INTO counters(name, value) VALUES (?, ?)', (counter, seq))
    return f'{prefix}{seq:08d}'


def get_community(conn, community_id):
    return _row(conn.execute('SELECT * FROM communities WHERE community_id=?', (community_id,)).fetchone())


# --------------------------------------------------------------------------- #
# matching: score components
# --------------------------------------------------------------------------- #

def _normalize_name(name):
    return re.sub(r'\s+', '', name or '')


def _name_exact(a, b):
    a, b = _normalize_name(a), _normalize_name(b)
    return bool(a) and a == b


def _name_similar(a, b):
    a, b = _normalize_name(a), _normalize_name(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= NAME_SIMILARITY_THRESHOLD


def _name_shingles(name):
    """Character bigrams used as the recall index for fuzzy name similarity."""
    text = _normalize_name(name)
    if not text:
        return set()
    if len(text) < 2:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _proximity_cell(lat, lng):
    return (math.floor(lat / PROXIMITY_CELL_DEG), math.floor(lng / PROXIMITY_CELL_DEG))


def _phase_match(obs_phase, comm_phase):
    obs_phase, comm_phase = _normalize_name(obs_phase), _normalize_name(comm_phase)
    return bool(obs_phase and comm_phase and obs_phase == comm_phase)


def _address_match(observation, community):
    a = normalize_address(observation.get('address'))
    b = normalize_address(community.get('address_normalized') or community.get('address_raw'))
    return bool(a and b and a == b)


def _obs_wgs_pair(observation):
    lng, lat = _as_float(observation.get('longitude')), _as_float(observation.get('latitude'))
    if lng is None or lat is None:
        return None
    # Compared coordinates must already be WGS84; other systems cannot be
    # normalised with the forward-only helper, so they are treated as unknown.
    if observation.get('coord_system') not in (None, '', 'wgs84'):
        return None
    return (lat, lng)


def _source_entity_linked(conn, source, source_id, community_id):
    if not source or not source_id:
        return False
    row = conn.execute('SELECT community_id FROM source_entities WHERE source=? AND source_id=?',
                       (source, source_id)).fetchone()
    if row is not None and row['community_id'] == community_id:
        return True
    row = conn.execute(
        '''SELECT 1 FROM identity_links il JOIN source_entities se ON se.id = il.source_entity_id
           WHERE se.source=? AND se.source_id=? AND il.community_id=? AND il.status='accepted' LIMIT 1''',
        (source, source_id, community_id)).fetchone()
    return row is not None


def _linked_community_ids(conn, observation):
    """Community ids already mapped to this exact source object."""
    source = normalize_source(observation.get('source'))
    source_id = observation.get('source_id')
    ids = set()
    if not source or not source_id:
        return ids
    row = conn.execute('SELECT community_id FROM source_entities WHERE source=? AND source_id=?',
                       (source, source_id)).fetchone()
    if row is not None and row['community_id']:
        ids.add(row['community_id'])
    for r in conn.execute(
            '''SELECT il.community_id FROM identity_links il
               JOIN source_entities se ON se.id = il.source_entity_id
               WHERE se.source=? AND se.source_id=? AND il.status='accepted' ''',
            (source, source_id)).fetchall():
        if r['community_id']:
            ids.add(r['community_id'])
    return ids


def _substantive_signals(observation, community, aliases, components):
    """Identity signals that can carry a candidate; 行政区/城市 alone cannot."""
    signals = []
    if components.get('来源对象ID', {}).get('matched'):
        signals.append('来源映射')
    if components.get('完整名称', {}).get('matched'):
        signals.append('名称一致')
    elif components.get('别名', {}).get('matched'):
        signals.append('别名一致')
    elif _name_similar(observation.get('name'), community.get('name')):
        signals.append('名称相似')
    if components.get('地址', {}).get('matched'):
        signals.append('地址')
    if components.get('坐标及精度', {}).get('matched'):
        signals.append('邻近位置')
    return signals


def _recall_candidates(conn, observation):
    """Narrow recall first, then return only raw rows worth scoring.

    Step 1 (scope): 行政区/城市 only narrows the search space.
    Step 2 (index): a candidate must be reached through the source-mapping
    lookup, the name/alias bigram index, the normalized-address index or the
    proximity grid, so a city with thousands of communities does not return
    thousands of candidates. The substantive gate is applied in match_candidates.
    """
    obs_city = observation.get('city_code')
    scope = []
    if obs_city:
        scope = [dict(r) for r in conn.execute(
            'SELECT * FROM communities WHERE city_code=?', (obs_city,)).fetchall()]
    if not scope:
        # City-code granularity can differ between map rows and source records
        # (e.g. 110000 vs 110100). An unknown code must not exclude real matches,
        # so fall back to the full table; the substantive gate still governs.
        scope = [dict(r) for r in conn.execute('SELECT * FROM communities').fetchall()]

    by_id = {}
    exact_index = {}
    shingle_index = {}
    addr_index = {}
    for community in scope:
        cid = community['community_id']
        by_id[cid] = community
        names = {_normalize_name(community.get('name'))}
        names |= {_normalize_name(a) for a in (_json_load(community.get('aliases'), []) or [])}
        names -= {''}
        for name in names:
            exact_index.setdefault(name, []).append(cid)
            for shingle in _name_shingles(name):
                shingle_index.setdefault(shingle, []).append(cid)
        addr = normalize_address(community.get('address_normalized') or community.get('address_raw'))
        if addr:
            addr_index.setdefault(addr, []).append(cid)

    recalled = set(_linked_community_ids(conn, observation)) & set(by_id)

    obs_names = {_normalize_name(observation.get('name'))}
    obs_names |= {_normalize_name(a) for a in (observation.get('aliases') or [])}
    obs_names -= {''}
    for name in obs_names:
        recalled.update(exact_index.get(name, ()))

    obs_shingles = set()
    for name in obs_names:
        obs_shingles |= _name_shingles(name)
    shingle_hits = set()
    for shingle in obs_shingles:
        shingle_hits.update(shingle_index.get(shingle, ()))
    for cid in shingle_hits:
        community = by_id[cid]
        names = {_normalize_name(community.get('name'))}
        names |= {_normalize_name(a) for a in (_json_load(community.get('aliases'), []) or [])}
        if any(_name_similar(observation.get('name'), name) for name in names if name):
            recalled.add(cid)

    obs_addr = normalize_address(observation.get('address'))
    if obs_addr:
        recalled.update(addr_index.get(obs_addr, ()))

    obs_pair = _obs_wgs_pair(observation)
    if obs_pair:
        lat0, lng0 = obs_pair
        grid = {}
        sql = '''SELECT g.community_id, g.coord_wgs84_lat lat, g.coord_wgs84_lon lon
                 FROM geometries g JOIN communities c ON c.community_id = g.community_id
                 WHERE g.is_current=1 AND g.coord_wgs84_lat IS NOT NULL
                   AND g.coord_wgs84_lon IS NOT NULL'''
        params = ()
        if obs_city:
            sql += ' AND c.city_code=?'
            params = (obs_city,)
        for r in conn.execute(sql, params).fetchall():
            grid.setdefault(_proximity_cell(r['lat'], r['lon']), []).append(r)
        clat, clon = _proximity_cell(lat0, lng0)
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                for r in grid.get((clat + dlat, clon + dlon), ()):
                    cid = r['community_id']
                    if cid in recalled or cid not in by_id:
                        continue
                    dist = _distance_m(lat0, lng0, r['lat'], r['lon'])
                    if dist is not None and dist <= PROXIMITY_RECALL_M:
                        recalled.add(cid)

    return [by_id[cid] for cid in sorted(recalled)]


def _score_components(conn, observation, community, aliases):
    comps = {}
    obs_dist, obs_city = observation.get('district_code'), observation.get('city_code')
    comm_dist, comm_city = community.get('district_code'), community.get('city_code')
    admin = bool(obs_dist and comm_dist and obs_dist == comm_dist) or (
        not obs_dist and not comm_dist and obs_city and comm_city and obs_city == comm_city)
    comps['行政区代码'] = {
        'matched': admin, 'score': 0.15 if admin else 0.0,
        'detail': f'observation district={obs_dist!r} community district={comm_dist!r}'}

    name_matched = _name_exact(observation.get('name'), community.get('name'))
    comps['完整名称'] = {
        'matched': name_matched, 'score': 0.35 if name_matched else 0.0,
        'detail': f'shared tokens={sorted(_tokens(_normalize_name(observation.get("name"))) & _tokens(_normalize_name(community.get("name"))))}'}

    obs_names = {_normalize_name(observation.get('name'))}
    obs_names |= {_normalize_name(a) for a in (observation.get('aliases') or [])}
    alias_set = {_normalize_name(a) for a in (aliases or [])}
    alias_matched = bool(obs_names & alias_set - {''})
    comps['别名'] = {
        'matched': alias_matched, 'score': 0.25 if alias_matched else 0.0,
        'detail': f'community aliases={sorted(alias_set)}'}

    phase_matched = _phase_match(observation.get('phase'), community.get('phase'))
    comps['期次'] = {
        'matched': phase_matched, 'score': 0.10 if phase_matched else 0.0,
        'detail': f'observation phase={observation.get("phase")!r} community phase={community.get("phase")!r}'}

    addr_matched = _address_match(observation, community)
    comps['地址'] = {
        'matched': addr_matched, 'score': 0.20 if addr_matched else 0.0,
        'detail': f'normalized={normalize_address(observation.get("address"))!r}'}

    sid_matched = _source_entity_linked(conn, observation.get('source'), observation.get('source_id'),
                                        community['community_id'])
    comps['来源对象ID'] = {
        'matched': sid_matched, 'score': 0.30 if sid_matched else 0.0,
        'detail': f'source={observation.get("source")!r} source_id={observation.get("source_id")!r}'}

    obs_pair = _obs_wgs_pair(observation)
    comm_pair = _current_wgs_pair(conn, community['community_id'])
    loc_matched, loc_detail = False, '缺少可比 WGS84 坐标'
    if obs_pair and comm_pair:
        dist = _distance_m(obs_pair[0], obs_pair[1], comm_pair[0], comm_pair[1])
        precision = observation.get('precision_level') or 'unknown'
        loc_matched = dist is not None and dist <= LOCATION_MATCH_M and precision in ('point', 'district')
        loc_detail = f'distance={dist:.0f}m precision={precision}'
    comps['坐标及精度'] = {'matched': loc_matched, 'score': 0.15 if loc_matched else 0.0, 'detail': loc_detail}
    return comps


def match_candidates(conn, observation):
    """Recall by 行政区/城市 scope, emit only communities with a substantive signal.

    A city/district match alone is a scope filter, never a candidate. Each result
    reports the substantive signal(s) in ``reason``/``substantive_signals`` plus the
    per-component ``score_components``.
    """
    results = []
    for community in _recall_candidates(conn, observation):
        aliases = _json_load(community.get('aliases'), [])
        components = _score_components(conn, observation, community, aliases)
        signals = _substantive_signals(observation, community, aliases, components)
        if not signals:
            continue
        results.append({
            'community_id': community['community_id'],
            'name': community['name'],
            'has_osm': bool(community.get('has_osm')),
            'reason': signals,
            'substantive_signals': signals,
            'score_components': components,
            'score': round(sum(c['score'] for c in components.values()), 4)})
    results.sort(key=lambda c: c['score'], reverse=True)
    return results


# --------------------------------------------------------------------------- #
# review tasks
# --------------------------------------------------------------------------- #

def add_review_task(conn, community_id=None, reason='', priority=1, evidence=None,
                    source=None, source_id=None):
    now = _utcnow()
    payload = dict(evidence) if isinstance(evidence, dict) else ({'detail': evidence} if evidence is not None else {})
    if source:
        payload['source'] = source
    if source_id:
        payload['source_id'] = source_id
    cur = conn.execute(
        '''INSERT INTO review_tasks (community_id, reason, priority, evidence, status, history, created_at)
           VALUES (?, ?, ?, ?, 'open', '[]', ?)''',
        (community_id, reason, priority, _json_dump(payload), now))
    conn.commit()
    return _row(conn.execute('SELECT * FROM review_tasks WHERE id=?', (cur.lastrowid,)).fetchone())


def open_review_tasks(conn, community_id=None):
    if community_id is None:
        rows = conn.execute("SELECT * FROM review_tasks WHERE status='open' ORDER BY priority, id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_tasks WHERE status='open' AND community_id=? ORDER BY priority, id",
            (community_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# decide match
# --------------------------------------------------------------------------- #

def _ensure_source_entity(conn, observation):
    source = normalize_source(observation.get('source'))
    source_id = observation.get('source_id')
    if not source or not source_id:
        return None
    payload = {k: observation.get(k) for k in
               ('city_code', 'district_code', 'address', 'phase', 'longitude', 'latitude',
                'coord_system', 'precision_level', 'has_osm', 'aliases')
               if observation.get(k) is not None}
    return upsert_source_entity(conn, source, source_id, observation.get('name'), payload)


def _link_observation(conn, observation, community_id):
    entity = _ensure_source_entity(conn, observation)
    if entity is None:
        return None
    conn.execute('UPDATE source_entities SET community_id=?, match_status=? WHERE id=?',
                 (community_id, 'supported', entity['id']))
    conn.commit()
    return link_identity(conn, community_id, entity['id'], 'accepted',
                         {'via': 'decide_match', 'source': observation.get('source'),
                          'source_id': observation.get('source_id')}, 'auto')


def _create_community(conn, observation):
    now = _utcnow()
    source = normalize_source(observation.get('source'))
    community_id = allocate_community_id(conn, observation.get('city_code'))
    has_wgs = _obs_wgs_pair(observation) is not None
    parent_id, relation_type = observation.get('parent_id'), observation.get('relation_type')
    if relation_type is not None and relation_type not in RELATION_TYPES:
        raise ValueError(f'Invalid relation_type: {relation_type!r}')
    year_start, year_end = observation.get('year_start'), observation.get('year_end')
    forms = list(observation.get('residential_forms') or [])
    subtypes = list(observation.get('villa_subtypes') or [])
    conn.execute(
        '''INSERT INTO communities
           (community_id, name, aliases, phase, parent_id, relation_type, district_code, city_code,
            address_raw, address_normalized, year_raw_text, year_start, year_end, year_precision,
            year_meaning, year_scope, residential_forms, villa_subtypes,
            identity_status, admin_area_status, year_status, villa_type_status,
            location_status, metro_status, has_osm, coverage_note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   'unknown', 'unknown', ?, ?, ?, 'unknown', 0, ?, ?, ?)''',
        (community_id, observation.get('name'),
         _json_dump(observation.get('aliases') or []), observation.get('phase'),
         parent_id, relation_type,
         observation.get('district_code'), observation.get('city_code'),
         observation.get('address'), normalize_address(observation.get('address')),
         observation.get('year_label'), year_start, year_end, observation.get('year_precision'),
         observation.get('year_meaning'), observation.get('year_scope'),
         _json_dump(forms), _json_dump(subtypes),
         'recorded' if (year_start or year_end) else 'unknown',
         'known' if forms else 'unknown',
         'known' if has_wgs else 'unknown',
         '来源身份清楚且坐标可靠；无OSM行独立入主目录', now, now))
    conn.commit()
    fetched = observation.get('fetched_at')
    if year_start is not None or year_end is not None:
        add_observation(conn, community_id, 'year', observation.get('year_label'),
                        {'start': year_start, 'end': year_end,
                         'precision': observation.get('year_precision'),
                         'meaning': observation.get('year_meaning'),
                         'scope': observation.get('year_scope')},
                        {'source': source, 'source_url': observation.get('source_url')},
                        fetched, fetched)
    if forms:
        add_observation(conn, community_id, 'type', observation.get('type_text'),
                        forms, {'source': source, 'source_url': observation.get('source_url')},
                        fetched, fetched)
    geometry_id = None
    if observation.get('longitude') is not None and observation.get('latitude') is not None:
        geom = set_geometry(conn, community_id, observation.get('longitude'), observation.get('latitude'),
                            observation.get('coord_system') or 'wgs84',
                            observation.get('precision_level') or 'unknown',
                            observation.get('source'), {'via': 'new_record'})
        geometry_id = geom['id']
    _link_observation(conn, observation, community_id)
    return community_id, geometry_id


def _review(conn, observation, candidate, reason):
    task = add_review_task(
        conn, community_id=candidate['community_id'] if candidate else None,
        reason=reason,
        evidence={'observation_name': observation.get('name'),
                  'candidates': [c['community_id'] for c in (candidate and [candidate] or [])]},
        source=observation.get('source'), source_id=observation.get('source_id'))
    return task


def decide_match(conn, observation, candidates=None):
    """Route one observation to reuse / auto / alias / isolate / review / new / pending."""
    source, source_id = observation.get('source'), observation.get('source_id')

    # Case 1: 已核验映射.
    link = _accepted_link(conn, source, source_id)
    if link:
        return {'case': 1, 'action': 'reuse', 'match_status': 'supported',
                'community_id': link['community_id'], 'review_task_id': None, 'isolation': False,
                'reason': '已有已核验来源映射，直接复用', 'audit_required': False, 'geometry_id': None}

    if candidates is None:
        candidates = match_candidates(conn, observation)
    substantive = [c for c in candidates if c.get('substantive_signals') or c.get('reason')]
    if observation.get('parent_id'):
        # An explicitly declared parent is a relation, not an identity ambiguity.
        substantive = [c for c in substantive if c['community_id'] != observation.get('parent_id')]

    name = observation.get('name')
    has_coords = observation.get('longitude') is not None and observation.get('latitude') is not None
    precision = observation.get('precision_level') or 'unknown'
    reliable_coords = has_coords and precision in ('point', 'district')

    # Case 7: no position / street-only position -> 待定位候选, no strict metro distance.
    if (not has_coords) or precision == 'street':
        # F3 gap: persist city/district on the source payload even though this
        # record is not linked to a community, so the pending queue carries a city.
        _ensure_source_entity(conn, observation)
        conn.execute("UPDATE communities SET location_status='unknown' WHERE community_id IN "
                     "(SELECT community_id FROM source_entities WHERE source=? AND source_id=?)",
                     (source, source_id))
        conn.commit()
        return {'case': 7, 'action': 'pending_location', 'match_status': 'unknown',
                'community_id': None, 'review_task_id': None, 'isolation': False,
                'reason': '无坐标或仅街道级定位，不计算严格地铁距离，转待定位候选',
                'audit_required': False, 'geometry_id': None}

    exact = [c for c in substantive if c['score_components']['完整名称']['matched']]

    # Case 2: unique exact candidate fully consistent -> auto accept + sampling audit.
    if len(exact) == 1:
        comp = exact[0]['score_components']
        if (comp['行政区代码']['matched'] and comp['地址']['matched']
                and comp['期次']['matched'] and comp['坐标及精度']['matched']):
            _link_observation(conn, observation, exact[0]['community_id'])
            return {'case': 2, 'action': 'auto_accept', 'match_status': 'supported',
                    'community_id': exact[0]['community_id'], 'review_task_id': None, 'isolation': False,
                    'reason': '同区同名、地址期次一致且位置吻合，唯一候选', 'audit_required': True,
                    'geometry_id': None}

    # Case 5: exact name but district or position conflict -> isolate, never overwrite map.
    if exact:
        obs_pair = _obs_wgs_pair(observation)
        obs_dist = observation.get('district_code')
        for cand in exact:
            community = get_community(conn, cand['community_id']) or {}
            district_conflict = bool(obs_dist and community.get('district_code')
                                     and obs_dist != community.get('district_code'))
            coords_conflict = False
            if obs_pair:
                comm_pair = _current_wgs_pair(conn, cand['community_id'])
                if comm_pair:
                    dist = _distance_m(obs_pair[0], obs_pair[1], comm_pair[0], comm_pair[1])
                    coords_conflict = dist is not None and dist > CONFLICT_DISTANCE_M
            if district_conflict or coords_conflict:
                return {'case': 5, 'action': 'isolate', 'match_status': 'conflict',
                        'community_id': cand['community_id'], 'review_task_id': None, 'isolation': True,
                        'reason': '名称一致但区县或坐标冲突，隔离冲突不覆盖地图',
                        'audit_required': False, 'geometry_id': None}

    # Case 3: verified alias + address + location agree -> accept as independent alias.
    for cand in substantive:
        comp = cand['score_components']
        if comp['别名']['matched'] and comp['地址']['matched'] and comp['坐标及精度']['matched']:
            _link_observation(conn, observation, cand['community_id'])
            return {'case': 3, 'action': 'accept_alias', 'match_status': 'supported',
                    'community_id': cand['community_id'], 'review_task_id': None, 'isolation': False,
                    'reason': '已核验别名且地址位置吻合', 'audit_required': False, 'geometry_id': None}

    # Case 4: similar name with unresolved address / phase / multiple candidates -> review.
    similar = [c for c in substantive if _name_similar(name, c['name'])]
    if similar:
        best = similar[0]
        address_match = best['score_components']['地址']['matched']
        if (not address_match) or (not observation.get('phase')) or len(candidates) > 1:
            task = _review(conn, observation, best, '名称相似需人工核对身份')
            return {'case': 4, 'action': 'review', 'match_status': 'unknown',
                    'community_id': None, 'review_task_id': task['id'], 'isolation': False,
                    'reason': '相似候选存在地址截断、缺期次或多候选，转身份待核',
                    'audit_required': False, 'geometry_id': None}

    # Case 6: no candidate, source identity clear, reliable coords -> new record without OSM.
    if not substantive and not observation.get('has_osm') and name and reliable_coords:
        community_id, geometry_id = _create_community(conn, observation)
        return {'case': 6, 'action': 'new_record', 'match_status': 'supported',
                'community_id': community_id, 'review_task_id': None, 'isolation': False,
                'reason': '无OSM对应但来源身份清楚且坐标可靠，新建平台小区记录',
                'audit_required': False, 'geometry_id': geometry_id}

    if not substantive:
        # F3: a record with no substantive identity signal must never become an
        # identity review; queue it as pending location when new_record is gated.
        _ensure_source_entity(conn, observation)
        return {'case': 7, 'action': 'pending_location', 'match_status': 'unknown',
                'community_id': None, 'review_task_id': None, 'isolation': False,
                'reason': '无实质身份信号且不满足新建门槛，转待定位候选，不进入身份复核',
                'audit_required': False, 'geometry_id': None}

    # Fallback: a substantive candidate exists but cannot be auto-decided (case 4 semantics).
    fallback = similar[0] if similar else None
    task = _review(conn, observation, fallback, '候选歧义需人工核对身份')
    return {'case': 4, 'action': 'review', 'match_status': 'unknown', 'community_id': None,
            'review_task_id': task['id'], 'isolation': False,
            'reason': '存在候选但无法自动判定，转身份待核', 'audit_required': False, 'geometry_id': None}


# --------------------------------------------------------------------------- #
# query snapshot + audit
# --------------------------------------------------------------------------- #

def _year_condition(community, year_start, year_end):
    verdict = judgement.year_condition(community, year_start, year_end)
    # snapshot_query audit only accepts supported/excluded/unknown/conflict.
    result = 'unknown' if verdict['status'] == 'pending' else verdict['status']
    return {'condition': '年份', 'result': result, 'reason': verdict['reason']}


def _form_condition(community, forms):
    verdict = judgement.form_condition(community, forms)
    return {'condition': '形态', 'result': verdict['status'], 'reason': verdict['reason']}


def _nearest_station(pair, stations):
    """Nearest station as ``(station, distance)``; ``(None, None)`` when undecidable."""
    if pair is None or not stations:
        return None, None
    best_station, best_dist = None, None
    for station in stations:
        dist = _distance_m(pair[0], pair[1], station['lat'], station['lng'])
        if dist is None:
            continue
        if best_dist is None or dist < best_dist:
            best_station, best_dist = station, dist
    if best_dist is None:
        return None, None
    return best_station, best_dist


_STATION_CELL_DEG = 0.02


def _station_cell(lat, lng):
    return (math.floor(lat / _STATION_CELL_DEG), math.floor(lng / _STATION_CELL_DEG))


def _build_station_index(stations):
    index = {}
    for idx, station in enumerate(stations):
        lat, lng = station['lat'], station['lng']
        if lat is None or lng is None:
            continue
        index.setdefault(_station_cell(lat, lng), []).append((idx, lat, lng))
    return index


def _nearest_station_indexed(pair, stations, index, min_cell_m, max_ring):
    """Exact nearest station using expanding grid rings.

    Cells are scanned by Chebyshev radius r = 0, 1, 2, ... . Once the best
    distance is below a conservative lower bound on the distance to any cell
    outside the scanned rings (r * min_cell_m), it is the global minimum.
    Ties keep the lowest original station index (same as `_nearest_station`).
    Returns ``(station, distance)``; threshold judgement is applied by the caller."""
    lat1, lng1 = pair
    clat, clon = _station_cell(lat1, lng1)
    best_dist = None
    best_idx = None
    for radius in range(max_ring + 1):
        for dlat in range(-radius, radius + 1):
            for dlon in range(-radius, radius + 1):
                if max(abs(dlat), abs(dlon)) != radius:
                    continue
                for idx, lat2, lng2 in index.get((clat + dlat, clon + dlon), ()):
                    dist = _distance_m(lat1, lng1, lat2, lng2)
                    if dist is None:
                        continue
                    if best_dist is None or dist < best_dist or (dist == best_dist and idx < best_idx):
                        best_dist, best_idx = dist, idx
        if best_dist is not None and best_dist < radius * min_cell_m:
            return stations[best_idx], best_dist
    return _nearest_station(pair, stations)


def _evidence_ids(conn, community_id, geom=None):
    ids = [r['id'] for r in conn.execute(
        'SELECT id FROM observations WHERE community_id=? ORDER BY id', (community_id,)).fetchall()]
    ids += [r['id'] for r in conn.execute(
        'SELECT id FROM identity_links WHERE community_id=? ORDER BY id', (community_id,)).fetchall()]
    if geom is None:
        geom = _current_geometry(conn, community_id)
    if geom:
        ids.append(geom['id'])
    return ids


def snapshot_query(conn, query):
    """Evaluate one saved filter into a traceable snapshot and audit row."""
    query = query or {}
    city = query.get('city_code')
    year_start, year_end = query.get('year_start'), query.get('year_end')
    forms = list(query.get('forms') or [])
    threshold = query.get('max_metro_distance_m')
    threshold = METRO_THRESHOLD_M if threshold is None else float(threshold)
    scope = query.get('station_operational_scope') or METRO_SCOPE
    include_pending = bool(query.get('include_pending'))
    data_version = query.get('data_version')
    normalized = {'city_code': city, 'year_start': year_start, 'year_end': year_end, 'forms': forms,
                  'max_metro_distance_m': threshold, 'station_operational_scope': scope,
                  'include_pending': include_pending, 'data_version': data_version}

    missing = []
    if not city:
        missing.append('查询缺少 city_code，无法限定城市范围')
    stations = [dict(s) for s in conn.execute(
        'SELECT * FROM metro_stations WHERE operational_status=?', (scope,)).fetchall()
        if s['lng'] is not None and s['lat'] is not None]
    if not stations:
        missing.append(f'metro_stations 无运营口径 {scope} 的可用站点，地铁距离一律 unknown')

    station_index = None
    min_cell_m = 0.0
    max_ring = 0
    if stations:
        station_index = _build_station_index(stations)
        max_abs_lat = max(abs(s['lat']) for s in stations)
        cos_lat = math.cos(math.radians(min(max_abs_lat, 89.0)))
        # 110000 m/deg under-estimates 1 deg latitude; cos(lat) under-estimates 1 deg longitude
        min_cell_m = _STATION_CELL_DEG * 110000.0 * max(cos_lat, 1e-6)
        rows = [r for r, _ in station_index]
        cols = [c for _, c in station_index]
        max_ring = (max(rows) - min(rows)) + (max(cols) - min(cols)) + 2

    if city:
        communities = [dict(r) for r in conn.execute(
            'SELECT * FROM communities WHERE city_code=?', (city,)).fetchall()]
    else:
        communities = [dict(r) for r in conn.execute('SELECT * FROM communities').fetchall()]

    items, status_counts = [], {'supported': 0, 'excluded': 0, 'unknown': 0, 'conflict': 0}
    for community in communities:
        conditions = []
        if city:
            ok = community.get('city_code') == city
            conditions.append({'condition': '城市', 'result': 'supported' if ok else 'excluded',
                               'reason': f'city_code={community.get("city_code")!r}'})
        else:
            conditions.append({'condition': '城市', 'result': 'unknown', 'reason': '查询未指定城市'})
        conditions.append(_year_condition(community, year_start, year_end))
        conditions.append(_form_condition(community, forms))

        geom = _current_geometry(conn, community['community_id'])
        pair = None
        if geom:
            lon, lat = geom.get('coord_wgs84_lon'), geom.get('coord_wgs84_lat')
            if lon is not None and lat is not None:
                pair = (lat, lon)
        conditions.append({'condition': '位置', 'result': 'supported' if pair else 'unknown',
                           'reason': '已有可用WGS84坐标' if pair else '缺少可用WGS84坐标'})

        if station_index is not None:
            station, distance = _nearest_station_indexed(
                pair, stations, station_index, min_cell_m, max_ring)
        else:
            station, distance = _nearest_station(pair, stations)
        station_id = station['station_id'] if station else None
        accuracy = geom.get('accuracy_m') if geom else None
        if distance is None:
            lower_m = upper_m = None
            distance_status = 'unknown'
            distance_reason = '无坐标或无运营站点，距离 unknown'
        else:
            lower_m, upper_m = judgement.metro_bounds(distance, accuracy)
            distance_status = judgement.metro_status(lower_m, upper_m, threshold)
            if distance_status == 'pending':
                distance_status = 'unknown'
            distance_reason = (f'最近运营站 {distance:.0f}m（直线，阈值 {threshold:.0f}m，'
                               f'下限 {lower_m:.0f}m..上限 {upper_m:.0f}m）')
        conditions.append({'condition': '地铁距离', 'result': distance_status,
                           'reason': distance_reason})

        results = [c['result'] for c in conditions]
        if 'excluded' in results:
            overall = 'excluded'
        elif 'conflict' in results:
            overall = 'conflict'
        elif 'unknown' in results:
            overall = 'unknown'
        else:
            overall = 'supported'
        if overall == 'unknown' and not include_pending:
            continue
        status_counts[overall] += 1

        missing_fields = []
        if community.get('year_start') is None and community.get('year_end') is None:
            missing_fields.append('year')
        if not _json_load(community.get('residential_forms'), []) and not _json_load(community.get('villa_subtypes'), []):
            missing_fields.append('residential_forms')
        if pair is None:
            missing_fields.append('location')
        if distance is None:
            missing_fields.append('metro_distance')

        items.append({
            'community_id': community['community_id'],
            'phase': {'parent_id': community.get('parent_id'),
                      'relation_type': community.get('relation_type'),
                      'phase': community.get('phase')},
            'match_status': overall,
            'conditions': conditions,
            'evidence_ids': _evidence_ids(conn, community['community_id'], geom),
            'nearest_station_id': station_id,
            'distance_m': distance,
            'distance_lower_m': lower_m,
            'distance_upper_m': upper_m,
            'distance_basis': 'straight_line',
            'distance_status': distance_status,
            'missing_fields': missing_fields,
            'source_fetched_at': community.get('source_fetched_at'),
            'coverage_note': community.get('coverage_note')})

    coverage_note = (f'候选 {len(communities)} 个，运营站点 {len(stations)} 个，'
                     f'距离口径 straight_line（阈值 {threshold:.0f}m）')
    conn.execute(
        '''INSERT INTO query_audits (query_json, data_version, result_counts, sample, missing_eval, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (_json_dump(normalized), data_version, _json_dump(status_counts),
         _json_dump(items[:5]), _json_dump(missing), _utcnow()))
    conn.commit()

    return {'query': normalized, 'items': items, 'status_counts': status_counts,
            'missing': missing, 'coverage_note': coverage_note}
