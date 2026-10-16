"""A file name or legacy source string is not evidence of an observed price."""
from urllib.parse import urlparse


def listing_evidence(row):
    url=row.get('source_url') or row.get('url')
    captured=row.get('fetched_at') or row.get('crawl_time')
    host=urlparse(url or '').hostname or ''
    traceable=bool(captured and (host=='anjuke.com' or host.endswith('.anjuke.com')))
    return dict(status='traceable_listing' if traceable else 'unverified_legacy',
                source_url=url, fetched_at=captured, verified_transaction=False,
                raw=row.get('raw'),
                claimed_source=row.get('data_source','anjuke'),
                note='采集链接与时间可追溯，非成交核验' if traceable else '历史文件自称安居客，但缺少采集链接或时间，无法确认原始来源及生成方式')


def apply_provenance(point, raw, anchor_raw=None):
    method=point.get('method')
    if method in ('listing_observation','legacy_unverified') or (not method and point.get('source')=='anjuke'):
        evidence=listing_evidence(raw)
        traced=evidence['status']=='traceable_listing'
        point.update(source='anjuke' if traced else 'legacy_unverified',
                     method='listing_observation' if traced else 'legacy_unverified',
                     quality=point.get('quality','ok') if traced else 'unverified', evidence=evidence)
    elif method=='unavailable':
        point.update(source='unavailable',quality='unverified',evidence=dict(kind='missing',verified_transaction=False,
            note='缺少可核对的观测锚点，不发布绝对价格；官方价格指数仍可用'))
    elif method=='index_estimate':
        point['source']='index_estimate'
        point['evidence']=dict(kind='estimate',verified_transaction=False,
            anchor=listing_evidence(anchor_raw or {}),anchor_date=point.get('anchor_date'),
            note='平台用统计局环比计算的价格，不是统计局发布的绝对价格')
    else:
        point['source']='reference_estimate'
        point['evidence']=dict(kind='estimate',status='unverified_reference',verified_transaction=False,
            note='预设参考价结合指数推导，参考锚点未核验')
    return point
