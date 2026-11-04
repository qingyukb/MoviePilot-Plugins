# 常见问题

[返回主页](../README.md)

## 签到返回 404

说明默认签到路径不适用于该站点。请在自定义规则中配置正确的 `path`。

查看：[自定义站点规则](site-rules.md)

## Cookie 无效

- 重新复制站点 Cookie。
- 确认 Cookie 没有过期。
- 或者在「登录站点」中配置账号密码，让插件重新登录。

## 卡在 Cloudflare / Turnstile

1. 确认 Browserless 地址和 Token 已配置。
2. 检查 Browserless 账户额度。
3. 尝试更换 Browserless 区域节点。
4. 站点风控较严时，即使浏览器成功打开也未必能通过验证。

查看：[Browserless](browserless.md)

## 最近动向未读取到

插件会在失败原因中列出尝试过的页面。可以配置 `activity_path` 指定正确的用户页或动向页。

## 想重新签到

打开「清理本日缓存」并保存一次配置，下一轮会重新执行。

## 插件图标不显示

把 `ptsignin.png` 放入 MoviePilot 的 `/public/plugin_icon/` 目录：

```bash
docker cp ptsignin.png moviepilot-v3:/public/plugin_icon/
```

## 同时勾选签到站点和登录站点

同一个站点同时被选中时，插件会在一轮中完成登录、签到和最近动向读取，不会重复执行两遍。

---

[返回主页](../README.md)
