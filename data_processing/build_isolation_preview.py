# -*- coding: utf-8 -*-
"""Offline, isolated verification preview for the community-filter chain.

Builds a self-contained preview (catalog DB + frontend data + HTTP page + offline
single file + verification.json) under ``output/source_trial_20260922/preview/``
WITHOUT touching any production path. Reuses the existing rule set
(community_catalog / year_policy / judgement / metro_proximity / source_pilot /
build_city_index / inliner); no network and no new filter/type/year rules.

Provenance note: ``observations.jsonl`` mostly carries the coordinate platforms
(丽兹行/中原); the primary-year platform 房天下 is exported to
``output/fang/profiles.json``, which is read here as the authoritative fang source
(any 房天下 row inlined in observations.jsonl is deduped against it). One identity
can therefore combine a 房天下 year with a coordinate platform's position, and the
split is recorded honestly in verification.json.

Identity note: records are no longer merged by name alone. Inside each same-name
pool, platform objects merge only when ``source_pilot.associate_pair`` confirms
EVERY cross-platform pair; an address-conflicting pair stays separate and is
recorded in ``output/anjuke_community_trial_20260923/identity_conflicts.jsonl``
(同名候选 ≠ 确认身份).

Coordinate basis is per-source (``coord_basis``): only an evidence-backed,
verified chain may become a WGS84 position. An unverified coordinate system keeps
its raw pair for traceability but contributes no position and no numeric distance.

The retained OSM rows are ingested into the isolated catalog first, so a new
source record is deduped against the OSM row through
``catalog.match_candidates`` / ``catalog.decide_match`` instead of being created
twice.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import build_city_index
import catalog_ingest
import community_catalog as catalog
import coord_basis
import inliner
import metro_proximity
import source_pilot
import year_policy

ROOT = Path(__file__).resolve().parent.parent
TRIAL_DIR = ROOT / 'output' / 'source_trial_20260922'
DEFAULT_OUT = TRIAL_DIR / 'preview'
# This round's identity-conflict evidence lives under the 20260923 output dir.
TRIAL_OUT_DIR = ROOT / 'output' / 'anjuke_community_trial_20260923'
IDENTITY_CONFLICTS_PATH = TRIAL_OUT_DIR / 'identity_conflicts.jsonl'

PROD_RE = ROOT / 'data_processing' / 'frontend' / 'realestate'
PROD_DATA = PROD_RE / 'data'
PROD_FRONTEND = ROOT / 'data_processing' / 'frontend'
PROD_METRO = PROD_DATA / 'metro.json'
PROD_METRO_MIN = PROD_DATA / 'metro_operational.min.json'
PROD_METRO_OP = PROD_DATA / 'metro_operational.json'
PROD_DIRECTORY_COVERAGE = ROOT / 'output' / 'catalog' / 'directory_coverage.json'
PROD_COMMUNITIES = PROD_DATA / 'communities.json'
PROD_PENDING = PROD_DATA / 'pending_location.json'
PROD_CATALOG_DB = ROOT / 'output' / 'catalog' / 'catalog.sqlite3'
PROD_FANG_DB = ROOT / 'output' / 'fang' / 'fang.sqlite3'
PROD_CHINA_GEO = PROD_FRONTEND / 'assets' / 'vendor' / 'china.geo.json'
YEAR_POLICY_PATH = TRIAL_DIR / 'year_policy.json'

PREVIEW_CITIES = ('北京', '上海', '广州')
CITY_CODES = {'北京': '110000', '上海': '310000', '广州': '440100'}
QUERY = {'city': '上海', 'forms': ['别墅'], 'yearFrom': 2000, 'yearTo': 2015,
         'metroMaxM': 2000, 'stationOperationalScope': 'operational'}
CHOSEN_NAME = '万顺水原墅'
CHOSEN_CITY = '上海'
SCHEMA = 'isolation_preview/v1'


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def _read_jsonl(path):
    rows = []
    with Path(path).open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _write_json(path, obj, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    path.write_text(text, encoding='utf-8')


def _sha256_file(path):
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _city_code(name):
    codes = metro_proximity._load_city_codes() or {}
    return codes.get(name) or CITY_CODES.get(name)


def _plausible_coord(lng, lat):
    """China bbox guard. Rejects (0,0) and any pair outside mainland China so a bad
    raw value can never become a geometry (the (0,0) bug made the nearest-station
    grid scan ~4e10 cells)."""
    if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
        return False
    if lng == 0 and lat == 0:
        return False
    return 73.0 <= lng <= 136.0 and 15.0 <= lat <= 54.0


def _row_coords_ok(row):
    """Frontend rows must never carry (0,0) or an out-of-bbox position."""
    lng, lat = row.get('lng'), row.get('lat')
    if lng is None or lat is None:
        return True  # no-coord row is fine; it is shown as 待定位
    return _plausible_coord(lng, lat)


# --------------------------------------------------------------------------- #
# group records into one identity per (city, normalized_name)
# --------------------------------------------------------------------------- #

def _member_from_observation(row):
    year_value = row.get('year_value')
    year_raw = None
    if year_value is not None:
        year_raw = {'start': year_value, 'end': year_value, 'precision': 'year',
                    'scope': '小区', 'meaning': 'completion', 'label': row.get('year_text')}
    return {
        'platform': row.get('source'),
        'source_id': row.get('source_id'),
        'name': row.get('name'),
        'city': row.get('city'),
        'district': row.get('district'),
        'address': row.get('address'),
        'year_raw': year_raw,
        'year_field': year_policy.OBS_YEAR_FIELD.get(row.get('source')),
        'type_tags': list(row.get('type_tags') or []),
        'villa_subtypes': list(row.get('villa_subtypes') or []),
        'has_villa': bool(row.get('has_villa')),
        'coords_raw': row.get('coords_raw'),
        'source_url': row.get('source_url'),
        'fetched_at': row.get('fetched_at'),
        'type_raw': row.get('type_raw'),
        'snapshot_path': row.get('snapshot_path'),
    }


def _member_from_fang(record):
    evidence = record.get('evidence') or {}
    return {
        'platform': record.get('platform'),
        'source_id': record.get('identity'),
        'name': record.get('name'),
        'city': record.get('city'),
        'district': record.get('district'),
        'address': record.get('address'),
        'year_raw': record.get('year_raw'),
        'year_field': record.get('year_field'),
        'type_tags': list(record.get('type_tags') or []),
        'villa_subtypes': [],
        'has_villa': bool(record.get('has_villa')),
        'coords_raw': None,
        'source_url': evidence.get('source_url'),
        'fetched_at': None,
        'type_raw': None,
        'snapshot_path': (evidence or {}).get('snapshot_path'),
    }


def _is_fang_profile(row):
    """房天下 profile rows share the schema with output/fang/profiles.json, not the
    source_pilot observation schema (nested year/housing_types/id/evidence_url)."""
    return row.get('source') == '房天下' or (
        isinstance(row.get('year'), dict) and 'housing_types' in row
        and 'id' in row and 'evidence_url' in row)


def _assoc_key(a, b):
    a, b = a or '', b or ''
    return (a, b) if a <= b else (b, a)


def _conflict_record(city, platform_a, members_a, platform_b, members_b, association):
    """One same-name pair that reality keeps apart (同名候选, never merged)."""
    rep_a, rep_b = members_a[0], members_b[0]
    coords_a, coords_b = rep_a.get('coords_raw'), rep_b.get('coords_raw')
    distance_m = None
    if coords_a and coords_b and all(
            isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool)
            for c in (coords_a, coords_b) for k in ('lng', 'lat')):
        dist = catalog._distance_m(coords_a['lat'], coords_a['lng'],
                                   coords_b['lat'], coords_b['lng'])
        distance_m = None if dist is None else round(dist, 1)
    return {
        'city': city,
        'name': rep_a.get('name'),
        'platform_a': platform_a,
        'platform_b': platform_b,
        'address_a': rep_a.get('address'),
        'address_b': rep_b.get('address'),
        'coords_a': coords_a,
        'coords_b': coords_b,
        'distance_m': distance_m,
        'association': {'level': association['level'],
                        'signals': association['signals'],
                        'reason': association['reason']},
        'decision': 'not_merged',
    }


def _group_members(obs_rows, fang_records, return_conflicts=False):
    """Cluster same-name records by real identity, not by name alone.

    A same-name pool is grouped by platform (same-platform same-name members stay
    one platform object), then platform objects merge only when every cross-platform
    representative pair is ``confirmed`` by ``source_pilot.associate_pair``. An
    address-conflicting pair is never merged; each non-confirmed pair is returned as
    a conflict record when ``return_conflicts`` is true. Groups are keyed
    ``(city, normalized_name, cluster_index)``.
    """
    pools = OrderedDict()
    seen_fang = set()

    def add(member):
        key = (member['city'], catalog._normalize_name(member['name']))
        pools.setdefault(key, []).append(member)

    for row in obs_rows:
        if not row.get('name') or not row.get('city'):
            continue
        if _is_fang_profile(row):
            member = _member_from_fang(year_policy._fang_record(row))
            if member['source_id'] and member['source_id'] in seen_fang:
                continue
            seen_fang.add(member['source_id'])
            add(member)
        else:
            add(_member_from_observation(row))
    # profiles.json is the authoritative 房天下 export; dedup against any inlined fang row.
    for record in fang_records:
        if not record.get('name') or not record.get('city'):
            continue
        member = _member_from_fang(record)
        if member['source_id'] and member['source_id'] in seen_fang:
            continue
        seen_fang.add(member['source_id'])
        add(member)

    groups = OrderedDict()
    conflicts = []
    for (city, norm_name), members in pools.items():
        platform_objects = OrderedDict()
        for member in members:
            platform_objects.setdefault(member['platform'], []).append(member)
        reps = list(platform_objects.items())

        assoc = {}
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                platform_a, members_a = reps[i]
                platform_b, members_b = reps[j]
                pair = source_pilot.associate_pair(members_a[0], members_b[0])
                assoc[_assoc_key(platform_a, platform_b)] = pair
                if pair['level'] != 'confirmed':
                    conflicts.append(_conflict_record(city, platform_a, members_a,
                                                      platform_b, members_b, pair))

        clusters = []
        for platform, platform_members in reps:
            for cluster in clusters:
                if all(assoc[_assoc_key(existing, platform)]['level'] == 'confirmed'
                       for existing, _ in cluster):
                    cluster.append((platform, platform_members))
                    break
            else:
                clusters.append([(platform, platform_members)])

        for index, cluster in enumerate(clusters):
            merged = [member for _, platform_members in cluster for member in platform_members]
            groups[(city, norm_name, index)] = merged

    if return_conflicts:
        return groups, conflicts
    return groups


def _year_policy_for(members, primary_source):
    year_obs = []
    for member in members:
        if member.get('year_raw') is None:
            continue
        year_obs.append({'platform': member['platform'], 'year_field': member.get('year_field'),
                         'year_raw': member['year_raw']})
    selected = None
    for member in members:
        if member['platform'] == primary_source and member.get('source_id'):
            selected = member['source_id']
            break
    return year_policy.select_object_year(year_obs, primary_source, selected_source_id=selected)


def _selected_year_basis(meta, policy):
    """The single member whose year backs a recorded policy: the primary platform's
    selected source_id when set, else its first member."""
    primary = policy.get('primary_source')
    sid = policy.get('selected_source_id')
    members = meta.get('members') or []
    for member in members:
        if member.get('platform') == primary and (sid is None or member.get('source_id') == sid):
            return member
    return next((member for member in members if member.get('platform') == primary), None)


def _union_forms(members):
    tags, subtypes = [], []
    for member in members:
        for tag in member.get('type_tags') or []:
            if tag and not source_pilot._is_unknown_type_tag(tag) and tag not in tags:
                tags.append(tag)
        for subtype in member.get('villa_subtypes') or []:
            if subtype and subtype not in subtypes:
                subtypes.append(subtype)
    return tags, subtypes


def _pick_coord_member(members):
    """Prefer a member whose source coordinate basis is verified; otherwise fall
    back to the first plausible raw member only to record an unverified raw pair
    (no position)."""
    fallback = None
    for member in members:
        raw = member.get('coords_raw') or {}
        if not _plausible_coord(raw.get('lng'), raw.get('lat')):
            continue
        basis = coord_basis.unify(member.get('platform'), raw['lng'], raw['lat'])
        if basis.get('distance_ready'):
            return member
        if fallback is None:
            fallback = member
    return fallback


def _pick_villa_member(members):
    """The member carrying explicit villa evidence; prefer 丽兹行, else the first."""
    villas = [m for m in members if m.get('has_villa')]
    if not villas:
        return None
    return next((m for m in villas if m.get('platform') == '丽兹行'), villas[0])


_ASSOC_SIGNAL_LABELS = OrderedDict([
    ('name_match', '名称一致'),
    ('address_match', '地址一致'),
    ('house_match', '门牌一致'),
    ('phase_match', '期次一致'),
    ('source_mapping', '来源映射'),
])


def _identity_basis(villa_member, coord_member):
    """Positive associate_pair signals between the villa-evidence member and the
    coordinate member; never a hard-coded address string."""
    if villa_member is None or coord_member is None:
        return None
    pair = source_pilot.associate_pair(villa_member, coord_member)
    labels = [_ASSOC_SIGNAL_LABELS[key] for key in pair.get('reason') or []
              if key in _ASSOC_SIGNAL_LABELS]
    return '+'.join(labels) or None


def _pick_address(members, coord_member):
    if coord_member is not None and coord_member.get('address'):
        return coord_member['address']
    for member in members:
        if member.get('address'):
            return member['address']
    return None


def _build_observation(key, members, primary_source):
    city = key[0]
    coord_member = _pick_coord_member(members)
    source_member = coord_member or next((m for m in members if m.get('source_id')), members[0])
    source = source_member['platform']
    name = source_member['name']
    source_id = source_member.get('source_id') or f'{source}:{city}:{name}'
    platform_ids = OrderedDict()
    for member in members:
        if member.get('source_id'):
            platform_ids[member['platform']] = member['source_id']

    tags, subtypes = _union_forms(members)
    policy = _year_policy_for(members, primary_source)
    year_raw = policy.get('year_raw') if policy.get('status') == 'recorded' else None
    year_start = policy.get('year_start') if year_raw else None
    year_end = policy.get('year_end') if year_raw else None
    year_label = (year_raw or {}).get('label')
    if year_raw and not year_label and year_start is not None:
        year_label = f'{year_start}年'

    address = _pick_address(members, coord_member)
    district_text = next((m.get('district') for m in members if m.get('district')), None)
    type_text = '、'.join(tags) if tags else next(
        (m.get('type_raw') for m in members if m.get('type_raw')), None)

    obs = {
        'source': source, 'source_id': source_id, 'name': name, 'aliases': [],
        'phase': None, 'parent_id': None, 'relation_type': None,
        'district_code': None, 'city_code': _city_code(city),
        'address': address,
        'year_start': year_start, 'year_end': year_end, 'year_label': year_label,
        'year_precision': 'exact' if year_start is not None else None,
        'year_meaning': 'completion' if year_start is not None else None,
        'year_scope': 'community' if year_start is not None else None,
        'residential_forms': tags, 'villa_subtypes': subtypes, 'type_text': type_text,
        'has_osm': False, 'source_url': source_member.get('source_url'),
        'fetched_at': source_member.get('fetched_at'),
    }

    # Per-source basis: only an evidence-backed, verified chain becomes a WGS84
    # position. An unverified system records its raw pair but never a position, so
    # no numeric distance can be derived from it.
    coord_meta = None
    obs['precision_level'] = 'unknown'
    if coord_member is not None:
        raw = coord_member['coords_raw']
        basis = coord_basis.unify(coord_member['platform'], raw['lng'], raw['lat'])
        unified = basis.get('unified_wgs84')
        if basis.get('verified') and unified and _plausible_coord(unified['lng'], unified['lat']):
            obs['longitude'] = unified['lng']
            obs['latitude'] = unified['lat']
            obs['coord_system'] = 'wgs84'
            obs['precision_level'] = 'point'
            coord_meta = {
                'raw': {'lng': raw['lng'], 'lat': raw['lat']},
                'raw_field_names': basis.get('raw_field_names'),
                'candidate_systems': basis.get('candidate_systems'),
                'chain': basis.get('chain'),
                'verified': True,
                'unified_wgs84': unified,
                'distance_ready': True,
                'verification_state': 'verified_basis',
                'source': basis.get('source'),
                'evidence_note': (basis.get('evidence') or {}).get('note'),
            }
        else:
            coord_meta = {
                'raw': {'lng': raw['lng'], 'lat': raw['lat']},
                'raw_field_names': basis.get('raw_field_names'),
                'candidate_systems': basis.get('candidate_systems'),
                'chain': None,
                'verified': False,
                'unified_wgs84': None,
                'distance_ready': False,
                'verification_state': 'unverified_system',
                'source': basis.get('source'),
                'evidence_note': (basis.get('evidence') or {}).get('note'),
            }

    missing = []
    if coord_meta is None or not coord_meta.get('distance_ready'):
        missing.append('坐标')
    if year_start is None:
        missing.append('年份')
    if not address:
        missing.append('地址')
    if not district_text:
        missing.append('区县')

    meta = {
        'city': city, 'name': name, 'district': district_text,
        'source': source, 'source_id': source_id, 'platform_ids': platform_ids,
        'year_policy': policy, 'year_raw': year_raw,
        'year_start': year_start, 'year_end': year_end, 'year_label': year_label,
        'forms': tags, 'villa_subtypes': subtypes,
        'mixed_state': source_pilot.classify_mixed_state(tags),
        'has_villa': source_pilot.has_villa_type(tags),
        'coord': coord_meta, 'address': address, 'missing_fields': missing,
        'members': members,
    }
    return obs, meta


# --------------------------------------------------------------------------- #
# isolated catalog ingestion
# --------------------------------------------------------------------------- #

def _mark_source_state(conn, obs, result, meta):
    source = catalog.normalize_source(obs['source'])
    row = conn.execute('SELECT id, payload FROM source_entities WHERE source=? AND source_id=?',
                       (source, obs['source_id'])).fetchone()
    if row is None:
        return
    payload = catalog._json_load(row['payload'], {}) or {}
    coord = meta.get('coord') or {}
    payload.update({'catalog_ingest_state': result.get('action'),
                    'decision_reason': result.get('reason'),
                    'city_code': obs.get('city_code'),
                    'district_code': obs.get('district_code'),
                    'missing_fields': meta.get('missing_fields') or [],
                    'coord_raw': coord.get('raw'),
                    'coord_chain': coord.get('chain'),
                    'coord_verified': coord.get('verified'),
                    'coord_basis_source': coord.get('source')})
    conn.execute('UPDATE source_entities SET payload=? WHERE id=?',
                 (catalog._json_dump(payload), row['id']))
    conn.commit()


def revoke_stale_links(conn, source, source_id, reason):
    """Revoke every accepted link whose community is not the entity's current decision."""
    source = catalog.normalize_source(source)
    entity = conn.execute(
        'SELECT id, community_id FROM source_entities WHERE source=? AND source_id=?',
        (source, source_id)).fetchone()
    if entity is None:
        return []
    revoked = []
    rows = conn.execute(
        "SELECT id, community_id FROM identity_links WHERE source_entity_id=? AND status='accepted'",
        (entity['id'],)).fetchall()
    for row in rows:
        if row['community_id'] != entity['community_id']:
            catalog.revoke_identity(conn, row['id'], reason)
            revoked.append(row['id'])
    return revoked


