# PT站点签到（PTSiteSignIn）

参考 [tmzg0000/moviepilot-plugins](https://github.com/tmzg0000/moviepilot-plugins) 的插件规范编写的 MoviePilot 双引擎签到插件。

| 引擎 | 原理 | 适用场景 | 成本 |
|---|---|---|---|
| **HTTP 直连** | `requests` 请求签到接口（NexusPHP `attendance.php`） | 普通站点、无验证码的标签到 | 免费、毫秒级 |
| **Browserless** | BrowserQL 驱动真实浏览器，自动解题/点击/提交 | 图片验证码、Cloudflare、**Turnstile 自动解题**、弹窗签到、Altcha | 消耗 Browserless 额度 |

默认走 HTTP 直连；内置预设或「自定义站点规则 JSON」中用 `engine` 声明为 `browserless` 的站点会自动改用浏览器引擎，其余站点一律 HTTP，无需手动切换全局引擎。**未声明引擎的站点**在 HTTP 直连遇到 Cloudflare 或图片验证码时，会自动改用 Browserless 重试一次（恒为开启，见下「自动回退到浏览器引擎」）。

> Browserless 部分参考并精简自 tmzg0000 的 `captchasignin` 插件，保留其验证码求解、页面预检与 AJAX 结果判定逻辑。

## 文件结构

```
ptsitesignin/
├── __init__.py    # 插件入口：配置页、结果页、Cron、调度与重试
├── core.py        # 双引擎核心：HTTP 引擎 + Browserless(BrowserQL) 引擎、站点登录 + 最近动向、内置站点预设
└── README.md
```

## 安装

把 `ptsitesignin` 目录放到 MoviePilot 插件目录：

- V2：`/config/plugins/ptsitesignin/`
- V3：`/config/plugins/ptsitesignin/`

重启（或重载插件）后在插件页启用。

## HTTP 引擎（默认）

- 请求 `站点域名 + path`，默认 `/attendance.php`；**默认路径返回 404 / 405 时自动换签到入口**：① 先在首页里找**签到表单**——提交按钮文字含「签到 / 打卡」时按表单的 `action` / `method` 提交，并把按钮字段（如 `signin=每日签到`）一起发过去（学校 BTSCHOOL 就是这种「首页挂一个每日签到按钮」的站，没有独立签到页）；② 再找同站链接里文字含「签到 / 打卡」的；③ 然后依次试 `/signin.php`、`/sign_in.php`、`/checkin.php`、`/plugin_sign-in.php`、`/attendance.php?action=sign`。都找不到时失败原因会列出试过哪些路径，可直接在自定义规则里用 `path` 指定
- 判定顺序：Cloudflare 验证页 → 登录页特征 → `success` 正则 → `already` 正则 → JSON `state` 字段 → 脚本 `state` 兜底
- 奖励显示：成功 / 已签到时自动提取奖励文案（如「获得 22 粒爆米花」「获得 N 点魔力值」）写进详情页「结果」列；**数值是每次跑的时候从页面文本抓的**，不是写死的（今天 87、明天按站点显示的那个数），规则里的 `bonus` 正则可覆盖默认模板（捕获组内容即显示文本）
- 遇 Cloudflare 人机验证或图片验证码时返回对应标记，未声明引擎的站点会自动改用浏览器重试（见下「自动回退到浏览器引擎」）
- 判定只在去掉 `script/style` 的可见正文上进行；`success` 先于 `already` 判断，避免「签到成功 + 已连续签到 N 天」被误判为今日已签到（观众 Audiences 回归用例）
- 正则默认：`success` 匹配 `state:success / 签到成功 / 簽到成功`，`already` 匹配 `今日已签 / 已连续签到 / already`
- 可在规则里加 `"method": "POST"` 改成 POST 提交

内置站点预设（按域名自动匹配，无需手写规则）：

| 站点 | path | 说明 |
|---|---|---|
| Audiences (`audiences.me`) | `/attendance.php` | Vue + Cloudflare 签到页，**没有提交按钮**，Turnstile 渲染在 closed shadow DOM 里（页面 JS 读不到 `cf-turnstile-response`，DOM 上也没有可调的 `data-callback`）。预设 `submit_method=auto` + `solve_count=1` 两段式：第一段官方 `solve` 拿回 token 并用 `reconnect` 留住同一个浏览器会话，第二段在同一会话把 token 回填页面、尝试站点回调、必要时按表单真实 action 兜底 POST（上游 NexusPHP 的契约就是 `POST /attendance.php` 带 `cf-turnstile-response`），最后回查 `/attendance.php` 确认真实状态（`verify_success_path` / `verify_wait=15000`） |

## 站点登录 + 最近动向

凭证只读取 **MoviePilot「站点管理」**，不再在插件配置里单独维护账号：

1. **MoviePilot「站点管理」里该站的「附加信息」(note)** —— V3 是 JSON 对象、V2 是 JSON 字符串，写 `{"username": "账号", "password": "密码"}` 即可，支持 `login_path` / `login_action` / `username_field` / `password_field` / `fields` / `activity_path`；
2. **MoviePilot「站点管理」里保存的 Cookie** —— 没有账号密码时直接用这份 Cookie 签到，结果表「登录」列会标注「复用站点管理 Cookie」。

部分 NexusPHP 站点的 Cookie 容易过期，表现为签到一直跳登录页；站点管理里填写账号后，插件会在每次签到前先真实登录一次、换回新 Cookie 再交给 HTTP / Browserless 引擎。

```json
{
  "username": "your-user",
  "password": "your-password",
  "username_field": "username",
  "password_field": "password",
  "fields": {"2fa": "123456"},
  "activity_path": "/userdetails.php?id=123"
}
```

- 默认登录路径为 `/login.php`，默认提交地址为 `/takelogin.php`；非标准 NexusPHP 站点可用 `login_path` / `login_action` 覆盖。
- 默认字段名为 `username` / `password`，可用 `username_field` / `password_field` 覆盖；额外隐藏字段 / 两步验证字段可放进 `fields`。
- 登录成功后会改用新 Cookie 签到。登录失败不会直接放弃整站：仍会用 MoviePilot 里已有的旧 Cookie 尝试签到，并在结果表「登录」列如实写明失败原因。
- 打开「读取「最近动向」」后，插件会按 `activity_path`（可选）→ 签到页 / 校验页（如观众的 `/attendance.php`）→ 首页发现的 `userdetails.php?id=...`（首页没有链接时用 Cookie 里的 `c_secure_uid` 解出 uid）→ 首页 的顺序读取「最近动向 / 上次访问 / 上次签到」，显示在详情页的「登录状态」表里；撞上 Cloudflare 挑战页的候选页会被跳过，读过之后会记录来源页面，读不到时直接给出可操作提示。关键词后面必须紧跟时间才算命中、多个关键词取距离最近的那个，没有关键词时只接受带时分的时间——裸日期（种子列表 / 公告里的 `2026-09-18`）不会被当成本站的最近动向。
- 「最近动向」会把站点自带的相对时间一起取出，例如站点写 `最近动向：2026-10-16 23:44:21 ( < 1分前 )` 时，插件就原样显示 `2026-10-16 23:44:21 ( < 1分前 )`；站点没写相对时间时只显示时间。

## 详情页（签到状态 / 登录状态）

插件详情页把「签到」和「登录」分成两张方框表格，一眼看完：

- 顶部一行统计胶囊：**站点 / 成功 / 失败 / 未确认 / 未记录** —— 「未确认」是结果未知、建议人工核对的那类失败（网络中断、二次核验没确认、token 注入没拿到响应）；「未记录」是本轮勾选却一条记录都没有的站点；数值为 0 的用灰色显示。
- **签到状态**表：`站点 / 状态 / 结果`。站点单元格里写着今天几点跑的、累计多少条记录；「状态」是签到结果（签到成功 / 今日已签到 / 未确认 / 未记录）；「结果」显示实际到手的东西（如 `获得 20 粒爆米花`、`获得 65`）。
- **登录状态**表：同样三列。状态是登录结果（登录成功 / 登录失败 / 未记录），「结果」列显示「最近动向」原文（如 `2026-10-17 03:20:00 ( < 1分前 )`）；登录失败时改成显示失败原因。
- 站点按设置里勾选的顺序排列；今天还没跑到的站点显示「未记录」，不会从表里消失；与签到站点双选的站点两张表里都有。
- 历史记录按天保留最近 30 天，「共 N 条记录」就是这个站点最近签到 / 登录过多少轮。
- 账号密码只保存在 MoviePilot「站点管理」的附加信息里，不写入签到结果或日志；没有账号密码时直接复用站点管理里的 Cookie。

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
- 自动回退**恒为开启**（设置页不再提供开关），避免「忘记打开导致 HTTP 站点一直失败」。
- 只有你在**自定义规则**里显式写了 `"engine": "http"` 的站点不回退（尊重手工配置）；内置预设里声明 `engine=http` 的站点仍会自动回退。
- 回退前会先用「首页签到表单 / 签到链接」修正签到入口，避免浏览器去打开一个 404 的地址（例如学校那种首页签到的站）。

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
4. 未写规则的站点遇到 Cloudflare 或验证码时会自动改用浏览器重试一次（恒为开启，无需开关）。

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
| AGSVPT (`agsvpt.com`) | HTTP `POST` | NexusPHP 签到页，页面上是「立即签到」提交按钮：GET 只渲染按钮不签到，预设用 `method: POST`；撞上验证码 / 盾时自动回退到浏览器并按「立即签到」按钮点击 |
| BTSCHOOL (`btschool.club`，学校) | HTTP `POST` | 签到按钮直接挂在**首页**，没有独立签到页（实测 `/attendance.php`、`/signin.php`、`/checkin.php` 全是 404）：预设用 `POST /index.php`。这站签到成功**不给任何提示**、只是把「每日签到」入口拿掉，所以用 `absent_keywords: ["每日签到"]` 判定「入口消失＝已完成」；奖励如「获得 N 点魔力值」每次从页面原文抓取（数值每天不同） |
| `dstudio.me` / `mua.xloli.cc` / `share.ilolicon.com` | `cloudflare` | Cloudflare 托管挑战自动通过 |

### 关于 Cloudflare Turnstile

**HTTP 引擎不行，Browserless 引擎可以。**

- Turnstile 需要执行 JS 并生成一次性 token，还要拿到 `cf_clearance` Cookie，纯 HTTP 无法伪造；
- Browserless 使用 `solve:solve(...)`（**不限定类型**，由它自动识别挑战种类），官方支持 reCAPTCHA、hCaptcha、Cloudflare Turnstile，能点击 iframe / shadow DOM 内的复选框，并带拟人化点击、指纹规避；
- 观众（Audiences）的签到页**没有提交按钮**，且 Turnstile 渲染在 **closed shadow DOM** 里：页面 JS 既读不到 `cf-turnstile-response`，DOM 上也没有可调的 `data-callback`。官方 `solve` 只负责生成 token（写在响应的 `token` 字段里），**不会**触发站点挂在自己 widget 上的提交逻辑，所以插件对这类站点走两段式：
  1. **第一段**：`goto` 打开签到页、`solve` 取回 token（`{found solved time token}`），并用 `reconnect(timeout: reconnect_wait)` 留住同一个浏览器会话；
  2. **第二段**：请求打到 `reconnect` 返回的 `browserQLEndpoint`（同一个浏览器，cookie / `cf_clearance` 都还在；**实测复用会话拿到的是新页面，所以第二段仍会重新 `goto` 打开签到页**，并先等 Vue / Turnstile 组件渲染出来再注入），把 token 回填所有 `cf-turnstile-response` 输入框（含 open shadow DOM），逐个尝试站点回调（`data-callback` 属性 + `cfCallback` / `turnstileCallback` 等常见全局名），页面仍停在验证中时再按表单真实 `action` 同源 POST 兜底（上游 NexusPHP 的契约就是 `POST /attendance.php` 带 `cf-turnstile-response`），最后重新打开 `/attendance.php` 核验真实状态。
- 第二段的 `evaluate` 显式带 `timeout: auto_wait + 12 秒`：BQL 的 `evaluate` 步骤默认 30 秒超时，脚本自身等待预算也设 30 秒时会被掐死在等待循环里，窗口里只剩脚本刚启动时的初始快照（历史日志里那句「未拿到 Turnstile token / 页面没有 data-callback 回调」就是这么来的假结论）。
- BrowserQL 的单个步骤失败（如 `solve` 超时）不会中止整个 mutation：失败步骤只在 `data` 里返回 `null`，其余步骤照常执行。插件据此把步骤错误并入失败诊断，不再整轮判失败——页面或回查已显示签到成功时仍上报成功。
- 签到完成后页面可能跳回首页，因此预置规则会在第二段（注入 token 之后）于同一个 Browserless 会话里重新打开 `/attendance.php` 并等待 15 秒核验，只有看到签到成功 / 今日已签到才上报成功；若仍停留在人机验证页，会明确报告未确认签到，并把 token 来源、回调、兜底 POST 状态与页面残留状态一起写进失败原因。
- Cookie 失效时 NexusPHP 会把 `/attendance.php` 302 到 `login.php?returnto=attendance.php`：插件直接提示「Cookie 无效或已过期，请重新登录后更新 Cookie」，不再让人机验证超时背锅。
- 普通「点击打勾」页面若另有提交按钮，可写成 `{"engine":"browserless","mode":"cloudflare","path":"/attendance.php"}`，由插件按默认 `click` 提交。

注意事项：

1. **IP 信誉决定成功率。** 云节点（含 Browserless 免费额度节点）更容易被 Cloudflare 反复挑战；自建 Browserless + 住宅代理成功率显著更高。
2. 默认走 `https://production-sfo.browserless.io` + `/stealth/bql`，自建服务填根地址或完整 `/stealth/bql` 地址即可（会自动补全路径并带上 `?token=`）。
3. 免费额度 1000 units/月，一次签到通常几十 units，建议并发设为 1–2。
4. 若日志出现「找到验证码但未能完成验证」「solve 超时」或「未拿到 Turnstile token」，多半是当前节点的 IP 被 Cloudflare 判定为可疑（观众站的 Turnstile 属托管挑战，机房 IP 常被判为必须人工交互）：换节点或改用住宅代理成功率最高。插件已把 solve 的 found/solved/time、token 来源与长度、站点回调是否被调用、兜底 POST 的 HTTP 状态码、widget 是否在 closed shadow DOM 里、页面是否仍停在人机验证一起写进失败原因，照着这几项即可判断是「没解出 token」「token 注入后站点没提交」还是「站点拒了 token」。
5. 出现「Browserless 请求超时（…秒内未收到响应）」通常是插件 / 服务器到 Browserless 的跨境链路抖动，或节点执行过慢。插件会自动把 HTTP 读超时抬到不低于「页面内等待预算 + 45 秒余量」，避免读超时比 BQL 内部等待还短而自己掐断连接；仍超时请调大「Browserless 超时」或换成更近的区域（如 `production-ams.browserless.io`）。主流程响应丢失时插件会自动只读回查一次，已签到不会被报成失败。

## 配置项

| 配置 | 说明 | 默认 |
|---|---|---|
| 启用插件 | 开关 | 关 |
| 执行周期 | 5 位 Cron | `25 8 * * *` |
| 保存后立即执行一次 | 保存后由**后台调度器**延后 3 秒跑一轮，不阻塞设置页；跳过失败重试 | 关 |
| 签到站点 | 多选，只做签到；仅列出已保存 Cookie 的站点，第一项「全部站点（含后续新增）」会自动包含以后新增的站点 | 空 |
| 登录站点 | 多选，只做真实登录并读取「最近动向」，不签到；同样有「全部站点」选项。**与签到站点同时选中时两件事都执行**：一轮内完成真实登录 + 签到 + 读最近动向（不会为了登录再登一次），并同时计入详情页的「签到状态」与「登录状态」 | 空 |
| 读取「最近动向」 | 用站点管理里的 Cookie（配置了账号时会真实登录换回新 Cookie）打开用户页 / 签到页，把最近动向时间 + 相对时间（如 `2026-10-16 23:44:21 ( < 1分前 )`）写入详情页的「登录状态」表；撞上 Cloudflare 挑战页的候选页会自动跳过，读不到时页面会给出提示 | 开 |
| 发送通知 | 开 / 关。通知保持简短：一行「站点 N 个｜成功 X｜失败 Y」，只有失败时才附「站点：原因」（每条最多 180 字） | 开 |
| 并发数 | 同时签到站点数 | 2 |
| HTTP 超时 | 秒 | 30 |
| 失败重试次数 / 等待分钟 | 额外重试次数与间隔；重试由后台 Timer 延迟执行，不占用执行线程 | 1 次 / 10 分钟 |
| 走代理 | **不用插件开关**：跟随 MoviePilot「站点管理 → 使用代理访问」——勾了的站点才走 MoviePilot 全局代理（设置 → 系统 → 代理），Browserless 引擎走它自己的出口；勾了但全局代理为空时日志会写明本次仍直连 | 关 |
| Browserless 地址 / Token / 超时 | Browserless 连接参数；读超时自动抬升到「页面内等待预算 + 45 秒余量」（上限 300 秒），避免读超时短于 BrowserQL 内部等待 | 官方地址 / 空 / 100 秒 |
| 自定义站点规则 | JSON；Cloudflare 交互挑战可用 `cloudflare_wait`（秒）覆盖默认 10 秒；页面内嵌 Turnstile、且验证通过后由站点自己提交的站点（如观众 Audiences）用 `submit_method: "auto"` + `solve_count: 1`：插件会两段式处理（solve 取 token → 同一会话注入并提交），`auto_wait` 控制脚本等 token 的时间、`reconnect_wait` 控制会话保留时间；只有确实能命中的站点才用 `turnstile_click` + `submit_selector` 原生点击 | 空 |

## 自定义规则示例

HTTP 站点：

```json
{
  "pt.example.com": {
    "path": "/attendance.php",
    "success": "(签到成功|state[\"']?\\s*:\\s*[\"']?success)",
    "already": "(已签到|今日已签)"
  },
  "audiences.me": { "engine": "browserless", "mode": "cloudflare", "path": "/attendance.php", "cloudflare_wait": 15, "submit_method": "auto", "solve_count": 1, "solve_timeout": 40000, "auto_wait": 30000, "reconnect_wait": 60000, "wait": 15000, "verify_success_path": "/attendance.php", "verify_wait": 15000 },
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
| `submit_method` | `click`（原生点击）/ `turnstile_click`（先等跨域 iframe 内的 Turnstile 复选框可见再原生点击；仅适用于选择器确实能命中的站点）/ `ajax` / `dom_click` / `confirm_click` / `altcha_click` / `auto`（页面内嵌 Turnstile：第一段 solve 取回 token 并保留会话，第二段在同一会话把 token 回填 `cf-turnstile-response`、尝试站点回调、必要时按真实 action 补一次 POST，随后回查）/ `none`（什么都不点，纯等页面自己签完）。观众 Audiences 用 `auto`；页面真能自动签完的站点才用 `none` |
| `captcha_selector` / `captcha_input_selector` | 图片验证码图片与输入框 |
| `trigger_selector` / `trigger_wait` | 弹窗触发按钮与等待毫秒 |
| `preflight_path` / `wait` / `preflight_wait` / `cloudflare_wait` / `turnstile_wait` / `auto_wait` / `reconnect_wait` / `solve_timeout` / `solve_count` / `solve2_wait` / `solve2_timeout` | 页面预检、签到后等待、Cloudflare 自动放行、Turnstile 复选框渲染等待（毫秒，5–60 秒）、Turnstile token 出现的等待时间、会话复用的空闲保留时间（毫秒，10–300 秒，默认 60 秒；`auto` 站点靠它把 solve 的会话留给第二段注入）、solve 找验证码的等待时间；`solve_count=0` 明确禁用 solve，`solve_count=1` 只解进站挑战，`solve_count=2` 时对两段挑战依次 solve，`solve2_wait` / `solve2_timeout` 分别控制两段之间的等待与第二段的解题目预算（缺省继承 `solve_timeout`） |
| `success_keywords` / `already_keywords` / `pending_keywords` | 文本关键词判定；`pending_keywords` 用于诊断仍停留在人机验证中的页面 |
| `absent_keywords` / `absent_status` | **静默成功**判定：返回页面里这些文字全都消失时，视为已完成（`absent_status` 可选 `success` / `already`，默认 `already`）。适用于「签到成功后不提示、只是把入口拿掉」的站点（如学校 BTSCHOOL 的「每日签到」按钮） |
| `success_json` | 形如 `{"field":"/state","values":["success"]}` 的 AJAX JSON 判定 |
| `strict_status_keywords` | `true` 时只认 `success_keywords`，忽略通用词 |

## 日志与安全

- Cookie、Token 均不会写入日志或签到结果；日志只记录站点名与状态
- 详情页按「站点 / 状态 / 时间 / 明细」展示，每个站点一张卡片（标题带历史条数），成功统一显示「签到成功」「今日已签到」「登录成功」
- 签到结果按天留存：`latest`、`history_YYYYMMDD`；自动清理，仅保留最近 30 天（`history_index` 保存索引）
