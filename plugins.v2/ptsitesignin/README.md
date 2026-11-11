# PT站点签到

MoviePilot 的 PT 站点自动签到插件，支持 V2 / V3。

签到默认走 HTTP 直连，支持 Cookie 凭证；验证码、Cloudflare / Turnstile 等场景可交给本机 Playwright 或 Browserless 处理。签到后会读取站点「最近动向」，写入登录状态表。

## 功能概览

- 自动识别内置站点，按站点规则完成签到与登录验证
- HTTP 直连优先，尽量减少 Browserless 额度消耗
- 支持 Cron 定时、并发执行和失败重试
- 当天已签到成功的站点自动跳过
- 详情页分别展示签到状态和登录状态
- 通知仅统计成功与失败，失败时附带简要原因
- Piggo 使用 MoviePilot 本机 Playwright 打开 `/calendar`，点击签到并处理 Cloudflare 人机验证，不回退云端 Browserless
- 支持自定义站点规则和站点专用模块

## 快速开始

1. 在 MoviePilot 插件市场添加本仓库。
2. 搜索并安装「PT站点签到」。
3. 在站点管理中配置 Cookie。
4. 勾选需要签到 / 登录的站点。
5. 设置执行周期，保存并启用插件。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [使用与配置](docs/usage.md) | 安装、配置项、内置站点和图标说明 |
| [自定义站点规则](docs/site-rules.md) | 规则字段、示例、答题签到和站点模块 |
| [Browserless](docs/browserless.md) | 地址、Token、私有部署和额度轮换 |
| [常见问题](docs/faq.md) | 常见故障和排查方法 |

---

如果内置规则已经覆盖你的站点，通常只需要配置 Cookie，无需编写额外规则。

[查看完整使用说明](docs/usage.md)