def _ingest_groups(conn, groups, primary_source, osm_rows):
    """One catalog observation per merged identity, decided against the real catalog.

    The retained OSM rows are ingested first so ``match_candidates`` can recall them
    and a new source record is deduped against the OSM row (never a second copy).
    Only a verified coordinate basis may write a geometry, and an existing (e.g.
    OSM) geometry is never overwritten.
    """
    if osm_rows:
        catalog_ingest._ingest_communities(conn, osm_rows, {}, {})
    keys = sorted(groups)
    total = len(keys)
    located = pending = 0
    for index, key in enumerate(keys, 1):
        obs, meta = _build_observation(key, groups[key], primary_source)
        candidates = catalog.match_candidates(conn, obs)
        result = catalog.decide_match(conn, obs, candidates=candidates)
        meta['decision'] = result
        meta['community_id'] = result.get('community_id')
        coord = meta.get('coord')
        if (meta['community_id'] and coord is not None
                and coord.get('verified') is True
                and coord.get('unified_wgs84') is not None
                and catalog._current_geometry(conn, meta['community_id']) is None):
            unified = coord['unified_wgs84']
            catalog.set_geometry(
                conn, meta['community_id'], unified['lng'], unified['lat'], 'wgs84', 'point',
                coord.get('source'),
                {'via': 'isolation_preview', 'raw': coord.get('raw'),
                 'candidate_systems': coord.get('candidate_systems'),
                 'chain': coord.get('chain'),
                 'basis': f'coord_basis.unify -> {coord.get("chain")}'})
        # A community is located once it carries a current geometry, whether the
        # catalog created it during decide_match or the verified basis set it here.
        if meta['community_id'] and catalog._current_geometry(conn, meta['community_id']) is not None:
            located += 1
        else:
            pending += 1
        _mark_source_state(conn, obs, result, meta)
        revoke_stale_links(conn, obs['source'], obs['source_id'], 'isolation_preview_decision_changed')
        groups[key] = (obs, meta)
        if index % 500 == 0 or index == total:
            print(f'[ingest] {index}/{total} groups (located={located} pending={pending})',
                  flush=True)
    return groups


