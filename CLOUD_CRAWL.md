# GitHub Actions 云端采集

采集依赖安装、HTTP 请求、JavaScript 计算、浏览器渲染、解析和校验均在 GitHub 的 Ubuntu runner 完成。使用者从 GitHub 网页启动任务和下载结果，本机无需 Python、Node.js、Playwright、Chromium 或系统 Edge。普通查看使用已部署网站。

## 启动与结果

1. 将项目及当前工作流提交到自己的 GitHub 仓库，在 **Actions** 启用工作流。手动运行入口需先存在于仓库默认分支。
2. 主看板选择 **update-data → Run workflow**。它每天北京时间 18:00 自动运行，成功校验后提交发布输入、价格和状态；失败保留旧有效数据。
3. 研究采集选择 **cloud-crawl → Run workflow**，选择 `crawler`，填写 `args`。参数是 JSON 字符串数组，例如 `["--cities","北京,上海","--pages","1"]`，不用填写 `python` 或脚本路径。
4. 等待执行结束，在 **Artifacts** 下载 `crawl-入口-运行ID-尝试次数`。`output/cloud_crawl/report.json` 包含入口、起止时间、状态和退出码；`collector.log` 保存完整采集输出。Actions Summary 也显示执行状态。
5. 默认 `resume=true`，恢复同分支、同入口上次可用的 `output/` 和研究地图数据，包含游标、冷却和请求预算。取消勾选从仓库基线开始；缓存会被 GitHub 清理，重要结果应另行下载。新分支和新入口不共享该恢复前缀。

地理编码等后续处理需要前序采集的输入时，同时填写 `input_run_id` 和 `input_artifact`（完整名称）。工作流会在恢复缓存后从本仓库指定运行导入结果，无需本机下载或上传文件。例如先运行 `anjuke_market`，再将其 Artifact 导入 `geocode` 并用 `--input` 指向 `properties_anjuke.json`。

`cloud-crawl` 提供采集成果下载，不自动提交研究数据。需要更新正式看板时运行 `update-data`，使用已有的隔离采集、证据核验、历史保留与发布流程。`update-data` 需要仓库 **Settings → Actions → General → Workflow permissions → Read and write permissions**；研究采集只需要读仓库。

## 数据入口

除表中注明必填项外，`args` 可填 `[]` 使用脚本默认范围。请求上限、间隔、冷却和解析约束沿用各来源规则。

| crawler | 用途 | args 示例或必填项 |
| --- | --- | --- |
| `dashboard` | 主看板完整更新与校验 | `[]`；通过 `update-data` 工作流持久发布 |
| `stats_gov` | 统计局 70 城月报 | `["--max-pages","2"]` |
| `income` | 官方收入、最低工资 | `["--max-pages","2"]` |
| `income_history` | 官方居民收支历史 | `[]` |
| `wage_history` | 最低工资历史网页 | `[]` |
| `wage_archive` | 已核对工资分表与附件 | `[]` |
| `macro` | LPR 与房地产宏观数据 | `[]` |
| `lpr` | 单独更新 LPR | `[]` |
| `realestate` | 单独更新房地产投资、新开工历史 | `[]` |
| `cih` | 中指公开样本 | `["--max-pages","2"]` |
| `cih_land` | 中指宅地供求渲染表 | `["--force"]` |
| `cih_rankings` | 中指公开榜单 | `[]` |
| `anjuke_prices` | 安居客价格 HTTP 采集 | `["--city","北京,上海","--from-year","2025"]` |
| `anjuke_browser` | 安居客无头浏览器采集 | `["--city","北京","--from-year","2025"]` |
| `anjuke_recheck` | 安居客历史复核 | `["--cities","北京,上海"]` |
| `anjuke_market` | 安居客挂牌行情、房源 | `["--cities","北京","--pages","1"]` |
| `anjuke_anonymous` | 无账号价格、小区目录、桌面/移动房源实采 | `[]`；默认含北京代表性入口与两段式验证处理，输出见下文 |
| `anjuke_collect` | 共享预算浏览器/HTTP 采集器 | 必填子命令，如 `["status"]`；采集示例见下文 |
| `anjuke_community` | 安居客小区目录探测 | 必填子命令与输出目录，如 `["probe","--city","北京","--out","output/cloud_anjuke"]` |
| `anjuke_probe` | 浏览器诊断探针 | `[]` 默认无头 path 4；path 3 可离线解析已有快照 |
| `crawl_public` | 公开价格来源 | `["--cities","北京","--max-requests","20"]` |
| `fang` | 房天下小区资料 | 必填子命令，如 `["crawl","--cities","北京","--max-pages","1"]` |
| `beike` | 贝壳历史 HTTP 适配器 | `["--cities","北京","--pages","1"]`；结果在 `output/beike/` |
| `communities` | 小区位置库 | `["--cities","北京","--max-cities","1"]` |
| `community_profiles` | 小区资料补充 | `["--city","北京","--max-pages","1"]` |
| `community_neighbourhoods` | 住宅片区覆盖补充 | `["--cities","北京,上海"]` |
| `hydrology` | 水系与测地面积 | `["--cities","北京","--max-cities","1"]` |
| `public_locations` | 公开地铁或许可来源 | 必填 `["metro"]` 或 `["permits","--max-pages","2"]` |
| `metro` | 地铁数据快捷入口 | `[]` |
| `permits` | 许可数据快捷入口 | `["--max-pages","2"]` |
| `nanjing_projects` | 南京项目资料 | `["--max-pages","2"]` |
| `geocode` | 公开地理编码 | 需要已有房源输入，例如 `["--input","data_processing/frontend/realestate/data/properties_anjuke.json","--limit","10"]` |
| `district_boundaries` | 城市区级边界 | `["--city","北京"]` |
| `city_regions` | 70 城行政区划 | `[]` |
| `source_pilot` | 来源样本试采 | 必填 `--combo` 或 `--url` 等，例如 `["--url","https://www.stats.gov.cn/","--budget","1"]` |
| `source_entries` | 入口与结构探测 | `["--resume"]` |
| `source_catalog` | 目录扩采 | `["--resume","--max-requests","20"]` |
| `external_candidates` | 外部候选来源 | 必填子命令，如 `["probe","--budget","20"]` |
| `site_reachability` | 网站 HTTP 可达性诊断 | `[]` |

