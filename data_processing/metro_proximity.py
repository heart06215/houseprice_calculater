"""Operational metro station registry and straight-line proximity.

Flattens the GCJ-02 station feed into a versioned registry, converts each station
to WGS84 exactly once (cached), and computes the nearest operational station by
great-circle distance. Distance basis is fixed to ``straight_line``; schematic and
unknown-precision station rows are excluded from strict distances.

Field contract: only city metro/LRT operational
stations count, threshold is inclusive (``distance_m <= 2000``), and the cache key
is community geometry version + station set version + coord transform version +
distance caliber + radius + query scope. Standard library plus sibling repo
modules only; no network.
"""
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import community_catalog as catalog
import data_store
import judgement

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data_processing/frontend/realestate/data'
DEFAULT_METRO_FILE = DATA / 'metro.json'
DEFAULT_CITY_FILE = DATA / 'cities.json'
DEFAULT_OUTPUT_PATH = DATA / 'metro_operational.json'
DEFAULT_MIN_OUTPUT_PATH = DATA / 'metro_operational.min.json'
MIN_NOTE = '未逐站按运营方核对'
MIN_COLS = ('id', 'name', 'lng', 'lat', 'lines', 'op', 'prec')

# Bump when the GCJ-02 -> WGS84 rule changes; part of every proximity cache key.
COORD_TRANSFORM_VERSION = 'gcj02->wgs84:v1'
STATION_REGISTRY_VERSION_PREFIX = 'metro:v1:'
METRO_SCOPE = 'metro_lrt_operational'
# Legacy feed: no operator verification, and every row is a currently-operational station.
OPERATIONAL_STATUS = 'operational'
VERIFICATION_STATE_LEGACY = 'unverified_legacy'
OPERATIONAL_STATUS_UNKNOWN='unknown'
VERIFICATION_STATE_VERIFIED='verified'
RAW_COORD_SYSTEM = 'gcj02'
DISTANCE_BASIS = 'straight_line'
CALIBER = 'straight_line_wgs84'
COVERAGE_NOTES = ('未逐站按运营方核对', '缺城/缺线显式标记', '数量0≠没有地铁')

