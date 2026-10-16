# 馒头登录（M-Team）

从 MoviePilot 的「站点管理」读取馒头（M-Team）凭证，读取站点**真实**的「最近动向」时间，兼容 MoviePilot V2 / V3。

## 它解决什么问题

M-Team 的网页前端在登录成功后会立刻调用 `/member/updateLastBrowse`，把「最近动向」刷新成当前时间。
因此用浏览器或普通脚本登录后，看到的「最近动向」其实是刚刚的登录动作，而不是账号真实的最后活跃时间。

本插件只读取一个接口：

| 接口 | 作用 |
|---|---|
| `GET /api/member/profile` | 读取 `data.memberStatus.lastBrowse`（即页面上的「最近动向」） |

它**从不**调用 `/member/updateLastBrowse`，所以拿到的是账号真实的最后活跃时间。

## 凭证从哪来

不需要在插件里手填账号密码——凭证默认直接取自 MoviePilot 的「站点管理」：

1. 先在 **设置 → 站点管理** 里添加馒头站点（域名如 `m-team.cc`）；
2. 在站点里填写 **API Key**（站点「控制台 → 实验室 → 存取令牌」创建）；
3. 打开本插件，「站点」留空即自动识别馒头站点：API Key、User-Agent、代理与超时都会从站点管理读取，API 基址按站点地址自动推导（`kp.m-team.cc` → `https://api.m-team.cc/api`）。

> 实测 `x-api-key` 请求头无需额外签名即可读取 `/api/member/profile`；MoviePilot 官方的馒头索引器也是这么用的。
> 只填 Cookie 不行——Cookie 只能用于网页，读「最近动向」需要 API Key（或账号密码登录）。

### 站点是前后端分离的

馒头的前端（如 `kp.m-team.cc`）和后端 API（`api.<域名>`）**不是同一个域名**。
所以插件不会直接请求站点地址，而是：

1. 先把站点地址推导成后端地址（`kp.m-team.cc` / `m-team.cc` → `https://api.m-team.cc/api`，`m-team.io` → `https://api.m-team.io/api`）；
2. 如果该地址没有返回 M-Team 的 JSON（比如误将前端当成 API，返回 nginx 403 HTML），会自动依次回退 `api.m-team.cc` → `api.m-team.io` → `api2.m-team.cc`；
3. 若后端把该只读接口改成只接受 POST，还会自动改用 POST 重试一次。

失败时日志会附上实际请求的 `METHOD URL`，方便定位到底命中了哪个地址。

### 可选：手动覆盖

- 站点管理里没有馒头站点、或没填 API Key 时，也可以在插件里填「手动 API Key」；
- 想用账号密码登录时，填「M-Team 用户名 / 密码」，并支持 OTP 密钥自动补动态码、邮箱 / Telegram 验证码。

凭证优先级：**手动 API Key > 站点管理 API Key > 账号密码登录**。

## 使用

1. 在插件市场启用「馒头登录」；
2. 到 **设置 → 站点管理** 确认馒头站点已填好 API Key；
3. 保存后点「保存后立即执行一次」，或在 Cron 周期到点时自动执行；
4. 插件详情页会显示「最近动向」的绝对时间与距今时长，并保留历史记录。

### 两步验证（2FA）

仅在使用账号密码登录时才有意义。站点返回 `code = 1001` 要求补充动态码时，插件按以下顺序处理：

- 填了 **OTP 密钥**（验证器里的 Base32 密钥，例如 `JBSWY3DPEHPK3PXP`）：插件自动算出 6 位动态码再提交一次；
- 只填了**动态码**：用该动态码重试一次；
- 站点要求**邮箱验证码**或 **Telegram 验证码**时，分别填「邮箱验证码」/「Telegram 验证码」后重试。

> 这些一次性验证码有效期很短，通常只在手动「立即执行一次」时有用；定时任务建议用 OTP 密钥或 API Key。

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
| 用户名 / 密码 | 可选；站点管理里没有 API Key 时才需要 |
| OTP 密钥 / 动态码 / 邮箱验证码 / Telegram 验证码 | 账号密码登录时的两步验证 |
| 手动 API Key | 可选；填了则覆盖站点管理里的 API Key |

## 实现要点

- 主路径直接用站点管理里的 API Key 读 `/api/member/profile`（`x-api-key` 请求头），无需签名；
- 接口地址支持多候选自动回退，并在「请求方法不支持」时自动回退 POST；
- 判断是否命中真正的 M-Team 接口，以「原始响应体是否为带 `code` 字段的 JSON」为准，HTML 等非 JSON 响应一律视为未命中；
- 账号密码登录路径使用 M-Team 的自定义签名（逆向所得，已用真实请求验证）：

  ```text
  sign = base64(HMAC-SHA1(secret, "METHOD&path&timestamp_ms"))
  ```

  - `path` **包含** `/api` 前缀，例如 `/api/login`；
  - 表单字段名是站点自身的拼写 `_sgin`（不是 `_sign`）；
  - GET 请求把 `_timestamp` / `_sgin` 放在 URL query，POST 请求放在 multipart 表单里；
  - 另外需要 `ts`（秒）、`version`、`webVersion` 等请求头；
  - 登录成功后 `authorization` 只出现在**响应头**里，插件用它请求后续的 `/member/profile`。

## 数据留存

- `latest`：最近一次检查结果；
- `history`：历史记录，最多保留最近 30 天、200 条。

密码、API Key 与 Token 都不会写入日志（日志里只出现脱敏后的片段）。
