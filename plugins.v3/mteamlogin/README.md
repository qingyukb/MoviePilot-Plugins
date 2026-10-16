# 馒头登录 + 保活（M-Team）

本插件的凭证**全部在插件配置页手填**，不再读取 MoviePilot「站点管理」。插件会用真实登录态
调用馒头（M-Team）的保活心跳，再用站点返回的「最近动向」时间自检，兼容 MoviePilot V2 / V3。

## 它解决什么问题

馒头是前后端分离的：前端 `kp.m-team.cc`，后端 API `api.m-team.cc`。前端在每次登录 / 校验通过后会**立刻**调用：

```text
POST /api/member/updateLastBrowse
```

这个接口就是账号的「保活心跳」——它会把页面上的「最近动向」
（`GET /api/member/profile` → `data.memberStatus.lastBrowse`）刷新成当前时间。

**注意：站点只认真实登录（`authorization` / JWT）的心跳。** 实测用 API Key（`x-api-key`）调
`updateLastBrowse` 一定返回 `401 Full authentication is required`：

```text
通过 API Key 调用接口 = 不算活跃，站点不会把你的「最近动向」往前刷。
```

所以插件每一轮都走完整的真实登录链路：

| 步骤 | 接口 | 作用 |
|---|---|---|
| 1 | 复用配置里的 `authorization`；过期或没填时 `POST /api/login` | **真实登录**，拿到新的 `authorization` |
| 2 | `POST /api/member/updateLastBrowse`（带 `authorization`） | **保活心跳**：把账号的「最近动向」刷成当前时间 |
| 3 | `GET /api/member/profile` | 读 `data.memberStatus.lastBrowse`，校验保活是否真的生效 |

保活正常时，「最近动向」应该永远是「刚刚 / 1 分钟前」。如果它仍然是几天前，说明这一轮的真实登录
没有生效，插件会在详情页和通知里写明是哪一步失败、以及去插件配置里补哪一项，同时把账号**真实的**
最近动向展示出来。

只读接口返回 405 时插件会自动改用 POST 重试；除上面这几个接口外，插件不会再碰任何其它写接口。

## 需要什么就填什么

在 MoviePilot 的插件配置页按下面的优先级填写，至少要有一种能真实登录的凭证：

| 优先级 | 配置项 | 发送方式 | 能保活？ |
|---|---|---|---|
| 1 | **账号 + 密码**（推荐） | `POST /api/login` 换新 `authorization` | ✅ 能 |
| 2 | 二步验证密钥 `otp_secret` / 临时动态码 `otp_code` | 登录时补 `otpCode` | ✅ 能（配合账号密码） |
| 3 | 邮箱验证码 `email_code` / Telegram 验证码 `tg_code` | 登录时补对应验证码 | ✅ 能（配合账号密码） |
| 4 | `authorization`（Token） | `authorization: <值>` | ✅ 能 |
| 5 | Cookie（能解析出 `authorization` / `token` / `jwt`） | `authorization: <解析出的值>` | ✅ 能 |
| 6 | API Key | `x-api-key: <值>` | ❌ **只读，不能保活** |

> **推荐做法**
>
> * **A. 填账号 + 密码**：插件会用它们在 `POST /api/login` 里真实登录，把响应头里的
>   `authorization` 用于保活。开启了二步验证就再填 `otp_secret`（Base32 密钥，插件会自动算出
>   6 位动态码）；临时动态码也可以直接填 `otp_code`。需要邮箱 / Telegram 验证码时填
>   `email_code` / `tg_code`。
> * **B. 直接填 `authorization`**：浏览器登录 `kp.m-team.cc` 后按 F12 → Application →
>   Local Storage → 找到 `auth` 的值，填到插件配置的 `authorization`。JWT 过期后需要重新获取，
>   或者同时填账号密码让插件自动登录。

两种方式可以同时填：Token 能用就直接复用，过期时插件会自动回退到账号密码重新登录。

## 站点是前后端分离的

馒头的前端（如 `kp.m-team.cc`）和后端 API（`api.<域名>`）不是同一个域名，所以插件请求的是后端地址：

1. 默认使用 `https://api.m-team.cc/api`；
2. 如果你的镜像不同，可在「API 基址」里手动填写后端地址，例如 `https://api.m-team.io/api`；
   **不要填 `kp.m-team.cc` 这类前端地址**；
