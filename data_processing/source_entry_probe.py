"""Stage runner: bounded, budget-locked entry/structure probe of 丽兹行/中原/房天下.

Implements the 入口与结构探测 stage of docs §4.2/§3.1/§7.1: one ordered PLAN over
北京/上海 entries, every network attempt appended to an append-only requests.jsonl,
raw bodies saved with sha256, blocks classified, fixed parsers reused, per-cell
coverage plus a resumable checkpoint. Reuses source_pilot's budget, io and parsers;
never re-implements them.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import source_pilot as sp
from source_pilot import (
    BLOCK_CLASSES,
    TRIAL_TOTAL_CAP,
    UA,
    BudgetLockError,
    BudgetStore,
    _now_epoch,
    _sleep,
    append_jsonl,
    combo_of,
    copy_to_failed,
    fetch_status,
    is_challenge,
    load_platform_policy_overrides,
    merge_observations,
    merge_signals,
    now_iso,
    read_jsonl,
    save_raw,
    write_jsonl,
)

STAGE = '入口与结构探测'
DEFAULT_OUT = 'output/source_trial_20260922'
CHECKPOINT_SCHEMA = 'source_entry_probe_checkpoint/v1'
REPORT_SCHEMA = 'source_entry_probe_report/v1'
COVERAGE_SCHEMA = 'source_entry_probe_coverage/v1'
# source_pilot.single_request doubles this into a (connect, read) pair.
TIMEOUT = 20


def _t(index, source, city, district, catalog_kind, url, parser, expected_name=None):
    target = {'index': index, 'source': source, 'city': city, 'district': district,
              'catalog_kind': catalog_kind, 'url': url, 'parser': parser}
    if expected_name is not None:
        target['expected_name'] = expected_name
    return target


# Ordered PLAN: 丽兹行 bj/sh lists+partitions, 中原 sh catalogs+detail seed, 中原 bj
# entry, 房天下 entries. Exactly 27 targets.
PLAN = [
    _t(0, '丽兹行', '北京', '全城', '普通', 'https://bj.lizihang.com/loupan', 'lizihang'),
    _t(1, '丽兹行', '北京', '全城', '普通', 'https://bj.lizihang.com/loupan/p2', 'lizihang'),
    _t(2, '丽兹行', '北京', '全城', '普通', 'https://bj.lizihang.com/loupan/p3', 'lizihang'),
    _t(3, '丽兹行', '北京', '全城', '普通', 'https://bj.lizihang.com/loupan/p4', 'lizihang'),
    _t(4, '丽兹行', '北京', '全城', '普通', 'https://bj.lizihang.com/loupan/p22', 'lizihang'),
    _t(5, '丽兹行', '北京', '朝阳', '普通', 'https://bj.lizihang.com/loupan/110105', 'lizihang'),
    _t(6, '丽兹行', '北京', '海淀', '普通', 'https://bj.lizihang.com/loupan/110108', 'lizihang'),
    _t(7, '丽兹行', '北京', '东城', '普通', 'https://bj.lizihang.com/loupan/110101', 'lizihang'),
    _t(8, '丽兹行', '上海', '全城', '普通', 'https://sh.lizihang.com/loupan', 'lizihang'),
    _t(9, '丽兹行', '上海', '全城', '普通', 'https://sh.lizihang.com/loupan/p2', 'lizihang'),
    _t(10, '丽兹行', '上海', '全城', '普通', 'https://sh.lizihang.com/loupan/p3', 'lizihang'),
    _t(11, '丽兹行', '上海', '全城', '普通', 'https://sh.lizihang.com/loupan/p4', 'lizihang'),
    _t(12, '丽兹行', '上海', '全城', '普通', 'https://sh.lizihang.com/loupan/p22', 'lizihang'),
    _t(13, '丽兹行', '上海', '浦东', '普通', 'https://sh.lizihang.com/loupan/310115', 'lizihang'),
    _t(14, '丽兹行', '上海', '徐汇', '普通', 'https://sh.lizihang.com/loupan/310104', 'lizihang'),
    _t(15, '丽兹行', '上海', '黄浦', '普通', 'https://sh.lizihang.com/loupan/310101', 'lizihang'),
    _t(16, '中原', '上海', '全城', '普通', 'https://sh.centanet.com/xiaoqu/', 'centanet'),
    _t(17, '中原', '上海', '全城', '普通', 'https://sh.centanet.com/xiaoqu/g2/', 'centanet'),
    _t(18, '中原', '上海', '全城', '普通', 'https://sh.centanet.com/xiaoqu/g100/', 'centanet'),
    _t(19, '中原', '上海', '全城', '别墅', 'https://sh.centanet.com/xiaoqu/t2/', 'centanet'),
    _t(20, '中原', '上海', '全城', '别墅', 'https://sh.centanet.com/xiaoqu/t2g23/', 'centanet'),
    _t(21, '中原', '上海', '浦东', '普通', 'https://sh.centanet.com/xiaoqu/pudongxinqu/', 'centanet'),
    _t(22, '中原', '上海', '浦东', '普通', 'https://sh.centanet.com/xiaoqu/pudongxinqu/g2/', 'centanet'),
    _t(23, '中原', '上海', '全城', '普通', 'https://sh.centanet.com/xiaoqu/xq-peeewgwyws/',
       'centanet', expected_name='龙湖郦城(云屏路88弄)'),
    _t(24, '中原', '北京', '全城', '入口', 'https://bj.centanet.com/xiaoqu/', None),
    _t(25, '房天下', '上海', '全城', '入口', 'https://sh.esf.fang.com/housing/', 'fang'),
    _t(26, '房天下', '北京', '全城', '入口', 'https://esf.fang.com/housing/', 'fang'),
]


# ---------------------------------------------------------------------------
# small io helpers
# ---------------------------------------------------------------------------
def _atomic_json(path, obj):
    """Atomic replace so a crash never leaves a half-written artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def read_checkpoint(out_dir):
    return _read_json(Path(out_dir) / 'run_state' / 'checkpoint.json')


