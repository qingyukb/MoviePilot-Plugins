# PT站点签到（PTSiteSignIn）

参考 [tmzg0000/moviepilot-plugins](https://github.com/tmzg0000/moviepilot-plugins) 的插件规范编写的 MoviePilot 双引擎签到插件。

| 引擎 | 原理 | 适用场景 | 成本 |
|---|---|---|---|
| **HTTP 直连** | `requests` 请求签到接口（NexusPHP `attendance.php`） | 普通站点、无验证码的标签到 | 免费、毫秒级 |
| **Browserless** | BrowserQL 驱动真实浏览器，自动解题/点击/提交 | 图片验证码、Cloudflare、**Turnstile 点击打勾**、弹窗签到、Altcha | 消耗 Browserless 额度 |

默认走 HTTP 直连；内置预设或「自定义站点规则 JSON」中用 `engine` 声明为 `browserless` 的站点会自动改用浏览器引擎，其余站点一律 HTTP，无需手动切换全局引擎。**未声明引擎的站点**在 HTTP 直连遇到 Cloudflare 或图片验证码时，会自动改用 Browserless 重试一次（可在配置中关闭，见下「自动回退到浏览器引擎」）。

> Browserless 部分参考并精简自 tmzg0000 的 `captchasignin` 插件，保留其验证码求解、页面预检与 AJAX 结果判定逻辑。

## 文件结构

```
ptsitesignin/
├── __init__.py    # 插件入口：配置页、结果页、Cron、调度与重试
├── core.py        # 双引擎核心：HTTP 引擎 + Browserless(BrowserQL) 引擎、内置站点预设
└── README.md
```

## 安装

把 `ptsitesignin` 目录放到 MoviePilot 插件目录：

- V2：`/config/plugins/ptsitesignin/`
- V3：`/config/plugins/ptsitesignin/`

重启（或重载插件）后在插件页启用。

## HTTP 引擎（默认）

- 请求 `站点域名 + path`，默认 `/attendance.php`
- 判定顺序：Cloudflare 验证页 → 登录页特征 → `success` 正则 → `already` 正则 → JSON `state` 字段 → 脚本 `state` 兜底
- 奖励显示：成功 / 已签到时自动提取奖励文案（如「获得 22 粒爆米花」「魔力值 +30」）显示在结果与通知中；规则里的 `bonus` 正则可覆盖默认模板（捕获组内容即显示文本）
- 遇 Cloudflare 人机验证或图片验证码时返回对应标记，未声明引擎的站点会自动改用浏览器重试（见下「自动回退到浏览器引擎」）
- 判定只在去掉 `script/style` 的可见正文上进行；`success` 先于 `already` 判断，避免「签到成功 + 已连续签到 N 天」被误判为今日已签到（观众 Audiences 回归用例）
- 正则默认：`success` 匹配 `state:success / 签到成功 / 簽到成功`，`already` 匹配 `今日已签 / 已连续签到 / already`
- 可在规则里加 `"method": "POST"` 改成 POST 提交

内置站点预设（按域名自动匹配，无需手写规则）：

| 站点 | path | 说明 |
|---|---|---|
| Audiences (`audiences.me`) | `/attendance.php` | Vue + Cloudflare Turnstile 签到页，**没有提交按钮**；验证通过后由页面 JS 自动提交。预设走 Browserless（`submit_method=auto`）：`solve` 只生成 token、**不会**触发站点回调，插件随后先触发站点 `data-callback` 回调，必要时再按表单真实 `action` 带 token 同源 POST 兜底（`cloudflare_wait=15` / `solve_timeout=30000` / `auto_wait=15000` / `wait=8000`），再在同一会话重新打开 `/attendance.php` 等待 8 秒核验（`verify_success_path` / `verify_wait=8000`）。只有确认真实的已签到 / 签到成功状态才上报成功；仍然停在人机验证页或只看到首页时会明确报失败，并附上 token / 回调 / 表单的现场诊断 |

## 并发与失败重试

- Cron 与「保存后立即执行一次」共用进程级互斥锁：同一时间只允许一轮签到，另一轮会直接跳过并记录日志，避免重复签到、重复消耗 Browserless 额度以及 latest/history 互相覆盖。
- 失败重试由后台 Timer 延迟触发，不再用 `sleep` 占用执行线程；等待间隔由「失败后等待分钟数」控制。
- 插件停用 / 重载会取消尚未触发的重试；已完成的首轮结果会先落盘，最终结果覆盖当天历史。

## 自动回退到浏览器引擎

只要站点**没有在自定义规则里声明 `engine`**（内置预设自带的默认值不算手工指定），HTTP 直连失败时若命中以下特征，会自动改用 Browserless 再试一次：

| HTTP 引擎检测到 | 自动回退方式 |
|---|---|
| Cloudflare 验证页（`just a moment` / `cf-chl-widget` / `challenge-platform` 等） | 浏览器打开签到页并自动通过挑战（`mode=cloudflare`） |
| 页面要求图片验证码（`captcha.php` / `id="captcha"` / `name="imagestring"` / 「验证码」等） | 浏览器打开签到页（`mode=open_page`） |

- 每个站点每轮只回退一次，且仅在 HTTP 判定为失败时触发；需要已填写 Browserless Token，未填写时结果会明确提示。
- 可在插件设置中用「遇验证码 / CF 自动用浏览器」开关整体关闭。
- 只有你在**自定义规则**里显式写了 `"engine": "http"` 的站点不回退（尊重手工配置）；内置预设里声明 `engine=http` 的站点仍会自动回退。

### Cloudflare「Just a moment...」交互挑战

部分站点使用 Cloudflare 的 **JS 交互挑战**（页面标题 `Just a moment...`，正文带 `_cf_chl_opt` / `chl_page`）。它**不需要人工点击**：真实浏览器只要在页面上等几秒，挑战会自己跑完 JS 并跳到真实页面。插件为此做了三件事：

- `mode=cloudflare` 时会在页面停留等待挑战自动放行，默认 **10 秒**；可用站点规则字段 `cloudflare_wait`（秒，范围 3–45）调整。
- Cloudflare 挑战页是以 **403 / 503** 返回的，挑战放行后 `goto` 记录的仍是挑战页状态码，所以不能只看状态码；插件改为「状态码 + 页面是否仍带挑战特征」一起判断。
- 等待超时且页面仍是挑战页时，结果会明确提示「Cloudflare 挑战在 N 秒内未自动放行」，而不是含糊的「未匹配到签到成功标志」。

> 判断「是否还卡在挑战页」只认 `just a moment` / `_cf_chl_opt` / `chl_page` 这些交互挑战**专属**标记；正常页面里的 Turnstile 组件（带 `challenge-platform` / `cf-turnstile`）不会被误判为交互挑战，而是按各自规则等待验证通过。

> 云节点 / 机房 IP 更容易被反复挑战。同一站点长期过不去时，优先换节点或住宅代理，而不是一味调大 `cloudflare_wait`。

> 图片验证码场景走 `open_page`：真实浏览器打开签到页后，部分站点不再要求验证码或可直接完成签到。
> 若该站确实需要填图片验证码，请在自定义规则中配置 `mode=image` 与 `captcha_selector` / `captcha_input_selector`。

## Browserless 引擎

### 如何获取 Browserless Token

1. 打开账号面板 <https://browserless.io/account/>，用邮箱注册（免费额度 **1000 units/月**，不需要信用卡）。
2. 登录后，面板**首页的「API Key」卡片**就显示你的 Token（默认打码，点眼睛图标显示、点复制按钮复制）；卡片上的 **Manage API keys** 可新建 / 管理多个 Token。
3. 回到 MoviePilot 插件设置，把 Token 粘贴到 **Browserless Token**，地址保持默认 `https://production-sfo.browserless.io` 即可 —— 插件会自动补全 `/stealth/bql` 并带上 `?token=`。
4. 保留「遇验证码 / CF 自动用浏览器」开关为开启（默认开），未写规则的站点遇到 Cloudflare 或验证码时会自动调用它。

要点：

- 免费额度 1000 units/月，一次签到通常消耗几十 units；可在面板查看已用量与剩余额度。
- 官方区域端点：美西 `production-sfo.browserless.io`（默认）、欧洲 `production-ams.browserless.io`，按目标站点就近选择成功率更高。
- 自建 Browserless（Docker）时：地址填你的服务根地址（如 `http://192.168.1.10:3000`），Token 填启动时 `-e TOKEN=xxx` 设置的值。
- Token 只保存在 MoviePilot 插件配置里，不会写入日志与签到结果。

### 内置站点预设

自动识别域名，无需手写规则：

| 站点 | mode | 说明 |
|---|---|---|
| OpenCD (`open.cd`) | `image` | 图片验证码 + 首页「查看簽到記錄」二次确认 |
| 包子 (`p.t-baozi.cc`) | `image` | 图片验证码 |
| 慕雪阁 (`pt.muxuege.org`) | `open_page` | 点击「立即签到」 |
| OshenPT (`oshen.win`) | `image` | AJAX 提交以捕获服务端校验结果 |
| LuckPT (`pt.luckpt.de`) | `open_page` | 领取奖励需确认弹窗 |
| YemaPT (`yemapt.org`) | `altcha` | Hash 路由 + Altcha 复选框 + 提交按钮 |
| HDSky (`hdsky.me`) | `trigger_image` | 先点开签到弹窗再解图片验证码 |
| `dstudio.me` / `mua.xloli.cc` / `share.ilolicon.com` | `cloudflare` | Cloudflare 托管挑战自动通过 |

### 关于 Cloudflare Turnstile「点击打勾」

**HTTP 引擎不行，Browserless 引擎可以。**

- Turnstile 需要执行 JS 并生成一次性 token，还要拿到 `cf_clearance` Cookie，纯 HTTP 无法伪造；
- Browserless 使用 `solve:solve(...)`（**不限定类型**，由它自动识别挑战种类），官方支持 reCAPTCHA、hCaptcha、Cloudflare Turnstile，能点击 iframe / shadow DOM 内的复选框，并带拟人化点击、指纹规避；
- 观众（Audiences）的 Turnstile 页面**没有提交按钮**：规则写 `submit_method=auto`。注意 **`solve` 只解题、不点提交**（[官方文档](https://docs.browserless.io/browserql/bot-detection/solving-captchas)：解完验证码要自己触发提交；没有提交按钮就用 `evaluate` 触发），所以插件在 `solve` 之后先触发站点 `data-callback` 回调，必要时再按表单真实 `action` 带 token 同源 POST 兜底，并把现场诊断（token / 回调 / 表单 / POST）写进失败原因。签到完成后页面可能跳回首页，因此预置规则会在同一个 Browserless 会话中重新打开 `/attendance.php` 并等待 8 秒核验，只有看到签到成功 / 今日已签到才上报成功，避免“跳回首页但实际没签到”被误报为失败或成功。
- Cookie 失效时 NexusPHP 会把 `/attendance.php` 302 到 `login.php?returnto=attendance.php`：插件直接提示「Cookie 无效或已过期，请重新登录后更新 Cookie」，不再让人机验证超时背锅。
- 普通「点击打勾」页面若另有提交按钮，可写成 `{"engine":"browserless","mode":"cloudflare","path":"/attendance.php"}`，由插件按默认 `click` 提交。

注意事项：

1. **IP 信誉决定成功率。** 云节点（含 Browserless 免费额度节点）更容易被 Cloudflare 反复挑战；自建 Browserless + 住宅代理成功率显著更高。
2. 默认走 `https://production-sfo.browserless.io` + `/stealth/bql`，自建服务填根地址或完整 `/stealth/bql` 地址即可（会自动补全路径并带上 `?token=`）。
3. 免费额度 1000 units/月，一次签到通常几十 units，建议并发设为 1–2。
4. 若日志出现「找到验证码但未能完成验证」，多半是该挑战未通过（换节点/IP 或改用住宅代理重试）。

## 配置项

| 配置 | 说明 | 默认 |
|---|---|---|
| 启用插件 | 开关 | 关 |
| 执行周期 | 5 位 Cron | `25 8 * * *` |
| 保存后立即执行一次 | 保存后由**后台调度器**延后 3 秒跑一轮，不阻塞设置页；跳过失败重试 | 关 |
| 签到站点 | 多选，仅列出已保存 Cookie 的站点 | 空 |
| 通知模式 | 每次执行后 / 仅失败时 / 不通知 | 每次执行后 |
| 并发数 | 同时签到站点数 | 2 |
| HTTP 超时 | 秒 | 30 |
| 失败重试次数 / 等待分钟 | 额外重试次数与间隔；重试由后台 Timer 延迟执行，不占用执行线程 | 1 次 / 10 分钟 |
| HTTP 引擎使用代理 | 复用 MoviePilot 全局代理 | 关 |
| 遇验证码 / CF 自动用浏览器 | 内置预设或未声明引擎的站点 HTTP 撞上 CF / 验证码时自动换浏览器重试（手工写了 engine 的除外） | 开 |
| Browserless 地址 / Token / 超时 | Browserless 连接参数 | 官方地址 / 空 / 100 秒 |
| 自定义站点规则 | JSON；Cloudflare 交互挑战可用 `cloudflare_wait`（秒）覆盖默认 10 秒；无提交按钮、由验证回调自己提交的页面用 `submit_method: "auto"`（等待 token 的毫秒数可用 `auto_wait` 调整） | 空 |

## 自定义规则示例

HTTP 站点：

```json
{
  "pt.example.com": {
    "path": "/attendance.php",
    "success": "(签到成功|state[\"']?\\s*:\\s*[\"']?success)",
    "already": "(已签到|今日已签)"
  },
  "audiences.me": { "path": "/attendance.php", "cloudflare_wait": 20 },
  "5": { "path": "/signin.php", "method": "POST" }
}
```

Browserless 站点（键可用域名或站点 ID，`engine` 会覆盖全局默认）：

```json
{
  "pt.example.com": {
    "engine": "browserless",
    "mode": "cloudflare",
    "path": "/attendance.php",
    "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    "success_keywords": ["签到成功"],
    "already_keywords": ["已签到"]
  }
}
```

常用可选字段：

| 字段 | 作用 |
|---|---|
| `mode` | `http` / `open_page` / `cloudflare` / `image` / `trigger_image` / `altcha` |
| `submit_selector` / `submit_text` | 提交按钮选择器或按钮文字 |
| `submit_method` | `click`（原生点击）/ `ajax` / `dom_click` / `confirm_click` / `altcha_click` / `auto`（页面内嵌 Turnstile，先触发 `data-callback`，必要时按真实 action 补一次 POST）/ `none`（什么都不点，纯等页面自己签完） |
| `captcha_selector` / `captcha_input_selector` | 图片验证码图片与输入框 |
| `trigger_selector` / `trigger_wait` | 弹窗触发按钮与等待毫秒 |
| `preflight_path` / `wait` / `preflight_wait` / `cloudflare_wait` / `auto_wait` / `solve_timeout` | 页面预检、签到后等待、Cloudflare 自动放行、Turnstile token 与 solve 找验证码的等待时间 |
| `success_keywords` / `already_keywords` / `pending_keywords` | 文本关键词判定；`pending_keywords` 用于诊断仍停留在人机验证中的页面 |
| `success_json` | 形如 `{"field":"/state","values":["success"]}` 的 AJAX JSON 判定 |
| `strict_status_keywords` | `true` 时只认 `success_keywords`，忽略通用词 |

## 日志与安全

- Cookie、Token 均不会写入日志或签到结果；日志只记录站点名与状态
- 结果表按「站点 / 方式 / 状态 / 时间 / 结果」展示，成功统一显示「签到成功」
- 签到结果按天留存：`latest`、`history_YYYYMMDD`；自动清理，仅保留最近 30 天（`history_index` 保存索引）
