# MoviePilot-Plugins

MoviePilot 第三方插件仓库。

## PT站点签到

PT 站点自动签到，支持 V2 / V3。签到走 HTTP 直连，遇验证码或 Cloudflare 改用 Browserless；凭证取「站点管理」里的 Cookie，也可填账号密码自动登录；签完读取站点「最近动向」。

主要功能：

- HTTP 直连签到（NexusPHP），默认路径 404 时自动查找签到入口
- 内置站点预设（观众、学校、憨憨、AGSVPT、LuckPT、HDSky 等），未覆盖的站点可写自定义规则
- 验证码 / Cloudflare / Turnstile 改用 Browserless；没写规则的站点签到失败时也会用它再试一次
- 详情页分「签到状态」「登录状态」，结果列显示奖励或失败原因
- 当天已签到成功的站点跳过，通知只统计成功与失败数量

### Browserless Token

不配置也能签普通站点。需要时：<https://browserless.io/signup/email?plan=free> 注册 → 打开 <https://browserless.io/account/> 复制 API Key → 填入插件设置，地址留 `https://production-sfo.browserless.io`。

## 安装

插件市场添加 `https://github.com/qingyukb/MoviePilot-Plugins`，刷新后搜索插件名；或把插件目录复制到 `/config/plugins/` 重启。

## 目录

```
├── package.json / package.v2.json / package.v3.json    市场清单
├── icons/                                             市场图标
├── plugins.v2/ptsitesignin/  plugins.v3/ptsitesignin/  插件源码
└── tests/v3/ptsitesignin/                             测试
```

配置项、内置站点、自定义规则见 [插件 README](plugins.v2/ptsitesignin/README.md)。
