# -*- coding: utf-8 -*-
"""Phase F 请求级预算断路器：发请求前逐条判定，底层子资源同样计量。

纯逻辑：无网络、无浏览器、导入无副作用。所有允许/拒绝/中止决策只走
decide_route，保证“导航与底层资源同一次请求不重复加总”。
"""
import anjuke_browser_probe as probe

BLOCK = 'block'
ABORT = 'abort'
CONTINUE = 'continue'


class RequestCircuitBreaker:
    """共享账本快照 + 本次运行计数器；拒绝发生在计数之前（before send）。"""

    def __init__(self, *, ledger, today, run_business_nav_cap, trial_total,
                 per_source_daily_cap, combined_daily_cap, platform_overrides=None,
                 run_id='run-F', clock=None):
        self.run_id = run_id
        self.today = today
        self.run_business_nav_cap = int(run_business_nav_cap)
        self.trial_total = int(trial_total)
        self.per_source_daily_cap = int(per_source_daily_cap)
        self.combined_daily_cap = int(combined_daily_cap)
        self.platform_overrides = dict(platform_overrides or {})
        self.clock = clock or probe.utcnow
        self.baseline_trial_used = int(ledger['trial_used'])
        self.baseline_combined = int(ledger.get('combined_daily_used', {}).get(today, 0))
        self.baseline_per_source = dict(ledger.get('platform_daily_used', {}).get(today, {}))
        self.business_navs = 0
        self.counted_total = 0
        self.per_source_run = {}
        self.exhausted_events = []
        self.stop_reason = None

    def _platform_cap(self, ledger_key):
        override = self.platform_overrides.get(ledger_key, {})
        if override.get('daily_cap') is not None:
            return int(override['daily_cap'])
        return self.per_source_daily_cap

    def min_interval_seconds(self, ledger_key):
        """Platform pacing override interval, or None when not set."""
        override = self.platform_overrides.get(ledger_key, {})
        value = override.get('interval_seconds')
        return float(value) if value is not None else None

    def check(self, ledger_key, *, is_business_nav=False):
        """只读判定：used >= cap 即拒绝，拒绝时不改任何计数器。"""
        if is_business_nav and self.business_navs >= self.run_business_nav_cap:
            return False, 'run_business_nav_cap'
        if self.baseline_trial_used + self.counted_total >= self.trial_total:
            return False, 'trial_total_cap'
        used = self.baseline_per_source.get(ledger_key, 0) + self.per_source_run.get(ledger_key, 0)
        if used >= self._platform_cap(ledger_key):
            return False, 'per_source_daily_cap'
        if self.baseline_combined + self.counted_total >= self.combined_daily_cap:
            return False, 'combined_daily_cap'
        return True, None

    def record_counted(self, ledger_key, *, is_business_nav=False):
        self.counted_total += 1
        self.per_source_run[ledger_key] = self.per_source_run.get(ledger_key, 0) + 1
        if is_business_nav:
            self.business_navs += 1

    def record_exhausted(self, *, ledger_key, reason, requested_url=None, resource_type=None,
                         is_business_nav=False, record_id=None, source=None, city=None, now=None):
        event = {
            'at': now if now is not None else self.clock(),
            'run_id': self.run_id,
            'record_id': record_id,
            'source': source,
            'city': city,
            'ledger_key': ledger_key,
            'requested_url': probe.sanitize(requested_url) if requested_url else None,
            'resource_type': resource_type,
            'is_business_nav': bool(is_business_nav),
            'reason': reason,
            'scope': 'per_request',
            'note': 'aborted before send',
        }
        self.exhausted_events.append(event)
        if self.stop_reason is None:
            self.stop_reason = reason
        return event

    def snapshot(self):
        """只读快照；绝不改共享账本。"""
        return {
            'run_id': self.run_id,
            'today': self.today,
            'baseline_trial_used': self.baseline_trial_used,
            'baseline_combined': self.baseline_combined,
            'baseline_per_source': dict(self.baseline_per_source),
            'business_navs': self.business_navs,
            'counted_total': self.counted_total,
            'per_source_run': dict(self.per_source_run),
            'stop_reason': self.stop_reason,
            'events': list(self.exhausted_events),
        }


def decide_route(*, blockable, breaker, ledger_key, is_business_nav=False):
    """唯一判定路径：返回 (decision, reason)。

    blockable -> 屏蔽；预算拒绝 -> 中止（发请求前）；否则计数并放行。
    """
    if blockable:
        return BLOCK, 'blocked_no_network'
    allowed, reason = breaker.check(ledger_key, is_business_nav=is_business_nav)
    if not allowed:
        return ABORT, reason
    breaker.record_counted(ledger_key, is_business_nav=is_business_nav)
    return CONTINUE, None
