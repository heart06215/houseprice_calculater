"""Idempotent offline ingestion of map communities + Fang profiles into the catalog.

Fills ``communities`` from the OSM map feed and routes Fang profiles through the
existing match decision, so the coverage metric no longer reads a zero catalog.
No network; re-running on the same DB leaves counts unchanged. Standard library
plus sibling repo modules only.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import community_catalog as catalog
import fang_publish

ROOT = Path(__file__).resolve().parent.parent
COMMUNITIES_PATH = ROOT / 'data_processing' / 'frontend' / 'realestate' / 'data' / 'communities.json'
PROFILES_PATH = ROOT / 'output' / 'fang' / 'profiles.json'
REPORT_PATH = ROOT / 'output' / 'catalog' / 'ingest_report.json'

# 字段契约: residential_forms 仅允许以下四个取值。
RESIDENTIAL_FORMS = ('楼房', '别墅', '公寓', '自建房')
MATCH_STATUSES = catalog.MATCH_STATUSES
CATALOG_RELATION = fang_publish.CATALOG_RELATION

_FALLBACK_CITY_CODES = {'北京': '110000', '上海': '310000', '广州': '440100'}

_COORD_SYSTEM_MAP = {'wgs84': 'wgs84', 'gcj02': 'gcj02', 'gcj-02': 'gcj02',
                     'bd09': 'bd09', 'bd-09': 'bd09'}

# 字段契约: 代码内精度 'year' 对外映射为 'exact'。
_YEAR_PRECISION_MAP = {'year': 'exact', 'exact': 'exact', 'approximate': 'approximate',
                       'range': 'range', 'decade': 'decade'}

# community_attributes scope values -> contract year_scope literals.
_YEAR_SCOPE_MAP = {'小区': 'community', '分期': 'phase', '组团': 'group', '单栋': 'building',
                   'building': 'building', 'community': 'community', 'phase': 'phase',
                   'group': 'group'}


class _BatchWriteConnection:
    """Proxy forwarding to a sqlite3 connection but deferring commit() so a
    bulk ingest runs as one transaction. Call flush() to commit for real."""
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass

    def flush(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _json_load(text, default=None):
    return catalog._json_load(text, default)


_EVIDENCE_KEYS = ('address', 'address_truncated', 'longitude', 'latitude', 'lng', 'lat',
                  'precision_level', 'precision', 'coord_system', 'coordinate_system',
                  'city_code', 'district_code', 'district', 'phase', 'name', 'aliases',
                  'community', 'identity_changed', 'field_evidence', 'detail_fields',
                  'raw_position', 'year', 'completion_year', 'housing_types',
                  'residential_forms', 'villa_subtypes', 'pure_villa', 'type', 'type_text')


def _evidence_hash(profile):
    """Stable fingerprint of a source record's observations + address/coord evidence."""
    canonical = {key: profile.get(key) for key in _EVIDENCE_KEYS}
    canonical['observation_count'] = len(profile.get('field_evidence') or {}) + \
        len(profile.get('detail_fields') or {})
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _supersede_prior_decision(conn, entity_id, source_id):
    """Retire the previous decision's links/review task before a re-decide."""
    now = _utcnow()
    conn.execute(
        "UPDATE identity_links SET status='revoked', revoked_at=?, revoke_reason='evidence_changed' "
        "WHERE source_entity_id=? AND revoked_at IS NULL AND status!='revoked'",
        (now, entity_id))
    conn.execute(
        "UPDATE review_tasks SET status='superseded', resolved_at=? "
        "WHERE status='open' AND json_extract(evidence,'$.source_id')=?",
        (now, source_id))
    conn.execute("UPDATE source_entities SET community_id=NULL, match_status='unknown' WHERE id=?",
                 (entity_id,))


def _norm_coord_system(raw):
    key = str(raw or '').strip().lower().replace(' ', '')
    return _COORD_SYSTEM_MAP.get(key, 'other')