def _row_belongs(row, url):
    """A redirect hop belongs to the target whose URL started its chain."""
    if row.get('requested_url') == url:
        return True
    return any((hop or {}).get('url') == url for hop in (row.get('redirect_chain') or []))


def _title_from_snapshot(path):
    """Recover a page title from a saved body; used for verbatim block findings."""
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        doc = BeautifulSoup(raw, 'html.parser')
        return doc.title.get_text(' ', strip=True)[:120] if doc.title else None
    except Exception:
        return None


def _load_signals(out_dir):
    report = _read_json(Path(out_dir) / 'entry_probe_report.json')
    if isinstance(report, dict) and isinstance(report.get('signals'), dict):
        return report['signals']
    return {}


def _merge_observations(out_dir, cards):
    path = Path(out_dir) / 'observations.jsonl'
    write_jsonl(path, merge_observations(read_jsonl(path), cards))


def _fang_catalog(body, city, url, fetched_at, raw):
    """Import fang_parser lazily so a listener/offline run never hard-fails on its deps."""
    import fang_parser
    return fang_parser.catalog(body, city, url, fetched_at, raw)


def _cards_for(target, body, row, url):
    """Parse a 200 body with the target's fixed parser; unsupported input parses empty."""
    kind = target.get('parser')
    source = target['source']
    city = target['city']
    if kind == 'fang':
        try:
            result = _fang_catalog(body, city, url, row['requested_at'],
                                   {'path': row.get('snapshot_path'),
                                    'sha256': row.get('sha256')})
            return list(result.get('profiles') or [])
        except Exception:
            return []
    if kind is None and source != '中原':
        return []
    parser = sp.PARSERS.get(kind or 'centanet')
    if parser is None:
        return []
    try:
        return list(parser(body, url, city, source, row['requested_at'], 'http',
                           row.get('snapshot_path')))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# coverage + report (read-only over requests.jsonl)
