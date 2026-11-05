# 使用与配置

[返回主页](../README.md)

## 安装

在 MoviePilot 插件市场添加：

```text
https://github.com/qingyukb/MoviePilot-Plugins
```

搜索「PT站点签到」并安装。也可以手动把 `plugins.v2/ptsitesignin` 或 `plugins.v3/ptsitesignin` 复制到 MoviePilot 的插件目录后重启。

## 配置项

| 选项 | 说明 |
| --- | --- |
| 启用插件 | 插件总开关 |
| 发送通知 | 发送签到结果通知 |
| 保存后立即执行一次 | 保存配置后延迟 3 秒执行一轮 |
| 清理本日缓存 | 清空当天记录，下一轮重新签到 |
| 执行周期 | 5 位 Cron 表达式 |
| 最大并发数 | 默认 6，可设 1～12；Browserless / Playwright 每个 Token 最多同时 2 个 |
| 失败重试次数 | `0` 表示关闭 |
| 失败后等待分钟数 | 仅失败站点等待后重试 |
| HTTP 超时 | 单次 HTTP 请求超时时间 |
| 读取最近动向 | 签到后读取并写入登录状态表 |
| 签到站点 / 登录站点 | 可选择全部站点或指定站点 |
| Browserless 地址 / Token | 用于处理验证码和人机验证 |
| 自定义站点规则 JSON | 覆盖或补充内置规则 |

## 执行说明

- HTTP 引擎优先，普通站点不会消耗 Browserless 额度。
- 大批量站点会分为 HTTP 与浏览器两组并行执行，慢速浏览器站点不会阻塞普通站点。
- 遇到验证码、Cloudflare 或 Turnstile 时，按站点规则决定是否使用浏览器。
- 当天已经签到成功的站点会跳过；失败站点仍会继续执行。
- 失败且没有同时勾选登录的站点不会额外读取最近动向，减少无效请求。
- 签到结果会保存到详情页和当天历史记录。
- 失败重试在后台执行，不会阻塞配置保存。

## 内置站点

当前包含以下类型的预设或专用处理：

- 观众：Turnstile
- 52PT：首页「做种会员签到处」Turnstile 签到（需站点入口可用）
- U2：showup 四选一签到（提交后回读确认）
- 彩虹岛：答题签到
- AGSVPT：表单签到
- 学校：首页签到入口
- 憨憨：日历签到
- 红豆饭、PT时间、皇后等 NexusPHP 站点
- SUNNYPT、TTG、HDKylin、LuckPT 等站点

未匹配到的站点会使用默认路径；如果默认路径不适用，请查看[自定义站点规则](site-rules.md)。

## 图标说明

如果插件图标不显示，把插件目录中的 `ptsignin.png` 放到 MoviePilot 的图标目录：

```bash
docker cp ptsignin.png moviepilot-v3:/public/plugin_icon/
```

使用完整 URL 作为图标时，由 MoviePilot 容器联网获取；容器无法访问外网时可能显示默认图标。

---

[返回主页](../README.md)
