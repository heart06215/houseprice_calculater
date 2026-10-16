"""Build the static Cloudflare Pages directory, excluding the local map application."""
import re
import json
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / 'data_processing/frontend'
DEST = ROOT / 'dist/pages'


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for key, value in attrs if key in ('src', 'href') and value)


def static_assets(front, html):
    """Follow HTML/CSS references and local asset URLs used by dashboard scripts."""
    front = front.resolve()
    parser = AssetLinks()
    parser.feed(html)
    pending = [(front, link) for link in parser.links]
    selected = set()
    while pending:
        base, link = pending.pop()
        url = urlsplit(link)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (front / unquote(url.path).lstrip('/') if url.path.startswith('/')
                else base / unquote(url.path)).resolve()
        if front not in path.parents:
            raise ValueError('Static asset outside frontend: ' + link)
        if not path.is_file():
            raise ValueError('Missing static asset: ' + link)
        relative = path.relative_to(front)
        if relative in selected:
            continue
        selected.add(relative)
        if path.suffix == '.css':
            css = path.read_text(encoding='utf-8')
            css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
            pending.extend((path.parent, match[1].strip()) for match in
                           re.findall(r'url\(\s*([\'"]?)(.*?)\1\s*\)', css))
            pending.extend((path.parent, link) for link in
                           re.findall(r'@import\s+[\'"]([^\'"]+)[\'"]', css))
        elif path.suffix == '.js' and relative.parts[:2] == ('assets', 'js'):
            pending.extend((front, link) for link in
                           re.findall(r'[\'"](assets/[^\'"\s]+)[\'"]', path.read_text(encoding='utf-8')))
    # Font and third-party attribution must accompany the deployed assets.
    for folder in ('fonts', 'vendor'):
        for path in (front / 'assets' / folder).rglob('*'):
            if path.is_file() and (path.suffix.lower() in ('.txt', '.md') or 'license' in path.name.lower()):
                selected.add(path.relative_to(front))
    return selected


def build():
    html = (FRONT / 'index.html').read_text(encoding='utf-8')
    assets = static_assets(FRONT, html)
    for name in ('app.js', 'components.js', 'city_analysis.js'):
        text = (FRONT / 'assets/js/dashboard' / name).read_text(encoding='utf-8')
        if any(token in text for token in ('propsmap', 'openStreetMap', 'realestate/map.html')):
            raise ValueError('Map entry still included: ' + name)
    if DEST.resolve() != (ROOT / 'dist/pages').resolve() or ROOT.resolve() not in DEST.resolve().parents:
        raise ValueError('Invalid Pages output directory')
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    for relative in sorted(assets):
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FRONT / relative, target)
    for name in ('index.html', 'intersection_housing_data.json', 'update_status.json', '_headers'):
        if name.endswith('.json'):
            payload=json.loads((FRONT/name).read_text(encoding='utf-8'))
            (DEST/name).write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')),encoding='utf-8')
        else:
            shutil.copy2(FRONT / name, DEST / name)
    if (DEST / 'realestate').exists():
        raise ValueError('Map application leaked into Pages output')
    files=[p for p in DEST.rglob('*') if p.is_file()]
    if len(files)>20000 or any(p.stat().st_size>25*1024*1024 for p in files):
        raise ValueError('Static output exceeds Cloudflare Pages file limits')
    print('Cloudflare Pages:', DEST)
    print(f'{len(files)} files, {sum(p.stat().st_size for p in files) / 1024**2:.2f} MiB')
    return DEST


if __name__ == '__main__':
    build()
