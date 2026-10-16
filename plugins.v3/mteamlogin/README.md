# 馒头登录（M-Team）

登录 [M-Team（馒头）](https://kp.m-team.cc/) 并读取站点**真实**的「最近动向」时间，兼容 MoviePilot V2 / V3。

## 它解决什么问题

M-Team 的网页前端在登录成功后会立刻调用 `/member/updateLastBrowse`，把「最近动向」刷新成当前时间。
因此用浏览器或普通脚本登录后，看到的「最近动向」其实是刚刚的登录动作，而不是账号真实的最后活跃时间。

本插件只调用两个接口：

| 接口 | 作用 |
|---|---|
| `POST /api/login` | 账号密码登录，拿到响应头里的 `authorization` |
| `GET /api/member/profile` | 读取 `data.memberStatus.lastBrowse`（即页面上的「最近动向」） |

它**从不**调用 `/member/updateLastBrowse`，所以拿到的是账号真实的最后活跃时间。

## 使用

1. 在插件市场启用「馒头登录」；
2. 填写 M-Team 用户名与密码；
3. 保存后点「保存后立即执行一次」，或在 Cron 周期到点时自动执行；
4. 插件详情页会显示「最近动向」的绝对时间与距今时长，并保留历史记录。

### 两步验证（2FA）

站点在开启两步验证时会返回 `code = 1001`，要求补充动态码。插件按以下顺序处理：

- 填了 **OTP 密钥**（验证器里的 Base32 密钥，例如 `JBSWY3DPEHPK3PXP`）：插件自动算出 6 位动态码再提交一次；
- 只填了**动态码**：用该动态码重试一次；
- 站点要求**邮箱验证码**或 **Telegram 验证码**时，分别填「邮箱验证码」/「Telegram 验证码」后重试。

> 这些一次性验证码有效期很短，通常只在手动「立即执行一次」时有用；定时任务建议用 OTP 密钥自动生成。

### API Key（更稳妥的方式）

如果站点的人机验证（Cloudflare Turnstile）拦住了直连登录，可以改用 API Key：

1. 登录站点 → 控制台 → 实验室 → 存取令牌，创建 API Key；
2. 填入插件的「API Key」字段（填了以后优先使用，跳过账号登录，也不用再填密码）。

插件会用 `x-api-key` 请求头读取用户资料，行为与账号登录一致。

## 配置项

| 配置 | 说明 |
|---|---|
| 启用插件 | 是否开启 Cron 调度 |
| 执行周期 | 5 位 Cron 表达式，默认 `0 9 * * *` |
| 保存后立即执行一次 | 保存配置后 3 秒在后台执行一轮，不阻塞设置页 |
| 通知模式 | 每次执行后通知 / 仅失败时通知 / 不通知 |
| API 基址 | 默认 `https://api.m-team.cc/api`，被区域限制时可换 `https://api.m-team.io/api` |
| 用户名 / 密码 | M-Team 账号（密码只存在 MoviePilot 本地配置里） |
| OTP 密钥 / 动态码 | 两步验证时使用 |
| 邮箱验证码 / Telegram 验证码 | 站点要求对应验证时使用 |
| API Key | 可选的替代凭证 |
| HTTP 超时 / 使用系统代理 | 网络相关配置 |

## 实现要点

M-Team API 使用自定义签名，签名算法（逆向所得，已用真实请求验证）：

```text
sign = base64(HMAC-SHA1(secret, "METHOD&path&timestamp_ms"))
```

- `path` **包含** `/api` 前缀，例如 `/api/login`；
- 表单字段名是站点自身的拼写 `_sgin`（不是 `_sign`）；
- GET 请求把 `_timestamp` / `_sgin` 放在 URL query，POST 请求放在 multipart 表单里；
- 另外需要 `ts`（秒）、`version`、`webVersion` 等请求头；
- 登录成功后 `authorization` 只出现在**响应头**里，插件把它用于后续的 `/member/profile` 请求。

## 数据留存

- `latest`：最近一次检查结果；
- `history`：历史记录，最多保留最近 30 天、200 条。

密码、API Key 与 Token 都不会写入日志（日志里只出现脱敏后的片段）。
