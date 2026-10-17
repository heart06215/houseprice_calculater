"""Acceptance cannot pass on a challenge page, another city, or existing data."""
import json
import unittest
from datetime import date

import anjuke_anonymous as A


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


if __name__ == '__main__':
    unittest.main()
