#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
爬虫可行性检测 - 仅用 requests + BeautifulSoup
如遇CAPTCHA/反爬，记录云端诊断结果并停止
"""
import requests, json, os, sys, time

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

TARGETS = [
    ('fang.com', 'https://m.fang.com/fangjia/fjmap.html', 'GET'),
    ('贝壳找房', 'https://bj.ke.com/ershoufang/', 'GET'),
    ('安居客', 'https://www.anjuke.com/fangjia/beijing2026/', 'GET'),
    ('58同城', 'https://bj.58.com/ershoufang/', 'GET'),
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

BLOCK_INDICATORS = ['验证','captcha','403','访问被拒绝','请稍后重试','系统检测']

def check_site(name, url, method='GET'):
    print(f'[{name}] {url}')
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        code = r.status_code
        size = len(r.text)
        text_lower = r.text[:2000].lower()

        blocked = any(ind in text_lower for ind in BLOCK_INDICATORS)
        result = {'name': name, 'url': url, 'status': code, 'size': size, 'blocked': blocked}

        if blocked or code >= 400:
            result['usable'] = False
            result['reason'] = f'HTTP {code}' if code>=400 else 'CAPTCHA/block detected'
            print(f'  -> BLOCKED ({result["reason"]})')
        elif size < 500:
            result['usable'] = False
            result['reason'] = 'response too small'
            print(f'  -> BLOCKED (size={size})')
        else:
            result['usable'] = True
            result['reason'] = 'OK'
            print(f'  -> OK (HTTP {code}, {size}B)')

        return result
    except requests.exceptions.Timeout:
        print('  -> TIMEOUT')
        return {'name': name, 'url': url, 'status': 0, 'size': 0, 'blocked': True, 'usable': False, 'reason': 'timeout'}
    except Exception as e:
        print(f'  -> ERROR: {e}')
        return {'name': name, 'url': url, 'status': 0, 'size': 0, 'blocked': True, 'usable': False, 'reason': str(e)[:100]}

if __name__ == '__main__':
    print('=== 爬虫可行性检测 ===\n')
    results = []
    for name, url, method in TARGETS:
        r = check_site(name, url, method)
        results.append(r)
        time.sleep(1.5)

    # 分析
    usable = [r for r in results if r['usable']]
    blocked = [r for r in results if not r['usable']]

    print(f'\n=== 结果: {len(usable)}/{len(results)} 可用 ===')

    path = os.path.join(DATA_DIR, 'crawler_check.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f'\n检测结果已输出至: {path}')

    if len(usable) == 0:
        print('本次云端 HTTP 探测未发现可用目标；查看 Actions 日志与各站点失败状态。')
        deps = {
            'state': 'blocked',
            'runtime': 'github-actions',
            'explanation': '依赖由云端工作流准备；可使用 anjuke_browser 或 anjuke_probe 检查无头浏览器可达性。'
        }
        print(json.dumps(deps, ensure_ascii=False, indent=2))
        path = os.path.join(DATA_DIR, 'crawler_dependencies.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(deps, f, ensure_ascii=False, indent=2)
        print(f'\n云端诊断状态已输出至: {path}')
        sys.exit(1)
    else:
        print(f'可用平台: {", ".join(r["name"] for r in usable)}')
        print(f'不可用: {", ".join(r["name"] for r in blocked)}')