# --------------------------------------------------------------------------- #
# isolated frontend data
# --------------------------------------------------------------------------- #

def _apply_primary_year_policy(row, primary_source):
    """Apply the unified primary-platform-year rule to one final export row.

    Only the primary platform's own year becomes ``completion_year``; every other
    platform's year moves to ``year_clue`` (a clue, never a filled value). A missing
    primary year stays ``unknown``.
    """
    row = dict(row)
    year = row.get('completion_year') if isinstance(row.get('completion_year'), dict) else {}
    evidence = year.get('evidence') if isinstance(year.get('evidence'), list) else []
    primary = [e for e in evidence
               if isinstance(e, dict) and str(e.get('source')) == primary_source]
    others = [e for e in evidence
              if not (isinstance(e, dict) and str(e.get('source')) == primary_source)]
    if primary:
        first = primary[0]
        start = first.get('start')
        row['completion_year'] = {
            'status': 'recorded', 'start': start, 'end': first.get('end'),
            'label': first.get('label') or (f'{start}年' if start is not None else None),
            'precision': first.get('precision') or 'year',
            'scope': first.get('scope') or '小区', 'evidence': primary,
        }
        if others:
            row['year_clue'] = others
    else:
        row['completion_year'] = {'status': 'unknown', 'label': '暂缺', 'start': None,
                                  'end': None, 'scope': None, 'evidence': []}
        if evidence:
            row['year_clue'] = others or evidence
    return row


