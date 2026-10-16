# MoviePilot-Plugins

个人维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件仓库。

## 插件列表

### [PT站点签到](plugins.v2/ptsitesignin)

双引擎 PT 自动签到，可选站点账号真实登录换新 Cookie，并在签到后读取「最近动向」，支持 MoviePilot V2 与 V3。

| 引擎 | 原理 | 适用场景 |
|---|---|---|
| **HTTP 直连** | `requests` 请求签到接口（NexusPHP `attendance.php`） | 普通站点、无验证码的签到 |
| **Browserless** | BrowserQL 驱动真实浏览器，自动解题 / 点击 / 提交 | 图片验证码、Cloudflare、Turnstile 自动解题、弹窗签到、Altcha |

默认走 HTTP 直连（免费、毫秒级）。需要图片验证码 / Cloudflare / Turnstile 的站点由内置预设自动切到 Browserless，也可在「自定义站点规则 JSON」中用 `engine` 字段手工指定；**未写规则的站点**在 HTTP 直连撞上 Cloudflare 或验证码时，会自动改用 Browserless 重试一次（可在插件设置中关闭）。

> Browserless 用的是官方云服务，免费额度 1000 units/月。注册账号、复制 Token 的具体步骤见 [插件 README「如何获取 Browserless Token」](plugins.v2/ptsitesignin/README.md#如何获取-browserless-token)。

- V2：[`plugins.v2/ptsitesignin`](plugins.v2/ptsitesignin)
- V3：[`plugins.v3/ptsitesignin`](plugins.v3/ptsitesignin)

主要功能：

- 默认复用 MoviePilot 已保存的站点 Cookie 与 UA；也可在插件里按域名 / 站点 ID 选填站点账号，签到前真实登录换回新 Cookie
- 内置 Audiences、OpenCD、包子、慕雪阁、OshenPT、LuckPT、YemaPT、HDSky、dstudio.me、mua.xloli.cc、share.ilolicon.com 的签到预设，按域名自动匹配
- 签到后用登录态打开站点用户页，读取「最近动向」（上次访问 / 上次签到等）并写入结果表与通知；未配置账号时保持原有 Cookie 签到行为
- 观众（Audiences）签到页内嵌 Cloudflare Turnstile：实测该组件渲染在 **closed shadow DOM** 中，DOM/CSS 与 BrowserQL 深度选择器都命不中复选框，因此改为官方 `solve`（内置支持 iframe / shadow DOM 内的验证码）生成 token，再由 `submit_method=auto` 的脚本触发站点 `cfCallback`、必要时按表单 action 兜底 POST `/attendance.php`；两段验证用 `solve_timeout=40000`、`solve2_timeout=50000`、`solve2_wait=5000`、`cloudflare_wait=15`、`auto_wait=30000` 分别给预算
- BrowserQL 单步失败（如 `solve` 超时）不会中止整个 mutation，插件不再把它当成整轮失败：失败步骤的错误会并入诊断，页面或回查已显示签到成功时仍如实上报成功
- 观众签到后会跳回首页，插件在同一浏览器会话回查 `/attendance.php` 并等待 8 秒，只有看到真实的已签到 / 签到成功状态才上报成功；仍停留在人机验证页或 302 到 `login.php` 时分别给出明确失败原因
- 观众成功页的奖励文案不会再误判为「今日已签到」，预检已签到与本轮刚签到的结果会分别确认
- Cron 定时执行、保存后立即执行一次、进程级并发控制、后台 Timer 失败重试（不阻塞执行线程）
- 未写规则的站点遇 Cloudflare / 验证码时自动回退到浏览器引擎重试，无需手工配规则
- 插件详情页统计表（站点 / 方式 / 状态 / 时间 / 结果，含实际奖励如爆米花 / 魔力值）+ 通知模式三档（每次执行后 / 仅失败时 / 不通知）
- Cookie 与 Token 不写入日志，签到结果按天留存（自动清理，仅保留最近 30 天）

### [馒头登录 + 保活](plugins.v2/mteamlogin)

在插件配置页手填馒头（M-Team）账号密码或 authorization，**真实登录该账号并保活**，再用站点返回的「最近动向」时间自检，支持 MoviePilot V2 与 V3。

馒头是前后端分离的：前端 `kp.m-team.cc`，后端 API `api.m-team.io`（登录态绑定设备标识 `did`，插件会自动生成并固定）。前端每次登录 / 校验通过后会**立刻**调用 `POST /api/member/updateLastBrowse`（保活心跳），把「最近动向」刷成当前时间。旧版插件只读 `profile`、从不保活，所以拿到的永远是「上一次别处登录」留下的时间。

- V2：[`plugins.v2/mteamlogin`](plugins.v2/mteamlogin)
- V3：[`plugins.v3/mteamlogin`](plugins.v3/mteamlogin)

主要功能：

- 每轮先调用 `POST /api/member/updateLastBrowse` 做**保活心跳**，再读 `GET /api/member/profile` 的 `data.memberStatus.lastBrowse` 校验；保活正常时「最近动向」应为「刚刚 / 1 分钟前」
- 凭证**只从「站点管理」读取**，插件已删除全部手填凭证（用户名 / 密码 / OTP 密钥与动态码 / 邮箱与 Telegram 验证码 / 手动 API Key）；按站点 Token → Cookie 里的令牌 → API Key → Cookie 逐个尝试
- 前后端分离友好：按站点地址推导后端（`api.<域名>/api`），不可用时自动回退 `api.m-team.cc` / `api.m-team.io` / `api2.m-team.cc`；只读接口返回 405 时自动改用 POST，且除保活与只读接口外不碰任何其它写接口
- 详情页同时展示「最近动向」与「保活心跳」状态，保活失败会直接给出指向「站点管理」的修复建议；保留最近 30 天历史
- Cron 定时执行、保存后立即执行一次、三档通知模式；Token、API Key 与 Cookie 不写入日志

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
│   └── mteamlogin/
│       ├── __init__.py        # 插件入口：配置页 / 详情页 / 调度 / 站点管理读取
│       ├── core.py            # 签名、认证、保活心跳与最近动向查询（不依赖 MoviePilot）
│       ├── README.md
│       └── mteam.png
└── plugins.v3/                # 兼容 MoviePilot V3 的插件
    ├── ptsitesignin/
    └── mteamlogin/
```

## 添加新站点规则

1. 浏览器登录目标站，打开签到页，F12 → Console 粘贴 `tools/collect-rule-snippet.js` 执行，复制输出的 JSON
2. 按输出内容编写规则（HTTP 或 Browserless），写入插件设置的「自定义站点规则 JSON」
3. 详细字段说明见 [插件 README](plugins.v2/ptsitesignin/README.md)
