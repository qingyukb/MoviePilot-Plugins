# PT站点签到

MoviePilot 的 PT 站点自动签到插件，支持 V2 / V3。

签到默认走 HTTP 直连，站点带验证码或 Cloudflare 时改用 Browserless 完成。凭证取自「站点管理」，也可在站点附加信息里填账号密码，由插件在签到前自动登录换新 Cookie。签到后读取站点「最近动向」，写入登录状态表。

## 安装

市场添加 `https://github.com/qingyukb/MoviePilot-Plugins` 后搜索「PT站点签到」；或把 `plugins.v2/ptsitesignin` 复制到 `/config/plugins/` 重启。

## 主要功能

- HTTP 直连签到，默认 `/attendance.php`，404 时自动查找首页签到入口
- 内置站点预设（观众、学校、憨憨、SUNNYPT、TTG、HDKylin、Piggo、AGSVPT、LuckPT 等）按域名匹配，未覆盖的站点写自定义规则
- 验证码 / Cloudflare / Turnstile 改用 Browserless；没写规则的站点签到失败时也会用它再试一次
- 签到后读取「最近动向」，包含站点自带的相对时间
- 详情页分「签到状态」「登录状态」，结果列显示奖励或失败原因
- 当天已签到成功的站点跳过；通知只统计签到成功 / 登录成功 / 失败
- 支持 Cron 定时、并发、失败重试

## 配置项

| 选项 | 说明 |
| --- | --- |
| 启用插件 / 发送通知 | 总开关；通知只报数量，失败时附原因 |
| 保存后立即执行一次 / 清理本日缓存 | 立即跑一轮；清空当天记录，下一轮重签 |
| 执行周期 / 并发数 / 失败重试 / HTTP 超时 | `25 8 * * *` / 3 / 2 次 5 分钟 / 30 秒 |
| 读取「最近动向」 | 把动向写入登录状态表 |
| 签到站点 / 登录站点 | 支持「全部站点」，同时选中时一轮做完 |
| Browserless 地址 / Token | 处理验证码用 |
| 自定义站点规则 JSON | 未覆盖的站点规则 |

## 内置站点

观众（Turnstile）、AGSVPT（`POST /attendance.php`）、学校（首页「每日签到」）、憨憨（日历「签到」）、红豆饭 / PT时间 / 皇后等 NexusPHP 站点，其余站点走默认路径。奖励文案（魔力值、爆米花、憨豆）每次从页面读取。

## 自定义站点规则

```json
{"pt.example.com": {"engine": "http", "method": "POST", "path": "/attendance.php",
  "success": "签到成功|已签到", "bonus": "(获得\\s*[\\d,]+\\s*粒爆米花)"}}
```

常用字段：`engine`（http / browserless）、`method`、`path`、`submit_text`、`pre_click_selector`、`success`、`already`、`absent_keywords`、`success_on_click`、`bonus`、`activity_path`。`wait` 是点完之后等多久再读结果（毫秒），`cloudflare_wait` 是等待人机验证放行的秒数。

## 站点模块

图片验证码、答题选海报、API Token 这类规则写不了的站点，用插件目录里的 \sites/\ 放一个 Python 模块（一个站一个文件），
插件会按域名自动加载并优先于规则执行。内置三个：\hdsky.py\（OCR 验证码）、\	jupt.py\（豆瓣海报比对）、
\mteam.py\（API Token）。

`python
class Example:
    site_url = "example.com"

    def signin(self, site, ctx):
        page = ctx.page("https://example.com/")
        if "已签到" in page:
            return True, "今日已签到"
        return False, "签到失败"
`

模块里可用 \ctx.page(url)\ / \ctx.post(url, data=...)\ / \ctx.image(url)\ / \ctx.ocr(图片地址)\。

## Browserless Token

1. <https://browserless.io/signup/email?plan=free> 注册免费账号
2. 打开 <https://browserless.io/account/> 复制 API Key
3. 填入插件设置，地址留 `https://production-sfo.browserless.io`

地址也可用 `production-ams`、`production-lon`；免费额度只有浏览器请求才消耗。

**免费额度只有 1000 次/月**，可以几个账号轮着用：插件里有 Token 1～4 四个输入框，当前 Token 返回「额度不足（HTTP 429）」或「鉴权失败（401/403）」时会自动换下一个继续签，并记住这次能用的那个，下一轮从它开始。

## 常见问题

- **签到返回 404**：站点签到路径不是 `/attendance.php`，用规则里的 `path` 指定
- **Cookie 无效**：重新复制 Cookie，或在「登录站点」里填账号密码
- **卡在 Cloudflare**：填写 Browserless 地址与 Token
- **最近动向未读取到**：提示里会列出试过的页面，可用 `activity_path` 指定
- **想重新签到**：打开「清理本日缓存」保存一次

## 图标不显示怎么办

图标用文件名（如 `ptsignin.png`）时，MP 前端是从容器里的 `/public/plugin_icon/` 直接读的，
所以把插件目录里的同名 png 拷过去即可（拷一次，重启容器后仍在）：

```bash
docker cp ptsignin.png moviepilot-v3:/public/plugin_icon/
```

如果填的是完整 URL，则走 MP 的图片代理（`/api/v1/system/img/…`），**由容器出网去抓**；
容器访问不了 GitHub 时就会回退成默认的拼图图标，作者头像同理。

