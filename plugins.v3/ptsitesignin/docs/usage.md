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
| 保存后立即执行一次 | 保存配置后延迟 3 秒执行一轮，失败站点仍按设置重试 |
| 清理本日缓存 | 清空当天记录，下一轮重新签到 |
| 执行周期 | 5 位 Cron 表达式 |
| 最大并发数 | 默认 6，可设 1～12；Browserless / Playwright 每个 Token 最多同时 2 个 |
| 失败重试次数 | `0` 表示关闭 |
| 失败后等待分钟数 | 仅失败站点等待后重试 |
| HTTP 超时 | 单次 HTTP 请求超时时间 |
| 签到站点 / 登录站点 | 可选择全部站点或指定站点 |
| Browserless 地址 / Token | 用于处理验证码和人机验证 |
| 自定义站点规则 JSON | 覆盖或补充内置规则 |

## 执行说明

- HTTP 引擎优先，普通站点不会消耗 Browserless 额度。
- 大批量站点会分为 HTTP 与浏览器两组并行执行，慢速浏览器站点不会阻塞普通站点。
- 遇到验证码、Cloudflare 或 Turnstile 时，按站点规则决定是否使用浏览器。
- 当天已经签到成功的站点会跳过；失败站点仍会继续执行。
- 最近动向默认读取；失败且没有同时勾选登录的站点不会额外读取，减少无效请求。
- 签到结果会保存到详情页和当天历史记录。
- 失败通知按站点配置、Cookie/Token/登录状态、Cloudflare/WAF、验证码、网络、HTTP 和结果未确认等语义分类。
- 手动「保存后立即执行一次」与 Cron 使用同一套失败重试规则；重试交给宿主调度器，登记失败时回退到本地 Timer。

## 支持站点

当前已适配：

- 观众、不可躺（tangpt.top）、PlayLet（playlet.cc）、可鲁可撸（our.kelu.one）
- 海胆之家（haidan.cc）、HDWing（hdwing.org）、好多油/HDU（pt.hdupt.com、pt.upxin.net）
- 高清视界（hdarea.club）、YemaPT（yemapt.org）、ColaPT（colapt.com）
- 52PT、U2、瓷器、好学（hxpt.org）、RousiPro（rousi.pro）、彩虹岛、AGSVPT
- 学校、憨憨、红豆饭、PT时间、皇后（open.cd）、SUNNYPT、TTG、HDKylin、LuckPT 等

认证方式：Cookie、API Key。

验证类型：普通签到、图片验证码、Cloudflare / Turnstile。

未列出的站点可通过[自定义站点规则](site-rules.md)扩展。

---

[返回主页](../README.md)
