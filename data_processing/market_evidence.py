"""Validate typed city quotes against captured pages and their chart projections."""
import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from anjuke_province_crawler import CITY_SLUG_70
from market_crawler import parse_market, market_hosts
from release_inputs import safe_path


def validate_projection(data, root):
    path = Path(root) / 'output/market/quotes.json'
    quotes = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    accepted = {}
    parsed = {}
    for quote in quotes:
        city, kind, month = quote.get('city'), quote.get('kind'), quote.get('month')
        key = (city, kind, month)
        price = quote.get('price')
        url = quote.get('source_url', '')
        location = urlparse(url)
        if (city not in CITY_SLUG_70 or kind not in ('new_house', 'second_hand') or key in accepted
                or not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or not 0 < price < 1000000
                or location.scheme != 'https' or location.hostname not in market_hosts(city)):
            raise ValueError('Invalid or duplicate typed city quote: ' + str(key))
        raw = Path(root) / safe_path(quote.get('raw_file', ''))
        if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest() != quote.get('raw_sha256'):
            raise ValueError('Market quote evidence missing or changed: ' + str(key))
        # Reparse with the capture date so a later rebuild cannot change the report's month.
        captured_at = datetime.fromisoformat(quote.get('fetched_at', '').replace('Z', '+00:00'))
        if captured_at.tzinfo is None:
            raise ValueError('Market quote capture time must include a timezone')
        captured = captured_at.astimezone(timezone(timedelta(hours=8))).date()
        cache_key = (raw, city, url, captured)
        if cache_key not in parsed:
            parsed[cache_key] = {(r['city'], r['kind'], r['month']): r
                                 for r in parse_market(raw.read_text(encoding='utf-8'), city, url, captured)}
        source = parsed[cache_key].get(key)
        if not source or source['price'] != price or source['basis'] != quote.get('basis'):
            raise ValueError('Market city/type/month/value differs from evidence: ' + str(key))
        accepted[key] = quote
    projected = set()
    for kind, segment in data.get('housing_segments', {}).items():
        for city in segment.get('cities', []):
            months = set()
            for row in city['monthly']:
                if row['month'] in months:
                    raise ValueError('Duplicate market chart month: ' + city['city'])
                months.add(row['month'])
                if row.get('price') is None:
                    continue
                key = (city['city'], kind, row['month'])
                quote = accepted.get(key)
                if not quote or row['price'] != quote['price'] or row.get('price_source') != quote['source_url']:
                    raise ValueError('Unmatched typed market chart price: ' + str(key))
                if row.get('provisional', False) != quote.get('provisional', False) or row.get('fetched_at') != quote.get('fetched_at'):
                    raise ValueError('Market chart capture metadata mismatch: ' + str(key))
                projected.add(key)
    if projected != set(accepted):
        raise ValueError('Typed city quote omitted from market charts')
    return len(accepted)
