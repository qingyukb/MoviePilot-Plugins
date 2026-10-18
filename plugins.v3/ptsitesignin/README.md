# PT站点签到

MoviePilot 的 PT 站点自动签到插件，支持 V2 / V3。

签到默认走 HTTP 直连，站点带验证码或 Cloudflare 时改用 Browserless 完成。凭证取自「站点管理」，也可在站点附加信息里填账号密码，由插件在签到前自动登录换新 Cookie。签到后读取站点「最近动向」，写入登录状态表。

## 安装

市场添加 `https://github.com/qingyukb/MoviePilot-Plugins` 后搜索「PT站点签到」；或把 `plugins.v2/ptsitesignin` 复制到 `/config/plugins/` 重启。

## 主要功能

- HTTP 直连签到，默认 `/attendance.php`，404 时自动查找首页签到入口
- 内置站点预设（观众、学校、憨憨、SUNNYPT、TTG、HDKylin、Piggo、AGSVPT、LuckPT 等）按域名匹配，未覆盖的站点写自定义规则
- 验证码 / Cloudflare / Turnstile 改用 Browserless；没写规则的站点签到失败时也会用它再试一次
- 签到后读取「最近动向」，包含站点自带的相对时间
- 详情页分「签到状态」「登录状态」，结果列显示奖励或失败原因
- 当天已签到成功的站点跳过；通知只统计签到成功 / 登录成功 / 失败
- 支持 Cron 定时、并发、失败重试

## 配置项

| 选项 | 说明 |
| --- | --- |
| 启用插件 / 发送通知 | 总开关；通知只报数量，失败时附原因 |
| 保存后立即执行一次 / 清理本日缓存 | 立即跑一轮；清空当天记录，下一轮重签 |
| 执行周期 / 并发数 / 失败重试 / HTTP 超时 | `25 8 * * *` / 3 / 2 次 5 分钟 / 30 秒 |
| 读取「最近动向」 | 把动向写入登录状态表 |
| 签到站点 / 登录站点 | 支持「全部站点」，同时选中时一轮做完 |
| Browserless 地址 / Token | 处理验证码用 |
| 自定义站点规则 JSON | 未覆盖的站点规则 |

## 内置站点

观众（Turnstile）、AGSVPT（`POST /attendance.php`）、学校（首页「每日签到」）、憨憨（日历「签到」）、红豆饭 / PT时间 / 皇后等 NexusPHP 站点，其余站点走默认路径。奖励文案（魔力值、爆米花、憨豆）每次从页面读取。

## 自定义站点规则

```json
{"pt.example.com": {"engine": "http", "method": "POST", "path": "/attendance.php",
  "success": "签到成功|已签到", "bonus": "(获得\\s*[\\d,]+\\s*粒爆米花)"}}
```

常用字段：`engine`（http / browserless）、`method`、`path`、`submit_text`、`success`、`already`、`absent_keywords`、`success_on_click`、`bonus`、`activity_path`。抓规则用 `tools/collect-rule-snippet.js`。

## Browserless Token

1. <https://browserless.io/signup/email?plan=free> 注册免费账号
2. 打开 <https://browserless.io/account/> 复制 API Key
3. 填入插件设置，地址留 `https://production-sfo.browserless.io`

地址也可用 `production-ams`、`production-lon`；免费额度只有浏览器请求才消耗。

## 常见问题

- **签到返回 404**：站点签到路径不是 `/attendance.php`，用规则里的 `path` 指定
- **Cookie 无效**：重新复制 Cookie，或在「登录站点」里填账号密码
- **卡在 Cloudflare**：填写 Browserless 地址与 Token
- **最近动向未读取到**：提示里会列出试过的页面，可用 `activity_path` 指定
- **想重新签到**：打开「清理本日缓存」保存一次
