# PT站点签到

MoviePilot 的 PT 站点自动签到插件，支持 V2 / V3。

签到默认走 HTTP 直连，支持 Cookie、账号密码和 API Key / Token 凭证；站点带验证码或 Cloudflare 时可按规则改用 Browserless 完成。签到后读取站点「最近动向」，写入登录状态表。

## 安装

市场添加 `https://github.com/qingyukb/MoviePilot-Plugins` 后搜索「PT站点签到」；或把 `plugins.v2/ptsitesignin` 复制到 `/config/plugins/` 重启。

## 主要功能

- Cookie 站点 HTTP 直连签到，默认 `/attendance.php`，404 时自动查找首页签到入口
- 内置站点预设（观众、学校、憨憨、SUNNYPT、TTG、HDKylin、AGSVPT、LuckPT 等）按域名匹配，未覆盖的站点写自定义规则
- API Key / Token 站点按显式固定端点完成登录验证和签到；不探测路径、不访问首页、不加载站点模块、不回退浏览器
- 需要交互的人机验证优先用本机 Playwright；本机流程失败或缺少浏览器助手时，可按规则回退到 Browserless 处理验证码 / Cloudflare
- 签到后读取「最近动向」，包含站点自带的相对时间
- 详情页分「签到状态」「登录状态」，只展示当前勾选站点，结果列显示奖励或失败原因
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

观众（Turnstile）、彩虹岛（答题签到）、AGSVPT（`POST /attendance.php`）、学校（首页「每日签到」）、憨憨（日历「签到」）、红豆饭 / PT时间 / 皇后等 NexusPHP 站点，其余站点走默认路径。奖励文案（魔力值、爆米花、憨豆）每次从页面读取。

## 自定义站点规则

```json
{"pt.example.com": {"engine": "http", "method": "POST", "path": "/attendance.php",
  "success": "签到成功|已签到", "bonus": "(获得\\s*[\\d,]+\\s*粒爆米花)"}}
```

常用字段：`engine`（http / browserless）、`method`、`path`、`submit_text`、`pre_click_selector`、`success`、`already`、`absent_keywords`、`success_on_click`、`bonus`、`activity_path`。`wait` 是点完之后等多久再读结果（毫秒），`cloudflare_wait` 是等待人机验证放行的秒数。

### API Key / Token 登录与签到

站点管理里已保存 `ApiKey` 或 `Token` 时，可在自定义规则中显式声明 API 流程：

```json
{
  "api.example.com": {
    "auth": "api_key",
    "api_key_source": "apikey",
    "api_key_header": "Authorization",
    "api_key_prefix": "Bearer ",
    "credential_check_path": "/api/user/profile",
    "credential_check_success": "success",
    "path": "/api/checkin",
    "method": "POST",
    "json": {},
    "success": "签到成功",
    "already": "今日已签到",
    "activity_path": "/api/user/activity"
  }
}
```

字段说明：

- `auth`：固定为 `api_key`；声明后强制使用 HTTP，并禁用 Browserless 回退。
- `api_key_source`：`apikey`（默认）或 `token`，对应站点管理中的字段。
- `api_key_header` / `api_key_prefix`：默认 `Authorization` + `Bearer `；使用 `X-API-Key` 时默认不加前缀。
- `credential_check_path`：登录校验端点，只测试登录时可只配置此项；`credential_check_method` 默认 `GET`，`credential_check_success` 可写正则或固定文本。
- `path`：签到端点，只签到时可只配置此项；`method` 支持 `GET / POST`，`data` 作为表单提交，`json` 作为 JSON body，GET 时两者都会作为查询参数。
- `success` / `already`：从响应中识别签到成功和今日已签到的正则。
- `activity_path`：可选的最近动向端点；未配置时不会访问首页。

API Key 模式只访问上述显式配置的端点，不探测路径、不访问首页、不加载站点模块，也不调用本机 Playwright / Browserless。纯 Key 站点没有声明 `auth: "api_key"` 时不会发起任何签到或登录请求；请求异常和结果中的 Key 会自动打码。

### 彩虹岛选择题签到

彩虹岛（`ptchdbits.co`）已内置 `/bakatest.php` 答题流程。未配置答案时会随机选择；配置固定答案时，值必须与页面 `choice[]` 的 `value` 一致：

```json
{"ptchdbits.co": {"quiz_answer": "正确选项值"}}
```

多题可按页面题目文本映射：

```json
{"ptchdbits.co": {"quiz_answers": {"题目文本": "正确选项值"}}}
```

插件提交后会重新 GET `/bakatest.php`，只有读到「今天已经签过到了」才判定成功；页面头部的魔力值不会被误判为签到成功。

## 站点模块

图片验证码、答题选海报这类规则写不了的站点，用插件目录里的 sites/ 放一个 Python 模块（一个站一个文件），
插件会按域名自动加载并优先于规则执行。内置四个：hdsky.py（OCR 验证码）、tjupt.py（豆瓣海报比对）、
htpt.py（海棠PT 表单签到）、farm.py（自由农场滑块直连）。

```python
class Example:
    site_url = "example.com"

    def signin(self, site, ctx):
        page = ctx.page("https://example.com/")
        if "已签到" in page:
            return True, "今日已签到"
        return False, "签到失败"
```

模块里可用 `ctx.page(url)` / `ctx.post(url, data=...)` / `ctx.image(url)` / `ctx.ocr(图片地址)`。

## Browserless Token

1. <https://browserless.io/signup/email?plan=free> 注册免费账号
2. 打开 <https://browserless.io/account/> 复制 API Key
3. 填入插件设置，地址留 `https://production-sfo.browserless.io`

地址也可用 `production-ams`、`production-lon`；免费额度只有浏览器请求才消耗。

**私有部署**也支持：地址填 `http://192.168.1.10:3000` 这种根地址会自动补 `/stealth/bql`；也可以直接填完整端点（以 `/bql` 结尾就原样使用，例如自建的 `/chromium/bql`）。自建实例关掉了鉴权时，Token 留空即可。

**免费额度只有 1000 次/月**，可以几个账号轮着用：插件里有 Token 1～4 四个输入框，当前 Token 返回「额度不足（HTTP 429）」或「鉴权失败（401/403）」时会自动换下一个继续签，并记住这次能用的那个，下一轮从它开始。

## 常见问题

- **签到返回 404**：站点签到路径不是 `/attendance.php`，用规则里的 `path` 指定
- **Cookie 无效**：重新复制 Cookie，或在「登录站点」里填账号密码
- **纯 Key 站点提示未声明规则**：补上 `auth: "api_key"` 和固定的 `path` / `credential_check_path`；未声明前插件不会发请求
- **API Key 站点能否回退 Browserless**：不能。API Key 模式只允许显式固定 HTTP 端点，避免把密钥交给浏览器或探测未知路径
- **卡在 Cloudflare / Turnstile**：可填写 Browserless 地址与 Token。插件优先用本机 Playwright，失败或缺少浏览器助手时可按规则回退到 Browserless。回退可能发送账号密码，且只调用官方 `solve`，能否通过仍取决于节点出口与站点风控
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
