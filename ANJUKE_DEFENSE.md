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

旧探针检测到挑战 DOM 后立即结束，可能截断异步注册和初始化。现改为最多等待 15 秒，并记录注册、校验接口的响应结果形状和状态码，不保存验证值、挑战签名或会话标识。

`anjuke-anonymous-live` 使用云端 Xvfb Chromium。先尝试正常加载，再在新的匿名上下文中模拟上述三个 SDK 加载地址不可达，最多刷新三次，让站点自己的错误处理运行。不会导入或直接篡改登录态、本地错误计数或验证回调。只有实际业务页面解析出有效记录才算成功；客户端降级提示、接口 HTTP 200 和跳转本身均不算成功。

价格、小区、房源三类全部取得本轮真实记录，整体验收才通过。试验保留 `report.json`、响应 HTML 和实际加载的防护脚本；不自动发布生产数据。