# ---------------------------------------------------------------------------
def _cell_state(target, rows, next_index, stop_reason, blocked_sources, skipped_sources):
    """One PLAN target's status, derived from its request rows (fetch_status semantics)."""
    if not rows:
        if target['source'] in skipped_sources:
            status, reason = '预算未完成', skipped_sources[target['source']]
        elif target['source'] in blocked_sources:
            status, reason = '预算未完成', stop_reason or 'source_blocked'
        elif target['index'] >= next_index:
            status, reason = '预算未完成', stop_reason or 'not_reached'
        else:
            status, reason = '未测', '无请求记录'
        return status, reason, {'blocked': False, 'class': None, 'status': None,
                                'retry_after': None, 'title': None}

    base = fetch_status(rows, city=target['city'], scope=target['catalog_kind'])
    failure_classes = {r.get('failure_class') for r in rows}
    block_row = next((r for r in rows if r.get('failure_class') in BLOCK_CLASSES), None)
    http202 = next((r for r in rows if r.get('failure_class') == 'http_202'), None)
    if http202 is not None:
        status, reason = '阻断', 'http_202'
    elif block_row is not None:
        status, reason = '阻断', block_row['failure_class']
    elif 'budget_exhausted' in failure_classes:
        status, reason = '预算未完成', 'budget_exhausted'
    elif 'transport_error' in failure_classes:
        status, reason = '阻断', 'transport_error'
    elif sum(int(r.get('cards_parsed') or 0) for r in rows) == 0:
        status, reason = '空目录', base.get('reason') or '解析0条'
    else:
        status, reason = '成功', ''
    marker = block_row or http202
    blocked = {
        'blocked': marker is not None,
        'class': (marker or {}).get('failure_class'),
        'status': (marker or {}).get('status'),
        'retry_after': (marker or {}).get('retry_after'),
        'title': _title_from_snapshot((marker or {}).get('snapshot_path')),
    }
    return status, reason, blocked


def build_coverage(out_dir, *, next_index, stop_reason, blocked_sources, skipped_sources):
    """Per-PLAN-target coverage matrix, keyed by (source, city, district, catalog_kind)."""
    rows = read_jsonl(Path(out_dir) / 'requests.jsonl')
    cells = []
    for target in PLAN:
        target_rows = [r for r in rows if _row_belongs(r, target['url'])]
        status, reason, blocked = _cell_state(
            target, target_rows, next_index, stop_reason, blocked_sources, skipped_sources)
        cells.append({
            'index': target['index'], 'source': target['source'], 'city': target['city'],
            'district': target['district'], 'catalog_kind': target['catalog_kind'],
            'url': target['url'],
            'key': [target['source'], target['city'], target['district'],
                    target['catalog_kind']],
            'status': status, 'reason': reason,
            'requests': len(target_rows),
            'cards_parsed': sum(int(r.get('cards_parsed') or 0) for r in target_rows),
            'snapshot_paths': sorted({r.get('snapshot_path') for r in target_rows
                                      if r.get('snapshot_path')}),
            'failure_classes': sorted({r.get('failure_class') for r in target_rows
                                       if r.get('failure_class')}),
            'blocked': blocked,
        })
    summary = {state: sum(1 for c in cells if c['status'] == state) for state in sp.FETCH_STATES}
    return {'schema': COVERAGE_SCHEMA, 'generated_at': now_iso(), 'stage': STAGE,
            'stop_reason': stop_reason, 'cells': cells, 'summary': summary}


def build_block_findings(rows):
    """Verbatim hard-block findings (status/url/retry_after/title snippet) from request rows."""
    findings = []
    for row in rows:
        failure_class = row.get('failure_class')
        if failure_class not in BLOCK_CLASSES:
            continue
        findings.append({
            'source': row.get('source'), 'city': row.get('city'),
            'url': row.get('final_url') or row.get('requested_url'),
            'status': row.get('status'), 'failure_class': failure_class,
            'retry_after': row.get('retry_after'),
            'title': _title_from_snapshot(row.get('snapshot_path')),
            'snapshot_path': row.get('snapshot_path'), 'sha256': row.get('sha256'),
            'requested_at': row.get('requested_at'),
        })
    return findings