_CONVERSION_CACHE = {}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 -> WGS84, memoised per rounded coordinate.

    Reverse of ``geocode_communities.wgs84_to_gcj02`` (offset reflected about the
    original point). Outside China the pair is returned unchanged. Always convert
    once; downstream stores only the WGS84 result.
    """
    key = (round(float(lng), 7), round(float(lat), 7))
    cached = _CONVERSION_CACHE.get(key)
    if cached is None:
        cached = _gcj02_to_wgs84_raw(*key)
        _CONVERSION_CACHE[key] = cached
    return cached


def _gcj02_to_wgs84_raw(lng, lat):
    if _out_of_china(lng, lat):
        return (lng, lat)
    from geocode_communities import wgs84_to_gcj02
    mg_lat, mg_lng = wgs84_to_gcj02(lat, lng)
    return (lng * 2 - mg_lng, lat * 2 - mg_lat)


def _load_city_codes(path=None):
    """Map platform city name -> GB/T 2260 adcode string from cities.json."""
    source = Path(path) if path is not None else DEFAULT_CITY_FILE
    if not source.exists():
        return {}
    payload = json.loads(source.read_text(encoding='utf-8'))
    return {name: str(cfg.get('adcode'))
            for name, cfg in payload.items() if cfg.get('adcode') is not None}


def _station_id(city, name, lng, lat):
    digest = hashlib.sha1(
        f'{city}|{name}|{lng:.6f}|{lat:.6f}'.encode('utf-8')).hexdigest()[:12]
    return f'metro:{city}:{digest}'


# --------------------------------------------------------------------------- #
# station registry
# --------------------------------------------------------------------------- #

def _load_stations_uncached(path=None, *, city_codes=None):
    """Flatten ``metro.json`` cities/lines into de-duplicated station records.

    De-dup key is (city, name, rounded source coord) so a same-name station at a
    different location stays its own record; transfer lines of one physical
    station share a record and keep every line name. Each record carries the raw
    GCJ-02 pair plus the once-converted WGS84 pair used for distance. Legacy rows
    default to ``operational_status='operational'`` and ``unverified_legacy``.
    """
    source = Path(path) if path is not None else DEFAULT_METRO_FILE
    payload = json.loads(source.read_text(encoding='utf-8'))
    codes = city_codes if city_codes is not None else _load_city_codes()
    fetched_at = payload.get('updated_at')
    merged = {}
    for city_entry in payload.get('cities', []):
        city = city_entry.get('name')
        if not city:
            continue
        for line in city_entry.get('lines', []):
            line_name = line.get('name')
            rows = line.get('stations') or []
            ids = line.get('station_ids') or []
            for index, row in enumerate(rows):
                try:
                    name, lng, lat = row[0], float(row[1]), float(row[2])
                except (TypeError, ValueError, IndexError):
                    continue
                key = (city, name, round(lng, 6), round(lat, 6))
                station = merged.get(key)
                if station is None:
                    wgs_lng, wgs_lat = gcj02_to_wgs84(lng, lat)
                    station = {
                        'station_id': _station_id(city, name, lng, lat),
                        'name': name,
                        'city': city,
                        'city_code': codes.get(city),
                        'lng': round(wgs_lng, 7),
                        'lat': round(wgs_lat, 7),
                        'raw_coord_system': RAW_COORD_SYSTEM,
                        'coord_system': RAW_COORD_SYSTEM,
                        'source_lng': lng,
                        'source_lat': lat,
                        'coord_wgs84_lon': round(wgs_lng, 7),
                        'coord_wgs84_lat': round(wgs_lat, 7),
                        'coord_transform_version': COORD_TRANSFORM_VERSION,
                        'operational_status': OPERATIONAL_STATUS,
                        'status_evidence_date': city_entry.get('fetched_at') or fetched_at,
                        'verification_state': VERIFICATION_STATE_LEGACY,
                        'coord_precision': (row[3] if len(row) > 3 and isinstance(row[3], str)
                                            and row[3] else 'point'),
                        'lines': [],
                        'source_ids': [],
                        'station_registry_version': None,
                        'source': city_entry.get('source'),
                        'source_url': city_entry.get('url'),
                        'fetched_at': fetched_at,
                    }
                    merged[key] = station
                if line_name and line_name not in station['lines']:
                    station['lines'].append(line_name)
                sid = ids[index] if index < len(ids) else None
                if sid and sid not in station['source_ids']:
                    station['source_ids'].append(sid)
    stations = list(merged.values())
    version = _registry_version(stations)
    for station in stations:
        station['station_registry_version'] = version
    stations.sort(key=lambda s: (s['city'], s['name'], s['station_id']))
    return stations


_LOAD_STATIONS_CACHE = {}


def load_stations(path=None, *, city_codes=None):
    """Cached wrapper around ``_load_stations_uncached`` keyed by file mtime/size."""
    source = Path(path) if path is not None else DEFAULT_METRO_FILE
    try:
        stat = source.stat()
        key = (str(source.resolve()), stat.st_mtime_ns, stat.st_size,
               tuple(city_codes) if city_codes is not None else None)
    except OSError:
        return _load_stations_uncached(path, city_codes=city_codes)
    cached = _LOAD_STATIONS_CACHE.get(key)
    if cached is None:
        cached = _load_stations_uncached(path, city_codes=city_codes)
        _LOAD_STATIONS_CACHE[key] = cached
    return [dict(record) for record in cached]


def _registry_version(stations):
    digest = hashlib.sha256()
    for station in sorted(stations, key=lambda s: s['station_id']):
        digest.update('|'.join([
            station['station_id'], station['name'], station['city'],
            f"{station['lng']:.7f}", f"{station['lat']:.7f}",
            ','.join(sorted(station.get('lines') or [])),
        ]).encode('utf-8'))
        digest.update(b'\n')
    return STATION_REGISTRY_VERSION_PREFIX + digest.hexdigest()[:12]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def duplicates_report(stations=None):
    """Report same-name/different-location and same-location/different-name sites.

    Purely diagnostic: the contract forbids merging same-name stations with
    different coordinates, so this surfaces them instead of collapsing them.
    """
    stations = stations if stations is not None else load_stations()
    name_locations = defaultdict(set)
    location_names = defaultdict(set)
    for station in stations:
        name_locations[(station['city'], station['name'])].add((station['lng'], station['lat']))
        location_names[(station['city'], station['lng'], station['lat'])].add(station['name'])

    same_name = [
        {'city': city, 'name': name, 'location_count': len(locations),
         'locations': sorted([{'lng': lng, 'lat': lat} for lng, lat in locations],
                             key=lambda item: (item['lng'], item['lat']))}
        for (city, name), locations in name_locations.items() if len(locations) > 1]

    same_location = [
        {'city': city, 'lng': lng, 'lat': lat, 'names': sorted(names)}
        for (city, lng, lat), names in location_names.items() if len(names) > 1]

    same_name.sort(key=lambda item: (item['city'], item['name']))
    same_location.sort(key=lambda item: (item['city'], item['lng'], item['lat']))
    return {
        'total_stations': len(stations),
        'same_name_different_location': same_name,
        'same_location_different_name': same_location,
    }


def city_coverage(stations=None):
    """Per-city coverage ledger with explicit gaps and skip counts."""
    stations = stations if stations is not None else load_stations()
    cities = {}
    for station in stations:
        entry = cities.setdefault(station['city'], {
            'city_code': station.get('city_code'), 'station_count': 0,
            'operational_count': 0, 'schematic_excluded': 0, 'lines': set()})
        entry['station_count'] += 1
        if station.get('operational_status') == OPERATIONAL_STATUS:
            entry['operational_count'] += 1
        if station.get('coord_precision') == 'schematic':
            entry['schematic_excluded'] += 1
        entry['lines'].update(station.get('lines') or [])
    for entry in cities.values():
        entry['lines'] = sorted(entry['lines'])
        entry['line_count'] = len(entry['lines'])
        # Legacy registry is never operator-verified, so every city is an explicit gap.
        entry['coverage_incomplete'] = True
        entry['verification_state'] = VERIFICATION_STATE_LEGACY
    return {
        'cities': cities,
        'coverage_note': list(COVERAGE_NOTES),
        'total': {
            'cities': len(cities),
            'stations': len(stations),
            'lines': len({(city, line) for city, entry in cities.items() for line in entry['lines']}),
        },
        'coordinate_system': 'GCJ-02->WGS84',
        'coord_transform_version': COORD_TRANSFORM_VERSION,
    }


def _version_triple(stations=None):
    """Version triple recorded with every computed distance."""
    stations = stations if stations is not None else load_stations()
    return {
        'station_set': _registry_version(stations),
        'coord_conversion': COORD_TRANSFORM_VERSION,
        'distance_caliber': CALIBER,
    }


def versions(stations=None):
    return _version_triple(stations)


# --------------------------------------------------------------------------- #
# registry sync into the catalog DB
# --------------------------------------------------------------------------- #

def sync_registry(conn, stations=None, *, registry_version=None):
    """Upsert the WGS84 station registry into ``catalog.metro_stations``.

    ``lng``/``lat`` hold the WGS84 pair actually used for distance; the GCJ-02
    source pair and transform version are kept inside ``evidence``. Rows of the
    same operational scope that vanish from the source are removed so the registry
    stays consistent with the current feed.
    """
    catalog.create_schema(conn)
    stations = stations if stations is not None else load_stations()
    if registry_version is None:
        registry_version = _registry_version(stations)
    now = _utcnow()
    keep = set()
    inserted = updated = 0
    for station in stations:
        exists = conn.execute('SELECT 1 FROM metro_stations WHERE station_id=?',
                              (station['station_id'],)).fetchone()
        evidence = json.dumps({
            'source': station.get('source'),
            'source_url': station.get('source_url'),
            'coord_system': station['coord_system'],
            'source_lng': station['source_lng'],
            'source_lat': station['source_lat'],
            'coord_transform_version': COORD_TRANSFORM_VERSION,
            'source_ids': station.get('source_ids') or [],
        }, ensure_ascii=False)
        conn.execute(
            '''INSERT INTO metro_stations
               (station_id, name, city_code, operational_status, line_info, lng, lat,
                coord_system, station_registry_version, evidence, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'wgs84', ?, ?, ?)
               ON CONFLICT(station_id) DO UPDATE SET
                   name=excluded.name, city_code=excluded.city_code,
                   operational_status=excluded.operational_status, line_info=excluded.line_info,
                   lng=excluded.lng, lat=excluded.lat, coord_system=excluded.coord_system,
                   station_registry_version=excluded.station_registry_version,
                   evidence=excluded.evidence, verified_at=excluded.verified_at''',
            (station['station_id'], station['name'], station.get('city_code'),
             OPERATIONAL_STATUS,
             json.dumps(station.get('lines') or [], ensure_ascii=False),
             station['lng'], station['lat'], registry_version, evidence, now))
        inserted += 0 if exists else 1
        updated += 1 if exists else 0
        keep.add(station['station_id'])
    removed = 0
    if keep:
        # Purge vanished rows, including any still stamped with the legacy scope value.
        placeholders = ','.join('?' * len(keep))
        cur = conn.execute(
            f'DELETE FROM metro_stations WHERE operational_status IN (?, ?) '
            f'AND station_id NOT IN ({placeholders})',
            (OPERATIONAL_STATUS, METRO_SCOPE, *sorted(keep)))
        removed = cur.rowcount
    conn.commit()
    return {
        'stations': len(stations), 'inserted': inserted, 'updated': updated,
        'removed': removed, 'registry_version': registry_version,
        'scope': METRO_SCOPE, 'synced_at': now,
    }


# --------------------------------------------------------------------------- #
# proximity queries
# --------------------------------------------------------------------------- #

def _community_id(community):
    """Accept a community_id str or a record dict carrying it."""
    if isinstance(community, dict):
        return community.get('community_id') or community.get('id')
    return community


def _community_geometry(conn, community_id):
    """Current WGS84 point + accuracy for a catalog community, else None.

    ``coord_wgs84_*`` is authoritative; a GCJ-02 source pair is converted once.
    Any other unset system has no reliable WGS84 value.
    """
    geom = catalog._current_geometry(conn, community_id)
    if not geom:
        return None
    lng, lat = geom.get('coord_wgs84_lon'), geom.get('coord_wgs84_lat')
    if lng is None or lat is None:
        if geom.get('coord_system') != 'gcj02':
            return None
        src_lng, src_lat = geom.get('coord_src_lon'), geom.get('coord_src_lat')
        if src_lng is None or src_lat is None:
            return None
        lng, lat = gcj02_to_wgs84(src_lng, src_lat)
    return {
        'lat': lat,
        'lng': lng,
        'accuracy_m': geom.get('accuracy_m'),
        'geometry_version': geom.get('version'),
        'coord_system': geom.get('coord_system'),
        'precision_level': geom.get('precision_level'),
    }


def _station_lines(line_info):
    lines = catalog._json_load(line_info, [])
    if isinstance(lines, list):
        return [str(line) for line in lines]
    return [str(lines)] if lines else []


def _station_precision(evidence):
    """coord_precision from evidence JSON; missing/unparsable defaults to point."""
    data = catalog._json_load(evidence, None)
    if isinstance(data, dict):
        precision = data.get('coord_precision')
        return precision if precision else 'point'
    return 'point'


def _station_row(row):
    return {
        'station_id': row['station_id'],
        'name': row['name'],
        'city': row['city_code'],
        'lines': _station_lines(row['line_info']),
        'lng': row['lng'],
        'lat': row['lat'],
        'operational_status': row['operational_status'],
        'coord_precision': _station_precision(row['evidence']),
    }


def neighbor_stations(conn, community, radius_m=None):
    """Stations within ``radius_m`` of a community, from any city, nearest first.

    Reads the DB station registry; schematic-precision rows are skipped because
    they cannot support a distance. Returns ``[]`` when community geometry missing.
    """
    if radius_m is None:
        radius_m = catalog.METRO_THRESHOLD_M
    community_id = _community_id(community)
    geometry = _community_geometry(conn, community_id) if community_id else None
    if geometry is None:
        return []
    found = []
    for row in conn.execute('SELECT * FROM metro_stations').fetchall():
        station = _station_row(row)
        if station['coord_precision'] == 'schematic':
            continue
        if station['lat'] is None or station['lng'] is None:
            continue
        distance = catalog._distance_m(
            geometry['lat'], geometry['lng'], station['lat'], station['lng'])
        if distance is None or distance > radius_m:
            continue
        found.append(dict(station, distance_m=distance))
    found.sort(key=lambda item: item['distance_m'])
    return found


def _station_set_version(conn):
    """Registry signature from ``metro_stations`` rows read off ``conn``.

    Hashes (station_id, rounded lng/lat, operational_status, line_info) of every
    registry row, so INSERT/UPDATE (add/move/coord-fix) yields a new signature and
    the proximity cache misses. Distinct from ``versions``/``_registry_version``,
    which stay file-based for publish metadata.
    """
    digest = hashlib.sha256()
    rows = conn.execute(
        'SELECT station_id, lng, lat, operational_status, line_info FROM metro_stations'
    ).fetchall()
    for row in sorted(rows, key=lambda r: r['station_id'] or ''):
        digest.update('|'.join([
            row['station_id'] or '',
            '' if row['lng'] is None else f"{row['lng']:.7f}",
            '' if row['lat'] is None else f"{row['lat']:.7f}",
            row['operational_status'] or '',
            row['line_info'] or '',
        ]).encode('utf-8'))
        digest.update(b'\n')
    return STATION_REGISTRY_VERSION_PREFIX + digest.hexdigest()[:12]


def _db_version_triple(conn):
    """Version triple for a proximity cache key, sourced from the conn registry."""
    return {
        'station_set': _station_set_version(conn),
        'coord_conversion': COORD_TRANSFORM_VERSION,
        'distance_caliber': CALIBER,
    }


def _proximity_key(version_info, geometry, radius_m, scope):
    material = '|'.join([
        str(version_info['station_set']), str(version_info['coord_conversion']),
        str(version_info['distance_caliber']), str(geometry['geometry_version']),
        str(radius_m), str(scope)])
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _nearest_unknown():
    return {'meters': None, 'station': None, 'caliber': CALIBER, 'status': 'unknown',
            'lower_m': None, 'upper_m': None, 'boundary_judgement': 'unknown'}


def _nearest_bounds(distance, accuracy_m):
    return judgement.metro_bounds(distance, accuracy_m)


def _nearest_judgement(lower_m, upper_m, radius_m):
    return judgement.metro_status(lower_m, upper_m, radius_m)


def _nearest_candidates(conn, scope):
    candidates = []
    for row in conn.execute('SELECT * FROM metro_stations').fetchall():
        station = _station_row(row)
        # METRO_SCOPE is the scope constant, accepted as an alias for 'operational'.
        if (scope in ('operational', METRO_SCOPE)
                and station['operational_status'] != OPERATIONAL_STATUS):
            continue
        if station['coord_precision'] in ('schematic', 'unknown'):
            continue
        if station['lat'] is None or station['lng'] is None:
            continue
        candidates.append(station)
    return candidates


def nearest_operational(conn, community, radius_m=2000, scope='operational', versions=None):
    """Nearest station distance for a community, with inclusive threshold bounds.

    Unknown position or no eligible station yields nulls and ``status='unknown'``,
    never zero or infinity. Results are cached in the ``proximity`` table keyed by
    the composite version string; a changed station set, radius or scope invalidates
    the row.
    """
    community_id = _community_id(community)
    geometry = _community_geometry(conn, community_id) if community_id else None
    if geometry is None:
        return _nearest_unknown()
    candidates = _nearest_candidates(conn, scope)
    if not candidates:
        return _nearest_unknown()

    version_info = versions if versions is not None else _db_version_triple(conn)
    composite = _proximity_key(version_info, geometry, radius_m, scope)
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proximity'").fetchone()
    if has_table is None:
        catalog.create_schema(conn)
    cached = conn.execute(
        'SELECT * FROM proximity WHERE community_id=? ORDER BY id DESC LIMIT 1',
        (community_id,)).fetchone()
    if cached is not None and cached['station_registry_version'] == composite:
        station = next((s for s in candidates
                        if s['station_id'] == cached['nearest_station_id']), None)
        return {'meters': cached['distance_m'], 'station': station, 'caliber': CALIBER,
                'status': 'known', 'lower_m': cached['distance_lower_m'],
                'upper_m': cached['distance_upper_m'],
                'boundary_judgement': cached['boundary_judgement'], 'cache_hit': True}

    nearest = None
    for station in candidates:
        distance = catalog._distance_m(
            geometry['lat'], geometry['lng'], station['lat'], station['lng'])
        if distance is None:
            continue
        if nearest is None or distance < nearest[1]:
            nearest = (station, distance)
    if nearest is None:
        return _nearest_unknown()

    station, distance = nearest
    lower_m, upper_m = _nearest_bounds(distance, geometry['accuracy_m'])
    judgement = _nearest_judgement(lower_m, upper_m, radius_m)
    conn.execute('DELETE FROM proximity WHERE community_id=?', (community_id,))
    conn.execute(
        '''INSERT INTO proximity
           (community_id, geometry_version, station_registry_version, nearest_station_id,
            distance_m, distance_lower_m, distance_upper_m, distance_basis, threshold_m,
            boundary_judgement, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (community_id, geometry['geometry_version'], composite, station['station_id'],
         distance, lower_m, upper_m, DISTANCE_BASIS, radius_m, judgement, _utcnow()))
    conn.commit()
    return {'meters': distance, 'station': station, 'caliber': CALIBER, 'status': 'known',
            'lower_m': lower_m, 'upper_m': upper_m, 'boundary_judgement': judgement,
            'cache_hit': False}


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #

def publish(path=None):
    """Write the deterministic metro operational feed; returns the output path.

    Pure file publication: no DB writes. ``coverage_note`` strings make missing
    cities/lines and unverified stations explicit rather than silent zeros.
    """
    target = Path(path) if path is not None else DEFAULT_OUTPUT_PATH
    payload = {
        'schema_version': catalog.SCHEMA_VERSION,
        'generated_at': _utcnow(),
        'versions': versions(),
        'coverage': city_coverage(),
        'duplicates_report': duplicates_report(),
        'stations': load_stations(),
    }
    data_store.atomic_json(target, payload)
    return str(target)


def publish_min(path=None):
    """Compact columnar browser feed; returns the output path.

    Schema-agnostic rows trading keys for bytes: schematic stations are excluded
    from the distance-usable set and counted in ``excluded_schematic`` instead.
    """
    target = Path(path) if path is not None else DEFAULT_MIN_OUTPUT_PATH
    stations = load_stations()
    cities = {}
    excluded_schematic = 0
    for station in stations:
        if station.get('coord_precision') == 'schematic':
            excluded_schematic += 1
            continue
        city = station['city']
        cities.setdefault(city, []).append([
            station['station_id'], station['name'], station['lng'], station['lat'],
            list(station.get('lines') or []), station.get('operational_status'),
            station.get('coord_precision')])
    coverage = city_coverage(stations)
    payload = {
        'schema_version': catalog.SCHEMA_VERSION,
        'generated_at': _utcnow(),
        'versions': versions(stations),
        'coverage': coverage['total'],
        'note': MIN_NOTE,
        'cities': cities,
        'cols': list(MIN_COLS),
        'excluded_schematic': excluded_schematic,
    }
    data_store.atomic_json(target, payload)
    return str(target)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _configure_stdout():
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace', line_buffering=True)


def main(argv=None):
    """Bare run publishes the metro feed and prints station coverage counts."""
    _configure_stdout()
    args = list(sys.argv[1:]) if argv is None else list(argv)
    path = publish(args[0] if args else None)
    min_path = publish_min()
    stations = load_stations()
    coverage = city_coverage(stations)
    counts = {city: coverage['cities'].get(city, {}).get('station_count', 0)
              for city in ('北京', '上海', '广州')}
    schematic = sum(entry['schematic_excluded'] for entry in coverage['cities'].values())
    print(f'published {len(stations)} stations -> {path} ({Path(path).stat().st_size} bytes)')
    print(f'published compact -> {min_path} ({Path(min_path).stat().st_size} bytes)')
    print(f'北京 {counts["北京"]} | 上海 {counts["上海"]} | 广州 {counts["广州"]}')
    print(f'schematic_excluded {schematic}')
    sys.exit(0)


if __name__ == '__main__':
    main()
