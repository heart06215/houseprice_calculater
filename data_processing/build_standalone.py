#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_standalone.py — 主看板单文件构建
将 frontend/index.html 打包为零依赖单文件 standalone_dashboard.html：
  - 内联全部本地 css/js（vue/echarts 均已在 assets/vendor/ 本地化）
  - 注入 window.__HOUSING_DATA__（主数据）与 window.__CHINA_GEOJSON__（地图轮廓）
  - 前端 loader 检测到内联变量后自动跳过 fetch，双击即可离线打开
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inliner import build_single_file

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(SCRIPT_DIR, "frontend")
SRC_HTML = os.path.join(FRONTEND_DIR, "index.html")
OUT_FILE = os.path.join(SCRIPT_DIR, "standalone_dashboard.html")


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    housing_path = os.path.join(FRONTEND_DIR, "intersection_housing_data.json")
    geo_path = os.path.join(FRONTEND_DIR, "assets", "vendor", "china.geo.json")
    for p in [SRC_HTML, housing_path, geo_path]:
        if not os.path.exists(p):
            print(f"[错误] 缺少 {p}")
            return 1

    with open(housing_path, "r", encoding="utf-8") as f:
        housing = json.load(f)
    with open(geo_path, "r", encoding="utf-8") as f:
        geo = json.load(f)

    build_single_file(SRC_HTML, FRONTEND_DIR, OUT_FILE, [
        ("__HOUSING_DATA__", housing),
        ("__CHINA_GEOJSON__", geo),
        ("__UPDATE_STATUS__", json.load(open(os.path.join(FRONTEND_DIR,'update_status.json'),encoding='utf-8')) if os.path.exists(os.path.join(FRONTEND_DIR,'update_status.json')) else {}),
    ])
    print("[验证] 双击 standalone_dashboard.html 离线打开，无需服务器和网络")
    return 0


if __name__ == "__main__":
    sys.exit(main())