3. 当前地址没有返回 M-Team 的结构化 JSON（比如误将前端当成 API，返回 nginx 403 HTML）时，
   插件会自动依次回退 `api.m-team.cc` → `api.m-team.io` → `api2.m-team.cc`；
4. 若后端把某个接口改成只接受 POST，还会自动改用 POST 重试一次。

失败时日志会附上实际请求的 `METHOD URL`，方便定位到底命中了哪个地址。

## 使用

1. 在插件市场启用「馒头登录」；
2. 打开插件配置页，填写账号 + 密码（推荐），或填写浏览器登录后的 `authorization`；
3. 保存配置时勾上「保存后立即执行一次」，或在 Cron 周期到点时自动执行；
4. 插件详情页会同时显示「最近动向」「保活心跳」与「是否走了真实登录」，并保留历史记录。

保活需要周期性执行才有意义，建议把执行周期设成每天若干次，例如 `0 */6 * * *`。

## 配置项

| 配置 | 说明 |
|---|---|
| 启用插件 | 是否开启 Cron 调度 |
| 账号 / 密码 | 推荐填写；插件会真实登录并保活 |
| 二步验证密钥 | 可选的 Base32 密钥，插件自动算动态码 |
| 动态码 / 邮箱验证码 / Telegram 验证码 | 站点要求二次验证时填写 |
| authorization | 浏览器真实登录后的 JWT；可直接保活 |
| API Key | 只读凭证，不能保活 |
| Cookie | 可选；能解析出 authorization 时也可保活 |
| API 基址（可选） | 留空使用 `https://api.m-team.cc/api` |
| 执行周期 | 5 位 Cron 表达式，默认 `0 9 * * *` |
| 保存后立即执行一次 | 保存配置后 3 秒后台执行一轮，不阻塞设置页 |
| 通知模式 | 每次执行后通知 / 仅失败时通知 / 不通知 |
| HTTP 超时 | 0 = 20 秒 |
| 使用系统代理 | 勾选则走系统代理 |

## 常见失败与修复

| 现象 | 原因 | 修复 |
|---|---|---|
| 保活失败：`Full authentication is required` | 只填了 API Key，而 API Key 不能用于保活 | 在插件配置里填账号 + 密码，或填浏览器登录后的 `authorization` |
| 保活失败：`key 无效` / `令牌无效` | 配置里的凭证过期或写错 | 重新获取 `authorization` 并更新，或改用账号 + 密码自动登录 |
| 真实登录失败：需要二步验证（code=1001） | 站点要求 OTP | 在插件配置里补 `otp_secret`（Base32 密钥）或临时的 `otp_code` |
| 真实登录失败：需要邮箱验证码（code=1002） | 站点要求邮箱验证码 | 在插件配置里补 `email_code` |
| 真实登录失败：账号或密码不正确 | 账号密码写错 | 检查插件配置里的 `username` / `password` |
| 保活返回成功，但最近动向仍是几天前 | 站点缓存了该接口，或该凭证没有保活权限 | 换用浏览器 `authorization`，或补上账号密码让插件真实登录 |
| 请求打到了 nginx 403 HTML | 用了前端地址当 API | 在「API 基址」里填 `https://api.m-team.cc/api` |

## 实现要点

- 三处接口都用 M-Team 自己的签名（对前端 bundle 逆向所得，已用真实请求验证）：

  ```text
  sign = base64(HMAC-SHA1(secret, "METHOD&path&timestamp_ms"))
  ```

  - `path` **包含** `/api` 前缀，例如 `/api/member/updateLastBrowse`；
  - 字段名沿用站点自身的拼写 `_sgin`（不是 `_sign`）；
  - GET 把 `_timestamp` / `_sgin` 放在 URL query，POST 放在 multipart 表单里；
  - 另外需要 `ts`（秒）、`version`、`webVersion`、`visitorId` 等请求头。

- 真实登录走后端自己的接口：`POST /api/login`（multipart：`username` / `password`，需要时再加
  `otpCode` / `emailCode` / `tgCode` / `turnstile`），成功的 `authorization` **只在响应头里**返回，
  与浏览器前端的行为一致。

## 数据留存

- `latest`：最近一次检查结果（含保活状态、是否走了真实登录）；
- `history`：历史记录，最多保留最近 30 天、200 条。

Token、API Key、Cookie、账号密码与 JWT 都不会写入日志，日志与详情页里只出现脱敏后的片段。