def _build_report(out_dir, *, coverage, stop_reason, blocked_sources, skipped_sources,
                  signals, store, raw_dir, checkpoint_path, clock):
    rows = read_jsonl(Path(out_dir) / 'requests.jsonl')
    network_rows = [r for r in rows if r.get('network_attempt') and r.get('request_id') is not None]
    per_source = {}
    per_stage = {}
    for row in network_rows:
        per_source[row.get('source')] = per_source.get(row.get('source'), 0) + 1
        per_stage[row.get('stage')] = per_stage.get(row.get('stage'), 0) + 1
    store_report = store.report(now=clock())
    override = load_platform_policy_overrides().get('房天下', {})
    pause_until = override.get('pause_until')
    fang_paused = {
        'active': pause_until is not None and clock() < pause_until,
        'until': datetime.fromtimestamp(pause_until, timezone.utc).isoformat()
        if pause_until else None,
    }
    unconfirmed = [{'source': c['source'], 'city': c['city'], 'url': c['url'],
                    'status': c['status'], 'reason': c['reason']}
                   for c in coverage['cells']
                   if c['catalog_kind'] == '入口' and c['status'] not in ('成功', '空目录')]
    return {
        'schema': REPORT_SCHEMA, 'generated_at': now_iso(), 'stage': STAGE,
        'stop_reason': stop_reason,
        'requests_used': {
            'total': len(network_rows), 'per_source': per_source, 'per_stage': per_stage,
            'store_total': store_report['trial']['used'],
            'store_stage': store_report['stages'].get(STAGE, {}).get('used'),
        },
        'requests_jsonl_row_count': len(rows),
        'raw_snapshot_count': len(list(Path(raw_dir).glob('*.html'))),
        'block_findings': build_block_findings(rows),
        'fang_paused': fang_paused,
        'checkpoint_path': str(checkpoint_path),
        'coverage': coverage['summary'],
        'unconfirmed_entries': unconfirmed,
        'blocked_sources': list(blocked_sources),
        'skipped_sources': dict(skipped_sources),
        'signals': signals,
    }


