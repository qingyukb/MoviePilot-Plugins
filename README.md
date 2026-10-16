# MoviePilot-Plugins

个人维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件仓库。

## 插件列表

### [PT站点签到](plugins.v2/ptsitesignin)

双引擎 PT 自动签到，登录凭证复用 MoviePilot「站点管理」，并在签到后读取「最近动向」，支持 MoviePilot V2 与 V3。

| 引擎 | 原理 | 适用场景 |
|---|---|---|
| **HTTP 直连** | `requests` 请求签到接口（NexusPHP `attendance.php`） | 普通站点、无验证码的签到 |
| **Browserless** | BrowserQL 驱动真实浏览器，自动解题 / 点击 / 提交 | 图片验证码、Cloudflare、Turnstile 自动解题、弹窗签到、Altcha |

默认走 HTTP 直连（免费、毫秒级）。需要图片验证码 / Cloudflare / Turnstile 的站点由内置预设自动切到 Browserless，也可在「自定义站点规则 JSON」中用 `engine` 字段手工指定；**未写规则的站点**在 HTTP 直连撞上 Cloudflare 或验证码时，会自动改用 Browserless 重试一次（可在插件设置中关闭）。

> Browserless 用的是官方云服务，免费额度 1000 units/月。注册账号、复制 Token 的具体步骤见 [插件 README「如何获取 Browserless Token」](plugins.v2/ptsitesignin/README.md#如何获取-browserless-token)。

- V2：[`plugins.v2/ptsitesignin`](plugins.v2/ptsitesignin)
- V3：[`plugins.v3/ptsitesignin`](plugins.v3/ptsitesignin)

主要功能：

- 默认复用 MoviePilot「站点管理」已保存的站点 Cookie 与 UA；账号密码直接写在站点管理的「附加信息」(note) 中（`{"username": "...", "password": "..."}`），签到前真实登录换回新 Cookie，不再维护插件内的账号 JSON
- 内置 Audiences、OpenCD、包子、慕雪阁、OshenPT、LuckPT、YemaPT、HDSky、dstudio.me、mua.xloli.cc、share.ilolicon.com 的签到预设，按域名自动匹配
- 签到后用登录态打开站点页面（站点管理里的 `activity_path` → 签到页 / 校验页 → 用户页 → 首页），读取「最近动向」（上次访问 / 上次签到等）并连站点自带的相对时间一起写入结果表与通知，形如 `2026-10-16 23:44:21 ( < 1分前 )`；没有账号密码时直接用站点管理里的 Cookie 签到，并在结果表标注「复用站点管理 Cookie」
- 观众（Audiences）签到页内嵌 Cloudflare Turnstile，且 widget 渲染在 closed shadow DOM 里：页面 JS 读不到 `cf-turnstile-response`，DOM 上也没有可调的 `data-callback`，而官方 `solve` 只生成 token（响应里的 `token` 字段）、不触发站点自己的提交逻辑。预设 `submit_method=auto` 走两段式：第一段 `solve` 取回 token 并用 `reconnect` 留住同一个浏览器会话，第二段在这个会话里**重新打开签到页**（复用会话拿到的是新页面）把 token 回填页面、尝试站点回调、必要时按表单真实 action 兜底 POST（上游 NexusPHP 契约即 `POST /attendance.php` 带 `cf-turnstile-response`），随后回查 `/attendance.php` 确认真实状态
- BrowserQL 单步失败（如 `solve` 超时）不会中止整个 mutation，插件不再把它当成整轮失败：失败步骤的错误会并入诊断，页面或回查已显示签到成功时仍如实上报成功
- 观众签到后会跳回首页，插件在第二段（注入 token 之后）于同一浏览器会话回查 `/attendance.php` 并等待 15 秒，只有看到真实的已签到 / 签到成功状态才上报成功；仍停留在人机验证页或 302 到 `login.php` 时分别给出明确失败原因，并把 token 来源与长度、站点回调是否被调用、兜底 POST 状态码、widget 是否在 closed shadow DOM 里一并写进失败原因
- 观众成功页的奖励文案不会再误判为「今日已签到」，预检已签到与本轮刚签到的结果会分别确认
- Cron 定时执行、保存后立即执行一次、进程级并发控制、后台 Timer 失败重试（不阻塞执行线程）
- 未写规则的站点遇 Cloudflare / 验证码时自动回退到浏览器引擎重试，无需手工配规则
- 插件详情页把「签到状态」与「登录状态」拆成两块：顶部一行统计胶囊（站点 / 成功 / 失败 / 未确认 / 未记录），每个站点一张可展开卡片、标题直接写「N 条记录」（按天累积的历史），展开是一张明细表（签到：时间 / 状态 / 结果；登录：时间 / 状态 / 登录 / 最近动向），同一个结果不会重复显示；设置页为扁平布局（顶部一行开关 + 两列字段），站点多选带「全部站点（含后续新增）」选项，签到站点与登录站点同时选中时两件事都执行
- 通知模式三档（每次执行后 / 仅失败时 / 不通知）
- Cookie 与 Token 不写入日志，签到结果按天留存（自动清理，仅保留最近 30 天）

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
│   ├── ptsitesignin/
│   │   ├── __init__.py        # 插件入口：配置页 / 结果页 / 调度
│   │   ├── core.py            # 双引擎核心逻辑（不依赖 MoviePilot）
│   │   ├── README.md
│   │   ├── signin.png
│   │   └── tools/
│   │       └── collect-rule-snippet.js   # 新站点规则采集脚本
└── plugins.v3/                # 兼容 MoviePilot V3 的插件
    └── ptsitesignin/
```

## 添加新站点规则

1. 浏览器登录目标站，打开签到页，F12 → Console 粘贴 `tools/collect-rule-snippet.js` 执行，复制输出的 JSON
2. 按输出内容编写规则（HTTP 或 Browserless），写入插件设置的「自定义站点规则 JSON」
3. 详细字段说明见 [插件 README](plugins.v2/ptsitesignin/README.md)
