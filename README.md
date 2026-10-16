# 中国城市房价信息平台

70 城房价、官方住宅指数、收入与最低工资信息平台。二手住宅价格序列使用已核验的安居客观测；缺失月份沿国家统计局二手住宅环比指数推导，自 2021-04 起逐月展示。观测值、推导值和缺失值分别标注。

## 功能

- **市场概览**：70 城价格排行、指定月份来源覆盖、涨跌与回撤、收入和最低工资历史，以及统计局开发投资、新开工面积的并列折线图。
- **城市分析**：单城均价走势、官方新房/二手房指数对照、面积分档、房价收入比（90㎡口径）。
- **房贷计算**：到手工资、公积金、还贷后现金余量测算。
- **中指数据**：新房、二手住宅、租金与地价独立样本表，保留各自时期和单位，展示公开榜单与宅地供求。
- **统计局月报**：70 城新房、二手住宅及面积分档，支持月份、类别与城市筛选，附原文链接。

项目使用原生 JS、统一主题变量及独立数据口径。模块职责见 [架构说明](ARCHITECTURE.md)，发布检查见 [release_audit.md](data_processing/release_audit.md)。小区地图代码仅供本地研究，发布入口与构建产物均不包含该功能。

## 目录结构

```
data_processing/
├── pipeline.py              # 数据统一管道（爬取→逆推→归一化→校验）
├── stats_gov_crawler.py     # 国家统计局 70 城环比爬虫
├── realestate_crawler.py    # 全国房地产累计历史与原文核验
├── realestate_sources.json  # 旧报告的官方档案链接；新报告从发布目录发现
├── anjuke_province_crawler.py
├── inverse_price_engine.py  # 环比逆推引擎（2021-04 基准）
├── update_data.py           # 更新调度、状态与校验流程
├── update_sources.py        # 来源任务与独立失败策略
├── update_workspace.py      # 隔离工作区、发布清单与回滚
├── release_inputs.py        # 可移植输入与原始证据快照
├── published_inputs.zip     # 干净检出时恢复历史输入，逐文件哈希校验
├── build_pages.py           # 主看板静态发布构建
├── verify_frontend_data.py  # 数据完整性校验
├── config/                  # 城市坐标、价格锚点、字段契约
└── frontend/                # 前端源文件
    ├── index.html           # 主看板
    └── realestate/          # 本地研究用小区地图，不进入发布产物
output/                      # 爬虫与管道产出（stats_gov/anjuke 核心数据入库）
```

## 使用（本机无需 Python 或采集浏览器）

日常查看使用部署后的静态网站；采集、解析和数据校验全部由 GitHub Actions 的 Ubuntu runner 完成。本机无需安装 Python、Node.js、Playwright 或 Chromium。

1. 将项目上传到自己的 GitHub 仓库，在 **Actions** 中启用工作流。
2. 更新并发布主看板：选择 **update-data → Run workflow**；之后每天北京时间 18:00 自动运行。
3. 单独运行爬虫：选择 **cloud-crawl → Run workflow**，在 `crawler` 选择数据来源，`args` 填 JSON 参数数组（默认 `[]`）。
4. 在运行详情的 **Artifacts** 下载数据、原始证据和日志；失败也保留报告，勾选 `resume` 可恢复同分支同入口的缓存状态。

完整入口清单、参数示例和可选远程浏览器设置见 [云端采集说明](CLOUD_CRAWL.md)。

## 数据更新

推荐从 GitHub Actions 网页运行 `update-data`；`cloud-crawl` 可分别采集统计局、安居客、房天下、中指、收入、工资、宏观、LPR、地铁、水系和行政区划等来源。运行所需依赖在云端自动准备。

默认只更新主看板：统计局、安居客、官方收入与最低工资、中指、官方宏观指标与 LPR → 原始证据核验 → 价格重建与独立来源投影 → 历史保留检查 → 发布。采集在隔离目录运行；空响应、验证码、异常、超时和发布校验失败均保留上一版有效数据。

房地产开发投资和房屋新开工面积的走势图默认显示统计局公布的累计同比（%），直接采用官方可比口径增速，保留正负号。可切换年初累计投资（亿元）、新开工面积（万㎡）与年份；累计总量随月份累加，不能作为单月增减走势。`realestate_crawler.py` 从官方发布目录发现新报告；半年、全年报告与旧版档案分别解析，1—2月合并发布，1月不编造数据。首次补齐自2021年2月起的历史，之后只补缺期并重新核对最新一期。每期保留统计期、原始发布日期、单位和HTML哈希证明；发布前逐期重新解析原文核对。请求或解析失败保留已核验历史，翌日继续检查；不把检查月份当成报告月份。

LPR 走势图采用 2019 年 8 月新机制以来的完整月度序列，同时展示 1 年期和 5 年期以上报价（%）。来源为[中国货币网官方历史查询](https://www.chinamoney.com.cn/chinese/bklpr/)，最新一期与[人民银行公告](https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/3876551/index.html)交叉核对。首次按年度查询，后续只补新增月份；接口请求失败时可从人民银行公告补取。缺月、期限错配、来源冲突或历史改写均禁止发布，保留上一版数据及原始证据。2019 年 8 月前的旧机制只有 1 年期报价，不拼入双期限图。

云端先运行 `release_inputs.py` 恢复随仓库保存的输入和证据，再执行采集与完整性校验。`requirements-crawl.txt` 包含全部云端爬虫依赖；Python、Node.js 与 Chromium 都安装在 runner。`validate-crawlers` 工作流在代码变更时检查全新 runner 的 HTTP、解析器、Node 与真实 Chromium DOM，并运行来源与故障保留回归。

开发者若已有本地环境，可继续使用 `python data_processing/pipeline.py --skip-crawl` 离线重建，或运行 `启动房价看板.bat` 预览；这些开发工具不属于日常使用的前置条件。

## 部署（Cloudflare Pages）

纯静态站点，构建产物为 `dist/pages/`：

- GitHub 仓库接入 Cloudflare Pages：构建命令填 `python data_processing/build_pages.py`，输出目录填 `dist/pages`，项目根目录留空；
- `.github/workflows/update-data.yml` 在北京时间每天 18:00 更新，也支持手动运行；依赖与浏览器在 GitHub runner 安装。仓库需允许 Actions 写入内容，推送触发 Pages 重新发布；本机不注册定时任务；
- 工作流先执行各数据来源、故障保留和前端映射回归，再从干净目录恢复快照、离线重建并核对历史一致性。采集全部失败或超时时，仅保存安居客轮转进度和失败状态，已验证的价格、输入及证据保留；下一次 runner 继续轮转；
- `frontend/_headers` 配置了数据 JSON 短缓存与静态资源长缓存。

Cloudflare 只托管静态文件；采集在 GitHub Actions 完成。外部网站改版或验证挑战仍可能暂停采集，状态面板和 Actions 日志会记录失败。GitHub 定时任务可能延迟；公共仓库连续 60 天无活动会自动停用定时工作流，需检查仓库 Actions 状态。

安居客记录为挂牌市场观测，并非成交价；缺失月份为指数推导值。统计局指数不作为元/㎡单价，中指样本不替换安居客锚点。不同口径仅独立对照，结果供研究参考。