def _load_osm_rows(communities_src, osm_cities, primary_source=None):
    """Retained OSM base rows for the preview cities (one load, shared by ingest/index).

    When ``primary_source`` is given, every kept row runs through the unified
    primary-year policy so no base row bypasses it.
    """
    source = _read_json(communities_src) or {}
    rows = source.get('communities', [])
    kept = [row for row in rows
            if row.get('city') in osm_cities and (row.get('source') or 'osm') == 'osm'
            and _row_coords_ok(row)]
    if primary_source:
        kept = [_apply_primary_year_policy(row, primary_source) for row in kept]
    return kept


def _write_isolated_communities(out_root, communities_src, osm_cities, osm_rows=None,
                                primary_source=None):
    source = _read_json(communities_src) or {}
    isolated = dict(source)
    kept = list(osm_rows) if osm_rows is not None \
        else _load_osm_rows(communities_src, osm_cities, primary_source)
    print(f'[osm-base] kept={len(kept)} rows', flush=True)
    isolated['communities'] = kept
    _write_json(out_root / 'data' / 'communities.json', isolated, compact=True)
    return kept


def _write_identity_conflicts(conflicts, path=None):
    target = Path(path) if path else IDENTITY_CONFLICTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in conflicts)
    target.write_text(text, encoding='utf-8')
    return target


