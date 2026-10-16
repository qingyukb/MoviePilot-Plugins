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

**注意：站点只认真实登录（`authorization` / JWT）的心跳。** 实测用 API Key（`x-api-key`）调
`updateLastBrowse` 一定返回 `401 Full authentication is required`——也就是说：

```text
通过 API Key 调用接口 = 不算活跃，站点不会把你的「最近动向」往前刷。
```

所以本插件每一轮都会走完整的真实登录链路：

| 步骤 | 接口 | 作用 |
|---|---|---|
| 1 | 复用站点管理里的 `authorization`（JWT）；过期或没有时 `POST /api/login` | **真实登录**，拿到新的 `authorization` |
| 2 | `POST /api/member/updateLastBrowse`（带 `authorization`） | **保活心跳**：把账号的「最近动向」刷成当前时间 |
| 3 | `GET /api/member/profile` | 读 `data.memberStatus.lastBrowse`，校验保活是否真的生效 |

保活正常时，「最近动向」应该永远是「刚刚 / 1 分钟前」。如果它仍然是几天前，说明这一轮的真实登录
没有生效，插件会在详情页和通知里写明是哪一步失败、以及怎么修，同时把账号**真实的**最近动向展示出来。

只读接口返回 405 时插件会自动改用 POST 重试；除上面这几个接口外，插件不会再碰任何其它写接口。

## 凭证从哪来（不手填）

插件**没有任何手填凭证的配置项**：用户名、密码、OTP 密钥 / 动态码、邮箱验证码、Telegram 验证码、
手动 API Key 全部已删除。凭证只从 MoviePilot 的「站点管理」里读取，按下面的优先级尝试：

| 优先级 | 站点管理里的位置 | 发送方式 | 能保活？ |
|---|---|---|---|
| 1 | 站点「Token」= 浏览器登录后的 `authorization`（JWT） | `authorization: <Token>` | ✅ 能 |
| 2 | 站点「Cookie」里能解析出 `authorization` / `token` / `jwt` | `authorization: <解析出的值>` | ✅ 能 |
| 3 | 站点「**备注(note)**」里的账号密码 JSON | `POST /api/login` 换新 `authorization` | ✅ 能 |
| 4 | 站点「API Key」 | `x-api-key: <API Key>` | ❌ **只读，不能保活** |

也就是说：**保活只靠 1 / 2 / 3 三条**。API Key 只能读取数据（第 4 条），站点不会因为 API Key 调用
而认为你活跃。

> **推荐做法（二选一）**
>
> * **A. 填 Token**：浏览器登录 `kp.m-team.cc` 后按 F12 → Application → Local Storage →
>   找到 `auth` 的值，把它填到 **站点管理**里该站点的「Token」；
> * **B. 填备注账号密码**：在**站点管理**里给馒头站点写「备注(note)」JSON：
>
>   ```json
>   {"username": "你的账号", "password": "你的密码", "otp_secret": "可选的 Base32 二步验证密钥"}
>   ```
>
>   插件会用它在 `POST /api/login` 里**真实登录**，把响应头里的 `authorization` 用于保活；
>   站点开了二步验证时，`otp_secret` 会自动算出 6 位动态码（也可以直接给临时的 `otp_code`）。
>   需要邮箱验证码时同理给 `email_code`。
>
> 两种方式都可以只留一种；Token 没填 / 过期时插件会自动回退到备注里的账号密码重新登录。

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
2. 到 **设置 → 站点管理** 添加 / 编辑馒头站点（域名如 `m-team.cc`），把 Token（推荐）或备注账号密码填进去；
3. 保存插件配置时勾上「保存后立即执行一次」，或在 Cron 周期到点时自动执行；
4. 插件详情页会同时显示「最近动向」「保活心跳」与「是否走了真实登录」，并保留历史记录。

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
| 保活失败：`Full authentication is required` | 站点管理里只填了 API Key，而 API Key 不能用于保活 | 把站点「Token」填成浏览器登录后的 `authorization`（JWT），或在备注里填账号密码 |
| 保活失败：`key 无效` / `令牌无效` | 站点管理里的凭证过期或写错 | 重新获取 `authorization` 并更新站点管理，或改用备注账号密码自动登录 |
| 真实登录失败：需要二步验证（code=1001） | 站点要求 OTP | 在站点「备注(note)」里补 `otp_secret`（Base32 密钥）或临时的 `otp_code` |
| 真实登录失败：需要邮箱验证码（code=1002） | 站点要求邮箱验证码 | 在站点「备注(note)」里补 `email_code` |
| 真实登录失败：账号或密码不正确 | 备注(note) 里的账号密码写错 | 修正备注 JSON 里的 `username` / `password` |
| 保活返回成功，但最近动向仍是几天前 | 站点缓存了该接口，或该凭证没有保活权限 | 换用浏览器 `authorization` 作为 Token，或补上备注账号密码让插件真实登录 |
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

- 真实登录走后端自己的接口：`POST /api/login`（multipart：`username` / `password`，需要时再加
  `otpCode` / `emailCode` / `tgCode` / `turnstile`），成功的 `authorization` **只在响应头里**返回，
  与浏览器前端的行为一致。

## 数据留存

- `latest`：最近一次检查结果（含保活状态、是否走了真实登录）；
- `history`：历史记录，最多保留最近 30 天、200 条。

Token、API Key、Cookie、账号密码与 JWT 都不会写入日志，日志与详情页里只出现脱敏后的片段。
