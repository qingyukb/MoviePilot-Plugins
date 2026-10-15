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
- 判定顺序：Cloudflare 验证页 → 登录页特征 → `already` 正则 → `success` 正则 → JSON `state` 字段
- 奖励显示：成功 / 已签到时自动提取奖励文案（如「获得 22 粒爆米花」「魔力值 +30」）显示在结果与通知中；规则里的 `bonus` 正则可覆盖默认模板（捕获组内容即显示文本）
- 遇 Cloudflare 人机验证或图片验证码时返回对应标记，未声明引擎的站点会自动改用浏览器重试（见下「自动回退到浏览器引擎」）
- 正则默认：`success` 匹配 `state:success / 签到成功 / 簽到成功`，`already` 匹配 `今日已签 / 已连续签到 / already`
- 可在规则里加 `"method": "POST"` 改成 POST 提交

内置 HTTP 站点预设（按域名自动匹配，无需手写规则）：

| 站点 | path | 说明 |
|---|---|---|
| Audiences (`audiences.me`) | `/attendance.php` | GET 即完成签到；成功判定「签到成功」，已签到判定「您今天已经签到过了」。被 Cloudflare 拦截时会自动回退到浏览器引擎 |

## 自动回退到浏览器引擎

只要站点**没有在自定义规则里声明 `engine`**（内置预设自带的默认值不算手工指定），HTTP 直连失败时若命中以下特征，会自动改用 Browserless 再试一次：

| HTTP 引擎检测到 | 自动回退方式 |
|---|---|
| Cloudflare 验证页（`just a moment` / `cf-chl-widget` / `challenge-platform` 等） | 浏览器打开签到页并自动通过挑战（`mode=cloudflare`） |
| 页面要求图片验证码（`captcha.php` / `id="captcha"` / `name="imagestring"` / 「验证码」等） | 浏览器打开签到页（`mode=open_page`） |

- 每个站点每轮只回退一次，且仅在 HTTP 判定为失败时触发；需要已填写 Browserless Token，未填写时结果会明确提示。
- 可在插件设置中用「遇验证码 / CF 自动用浏览器」开关整体关闭。
- 只有你在**自定义规则**里显式写了 `"engine": "http"` 的站点不回退（尊重手工配置）；内置预设的 HTTP 站点（如 Audiences）仍会自动回退。

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
- 因此「点击打勾」这类 **Managed Challenge** 只需规则写成 `{"engine":"browserless","mode":"cloudflare","path":"/attendance.php"}` 即可，无需指定具体类型。

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
| 签到站点 | 多选，仅列出已保存 Cookie 的站点 | 空 |
| 通知模式 | 每次执行后 / 仅失败时 / 不通知 | 每次执行后 |
| 并发数 | 同时签到站点数 | 2 |
| HTTP 超时 | 秒 | 30 |
| 失败重试次数 / 等待分钟 | 额外重试次数与间隔 | 1 次 / 10 分钟 |
| HTTP 引擎使用代理 | 复用 MoviePilot 全局代理 | 关 |
| 遇验证码 / CF 自动用浏览器 | 未声明引擎的站点 HTTP 撞上 CF / 验证码时自动换浏览器重试 | 开 |
| Browserless 地址 / Token / 超时 | Browserless 连接参数 | 官方地址 / 空 / 100 秒 |
| 自定义站点规则 | JSON | 空 |

## 自定义规则示例

HTTP 站点：

```json
{
  "pt.example.com": {
    "path": "/attendance.php",
    "success": "(签到成功|state[\"']?\\s*:\\s*[\"']?success)",
    "already": "(已签到|今日已签)"
  },
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
| `submit_method` | `click`（原生点击）/ `ajax` / `dom_click` / `confirm_click` / `altcha_click` |
| `captcha_selector` / `captcha_input_selector` | 图片验证码图片与输入框 |
| `trigger_selector` / `trigger_wait` | 弹窗触发按钮与等待毫秒 |
| `preflight_path` / `wait` / `preflight_wait` | 页面预检与等待时间 |
| `success_keywords` / `already_keywords` | 文本关键词判定（严格模式） |
| `success_json` | 形如 `{"field":"/state","values":["success"]}` 的 AJAX JSON 判定 |
| `strict_status_keywords` | `true` 时只认 `success_keywords`，忽略通用词 |

## 日志与安全

- Cookie、Token 均不会写入日志或签到结果；日志只记录站点名与状态
- 结果表按「站点 / 方式 / 状态 / 时间 / 结果」展示，成功统一显示「签到成功」
- 签到结果按天留存：`latest`、`history_YYYYMMDD`