def _copy_data_files(out_root, metro_src=None):
    data_dir = out_root / 'data'
    overrides = {'metro_operational.min.json': Path(metro_src) if metro_src else PROD_METRO_MIN}
    for name in ('metro.json', 'metro_operational.min.json', 'highspeed.json',
                 'cities.json', 'water.json', 'city_regions.json'):
        src = overrides.get(name) or (PROD_DATA / name)
        if src.exists():
            shutil.copy2(src, data_dir / name)
    for city in PREVIEW_CITIES:
        for sub in ('water', 'districts'):
            src = PROD_DATA / sub / f'{city}.json'
            if src.exists():
                (data_dir / sub).mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, data_dir / sub / f'{city}.json')


def _write_index_html(out_root):
    html = (PROD_RE / 'map.html').read_text(encoding='utf-8')
    # Page lives at out_root/index.html, so app assets are one level down and shared
    # frontend assets are at out_root/assets; data stays at out_root/data.
    html = html.replace('href="assets/css/map.css', 'href="app/assets/css/map.css')
    html = html.replace('href="assets/css/community.css', 'href="app/assets/css/community.css')
    html = html.replace('src="assets/js/map-app.js', 'src="app/assets/js/map-app.js')
    html = html.replace('href="../assets/', 'href="assets/')
    html = html.replace('src="../assets/', 'src="assets/')
    (out_root / 'index.html').write_text(html, encoding='utf-8')


def _copy_app(out_root):
    shutil.copytree(PROD_RE / 'assets', out_root / 'app' / 'assets', dirs_exist_ok=True)
    shutil.copy2(PROD_RE / 'map.html', out_root / 'app' / 'map.html')
    shutil.copytree(PROD_FRONTEND / 'assets', out_root / 'assets', dirs_exist_ok=True)


def _build_re_data(out_root):
    communities = _read_json(out_root / 'data' / 'communities.json') or {}
    properties = [row for row in communities.get('communities', []) if _row_coords_ok(row)]
    manifest = _read_json(out_root / 'data' / 'communities' / 'index.json') or {}
    existing = {str(p.get('id')) for p in properties}
    evidence_by_id = {}
    seen = 0
    for city, entry in (manifest.get('cities') or {}).items():
        city_file = out_root / 'data' / 'communities' / (entry.get('file') or f'{city}.json')
        doc = _read_json(city_file)
        if not doc:
            continue
        cols = doc.get('cols') or []
        for row in doc.get('rows') or []:
            seen += 1
            obj = {cols[i]: row[i] for i in range(min(len(cols), len(row)))}
            rid = str(obj.get('id'))
            evidence_by_id[rid] = obj.get('evidence')
            if obj.get('catalog_only') and rid not in existing and _row_coords_ok(obj):
                properties.append(obj)
                existing.add(rid)
            if seen % 500 == 0:
                print(f'[index] decoded {seen} rows (properties={len(properties)})', flush=True)
    print(f'[index] decoded {seen} rows total (properties={len(properties)})', flush=True)
    for prop in properties:
        ev = evidence_by_id.get(str(prop.get('id')))
        if ev is not None:
            prop['evidence'] = ev

    cities = _read_json(out_root / 'data' / 'cities.json') or {}
    water = {'cities': {city: _read_json(out_root / 'data' / 'water' / f'{city}.json')
                        for city in cities
                        if (out_root / 'data' / 'water' / f'{city}.json').exists()}}
    districts = {city: _read_json(out_root / 'data' / 'districts' / f'{city}.json')
                 for city in cities
                 if (out_root / 'data' / 'districts' / f'{city}.json').exists()}
    return {
        'properties': properties,
        'coverage': {k: v for k, v in communities.items() if k != 'communities'},
        'coverageDataset': _read_json(out_root / 'data' / 'coverage.json'),
        'pendingQueue': _read_json(out_root / 'data' / 'pending_location.json'),
        'metro': _read_json(out_root / 'data' / 'metro.json'),
        'metroOperational': _read_json(out_root / 'data' / 'metro_operational.min.json'),
        'highspeed': _read_json(out_root / 'data' / 'highspeed.json'),
        'cities': cities,
        'water': water,
        'regions': _read_json(out_root / 'data' / 'city_regions.json'),
        'districts': districts,
    }


def _build_offline(out_root):
    re_data = _build_re_data(out_root)
    geo = _read_json(PROD_CHINA_GEO)
    inliner.build_single_file(str(out_root / 'index.html'), str(out_root),
                              str(out_root / 'index_offline.html'),
                              [('__RE_DATA__', re_data), ('__CHINA_GEOJSON__', geo)])
    return re_data


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #

def _production_hashes():
    return {
        'output/catalog/catalog.sqlite3': _sha256_file(PROD_CATALOG_DB),
        'output/fang/fang.sqlite3': _sha256_file(PROD_FANG_DB),
        'data_processing/frontend/realestate/data/communities.json': _sha256_file(PROD_COMMUNITIES),
        'data_processing/frontend/realestate/data/pending_location.json': _sha256_file(PROD_PENDING),
    }


def _fang_profile_year(profile_id):
    """Raw 房天下 year fact for one profile id (value text + evidence url)."""
    profiles = _read_json(ROOT / 'output' / 'fang' / 'profiles.json', {}) or {}
    for record in profiles.get('records', []):
        if record.get('id') != profile_id:
            continue
        year = record.get('year') if isinstance(record.get('year'), dict) else {}
        return {'value': year.get('value'), 'label': year.get('label'),
                'source_url': year.get('source_url') or record.get('evidence_url'),
                'field': year.get('field')}
    return {}