单页浏览器采集示例（`crawler=anjuke_collect`）：

```json
["collect","--source","anjuke","--city","北京","--url","https://m.anjuke.com/bj/xiaoqu/","--url-class","catalog","--browser","chromium","--out","output/cloud_anjuke","--budget-ledger","output/cloud_anjuke/run_state/budget.json","--allow-missing-ledger","--max-requests","1"]
```

首次创建独立预算账本需明确提供 `--allow-missing-ledger`；恢复已有试验时使用原账本与原输出目录，不使用该选项重置预算。该采集器还会计入文档跳转及必要资源请求，资源预算用尽会如实标记渲染不完整。已归档的手动 Cookie 工具、旧 Scrapy 入口和本地浏览器验收脚本不由云端入口调用。

## 浏览器配置

默认使用 runner 上安装的 Playwright Chromium，不需要任何 Secret。浏览器配置目录位于 runner 临时目录，不依赖本机浏览器或个人登录资料。

如已有远程浏览器服务，可在 **Settings → Secrets and variables → Actions** 配置以下两项之一。普通云端运行不需要购买该服务。

| Secret / 变量 | 用途 | 默认 |
| --- | --- | --- |
| `CRAWLER_BROWSER_WS_ENDPOINT` | Playwright 协议浏览器 WebSocket；服务端需兼容客户端 Playwright 版本 | 空，使用 runner Chromium |
| `CRAWLER_BROWSER_CDP_ENDPOINT` | Chromium CDP 端点；只与上述 WS 配置二选一 | 空 |
| `CRAWLER_CLOUD` | 调度器和工作流设为 `1`，要求无人值守浏览器运行 | 调度器为 `1` |
| `CRAWLER_BROWSER_CHANNEL` | 工作流固定为 `chromium` | `chromium` |
| `CRAWLER_PROFILE_ROOT` | 工作流设为 runner 临时目录下的 `crawler-profiles` | 系统临时目录 |

远程连接创建新上下文，不向服务商上传个人浏览器资料。研究工作流配置远程端点后可跳过 runner Chromium 下载；发布工作流仍安装 Chromium 供静态页面验收。云端拒绝 `--headful`、系统 `--edge`、`--browser edge/chrome`、手动采集模式及探针 path 1/2。

## 失败与校验

### 无账号的实时验收

正式入口为 **cloud-crawl → crawler=anjuke_anonymous → args=[]**。它在云端 Xvfb Chromium 内自动执行，忽略续跑缓存、禁用远程浏览器端点、拒绝导入前序结果，不恢复个人 Cookie 或账号资料。当前默认采集北京年份价格页、移动小区目录和移动房源第一页；没有声称覆盖全国或全部页面。