# ---------------------------------------------------------------------------
# stage runner
# ---------------------------------------------------------------------------
def run(out_dir, *, resume=False, transport=None, clock=None):
    """Execute the 入口与结构探测 stage against fixed PLAN targets.

    transport/clock are injectable so tests exercise the loop with no network and no
    real sleep. Every real attempt is appended and checkpointed immediately.
    """
    out_dir = Path(out_dir)
    raw_dir = out_dir / 'raw'
    run_state_dir = out_dir / 'run_state'
    raw_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or _now_epoch
    transport = transport or sp.single_request
    store = BudgetStore(run_state_dir, trial_total=TRIAL_TOTAL_CAP,
                        platform_overrides=load_platform_policy_overrides(), clock=clock)
    try:
        store.acquire_lock()
    except BudgetLockError as exc:
        print(json.dumps({'stage': STAGE, 'error': 'budget_lock', 'detail': str(exc)},
                         ensure_ascii=False))
        return 2

    checkpoint_path = run_state_dir / 'checkpoint.json'
    next_index = 0
    last_request_id = None
    cursor = None
    blocked_sources = []
    skipped_sources = {}
    stop_reason = None
    signals = _load_signals(out_dir)
    if resume:
        checkpoint = read_checkpoint(out_dir)
        if checkpoint and checkpoint.get('stage') == STAGE:
            next_index = int(checkpoint.get('next_index') or 0)
            blocked_sources = list(checkpoint.get('blocked_sources') or [])
            skipped_sources = dict(checkpoint.get('skipped_sources') or {})
            last_request_id = checkpoint.get('last_request_id')
            stop_reason = None  # resume re-opens the stage so remaining sources run

    session = requests.Session()
    session.trust_env = False  # never honour environment proxies
    session.headers['User-Agent'] = UA

    def checkpoint_now():
        _atomic_json(checkpoint_path, {
            'schema': CHECKPOINT_SCHEMA, 'stage': STAGE, 'updated_at': now_iso(),
            'plan_total': len(PLAN), 'next_index': next_index,
            'completed': [t['url'] for t in PLAN[:next_index]],
            'last_request_id': last_request_id, 'cursor': cursor,
            'counters': store.report(now=clock()),
            'blocked_sources': list(blocked_sources),
            'skipped_sources': dict(skipped_sources),
            'stop_reason': stop_reason,
        })

    def base_row(target, url, request_id):
        source = target['source']
        return {
            'source': source, 'city': target['city'],
            'combo': combo_of(source, target['city']), 'requested_url': url,
            'final_url': url, 'requested_at': now_iso(), 'status': None,
            'redirect_chain': [], 'failure_class': None, 'error_type': None,
            'snapshot_path': None, 'sha256': None, 'bytes': None, 'content_type': None,
            'retry_after': None, 'elapsed_ms': None, 'cards_parsed': 0,
            'body_state': None, 'request_id': request_id, 'stage': STAGE,
            'platform': source, 'network_attempt': True,
        }

    def emit(row):
        append_jsonl(out_dir / 'requests.jsonl', [row])
        if row.get('snapshot_path') and (row.get('failure_class') or row.get('status') != 200):
            copy_to_failed(raw_dir, row['snapshot_path'])
        checkpoint_now()

    def classify_success(target, body, row, url):
        cards = _cards_for(target, body, row, url)
        row['cards_parsed'] = len(cards)
        doc = BeautifulSoup(body, 'html.parser')
        title = doc.title.get_text(' ', strip=True) if doc.title else ''
        text = doc.get_text(' ', strip=True)
        if is_challenge(title, text, bool(cards), final_url=row.get('final_url') or url,
                        redirect_chain=row.get('redirect_chain'), body=body):
            row['failure_class'] = 'challenge'
        elif not cards:
            row['failure_class'] = 'no_cards'
        else:
            _merge_observations(out_dir, cards)
            merge_signals(signals, target['source'], body,
                          snapshot_path=row.get('snapshot_path'), url=url,
                          city=target['city'])

    def reserve_attempt(target, url):
        """Sleep through interval gates; on any hard cap emit a refusal row and stop."""
        nonlocal stop_reason
        source = target['source']
        combo = combo_of(source, target['city'])
        while True:
            res = store.reserve(source, STAGE, combo, url, now=clock())
            if res['allowed']:
                return res['request_id']
            if res['reason'] == 'interval_not_elapsed':
                _sleep(res['wait_seconds'])
                continue
            store.record_exhausted(platform=source, stage=STAGE, combo=combo,
                                   requested_url=url, reason=res['reason'], now=clock())
            row = base_row(target, url, None)
            row['failure_class'] = 'budget_exhausted'
            row['network_attempt'] = False
            row['blocking_reason'] = res['reason']
            emit(row)
            stop_reason = res['reason']
            return None

    def fetch_hop(target, url, chain, request_id):
        row = base_row(target, url, request_id)
        row['redirect_chain'] = list(chain)
        outcome = transport(session, url, TIMEOUT)
        row['elapsed_ms'] = outcome.get('elapsed_ms')
        response = outcome.get('response')
        if response is None:
            row['failure_class'] = 'transport_error'
            row['error_type'] = outcome.get('error_type')
            row['body_state'] = 'no_body'
            emit(row)
            return row

        body = response.content
        status = response.status_code
        headers = response.headers
        row['status'] = status
        row['content_type'] = headers.get('Content-Type')
        row['retry_after'] = headers.get('Retry-After')
        row['bytes'] = len(body)
        row['sha256'] = hashlib.sha256(body).hexdigest()
        row['body_state'] = 'retained' if body else 'no_body'
        if body:
            row['snapshot_path'] = str(save_raw(
                raw_dir, combo_of(target['source'], target['city']), url, body,
                row['sha256']))
        if status == 403:
            row['failure_class'] = 'blocked_403'
        elif status == 429:
            row['failure_class'] = 'blocked_429'
        elif status == 202:
            row['failure_class'] = 'http_202'
        elif 300 <= status < 400 and headers.get('Location'):
            location = headers['Location']
            next_url = urljoin(url, location)
            row['final_url'] = next_url
            row['redirect_chain'] = list(chain) + [{'url': url, 'status': status,
                                                    'location': location, 'next': next_url}]
            if sp.challenge_url_markers(next_url, row['redirect_chain']):
                # Redirect into a known CAPTCHA host is a block, not just a hop.
                row['failure_class'] = 'challenge'
            elif len(chain) >= sp.MAX_REDIRECT_HOPS:
                row['failure_class'] = 'transport_error'
                row['error_type'] = 'TooManyRedirects'
        elif status != 200:
            row['failure_class'] = 'transport_error'
            row['error_type'] = f'http_{status}'
        else:
            classify_success(target, body, row, url)
        emit(row)
        return row

    def process_target(target):
        """Follow one target's manual redirects; each hop reserves and logs one request."""
        nonlocal last_request_id, stop_reason
        current = target['url']
        chain = []
        while True:
            request_id = reserve_attempt(target, current)
            if request_id is None:
                return 'stop', stop_reason
            last_request_id = request_id
            row = fetch_hop(target, current, chain, request_id)
            status = row['status']
            failure = row['failure_class']
            if status is None:
                stop_reason = 'transport_error'
                return 'stop', 'transport_error'
            if failure in BLOCK_CLASSES:
                stop_reason = failure
                if target['source'] not in blocked_sources:
                    blocked_sources.append(target['source'])
                store.pause_platform(target['source'], failure, now=clock())
                return 'stop', failure
            if failure == 'transport_error':
                stop_reason = 'transport_error'
                return 'stop', 'transport_error'
            if failure == 'http_202':
                return 'skip_source', 'http_202'
            if failure == 'no_cards':
                return 'skip_source', 'no_cards'
            is_redirect = (failure is None and 300 <= status < 400
                           and row['redirect_chain']
                           and row['redirect_chain'][-1]['url'] == current)
            if is_redirect:
                chain = row['redirect_chain']
                current = chain[-1]['next']
                continue
            return 'ok', None

    coverage = {'schema': COVERAGE_SCHEMA, 'cells': [], 'summary': {}}
    report = {'requests_used': {}, 'coverage': {}}
    try:
        for target in PLAN:
            index = target['index']
            source = target['source']
            if index < next_index:
                continue
            if stop_reason is not None:
                break
            if source in blocked_sources or source in skipped_sources:
                next_index = index + 1
                cursor = {'source': source, 'url': target['url']}
                checkpoint_now()
                continue
            cursor = {'source': source, 'url': target['url']}
            checkpoint_now()
            outcome, reason = process_target(target)
            next_index = index + 1
            if outcome == 'skip_source':
                skipped_sources[source] = reason
            checkpoint_now()
            if outcome == 'stop':
                break

        coverage = build_coverage(out_dir, next_index=next_index,
                                  stop_reason=stop_reason,
                                  blocked_sources=blocked_sources,
                                  skipped_sources=skipped_sources)
        _atomic_json(out_dir / 'coverage.json', coverage)
        report = _build_report(out_dir, coverage=coverage, stop_reason=stop_reason,
                               blocked_sources=blocked_sources,
                               skipped_sources=skipped_sources, signals=signals,
                               store=store, raw_dir=raw_dir,
                               checkpoint_path=checkpoint_path, clock=clock)
        _atomic_json(out_dir / 'entry_probe_report.json', report)
    finally:
        store.release_lock()

    print(json.dumps({'stage': STAGE, 'stop_reason': stop_reason,
                      'requests_used': report.get('requests_used'),
                      'coverage': report.get('coverage')}, ensure_ascii=False))
    return 0 if stop_reason is None else 2


