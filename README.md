# MoviePilot-Plugins

个人维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件仓库。

## 插件列表

### [PT站点签到](plugins.v2/ptsitesignin)

双引擎 PT 自动签到，支持 MoviePilot V2 与 V3。

| 引擎 | 原理 | 适用场景 |
|---|---|---|
| **HTTP 直连** | `requests` 请求签到接口（NexusPHP `attendance.php`） | 普通站点、无验证码的签到 |
| **Browserless** | BrowserQL 驱动真实浏览器，自动解题 / 点击 / 提交 | 图片验证码、Cloudflare、Turnstile 点击打勾、弹窗签到、Altcha |

默认全局走 HTTP（免费、毫秒级），可在「自定义站点规则 JSON」中用 `engine` 字段为单个站点单独切换到 Browserless。

- V2：[`plugins.v2/ptsitesignin`](plugins.v2/ptsitesignin)
- V3：[`plugins.v3/ptsitesignin`](plugins.v3/ptsitesignin)

主要功能：

- 复用 MoviePilot 已保存的站点 Cookie 与 UA，无需重复填账号
- 内置 Audiences、OpenCD、包子、慕雪阁、OshenPT、LuckPT、YemaPT、HDSky、dstudio.me、mua.xloli.cc、share.ilolicon.com 的签到预设，按域名自动匹配
- Cron 定时执行、保存后立即执行一次、并发控制、失败重试
- 插件详情页统计表（站点 / 方式 / 状态 / 时间 / 结果，含实际奖励如爆米花 / 魔力值）+ 通知模式三档（每次执行后 / 仅失败时 / 不通知）
- Cookie 与 Token 不写入日志，签到结果按天留存

## 安装

### 通过插件市场（推荐）

在 MoviePilot 中添加本仓库为插件市场地址：

```
https://github.com/qingyukb/MoviePilot-Plugins
```

V2 读取根目录 `package.v2.json`，V3 读取 `package.v3.json`。

### 手动安装

把插件目录复制到 MoviePilot 插件目录下（通常为 `/config/plugins/`）：

```bash
# V2
git clone --depth 1 https://github.com/qingyukb/MoviePilot-Plugins.git /tmp/mp-plugins
cp -r /tmp/mp-plugins/plugins.v2/ptsitesignin /config/plugins/

# V3
cp -r /tmp/mp-plugins/plugins.v3/ptsitesignin /config/plugins/
```

重启 MoviePilot 后在插件页启用。

## 仓库结构

```
.
├── package.v2.json            # V2 插件市场清单
├── package.v3.json            # V3 插件市场清单
├── icons/                     # 插件图标
├── plugins.v2/                # 兼容 MoviePilot V2 的插件
│   └── ptsitesignin/
│       ├── __init__.py        # 插件入口：配置页 / 结果页 / 调度
│       ├── core.py            # 双引擎核心逻辑（不依赖 MoviePilot）
│       ├── README.md
│       ├── signin.png
│       └── tools/
│           └── collect-rule-snippet.js   # 新站点规则采集脚本
└── plugins.v3/                # 兼容 MoviePilot V3 的插件
    └── ptsitesignin/
```

## 添加新站点规则

1. 浏览器登录目标站，打开签到页，F12 → Console 粘贴 `tools/collect-rule-snippet.js` 执行，复制输出的 JSON
2. 按输出内容编写规则（HTTP 或 Browserless），写入插件设置的「自定义站点规则 JSON」
3. 详细字段说明见 [插件 README](plugins.v2/ptsitesignin/README.md)
