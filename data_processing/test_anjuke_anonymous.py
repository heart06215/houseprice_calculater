"""Acceptance cannot pass on a challenge page, another city, or existing data."""
import json
import unittest
from datetime import date
from unittest.mock import patch

import anjuke_anonymous as A
import anjuke_province_crawler as P


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


if __name__ == '__main__':
    unittest.main()
