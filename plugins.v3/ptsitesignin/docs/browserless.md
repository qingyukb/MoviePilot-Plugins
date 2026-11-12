# Browserless

[返回主页](../README.md)

Browserless 用于处理图片验证码、Cloudflare、Turnstile 等需要真实浏览器的站点。普通 HTTP 签到不会消耗 Browserless 额度。

## 快速配置

1. 打开 <https://browserless.io/signup/email?plan=free> 注册账号。
2. 打开 <https://browserless.io/account/> 获取 Token。
3. 在插件设置中填写 Token。
4. Browserless 地址保持默认：

```text
https://production-sfo.browserless.io
```

也可以使用：

- `https://production-ams.browserless.io`
- `https://production-lon.browserless.io`

## 私有部署

支持填写根地址：

```text
http://192.168.1.10:3000
```

插件会自动补全 `/stealth/bql`。也可以直接填写完整端点，例如：

```text
http://192.168.1.10:3000/chromium/bql
```

自建实例关闭鉴权时，Token 可以留空。

## 多 Token 轮换

插件支持 4 个 Browserless Token：

- 当前 Token 返回额度不足或鉴权失败时，自动尝试下一个。
- 成功使用的 Token 会被记住，下一轮优先复用。
- 免费额度每月 1000 次，只在实际调用浏览器时消耗。

## 注意事项

- Browserless 回退会把站点 Cookie 发送到配置的 Browserless 服务，请确认服务地址可信。
- 能否通过 Cloudflare / Turnstile 仍取决于站点风控和 Browserless 节点。
- Piggo 等对出口 IP 敏感的站点，云端 Browserless 会被提示异地登录，建议自建 Browserless 或使用本机 Playwright。
- 普通站点优先使用本机 Playwright 或 HTTP，不会主动消耗远程额度。

---

[返回主页](../README.md)
