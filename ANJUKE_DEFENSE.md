# 安居客匿名云端防护调查

调查时间：2026-10-17。目标是无需个人账号、Cookie、浏览器资料或本机运行环境的云端采集。以下区分捕获事实、静态分析和待验证假设。

## 已捕获的链路

首轮 GitHub Actions 运行 [39022424826](https://github.com/heart06215/houseprice_calculater/actions/runs/39022424826) 保存了实际 HTML 与内联脚本。受拦页面包含 `@@xxzlGatewayUrl`，内联脚本读取该节点并执行 `window.location.assign`，浏览器最终进入 `callback.58.com/antibot/verifycode`。

验证码页的内联脚本从页面获取本次匿名挑战参数，向 `/antibot/yidun/register.do` 发送请求。成功注册后调用 `initNECaptchaWithFallback`，模式为 `popup`。验证回调将 SDK 产生的 `validate` 交给 `/antibot/yidun/checkcode.do`；只有服务端返回成功后才跳回业务页面。单独改页面 DOM 或伪造前端“验证成功”不能证明服务端放行。

页面也通过 `/fwdata/load.gif` 上报页面、屏幕、语言等信息。它证明存在客户端环境上报，不足以确定服务器的 IP、TLS、行为或指纹判定规则。

## REA 静态分析

REA 分析了捕获的网关脚本、验证码内联脚本，以及该页面引用的公开资源：

- `https://j1.58cdn.com.cn/firewall/NEcaptcha.js`
- 下载字节数：10876；SHA-256：`aacd43ed626b182078a2426d1dd4413870cec2a5e1c5385dbe2d5d218f86c7a2`
- REA Evidence：`ev_56dd133527cce0e46e321240215f4c46f4273622ae8266499ea9351a170c27c7`
- 3 个 JavaScript 文件均成功解析，共 2404 个 AST 节点。分析未执行目标脚本；动态调用和服务端验证规则仍属未知。

封装脚本有明确的 SDK 错误降级分支：`loadResource` 依次尝试三个公开 SDK 静态域名；`normalizeFallbackConfig` 默认 `errorFallbackCount=3`；初始化失败通过 `proxyOnError` 累计匿名本地错误次数；达到阈值后，默认 `fallbackFn` 调用原页面的 `onVerify`。降级值由公开脚本自带，调查记录不复制其原文。

该分支只是客户端事实。服务端是否接受、是否还检查挑战时效、会话、来源或网络条件，必须实测，不能由静态分析推断成功。

## 对应云端试验

旧探针检测到挑战 DOM 后立即结束，可能截断异步注册和初始化。现改为最多等待 15 秒，接口结果仅保留响应形状、状态码等诊断字段。

`anjuke-anonymous-live` 使用云端 Xvfb Chromium。`sdk-fallback` 场景先尝试正常加载，再在新的匿名上下文中模拟上述三个 SDK 加载地址不可达，最多刷新三次，让站点自己的错误处理运行。不会导入或直接篡改登录态、本地错误计数或验证回调。只有实际业务页面解析出有效记录才算成功；客户端降级提示、接口 HTTP 200 和跳转本身均不算成功。另有 `public-mobile` 和 `verify-ui` 场景，分别检查移动入口与站点自带验证按钮。

价格、小区、房源三类全部取得本轮真实记录，整体验收才通过。试验保留 `report.json`、响应 HTML 和实际加载的防护脚本；不自动发布生产数据。

## 服务端降级实测结果

[运行 39023692328](https://github.com/heart06215/houseprice_calculater/actions/runs/39023692328)在空会话中拿到北京 9 个月房价、100 条小区目录记录，桌面房源页仍被拦截。降级场景共阻断 9 次 SDK 加载请求，三次注册均返回业务码 `0`；第三次触发站点自身 `/antibot/yidun/checkcode.do`，服务端业务码为 `1`，最终仍停留挑战页、有效房源为 0。客户端降级假设已实际触发，但没有得到业务页面访问权限。

本次云端实际加载的 `NEcaptcha.js` 与静态分析文件 SHA-256 相同。同时捕获 `load.min.js`、`ir.2.0.13.min.js`、`core-optimi.m25b40.v2.28.5.min.js`。REA 对保留的 7 份脚本均成功做语法解析，共 134662 个 AST 节点；Evidence 为 `ev_c84def879aeb57e7e0a9f8013a9485fe3c6324451c13318e4c8a8c40e3574f2e`。语义图因大小预算和动态混淆而部分覆盖，不构成完整解密或掌握服务端风控算法的证据。

[运行 39023944535](https://github.com/heart06215/houseprice_calculater/actions/runs/39023944535)沿用全新云端有界面 Chromium，移动端房源、首页和房价推荐页均进入挑战页。价格年份页、小区目录再次分别返回 9 条和 100 条；整体验收失败。

[运行 39024114462](https://github.com/heart06215/houseprice_calculater/actions/runs/39024114462)打开移动房源页的站点验证按钮，`/api/v3/get` 返回 `error=0,type=2`，实际截图为滑块拼图；没有静默放行，房源仍为 0。

`slider-once` 是后续有界试验：只在当前匿名会话可见的拼图上读取两张图片，用 [OpenCV 模板匹配](https://docs.opencv.org/4.12.0/d4/dc6/tutorial_py_template_matching.html)估计位置；通过可见滑块测量 CSS 拖动映射，由原站 SDK 生成并提交验证请求。每个目标最多提交一次拖动，最终仍按真实业务数据判断。图像库仅由 Actions 安装，不要求使用者本机配置。离线几何测试通过不代表服务器接受验证。

[运行 39024528655](https://github.com/heart06215/houseprice_calculater/actions/runs/39024528655)的移动房源页直接返回完整业务 HTML，没有触发验证码按钮或拖动。页面有 60 张实际房源卡片，旧桌面解析器将其记为 `unparsed`，因此当轮整体仍失败。按该响应适配移动卡片后，离线重放解析出 60 条，均有小区、单价和面积；这属于同一云端响应的重解析，尚不等于新的云端实采。该次访问不证明滑块算法有效，也不证明稳定绕过风控。