下载 Artifact 后，`output/anjuke_anonymous/exports/` 中分别有 `prices.json`、`communities.json`、`properties.json`，只包含本轮确认解析成功的记录。`report.json` 保留运行 ID、源响应 SHA-256、挑战结果与采集状态。后续地理编码可导入该 Artifact，并把 `--input` 指向 `output/anjuke_anonymous/exports/properties.json`。

[2026-10-17 的云端运行 39025488177](https://github.com/heart06215/houseprice_calculater/actions/runs/39025488177)实际打开滑块挑战、完成图像定位与反馈拖动，验证接口返回 `verified=true`，随后移动业务页返回 60 条有效房源。同一空会话试验还获得 9 个月已完成月份价格、100 条小区目录记录。桌面房源入口仍被拦截；这证明该轮代表性匿名采集通过，不保证所有入口或长期运行都能通过。

[正式入口独立复验 39026016031](https://github.com/heart06215/houseprice_calculater/actions/runs/39026016031)通过调度器再次完成滑块验证并导出 9/100/60 条记录，`accepted=true`，无账号、无存储导入、无缓存恢复。完整防护分析和各轮失败/成功边界见 [ANJUKE_DEFENSE.md](ANJUKE_DEFENSE.md)。

2026-10-17 后续修复了桌面房源“滑块已通过仍被拦截”的两段式验证原因：网关滑块成功后站点会再跳到 `esfcommon-captcha-wy`，要求一次 `type=5` 一键智能验证。`anjuke_anonymous.py` 现在按可见元素依次处理打开验证、重复滑块、点击智能验证，并把每轮动作写入报告；默认目标加入 `beijing_properties`。行情采集 `market_crawler` 改用真实浏览器请求头，HTTP 被拦时回退到同一套匿名验证流程，且单城失败不再终止整批。`anjuke-anonymous-live` 增加北京时间每日 17:00 的定时运行。上述链路已在本机真实被拦截的 IP 上完整复现并解析出业务记录，但**仍需一次新的云端运行报告**确认 GitHub runner 上的通过率与导出数量。

`anjuke-anonymous-live` 提供相同正式入口及多个诊断场景，在全新 GitHub runner 上执行，不导入 Cookie、浏览器配置、远程浏览器、个人账号或旧采集缓存。每个浏览器目标使用空上下文；只解析本轮收到的响应。价格、小区目录和房源三类均获得有效记录时才通过，否则任务以非零退出，并保留响应快照和 `report.json` 七天供检查。

2026-10-17 的[首轮实测](https://github.com/heart06215/houseprice_calculater/actions/runs/39022424826)从公开年份页获得北京 9 个月的已完成月份价格，从移动小区目录获得 100 条目录记录；市场页和桌面房源页仍被反爬挑战阻断，整体验收失败。这些样本不证明全国、全量或长期稳定采集，也不证明已经绕过验证码。解析器测试、浏览器启动和旧数据保留均不能替代这项实时验收。

[第二轮实测](https://github.com/heart06215/houseprice_calculater/actions/runs/39022772333)扩大到移动房源、首页和房价推荐入口，8 个入口的 HTTP 与 Chromium 均进入挑战页，包括首轮能访问的入口。匿名公开入口在不同新 runner 上并不稳定，不能据首轮样本启用“稳定匿名采集”的生产承诺。有界面诊断通过云端 Xvfb 运行，仍不使用本机浏览器或账号。

网络失败、空页面、验证码与无有效记录会由各采集器记录；不会把验证页当成价格。不同来源的请求预算与熔断保持有效。云端调度报告反映脚本退出状态，某来源的具体成功、部分成功、冷却和拒绝原因需同时查看来源状态文件。宏观入口部分失败或全部失败返回非零；贝壳未取得任何有效记录返回非零并保留旧文件。

每次采集默认最多 3300 秒；超时会在 Linux runner 清理采集进程组，包括派生的 Node 和 Chromium，返回 124。失败后仍尝试保存缓存与 Artifact；GitHub 强制取消或 runner 异常中断时不保证保存。Artifact 保留 14 天，私有快照目录与临时发布工作区不打包。

`validate-crawlers` 在 push、pull request 和手动运行时安装全新运行环境，检查真实 Chromium DOM，并运行入口、浏览器生命周期和主要来源解析回归。它不对外部房产网站做批量抓取。网站可能限制云端 IP 或调整接口，运行环境可用不代表每个来源始终可达；查看具体运行证据判断。
