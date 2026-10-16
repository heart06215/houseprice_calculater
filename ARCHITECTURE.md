---
description: 房价平台的数据流、更新模块、发布边界与验证入口。
keywords: 数据管线,来源核验,隔离更新,静态发布
---

# 项目架构

平台由 Python 数据处理程序和原生 JavaScript 静态看板组成。数据采集在 GitHub Actions 中运行；Cloudflare Pages 托管构建后的 HTML、资源和 JSON。

## 数据流

```text
来源采集器 → output/ 原始记录与证据
          → 来源校验、价格逆推、独立数据投影
          → frontend/ JSON + 已发布输入快照
          → build_pages.py → dist/pages/
```

`pipeline.py` 是命令入口。默认调用主看板隔离更新；`--skip-crawl` 根据已有输入离线重建。`update_policy.json` 配置各来源的更新周期和采集参数。

## 更新模块

| 模块 | 职责 |
| --- | --- |
| `update_data.py` | 来源到期判定、子进程调度、工作状态、构建校验与发布流程；保留既有命令入口。 |
| `update_sources.py` | 注册来源任务，调用各采集器，返回独立的来源状态、时期和失败说明；接收项目根目录和脚本执行函数。 |
| `update_workspace.py` | 单写者锁、隔离输入复制、SQLite 一致性备份、证据保留、发布清单、逐文件原子替换、失败回滚和工作区清理。 |
| `release_inputs.py` | 已发布输入及原始证据的可移植快照，逐文件哈希校验；失败时仅保存安居客轮转进度。 |
| `release_contract.py` | 主看板口径、来源证据、历史价格与记录覆盖的发布校验。 |
| `realestate_crawler.py` | 统计局开发投资、新开工面积等逐期累计数据；目录增量发现、旧档案回填、单位与统计期解析、逐期原文核验。 |
| `build_pages.py` | 静态文件选择、JSON 紧凑输出、缓存规则与发布目录检查。 |

更新过程在 `output/update_work/` 中采集和构建。验证成功后发布去重后的文件清单，并最后写入更新状态；发布中途失败则恢复已触及文件，移除此次新增文件。采集失败、验证码、空响应或超时保留上一版有效数据和来源时期。

采集进度与观测事实分别保存。失败任务可以推进轮转游标，但不能把失败结果写成新价格，也不能用未通过核验的输入替换已发布快照。

## 数据契约

- 价格序列为二手住宅。可追溯的安居客挂牌观测作为锚点；缺失月份按国家统计局二手住宅环比指数推导，基准月为 2021-04。缺少可信输入的价格保持空值。
- 官方住宅指数、中指价格样本、官方收支、最低工资和 LPR 分别保留单位、时期及来源。指数不能作为元/㎡单价，中指样本不能替换安居客锚点。
- `information.macro.realestate.history` 为全国房地产年初累计历史；投资单位亿元，新开工面积单位万㎡，包含住宅等全部房屋。各期HTML证明进入输入快照，最新摘要必须等于历史最后一期。图表默认映射 `items[].yoy` 为官方累计同比（%），可切换 `items[].value` 查看累计总量；年份筛选后的图表摘要对应所选范围的最后一期。1—2月合并发布，同比时间轴不生成单独1月点，12月直接连接次年1—2月。实际缺期数值保持为空，以红色直线标注前后有效端点；连续缺期合为一段，无两端值时不外推。正常累计总量跨年重置不连接，单独1月不标缺失。
- `price_provenance.py` 核对观测与证据，`validate_index_sources.py` 校验官方月报，`inverse_price_engine.py` 重建价格，`information_hub.py` 汇总独立来源投影。`market_evidence.py` 将分类型挂牌报价与原始页面、城市、房型、月份及图表价格逐项匹配；收入和最低工资也在发布前从原始证据重新解析核对。
- 新输入须通过历史保留检查；缺月、来源冲突或历史覆盖缩减阻止发布。

## 前端与发布

