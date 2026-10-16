"""Build per-city columnar community indexes for lazy frontend loading.

Each city gets a compact ``<city>.json`` under ``data/communities/`` plus a
deterministic ``index.json`` manifest. Never mutates communities.json. The main
catalog SQLite is merged in so non-OSM communities reach the city files, and the
dataset-level coverage / pending-location ledgers are published alongside.
"""
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import judgement

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data_processing" / "frontend" / "realestate" / "data"
SOURCE_FILE = "communities.json"
OUT_DIR = DATA_DIR / "communities"
CATALOG_DB = ROOT / "output" / "catalog" / "catalog.sqlite3"

COLUMNS = ["id", "name", "city", "district", "address", "type", "housing_types",
           "residential_forms", "villa_subtypes", "pure_villa", "type_hints",
           "completion_year", "type_evidence", "lat", "lng", "coordinate_system",
           "precision", "source", "source_url", "fetched_at", "location_note",
           "fang_profile", "fang_reference", "catalog_only", "year_status",
           "year_start", "year_end", "year_precision", "year_meaning", "year_scope",
           "evidence"]

CITY_FALLBACK = {"110000": "北京", "310000": "上海", "440100": "广州"}


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def write_file(path, payload):
    data = compact(payload).encode("utf-8")
    path.write_bytes(data)
    return data


def build_city_payload(city, records):
    kept = [c for c in COLUMNS if any(r.get(c) is not None for r in records)]
    ordered = sorted(records, key=lambda r: str(r.get("id", "")))
    rows = [[r.get(c) for c in kept] for r in ordered]
    return {"schema_version": 1, "city": city, "cols": kept, "rows": rows,
            "row_count": len(rows)}


# --------------------------------------------------------------------------- #
# catalog merge
# --------------------------------------------------------------------------- #

def _city_name_by_code():
    mapping = {}
    try:
        from metro_proximity import _load_city_codes
        mapping.update({str(code): name for name, code in (_load_city_codes() or {}).items()})
    except Exception:
        pass
    for code, name in CITY_FALLBACK.items():
        mapping.setdefault(code, name)
    return mapping


