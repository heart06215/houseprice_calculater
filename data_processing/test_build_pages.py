"""Static publishing keeps referenced assets and excludes preparation files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_pages


class BuildPagesTests(unittest.TestCase):
    def test_build_follows_assets_preserves_licenses_and_json_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            front = root / 'data_processing/frontend'
            source = {
                'index.html': '<link href="assets/css/main.css?v=1"><script src="assets/js/dashboard/app.js"></script>',
                'assets/css/main.css': '@import "nested.css"; body{background:url(../images/paper.png)}',
                'assets/css/nested.css': '@font-face{src:url("../fonts/body.woff2?#iefix")} .x{background:url(data:image/png;base64,abcd)}',
                'assets/images/paper.png': 'image',
                'assets/fonts/body.woff2': 'font',
                'assets/fonts/Body-OFL.txt': 'license',
                'assets/fonts/body.ttf': 'unused original',
                'assets/vendor/leaflet.js': 'unused map library',
                'assets/vendor/china.geo.json': '{}',
                'assets/js/dashboard/app.js': "fetch('assets/vendor/china.geo.json')",
                'assets/js/dashboard/components.js': '',
                'assets/js/dashboard/city_analysis.js': '',
                'intersection_housing_data.json': '{"city": "上海", "price": 12345.6789, "missing": null}',
                'update_status.json': '{}',
                '_headers': '/*\n  X-Content-Type-Options: nosniff',
            }
            for name, content in source.items():
                path = front / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
            dest = root / 'dist/pages'
            dest.mkdir(parents=True)
            (dest / 'obsolete.html').write_text('stale')
            with patch.multiple(build_pages, ROOT=root, FRONT=front, DEST=dest):
                build_pages.build()
            for name in ('assets/css/nested.css', 'assets/fonts/body.woff2', 'assets/images/paper.png',
                         'assets/fonts/Body-OFL.txt', 'assets/vendor/china.geo.json'):
                self.assertEqual((dest / name).read_bytes(), (front / name).read_bytes())
            for name in ('assets/fonts/body.ttf', 'assets/vendor/leaflet.js', 'obsolete.html'):
                self.assertFalse((dest / name).exists())
            self.assertEqual(json.loads((dest / 'intersection_housing_data.json').read_text(encoding='utf-8')),
                             json.loads(source['intersection_housing_data.json']))

    def test_missing_css_asset_fails_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            front = Path(temporary)
            (front / 'style.css').write_text('body{background:url(missing.png)}')
            with self.assertRaisesRegex(ValueError, 'Missing static asset'):
                build_pages.static_assets(front, '<link href="style.css">')

    def test_external_urls_are_ignored_but_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            front = Path(temporary)
            self.assertEqual(build_pages.static_assets(front,
                '<a href="#panel">x</a><script src="https://example.com/app.js"></script>'), set())
            with self.assertRaisesRegex(ValueError, 'outside frontend'):
                build_pages.static_assets(front, '<script src="../outside.js"></script>')


if __name__ == '__main__':
    unittest.main()
