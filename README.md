# MoviePilot-Plugins

MoviePilot 第三方插件仓库。

## PT站点签到

PT 站点自动签到插件，支持 MoviePilot V2 与 V3。

签到默认走 HTTP 直连；站点带验证码或 Cloudflare 时改用 Browserless 浏览器完成。站点凭证取自 MoviePilot「站点管理」，也可以在站点附加信息里填写账号密码，由插件在签到前自动登录换取新 Cookie。签到结束后读取站点「最近动向」，写入详情页的登录状态表。

### 主要功能

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

### Browserless Token

Browserless 用于处理验证码与 Cloudflare，不配置也能签普通站点。

1. 在 <https://browserless.io/signup/email?plan=free> 注册免费账号；
2. 登录后打开 <https://browserless.io/account/>，在 **API Key** 一栏点击 **Copy** 复制；
3. 需要多个 Key 时点击 **Manage API keys** → **Create API key**，生成的 Key 只显示一次；
4. 把 Key 填入插件设置的 **Browserless Token**，地址保持 `https://production-sfo.browserless.io`。

地址可选 `production-ams`、`production-lon`，本机实测 `production-sfo` 响应最快（单次请求约 0.7 秒）。免费额度按月计算，只有浏览器请求才消耗。

## 安装

在 MoviePilot 的「插件市场」中添加本仓库地址，刷新市场后搜索插件名。

```
https://github.com/qingyukb/MoviePilot-Plugins
```

也可以手动安装：把插件目录复制到 MoviePilot 的 `/config/plugins/` 下，重启后启用。

```bash
git clone --depth 1 https://github.com/qingyukb/MoviePilot-Plugins.git /tmp/mp-plugins
cp -r /tmp/mp-plugins/plugins.v2/ptsitesignin /config/plugins/   # V2
cp -r /tmp/mp-plugins/plugins.v3/ptsitesignin /config/plugins/   # V3
```

## 目录

```
.
├── package.json               # 插件市场清单
├── package.v2.json            # V2 市场清单
├── package.v3.json            # V3 市场清单
├── icons/                     # 市场图标
├── plugins.v2/ptsitesignin/   # 插件源码（V2）
├── plugins.v3/ptsitesignin/   # 插件源码（V3，与 V2 一致）
└── tests/v3/ptsitesignin/     # 测试
```

配置项、内置站点、自定义规则与常见问题见 [插件 README](plugins.v2/ptsitesignin/README.md)。

## 自定义站点规则

内置预设未覆盖的站点，登录后打开签到页，在浏览器控制台执行 `plugins.v2/ptsitesignin/tools/collect-rule-snippet.js`，将输出内容填入插件的「自定义站点规则 JSON」。