def _parse_list(text):
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def load_catalog_rows(db_path=None):
    """Frontend rows for non-OSM catalog communities with a usable position."""
    db = Path(db_path) if db_path is not None else CATALOG_DB
    if not db.exists():
        return []
    names = _city_name_by_code()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT c.*, g.coord_src_lon, g.coord_src_lat, g.coord_wgs84_lon,
                      g.coord_wgs84_lat, g.coord_system, g.precision_level, g.accuracy_m
               FROM communities c
               LEFT JOIN geometries g ON g.community_id=c.community_id AND g.is_current=1
               WHERE c.has_osm=0""").fetchall()
        out = []
        for row in rows:
            lng = row["coord_wgs84_lon"]
            lng = lng if lng is not None else row["coord_src_lon"]
            lat = row["coord_wgs84_lat"]
            lat = lat if lat is not None else row["coord_src_lat"]
            if lng is None or lat is None:
                continue
            forms = _parse_list(row["residential_forms"])
            subtypes = _parse_list(row["villa_subtypes"])
            year_start, year_end = row["year_start"], row["year_end"]
            src = conn.execute("SELECT source FROM source_entities WHERE community_id=? LIMIT 1",
                               (row["community_id"],)).fetchone()
            source = src["source"] if src is not None and src["source"] else "fang"
            precision = row["precision_level"]
            accuracy_m = row["accuracy_m"]
            out.append({
                "id": row["community_id"],
                "name": row["name"],
                "city": names.get(str(row["city_code"])) or row["city_code"],
                "district": row["district_code"] or "",
                "address": row["address_raw"] or "",
                "type": forms[0] if forms else "住宅",
                "housing_types": forms,
                "residential_forms": forms,
                "villa_subtypes": subtypes,
                # No pure_villa column in the catalog; default rather than infer.
                "pure_villa": False,
                "type_hints": [],
                "completion_year": {
                    "status": "recorded" if (year_start or year_end) else "unknown",
                    "label": row["year_raw_text"] or "",
                    "start": year_start, "end": year_end,
                    "precision": row["year_precision"], "scope": row["year_scope"],
                    "evidence": []},
                "type_evidence": [],
                "lat": lat,
                "lng": lng,
                "accuracy_m": accuracy_m,
                "coordinate_system": "WGS84",
                "precision": "exact" if precision == "point" else (precision or "unknown"),
                "source": source,
                "source_url": "",
                "fetched_at": row["source_fetched_at"],
                "location_note": row["coverage_note"],
                "catalog_only": True,
                "year_status": row["year_status"],
                "year_start": year_start,
                "year_end": year_end,
                "year_precision": row["year_precision"],
                "year_meaning": row["year_meaning"],
                "year_scope": row["year_scope"],
            })
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# normalized judgement evidence
# --------------------------------------------------------------------------- #

def _metro_context():
    try:
        import metro_proximity as mp
        import community_catalog as catalog
    except Exception:
        return None
    all_stations = mp.load_stations()
    stations = [s for s in all_stations
                if s.get('operational_status') == 'operational'
                and s.get('coord_precision') not in ('schematic', 'unknown')
                and s.get('lat') is not None and s.get('lng') is not None]
    if not stations:
        return None
    index = catalog._build_station_index(stations)
    max_abs_lat = max(abs(s['lat']) for s in stations)
    cos_lat = math.cos(math.radians(min(max_abs_lat, 89.0)))
    min_cell_m = catalog._STATION_CELL_DEG * 110000.0 * max(cos_lat, 1e-6)
    rows = [r for r, _ in index]
    cols = [c for _, c in index]
    max_ring = (max(rows) - min(rows)) + (max(cols) - min(cols)) + 2
    # Versions must be the full published registry signature (default no-arg).
    return {'stations': stations, 'index': index, 'min_cell_m': min_cell_m,
            'max_ring': max_ring, 'versions': mp.versions(), 'caliber': mp.CALIBER}


def _metro_evidence(row, context):
    """Nearest operational station bounds; None when position is unusable/absent."""
    if context is None:
        return None
    lat, lng = row.get('lat'), row.get('lng')
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    if not judgement.normalize_location(row)['usable']:
        return None
    import community_catalog as catalog
    station, distance = catalog._nearest_station_indexed(
        (lat, lng), context['stations'], context['index'],
        context['min_cell_m'], context['max_ring'])
    if station is None or distance is None:
        return None
    accuracy = judgement.normalize_location(row)['accuracy_m']
    lower, upper = judgement.metro_bounds(distance, accuracy)
    return {'meters': distance, 'lower_m': lower, 'upper_m': upper,
            'station': station.get('name'), 'station_id': station.get('station_id'),
            'caliber': context['caliber'], 'scope': 'operational',
            'versions': context['versions']}


def attach_evidence(rows, context=None):
    """Attach a normalized evidence bundle to every row (in memory only)."""
    context = context or _metro_context()
    for row in rows:
        row['evidence'] = judgement.build_evidence(row, metro=_metro_evidence(row, context))
    return rows


def build_index(source_path=DATA_DIR / SOURCE_FILE, out_dir=OUT_DIR, db_path=None):
    source_bytes = os.path.getsize(source_path)
    source_sha256 = sha256_bytes(source_path.read_bytes())
    source = json.loads(source_path.read_text(encoding="utf-8"))

    groups = {}
    for row in source.get("communities", []):
        groups.setdefault(row.get("city"), []).append(row)

    catalog_added = 0
    for row in load_catalog_rows(db_path):
        city = row.get("city")
        if not city:
            continue
        bucket = groups.setdefault(city, [])
        if str(row.get("id")) in {str(r.get("id")) for r in bucket}:
            continue
        bucket.append(row)
        catalog_added += 1

    ctx = _metro_context()
    for bucket in groups.values():
        attach_evidence(bucket, ctx)

    out_dir.mkdir(parents=True, exist_ok=True)
    cities = {}
    total_rows = 0
    default_city = None
    default_rows = -1
    sizes = {}
    for city in sorted(groups):
        payload = build_city_payload(city, groups[city])
        data = write_file(out_dir / f"{city}.json", payload)
        rows = payload["row_count"]
        cities[city] = {"file": f"{city}.json", "rows": rows,
                        "bytes": len(data), "sha256": sha256_bytes(data)}
        sizes[city] = (len(data), rows)
        total_rows += rows
        if rows > default_rows:
            default_rows = rows
            default_city = city

    manifest = {
        "schema_version": 1,
        "source": SOURCE_FILE,
        "source_bytes": source_bytes,
        "source_sha256": source_sha256,
        "version": source_sha256[:12],
        "default_city": default_city,
        "total_rows": total_rows,
        "cols": COLUMNS,
        "coverage": {k: v for k, v in source.items() if k != "communities"},
        "catalog": {"db": str(db_path or CATALOG_DB), "rows": catalog_added},
        "cities": cities,
    }
    manifest_path = out_dir / "index.json"
    write_file(manifest_path, manifest)
    return manifest_path, sizes, total_rows


# --------------------------------------------------------------------------- #
# dataset-level coverage + pending location
# --------------------------------------------------------------------------- #

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_json_text(text):
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def _fang_status(pagination):
    if not isinstance(pagination, dict):
        return None
    if pagination.get("truncated") is True or pagination.get("capped") is True:
        return "截断"
    if pagination.get("bulk_state") == "partial" or pagination.get("pause_reason"):
        return "未采完"
    if pagination.get("bulk_state") == "complete":
        return "完成"
    return None


def build_coverage(out_path=None, ledger_path=None, metro_path=None):
    ledger = _read_json(ledger_path or (ROOT / "output" / "catalog" / "directory_coverage.json"))
    metro = _read_json(metro_path or (DATA_DIR / "metro_operational.json"))
    pagination = ((ledger or {}).get("sources") or {}).get("fang", {}).get("directory_pagination")
    status = _fang_status(pagination)

    source_entry = {
        "status": status,
        "truncated": pagination.get("truncated") if isinstance(pagination, dict) else None,
        "last_success_time": pagination.get("last_success_time") if isinstance(pagination, dict) else None,
        "pause_reason": pagination.get("pause_reason") if isinstance(pagination, dict) else None,
        "bulk_state": pagination.get("bulk_state") if isinstance(pagination, dict) else None,
    }

    metro_cities = ((metro or {}).get("coverage") or {}).get("cities") or {}
    cities = {}
    for city in (ledger or {}).get("cities", {}):
        metro_entry = metro_cities.get(city)
        cities[city] = {
            "source_status": status,
            "source_status_reason": source_entry["pause_reason"],
            "station_verification": ((metro_entry or {}).get("verification_state") or "unverified")
                                    if metro_entry is not None else None,
            "verification_state": ((metro_entry or {}).get("verification_state") or "unverified")
                                  if metro_entry is not None else None,
            "metro_coverage_incomplete": metro_entry.get("coverage_incomplete") if metro_entry else None,
            "directory_truncated": source_entry["truncated"],
            "last_success_time": source_entry["last_success_time"],
            "note": "数据集级状态；不等于该城小区计数。",
        }

    payload = {
        "schema_version": 1,
        "generated_at": _utcnow(),
        "not_additive": True,
        "note": "数据集级覆盖状态：单小区计数不能代替数据集覆盖；来源未采完/站点未核验/目录截断分别列出。",
        "sources": {"fang": source_entry},
        "cities": cities,
    }
    write_file(Path(out_path) if out_path else DATA_DIR / "coverage.json", payload)
    return payload


PENDING_LOCATION_REASON = "无坐标或仅街道级定位，不计算严格地铁距离，转待定位候选"


def _pending_missing_fields(payload):
    """Fields still missing for an unlocated source record, from its stored payload."""
    missing = []
    if not payload.get("address"):
        missing.append("地址")
    if not payload.get("phase"):
        missing.append("期次")
    if payload.get("longitude") is None or payload.get("latitude") is None:
        missing.append("坐标")
    if not payload.get("district_code"):
        missing.append("区县")
    return missing


def build_pending_location(out_path=None, db_path=None):
    db = Path(db_path) if db_path is not None else CATALOG_DB
    records = []
    if db.exists():
        names = _city_name_by_code()
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT source, source_id, name, source_url, payload FROM source_entities "
                "WHERE json_extract(payload,'$.catalog_ingest_state')='pending_location' "
                "   OR (source='fang' AND community_id IS NULL "
                "       AND json_extract(payload,'$.longitude') IS NULL "
                "       AND json_extract(payload,'$.latitude') IS NULL "
                "       AND COALESCE(match_status,'unknown')='unknown')").fetchall()
            for row in rows:
                payload = _read_json_text(row["payload"]) or {}
                code = payload.get("city_code")
                reason = payload.get("decision_reason") or PENDING_LOCATION_REASON
                missing = payload.get("missing_fields")
                if not isinstance(missing, list) or not missing:
                    missing = _pending_missing_fields(payload)
                records.append({
                    "id": row["source_id"],
                    "name": row["name"],
                    "city": names.get(str(code)) if code else None,
                    "district": payload.get("district_code"),
                    "source": row["source"],
                    "reason": reason,
                    "missing_fields": missing,
                    "source_url": row["source_url"],
                })
        finally:
            conn.close()

    payload = {
        "schema_version": 1,
        "generated_at": _utcnow(),
        "note": "来源记录已入库但未定位，仅进入列表待核；计数不代表物理小区数。",
        "records": records,
    }
    write_file(Path(out_path) if out_path else DATA_DIR / "pending_location.json", payload)
    return payload


def main():
    manifest_path, sizes, total_rows = build_index()
    for city in sorted(sizes):
        size, rows = sizes[city]
        print(f"{city} {size} bytes ({rows} rows)")
    print(f"total {total_rows} rows")
    print(f"manifest {manifest_path}")

    coverage = build_coverage()
    print(f"coverage {DATA_DIR / 'coverage.json'} ({len(coverage['cities'])} cities)")
    pending = build_pending_location()
    print(f"pending {DATA_DIR / 'pending_location.json'} ({len(pending['records'])} records)")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main())
