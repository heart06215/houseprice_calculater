# -*- coding: utf-8 -*-
"""community_directory → 主目录合并的离线回归，不联网。"""
import json
import tempfile
import unittest
from pathlib import Path

import community_catalog as catalog
import community_directory_publish as P


RECORDS = [
    dict(source='我爱我家', source_group='5i5j', city='北京', name='杏花西里',
         detail_url='https://m.5i5j.com/bj/xiaoqu/339050.html', fetched_at='2026-10-18T00:00:00+00:00',
         year=1982, year_end=2022, year_text='1982-2022年', building_type='板楼',
         lng=115.964401, lat=39.726601, tags=[]),
    dict(source='58同城', source_group='58', city='北京', name='美景东方',
         detail_url='https://bj.58.com/xiaoqu/meijingdongfang/', fetched_at='2026-10-18T00:00:00+00:00',
         year=2002, year_text='2002年竣工', district='朝阳', subarea='华威桥',
         address='松榆南路38号', tags=['别墅'], lng=None, lat=None),
]


class PublishTests(unittest.TestCase):
    def test_source_object_ids_are_stable_and_account_free(self):
        self.assertEqual(P.source_object_id(RECORDS[0]), '5i5j:339050')
        self.assertEqual(P.source_object_id(RECORDS[1]), '58:meijingdongfang')
        self.assertEqual(P.source_object_id(dict(source='安居客', stable_id='12345')), 'anjuke:12345')

    def test_year_fields_range_and_approximate(self):
        self.assertEqual(P.year_fields(RECORDS[0])[:3], (1982, 2022, 'range'))
        self.assertEqual(P.year_fields(dict(year=1982, year_text='约1982年建成'))[:3],
                         (1982, 1982, 'approximate'))
        self.assertEqual(P.year_fields(dict(year=2002, year_text='2002年竣工'))[:3],
                         (2002, 2002, 'exact'))

    def test_villa_tag_becomes_residential_form(self):
        forms, subtypes = P.residential_forms(RECORDS[1])
        self.assertEqual(forms, ['别墅'])
        self.assertEqual(subtypes, [])

    def test_verified_5i5j_coordinates_convert_from_bd09(self):
        from coordinate_audit import bd09_to_wgs84
        obs = P.observation(RECORDS[0], {'北京': '110100'})
        self.assertEqual(obs['source'], '5i5j')
        self.assertEqual(obs['coord_system'], 'wgs84')
        self.assertEqual(obs['precision_level'], 'point')
        self.assertEqual(obs['city_code'], '110100')
        lat, lng = bd09_to_wgs84(RECORDS[0]['lat'], RECORDS[0]['lng'])
        self.assertAlmostEqual(obs['latitude'], lat)
        self.assertAlmostEqual(obs['longitude'], lng)
        unverified = P.observation(RECORDS[1], {'北京': '110100'})
        self.assertEqual(unverified['coord_system'], None)
        self.assertEqual(unverified['precision_level'], 'unknown')

    def test_merge_creates_community_only_for_verified_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'records.json').write_text(json.dumps({'records': RECORDS}), encoding='utf-8')
            db = root / 'catalog.sqlite3'
            report = P.merge(root / 'records.json', db=db, out=root)
            self.assertEqual(report['records'], 2)
            self.assertEqual(report['by_source'], {'5i5j': 1, '58': 1})
            # 5i5j has verified BD-09 -> WGS84 coords and becomes a record;
            # 58 has no coordinates so it only queues as pending location.
            self.assertEqual(report['actions'].get('new_record'), 1)
            self.assertEqual(report['actions'].get('pending_location'), 1)
            conn = catalog.connect(db)
            try:
                sources = {r['source'] for r in conn.execute('SELECT DISTINCT source FROM source_entities')}
                placed = conn.execute('SELECT COUNT(*) FROM communities').fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(sources, {'5i5j', '58'})
            self.assertEqual(placed, 1)


if __name__ == '__main__':
    unittest.main()