`data_processing/frontend/assets/js/dashboard/` 负责看板功能，`shared/` 提供共用图表主题。浏览器读取发布 JSON，无框架构建步骤。主题变量集中在 `assets/css/visual-theme.css`；页面使用本地 vendor 资源。

`build_pages.py` 从 HTML 入口递归收集 CSS 引用、字体、图片和看板脚本中的 `assets/` 资源路径，仅复制所需文件；字体与第三方许可说明随包保留。未引用的 TTF 原始字体、Leaflet 地图库和资源制作清单不进入发布包。新增通过动态拼接路径加载的资源时，应同时补充构建收集规则和浏览器校验。

`realestate/` 和相关小区采集工具用于本地研究，主看板发布包不包含该目录。业务原始资料和 SQLite 库保留在本地 `output/`；Git 仅版本化 `output/stats_gov/`、`output/anjuke/` 的核心数据及经过验证的输入快照。

`.github/workflows/update-data.yml` 每天北京时间 18:00（UTC 10:00）触发更新，也支持手动执行。有效数据提交后，Cloudflare Pages 的 Git 集成重新构建。调度在 GitHub 中生效，无需本机定时任务；GitHub 调度可能延迟。

## 验证入口

```powershell
python -m unittest discover -s data_processing -p 'test_*.py'
python data_processing/verify_frontend_data.py
python data_processing/verify_release_checkout.py
python data_processing/build_pages.py
```

`verify_release_checkout.py` 从仓库文件和输入快照恢复干净目录，离线重建并比对价格、中指、收入、工资和 LPR 历史。更新故障及发布回滚由 `test_update_data.py`、`test_release_safety.py` 覆盖；前端字段映射由各 `test_*.cjs` 校验。

## 历史存储与空间管理

当前前端、SQLite 主库、已发布输入包和仍被数据引用的原始证据保持原路径与原始精度。历史项目快照写入 `output/warehouse/history.sqlite3`：按64 KiB分块、SHA-256去重、zlib无损压缩；时间戳、坐标和所有原始字节均保留，旧 manifest 中的内容哈希不变。`data_store.read_snapshot()` 同时读取旧散文件与压缩历史。

`python data_processing/compact_storage.py` 在更新互斥锁内归档未引用的清洗快照和失效临时副本，每个文件完成压缩、回读哈希验证后才删除原副本。开发副本的源码、当前输入及其引用证据继续保留，历史快照共享主仓库的去重存储。旧临时目录的内容也可按原路径恢复：`python data_processing/compact_storage.py --restore output/相对路径`。恢复拒绝路径越界与覆盖更新文件。

每日更新创建工作区标记，自动归档并清理超过24小时的异常残留；历史仓库不再整库复制或硬链接到可写工作区，新生成的历史在清理前合并回主仓库。干净检出验证默认结束即清理临时目录，日志与报告保留；仅显式传入 `--keep-checkout` 时保留检出。

目录存在读取权限错误时，保留原目录并在 `output/storage-maintenance/report.json` 的 `skipped` 中记录原因，继续处理可访问的副本。空间统计同时列出不可读取路径；归档内容不完整或哈希失败时拒绝删除。`python data_processing/verify_snapshot_archive.py` 核验全部归档对象、历史清单以及本次精简前记录的现用文件哈希。

`python data_processing/compact_storage.py --generated` 可单独归档 `GENERATED_OUTPUTS` 清单中的4组历史预览及2个单文件 HTML 产物。当前页面、采集输入和原始证据不在该清单内；每个目录全部归档并再次核对原文件哈希后才移除散文件，结果写入 `output/storage-maintenance/generated-report.json`。归档保存在本地忽略的历史库中，需要随历史库一起备份。

需要重访旧预览或运行依赖这些产物的研究测试时，先按原路径恢复整个目录，例如 `python data_processing/compact_storage.py --restore-tree output/source_trial_20260922/preview`。单文件使用 `--restore data_processing/standalone_dashboard.html` 恢复历史版本，或运行 `python data_processing/build_standalone.py` 生成当前版本；地图单文件对应 `python data_processing/frontend/realestate/build_inline.py`。恢复不会覆盖已经修改的文件。
