# PT站点签到

自动签到 PT 站点的小插件。支持站点登录、刷新 Cookie、读取「最近动向」，遇到验证码或 Cloudflare 会改用浏览器完成。

## 安装

- 插件市场：把 `https://github.com/qingyukb/MoviePilot-Plugins` 加到 MoviePilot 的「插件市场」里，刷新后搜索「PT站点签到」。
- 手动：把 `plugins.v2/ptsitesignin`（或 `plugins.v3/ptsitesignin`）复制到 MoviePilot 的 `/config/plugins/` 下，重启。

装完在插件页启用，填好站点就能跑。

## 用起来就三步

1. 在「站点管理」里把要签到的站加好，Cookie 直接复制浏览器里的；
2. 插件设置里勾「签到站点」，只想刷新登录状态的站勾到「登录站点」；
3. 需要过验证码 / Cloudflare 的站，再填 Browserless 的地址和 Token。

默认每天早上 8:25 跑一次，设置里可以改 Cron。

## 配置项

| 选项 | 说明 | 默认 |
| --- | --- | --- |
| 启用插件 | 总开关 | 关 |
| 发送通知 | 只报数量，有失败才附原因 | 开 |
| 保存后立即执行一次 | 保存后 3 秒跑一轮，已签到的站也会跑 | 关 |
| 清理本日缓存 | 清空今天的记录，下一轮重新签（用完自动关掉） | 关 |
| 执行周期 | 5 位 Cron | `25 8 * * *` |
| 并发数 | 同时跑几个站 | 3 |
| 失败重试 | 重试次数 / 间隔（分钟） | 2 / 5 |
| HTTP 超时 | 秒 | 30 |
| 读取「最近动向」 | 打开用户页或签到页，把动向写进「登录状态」 | 开 |
| 签到站点 / 登录站点 | 支持「全部站点」；两边都勾只跑一次，登录 + 签到 + 动向一起做 | 空 |
| Browserless 地址 / Token / 超时 | 过验证码用，地址填 `https://production-sfo.browserless.io` | 空 / 空 / 120 |
| 自定义站点规则 JSON | 内置预设不合适的站自己写规则 | 空 |

## 内置预设

大部分 NexusPHP 站不用配置，这些已经内置：

| 站点 | 做法 |
| --- | --- |
| 观众（audiences） | 浏览器过 Turnstile 后自动签到 |
| 红豆饭 / PT时间 / 皇后 等 NexusPHP | HTTP 直接 POST 签到接口 |
| AGSVPT | POST `/attendance.php`，页面上那个「立即签到」按钮 |
| 学校（btschool） | 首页的「每日签到」按钮，签完入口消失就算完成 |
| 憨憨（hhanclub） | 日历页点「签到」，按钮变「已领取」就算完成 |
| 其它 NexusPHP | 默认 `/attendance.php`；404 时自动去首页找签到入口、再试常见路径 |

签到奖励（魔力值、爆米花、憨豆、积分等）写进「结果」列，例如「获得 20 粒爆米花」「本次签到获得405个憨豆」。数值每次从页面抓，不是写死的。

## 结果怎么看

- **签到状态**：站点 / 状态 / 结果。结果列是实际到手的奖励，失败时是失败原因。
- **登录状态**：站点 / 状态 / 结果。结果列是站点上的「最近动向」，形如 `2026-10-17 10:58:16 ( < 1分前 )`。
- **通知**：一行「签到成功 N｜登录成功 N｜失败 N」，有失败才在下面列「站点：原因」。

## 自定义站点规则

内置预设不合适时，在「自定义站点规则 JSON」里按域名写：

```json
{
  "pt.example.com": {
    "engine": "http",
    "method": "POST",
    "path": "/attendance.php",
    "success": "签到成功|已签到",
    "bonus": "(获得\\s*[\\d,]+\\s*粒爆米花)"
  }
}
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `engine` | `http`（默认）或 `browserless` |
| `method` / `path` | 请求方式和签到路径 |
| `submit_text` | 浏览器引擎点的按钮文字，如「立即签到」 |
| `success` / `already` | 判定成功 / 已签到的正则 |
| `success_keywords` / `already_keywords` | 用关键词判定（浏览器引擎） |
| `absent_keywords` | 这些字消失了就算完成（签完不给提示的站） |
| `success_on_click` | 点到按钮就算成功 |
| `bonus` | 奖励文案正则，取第一个捕获组 |
| `activity_path` | 「最近动向」在哪个页面 |

抓规则：浏览器登录目标站、打开签到页，F12 控制台执行 `tools/collect-rule-snippet.js`，会输出选择器和文案。

## 常见问题

**签到失败，提示 HTTP 404？**
那个站的签到路径不是 `/attendance.php`。插件会自动去首页找入口，还找不到就在规则里用 `path` 指定浏览器里的真实地址。

**提示 Cookie 无效？**
Cookie 过期了，重新复制一次。勾在「登录站点」的站可以填账号密码让它自己换 Cookie，部分站有验证码只能手填。

**一直卡在 Cloudflare / 验证码？**
填 Browserless 地址和 Token（[browserless.io](https://www.browserless.io/) 注册拿），默认 `https://production-sfo.browserless.io` 实测最快。

**「最近动向」显示未读取到？**
提示里会列出试过哪些页面。确认页面后，在站点管理的「附加信息」里写 `{"activity_path": "/userdetails.php?id=你的uid"}` 指定页面。

**今天想重跑？**
打开「清理本日缓存」保存一次，下一轮全部重签；也可以打开「保存后立即执行一次」强制跑一轮。

## 日志

签到、登录、读取动向都有日志，代理和 Cookie 的敏感部分打了码。贴日志求助前也建议先扫一眼。