def _norm_precision(raw):
    key = str(raw or '').strip().lower()
    if key in catalog.PRECISION_LEVELS:
        return key
    if key == 'exact':
        return 'point'
    if key == 'street':
        return 'street'
    if key in ('district', 'approximate'):
        return 'district'
    return 'unknown'


def _year_parts(completion_year):
    """Map community_attributes completion_year -> (start, end, precision, meaning, scope)."""
    cy = completion_year if isinstance(completion_year, dict) else {}
    start, end = cy.get('start'), cy.get('end')
    precision = _YEAR_PRECISION_MAP.get(str(cy.get('precision') or '').lower())
    meaning = 'completion' if (start is not None or end is not None) else None
    scope = _YEAR_SCOPE_MAP.get(cy.get('scope')) if cy.get('scope') else None
    return start, end, precision, meaning, scope


def _city_code_for(name):
    if not name:
        return None
    try:
        code = fang_publish._city_code(name)
    except Exception:
        code = None
    if code:
        return code
    try:
        from metro_proximity import _load_city_codes
        code = (_load_city_codes() or {}).get(name)
    except Exception:
        code = None
    return code or _FALLBACK_CITY_CODES.get(name)


def _city_name_by_code():
    mapping = {}
    try:
        from metro_proximity import _load_city_codes
        codes = _load_city_codes() or {}
        mapping.update({str(code): name for name, code in codes.items()})
    except Exception:
        pass
    for name, code in _FALLBACK_CITY_CODES.items():
        mapping.setdefault(code, name)
    return mapping


def _db_path(conn):
    try:
        for row in conn.execute('PRAGMA database_list').fetchall():
            if row[1] == 'main' and row[2]:
                return row[2]
    except Exception:
        pass
    return str(catalog.DEFAULT_DB)


# --------------------------------------------------------------------------- #
# map community ingestion
# --------------------------------------------------------------------------- #

