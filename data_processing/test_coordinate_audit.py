# -*- coding: utf-8 -*-
"""坐标系转换与同名互证审计的离线回归，不联网。"""
import unittest

import coordinate_audit as CA


class CoordinateAuditTests(unittest.TestCase):
    def test_gcj02_roundtrip_is_stable(self):
        for lat, lng in ((39.9042, 116.4074), (31.2304, 121.4737), (23.1291, 113.2644)):
            glat, glng = CA.wgs84_to_gcj02(lat, lng)
            back = CA.gcj02_to_wgs84(glat, glng)
            self.assertLess(abs(back[0] - lat), 1e-4)
            self.assertLess(abs(back[1] - lng), 1e-4)

    def test_audit_identifies_bd09_source(self):
        wgs = (39.9042, 116.4074)
        glat, glng = CA.wgs84_to_gcj02(*wgs)
        blat, blng = CA.gcj02_to_bd09(glat, glng)
        source = [dict(name='测试小区', city='北京', lat=blat, lng=blng)]
        reference = [dict(name='测试小区', city='北京', lat=wgs[0], lng=wgs[1])]
        result = CA.audit(source, reference)
        self.assertEqual(result['pairs'], 1)
        self.assertEqual(result['winner'], 'bd09')
        self.assertLess(result['summary']['bd09']['median_m'], 5)
        self.assertGreater(result['summary']['raw']['median_m'], 100)

    def test_audit_requires_a_unique_same_name_match(self):
        source = [dict(name='同名A', city='北京', lat=39.9, lng=116.4),
                  dict(name='没有参照', city='北京', lat=39.9, lng=116.4)]
        reference = [dict(name='同名A', city='北京', lat=39.9, lng=116.4),
                     dict(name='同名A', city='北京', lat=39.91, lng=116.41)]
        result = CA.audit(source, reference)
        self.assertEqual(result['pairs'], 0)

    def test_haversine_matches_known_distance(self):
        # ~1 degree of latitude is about 111 km.
        self.assertGreater(CA.haversine(39.0, 116.0, 40.0, 116.0), 110000)
        self.assertLess(CA.haversine(39.0, 116.0, 40.0, 116.0), 112000)


if __name__ == '__main__':
    unittest.main()
