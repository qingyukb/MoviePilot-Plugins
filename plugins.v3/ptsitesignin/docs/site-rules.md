# 自定义站点规则

[返回主页](../README.md)

当内置规则无法覆盖站点时，可以在「自定义站点规则 JSON」中填写规则。规则键可以是站点域名，也可以是 MoviePilot 中的站点 ID。

## 基础示例

```json
{
  "pt.example.com": {
    "engine": "http",
    "method": "POST",
    "path": "/attendance.php",
    "success": "签到成功|已签到",
    "already": "今日已签到",
    "bonus": "(获得\s*[\d,]+\s*粒爆米花)"
  }
}
```

## 常用字段

| 字段 | 说明 |
| --- | --- |
| `engine` | 执行引擎：`http` / `browserless` / `playwright` |
| `method` | 请求方式，通常为 `GET` 或 `POST` |
| `path` | 签到接口或页面路径 |
| `data` | 表单提交字段 |
| `json` | JSON 请求体 |
| `success` | 判断签到成功的正则 |
| `already` | 判断今日已签到的正则 |
| `already_html` | 按原始 HTML 判断今日已签到（用于链接地址变化等页面文字看不出的状态） |
| `absent_keywords` | 页面中消失时代表成功的文字 |
| `success_on_click` | 点击后直接判定成功 |
| `bonus` | 从页面提取奖励文案的正则 |
| `activity_path` | 最近动向页面路径 |
| `submit_text` | 需要点击的按钮文字 |
| `require_submit_text` | 为 `true` 时必须按文字找到按钮，找不到就不提交（避免误点其它表单） |
| `strict_status_keywords` | 为 `true` 时只认规则配置的成功/已签到文案，不用通用状态词 |
| `pre_click_selector` | 点击签到前需要操作的页面元素 |
| `pre_click_text` | 点击签到前按文字匹配页面元素，例如首页的签到入口 |
| `wait` | 操作后等待时间，单位毫秒 |
| `cloudflare_wait` | 等待人机验证放行的时间，单位秒 |

## 第二段签到

部分站点需要先读取首页令牌，再提交第二段签到请求：

```json
{
  "pt.example.com": {
    "path": "/index.php",
    "signin_path": "/signed.php",
    "signin_method": "POST",
    "signin_data": {
      "action": "signin"
    },
    "signin_token_patterns": {
      "token": "signed_token\s*=\s*['"]([^'"]+)"
    }
  }
}
```

## 彩虹岛答题签到

彩虹岛（`ptchdbits.co`）已内置 `/bakatest.php` 答题流程。

固定答案：

```json
{
  "ptchdbits.co": {
    "quiz_answer": "正确选项值"
  }
}
```

按题目映射答案：

```json
{
  "ptchdbits.co": {
    "quiz_answers": {
      "题目文本": "正确选项值"
    }
  }
}
```

未配置答案时会随机选择。提交后插件会重新读取签到页，只有确认出现成功状态才会判定签到完成。

## 站点模块

规则无法表达的复杂站点，可以在插件的 `sites/` 目录添加 Python 模块。模块按域名匹配，并优先于普通规则执行。

当前内置模块：

- `hdsky.py`：OCR 验证码
- `tjupt.py`：豆瓣海报比对
- `htpt.py`：海棠PT 表单签到
- `hdchina.py`：瓷器 CSRF 签到（保留站点刷新后的 Cookie）
- `hxpt.py`：好学 API 签到（home 状态 + flux_csrf + attendance/checkin）
- `u2.py`：U2 showup 四选一签到（提交后回读确认）
- `farm.py`：自由农场滑块处理

模块示例：

```python
class Example:
    site_url = "example.com"

    def signin(self, site, ctx):
        page = ctx.page("https://example.com/")
        if "已签到" in page:
            return True, "今日已签到"
        return False, "签到失败"
```

模块中可以使用：

- `ctx.page(url)`
- `ctx.post(url, data=...)`
- `ctx.image(url)`
- `ctx.ocr(image_url)`

---

[返回主页](../README.md)