def _ingest_communities(conn, rows, skipped, stats):
    for row in rows:
        source = row.get('source') or 'osm'
        source_id = row['id']
        existing = conn.execute(
            'SELECT * FROM source_entities WHERE source=? AND source_id=?',
            (source, source_id)).fetchone()
        evidence = _evidence_hash(row)
        if existing is not None and existing['community_id']:
            previous = _json_load(existing['payload'], {}) or {}
            if 'catalog_ingest_state' in previous:
                if previous.get('evidence_hash') == evidence:
                    skipped['already_ingested'] = skipped.get('already_ingested', 0) + 1
                    continue
                # Source evidence changed: revise the live row and append history.
                stats['map_redecided'] = stats.get('map_redecided', 0) + 1
                _revise_community(conn, existing, source, source_id, row, evidence, stats)
                continue

        entity = catalog.upsert_source_entity(conn, source, source_id, row.get('name'), {
            'source_url': row.get('source_url'),
            'catalog_ingest_state': 'map',
            'evidence_hash': evidence,
            'fetched_at': row.get('fetched_at')})

        city_code = _city_code_for(row.get('city'))
        community_id = catalog.allocate_community_id(conn, city_code or 'unknown')

        name = row.get('name')
        alias = row.get('community')
        aliases = [alias] if alias and alias != name else []
        address = row.get('address') or None
        y_start, y_end, y_prec, y_meaning, y_scope = _year_parts(row.get('completion_year'))
        forms = [f for f in (row.get('housing_types') or []) if f in RESIDENTIAL_FORMS]
        lng, lat = row.get('lng'), row.get('lat')
        has_coords = lat is not None and lng is not None

        conn.execute(
            '''INSERT INTO communities
               (community_id, name, aliases, phase, district_code, city_code,
                address_raw, address_normalized, year_start, year_end, year_precision,
                year_meaning, year_scope, residential_forms, villa_subtypes, location_status,
                source_fetched_at, has_osm, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (community_id, name, json.dumps(aliases, ensure_ascii=False),
             row.get('phase'), None, city_code,
             address, catalog.normalize_address(address), y_start, y_end, y_prec,
             y_meaning, y_scope, json.dumps(forms, ensure_ascii=False), '[]',
             'point' if has_coords else 'unknown',
             row.get('fetched_at'), 1 if source == 'osm' else 0, _utcnow(), _utcnow()))
        conn.commit()

        fetched = row.get('fetched_at')
        evidence = {'source': source, 'source_url': row.get('source_url'), 'fetched_at': fetched}
        catalog.add_observation(conn, community_id, 'name', name, None, evidence, fetched, fetched)
        if aliases:
            catalog.add_observation(conn, community_id, 'alias', aliases[0], None, evidence, fetched, fetched)
        if address:
            catalog.add_observation(conn, community_id, 'address', address, None, evidence, fetched, fetched)
        if y_start is not None or y_end is not None:
            completion = row.get('completion_year') or {}
            catalog.add_observation(conn, community_id, 'year', completion.get('label'),
                                    completion, evidence, fetched, fetched)
        if forms or row.get('type'):
            catalog.add_observation(conn, community_id, 'type', row.get('type'),
                                    row.get('housing_types') or row.get('type'),
                                    evidence, fetched, fetched)

        if has_coords:
            catalog.set_geometry(
                conn, community_id, lng, lat,
                _norm_coord_system(row.get('coordinate_system')),
                _norm_precision(row.get('precision')), source,
                {'via': 'catalog_ingest', 'source_id': source_id,
                 'source_url': row.get('source_url')})
        else:
            stats['osm_pending_location'] = stats.get('osm_pending_location', 0) + 1
            conn.execute("UPDATE communities SET location_status='unknown' WHERE community_id=?",
                         (community_id,))
            conn.commit()

        conn.execute('UPDATE source_entities SET community_id=? WHERE id=?',
                     (community_id, entity['id']))
        conn.commit()
        exists = conn.execute(
            "SELECT 1 FROM identity_links WHERE community_id=? AND source_entity_id=? "
            "AND relation=? AND revoked_at IS NULL",
            (community_id, entity['id'], CATALOG_RELATION)).fetchone()
        if not exists:
            catalog.link_identity(conn, community_id, entity['id'], CATALOG_RELATION,
                                  {'via': 'catalog_ingest', 'source_id': source_id}, 'catalog_ingest')


def _revise_community(conn, existing, source, source_id, row, evidence, stats):
    """Update the live community row for a changed map source and append history.

    The prior identity link is kept; only the community columns, observations and
    (when present) geometry version move forward. A moved point bumps the geometry
    version, so the cached proximity row is deleted to force a recompute.
    """
    community_id = existing['community_id']
    previous = _json_load(existing['payload'], {}) or {}
    name = row.get('name')
    alias = row.get('community')
    aliases = [alias] if alias and alias != name else []
    address = row.get('address') or None
    y_start, y_end, y_prec, y_meaning, y_scope = _year_parts(row.get('completion_year'))
    forms = [f for f in (row.get('housing_types') or []) if f in RESIDENTIAL_FORMS]
    city_code = _city_code_for(row.get('city'))
    lng, lat = row.get('lng'), row.get('lat')
    has_coords = lat is not None and lng is not None

    conn.execute(
        '''UPDATE communities SET name=?, aliases=?, city_code=?, district_code=?,
           address_raw=?, address_normalized=?, year_start=?, year_end=?, year_precision=?,
           year_meaning=?, year_scope=?, residential_forms=?, source_fetched_at=?, updated_at=?
           WHERE community_id=?''',
        (name, json.dumps(aliases, ensure_ascii=False), city_code, None,
         address, catalog.normalize_address(address), y_start, y_end, y_prec,
         y_meaning, y_scope, json.dumps(forms, ensure_ascii=False),
         row.get('fetched_at'), _utcnow(), community_id))
    conn.commit()

    fetched = row.get('fetched_at')
    obs_evidence = {'source': source, 'source_url': row.get('source_url'), 'fetched_at': fetched}
    catalog.add_observation(conn, community_id, 'name', name, None, obs_evidence, fetched, fetched)
    if aliases:
        catalog.add_observation(conn, community_id, 'alias', aliases[0], None, obs_evidence, fetched, fetched)
    if address:
        catalog.add_observation(conn, community_id, 'address', address, None, obs_evidence, fetched, fetched)
    if y_start is not None or y_end is not None:
        completion = row.get('completion_year') or {}
        catalog.add_observation(conn, community_id, 'year', completion.get('label'), completion,
                                obs_evidence, fetched, fetched)
    if forms or row.get('type'):
        catalog.add_observation(conn, community_id, 'type', row.get('type'),
                                row.get('housing_types') or row.get('type'),
                                obs_evidence, fetched, fetched)

    if has_coords:
        catalog.set_geometry(
            conn, community_id, lng, lat,
            _norm_coord_system(row.get('coordinate_system')),
            _norm_precision(row.get('precision')), source,
            {'via': 'catalog_ingest', 'source_id': source_id,
             'source_url': row.get('source_url')})
    else:
        conn.execute("UPDATE communities SET location_status='unknown' WHERE community_id=?",
                     (community_id,))
        conn.commit()

    # Geometry version moved (or location reset): drop the stale distance cache.
    conn.execute('DELETE FROM proximity WHERE community_id=?', (community_id,))
    conn.commit()

    payload = dict(previous)
    payload.update({
        'source_url': row.get('source_url'),
        'fetched_at': row.get('fetched_at'),
        'catalog_ingest_state': 'map',
        'evidence_hash': evidence})
    catalog.upsert_source_entity(conn, source, source_id, name, payload)


# --------------------------------------------------------------------------- #
# fang profile ingestion
# --------------------------------------------------------------------------- #

def _ingest_profiles(conn, profiles, skipped, fang_states, stats):
    for profile in profiles:
        source_id = fang_publish._source_id(profile)
        existing = conn.execute(
            'SELECT * FROM source_entities WHERE source=? AND source_id=?',
            ('fang', source_id)).fetchone()
        previous = (_json_load(existing['payload'], {}) or {}) if existing is not None else {}
        previous_state = previous.get('catalog_ingest_state')
        evidence = _evidence_hash(profile)
        obs = fang_publish._observation(profile)
        # Re-decide only when the source's observation set changed. Unchanged evidence keeps
        # the stored decision; re-running it would append duplicate links/review tasks.
        redeciding = bool(previous_state) and previous.get('evidence_hash') != evidence
        if previous_state and not redeciding:
            # Backfill city/district onto payloads written before F3 so the
            # pending queue can label a city without re-deciding identity.
            backfill = {}
            if obs.get('city_code') and not previous.get('city_code'):
                backfill['city_code'] = obs.get('city_code')
            if obs.get('district_code') and not previous.get('district_code'):
                backfill['district_code'] = obs.get('district_code')
            if backfill:
                merged = dict(previous)
                merged.update(backfill)
                conn.execute('UPDATE source_entities SET payload=? WHERE id=?',
                             (json.dumps(merged, ensure_ascii=False), existing['id']))
                conn.commit()
                stats['city_backfilled'] = stats.get('city_backfilled', 0) + 1
            skipped['already_ingested'] = skipped.get('already_ingested', 0) + 1
            continue
        if redeciding:
            stats['redecided'] = stats.get('redecided', 0) + 1
            _supersede_prior_decision(conn, existing['id'], source_id)
        prior_decision = previous_state if previous_state and previous_state != 'fang' else None

        entity = catalog.upsert_source_entity(conn, 'fang', source_id, profile.get('name'), {
            'source_url': profile.get('source_url'),
            'catalog_ingest_state': 'fang',
            'evidence_hash': evidence,
            'address_source': obs.get('address_source'),
            'city_code': obs.get('city_code'),
            'district_code': obs.get('district_code'),
            'missing_fields': fang_publish._missing_fields(profile)})

        # decide_match recalls candidates before its no-position branch, but a profile with
        # no coords (or street precision) always lands on case 7, so skip that O(N*M) scan.
        has_coords = obs.get('longitude') is not None and obs.get('latitude') is not None
        precision = obs.get('precision_level') or 'unknown'
        candidates = [] if (not has_coords or precision == 'street') else None
        result = catalog.decide_match(conn, obs, candidates=candidates)
        action = result['action']
        fang_states[action] = fang_states.get(action, 0) + 1
        if action == 'pending_location':
            stats['fang_pending_location'] = stats.get('fang_pending_location', 0) + 1
        if redeciding and prior_decision and prior_decision != action:
            stats['changed_outcome'] = stats.get('changed_outcome', 0) + 1

        community_id = result.get('community_id')
        if community_id and action in ('reuse', 'auto_accept', 'accept_alias', 'new_record'):
            exists = conn.execute(
                "SELECT 1 FROM identity_links WHERE community_id=? AND source_entity_id=? "
                "AND relation=? AND revoked_at IS NULL",
                (community_id, entity['id'], CATALOG_RELATION)).fetchone()
            if not exists:
                catalog.link_identity(conn, community_id, entity['id'], CATALOG_RELATION,
                                      {'via': 'catalog_ingest', 'source_id': source_id}, 'catalog_ingest')

        if result.get('match_status') in MATCH_STATUSES:
            conn.execute('UPDATE source_entities SET match_status=? WHERE id=?',
                         (result['match_status'], entity['id']))

        # Overwrite the 'fang' marker with the decision so pending_location is DB-queryable.
        merged = dict(_json_load(entity['payload'], {}) or {})
        merged['catalog_ingest_state'] = action
        merged['decision_reason'] = result.get('reason')
        merged['evidence_hash'] = evidence
        merged['address_source'] = obs.get('address_source')
        conn.execute('UPDATE source_entities SET payload=? WHERE id=?',
                     (json.dumps(merged, ensure_ascii=False), entity['id']))
        conn.commit()


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def _build_report(conn, skipped, fang_states, stats=None):
    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row is not None else 0

    names = _city_name_by_code()
    per_city = {}
    for row in conn.execute('SELECT city_code, COUNT(*) c FROM communities GROUP BY city_code'):
        label = names.get(row['city_code']) or row['city_code'] or 'unknown'
        per_city[label] = row['c']
    source_entities = {}
    for row in conn.execute("SELECT source, COUNT(*) c FROM source_entities WHERE COALESCE(match_status,'') != 'merged' GROUP BY source"):
        source_entities[row['source']] = row['c']
    coord_systems = {}
    for row in conn.execute('SELECT coord_system, COUNT(*) c FROM geometries GROUP BY coord_system'):
        coord_systems[row['coord_system']] = row['c']
    stats = stats or {}
    reasons = {}
    for row in conn.execute(
            "SELECT COALESCE(json_extract(payload,'$.decision_reason'),'') r, COUNT(*) c "
            "FROM source_entities WHERE source='fang' "
            "AND json_extract(payload,'$.catalog_ingest_state')='pending_location' GROUP BY r"):
        reasons[row['r']] = row['c']
    address_sources = {}
    for row in conn.execute(
            "SELECT COALESCE(json_extract(payload,'$.address_source'),'none') s, COUNT(*) c "
            "FROM source_entities WHERE source='fang' GROUP BY s"):
        address_sources[row['s']] = row['c']

    return {
        'db_path': _db_path(conn),
        'total_communities': scalar('SELECT COUNT(*) FROM communities'),
        'per_city': dict(sorted(per_city.items())),
        'source_entities': dict(sorted(source_entities.items())),
        'links_created': scalar('SELECT COUNT(*) FROM identity_links'),
        'review_tasks': scalar('SELECT COUNT(*) FROM review_tasks'),
        'pending_location': scalar(
            "SELECT COUNT(*) FROM source_entities WHERE source='fang' "
            "AND json_extract(payload,'$.catalog_ingest_state')='pending_location'"),
        'geometry_written': scalar('SELECT COUNT(*) FROM geometries'),
        'coord_systems_seen': dict(sorted(coord_systems.items())),
        'skipped': dict(sorted(skipped.items())),
        'fang_states': dict(sorted(fang_states.items())),
        'redecided': int(stats.get('redecided', 0)),
        'map_redecided': int(stats.get('map_redecided', 0)),
        'changed_outcome': int(stats.get('changed_outcome', 0)),
        'city_backfilled': int(stats.get('city_backfilled', 0)),
        'merged_sources': int(stats.get('merged_sources', 0)),
        'pending_location_reasons': dict(sorted(reasons.items())),
        'address_sources': dict(sorted(address_sources.items())),
    }


def ingest(conn, *, communities_path=None, profiles_path=None):
    """Idempotently load map communities + Fang profiles; returns a DB-state report."""
    communities = _load_json(communities_path or COMMUNITIES_PATH).get('communities', [])
    profiles = _load_json(profiles_path or PROFILES_PATH).get('records', [])
    skipped = {}
    fang_states = {}
    stats = {}
    work = _BatchWriteConnection(conn)
    try:
        _ingest_communities(work, communities, skipped, stats)
        _ingest_profiles(work, profiles, skipped, fang_states, stats)
        stats['merged_sources'] = catalog.merge_duplicate_sources(work)
        work.flush()
    except BaseException:
        work.rollback()
        raise
    return _build_report(work, skipped, fang_states, stats)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def verify(conn):
    """Duplicate counts that must all be 0 for a clean catalog."""
    def scalar(sql):
        row = conn.execute(sql).fetchone()
        return row[0] if row is not None else 0

    # Alias-aware: 房天下 and fang are the same source object, so group on the
    # normalized source rather than the raw stored value.
    groups = {}
    for row in conn.execute("SELECT source, source_id FROM source_entities WHERE COALESCE(match_status,'') != 'merged'"):
        key = (catalog.normalize_source(row['source']), row['source_id'])
        groups[key] = groups.get(key, 0) + 1

    return {
        'duplicate_source_entities': sum(1 for count in groups.values() if count > 1),
        'duplicate_current_geometries': scalar(
            'SELECT COUNT(*) FROM (SELECT community_id FROM geometries WHERE is_current=1 '
            'GROUP BY community_id HAVING COUNT(*) > 1)'),
        'duplicate_identity_links': scalar(
            'SELECT COUNT(*) FROM (SELECT community_id, source_entity_id, relation FROM identity_links '
            "WHERE status != 'revoked' GROUP BY community_id, source_entity_id, relation HAVING COUNT(*) > 1)"),
        # Same-name communities are distinct OSM objects; real duplication = one source
        # entity linked to multiple communities via live identity_links.
        'duplicate_communities': scalar(
            'SELECT COUNT(*) FROM (SELECT source_entity_id FROM identity_links '
            'WHERE revoked_at IS NULL GROUP BY source_entity_id '
            'HAVING COUNT(DISTINCT community_id) > 1)'),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _write_report(report, path=None):
    target = Path(path) if path else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from data_store import atomic_json
    except Exception:
        atomic_json = None
    if atomic_json is not None:
        atomic_json(str(target), report)
    else:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                          encoding='utf-8')
    return str(target)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    parser = argparse.ArgumentParser(description='Idempotent catalog ingestion (offline).')
    parser.add_argument('--verify', action='store_true', help='report duplicate counts and exit')
    parser.add_argument('--db', help='catalog DB path (default output/catalog/catalog.sqlite3)')
    parser.add_argument('--report', help='report JSON path')
    args = parser.parse_args(argv)

    conn = catalog.connect(args.db)
    try:
        if args.verify:
            result = verify(conn)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if all(value == 0 for value in result.values()) else 1
        catalog.create_schema(conn)
        report = ingest(conn)
        path = _write_report(report, args.report)
        print(f"total_communities {report['total_communities']} | per_city {report['per_city']}")
        print(f"source_entities {report['source_entities']} | links_created {report['links_created']}")
        print(f"review_tasks {report['review_tasks']} | pending_location {report['pending_location']} "
              f"| geometry_written {report['geometry_written']}")
        print(f"redecided {report['redecided']} | changed_outcome {report['changed_outcome']} | skipped {report['skipped']}")
        print(f"report -> {path}")
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