def dry_run_payload(out_dir, *, clock=None):
    """PLAN + persisted budget + checkpoint; read-only, ZERO network."""
    out_dir = Path(out_dir)
    clock = clock or _now_epoch
    store = BudgetStore(out_dir / 'run_state', trial_total=TRIAL_TOTAL_CAP,
                        platform_overrides=load_platform_policy_overrides(), clock=clock)
    return {'stage': STAGE, 'plan_total': len(PLAN), 'plan': PLAN,
            'budget': store.report(now=clock()),
            'checkpoint': read_checkpoint(out_dir)}


def status_payload(out_dir, *, clock=None):
    out_dir = Path(out_dir)
    clock = clock or _now_epoch
    store = BudgetStore(out_dir / 'run_state', trial_total=TRIAL_TOTAL_CAP,
                        platform_overrides=load_platform_policy_overrides(), clock=clock)
    return {'checkpoint': read_checkpoint(out_dir), 'budget': store.report(now=clock())}


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def _build_cli():
    parser = argparse.ArgumentParser(
        description='入口与结构探测 stage: bounded entry/structure probe.')
    parser.add_argument('command', nargs='?', default='run',
                        choices=['run', 'status'],
                        help="'run' (default, real; --resume) or 'status'")
    parser.add_argument('--resume', action='store_true',
                        help='resume from checkpoint next_index')
    parser.add_argument('--dry-run', action='store_true',
                        help='print PLAN + budget + checkpoint, zero network')
    parser.add_argument('--status', action='store_true',
                        help='print checkpoint + budget')
    parser.add_argument('--out', default=DEFAULT_OUT,
                        help=f'experiment directory (default {DEFAULT_OUT})')
    return parser


def main(argv=None):
    args = _build_cli().parse_args(argv)
    out_dir = Path(args.out)
    if args.status or args.command == 'status':
        print(json.dumps(status_payload(out_dir), ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        print(json.dumps(dry_run_payload(out_dir), ensure_ascii=False, indent=2))
        return 0
    return run(out_dir, resume=args.resume)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
