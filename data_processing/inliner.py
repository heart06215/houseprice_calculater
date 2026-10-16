# -*- coding: utf-8 -*-
"""通用 HTML 资源内联器。

把 HTML 里所有本地 <script src> / <link rel=stylesheet href> 替换为内联内容，
并在 </head> 前注入数据变量。配合前端 loader 的约定：
window.__XXX_DATA__ 存在时不走 fetch，因此内联后的单文件
在 file:// 双击打开即可完整工作，无需网络。
"""

import json
import os
import re
import base64


def inline_css_fonts(content, css_path):
    """Embed local fonts and WebP backgrounds relative to their stylesheet.

    Retain the public function name for existing callers.
    """
    types = {'.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.otf': 'font/otf',
             '.webp': 'image/webp'}
    def replace(match):
        ref = match.group(2).strip()
        if ref.startswith(('data:', 'http:', 'https:', '//', '#')):
            return match.group(0)
        path = _resolve(os.path.dirname(css_path), ref)
        mime = types.get(os.path.splitext(path)[1].lower())
        if not mime:
            return match.group(0)
        with open(path, 'rb') as stream:
            encoded = base64.b64encode(stream.read()).decode('ascii')
        return 'url("data:' + mime + ';base64,' + encoded + '")'
    return re.sub(r'''url\(\s*(['"]?)([^'"\)]+)\1\s*\)''', replace, content)


def _resolve(base_dir, ref):
    path = os.path.normpath(os.path.join(base_dir, ref.split('?',1)[0].split('#',1)[0]))
    return path


def _json_safe(obj_or_str):
    r"""JSON 字符串里禁止出现 </script>（\/ 是合法 JSON 转义）。"""
    if isinstance(obj_or_str, (dict, list)):
        s = json.dumps(obj_or_str, ensure_ascii=False)
    else:
        s = obj_or_str
    return s.replace("</", "<\\/")


def inline_assets(html, base_dir):
    """内联全部本地 css/js 引用；http(s) 外链保留并告警。"""

    def _inline_script(m):
        attrs, ref = m.group(1), m.group(2)
        if ref.startswith(("http://", "https://", "//")):
            print(f"  [警告] 保留外链脚本: {ref}")
            return m.group(0)
        path = _resolve(base_dir, ref)
        if not os.path.exists(path):
            raise FileNotFoundError(f'构建缺少必要脚本: {ref}')
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        print(f"  [内联] {ref} ({len(content) // 1024}KB)")
        content=re.sub(r'</script',lambda m:'<\\/script',content,flags=re.I)
        return f"<script{attrs}>\n{content}\n</script>"

    def _inline_css(m):
        attrs, ref = m.group(1), m.group(2)
        if ref.startswith(("http://", "https://", "//")):
            print(f"  [警告] 保留外链样式: {ref}")
            return m.group(0)
        path = _resolve(base_dir, ref)
        if not os.path.exists(path):
            print(f"  [警告] 找不到样式（保留原标签）: {ref}")
            return m.group(0)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        content = inline_css_fonts(content, path)
        print(f"  [内联] {ref} ({len(content) // 1024}KB)")
        return f"<style{attrs}>\n{content}\n</style>"

    html = re.sub(r"<script([^>]*)\ssrc=[\"']([^\"']+)[\"']([^>]*)>\s*</script>",
                  _inline_script, html)
    html = re.sub(r"<link([^>]*)\shref=[\"']([^\"']+\.css(?:\?[^\"']*)?)[\"']([^>]*)>",
                  _inline_css, html)
    return html


def inject_vars(html, var_name, payload):
    """在 </head> 前注入 window.<var> = <json>。"""
    snippet = f"\n<script>\n// ===== 内联数据（构建时注入）=====\nwindow.{var_name} = {_json_safe(payload)};\n</script>\n"
    if "</head>" not in html:
        raise ValueError("HTML 缺少 </head>，无法注入数据")
    return html.replace("</head>", snippet + "</head>", 1)


def build_single_file(src_html, base_dir, out_file, data_vars):
    """完整构建流程。data_vars: [(变量名, dict/str), ...]"""
    with open(src_html, "r", encoding="utf-8") as f:
        html = f.read()
    print(f"[源文件] {src_html}")
    html = inline_assets(html, base_dir)
    for name, payload in data_vars:
        size = len(payload) if isinstance(payload, str) else len(json.dumps(payload, ensure_ascii=False))
        print(f"  [注入] window.{name} ({size // 1024}KB)")
        html = inject_vars(html, name, payload)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[完成] {out_file} ({os.path.getsize(out_file) // 1024}KB)")
