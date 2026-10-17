"""Acceptance cannot pass on a challenge page, another city, or existing data."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import anjuke_anonymous as A
import anjuke_province_crawler as P


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 1 if any(token in self.selector for token in self.page.visible) else 0

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self.page.clicks.append(self.selector)


class FakePage:
    def __init__(self, url, visible=()):
        self.url = url
        self.visible = set(visible)
        self.clicks = []

    def locator(self, selector):
        return FakeLocator(self, selector)


class ChallengeFlowTests(unittest.TestCase):
    def test_page_is_challenge_only_for_gateway_and_captcha_shells(self):
        self.assertTrue(A.page_is_challenge('https://callback.58.com/antibot/verifycode', '<html></html>'))
        self.assertTrue(A.page_is_challenge('https://www.anjuke.com/esfcommon-captcha-wy?x=1', '<html></html>'))
        self.assertTrue(A.page_is_challenge('https://bj.anjuke.com/sale/', '<div id="@@xxzlGatewayUrl"></div>'))
        self.assertFalse(A.page_is_challenge('https://bj.anjuke.com/sale/', '<div class="property"></div>'))

    def test_first_round_opens_the_site_challenge_button(self):
        page = FakePage('https://callback.58.com/antibot/verifycode', visible=('#btnSubmit',))
        action = A.next_challenge_action(page, None, Path('.'), 'k', [], [], 0)
        self.assertEqual(action, 'open_site_challenge')
        self.assertTrue(page.clicks)

    def test_secondary_page_opens_its_own_verification_button(self):
        page = FakePage('https://www.anjuke.com/esfcommon-captcha-wy?x=1', visible=('verify-button button',))
        action = A.next_challenge_action(page, None, Path('.'), 'k', [], [], 0)
        self.assertEqual(action, 'open_secondary_verification')

    def test_intellisense_is_one_click_and_bounded(self):
        page = FakePage('https://www.anjuke.com/esfcommon-captcha-wy?x=1',
                        visible=('.yidun_intelli-control',))
        self.assertEqual(A.next_challenge_action(page, None, Path('.'), 'k', [], [], 0), 'click_intellisense')
        self.assertIsNone(A.next_challenge_action(page, None, Path('.'), 'k', [], [], 3))
        self.assertIsNone(A.next_challenge_action(page, None, Path('.'), 'k', [], [], 0, solve=False))

    def test_slider_can_repeat_until_the_round_budget_is_spent(self):
        page = FakePage('https://callback.58.com/antibot/verifycode', visible=('img.yidun_jigsaw',))
        with patch('anjuke_slider_probe.try_slider', return_value=dict(state='drag_submitted')) as slider:
            attempts = []
            self.assertEqual(A.next_challenge_action(page, object(), Path('.'), 'k', [], attempts, 0),
                             'solve_slider')
            self.assertEqual(len(attempts), 1)
            self.assertTrue(slider.called)
        self.assertEqual(A.next_challenge_action(page, None, Path('.'), 'k', [],
                                                 [dict(state='x')] * 4, 0), None)

    def test_multi_round_actions_are_recorded_in_order(self):
        # Slider, secondary button and one-click verification may all appear in one visit.
        page = FakePage('https://callback.58.com/antibot/verifycode', visible=('#btnSubmit',))
        actions, sliders = [], []
        with patch('anjuke_slider_probe.try_slider', return_value=dict(state='drag_submitted')):
            page.visible = {'#btnSubmit'}
            actions.append(A.next_challenge_action(page, object(), Path('.'), 'k', actions, sliders, 0))
            page.url = 'https://www.anjuke.com/esfcommon-captcha-wy?x=1'
            page.visible = {'verify-button button'}
            actions.append(A.next_challenge_action(page, object(), Path('.'), 'k', actions, sliders, 0))
            page.visible = {'.yidun_intelli-control'}
            actions.append(A.next_challenge_action(page, object(), Path('.'), 'k', actions, sliders, 0))
        self.assertEqual(actions, ['open_site_challenge', 'open_secondary_verification', 'click_intellisense'])


class AnonymousTests(unittest.TestCase):
    def price_page(self, city):
        return '<script>window.__NUXT__=' + json.dumps(dict(data=[{}, {}, dict(
            infoList=dict(cityName=city), year=date.today().year - 1,
            yearAreaData=dict(trendList=[dict(title='1月', avgPrice=12345)]))])) + '</script>'

    def test_real_completed_month_is_observed(self):
        target = A.targets()[0]
        result = A.parse_response(target, self.price_page('北京'), target['url'], 200)
        self.assertEqual(result['state'], 'observed')
        self.assertEqual(result['records'][0]['price'], 12345)

    def test_wrong_city_and_empty_200_are_failures(self):
        target = A.targets()[0]
        for html in (self.price_page('上海'), '<html>访问中</html>'):
            self.assertNotEqual(A.parse_response(target, html, target['url'], 200)['state'], 'observed')

    def test_optional_login_prompt_does_not_hide_delivered_public_data(self):
        target = A.targets()[0]
        html = self.price_page('北京') + '<aside>登录后查看收藏</aside>'
        self.assertEqual(A.parse_response(target, html, target['url'], 200)['state'], 'observed')

    def test_captcha_redirect_never_counts_as_data(self):
        target = A.targets()[0]
        result = A.parse_response(target, self.price_page('北京'),
                                  'https://callback.58.com/antibot/verifycode', 200)
        self.assertEqual(result['state'], 'challenge')
        self.assertEqual(result['records'], [])
        result = A.parse_response(target, '', 'https://www.anjuke.com/esfcommon-captcha-wy', 302)
        self.assertEqual(result['state'], 'challenge')

    def test_explicit_beijing_browser_selection_reaches_the_source(self):
        # Beijing is absent from the default changed-slug list; it must still run.
        year = date.today().year - 1
        info = dict(city='北京', blocks=[dict(year=year, months=[dict(month=1, price=12345)])])
        with patch.object(P.sys, 'platform', 'linux'), \
             patch.object(P, 'existing_year_months', return_value=set()), \
             patch.object(P, 'browser_extract', return_value='fresh response') as fetch, \
             patch.object(P, 'extract_trend', return_value=info), \
             patch.object(P.time, 'sleep'), \
             patch.object(P, 'merge_city_rows', return_value=(1, 1, [])) as merge, \
             patch('builtins.print'):
            P.crawl_browser_mode(years_from=year, only_cities=['北京'], sleep_s=0)
        self.assertTrue(fetch.called)
        self.assertIn('/fangjia/beijing', fetch.call_args_list[0].args[0])
        self.assertEqual(merge.call_args.args[:2], ('beijing', '北京'))

    def test_sdk_fault_scope_is_exact(self):
        self.assertTrue(A.sdk_loader('https://cstaticdun.126.net/load.min.js?v=123'))
        for url in ('https://cstaticdun.126.net.evil.example/load.min.js',
                    'https://j1.58cdn.com.cn/firewall/NEcaptcha.js',
                    'https://callback.58.com/antibot/yidun/checkcode.do'):
            self.assertFalse(A.sdk_loader(url))

    def test_defense_capture_keeps_outcome_without_validation_values(self):
        result = A.defense_result(json.dumps(dict(code=0, data=dict(captchaId='private-id'),
                                                  validate='private-value', sign='private-sign',
                                                  msg='https://bj.anjuke.com/sale/')))
        self.assertEqual(result['code'], 0)
        self.assertTrue(result['captcha_config'])
        self.assertTrue(result['redirect'])
        self.assertNotIn('private', json.dumps(result))

    def test_exports_contain_only_this_run_observed_records(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out / 'exports').mkdir()
            (out / 'exports/prices.json').write_text('[{"price": 999}]', encoding='utf8')
            fresh = dict(city='北京', month='2026-01', price=12345)
            report = dict(attempts=[dict(kind='prices', state='observed', records=[fresh, fresh]),
                                   dict(kind='prices', state='challenge', records=[dict(price=666)])])
            counts = A.export_records(report, out)
            self.assertEqual(counts, dict(prices=1, communities=0, properties=0))
            self.assertEqual(json.loads((out / 'exports/prices.json').read_text(encoding='utf8')), [fresh])


if __name__ == '__main__':
    unittest.main()
