# PT站点签到

MoviePilot 的 PT 站点自动签到插件，支持 V2 与 V3。

签到默认走 HTTP 直连；站点带验证码或 Cloudflare 时改用 Browserless 浏览器完成。凭证取自 MoviePilot「站点管理」，也可在站点附加信息里填写账号密码，由插件在签到前自动登录换取新 Cookie。签到结束后读取站点「最近动向」，写入详情页的登录状态表。

## 安装

- 插件市场：在 MoviePilot 的「插件市场」中添加 `https://github.com/qingyukb/MoviePilot-Plugins`，刷新后搜索「PT站点签到」。
- 手动：把 `plugins.v2/ptsitesignin`（或 `plugins.v3/ptsitesignin`）复制到 MoviePilot 的 `/config/plugins/` 下，重启后启用。

## 主要功能

- 按 NexusPHP 协议 HTTP 直连签到，默认请求 `/attendance.php`
- 默认路径返回 404 时，自动在首页查找签到入口，再尝试常见签到路径
- 内置站点预设，按域名匹配；未覆盖的站点可写自定义规则
- 遇验证码、Cloudflare、Turnstile 时改用 Browserless 完成，未写规则的站点自动回退一次
- 使用站点管理里的 Cookie 签到；配置账号密码时先登录再签到，并复用新 Cookie
- 签到后读取站点「最近动向」，同时保留站点自带的相对时间
- 详情页分「签到状态」「登录状态」两张表，结果列显示实际获得的奖励或失败原因
- 当天已签到成功的站点跳过，打开「清理本日缓存」可强制重跑
- 通知只统计签到成功、登录成功、失败三项，有失败时列出站点与原因
- 支持 Cron 定时、并发执行、失败重试

## 配置项

| 选项 | 说明 | 默认 |
| --- | --- | --- |
| 启用插件 | 插件总开关 | 关 |
| 发送通知 | 只报数量，有失败时附原因 | 开 |
| 保存后立即执行一次 | 保存后延迟 3 秒执行一轮，已签到的站点也会执行 | 关 |
| 清理本日缓存 | 清空当天记录，下一轮重新签到，执行后自动关闭 | 关 |
| 执行周期 | 5 位 Cron 表达式 | `25 8 * * *` |
| 并发数 | 同时执行的站点数 | 3 |
| 失败重试 | 重试次数与间隔（分钟） | 2 / 5 |
| HTTP 超时 | 单次请求超时（秒） | 30 |
| 读取「最近动向」 | 打开用户页或签到页，把动向写入登录状态表 | 开 |
| 签到站点 / 登录站点 | 支持「全部站点」；同时选中时登录、签到、读取动向一轮完成 | 空 |
| Browserless 地址 / Token / 超时 | 处理验证码用 | 空 / 空 / 120 |
| 自定义站点规则 JSON | 内置预设未覆盖的站点规则 | 空 |

## 内置站点

| 站点 | 处理方式 |
| --- | --- |
| 观众（Audiences） | Browserless 完成 Turnstile 后签到 |
| 红豆饭、PT时间、皇后等 NexusPHP 站点 | HTTP POST 签到接口 |
| AGSVPT | POST `/attendance.php`，点击页面「立即签到」 |
| 学校（BTSCHOOL） | 提交首页「每日签到」按钮，入口消失即视为完成 |
| 憨憨（hhanclub） | 日历页点击「签到」，按钮变为「已领取」即视为完成 |
| 其他 NexusPHP 站点 | 默认 `/attendance.php`，404 时自动发现入口 |

签到成功后提取奖励文案写入结果列，例如「获得 20 粒爆米花」「本次签到获得405个憨豆」，数值每次从页面读取。

## 结果说明

- **签到状态**：站点 / 状态 / 结果。结果列是实际获得的奖励，失败时为失败原因。
- **登录状态**：站点 / 状态 / 结果。结果列是站点「最近动向」原文，形如 `2026-10-17 10:58:16 ( < 1分前 )`。
- **通知**：一行「签到成功 N｜登录成功 N｜失败 N」，有失败时逐条列出「站点：原因」。

## 自定义站点规则

内置预设未覆盖时，在「自定义站点规则 JSON」中按域名配置：

```json
{
  "pt.example.com": {
    "engine": "http",
    "method": "POST",
    "path": "/attendance.php",
    "success": "签到成功|已签到",
    "bonus": "(获得\\s*[\\d,]+\\s*粒爆米花)"
  }
}
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `engine` | `http`（默认）或 `browserless` |
| `method` / `path` | 请求方式与签到路径 |
| `submit_text` | 浏览器引擎点击的按钮文字 |
| `success` / `already` | 成功 / 已签到的判定正则 |
| `success_keywords` / `already_keywords` | 关键字判定，用于浏览器引擎 |
| `absent_keywords` | 页面中这些文字消失时视为完成 |
| `success_on_click` | 点击到按钮即视为成功 |
| `bonus` | 奖励文案正则，取第一个捕获组 |
| `activity_path` | 「最近动向」所在页面 |

抓取规则：登录后打开签到页，在浏览器控制台执行 `tools/collect-rule-snippet.js`，按输出填写。

## Browserless Token

Browserless 用于处理验证码与 Cloudflare，不配置也能签普通站点。

1. 在 <https://browserless.io/signup/email?plan=free> 注册免费账号；
2. 登录后打开 <https://browserless.io/account/>，在 **API Key** 一栏点击 **Copy** 复制；
3. 需要多个 Key 时点击 **Manage API keys** → **Create API key**，生成的 Key 只显示一次；
4. 把 Key 填入插件设置的 **Browserless Token**，地址保持 `https://production-sfo.browserless.io`。

地址可选 `production-ams`、`production-lon`，本机实测 `production-sfo` 响应最快。免费额度按月计算，只有浏览器请求才消耗。

## 常见问题

**签到返回 HTTP 404**：该站签到路径不是 `/attendance.php`，插件会自动查找入口；仍失败时在规则中用 `path` 指定实际路径。

**提示 Cookie 无效**：重新从浏览器复制 Cookie。勾在「登录站点」的站点可填写账号密码由插件自动登录，带验证码的站点只能手动填写。

**停留在 Cloudflare 或验证码页面**：填写 Browserless 地址与 Token 后重试。

**「最近动向」显示未读取到**：提示中会列出尝试过的页面。确认页面后，在站点管理的附加信息里写 `{"activity_path": "/userdetails.php?id=你的uid"}`。

**需要重新签到**：打开「清理本日缓存」保存一次，下一轮全部重新签到；也可打开「保存后立即执行一次」。

## 日志

签到、登录、读取最近动向的过程均写入日志，代理与 Cookie 中的敏感内容已打码。