def _chosen_block(out_root, conn, groups, stations):
    target = catalog._normalize_name(CHOSEN_NAME)
    matches = [value for key, value in groups.items()
               if key[0] == CHOSEN_CITY and key[1] == target]
    if not matches:
        return {'found': False}
    # A same-name pool may hold several clusters; prefer the verified-position one.
    chosen = next((value for value in matches
                   if (value[1].get('coord') or {}).get('verified') is True), matches[0])
    obs, meta = chosen
    cid = meta.get('community_id')
    coord = meta.get('coord') or {}
    policy = meta.get('year_policy') or {}

    nearest = None
    if cid:
        result = metro_proximity.nearest_operational(conn, cid, radius_m=2000, scope='operational')
        station = result.get('station') or {}
        # DB registry stores the adcode in 'city'; resolve the human city/lines from the feed.
        file_station = next((s for s in stations if s['station_id'] == station.get('station_id')), None)
        info = file_station or station
        station_city = info.get('city')
        nearest = {
            'name': info.get('name'),
            'lines': info.get('lines'),
            'metres': None if result.get('meters') is None else round(result['meters'], 1),
            'caliber': result.get('caliber'),
            'station_set_version': metro_proximity.versions().get('station_set'),
            'station_collection_date': _read_json(
                out_root / 'data' / 'metro_operational.min.json', {}).get('generated_at'),
            'operational_status': info.get('operational_status'),
            'cross_boundary_check': {
                'station_city': station_city,
                'community_city': CHOSEN_CITY,
                'cross_city': bool(station_city and station_city != CHOSEN_CITY),
                'within_radius_2000m': result.get('status') == 'known',
            },
        }

    distance_by_candidate = None
    if coord.get('raw'):
        raw = coord['raw']
        station_raw, dist_raw = catalog._nearest_station((raw['lat'], raw['lng']), stations)
        distance_by_candidate = {
            'raw_as_wgs84': {'station': (station_raw or {}).get('name'),
                             'metres': None if dist_raw is None else round(dist_raw, 1)},
            coord.get('chain') or 'unverified_chain': {
                'station': (nearest or {}).get('name'),
                'metres': (nearest or {}).get('metres')},
        }

    fang_member = next((m for m in meta['members'] if m['platform'] == '房天下'), None)
    coord_member = _pick_coord_member(meta['members'])
    villa_member = _pick_villa_member(meta['members'])
    villa_platform = (villa_member or {}).get('platform')

    # R2: value/source_id/raw_text/field/evidence_url/snapshot must all come from the
    # SAME selection basis (the primary platform's member), never mixed with 房天下.
    status = policy.get('status')
    if status == 'recorded':
        basis = _selected_year_basis(meta, policy)
        if basis is None:
            raise ValueError('chosen primary year basis missing: no %s member'
                             % policy.get('primary_source'))
        basis_raw = basis.get('year_raw') or {}
        if (basis.get('platform') != policy.get('primary_source')
                or policy.get('year_start') != basis_raw.get('start')
                or policy.get('year_end') != basis_raw.get('end')):
            raise ValueError('chosen primary year basis inconsistent: policy vs member')
        snap = basis.get('snapshot_path')
        snap_sha = _sha256_file(ROOT / snap) if snap else None
        primary_year = {
            'status': 'recorded', 'start': policy.get('year_start'), 'end': policy.get('year_end'),
            'primary_source': policy.get('primary_source'),
            'selected_source_id': policy.get('selected_source_id'),
            'raw_text': basis_raw.get('label'), 'field': basis.get('year_field'),
            'evidence_url': basis.get('source_url'), 'snapshot_path': snap,
            'snapshot_sha256': snap_sha,
            'note': '小区目录标注的建成年份，非竣工验收证明',
            'selection_reason': policy.get('selection_reason'),
            'cross_source_difference': policy.get('cross_source_difference'),
        }
    else:
        primary_year = {
            'status': status or 'unknown', 'start': None, 'end': None,
            'primary_source': policy.get('primary_source'),
            'selected_source_id': policy.get('selected_source_id'),
            'raw_text': None, 'field': None, 'evidence_url': None,
            'snapshot_path': None, 'snapshot_sha256': None,
            'note': '主平台无年份事实，保持未知；其他平台年份仅作线索',
            'selection_reason': policy.get('selection_reason'),
            'cross_source_difference': policy.get('cross_source_difference'),
            'auxiliary_clues': policy.get('auxiliary_clues'),
        }
    return {
        'found': True,
        'community_id': cid,
        'name': meta['name'],
        'city': meta['city'],
        'district': meta['district'],
        'per_platform_source_ids': dict(meta['platform_ids']),
        'primary_year': primary_year,
        'villa_evidence': {
            'platform': villa_platform,
            'type_raw': (villa_member or {}).get('type_raw'),
            'type_tags': (villa_member or {}).get('type_tags'),
            'villa_subtypes': meta.get('villa_subtypes'),
            'has_villa': meta.get('has_villa'),
            'mixed_state': meta.get('mixed_state'),
            'identity_basis': _identity_basis(villa_member, coord_member),
            'fang_housing_types': (fang_member or {}).get('type_tags'),
            'note': (f'别墅证据来自 {villa_platform} 的显式形态标签' if villa_platform
                     else '无显式形态标签来源'),
        },
        'coordinate': coord,
        'nearest_operational_station': nearest,
        'comparison_only': {
            'source_metro_claim': None,
            'note': '来源记录未提供地铁口径声明，仅作候选坐标系统距离对比',
            'distance_by_candidate': distance_by_candidate,
        },
    }


def _pending_check(conn, marker_ids):
    """Pending source entities must never carry coordinates nor reach the marker set."""
    rows = conn.execute(
        "SELECT source_id, payload FROM source_entities "
        "WHERE json_extract(payload,'$.catalog_ingest_state')='pending_location'").fetchall()
    with_coords = 0
    in_markers = 0
    ids = []
    for row in rows:
        ids.append(row['source_id'])
        payload = catalog._json_load(row['payload'], {}) or {}
        if payload.get('longitude') is not None or payload.get('latitude') is not None:
            with_coords += 1
        if str(row['source_id']) in marker_ids:
            in_markers += 1
    return {
        'pending_rows': len(rows),
        'pending_ids': ids,
        'pending_rows_with_coords': with_coords,
        'pending_ids_in_marker_set': in_markers,
        'assert_no_pending_with_coords': with_coords == 0,
        'assert_no_pending_in_markers': in_markers == 0,
    }


def _marker_ids(out_root):
    """Community ids that carry finite coordinates (the map marker set)."""
    marker_ids = set()
    manifest = _read_json(out_root / 'data' / 'communities' / 'index.json') or {}
    for city, entry in (manifest.get('cities') or {}).items():
        doc = _read_json(out_root / 'data' / 'communities' / (entry.get('file') or f'{city}.json')) or {}
        cols = doc.get('cols') or []
        lat_i = cols.index('lat') if 'lat' in cols else None
        lng_i = cols.index('lng') if 'lng' in cols else None
        if lat_i is None or lng_i is None:
            continue
        for row in doc.get('rows') or []:
            lat = row[lat_i] if lat_i < len(row) else None
            lng = row[lng_i] if lng_i < len(row) else None
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                marker_ids.add(str(row[0]))
    return marker_ids


