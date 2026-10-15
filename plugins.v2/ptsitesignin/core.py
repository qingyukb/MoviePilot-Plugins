"""PT 签到核心逻辑：HTTP 引擎 + Browserless(BrowserQL) 引擎。

本模块不含任何 MoviePilot 依赖，方便单独测试与复用。
Browserless 相关实现参考 tmzg0000/moviepilot-plugins 的 captchasignin 插件
（AGPL/MIT 见原仓库），在其基础上精简并与 HTTP 引擎合并为双引擎结构。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_HTTP_PATH = "/attendance.php"
DEFAULT_SUCCESS_PATTERN = r"(state['\"]?\s*[:=]\s*['\"]?success|签到成功|簽到成功|签到 成功)"
DEFAULT_ALREADY_PATTERN = r"(今日已签|已连续签到|已经签到|已签到|已簽到|already)"
COOKIE_INVALID_PATTERN = r"(用户登录|會員登入|请先登录|请登录|login\.php)"
# Cloudflare 挑战页特征（出现即说明请求未穿透盾，需改用 Browserless）
CLOUDFLARE_PATTERN = r"(just a moment|cf-chl-widget|cf_chl_opt|challenge-platform)"
# Cloudflare「Just a moment...」交互挑战的专属特征：这类挑战会在真实浏览器里自动执行 JS
# 并放行，只要留够等待时间即可，无需人工点击。注意不能用 challenge-platform /
# cf-chl-widget 判断——正常的 Turnstile 组件页也会带这些标记；挑战放行后页面会换成新
# 文档，_cf_chl_opt / chl_page 随之消失。
CF_INTERSTITIAL_PATTERN = r"(just a moment|_cf_chl_opt|chl_page)"
# 页面内嵌的 Cloudflare Turnstile 人机验证组件（观众 Audiences 的签到页就是这种）：验证结果
# 由页面 JS 在回调里生成，HTTP 直连拿不到，必须在真实浏览器里执行 JS 才能完成签到；注意与
# 上面的「Just a moment...」交互挑战区分开——这类正常页面不带 _cf_chl_opt / chl_page。
TURNSTILE_PATTERN = r"(cf-turnstile|turnstile/v0|turnstile\.min\.js|turnstile\.render)"
# 页面内嵌 Cloudflare Turnstile 组件的隐藏 token 输入框（观众 Audiences 的签到页用它承载验证结果）
TURNSTILE_TOKEN_SELECTOR = 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
# 站点把签到页踢回登录页的特征（NexusPHP login.php?returnto=... / takelogin.php）
LOGIN_PAGE_PATTERN = r"(takelogin\.php|login\.php\?returnto)"
# 等待 Cloudflare 交互挑战自动放行的默认秒数，可用站点规则字段 cloudflare_wait 覆盖。
DEFAULT_CLOUDFLARE_WAIT = 10
# 「验证通过后由页面 JS 自己提交」的提交方式：Browserless 的 solve 只负责生成 token，
# 不会触发站点挂在 Turnstile 回调上的提交逻辑（官方文档：解完验证码要自己触发提交），
# 所以插件还要显式补一次页面提交。
AUTO_SUBMIT_METHODS = {"auto", "turnstile_auto"}
# auto 提交方式等待 Turnstile token 出现的默认毫秒数，可用站点规则字段 auto_wait 覆盖。
DEFAULT_AUTO_WAIT = 20000
# 图片验证码 / 人机校验特征（HTTP 直连拿不到结果，需改用 Browserless）
CAPTCHA_PATTERN = (
    r"(captcha\.php|<img[^>]+captcha"
    r"|id\s*=\s*[\"']?(?:captcha|imagestring|imagehash|verifycode|imagecode)[\"']?"
    r"|name\s*=\s*[\"']?(?:imagestring|imagehash|verifycode|imagecode|answer|captcha)[\"']?"
    r"|验证码|驗證碼)"
)
# 签到奖励提取：按「获得 22 粒爆米花」「魔力值 +30」「签到已得 22」等常见文案取第一个命中（含繁体）
BONUS_PATTERNS = (
    r"((?:获得|獲得|獲取)\s*[\d,]+(?:\.\d+)?\s*(?:个|粒|枚|点|個|顆)?\s*(?:魔力值|爆米花|积分|做种积分|金币|奖励))",
    r"([\d,]+(?:\.\d+)?\s*(?:个|粒|枚|点|個|顆)\s*(?:魔力值|爆米花|积分|做种积分|金币))",
    r"((?:魔力值|爆米花|积分|做种积分|金币)\s*[:：]?\s*\+?\s*[\d,]+(?:\.\d+)?)",
    r"\+([\d,]+(?:\.\d+)?)\s*(?:魔力值|爆米花|积分|做种积分|金币)",
    r"(?:签到已得|簽到已得)\s*([\d,]+)",
)
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功")
ALREADY_WORDS = ("已签到", "已经签到", "今日已签", "今天已签", "已打卡", "already")
LOGIN_WORDS = ("用户登录", "會員登入", "login")

DEFAULT_BROWSERLESS_URL = "https://production-sfo.browserless.io"

# ---------------------------------------------------------------- 内置站点规则
# mode 仅对 Browserless 引擎有效：
#   http          直接 HTTP 请求签到接口（默认引擎，轻量、不消耗 Browserless 额度）
#   open_page     打开页面并点击签到按钮（无需验证码）
#   cloudflare    打开页面并自动通过 Cloudflare / Turnstile 挑战
#   image         图片验证码签到
#   trigger_image 先点开弹窗再处理图片验证码（HDSky 类）
#   altcha        需先完成 Altcha 人机验证再点击提交（YemaPT 类）
DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    # ---- 以下为需要浏览器交互的站点预设（来自 captchasignin 参考实现）----
    "open.cd": {
        "engine": "browserless",
        "mode": "image",
        "path": "/plugin_sign-in.php",
        "preflight_path": "/",
        "verify_success_path": "/",
        "captcha_selector": "#frmSignin img",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#ok",
        "submit_method": "ajax",
        "success_json": {"field": "/state", "values": ["success"]},
        "already_keywords": ["查看簽到記錄"],
        "strict_status_keywords": True,
    },
    "p.t-baozi.cc": {
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img",
        "captcha_input_selector": "input[name='imagestring']",
        "submit_selector": "input[type='submit']",
        "already_keywords": ["签到成功"],
    },
    "pt.muxuege.org": {
        "engine": "browserless",
        "mode": "open_page",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit']",
        "submit_text": "立即签到",
    },
    "oshen.win": {
        "engine": "browserless",
        "mode": "image",
        "path": "/attendance.php",
        "captcha_selector": "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
        "captcha_input_selector": "form input[name='imagestring']",
        "submit_selector": "form input[type='submit'][value='立即签到']",
        "submit_method": "ajax",
    },
    "pt.luckpt.de": {
        "engine": "browserless",
        "mode": "open_page",
        "path": "/medal_collection.php",
        "submit_selector": ".claim-bar button.claim-reward[data-type='bonus_daily']",
        "submit_method": "confirm_click",
        "submit_text": "领取 幸运星 ×1000",
        "success_keywords": ["奖励领取成功"],
        "already_keywords": ["今日已领取", "已经领取", "已领取"],
    },
    "yemapt.org": {
        "engine": "browserless",
        "mode": "altcha",
        "path": "/",
        "route_fragment": "#/user/growth?tab=checkIn",
        "altcha_checkbox_selector": "altcha-widget input[type='checkbox']",
        "altcha_payload_selector": "altcha-widget .altcha[data-state='verified'] input[name='altchaPayload']",
        "submit_selector": "button.ant-btn-primary.ant-btn-lg.ant-btn-block:not([disabled])",
        "submit_method": "altcha_click",
        "submit_text": "立即签到",
        "already_keywords": ["已签到，明日继续", "你的今日状态：已签到"],
        "preflight_wait": 8000,
        "wait": 5000,
    },
    "hdsky.me": {
        "engine": "browserless",
        "mode": "trigger_image",
        "path": "/index.php",
        "trigger_selector": "#showup",
        "captcha_selector": "#showupimg",
        "captcha_input_selector": "#imagestring",
        "submit_selector": "#showupbutton",
        "submit_method": "dom_click",
        "submit_text": "Let's Go",
        "success_keywords": ["已签到"],
    },
    "dstudio.me": {"engine": "browserless", "mode": "cloudflare", "path": "/attendance.php"},
    "mua.xloli.cc": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    },
    "share.ilolicon.com": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_selector": "form[action*='attendance.php'] input[type='submit']",
    },
    # ---- 观众（Audiences）：签到由页面 JS 自动完成，必须走 Browserless ----
    # 该站 /attendance.php 是一个 Vue 签到页：正文是「每日签到 / 完成人机验证即可领取今日
    # 爆米花奖励」「人机验证 / 验证通过后将自动完成签到」，验证组件是 Cloudflare Turnstile。
    # 签到由页面 JS 在 Turnstile 回调里**自动提交**，页面上没有可点的提交按钮；HTTP 直连
    # （GET / POST）只会拿到未签到的页面骨架，永远匹配不到结果，所以这里直接指定浏览器引擎：
    #   加载签到页 -> solve 生成 Turnstile token -> 显式补一刀页面提交 -> 等签到请求发完
    #   - submit_method=auto：页面没有可点的提交按钮，而 Browserless 的 solve 只生成 token、
    #     不会触发站点的 data-callback；所以由插件按「回调 -> 表单 -> 同源 POST」补一次提交
    #   - cloudflare_wait 调大一点（10 -> 15 秒）更稳，wait 给足页面 AJAX 时间
    #   - auto_wait：solve 之后等待 Turnstile token 出现在隐藏输入框里的毫秒数
    #   - solve_timeout：solve 找验证码的毫秒预算（默认 60 秒，这里 30 秒够了，省得整轮超时）
    #   - pending_keywords：仍停留在「验证中」的正文特征。命中说明人机验证没通过 / 签到没提交，
    #     此时给出明确提示，而不是含糊的「未匹配到签到成功标志」
    #   - 判定顺序：先看成功 / 已签到关键词，再判 pending，避免已签完的页面被 pending 文案误杀
    "audiences.me": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_method": "auto",
        "cloudflare_wait": 15,
        # solve 只等验证码出现：组件本身 15 秒内就渲染好了，不必给 60 秒的默认预算
        "solve_timeout": 30000,
        "auto_wait": 15000,
        "wait": 8000,
        # 签到成功后页面可能跳回首页；同一浏览器会话重新打开 attendance.php 再确认一次，
        # 避免只看到首页就误报失败，也避免把未完成的验证当成成功。
        "verify_success_path": "/attendance.php",
        "verify_wait": 8000,
        # 「已领取 / 已获得」不能作为成功标志：签到后刷新页面仍会显示奖励，
        # 很容易把已签到状态误判成刚刚签到。
        "success_keywords": ["签到成功", "簽到成功", "成功签到", "签到完成", "领取成功"],
        "already_keywords": ["今日已签到", "今天已经签到", "您今天已经签到", "已经签到",
                             "已签到", "今日已领取", "已连续签到", "明日再来", "明天再来"],
        "pending_keywords": ["验证通过后将自动完成签到", "完成人机验证即可领取今日爆米花奖励"],
    },
    # ---- 未内置的站点默认走 HTTP 直连（无需浏览器交互）----
    # GET 打开签到页即完成签到，判定文案取自站点实际页面：
    #   签到成功 -> 「签到成功」；已签到 -> 「您今天已经签到过了」
    # already 只保留上述「已签到」原文案：签到成功页往往同时带「已签到 N 天」「您已连续
    # 签到 N 天」之类的状态行，一旦把裸「已签到」写进 already，新签到就会被误判成
    # 「今日已签到」。
    # 页面内嵌 Turnstile 的站点 HTTP 拿不到结果，会自动回退到 Browserless（见 judge_http）。
}


@dataclass(frozen=True)
class SignResult:
    status: str   # success / already / failed
    message: str
    challenge: str = ""   # "" / cloudflare / captcha：HTTP 失败时提示该站可改用浏览器引擎


# ------------------------------------------------------------------ 公共工具

def normalize_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.").rstrip(".")


def target_url(base_url: str, path: str, route_fragment: Optional[str] = None) -> str:
    target = urllib.parse.urljoin(base_url.rstrip("/") + "/", str(path).lstrip("/"))
    return target.split("#", 1)[0] + (route_fragment or "")


def site_state_key(site: Mapping[str, Any]) -> str:
    site_id = str(site.get("id") or "").strip()
    if site_id:
        return "id:" + site_id
    host = urllib.parse.urlparse(str(site.get("url") or "")).hostname
    return "host:" + host.lower() if host else ""


def parse_rules(raw: Any) -> Dict[str, Dict[str, Any]]:
    """解析自定义规则；键为域名或站点 ID。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as error:
        raise ValueError(f"站点规则必须是有效 JSON：{error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("站点规则必须是以域名或站点 ID 为键的 JSON 对象")
    result: Dict[str, Dict[str, Any]] = {}
    for key, rule in value.items():
        if isinstance(key, str) and isinstance(rule, Mapping):
            result[key.lower().strip()] = dict(rule)
    return result


def resolve_rule(site: Mapping[str, Any], custom_rules: Mapping[str, Mapping[str, Any]],
                 default_engine: str = "http") -> Dict[str, Any]:
    host = normalize_host(str(site.get("url") or ""))
    custom = (custom_rules.get(str(site.get("id"))) or custom_rules.get(host)
              or custom_rules.get("www." + host) or {})
    preset = DEFAULT_RULES.get(host)
    if preset is None and host.endswith(".open.cd"):
        preset = DEFAULT_RULES["open.cd"]
    rule = dict(preset or {})
    rule.update(custom)
    # 只有用户在自定义规则里显式声明 engine 才算「手工指定」；内置预设声明的 http
    # 仍允许在遇盾 / 验证码时自动回退到浏览器引擎
    user_engine = str(custom.get("engine") or "").strip().lower()
    declared_engine = str(rule.get("engine") or "").strip().lower()
    rule["engine"] = declared_engine or str(default_engine or "http").lower()
    if rule["engine"] not in {"http", "browserless"}:
        raise ValueError("签到引擎只能是 http 或 browserless")
    rule["auto_fallback"] = not user_engine
    if rule["engine"] == "http":
        rule.setdefault("path", DEFAULT_HTTP_PATH)
        rule.setdefault("success", DEFAULT_SUCCESS_PATTERN)
        rule.setdefault("already", DEFAULT_ALREADY_PATTERN)
    else:
        rule.setdefault("mode", "open_page")
        rule.setdefault("path", "/attendance.php")
        rule.setdefault("submit_selector", "input[type='submit']")
        rule.setdefault("already_keywords", [])
    return validate_rule(rule)


def validate_rule(rule: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(rule)
    if result["engine"] == "http":
        path = str(result.get("path") or "")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("签到路径必须是以 / 开头的站内路径")
        return result
    mode = result.get("mode")
    if mode not in {"open_page", "cloudflare", "image", "trigger_image", "altcha"}:
        raise ValueError("Browserless 签到方式必须为 open_page、cloudflare、image、trigger_image 或 altcha")
    path = str(result.get("path") or "")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
        raise ValueError("签到路径必须是以 / 开头的站内路径")
    route_fragment = str(result.get("route_fragment") or "")
    if route_fragment and (not route_fragment.startswith("#/") or "\\" in route_fragment):
        raise ValueError("路由片段必须以 #/ 开头")
    if mode in {"image", "trigger_image"}:
        for name in ("captcha_selector", "captcha_input_selector", "submit_selector"):
            if not str(result.get(name) or "").strip():
                raise ValueError("图片验证码规则缺少 " + name)
    if mode == "trigger_image" and not str(result.get("trigger_selector") or "").strip():
        raise ValueError("弹窗图片验证码规则缺少 trigger_selector")
    if mode in {"image", "trigger_image", "altcha"} and str(
            result.get("submit_method") or "").lower() in {"none", "skip"}:
        raise ValueError("图片验证码 / Altcha 站点必须指定提交按钮，不能用 submit_method=none")
    if mode == "altcha":
        for name in ("altcha_checkbox_selector", "altcha_payload_selector", "submit_selector"):
            if not str(result.get(name) or "").strip():
                raise ValueError("Altcha 规则缺少 " + name)
    # cloudflare / turnstile：仅允许自动检测，不允许手工指定 solve 类型
    return result


# 挑战类型 → Browserless 签到方式
FALLBACK_MODES = {"cloudflare": "cloudflare", "captcha": "open_page"}


def fallback_rule(rule: Mapping[str, Any], challenge: str) -> Dict[str, Any]:
    """由 HTTP 规则派生一份 Browserless 回退规则。

    用于「没有写规则的站点」：HTTP 直连遇 Cloudflare / 验证码时，自动改用浏览器引擎
    再试一次。Cloudflare 走 ``cloudflare`` 自动过盾；验证码走 ``open_page``（浏览器
    打开签到页，部分站点在真实浏览器环境下不再要求验证码或可直接完成签到）。
    """
    fallback: Dict[str, Any] = {
        "engine": "browserless",
        "mode": FALLBACK_MODES.get(str(challenge), "open_page"),
        "path": str(rule.get("path") or DEFAULT_HTTP_PATH),
        "submit_selector": "input[type='submit']",
        "already_keywords": [],
    }
    if rule.get("route_fragment"):
        fallback["route_fragment"] = rule["route_fragment"]
    # 让用户在 HTTP 规则里写的等待参数（如 cloudflare_wait）对自动回退同样生效
    for name in ("cloudflare_wait", "wait", "preflight_wait"):
        if rule.get(name) is not None:
            fallback[name] = rule[name]
    return validate_rule(fallback)


# =============================================================== HTTP 引擎

def http_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
              proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """直接 HTTP 请求签到接口（NexusPHP attendance.php 等）。"""
    import requests

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return SignResult("failed", "站点地址或 Cookie 为空")
    url = target_url(base_url, str(rule.get("path") or DEFAULT_HTTP_PATH))
    headers = {
        "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
        "Referer": f"{base_url}/index.php",
        "Cookie": cookie,
        "Accept": "application/json, text/javascript, text/html, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        method = str(rule.get("method") or "GET").upper()
        if method == "POST":
            response = requests.post(url, headers=headers, timeout=timeout,
                                     allow_redirects=True, proxies=proxies)
        else:
            response = requests.get(url, headers=headers, timeout=timeout,
                                    allow_redirects=True, proxies=proxies)
    except Exception as error:
        return SignResult("failed", f"请求异常：{error}")
    content = response.text or ""
    if response.status_code >= 400:
        return SignResult("failed", f"请求失败（HTTP {response.status_code}）")
    return judge_http(content, rule)


def plain_text(content: str) -> str:
    """去掉注释、script/style 与 HTML 标签并压缩空白，便于按文案匹配。"""
    text = re.sub(r"<!--.*?-->", " ", content, flags=re.DOTALL)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_bonus(content: str, rule: Mapping[str, Any]) -> str:
    """提取签到奖励描述（如「获得 22 粒爆米花」）。规则里的 bonus 字段可覆盖默认模板。"""
    custom = str(rule.get("bonus") or "").strip()
    patterns = (custom,) if custom else BONUS_PATTERNS
    for candidate in (content, plain_text(content)):
        for pattern in patterns:
            try:
                found = re.search(pattern, candidate, re.IGNORECASE)
            except re.error:
                continue
            if not found:
                continue
            values = [group for group in found.groups() if group]
            text = values[-1] if values else found.group(0)
            return re.sub(r"\s+", " ", text).strip()
    return ""


def snippet_around(text: str, keyword: str, width: int = 60) -> str:
    """截取 keyword 前后的一小段正文，便于定位页面实际内容。"""
    index = text.find(keyword)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def page_hint(content: str) -> str:
    """截取响应摘要，便于判断站点到底返回了什么页面。"""
    title = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    body = plain_text(content)
    parts = []
    if title:
        parts.append("标题「" + re.sub(r"\s+", " ", title.group(1)).strip()[:60] + "」")
    if body:
        parts.append("正文「" + body[:120] + "」")
        # 签到卡片常在页面中后段，正文前 120 字全是站点头尾时再补一段签到相关片段
        for word in ("每日签到", "人机验证", "签到成功", "已签到", "签到"):
            if body.find(word) > 100:
                parts.append("片段「" + snippet_around(body, word) + "」")
                break
    return "；".join(parts) or "响应为空"


def judge_http(content: str, rule: Mapping[str, Any]) -> SignResult:
    # 完整页面匹配：Audiences 等站的判定文案位于页面较后位置，按 20000 字符截断会漏掉
    text = content[:200000]
    if re.search(CLOUDFLARE_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "站点返回 Cloudflare 验证页", challenge="cloudflare")
    if re.search(COOKIE_INVALID_PATTERN, text, re.IGNORECASE) and "logout" not in text.lower():
        return SignResult("failed", "Cookie 无效或已过期")
    already = str(rule.get("already") or DEFAULT_ALREADY_PATTERN)
    success = str(rule.get("success") or DEFAULT_SUCCESS_PATTERN)
    # 判定文案只在去掉标签 / script/style 的可见正文上匹配，避免命中页面 JS 里的
    # 签到分支字符串。
    visible = plain_text(text)
    # 先判「签到成功」再判「今日已签到」：多数 NexusPHP 的成功页同时含「签到成功」与
    # 「已签到 N 天」，顺序反了就会把刚完成的签到误报成「今日已签到」。
    if re.search(success, visible, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if re.search(already, visible, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")
    # 页面内嵌 Cloudflare Turnstile 人机验证（观众 Audiences 这类 Vue 签到页）：签到的判定
    # 文案由 JS 在验证回调里生成，HTTP 直连拿不到，只能交给浏览器引擎。放在成功 / 已签到
    # 之后判断，避免「已签到」页仍带 Turnstile 组件时被判成还需要验证。
    if re.search(TURNSTILE_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "页面需要完成 Cloudflare 人机验证（Turnstile）",
                          challenge="cloudflare")
    # NexusPHP JSON 返回
    try:
        data = json.loads(text.strip())
    except (ValueError, TypeError):
        data = None
    if isinstance(data, Mapping):
        state = str(data.get("state") or data.get("status") or "").lower()
        message = str(data.get("sign") or data.get("msg") or data.get("message") or "签到成功")
        if state in {"success", "ok", "true", "1"}:
            bonus = extract_bonus(text, rule)
            return SignResult("success", f"{message}（{bonus}）" if bonus else message)
        if state in {"false", "fail", "failed", "error"}:
            return SignResult("failed", str(data.get("msg") or data.get("message")
                                            or data.get("error") or "站点返回签到失败"))
    # 少数站点把结果只放在 <script> 里的 state 字段；此时才回退到原始文本。
    # 上面已经先排除了可见的「已签到」，不会把已签到页面脚本里的成功分支误判为新签到。
    if re.search(success, text, re.IGNORECASE):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if re.search(CAPTCHA_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "页面要求填写验证码", challenge="captcha")
    return SignResult("failed", f"未匹配到签到成功标志；{page_hint(text)}")


# =========================================================== Browserless 引擎

def cookie_objects(cookie_header: str, target: str) -> List[Dict[str, Any]]:
    parsed = urllib.parse.urlparse(target)
    domain = parsed.hostname
    if not domain:
        return []
    cookies: List[Dict[str, Any]] = []
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if name:
            cookies.append({"name": name.strip(), "value": value.strip(), "domain": domain,
                            "path": "/", "secure": parsed.scheme == "https", "url": target})
    return cookies


def bql_endpoint(address: str, token: str) -> str:
    address, token = address.strip(), token.strip()
    if not address or not token:
        raise ValueError("Browserless 地址和 Token 都不能为空")
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Browserless 地址必须以 http:// 或 https:// 开头")
    path = parsed.path.rstrip("/")
    if not path.endswith("/bql"):
        path = (path + "/bql") if path.endswith("/stealth") else (path + "/stealth/bql")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "",
                                    urllib.parse.urlencode(query), ""))


def _submit_script(selector: str, method: str, captcha_input: Optional[str] = None,
                   submit_text: Optional[str] = None) -> str:
    selector_json = json.dumps(selector)
    input_json = json.dumps(captcha_input or "")
    text_json = json.dumps(submit_text or "")
    target_script = """(() => { const wanted = %s.trim();
      if (wanted) { const matches = [...document.querySelectorAll('input, button, a')];
        const found = matches.find((node) => ((node.value || node.textContent || '').trim().includes(wanted)));
        if (found) return found; }
      return document.querySelector(%s);
    })()""" % (text_json, selector_json)
    if method in {"click", "dom_click", "confirm_click"}:
        confirm = "" if method != "confirm_click" else """
          await new Promise((resolve) => setTimeout(resolve, 150));
          const confirm = [...document.querySelectorAll('.layui-layer-btn0, .layui-layer-btn a')]
            .find((node) => node.offsetParent !== null && /确定|确认/.test((node.textContent || '').trim()));
          if (!confirm) return false; confirm.click();"""
        return """(async () => { const input = %s ? document.querySelector(%s) : null;
          if (input && !input.value.trim()) return false;
          const button = %s; if (!button) return false; button.click(); %s return true;
        })()""" % (input_json, input_json, target_script, confirm)
    return """(async () => {
      const input = %s ? document.querySelector(%s) : null; if (input && !input.value.trim()) return false;
      const target = %s; const form = target && (target.form || target);
      if (!(form instanceof HTMLFormElement)) throw new Error('未找到签到表单');
      const action = new URL(form.action, location.href);
      if (action.origin !== location.origin) throw new Error('只允许同源提交');
      const data = new FormData(form); if (target.name && !target.disabled) data.append(target.name, target.value);
      const response = await fetch(action.href, {method: form.method || 'POST', credentials:'same-origin', body:data});
      window.__ptsignin_response = {status:response.status, text:await response.text()}; return response.status;
    })()""" % (input_json, input_json, target_script)


def _altcha_submit_script(rule: Mapping[str, Any]) -> str:
    payload = json.dumps(str(rule["altcha_payload_selector"]))
    submit = _submit_script(str(rule["submit_selector"]), "dom_click", submit_text=rule.get("submit_text"))
    return """(async () => {
      const ready = () => { const input = document.querySelector(%s); return input && input.value.trim() ? input : null; };
      const deadline = Date.now() + 30000;
      while (!ready() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 250));
      if (!ready()) return false;
      return await %s;
    })()""" % (payload, submit)


def _trigger_script(selector: str) -> str:
    return """(() => { const trigger = document.querySelector(%s);
      if (!trigger) return false; trigger.click(); return true;
    })()""" % json.dumps(selector)


def _turnstile_auto_script(rule: Mapping[str, Any]) -> str:
    """页面内嵌 Turnstile「验证通过后自动签到」的补充提交脚本（观众 Audiences 类站点）。

    Browserless 的 solve 只会把验证结果写进隐藏输入框（必要时连勾选都不显示），**不会**
    触发站点挂在 Turnstile 回调上的自动提交逻辑，所以这里按页面实际结构依次补一刀：

    1. 先主动调用 ``data-callback`` 指向的站点回调，让站点自己的 JS 完成提交；
    2. 回调不存在或抛错时，再按表单真实 ``action`` 做同源 POST（没有表单则 POST 当前
       签到页），并带上表单中已有的字段和提交按钮字段。

    回调有可能触发整页提交，因此回调放到 setTimeout 中异步触发，当前 evaluate 先返回；
    回调若只是 AJAX，等 2.5 秒后若页面仍在 pending 状态，再触发一次表单 POST 兜底。
    每步结果写入 ``window.__ptsignin_auto`` / sessionStorage，失败时由
    ``auto_submit_hint`` 输出，便于判断到底是 token、回调还是提交环节出了问题。
    """
    wait = auto_wait_ms(rule)
    pending = json.dumps([str(item) for item in (rule.get("pending_keywords") or [])],
                         ensure_ascii=False)
    return """(async () => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const pendingWords = %s;
      const diag = {token: false, src: null, cb: null, form: false, act: null, post: null};
      const save = () => {
        window.__ptsignin_auto = diag;
        try { sessionStorage.setItem('__ptsignin_auto', JSON.stringify(diag)); } catch (err) {}
      };
      save();
      const sel = '%s';
      const inputToken = () => { const el = document.querySelector(sel); return (el && (el.value || '').trim()) || ''; };
      const apiToken = () => {
        try {
          if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
            return (window.turnstile.getResponse() || '').trim();
          }
        } catch (err) { return ''; }
        return '';
      };
      const readToken = () => {
        const viaInput = inputToken();
        if (viaInput) { diag.src = 'input'; return viaInput; }
        const viaApi = apiToken();
        if (viaApi) { diag.src = 'turnstile'; return viaApi; }
        return '';
      };
      const deadline = Date.now() + %d;
      while (!readToken() && Date.now() < deadline) await sleep(250);
      const token = readToken();
      diag.token = !!token;
      const forms = Array.prototype.slice.call(document.querySelectorAll('form'));
      const form = forms.filter((item) => /attendance/i.test(item.getAttribute('action') || ''))[0]
        || forms.filter((item) => item.querySelector(sel))[0]
        || forms.filter((item) => (item.method || '').toLowerCase() === 'post')[0] || null;
      if (form) { diag.form = true; }

      const formData = () => {
        const data = form ? new FormData(form) : new FormData();
        if (!form) {
          document.querySelectorAll('input[name], textarea[name], select[name]').forEach((el) => {
            if (!el.disabled && !data.has(el.name)) data.append(el.name, el.value || '');
          });
        }
        const submit = form && form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
        if (submit && submit.name && !submit.disabled && !data.has(submit.name)) {
          data.append(submit.name, submit.value || '');
        }
        if (token) data.set('cf-turnstile-response', token);
        return data;
      };

      const postFallback = async () => {
        const actionValue = form ? (form.getAttribute('action') || location.href) : location.href;
        const action = new URL(actionValue, location.href);
        if (action.origin !== location.origin) {
          diag.act = diag.act ? diag.act + '+post-blocked' : 'post-blocked';
          save();
          return;
        }
        try {
          const response = await fetch(action.href, {
            method: 'POST', credentials: 'same-origin', body: formData()
          });
          window.__ptsignin_response = {status: response.status, url: response.url, text: await response.text()};
          diag.post = response.status;
          diag.act = diag.act ? diag.act + '+post' : 'post';
          save();
        } catch (err) {
          diag.post = 'error';
          diag.act = diag.act ? diag.act + '+post-error' : 'post-error';
          save();
        }
      };

      const stillPending = () => {
        const text = (document.body && document.body.innerText) || '';
        return pendingWords.some((word) => word && text.indexOf(word) !== -1);
      };

      const widget = document.querySelector('[data-callback]');
      const cbName = (widget && (widget.getAttribute('data-callback') || '').trim()) || '';
      if (cbName) {
        diag.cb = cbName;
        const cb = window[cbName];
        if (typeof cb === 'function') {
          diag.act = 'callback-scheduled';
          save();
          setTimeout(async () => {
            try {
              cb(token);
              diag.act = 'callback';
              save();
            } catch (err) {
              diag.act = 'callback-error';
              save();
              await postFallback();
              return;
            }
            setTimeout(async () => {
              if (stillPending()) {
                diag.act = diag.act + '+fallback';
                save();
                await postFallback();
              }
            }, 2500);
          }, 0);
          return JSON.stringify(diag);
        }
        diag.act = 'callback-missing';
      }

      if (token) await postFallback();
      save();
      return JSON.stringify(diag);
    })()""" % (pending, TURNSTILE_TOKEN_SELECTOR, wait)


def build_query(rule: Mapping[str, Any], user_agent: Optional[str] = None) -> str:
    """构造一次签到的 BrowserQL mutation。

    Cloudflare / Turnstile 使用 ``solve:solve``（不限制类型）以支持自动检测，
    包括页面内嵌 iframe / shadow DOM 中的 Turnstile 复选框挑战。
    """
    image = rule["mode"] in {"image", "trigger_image"}
    triggered_image = rule["mode"] == "trigger_image"
    altcha = rule["mode"] == "altcha"
    verifies_success = bool(rule.get("verify_success_path"))
    needs_solver = rule["mode"] in {"image", "trigger_image", "cloudflare"}
    submit_method = str(rule.get("submit_method") or "click")
    native_click = submit_method == "click"
    # submit_method=auto：页面没有提交按钮、由验证回调自己提交（观众 Audiences）；solve 只
    # 生成 token 不触发回调，所以由插件补一刀显式提交。
    auto_submit = submit_method in AUTO_SUBMIT_METHODS
    # submit_method=none：什么都不点，纯等页面自己签完
    skip_submit = submit_method in {"none", "skip"}
    solve = ("solve:solveImageCaptcha(captchaSelector:$captchaSelector,inputSelector:$captchaInputSelector,timeout:$solveTimeout){found solved time}"
             if image else "solve:solve(timeout:$solveTimeout){found solved time}" if rule["mode"] == "cloudflare"
             else "solve:evaluate(content:\"'skipped'\"){value}")
    extra = "$captchaSelector:String! $captchaInputSelector:String!" if image else ""
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    variable_suffix = (" $solveTimeout:Float!" if needs_solver else "")
    if native_click:
        variable_suffix += " $selector:String!"
    elif auto_submit:
        variable_suffix += " $autoSubmit:String!"
    elif not skip_submit:
        variable_suffix += " $submit:String!"
    if triggered_image:
        variable_suffix += " $trigger:String! $triggerWait:Float!"
    if altcha:
        variable_suffix += " $altchaSelector:String!"
    verify = ""
    if verifies_success:
        variable_suffix += " $verifyUrl:String!"
        if rule.get("verify_wait") is not None:
            variable_suffix += " $verifyWait:Float!"
        verify = "verify:goto(url:$verifyUrl,waitUntil:domContentLoaded){status}"
        if rule.get("verify_wait") is not None:
            verify += "\n      waitForVerify:waitForTimeout(time:$verifyWait){time}"
        verify += "\n      verifyPage:html{html}"
    auto_diag = ("" if not auto_submit else
                 'autoDiag:evaluate(content:"JSON.stringify(window.__ptsignin_auto || (function(){try{return JSON.parse(sessionStorage.getItem(\'__ptsignin_auto\')||\'null\')}catch(e){return null}})())"){value}')
    if extra:
        variable_suffix += " " + extra
    variable_suffix += user_agent_variable
    return """mutation CheckIn($cookies:[CookieInput!]! $url:String! $beforeWait:Float! $wait:Float!%s) {
      %s
      cookies(cookies:$cookies){cookies{name}}
      goto(url:$url,waitUntil:domContentLoaded){status}
      waitBefore:waitForTimeout(time:$beforeWait){time}
      before:html{html}
      %s
      %s
      %s
      %s
      waitAfter:waitForTimeout(time:$wait){time}
      response:evaluate(content:"JSON.stringify(window.__ptsignin_response || null)"){value}
      %s
      %s
      after:html{html}
    }""" % (variable_suffix, set_user_agent,
            "trigger:evaluate(content:$trigger){value}\n      waitForTrigger:waitForTimeout(time:$triggerWait){time}" if triggered_image else "",
            "altcha:click(selector:$altchaSelector){selector time}" if altcha else "",
            solve,
            "submit:click(selector:$selector){selector time}" if native_click
            else "submit:evaluate(content:$autoSubmit){value}" if auto_submit
            else "" if skip_submit else "submit:evaluate(content:$submit){value}",
            auto_diag,
            verify)


def build_preflight_query(user_agent: Optional[str] = None, wait: int = 2000) -> str:
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    return """mutation CheckInPreflight($cookies:[CookieInput!]! $url:String!%s) {
      %s
      cookies(cookies:$cookies){cookies{name}}
      goto(url:$url,waitUntil:domContentLoaded){status}
      waitForTimeout(time:%s){time}
      before:html{html}
    }""" % (user_agent_variable, set_user_agent, max(1000, min(int(wait), 15000)))


def page_text(html: str) -> str:
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "",
                     flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).strip()


def first_keyword(text: str, keywords: Any) -> str:
    """返回 keywords 里第一个出现在 text 中的词（大小写不敏感），都不命中时返回空串。"""
    haystack = (text or "").lower()
    for word in keywords or []:
        needle = str(word).strip()
        if needle and needle.lower() in haystack:
            return needle
    return ""


def cloudflare_pending(html: str) -> bool:
    """页面是否仍停留在 Cloudflare 交互挑战页（自动放行后即不再命中）。"""
    return bool(re.search(CF_INTERSTITIAL_PATTERN, html or "", re.IGNORECASE))


def login_page(html: str) -> bool:
    """页面是否就是站点登录页。

    NexusPHP 在 Cookie 失效 / 未登录时会把 attendance.php 302 到
    ``login.php?returnto=attendance.php``；也有站点直接把登录表单塞进返回的页面。
    命中时要明确提示 Cookie 失效，而不是让人机验证超时背锅。
    """
    text = html or ""
    if re.search(LOGIN_PAGE_PATTERN, text, re.IGNORECASE):
        return True
    if "logout.php" in text.lower():
        return False
    return bool(re.search(r"name\s*=\s*[\"']?password", text, re.IGNORECASE)
                and re.search(r"name\s*=\s*[\"']?(?:username|email)", text, re.IGNORECASE))


def login_required_message(where: str) -> str:
    return (f"{where}：Cookie 无效或已过期，请在 MoviePilot 里重新登录该站并更新 Cookie；"
            "否则插件每次拿到的都只是登录页")


def append_hint(message: str, hint: str) -> str:
    """把现场诊断拼到失败原因后面（没有诊断时原样返回）。"""
    return f"{message}；{hint}" if hint else message


def cloudflare_wait_ms(rule: Mapping[str, Any]) -> int:
    """Cloudflare 模式下停留在页面等待挑战自动放行的毫秒数（3~45 秒，可规则覆盖）。"""
    try:
        seconds = float(rule.get("cloudflare_wait") or DEFAULT_CLOUDFLARE_WAIT)
    except (TypeError, ValueError):
        seconds = DEFAULT_CLOUDFLARE_WAIT
    return int(max(3.0, min(seconds, 45.0)) * 1000)


def solve_timeout_ms(rule: Mapping[str, Any]) -> int:
    """solve 等待验证码出现的毫秒预算（5~120 秒，可用 solve_timeout 覆盖）。"""
    try:
        milli = int(float(rule.get("solve_timeout") or 60000))
    except (TypeError, ValueError):
        milli = 60000
    return max(5000, min(milli, 120000))


def auto_wait_ms(rule: Mapping[str, Any]) -> int:
    """auto 提交方式等待 Turnstile token 出现的毫秒数（3~45 秒，可用 auto_wait 覆盖）。"""
    try:
        milli = int(float(rule.get("auto_wait") or DEFAULT_AUTO_WAIT))
    except (TypeError, ValueError):
        milli = DEFAULT_AUTO_WAIT
    return max(3000, min(milli, 45000))


def navigation_ok(mode: str, status: Any, html: str) -> bool:
    """这次导航是否拿到了可用页面。

    Cloudflare 交互挑战以 403 / 503 返回挑战页，等它自动放行后才加载真实页面，而 goto
    记录的仍是挑战页的状态码，因此 cloudflare 模式下不能只看状态码。
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    if 200 <= code < 400:
        return True
    return str(mode) == "cloudflare" and code in {403, 503} and not cloudflare_pending(html)


def _bonus_message(prefix: str, text: str, rule: Mapping[str, Any]) -> str:
    """成功 / 已签消息只带奖励数字，不再粘贴整页文字。"""
    bonus = extract_bonus(text, rule)
    return f"{prefix}（{bonus}）" if bonus else prefix


def auto_submit_hint(value: Any) -> str:
    """把页面里 ``window.__ptsignin_auto`` 的现场诊断翻译成一句中文提示。

    用于「页面仍停在人机验证」这类失败：token 到底有没有生成、站点回调有没有触发、
    表单有没有提交、兜底 POST 返回什么，直接写在失败原因里，省得来回猜。
    """
    if not value or value == "null":
        return ""
    try:
        state = json.loads(str(value))
    except (TypeError, ValueError):
        return ""
    if not isinstance(state, Mapping):
        return ""
    parts = ["已拿到 Turnstile token" if state.get("token") else "未拿到 Turnstile token"]
    name = str(state.get("cb") or "").strip()
    action = str(state.get("act") or "")
    if name:
        if action.startswith("callback-scheduled"):
            parts.append(f"已调度站点回调 {name}（执行结果未确认）")
        elif action.startswith("callback-error"):
            parts.append(f"站点回调 {name} 执行失败")
        elif action.startswith("callback-missing"):
            parts.append(f"页面声明的站点回调 {name} 不存在")
        elif action.startswith("callback"):
            parts.append(f"已调用站点回调 {name}")
        else:
            parts.append(f"站点回调 {name} 没能调用")
    else:
        parts.append("页面没有 data-callback 回调")
    if state.get("form"):
        parts.append("已提交页面表单")
    post = state.get("post")
    if isinstance(post, int) and not isinstance(post, bool):
        parts.append(f"兜底 POST 返回 HTTP {post}")
    elif post:
        parts.append("兜底 POST 失败")
    return "诊断：" + "；".join(parts)


def classify(html: str, rule: Mapping[str, Any]) -> SignResult:
    full_text = page_text(html)
    lower = full_text.lower()
    if any(word.lower() in lower for word in LOGIN_WORDS) and "logout" not in lower:
        return SignResult("failed", "Cookie 无效或已过期")
    if any(word.lower() in lower for word in rule.get("success_keywords", [])):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if any(word.lower() in lower for word in rule.get("already_keywords", [])):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in SUCCESS_WORDS):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in ALREADY_WORDS):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    return SignResult("failed", "未识别到签到成功结果；" + page_hint(html))


def classify_ajax_response(value: Any, rule: Mapping[str, Any]) -> Optional[SignResult]:
    if not value or value == "null":
        return None
    try:
        response = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(response, Mapping):
        return None
    status = response.get("status")
    if isinstance(status, int) and not 200 <= status < 400:
        return SignResult("failed", f"签到请求失败（HTTP {status}）")
    text = str(response.get("text") or "")
    success_json = rule.get("success_json") or {}
    if success_json:
        try:
            body = json.loads(text)
            current: Any = body
            for key in str(success_json.get("field") or "").lstrip("/").split("/"):
                current = current[key]
            if str(current) in {str(item) for item in success_json.get("values", [])}:
                message = str(body.get("sign") or body.get("msg") or "签到成功")
                bonus = extract_bonus(text, rule)
                if bonus and bonus not in message:
                    message = f"{message}（{bonus}）"
                return SignResult("success", message)
        except (KeyError, TypeError, ValueError):
            pass
    return classify(text, rule)


class BrowserlessSigner:
    """通过 Browserless BrowserQL 执行一次签到。"""

    def __init__(self, address: str, token: str, timeout: int = 100):
        self.endpoint = bql_endpoint(address, token)
        self.timeout = max(15, min(int(timeout), 300))

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        base_url = str(site.get("url") or "").rstrip("/")
        cookie = str(site.get("cookie") or "").strip()
        if not base_url or not cookie:
            return SignResult("failed", "站点地址或 Cookie 为空")
        target = target_url(base_url, str(rule["path"]), rule.get("route_fragment"))
        cookies = cookie_objects(cookie, target)
        if not cookies:
            return SignResult("failed", "站点 Cookie 格式无效")
        user_agent = str(site.get("ua") or "").strip()
        mode = str(rule.get("mode") or "open_page")
        # Cloudflare 交互挑战需要在真实浏览器里等它自己跑完 JS 才放行，这里给出等待预算
        page_wait = cloudflare_wait_ms(rule) if mode == "cloudflare" else 2000
        # 先读页面：可能已签到或按钮已消失，两种情况都应当直接判定
        preflight_target = target_url(base_url, str(rule.get("preflight_path") or rule["path"]))
        preflight_variables: Dict[str, Any] = {"cookies": cookies, "url": preflight_target}
        if user_agent:
            preflight_variables["userAgent"] = user_agent
        preflight_wait = max(1000, min(int(rule.get("preflight_wait") or page_wait), 15000))
        preflight = self._request(preflight_variables, "CheckInPreflight",
                                  build_preflight_query(user_agent, preflight_wait))
        if isinstance(preflight, SignResult):
            return preflight
        preflight_html = str((preflight.get("before") or {}).get("html") or "")
        # 进页面时是否还停在「验证中 / 未签到」状态（只有配了 pending_keywords 的站点才有意义）
        pre_state_pending = False
        if cloudflare_pending(preflight_html):
            # 挑战还没放行：不要把挑战页的 403 当成「打开失败」，交给下面等待更久的主流程
            pass
        else:
            if not navigation_ok(mode, preflight.get("goto", {}).get("status"), preflight_html):
                return SignResult("failed", "打开签到页失败")
            if login_page(preflight_html):
                # 预检就已经被踢到登录页：Cookie 失效时怎么验人机验证都没用，直接给结论
                return SignResult("failed", login_required_message("签到页跳转到了登录页"))
            initial = classify(preflight_html, rule)
            if initial.status in {"already", "success"}:
                return initial
            pre_state_pending = bool(first_keyword(page_text(preflight_html),
                                                  rule.get("pending_keywords")))

        submit_method = str(rule.get("submit_method") or "click")
        variables: Dict[str, Any] = {"cookies": cookies, "url": target,
                                     "beforeWait": page_wait,
                                     "wait": int(rule.get("wait") or 3500)}
        if submit_method == "click":
            variables["selector"] = rule["submit_selector"]
        elif submit_method in AUTO_SUBMIT_METHODS:
            # solve 只生成 Turnstile token、不触发站点回调：这里补一次显式提交
            variables["autoSubmit"] = _turnstile_auto_script(rule)
        elif submit_method in {"none", "skip"}:
            # 页面在人机验证通过后自己提交签到，不需要点任何按钮
            pass
        else:
            variables["submit"] = (_altcha_submit_script(rule) if submit_method == "altcha_click"
                                   else _submit_script(rule["submit_selector"], submit_method,
                                                       rule.get("captcha_input_selector"),
                                                       rule.get("submit_text")))
        if submit_method == "altcha_click":
            variables["altchaSelector"] = rule["altcha_checkbox_selector"]
        if rule["mode"] == "trigger_image":
            variables["trigger"] = _trigger_script(str(rule["trigger_selector"]))
            variables["triggerWait"] = int(rule.get("trigger_wait") or 500)
        if rule["mode"] != "open_page":
            variables["solveTimeout"] = solve_timeout_ms(rule)
        if rule["mode"] in {"image", "trigger_image"}:
            variables.update({"captchaSelector": rule["captcha_selector"],
                              "captchaInputSelector": rule["captcha_input_selector"]})
        if rule.get("verify_success_path"):
            variables["verifyUrl"] = target_url(base_url, str(rule["verify_success_path"]))
            if rule.get("verify_wait") is not None:
                try:
                    verify_wait = int(rule.get("verify_wait"))
                except (TypeError, ValueError):
                    verify_wait = 8000
                variables["verifyWait"] = max(1000, min(verify_wait, 20000))
        if user_agent:
            variables["userAgent"] = user_agent

        data = self._request(variables, "CheckIn", build_query(rule, user_agent))
        if isinstance(data, SignResult):
            return data
        after_html = str((data.get("after") or {}).get("html") or "")
        # auto 提交的现场诊断：token 有没有生成、回调有没有触发、表单有没有提交
        auto_hint = auto_submit_hint((data.get("autoDiag") or {}).get("value"))
        # 已经等满预算却仍卡在挑战页：给出可操作的提示，而不是笼统的「打开签到页失败」
        if mode == "cloudflare" and cloudflare_pending(after_html):
            return SignResult("failed", append_hint(
                f"Cloudflare 挑战在 {page_wait // 1000} 秒内未自动放行；"
                "可在该站规则里调大 cloudflare_wait 后重试", auto_hint))
        if not navigation_ok(mode, data.get("goto", {}).get("status"), after_html):
            return SignResult("failed", "打开签到页失败")
        solve = data.get("solve") or {}
        if rule["mode"] in {"image", "trigger_image"} and not solve.get("found"):
            return SignResult("failed", "未找到图片验证码；请检查验证码图片选择器")
        # Cloudflare / Turnstile 存在「solve 没解成功、但页面自己跑完 JS 也放行了」的情况
        # （观众 Audiences 就是等一会儿自动过），所以先按页面实际内容判定，最后才回落到
        # 「找到验证码但未能完成验证」。
        solve_failed = (rule["mode"] != "open_page" and solve.get("found")
                        and not solve.get("solved"))
        submitted = (data.get("submit") or {}).get("value")
        if rule["mode"] in {"image", "trigger_image"} and submitted in (False, "false"):
            return SignResult("failed", "验证码未自动填入输入框，未发送签到请求")
        ajax_result = classify_ajax_response((data.get("response") or {}).get("value"), rule)
        if rule.get("verify_success_path"):
            verify = data.get("verify") or {}
            verify_html = str((data.get("verifyPage") or {}).get("html") or "")
            if cloudflare_pending(verify_html):
                return SignResult("failed", append_hint(
                    "签到后二次核验仍停留在 Cloudflare 人机验证页，未确认签到成功", auto_hint))
            if login_page(verify_html):
                return SignResult("failed", login_required_message("二次核验时签到页跳转到了登录页"))
            if not navigation_ok(mode, verify.get("status"), verify_html):
                return SignResult("failed", "无法打开签到页确认签到记录")
            verified = classify(verify_html, rule)
            if verified.status in {"success", "already"}:
                return SignResult(
                    "success",
                    _bonus_message("已重新打开签到页确认签到成功",
                                   page_text(verify_html), rule),
                )
            pending = first_keyword(page_text(verify_html), rule.get("pending_keywords"))
            if pending:
                return SignResult("failed", append_hint(
                    f"签到后二次核验仍停留在「{pending}」（人机验证未通过或签到未提交）",
                    auto_hint))
            return SignResult("failed", append_hint(
                "签到后二次核验未显示签到成功状态；" + page_hint(verify_html), auto_hint))
        if ajax_result:
            return ajax_result
        result = classify(after_html, rule)
        if result.status == "failed":
            before = classify(str((data.get("before") or {}).get("html") or ""), rule)
            if before.status == "already":
                return before
        # 配了 pending_keywords 的站点（如观众 Audiences）：页面仍停在「人机验证中」说明验证
        # 没过或签到没提交，给出明确结论；若进页面时是未签到、跑完变成已签到，按签到成功上报。
        pending = first_keyword(page_text(after_html), rule.get("pending_keywords"))
        if pending and result.status not in {"success", "already"}:
            return SignResult("failed", append_hint(
                f"页面仍停留在「{pending}」（人机验证未通过或签到未提交）；"
                + page_hint(after_html), auto_hint))
        if solve_failed and result.status not in {"success", "already"}:
            return SignResult("failed", append_hint(
                "找到验证码但未能完成验证（Turnstile/Cloudflare 可能未通过）", auto_hint))
        if result.status == "already" and pre_state_pending:
            return SignResult("success", _bonus_message("签到成功", page_text(after_html), rule))
        if result.status == "failed" and login_page(after_html):
            return SignResult("failed", login_required_message("签到页跳转到了登录页"))
        if result.status == "failed" and auto_hint:
            return SignResult("failed", f"{result.message}；{auto_hint}", result.challenge)
        return result

    def _request(self, variables: Mapping[str, Any], operation: str, query: str) -> Any:
        payload = json.dumps({"query": query, "operationName": operation,
                              "variables": variables}).encode()
        request = urllib.request.Request(self.endpoint, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                return SignResult("failed", f"Browserless 鉴权失败（HTTP {exc.code}），请检查 Token")
            if exc.code == 429:
                return SignResult("failed", "Browserless 额度不足或请求过于频繁（HTTP 429）")
            return SignResult("failed", f"Browserless HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return SignResult("failed", f"Browserless 请求失败：{exc}")
        errors = value.get("errors") or []
        if errors:
            message = str(errors[0].get("message") or "未知错误")
            if "waiting for page navigation" in message.lower():
                return SignResult("failed", "Browserless 页面导航超时；请检查该站是否可由当前节点访问")
            return SignResult("failed", "Browserless 执行失败：" + message)
        return value.get("data") or {}
