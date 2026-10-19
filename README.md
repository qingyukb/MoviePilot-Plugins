# MoviePilot-Plugins

MoviePilot 第三方插件仓库。

## PT站点签到

PT 站点自动签到，支持 V2 / V3。签到走 HTTP 直连，遇验证码或 Cloudflare 改用 Browserless；凭证取「站点管理」里的 Cookie，也可填账号密码自动登录；签完读取站点「最近动向」。

主要功能：

- HTTP 直连签到（NexusPHP），默认路径 404 时自动查找签到入口
- 内置站点预设（观众、学校、憨憨、SUNNYPT、TTG、HDKylin、Piggo、AGSVPT、LuckPT 等），未覆盖的站点可写自定义规则
- 验证码 / Cloudflare / Turnstile 改用 Browserless；没写规则的站点签到失败时也会用它再试一次
- 详情页分「签到状态」「登录状态」，结果列显示奖励或失败原因
- 当天已签到成功的站点跳过，通知只统计成功与失败数量

### Browserless Token

不配置也能签普通站点。需要时：<https://browserless.io/signup/email?plan=free> 注册 → 打开 <https://browserless.io/account/> 复制 API Key → 填入插件设置，地址留 `https://production-sfo.browserless.io`。

## 馒头保活（M-Team）

馒头（M-Team）连续 40 天不登录会删号，而且只有真实登录才重置计时。插件每天用 MoviePilot
自带的浏览器在家宽 IP 上登录一次，账号密码 + TOTP 全自动；顺带读取分享率、上传下载与魔力。

主要功能：

- 真实浏览器登录保活（不靠 API ping），支持 TOTP 二次验证
- 登录态持久化：保存 localStorage 下次复用，减少登录次数与异地风控
- 用 API 的「最近动向」二次确认，避免「看着登录成功其实没登录」
- API Key 自动取自「站点管理」里 m-team 站点的「令牌（API Key）」
- 详情页显示分享率 / 魔力 / 上传下载与最近 10 次保活记录，分享率过低会提醒

配置项与常见问题见 [插件 README](plugins.v2/mteamkeepalive/README.md)。

## 安装

插件市场添加 `https://github.com/qingyukb/MoviePilot-Plugins`，刷新后搜索插件名；或把插件目录复制到 `/config/plugins/` 重启。

## 目录

```
├── package.json / package.v2.json / package.v3.json    市场清单
├── icons/                                             市场图标
├── plugins.v2/<插件>/  plugins.v3/<插件>/               插件源码（ptsitesignin / mteamkeepalive）
└── tests/v3/<插件>/                                    测试
```

配置项、内置站点、自定义规则见 [插件 README](plugins.v2/ptsitesignin/README.md)。