def _osm_absent(osm_rows):
    exact = [r for r in osm_rows if r.get('name') == CHOSEN_NAME]
    substring = [r for r in osm_rows if CHOSEN_NAME in str(r.get('name') or '')]
    return {
        'osm_rows': len(osm_rows),
        'chosen_name': CHOSEN_NAME,
        'exact_name_matches': len(exact),
        'substring_matches': len(substring),
        'osm_absent': not substring,
    }


def _node_bin():
    for candidate in (os.environ.get('NODE'), 'node', 'D:/nodejs/node.exe', 'D:\\nodejs\\node.exe'):
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        if Path(candidate).exists():
            return candidate
    return None


def _run_node_parity(out_root, base_url=None):
    """Run the parity runner once; returns the parsed JSON (or an error dict)."""
    node = _node_bin()
    script = Path(__file__).with_name('verify_isolation_preview.cjs')
    if node is None:
        return {'ok': False, 'error': 'node executable not found'}
    if not script.exists():
        return {'ok': False, 'error': f'parity runner missing: {script}'}
    cmd = [node, str(script), '--root', str(out_root)]
    if base_url:
        cmd += ['--base-url', base_url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return {'ok': False, 'error': f'node run failed: {exc!r}', 'base_url': base_url}
    payload = None
    stdout = proc.stdout or ''
    try:
        payload = json.loads(stdout)
    except ValueError:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
    if payload is None:
        return {'ok': False, 'returncode': proc.returncode, 'base_url': base_url,
                'stderr': (proc.stderr or '')[-2000:]}
    payload['ok'] = proc.returncode == 0
    payload['returncode'] = proc.returncode
    payload['base_url'] = base_url
    return payload


def _free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _wait_for_http(url, timeout=30.0):
    import time
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _run_parity_modes(out_root):
    """HTTP mode (spawns a localhost http.server) + fs mode; never raises."""
    result = {'http': None, 'fs': None, 'agree': False, 'errors': []}
    server = None
    port = None
    try:
        port = _free_port()
        server = subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(port), '--directory', str(out_root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base_url = f'http://127.0.0.1:{port}/'
        if _wait_for_http(base_url + 'index.html'):
            result['http'] = _run_node_parity(out_root, base_url=base_url)
        else:
            result['errors'].append(f'http.server did not serve on {base_url}')
    except Exception as exc:
        result['errors'].append(f'http server failed: {exc!r}')
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except Exception:
                server.kill()
    result['fs'] = _run_node_parity(out_root, base_url=None)
    http, fs = result['http'], result['fs']
    if http and fs and http.get('ok') and fs.get('ok'):
        keys = ('same_result_set', 'map_list_parity', 'click_jump_check')
        result['agree'] = all(bool(http.get(k)) == bool(fs.get(k)) for k in keys) \
            and bool(http.get('same_result_set'))
    return result


def _build_verification(out_root, groups, osm_rows, hashes_before, hashes_after,
                        re_data, primary_source, year_policy_doc, parity_modes,
                        identity_conflicts=None):
    conn = catalog.connect(out_root / 'catalog.sqlite3')
    try:
        stations = metro_proximity.load_stations()
        chosen = _chosen_block(out_root, conn, groups, stations)
        counts = {
            'communities': conn.execute('SELECT COUNT(*) FROM communities').fetchone()[0],
            'source_entities': conn.execute('SELECT COUNT(*) FROM source_entities').fetchone()[0],
            'geometries': conn.execute('SELECT COUNT(*) FROM geometries').fetchone()[0],
            'pending_location': conn.execute(
                "SELECT COUNT(*) FROM source_entities "
                "WHERE json_extract(payload,'$.catalog_ingest_state')='pending_location'").fetchone()[0],
        }
        marker_ids = _marker_ids(out_root)
        pending = _pending_check(conn, marker_ids)
    finally:
        conn.close()

    # Structural offline/HTTP parity fallback: inlined properties ids vs isolated city rows.
    inlined_ids = {str(p.get('id')) for p in re_data.get('properties', [])}
    city_ids = set()
    manifest = _read_json(out_root / 'data' / 'communities' / 'index.json') or {}
    for city, entry in (manifest.get('cities') or {}).items():
        doc = _read_json(out_root / 'data' / 'communities' / (entry.get('file') or f'{city}.json')) or {}
        for row in doc.get('rows') or []:
            city_ids.add(str(row[0]))
    structural_parity = inlined_ids == city_ids

    http = (parity_modes or {}).get('http')
    fs = (parity_modes or {}).get('fs')
    agree = bool((parity_modes or {}).get('agree'))
    if http and 'same_result_set' in http:
        map_list_parity = bool(http.get('map_list_parity'))
        click_jump_check = bool(http.get('click_jump_check'))
        pending_check = http.get('pending_check') or pending
    else:
        map_list_parity = None
        click_jump_check = None
        pending_check = pending

    return {
        'schema': SCHEMA,
        'isolated_root': str(out_root),
        'primary_source': primary_source,
        'year_policy': {
            'primary_source': year_policy_doc.get('primary_source'),
            'policy_version': year_policy_doc.get('policy_version'),
            'selection_reason': year_policy_doc.get('selection_reason'),
        },
        'query': QUERY,
        'chosen_community': chosen,
        # 同名候选 kept separate from 确认身份: non-merged same-name pairs.
        'identity_conflicts': {
            'count': len(identity_conflicts or []),
            'sample': (identity_conflicts or [])[:5],
        },
        'osm_absent': _osm_absent(osm_rows),
        'counts': counts,
        'map_list_parity': map_list_parity,
        'click_jump_check': click_jump_check,
        # Full parsed HTTP-mode runner result (same_result_set/chosen/pending_check/...).
        'offline_http_parity': http if http is not None else {'ok': False, 'error': 'http parity not run'},
        'parity_modes': {'http': http, 'fs': fs, 'agree': agree,
                         'errors': (parity_modes or {}).get('errors', []),
                         'structural_inlined_equals_city_rows': structural_parity},
        'pending_location_check': pending_check,
        'production_untouched': {
            'before': hashes_before,
            'after': hashes_after,
            'unchanged': hashes_before == hashes_after,
            'scheduled_task_created': False,
        },
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def build_preview(out_root=None, obs_path=None, metro_src=None, communities_src=None,
                  osm_cities=None, year_policy_path=None):
    out_root = Path(out_root) if out_root else DEFAULT_OUT
    obs_path = Path(obs_path) if obs_path else TRIAL_DIR / 'observations.jsonl'
    communities_src = Path(communities_src) if communities_src else PROD_COMMUNITIES
    osm_cities = tuple(osm_cities) if osm_cities else PREVIEW_CITIES

    hashes_before = _production_hashes()
    policy_path = Path(year_policy_path) if year_policy_path else YEAR_POLICY_PATH
    year_policy_doc = _read_json(policy_path, {}) or {}
    primary_source = year_policy_doc.get('primary_source') or '房天下'

    # Fresh isolated catalog + data each run so stale (e.g. the (0,0) geometry) never
    # survives; isolated dir only, production paths are never touched.
    db_file = out_root / 'catalog.sqlite3'
    if db_file.exists():
        db_file.unlink()
    if (out_root / 'data').exists():
        shutil.rmtree(out_root / 'data')
    (out_root / 'data' / 'communities').mkdir(parents=True, exist_ok=True)
    (out_root / 'app').mkdir(parents=True, exist_ok=True)
    print(f'[build] root={out_root} osm_cities={osm_cities}', flush=True)

    obs_rows = _read_jsonl(obs_path)
    fang_records = year_policy.load_candidate_records('fang', ROOT)
    osm_rows = _load_osm_rows(communities_src, osm_cities, primary_source)
    groups, conflicts = _group_members(obs_rows, fang_records, return_conflicts=True)
    print(f'[build] groups={len(groups)} conflicts={len(conflicts)} '
          f'(observations={len(obs_rows)} fang={len(fang_records)} osm={len(osm_rows)})',
          flush=True)

    conn = catalog.connect(out_root / 'catalog.sqlite3')
    try:
        catalog.create_schema(conn)
        _ingest_groups(conn, groups, primary_source, osm_rows)
        # 同名候选 stay as review tasks; 确认身份 is decided by associate_pair/decide_match.
        for conflict in conflicts:
            catalog.add_review_task(conn, community_id=None,
                                    reason='同名候选地址冲突，保持分离待人工确认身份',
                                    priority=2, evidence=conflict,
                                    source=conflict.get('platform_a'))
        metro_proximity.sync_registry(conn, metro_proximity.load_stations())
    finally:
        conn.close()
    # Isolation: write conflicts into the isolated preview, not the read-only module default.
    _write_identity_conflicts(conflicts, out_root / 'identity_conflicts.jsonl')

    print('[index] building isolated city index...', flush=True)
    # R1: isolated export must carry the non-primary year clue column; production rows have none.
    if 'year_clue' not in build_city_index.COLUMNS:
        build_city_index.COLUMNS = list(build_city_index.COLUMNS) + ['year_clue']
    _write_isolated_communities(out_root, communities_src, osm_cities, osm_rows=osm_rows,
                                primary_source=primary_source)
    build_city_index.build_index(source_path=out_root / 'data' / 'communities.json',
                                 out_dir=out_root / 'data' / 'communities',
                                 db_path=out_root / 'catalog.sqlite3')
    build_city_index.build_pending_location(out_path=out_root / 'data' / 'pending_location.json',
                                            db_path=out_root / 'catalog.sqlite3')
    build_city_index.build_coverage(out_path=out_root / 'data' / 'coverage.json',
                                    ledger_path=PROD_DIRECTORY_COVERAGE,
                                    metro_path=PROD_METRO_OP)
    _copy_data_files(out_root, metro_src=metro_src)

    _copy_app(out_root)
    _write_index_html(out_root)
    print('[offline] inlining single-file page...', flush=True)
    re_data = _build_offline(out_root)

    hashes_after = _production_hashes()
    print('[parity] running http + fs parity...', flush=True)
    parity_modes = _run_parity_modes(out_root)
    verification = _build_verification(out_root, groups, osm_rows, hashes_before, hashes_after,
                                       re_data, primary_source, year_policy_doc, parity_modes,
                                       identity_conflicts=conflicts)
    _write_json(out_root / 'verification.json', verification)

    chosen = verification['chosen_community']
    summary = {
        'isolated_root': str(out_root),
        'primary_source': primary_source,
        'counts': verification['counts'],
        'chosen_community_id': chosen.get('community_id'),
        'chosen_name': chosen.get('name'),
        'identity_conflicts': verification['identity_conflicts']['count'],
        'chosen_unified_wgs84': (chosen.get('coordinate') or {}).get('unified_wgs84'),
        'chosen_nearest_station': (chosen.get('nearest_operational_station') or {}).get('name'),
        'chosen_nearest_metres': (chosen.get('nearest_operational_station') or {}).get('metres'),
        'map_list_parity': verification['map_list_parity'],
        'click_jump_check': verification['click_jump_check'],
        'parity_modes_agree': verification['parity_modes']['agree'],
        'offline_http_parity_ok': (verification['offline_http_parity'] or {}).get('same_result_set'),
        'pending_location_check': verification['pending_location_check'],
        'production_untouched': verification['production_untouched']['unchanged'],
    }
    return summary


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    parser = argparse.ArgumentParser(description='Offline isolated verification preview.')
    parser.add_argument('--out', default=None, help='preview root (default output/source_trial_20260922/preview)')
    parser.add_argument('--observations', default=None, help='observations.jsonl path')
    parser.add_argument('--communities', default=None, help='production communities.json path')
    parser.add_argument('--osm-cities', default='北京,上海',
                        help='comma-separated OSM base cities (default 北京,上海)')
    parser.add_argument('--year-policy', default=None,
                        help='year_policy.json path (default trial dir year_policy.json)')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    osm_cities = tuple(c.strip() for c in args.osm_cities.split(',') if c.strip())
    summary = build_preview(out_root=args.out, obs_path=args.observations,
                            communities_src=args.communities, osm_cities=osm_cities,
                            year_policy_path=args.year_policy)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == '__main__':
    main()
