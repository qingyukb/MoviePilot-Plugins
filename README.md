# MoviePilot-Plugins

MoviePilot 第三方插件仓库。

## PT站点签到

PT 站点自动签到，支持 V2 / V3。签到走 HTTP 直连，遇验证码或 Cloudflare 改用 Browserless；凭证取「站点管理」里的 Cookie，也可填账号密码自动登录；签完读取站点「最近动向」。

主要功能：

- HTTP 直连签到（NexusPHP），默认路径 404 时自动查找签到入口
- 内置站点预设（观众、学校、憨憨、SUNNYPT、TTG、HDKylin、Piggo、AGSVPT、LuckPT 等），未覆盖的站点可写自定义规则
- 验证码 / Cloudflare / Turnstile 改用 Browserless；没写规则的站点签到失败时也会用它再试一次
- 详情页分「签到状态」「登录状态」，结果列显示奖励或失败原因
- 当天已签到成功的站点跳过，通知只统计成功与失败数量

### Browserless Token

不配置也能签普通站点。需要时：<https://browserless.io/signup/email?plan=free> 注册 → 打开 <https://browserless.io/account/> 复制 API Key → 填入插件设置，地址留 `https://production-sfo.browserless.io`。

## 馒头保活（M-Team）

馒头（M-Team）连续 40 天不登录会删号，而且只有真实登录才重置计时。插件每 20 天用
MoviePilot 自带的浏览器在家宽 IP 上真实登录一次（账号密码 + TOTP 全自动），顺带读取
分享率、上传下载与魔力。

主要功能：

- 真实浏览器登录保活（不靠 API ping），自动填验证码完成 2FA
- 「最近动向」执行前后对比，站点时间没变新就判失败，避免手动浏览造成误判
- API Key 自动取自「站点管理」里 m-team 站点的「令牌（API Key）」，无需手填
- 默认每 20 天执行一次（每天检查、不满间隔就跳过），可在配置里改
- 详情页显示分享率 / 魔力 / 上传下载与最近 10 次保活记录，分享率过低会提醒

### 怎么拿到 TOTP 密钥

TOTP 密钥是一串 32 位字符（如 `JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP`），只在**绑定二次验证时**显示一次。

**还没开两步验证时**

1. 登录 M-Team → 右上角头像 →「控制面板」→「安全設置」；
2. 点「兩步驗證 / 雙重認證」开启；
3. 二维码旁边会给出**密鑰 / Secret / 金鑰**，抄下来填进插件的「TOTP 密钥」；
4. 用手机认证器扫码完成绑定，页面上输入一次 6 位码确认。

**已经开过两次验证（站点不再显示密钥）**

- 解绑重绑：安全设置里关闭两步验证 → 重新开启 → 抄新密钥（推荐）；
- 或者从二维码内容取：`otpauth://totp/M-Team - TP:账号?secret=XXXX&issuer=...`，
  `secret=` 到 `&` 之间就是密钥；页面上若有「無法掃描 / 手動輸入」通常能直接看到明文；
- 或者干脆关掉两步验证，插件只走账号密码也能保活（安全性会下降）。

**确认密钥正确**

填好密钥 → 勾「保存后立即执行一次」→ 日志出现
`浏览器登录结果 - 登录成功（…）；已访问首页确认登录`，且「最近动向」变成刚才的时间即为成功。

密钥属于账号二次验证凭据，别外传；怀疑泄露就去解绑重绑换新的。NAS 系统时间偏差别超过 30 秒。

配置项与更多常见问题见 [插件 README](plugins.v2/mteamalive/README.md)。

## 安装

插件市场添加 `https://github.com/qingyukb/MoviePilot-Plugins`，刷新后搜索插件名；或把插件目录复制到 `/config/plugins/` 重启。

## 目录

```
├── package.json / package.v2.json / package.v3.json    市场清单
├── icons/                                             市场图标
├── plugins.v2/<插件>/  plugins.v3/<插件>/               插件源码（ptsitesignin / mteamkeepalive）
└── tests/v3/<插件>/                                    测试
```

配置项、内置站点、自定义规则见 [插件 README](plugins.v2/ptsitesignin/README.md)。
