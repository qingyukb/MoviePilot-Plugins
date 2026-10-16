# 馒头登录 + 保活（M-Team）

从 MoviePilot 的「站点管理」读取馒头（M-Team）凭证，**真实登录该账号并保活**，再用站点返回的
「最近动向」时间自检，兼容 MoviePilot V2 / V3。

## 它解决什么问题

馒头是前后端分离的：前端 `kp.m-team.cc`，后端 API `api.m-team.cc`。前端在每次登录 / 校验通过后会**立刻**调用：

```text
POST /api/member/updateLastBrowse
```

这个接口就是账号的「保活心跳」——它会把页面上的「最近动向」
（`GET /api/member/profile` → `data.memberStatus.lastBrowse`）刷新成当前时间。

只读 `profile`、不调用 `updateLastBrowse` 的插件，拿到的永远是「上一次别处登录」留下的时间：
它只能**看**，不能**保活**。本插件现在每一轮都会依次做两件事：

| 步骤 | 接口 | 作用 |
|---|---|---|
| 1 | `POST /api/member/updateLastBrowse` | **保活心跳**：把账号的「最近动向」刷成当前时间 |
| 2 | `GET /api/member/profile` | 读 `data.memberStatus.lastBrowse`，校验保活是否真的生效 |

所以保活正常时，「最近动向」应该永远是「刚刚 / 1 分钟前」。如果它仍然是几天前，插件会在详情页和通知里
直接写明是哪一步失败、以及怎么修。

只读接口返回 405 时插件会自动改用 POST 重试；除这两个接口外，插件不会再碰任何其它写接口。

## 凭证从哪来（不手填）

插件**没有任何手填凭证的配置项**：用户名、密码、OTP 密钥 / 动态码、邮箱验证码、Telegram 验证码、
手动 API Key 全部已删除。凭证只从 MoviePilot 的「站点管理」读取。站点表里可用的字段只有
`token` / `apikey` / `cookie`，插件按下面的顺序逐个尝试：

| 优先级 | 站点管理字段 | 发送的请求头 |
|---|---|---|
| 1 | Token | `authorization: <Token>` |
| 2 | Cookie 里的 `authorization` / `token` / `jwt` | `authorization: <解析出的值>` |
| 3 | API Key | `x-api-key: <API Key>` |
| 4 | API Key（长得像 JWT 时） | `authorization: <API Key>` |
| 5 | Cookie | `Cookie: <原始 Cookie>` |

第一个能让 `updateLastBrowse` 返回 `code == 0` 的方案胜出，并用于随后的 `profile` 请求。

> **推荐做法**：把站点「Token」填成浏览器登录后的 `authorization`
> （F12 → Network → 任意 `/api/` 请求 → 请求头里的 `authorization`），这样保活最稳；
> 站点「API Key」（站点「控制台 → 实验室 → 存取令牌」）作为备选。

### 站点是前后端分离的

馒头的前端（如 `kp.m-team.cc`）和后端 API（`api.<域名>`）**不是同一个域名**，所以插件不会直接请求站点地址，而是：

1. 先把站点地址推导成后端地址（`kp.m-team.cc` / `m-team.cc` → `https://api.m-team.cc/api`，
   `kp.m-team.io` → `https://api.m-team.io/api`）；
2. 如果该地址没有返回 M-Team 的 JSON（比如误将前端当成 API，返回 nginx 403 HTML），
   会自动依次回退 `api.m-team.cc` → `api.m-team.io` → `api2.m-team.cc`；
3. 若后端把某个接口改成只接受 POST，还会自动改用 POST 重试一次。

失败时日志会附上实际请求的 `METHOD URL`，方便定位到底命中了哪个地址。
判断「是否命中真正的 M-Team 接口」以原始响应体是否为带 `code` 字段的 JSON 为准，HTML 一律视为未命中。

## 使用

1. 在插件市场启用「馒头登录」；
2. 到 **设置 → 站点管理** 添加 / 编辑馒头站点（域名如 `m-team.cc`），把 Token 或 API Key 填进去；
3. 保存插件配置时勾上「保存后立即执行一次」，或在 Cron 周期到点时自动执行；
4. 插件详情页会同时显示「最近动向」和「保活心跳」状态，并保留历史记录。

保活需要周期性执行才有意义，建议把执行周期设成每天若干次，例如 `0 */6 * * *`。

## 配置项

| 配置 | 说明 |
|---|---|
| 启用插件 | 是否开启 Cron 调度 |
| 站点（来自站点管理） | 选择要用的站点；留空自动识别馒头站点 |
| 执行周期 | 5 位 Cron 表达式，默认 `0 9 * * *` |
| 保存后立即执行一次 | 保存配置后 3 秒在后台执行一轮，不阻塞设置页 |
| 通知模式 | 每次执行后通知 / 仅失败时通知 / 不通知 |
| API 基址（可选） | 留空按站点地址推导，馒头默认 `https://api.m-team.cc/api` |
| HTTP 超时 | 0 = 使用站点管理里的超时设置 |
| 使用系统代理 | 勾选则走系统代理；否则沿用站点管理里的代理设置 |

## 常见失败与修复

| 现象 | 原因 | 修复 |
|---|---|---|
| 保活失败：`Full authentication is required` | 站点管理里没有 Token，且 API Key 不能用于保活 | 在站点管理填写「Token」= 浏览器登录后的 `authorization` |
| 保活失败：`key 无效` / `令牌无效` | 站点管理里的凭证过期或写错 | 重新获取 Token / API Key 并更新站点管理 |
| 保活返回成功，但最近动向仍是几天前 | 站点缓存了该接口，或该凭证是只读令牌 | 换用浏览器 `authorization` 作为 Token 再试 |
| 站点管理里找不到馒头站点 | 站点未添加或域名不含 `m-team` / `mteam` / `馒头` | 在站点管理添加馒头站点，或在本插件「站点」下拉里手动选择 |
| 请求打到了 nginx 403 HTML | 用了前端地址当 API | 让「API 基址」留空以自动推导，或手动填 `https://api.m-team.cc/api` |

## 实现要点

- 三处都用 M-Team 自己的签名（对前端 bundle 逆向所得，已用真实请求验证）：

  ```text
  sign = base64(HMAC-SHA1(secret, "METHOD&path&timestamp_ms"))
  ```

  - `path` **包含** `/api` 前缀，例如 `/api/member/updateLastBrowse`；
  - 字段名沿用站点自身的拼写 `_sgin`（不是 `_sign`）；
  - GET 把 `_timestamp` / `_sgin` 放在 URL query，POST 放在 multipart 表单里；
  - 另外需要 `ts`（秒）、`version`、`webVersion`、`visitorId` 等请求头。

## 数据留存

- `latest`：最近一次检查结果（含保活状态）；
- `history`：历史记录，最多保留最近 30 天、200 条。

Token、API Key 与 Cookie 都不会写入日志，日志与详情页里只出现脱敏后的片段。