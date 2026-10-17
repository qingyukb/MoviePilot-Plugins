"""PT 签到核心逻辑：HTTP 引擎 + Browserless(BrowserQL) 引擎 + 站点登录 / 最近动向。

本模块不含任何 MoviePilot 依赖，方便单独测试与复用。
Browserless 相关实现参考 tmzg0000/moviepilot-plugins 的 captchasignin 插件
（AGPL/MIT 见原仓库），在其基础上精简并与 HTTP 引擎合并为双引擎结构。
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import unescape as _html_unescape
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
# Turnstile 复选框的深度选择器（保留给确实能命中的自定义规则）。观众 Audiences 的
# Turnstile 渲染在 closed shadow DOM 里：DOM 中既没有 checkbox，也没有可命中的 iframe，
# 该选择器必然等不到元素，所以观众预设不再使用它，改用官方 solve（支持 iframe / shadow DOM）。
TURNSTILE_CHECKBOX_SELECTOR = "< *challenges.cloudflare.com* input[type='checkbox']"
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
# 二次注入（把 solve 返回的 token 回填页面并触发提交）的 evaluate 步骤必须在脚本自身等待
# 预算之外再留出余量：BQL 的 evaluate 步骤默认 30 秒超时（可用 preferences 改），预算相等时
# 脚本会在等待循环里被步骤超时掐死，连一句诊断都写不出来——只留下脚本刚启动时的初始快照，
# 日志里那句「未拿到 Turnstile token」就是这么来的假结论。
INJECT_STEP_MARGIN_MS = 12000
# 复用 solve 所在浏览器会话（BrowserQL reconnect）的默认空闲保留毫秒数。
DEFAULT_RECONNECT_WAIT = 60000
# 第一段 Cloudflare 挑战通过后，等待签到页内嵌 Turnstile 渲染的默认毫秒数。
DEFAULT_SOLVE2_WAIT = 3000
# Turnstile 复选框从外层 Cloudflare 放行到 iframe 渲染完成的最长等待时间。
DEFAULT_TURNSTILE_WAIT = 45000
# 网络层失败（跨境链路抖动 / 节点临时故障）时的重试间隔（秒）。
RETRY_DELAY_SECONDS = 3
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
    # ---- AGSVPT（agsvpt.com）：NexusPHP 签到页，页面上是一个「立即签到」表单提交按钮 ----
    # GET /attendance.php 只会把按钮渲染出来，不点按钮不会签到（实测未登录时 302 到
    # login.php?returnto=attendance.php），所以 HTTP 引擎必须用 POST；万一站点开了验证码 /
    # 撞上盾，自动回退到浏览器引擎时会按下面的选择器与按钮文字点「立即签到」。
    # 判定文案沿用默认正则（签到成功 / 已经签到），不额外收窄。
    "agsvpt.com": {
        "engine": "http",
        "method": "POST",
        "path": "/attendance.php",
        "submit_selector": "input[type='submit'], button[type='submit'], button",
        "submit_text": "立即签到",
    },
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
    # ---- BTSCHOOL（btschool.club，学校）：签到按钮直接挂在首页，没有独立签到页 ----
    # 实测 /attendance.php、/signin.php、/checkin.php 等全是 404，首页上是一个「每日签到」
    # 提交按钮，所以直接 POST 首页即可；判定沿用默认正则（今天已签到 / 签到成功），
    # 「您获得 N 点魔力值」这类奖励是每天不同的，由 extract_bonus 每次从页面文本抓取，不写死。
    "btschool.club": {
        "engine": "http",
        "method": "POST",
        "path": "/index.php",
        "submit_selector": "input[type='submit'], button[type='submit'], button",
        "submit_text": "每日签到",
        # 这站签到成功时**不给任何提示**，只是把「每日签到」入口拿掉（实测），所以用
        # 「入口消失」作为完成依据；奖励（如「获得 N 点魔力值」）仍按页面原文抓。
        "absent_keywords": ["每日签到"],
        "absent_status": "already",
    },
    # ---- 观众（Audiences）：签到页内嵌 Turnstile，验证通过后由站点自行提交 ----
    # 该站 Turnstile 渲染在 closed shadow DOM 里（本地 Edge 复现），页面 JS 读不到
    # cf-turnstile-response，DOM 上也没有可调的 data-callback；而 Browserless 的 solve 只
    # 负责生成 token（写在响应的 token 字段里），不会触发站点挂在自己 widget 上的提交逻辑。
    # 所以 submit_method=auto 走两段式：
    #   第一段：打开签到页 + solve 拿 token + reconnect 留住同一个浏览器会话；
    #   第二段：在同一会话里把 token 回填页面、试着调站点回调，必要时按表单真实 action 兜底
    #          POST（上游 NexusPHP 的契约就是 POST /attendance.php 带 cf-turnstile-response），
    #          最后在同一个会话里重新打开 attendance.php 核验真实状态。
    "audiences.me": {
        "engine": "browserless",
        "mode": "cloudflare",
        "path": "/attendance.php",
        "submit_method": "auto",
        "auto_wait": 30000,
        "cloudflare_wait": 15,
        "solve_count": 1,
        "solve_timeout": 40000,
        "wait": 15000,
        # 签到成功后页面可能跳回首页；同一浏览器会话重新打开 attendance.php 再确认一次，
        # 避免只看到首页就误报失败，也避免把未完成的验证当成成功。
        "verify_success_path": "/attendance.php",
        "verify_wait": 15000,
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
    # 网络层失败（读超时 / 连接中断）时置 True：请求是否真的执行过未知，调用方可以补一次
    # 只读回查，避免把「已签到但响应丢失」误报成失败。
    transient: bool = False
    # 签到路径不存在（HTTP 404 / 405）时置 True：调用方可以换签到入口再试
    missing_path: bool = False


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


def preset_for_host(host: str) -> Optional[Dict[str, Any]]:
    """按域名取内置预设：先精确匹配，再按「以 .<预设域名> 结尾」兜底。

    站点常用 `pt.` / `www.` / `bbs.` 前缀（例如学校是 `pt.btschool.club`，预设键写的是
    `btschool.club`），这样不必为每个子域各写一条；`open.cd` 的子域也一并覆盖。
    """
    preset = DEFAULT_RULES.get(host)
    if preset is not None:
        return preset
    for key, value in DEFAULT_RULES.items():
        if host.endswith("." + key):
            return value
    return None


def resolve_rule(site: Mapping[str, Any], custom_rules: Mapping[str, Mapping[str, Any]],
                 default_engine: str = "http") -> Dict[str, Any]:
    host = normalize_host(str(site.get("url") or ""))
    custom = (custom_rules.get(str(site.get("id"))) or custom_rules.get(host)
              or custom_rules.get("www." + host) or {})
    preset = preset_for_host(host)
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
    if str(result.get("submit_method") or "").lower() == "turnstile_click" \
            and not str(result.get("submit_selector") or "").strip():
        raise ValueError("Turnstile 点击规则缺少 submit_selector")
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
    # 让用户在 HTTP 规则里写的等待参数（如 cloudflare_wait）与点击目标（submit_selector /
    # submit_text，例如 AGSVPT 的「立即签到」）对自动回退同样生效
    for name in ("cloudflare_wait", "wait", "preflight_wait", "submit_selector", "submit_text"):
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
        session = requests.Session()
        session.headers.update(headers)
        method = str(rule.get("method") or "GET").upper()
        # 规则里的 data：签到表单需要提交字段时用（例如首页「每日签到」按钮带 name）
        data = rule.get("data") if isinstance(rule.get("data"), Mapping) else None
        if method == "POST":
            response = session.post(url, data=data, timeout=timeout,
                                    allow_redirects=True, proxies=proxies)
        else:
            response = session.get(url, params=data, timeout=timeout,
                                   allow_redirects=True, proxies=proxies)
    except Exception as error:
        return SignResult("failed", f"请求异常：{error}")
    content = response.text or ""
    if response.status_code >= 400:
        return SignResult("failed", f"请求失败（HTTP {response.status_code}）",
                          missing_path=response.status_code in {404, 405})
    return judge_http(content, rule)


# 常见签到入口路径：默认路径 404 时按顺序再试，仍然找不到就从站点首页自动发现
FALLBACK_SIGNIN_PATHS = ("/attendance.php", "/signin.php", "/sign_in.php", "/checkin.php",
                         "/plugin_sign-in.php", "/attendance.php?action=sign")
# 首页里「签到」入口的文字（链接文字或提交按钮文字）
SIGNIN_LINK_WORDS = ("签到", "簽到", "打卡")
_SIGNIN_LINK_RE = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_SUBMIT_RE = re.compile(r"<(?:input|button)\b[^>]*>", re.IGNORECASE)
_BUTTON_TEXT_RE = re.compile(r"<button\b[^>]*>(.*?)</button>", re.IGNORECASE | re.DOTALL)


def fetch_page(url: str, cookie: str, timeout: int = 30,
               proxies: Optional[Dict[str, str]] = None, user_agent: str = "") -> str:
    """GET 一个页面并返回 HTML（失败返回空串），用于发现签到入口。"""
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent or DEFAULT_USER_AGENT,
                            "Cookie": str(cookie or ""),
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception:
        return ""
    if response.status_code >= 400:
        return ""
    return response.text or ""


def same_site_path(base_url: str, href: str) -> str:
    """把页面里的链接转成同站路径；跨域、锚点、javascript: 一律丢弃。"""
    value = str(href or "").strip()
    if not value or value.startswith(("#", "javascript:", "mailto:", "data:")):
        return ""
    target = urllib.parse.urljoin(base_url.rstrip("/") + "/", value)
    parsed, base = urllib.parse.urlparse(target), urllib.parse.urlparse(base_url)
    if not parsed.hostname or parsed.hostname != base.hostname:
        return ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path if path.startswith("/") else ""


def discover_signin_path(html: str, base_url: str) -> str:
    """从首页 HTML 里找文字含「签到 / 打卡」的链接，返回同站路径（找不到返回空串）。

    链接文字里的空白先全部去掉，兼容「每 日 签 到」这类带空格的排版。
    """
    for href, label in _SIGNIN_LINK_RE.findall(html or ""):
        text = re.sub(r"\s+", "", plain_text(label))
        if not any(word in text for word in SIGNIN_LINK_WORDS):
            continue
        path = same_site_path(base_url, href)
        if path:
            return path
    return ""


def _submit_control(inner: str) -> Dict[str, str]:
    """表单里的提交控件属性（``<input type=submit>`` / ``<button>``），没有就返回空字典。"""
    for tag in _SUBMIT_RE.findall(inner or ""):
        attributes = _tag_attrs(tag)
        kind = (attributes.get("type") or "").lower()
        if tag.lower().startswith("<button"):
            if kind in {"", "submit"}:
                return attributes
        elif kind == "submit":
            return attributes
    return {}


def discover_signin_target(html: str, base_url: str) -> Dict[str, Any]:
    """找签到入口，返回 ``{"path", "method", "data"}``（找不到返回空字典）。

    先看表单：首页上直接挂一个「每日签到」提交按钮的站点（BTSCHOOL / 学校 就是这种）没有独立
    签到页，必须按表单的 action / method 提交，按钮带 name 时还要把它的字段一起发过去；
    表单里找不到再退回「找链接」。
    """
    body = html or ""
    for raw_attrs, inner in _FORM_RE.findall(body):
        control = _submit_control(inner)
        if not control:
            continue
        label = re.sub(r"\s+", "", plain_text(inner) + str(control.get("value") or ""))
        if not any(word in label for word in SIGNIN_LINK_WORDS):
            continue
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        path = same_site_path(base_url, attrs.get("action") or "") or \
            (urllib.parse.urlparse(base_url).path or "/")
        method = (attrs.get("method") or "POST").strip().upper()
        target: Dict[str, Any] = {"path": path, "method": "GET" if method == "GET" else "POST"}
        name = str(control.get("name") or "").strip()
        if name:
            value = control.get("value")
            if value is None:
                match = _BUTTON_TEXT_RE.search(inner)
                value = re.sub(r"\s+", "", plain_text(match.group(1))) if match else ""
            target["data"] = {name: str(value or "")}
        return target
    found = discover_signin_path(body, base_url)
    return {"path": found, "method": "GET"} if found else {}


def http_sign_auto(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                   proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """HTTP 签到：默认路径 404 / 405 时自动换签到入口，并把找法写进失败原因。

    站点用的签到路径五花八门（`/attendance.php`、`/signin.php`、自定义路由……）。以前路径写死，
    碰上没有 `/attendance.php` 的站点只会得到一句「请求失败（HTTP 404）」。现在的顺序是：
    先试规则里的 path → 从站点首页「签到」链接自动发现 → 再试几个常见路径。
    """
    result = http_sign(site, rule, timeout=timeout, proxies=proxies)
    if not result.missing_path:
        return result
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    user_agent = str(site.get("ua") or "").strip()
    tried = [str(rule.get("path") or DEFAULT_HTTP_PATH)]
    # 首页里可能挂着「每日签到」表单（学校 BTSCHOOL 就是这种）或一个签到链接
    target = discover_signin_target(
        fetch_page(target_url(base_url, DEFAULT_INDEX_PATH), cookie, timeout, proxies, user_agent),
        base_url)
    attempts: List[Dict[str, Any]] = []
    discovered = str(target.get("path") or "")
    if discovered and discovered not in tried:
        attempt = dict(rule, path=discovered)
        if target.get("method"):
            attempt["method"] = str(target["method"])
        if target.get("data"):
            attempt["data"] = dict(target["data"])
        attempts.append(attempt)
    for path in FALLBACK_SIGNIN_PATHS:
        if path in tried or any(item.get("path") == path for item in attempts):
            continue
        attempts.append(dict(rule, path=path))
    if not attempts:
        return result
    for attempt in attempts[:4]:
        path = str(attempt.get("path") or "")
        tried.append(path)
        retry = http_sign(site, attempt, timeout=timeout, proxies=proxies)
        if not retry.missing_path:
            if retry.status in {"success", "already"}:
                return SignResult(retry.status, f"{retry.message}（签到入口：{path}）")
            return retry
    return SignResult("failed", f"没找到可用的签到入口（HTTP 404）：已试过 {'、'.join(tried)}；"
                                "可在自定义规则里用 path 指定该站的签到路径；若该站本身没有签到功能，"
                                "把它从「签到站点」移到「登录站点」即可（插件仍会登录并读最近动向）")


def plain_text(content: str) -> str:
    """去掉注释、script/style 与 HTML 标签并压缩空白，便于按文案匹配。

    最后再解一次 HTML 实体：站点源码里常把 `<` 写成 `&lt;`（例如「2026-10-17 03:52:21
    ( &lt; 1分前 )」），不解码的话结果里会原样带出 `&lt;`。
    """
    text = re.sub(r"<!--.*?-->", " ", content, flags=re.DOTALL)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # 标签名必须以字母开头，避免把站点相对时间里的 <（如 " < 1分前"）当成标签吞掉
    text = re.sub(r"</?[a-zA-Z][^>]*>", " ", text)
    text = _html_unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_reward_sentence(content: str) -> str:
    """只认「获得 X 点魔力值」这种明确的奖励句子。

    静默成功的站点（返回的是首页）侧栏也有「魔力值 [使用]: 242,141.0」这类统计数字，
    用宽泛的 extract_bonus 会把它们当成奖励，所以这里只匹配带「获得 / 签到已得」的说法。
    """
    match = re.search(REWARD_SENTENCE_PATTERN, str(content or ""), re.IGNORECASE)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


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
            # 顺手去掉尾部的逗号 / 顿号（站点文案常写成「魔力值 65,」这类）
            return re.sub(r"\s+", " ", text).strip().strip(",，、;；")
    return ""


def message_bonus(message: Any) -> str:
    """从结果文案里取出奖励部分：「签到成功（获得 20 粒爆米花）」→「获得 20 粒爆米花」。

    详情页「结果」列用它显示实际到手的东西（魔力值 / 爆米花 / 奖励），比整句文案清爽。
    """
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    match = re.search(r"[（(]([^（()）]{1,80})[)）]\s*$", text)
    if not match:
        return ""
    bonus = match.group(1).strip().strip(",，、;；:：")
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", bonus):
        return "获得 " + bonus
    return bonus


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
    # 静默成功：有的站点签到完不给任何提示，只是把「每日签到」入口拿掉（BTSCHOOL/学校 就是这种）。
    # 规则里写了 absent_keywords 时，返回页面里这些文字全都不见了 → 视为已完成；奖励仍从整页
    # 原始 HTML 里抓（站点可能把它塞在 JS alert / 隐藏节点里，plain_text 看不到）。
    # 注意要在**原始 HTML** 里找这些文字：签到入口是 `<input value="每日签到">`，去标签后的
    # 可见正文里本来就没有这个词，用 visible 判断会把「入口还在」误当成「入口已消失」。
    absent = [str(item).strip() for item in (rule.get("absent_keywords") or [])
              if str(item).strip()]
    if absent and not login_page(content) and not cloudflare_pending(content) \
            and not any(word in content or word in visible for word in absent):
        status = "success" if str(rule.get("absent_status") or "").lower() == "success" else "already"
        prefix = "签到成功" if status == "success" else "今日已签到"
        bonus = extract_reward_sentence(text)
        return SignResult(status, f"{prefix}（{bonus}）" if bonus else prefix)
    return SignResult("failed", f"未匹配到签到成功标志；{page_hint(text)}")


# ======================================================== 站点登录 + 最近动向

# 很多 NexusPHP 站点的站点 Cookie 会过期：签到失败其实是「没登录」。配置了账号后，
# 插件会在签到前用账号密码真实登录一次、换回新的 Cookie，签到引擎随后用这份新 Cookie；
# 登录后再打开用户页抓「最近动向」，作为该账号真实登录 / 活跃的证据。
DEFAULT_LOGIN_PATH = "/login.php"
DEFAULT_LOGIN_ACTION = "/takelogin.php"
DEFAULT_INDEX_PATH = "/index.php"
DEFAULT_USERDETAILS_PATH = "/userdetails.php"
DEFAULT_USERNAME_FIELD = "username"
DEFAULT_PASSWORD_FIELD = "password"
# 登录成功的特征：页面出现登出 / 退出入口，或拿到了 NexusPHP 的登录 Cookie
LOGIN_SUCCESS_PATTERN = r"(logout\.php|退出|登出|注销|我的控制面板|我的面板)"
# 登录失败特征：明确提示账号密码错误
LOGIN_FAILURE_PATTERN = (r"(用户名或密码错误|用户名或密碼錯誤|账号或密码错误|密碼錯誤"
                         r"|登录失败|登入失敗|invalid (?:username|password)|incorrect password)")
# 登录页要求人机校验时的特征
LOGIN_CAPTCHA_PATTERN = r"(captcha|imagestring|验证码|驗證碼|turnstile)"
# 用户页里「最近动向」相关关键词（含繁体）
ACTIVITY_KEYWORDS = ("最近动向", "最近動向", "最近活动", "最近活動", "最近访问", "最近訪問",
                     "上次访问", "上次訪問", "上次签到", "上次簽到", "上次活动", "上次活動",
                     "最后访问", "最後訪問", "最后活动", "最後活動", "最后上线", "最後上線",
                     "最后活跃", "最後活躍", "上次登录", "上次登入")
# 站点常见的时间格式（NexusPHP 多为 YYYY-MM-DD HH:MM:SS）
DATETIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)"
# 没有关键词命中时用的严格时间格式：必须带时分。裸日期（2026-09-18）在种子列表、
# 公告、排行榜里到处都是，拿它当「最近动向」只会给出一个看起来很旧的假值。
ACTIVITY_TIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
# 关键词后面允许多长的间隔才算「紧跟着时间」：站点写法都是「上次访问：2026-…」这种，
# 窗口开太大就会把旁边字段（注册日期之类）的时间算成这个关键词的值。
ACTIVITY_WINDOW = 60
# 站点常常在时间后面再补一段相对时间，例如「最近动向：2026-10-16 23:44:21 ( < 1分前 )」
RELATIVE_TIME_PATTERN = r"\s*([（(]\s*[^（()）]{0,16}?前\s*[)）])"
_DATETIME_RE = re.compile(DATETIME_PATTERN)
_ACTIVITY_TIME_RE = re.compile(ACTIVITY_TIME_PATTERN)
_RELATIVE_TIME_RE = re.compile(RELATIVE_TIME_PATTERN)

_FORM_BLOCK_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_UID_RE = re.compile(r"userdetails\.php\?id=(\d+)", re.IGNORECASE)
# 「获得 87 点魔力值」这类明确的奖励句子（静默成功时只用它，避免误抓侧栏统计数字）
REWARD_SENTENCE_PATTERN = (r"((?:获得|獲得|獲取|签到已得|簽到已得)\s*[\d,]+(?:\.\d+)?"
                           r"\s*(?:个|粒|枚|点|個|顆)?\s*(?:魔力值|爆米花|积分|做种积分|金币|奖励))")


@dataclass(frozen=True)
class LoginResult:
    status: str     # success / failed
    message: str
    cookie: str = ""


@dataclass(frozen=True)
class ActivityResult:
    status: str     # success / failed
    message: str
    last_seen: str = ""
    snippet: str = ""
    source: str = ""   # 从哪个候选页读到的，便于排查「最近动向」读不到的问题


def parse_accounts(raw: Any) -> Dict[str, Dict[str, Any]]:
    """解析站点账号配置；键为域名或站点 ID，值为账号对象。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as error:
        raise ValueError(f"站点账号必须是有效 JSON：{error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("站点账号必须是以域名或站点 ID 为键的 JSON 对象")
    result: Dict[str, Dict[str, Any]] = {}
    for key, account in value.items():
        if not isinstance(key, str) or not isinstance(account, Mapping):
            continue
        username = str(account.get("username") or "").strip()
        password = str(account.get("password") or "")
        if not username or not password:
            continue
        result[key.lower().strip()] = dict(account)
    return result


def account_for(site: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """按站点 ID 或域名取出该站的登录账号，未配置时返回 None。"""
    if not accounts:
        return None
    site_id = str(site.get("id") or "").strip()
    if site_id and site_id in accounts:
        return dict(accounts[site_id])
    host = normalize_host(str(site.get("url") or ""))
    for key in (host, "www." + host):
        if key and key in accounts:
            return dict(accounts[key])
    return None


# MoviePilot「站点管理」(SiteOper) 保存的是 Cookie / ApiKey / Token / UA / 代理等，没有账号
# 密码输入框；账号密码可以写在站点管理的「附加信息」(note) 里（V3 是 JSON 对象，V2 是字符
# 串），插件会自动解析；同时兼容站点记录里以后可能出现的 username / password 字段。
SITE_ACCOUNT_FIELDS = ("username", "user", "account", "password", "passwd", "pwd",
                       "login_path", "login_action", "username_field", "password_field",
                       "fields", "activity_path")


def note_object(note: Any) -> Dict[str, Any]:
    """把站点管理的「附加信息」解析成字典（V3 本来就是对象，V2 是 JSON 字符串）。"""
    if isinstance(note, Mapping):
        return dict(note)
    if isinstance(note, str):
        raw = note.strip()
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except ValueError:
                return {}
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def account_from_site(site: Mapping[str, Any]) -> Dict[str, Any]:
    """从 MoviePilot「站点管理」的站点记录里取登录凭证与登录选项（取不到时返回空字典）。"""
    account: Dict[str, Any] = {}
    for source in (note_object(site.get("note")), site):
        if not isinstance(source, Mapping):
            continue
        for key in SITE_ACCOUNT_FIELDS:
            value = source.get(key)
            if value not in (None, "") and key not in account:
                account[key] = value
    username = str(account.get("username") or account.get("user")
                   or account.get("account") or "").strip()
    password = str(account.get("password") or account.get("passwd") or account.get("pwd") or "")
    if username:
        account["username"] = username
    if password:
        account["password"] = password
    return account


def account_ready(account: Optional[Mapping[str, Any]]) -> bool:
    """凭证是否够做一次账号密码登录。"""
    if not account:
        return False
    return bool(str(account.get("username") or "").strip() and str(account.get("password") or ""))


def merge_accounts(*accounts: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """按优先级合并登录凭证，后面的覆盖前面的。"""
    merged: Dict[str, Any] = {}
    for account in accounts:
        if account:
            merged.update(dict(account))
    return merged


def _tag_attrs(tag: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        value = match.group(2)
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        attrs[match.group(1).lower()] = value
    return attrs


def find_login_form(html: str) -> Dict[str, Any]:
    """从登录页里挑出含密码框的表单，返回 {"action": str, "fields": {...}}。"""
    for raw_attrs, body in _FORM_BLOCK_RE.findall(html or ""):
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        fields: Dict[str, str] = {}
        has_password = False
        for input_tag in _INPUT_RE.findall(body):
            field = _tag_attrs(input_tag)
            name = field.get("name")
            if not name:
                continue
            kind = (field.get("type") or "text").lower()
            if kind == "password":
                has_password = True
                continue
            if kind in {"submit", "button", "image", "file", "reset", "checkbox", "radio"}:
                continue
            fields[name] = field.get("value", "")
        if has_password:
            return {"action": attrs.get("action") or "", "fields": fields}
    return {"action": "", "fields": {}}


def cookie_header(session: Any) -> str:
    """把 requests.Session 的 Cookie 拼成请求头字符串。"""
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)


def _has_password_input(html: str) -> bool:
    return bool(re.search(r"<input\b[^>]+type\s*=\s*[\"']?password", html or "", re.IGNORECASE))


def login(site: Mapping[str, Any], account: Mapping[str, Any], timeout: int = 30,
          proxies: Optional[Dict[str, str]] = None) -> LoginResult:
    """用账号密码真实登录站点，换回新的 Cookie（NexusPHP takelogin.php 通用）。"""
    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return LoginResult("failed", "站点地址为空")
    username = str(account.get("username") or "").strip()
    password = str(account.get("password") or "")
    if not username or not password:
        return LoginResult("failed", "未配置账号或密码")
    import requests
    login_path = str(account.get("login_path") or DEFAULT_LOGIN_PATH)
    login_action = str(account.get("login_action") or DEFAULT_LOGIN_ACTION)
    username_field = str(account.get("username_field") or DEFAULT_USERNAME_FIELD)
    password_field = str(account.get("password_field") or DEFAULT_PASSWORD_FIELD)
    headers = {"User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    session = requests.Session()
    try:
        page = session.get(target_url(base_url, login_path), headers=headers,
                           timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return LoginResult("failed", f"打开登录页失败：{error}")
    if page.status_code >= 400:
        return LoginResult("failed", f"打开登录页失败（HTTP {page.status_code}）")
    form = find_login_form(page.text or "")
    action = str(form.get("action") or "").strip()
    post_url = urllib.parse.urljoin(base_url + "/", action or login_action.lstrip("/"))
    data: Dict[str, str] = dict(form.get("fields") or {})
    extra = account.get("fields")
    if isinstance(extra, Mapping):
        for name, value in extra.items():
            data[str(name)] = str(value)
    data[username_field] = username
    data[password_field] = password
    try:
        response = session.post(post_url, data=data,
                                headers={**headers, "Referer": page.url,
                                         "Content-Type": "application/x-www-form-urlencoded"},
                                timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return LoginResult("failed", f"提交登录表单失败：{error}")
    if response.status_code >= 400:
        return LoginResult("failed", f"登录请求失败（HTTP {response.status_code}）")
    content = response.text or ""
    text = plain_text(content)
    cookie = cookie_header(session)
    if re.search(LOGIN_FAILURE_PATTERN, text, re.IGNORECASE):
        return LoginResult("failed", "登录被拒绝：账号或密码错误")
    if ("c_secure_uid" in cookie or "c_secure_pass" in cookie
            or re.search(LOGIN_SUCCESS_PATTERN, text, re.IGNORECASE)
            or (not _has_password_input(content) and "login.php" not in str(response.url))):
        return LoginResult("success", "登录成功", cookie)
    if re.search(LOGIN_CAPTCHA_PATTERN, content, re.IGNORECASE):
        return LoginResult("failed", "登录页要求验证码 / 人机校验，请改用站点 Cookie")
    return LoginResult("failed", "登录后仍停留在登录页：账号密码错误或站点要求两步验证")


def _normalize_time(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[/.]", "-", str(value or "").strip()))


def relative_suffix(text: str, start: int) -> str:
    """取出紧跟在时间后面的相对时间括号，例如「( < 1分前 )」。"""
    match = _RELATIVE_TIME_RE.match(text[start:start + 32])
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def activity_value(text: str, match: Any) -> str:
    """时间 + 站点自带的相对时间，例如「2026-10-16 23:44:21 ( < 1分前 )」。"""
    value = _normalize_time(match.group(1))
    suffix = relative_suffix(text, match.end())
    return f"{value} {suffix}" if suffix else value


def extract_activity(html: str) -> Dict[str, str]:
    """从用户页里抓「最近动向」片段与其中的时间，返回 keyword / snippet / last_seen。

    两个防呆点（都踩过坑）：

    1. 关键词后面必须紧跟时间才算命中。以前关键词附近没时间时会退回「全页第一个日期」，
       而种子列表 / 公告里的裸日期到处都是，于是「最近动向」被填成 `2026-09-18` 这种假值；
    2. 没有关键词命中时，只接受**带时分**的时间。裸日期一律不认——宁可返回空（页面会提示
       未读取到、可以配 activity_path），也不要给一个看起来像那么回事的错误时间。
    """
    text = plain_text(html or "")
    best: Any = None
    for order, keyword in enumerate(ACTIVITY_KEYWORDS):
        index = text.find(keyword)
        if index < 0:
            continue
        match = _DATETIME_RE.search(text, index, index + ACTIVITY_WINDOW)
        if match is None:
            # 这个关键词后面没有紧跟着时间（多半是别处的同名文字），换下一个关键词
            continue
        candidate = (match.start() - (index + len(keyword)), order, index, keyword, match)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is not None:
        _, _, index, keyword, match = best
        snippet = text[max(0, index - 12):index + 140].strip()
        return {"keyword": keyword, "snippet": snippet,
                "last_seen": activity_value(text, match)}
    match = _ACTIVITY_TIME_RE.search(text)
    if match:
        snippet = text[max(0, match.start() - 40):match.end() + 60].strip()
        return {"keyword": "", "snippet": snippet, "last_seen": activity_value(text, match)}
    return {}


def cookie_uid(cookie: str) -> str:
    """从 NexusPHP 的 Cookie 里解出用户 ID：``c_secure_uid`` 是 base64 过的 uid。

    自定义主题的站点首页常常不挂 ``userdetails.php?id=`` 链接，这时插件拿不到 uid、也就打不开
    用户页，「最近动向」自然读不到——用 Cookie 里这份 uid 兜底即可。
    """
    match = re.search(r"(?:^|;\s*)c_secure_uid=([^;]+)", str(cookie or ""), re.IGNORECASE)
    if not match:
        return ""
    value = urllib.parse.unquote(match.group(1).strip())
    if not value:
        return ""
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "ignore")
    except Exception:
        return ""
    decoded = decoded.strip()
    return decoded if decoded.isdigit() else ""


def read_activity(site: Mapping[str, Any], cookie: str, timeout: int = 30,
                  proxies: Optional[Dict[str, str]] = None,
                  path: Optional[str] = None,
                  paths: Optional[Sequence[str]] = None) -> ActivityResult:
    """用登录态 Cookie 打开用户页，读取站点展示的「最近动向」。

    path 优先，paths 是调用方按站点规则补充的候选页面（比如签到页 / 校验页）。
    """
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(cookie or "").strip()
    if not base_url or not cookie:
        return ActivityResult("failed", "站点地址或 Cookie 为空")
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
                            "Cookie": cookie,
                            "Referer": base_url + DEFAULT_INDEX_PATH,
                            "Accept": "text/html,application/xhtml+xml,xml;q=0.9,*/*;q=0.8"})
    try:
        index = session.get(target_url(base_url, DEFAULT_INDEX_PATH),
                            timeout=timeout, allow_redirects=True, proxies=proxies)
    except Exception as error:
        return ActivityResult("failed", f"读取站点首页失败：{error}")
    if index.status_code >= 400:
        return ActivityResult("failed", f"读取站点首页失败（HTTP {index.status_code}）")
    index_html = index.text or ""
    if login_page(index_html):
        return ActivityResult("failed", "登录态无效：站点把请求跳回了登录页")
    if cloudflare_pending(index_html):
        return ActivityResult("failed", "站点首页返回 Cloudflare 验证页，未能读取最近动向")
    # 用户页优先：先试调用方给的 activity_path / 签到页，再试能定位到的 userdetails.php
    candidates: List[str] = []
    if path:
        candidates.append(str(path))
    for extra in paths or ():
        value = str(extra or "").strip()
        if value:
            candidates.append(value)
    match = _UID_RE.search(index_html)
    # 优先用 Cookie 里的 c_secure_uid：那才是「你自己」；首页里的 userdetails.php?id= 链接
    # 常常指向**别人**（论坛帖作者、最新注册用户、版主），拿它去开资料页会读到别人的「最近动向」
    # （皇后那条 2026-05-06 00:00:00 就是这么来的）。Cookie 里解不出 uid 时才退回首页链接。
    uid = cookie_uid(cookie) or (match.group(1) if match else "")
    if uid:
        candidates.append(f"{DEFAULT_USERDETAILS_PATH}?id={uid}")
    candidates.append(DEFAULT_INDEX_PATH)
    seen: set = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            page = session.get(target_url(base_url, candidate),
                               timeout=timeout, allow_redirects=True, proxies=proxies)
        except Exception:
            continue
        if page.status_code >= 400:
            continue
        if cloudflare_pending(page.text or ""):
            # 撞盾时页面正文是挑战页，从中提取的「最近动向」是假的：直接换下一个候选页
            continue
        found = extract_activity(page.text or "")
        if found:
            return ActivityResult("success", f"已读取最近动向（{candidate}）",
                                  last_seen=found.get("last_seen", ""),
                                  snippet=found.get("snippet", ""),
                                  source=str(candidate))
    return ActivityResult("failed", "未在站点页面找到「最近动向」")


# ---------------------------------------------------- 结果记录汇总（插件详情页用）
# 一轮签到会留下两类记录：签到记录（success / already / failed）与登录记录（login /
# login_failed）。同时勾了「签到站点」和「登录站点」的站点只跑一次（真实登录 + 签到 +
# 读最近动向），同一条记录带上 dual 标记后同时进两块统计。
SIGNIN_OK_STATUSES = {"success", "already"}
LOGIN_STATUSES = {"login", "login_failed"}
# 失败里属于「结果未知、需要人工确认」的那类：网络中断、二次核验没确认、token 注入没响应。
# 单独计数，避免和「Cookie 失效」这种明确失败混在一起。
UNCERTAIN_PATTERN = (r"(未确认|未能确认|响应丢失|未拿到可用响应|未显示签到成功状态"
                     r"|未拿到 Turnstile token|未识别到签到成功结果|未找到图片验证码)")


def record_uncertain(row: Mapping[str, Any]) -> bool:
    """这条失败记录是不是「结果未知、需人工确认」的那类。"""
    if str(row.get("status") or "") not in {"failed", "login_failed"}:
        return False
    if row.get("transient"):
        return True
    return bool(re.search(UNCERTAIN_PATTERN, str(row.get("message") or "")))


def is_login_record(row: Mapping[str, Any]) -> bool:
    """这条记录要不要进「登录状态」。

    纯登录记录、双选站点的签到记录都算；此外只要这一轮确实做过登录（有登录结论）或读过
    「最近动向」，也一并计入——1.4.0 之前的记录没有 dual 标记，这样历史条数不会因为升级
    而突然变成 0。
    """
    if bool(row.get("dual")) or str(row.get("status") or "") in LOGIN_STATUSES:
        return True
    return bool(str(row.get("login") or "").strip() or str(row.get("activity") or "").strip())


def is_signin_record(row: Mapping[str, Any]) -> bool:
    """这条记录要不要进「签到状态」：签到记录（含双选站点）算，纯登录记录不算。"""
    return str(row.get("status") or "") not in LOGIN_STATUSES


def record_meta(row: Mapping[str, Any]) -> Dict[str, str]:
    """记录的状态标签 / 颜色 / 图标；「未确认」的失败单独标色，提醒人工核对。"""
    status = str(row.get("status") or "")
    if record_uncertain(row):
        return {"label": "未确认", "color": "warning", "icon": "mdi-help-circle"}
    if status == "success":
        return {"label": "签到成功", "color": "success", "icon": "mdi-check-circle"}
    if status == "already":
        return {"label": "今日已签到", "color": "success", "icon": "mdi-check-circle"}
    if status == "login":
        return {"label": "登录成功", "color": "success", "icon": "mdi-login"}
    if status == "login_failed":
        return {"label": "登录失败", "color": "error", "icon": "mdi-alert-circle"}
    return {"label": "签到失败", "color": "error", "icon": "mdi-alert-circle"}


def summarize_records(records: Sequence[Mapping[str, Any]],
                      expected_sites: Sequence[str] = ()) -> Dict[str, int]:
    """汇总一轮记录：成功 / 失败 / 未确认 / 未记录。

    * 成功：success、already、login；
    * 未确认：结果未知、需人工核对的失败（与「失败」互斥，单独计数）；
    * 失败：其余可判定的失败（含 login_failed）；
    * 未记录：expected_sites（本轮勾选的站点）里一条记录都没有的站点数。
    """
    success = failed = uncertain = 0
    seen: set = set()
    for row in records:
        name = str(row.get("site") or "")
        if name:
            seen.add(name)
        status = str(row.get("status") or "")
        if record_uncertain(row):
            uncertain += 1
        elif status in SIGNIN_OK_STATUSES or status == "login":
            success += 1
        else:
            failed += 1
    missing = len({str(name) for name in expected_sites if str(name)} - seen)
    return {"total": len(list(records)), "success": success, "failed": failed,
            "uncertain": uncertain, "missing": missing}


def group_records_by_site(records: Sequence[Mapping[str, Any]]
                          ) -> Dict[str, List[Dict[str, Any]]]:
    """按站点名分组，保持首次出现的顺序（详情页按这个顺序渲染站点卡片）。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        name = str(row.get("site") or "未知站点")
        grouped.setdefault(name, []).append(dict(row))
    return grouped


def decode_entities(text: Any) -> str:
    """把文本里残留的 HTML 实体解码。

    用于展示层兜底：修复前存进历史的记录里是 `2026-10-17 03:52:21 ( &lt; 1分前 )`，
    显示时解码一次就能立刻变正常，不必等下一轮重新抓取。
    """
    return _html_unescape(str(text or ""))


def site_record_counts(records: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """每个站点的记录条数（详情页的「N 条记录」）。"""
    counts: Dict[str, int] = {}
    for row in records:
        name = str(row.get("site") or "未知站点")
        counts[name] = counts.get(name, 0) + 1
    return counts


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


def session_endpoint(address: str, token: str) -> str:
    """给 BrowserQL ``reconnect`` 返回的会话地址补上 API token（该地址本身不含 token）。

    只补查询参数、不改路径：会话地址自带 ``/e/<uuid>/...`` 前缀，重写路径会让它失效。
    """
    address, token = str(address or "").strip(), str(token or "").strip()
    if not address or not token:
        return ""
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "",
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


def _turnstile_inject_script(rule: Mapping[str, Any], token: str = "") -> str:
    """把 solve 返回的 Turnstile token 回填页面并触发站点提交（观众 Audiences 类站点）。

    Browserless 的 ``solve`` 只负责生成 token（写在响应的 ``token`` 字段里），**不会**触发
    站点挂在 Turnstile 上的提交逻辑；而签到页的 widget 往往渲染在 closed shadow DOM 里，页面
    JS 既读不到 ``input[name=cf-turnstile-response]``，也找不到 ``data-callback`` 元素。所以
    token 必须由插件从 solve 响应里带回来，在同一个浏览器会话里显式注入，再按页面实际结构
    依次补一刀：

    1. 先等 Vue / Turnstile 组件渲染出来（导航后是异步渲染的，太早注入会落空）；
    2. 把 token 写进所有能找到的 ``cf-turnstile-response`` 输入框（含 open shadow DOM）；
       页面自己已经产出 token 时（``turnstile.getResponse()``）优先用它；
    3. 调用站点回调：DOM 上的 ``data-callback``，以及闭包渲染时常见的全局函数名候选；
    4. 页面仍停在 pending 文案时，再按表单真实 ``action`` 做同源 POST 兜底（没有表单就 POST
       当前签到页），带上表单字段与 token——上游 NexusPHP 的契约就是
       ``POST /attendance.php`` 带 ``cf-turnstile-response``。

    每一步都写进 ``window.__ptsignin_auto`` / sessionStorage，由 ``auto_submit_hint`` 输出，
    保证诊断是真实观测，而不是脚本刚启动时的初始快照。脚本自身等待预算为 ``auto_wait``，
    调用方必须把 evaluate 步骤超时设成 ``auto_wait + INJECT_STEP_MARGIN_MS``（见
    ``submit_timeout_ms``），否则脚本会在等待循环里被步骤超时掐死，诊断全丢。
    """
    wait = auto_wait_ms(rule)
    pending = json.dumps([str(item) for item in (rule.get("pending_keywords") or [])],
                         ensure_ascii=False)
    return """(async () => {
      const solveToken = %s;
      const pendingWords = %s;
      const sel = %s;
      const waitBudget = %d;
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const diag = {url: location.href, token: false, src: null, len: 0, cb: null, cands: [],
                    called: null, form: false, act: null, post: null, still: null,
                    rendered: false, turnstile: false, inputs: 0, widget: false, frames: 0,
                    text: ''};
      const save = () => {
        window.__ptsignin_auto = diag;
        try { sessionStorage.setItem('__ptsignin_auto', JSON.stringify(diag)); } catch (err) {}
      };
      save();
      const start = Date.now();
      const deadline = start + waitBudget;

      // open shadow root 也要一起扫：widget 用 open 模式渲染时，响应输入框藏在 shadow root 里
      const picker = () => {
        const roots = [document];
        for (let index = 0; index < roots.length; index += 1) {
          const all = roots[index].querySelectorAll('*');
          for (let i = 0; i < all.length; i += 1) {
            if (all[i].shadowRoot) roots.push(all[i].shadowRoot);
          }
        }
        return (selector) => {
          const found = [];
          roots.forEach((root) => {
            root.querySelectorAll(selector).forEach((node) => {
              if (found.indexOf(node) < 0) found.push(node);
            });
          });
          return found;
        };
      };
      let pick = picker();
      let inputs = pick(sel);
      let widgets = pick('[data-sitekey], .cf-turnstile');
      const apiReady = () => !!(window.turnstile && typeof window.turnstile.getResponse === 'function');
      const renderReady = () => inputs.length > 0 || widgets.length > 0 || apiReady();
      const refresh = () => {
        pick = picker();
        inputs = pick(sel);
        widgets = pick('[data-sitekey], .cf-turnstile');
      };
      const readApi = () => {
        try { return apiReady() ? (window.turnstile.getResponse() || '').trim() : ''; }
        catch (err) { return ''; }
      };
      const readInput = () => {
        return inputs.map((node) => (node.value || '').trim()).filter(Boolean)[0] || '';
      };
      const putToken = (value) => {
        inputs.forEach((node) => {
          node.value = value;
          node.dispatchEvent(new Event('input', { bubbles: true }));
          node.dispatchEvent(new Event('change', { bubbles: true }));
        });
      };
      const stillPending = () => {
        const text = (document.body && document.body.innerText) || '';
        return pendingWords.some((word) => word && text.indexOf(word) !== -1);
      };

      // A) 页面刚导航完，Vue 应用与 Turnstile 组件是异步渲染的：先等它们出现，否则注入无处可去
      while (!renderReady() && Date.now() < deadline && Date.now() < start + 12000) {
        await sleep(250);
        refresh();
      }
      diag.url = location.href;
      diag.rendered = renderReady();
      diag.inputs = inputs.length;
      diag.widget = widgets.length > 0;
      diag.frames = pick('iframe').length;
      diag.turnstile = apiReady();
      save();

      let token = (solveToken || '').trim();
      if (token) { diag.src = 'solve'; }
      else {
        token = readInput();
        if (token) { diag.src = 'input'; }
        else { token = readApi(); if (token) { diag.src = 'turnstile'; } }
      }
      if (token) { putToken(token); }

      // B) solve 没给 token 时，继续等页面自己产出（可能站点自己的 widget 过了验证）
      if (!token) {
        let refreshNow = true;
        while (!token && Date.now() < deadline) {
          await sleep(250);
          if (refreshNow) { refresh(); }
          refreshNow = !refreshNow;
          token = readInput() || readApi();
          if (token) { diag.src = diag.src || 'late'; putToken(token); }
        }
      }
      diag.token = !!token;
      diag.len = token.length;
      save();

      const forms = pick('form');
      const form = forms.filter((item) => /attendance/i.test(item.getAttribute('action') || ''))[0]
        || forms.filter((item) => item.querySelector(sel))[0]
        || forms.filter((item) => (item.method || '').toLowerCase() === 'post')[0] || null;
      if (form) { diag.form = true; }
      const formData = () => {
        const data = form ? new FormData(form) : new FormData();
        if (!form) {
          pick('input[name], textarea[name], select[name]').forEach((el) => {
            if (!el.disabled && !data.has(el.name)) data.append(el.name, el.value || '');
          });
        }
        const submit = form && form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
        if (submit && submit.name && !submit.disabled && !data.has(submit.name)) {
          data.append(submit.name, submit.value || '');
        }
        if (token) { data.set('cf-turnstile-response', token); }
        return data;
      };
      const postFallback = async () => {
        const actionValue = form ? (form.getAttribute('action') || location.href) : location.href;
        const action = new URL(actionValue, location.href);
        if (action.origin !== location.origin) {
          diag.act = (diag.act ? diag.act + '+' : '') + 'post-blocked';
          save();
          return;
        }
        try {
          const response = await fetch(action.href, {
            method: 'POST', credentials: 'same-origin', body: formData()
          });
          window.__ptsignin_response = {status: response.status, url: response.url,
                                        text: await response.text()};
          diag.post = response.status;
        } catch (err) {
          diag.post = 'error';
        }
        save();
      };

      // 进页面就已经不在 pending 状态：站点可能已自行签完（例如本轮之前就签过），别重复提交
      if (pendingWords.length && !stillPending()) {
        diag.still = false;
        diag.act = 'already-done';
        diag.text = ((document.body && document.body.innerText) || '').slice(0, 160);
        save();
        return JSON.stringify(diag);
      }

      // 1) 站点回调：data-callback 优先，其次是闭包渲染时的常见全局名
      const callbackNodes = pick('[data-callback]');
      const names = [];
      callbackNodes.forEach((node) => {
        const name = (node.getAttribute('data-callback') || '').trim();
        if (name && names.indexOf(name) < 0) names.push(name);
      });
      diag.cb = names[0] || null;
      ['cfCallback', 'turnstileCallback', 'onTurnstileSuccess', 'onTurnstileVerify',
       'onVerify', 'verifyCallback', 'onCaptchaSuccess', 'captchaCallback'].forEach((name) => {
        if (names.indexOf(name) < 0) names.push(name);
      });
      diag.cands = names.slice(0, 12);
      if (!token) {
        diag.act = 'no-token';
      } else {
        let lastError = '';
        for (let i = 0; i < names.length; i += 1) {
          if (typeof window[names[i]] !== 'function') { continue; }
          try {
            window[names[i]](token);
            diag.called = names[i];
            diag.act = 'callback';
            break;
          } catch (err) {
            lastError = names[i];
          }
        }
        if (!diag.called && lastError) { diag.act = 'callback-error:' + lastError; }
        else if (!diag.called) { diag.act = callbackNodes.length ? 'callback-missing' : 'callback-absent'; }
      }
      save();
      if (diag.called) { await sleep(2500); }
      // 2) 页面还停在验证中（或站点没配 pending 文案）时，按表单真实 action 兜底 POST
      if (token && !diag.post && (stillPending() || !pendingWords.length)) {
        diag.act = (diag.act ? diag.act + '+' : '') + 'fallback';
        save();
        await postFallback();
        await sleep(1500);
      }
      diag.still = stillPending();
      diag.text = ((document.body && document.body.innerText) || '').slice(0, 160);
      save();
      return JSON.stringify(diag);
    })()""" % (json.dumps(token or ""), pending, json.dumps(TURNSTILE_TOKEN_SELECTOR), wait)


def build_query(rule: Mapping[str, Any], user_agent: Optional[str] = None,
                reuse_session: bool = True) -> str:
    """构造第一段签到的 BrowserQL mutation。

    Cloudflare / Turnstile 默认使用 ``solve:solve``（不限制类型）以支持自动检测，并把
    ``token`` 一起取回来：官方文档明确 ``solve`` 只负责生成 token，站点挂在 Turnstile 上的
    回调不会因此触发，必须由插件把 token 回填页面（见 ``build_inject_query``）。

    所以 ``submit_method=auto`` 的站点这一轮只做「打开签到页 + solve 拿 token + 保留会话」：
    ``reconnect`` 返回下一次请求要用的 ``browserQLEndpoint``，第二段请求打到该地址即可拿到
    同一个浏览器（cookie / cf_clearance / 页面状态都还在）。``reuse_session=False`` 时不申请
    会话复用，用于自建 / 旧版 Browserless 没有 ``reconnect`` 的情况。

    ``submit_method=turnstile_click`` 用于必须手动勾选复选框的页面：先用 BrowserQL 深度选择器
    等待 iframe 内 checkbox 可见，再用原生 click 点击；``solve_count=0`` 时不调用 solve。
    """
    image = rule["mode"] in {"image", "trigger_image"}
    triggered_image = rule["mode"] == "trigger_image"
    altcha = rule["mode"] == "altcha"
    submit_method = str(rule.get("submit_method") or "click")
    auto_submit = submit_method in AUTO_SUBMIT_METHODS
    # auto 提交的核验放在第二段（注入 token 之后）做：这一轮还没有提交动作可核验
    verifies_success = bool(rule.get("verify_success_path")) and not auto_submit
    needs_solver = solver_enabled(rule)
    second_solve = secondary_solve_enabled(rule)
    native_click = submit_method in {"click", "turnstile_click"}
    turnstile_click = submit_method == "turnstile_click"
    # auto 这一轮不点提交：只等页面把验证组件渲染出来，token 由 solve 取回后第二段再注入
    skip_submit = submit_method in {"none", "skip"} or auto_submit
    # 只有 auto 提交才需要 token 字段（它靠这个 token 回填页面、再触发站点提交）；其它站点
    # 不索取该字段，免得旧版 / 自建 Browserless 的 schema 里没有 CaptchaResponse.token 时
    # 整段查询被拒（GraphQL 只认写出来的字段）。
    token_field = " token" if auto_submit else ""
    solve = (
        ("solve:solveImageCaptcha(captchaSelector:$captchaSelector,"
         "inputSelector:$captchaInputSelector,timeout:$solveTimeout){found solved time"
         + token_field + "}")
        if image and needs_solver
        else ("solve:solve(timeout:$solveTimeout){found solved time" + token_field + "}")
        if rule["mode"] == "cloudflare" and needs_solver
        else "solve:evaluate(content:\"'skipped'\"){value}"
    )
    if second_solve:
        # 第一次 solve 可能解决 /attendance.php 外层的 Cloudflare 交互挑战；等待页面切换后，
        # 再对签到页内嵌的 Turnstile 执行一次 solve。没有这一步时，第一次 solve 返回时
        # 第二段验证尚未发生，第二段注入就只会拿到空的 cf-turnstile-response。
        solve += ("\n      waitBetweenSolves:waitForTimeout(time:$solve2Wait){time}"
                  "\n      solve2:solve(timeout:$solve2Timeout){found solved time"
                  + token_field + "}")
    extra = "$captchaSelector:String! $captchaInputSelector:String!" if image else ""
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    variable_suffix = (" $solveTimeout:Float!" if needs_solver else "")
    if second_solve:
        variable_suffix += " $solve2Wait:Float! $solve2Timeout:Float!"
    if native_click:
        variable_suffix += " $selector:String!"
        if turnstile_click:
            variable_suffix += " $turnstileWait:Float!"
    elif auto_submit and reuse_session:
        variable_suffix += " $reconnectTimeout:Float!"
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
    keep_session = ""
    if auto_submit and reuse_session:
        keep_session = "\n      session:reconnect(timeout:$reconnectTimeout){browserQLEndpoint}"
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
      after:html{html}%s
    }""" % (variable_suffix, set_user_agent,
            "trigger:evaluate(content:$trigger){value}\n      waitForTrigger:waitForTimeout(time:$triggerWait){time}" if triggered_image else "",
            "altcha:click(selector:$altchaSelector){selector time}" if altcha else "",
            solve,
            ("waitForTurnstile:waitForSelector(selector:$selector,timeout:$turnstileWait,visible:true){selector time}\n      "
             if turnstile_click else "")
            + ("submit:click(selector:$selector){selector time}" if native_click
               else "" if skip_submit else "submit:evaluate(content:$submit){value}"),
            verify,
            keep_session)


def build_inject_query(rule: Mapping[str, Any], user_agent: Optional[str] = None) -> str:
    """构造第二段 BrowserQL：把 solve 拿到的 Turnstile token 回填签到页并提交。

    第二段一定重新 ``goto`` 打开签到页：实测复用会话（``reconnect``）只保住浏览器与
    cookie / ``cf_clearance``，页面本身是新的，第一段 solve 过的那个页面并不会保留。提交与
    核验（``verify_success_path``）都在这一段完成，所以最终判定以这一段为准。``evaluate``
    显式带上 ``timeout``：默认 30 秒会砍掉脚本自己的等待。
    """
    set_user_agent = "userAgent(userAgent:$userAgent){time}" if user_agent else ""
    user_agent_variable = " $userAgent:String!" if user_agent else ""
    # 只声明真正用到的变量：BQL 是 GraphQL，多声明一个就会整段查询报错
    variables = ("$cookies:[CookieInput!]! $url:String! $beforeWait:Float! $wait:Float!"
                 " $autoSubmit:String! $autoTimeout:Float!")
    steps: List[str] = [
        "cookies(cookies:$cookies){cookies{name}}",
        "goto(url:$url,waitUntil:domContentLoaded){status}",
        "waitBefore:waitForTimeout(time:$beforeWait){time}",
        "before:html{html}",
        "submit:evaluate(content:$autoSubmit,timeout:$autoTimeout){value}",
    ]
    steps.append("waitAfter:waitForTimeout(time:$wait){time}")
    steps.append("autoDiag:evaluate(content:\"JSON.stringify(window.__ptsignin_auto "
                 "|| (function(){try{return JSON.parse(sessionStorage.getItem('__ptsignin_auto')"
                 "||'null')}catch(e){return null}})())\"){value}")
    steps.append("response:evaluate(content:\"JSON.stringify(window.__ptsignin_response "
                 "|| null)\"){value}")
    steps.append("after:html{html}")
    if rule.get("verify_success_path"):
        variables += " $verifyUrl:String!"
        if rule.get("verify_wait") is not None:
            variables += " $verifyWait:Float!"
        steps.append("verify:goto(url:$verifyUrl,waitUntil:domContentLoaded){status}")
        if rule.get("verify_wait") is not None:
            steps.append("waitForVerify:waitForTimeout(time:$verifyWait){time}")
        steps.append("verifyPage:html{html}")
    variables += user_agent_variable
    return """mutation CheckInSubmit(%s) {
      %s
      %s
    }""" % (variables, set_user_agent, "\n      ".join(steps))


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
    # 同 plain_text：解 HTML 实体，避免结果里带出 `&lt;` / `&amp;` 这类源码写法
    visible = _html_unescape(re.sub(r"<[^>]+>", " ", visible))
    return re.sub(r"\s+", " ", visible).strip()


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


def solve2_wait_ms(rule: Mapping[str, Any]) -> int:
    """两次 solve 之间的等待预算（1~10 秒，可用 solve2_wait 覆盖）。"""
    try:
        milli = int(float(rule.get("solve2_wait") or DEFAULT_SOLVE2_WAIT))
    except (TypeError, ValueError):
        milli = DEFAULT_SOLVE2_WAIT
    return max(1000, min(milli, 10000))


def turnstile_wait_ms(rule: Mapping[str, Any]) -> int:
    """等待 Turnstile iframe 内复选框出现的毫秒数（5~60 秒）。"""
    try:
        milli = int(float(rule.get("turnstile_wait") or DEFAULT_TURNSTILE_WAIT))
    except (TypeError, ValueError):
        milli = DEFAULT_TURNSTILE_WAIT
    return max(5000, min(milli, 60000))


def solve2_timeout_ms(rule: Mapping[str, Any]) -> int:
    """第二段 solve（签到页内嵌 Turnstile）的毫秒预算（5~120 秒）。

    第一段 solve 针对进站时的 Cloudflare 交互挑战，第二段针对签到页自己的 Turnstile；
    两者难度不同，可分别用 ``solve_timeout`` / ``solve2_timeout`` 覆盖，缺省继承前者。
    """
    value = rule.get("solve2_timeout")
    if value is None:
        value = rule.get("solve_timeout")
    try:
        milli = int(float(value if value is not None else 60000))
    except (TypeError, ValueError):
        milli = 60000
    return max(5000, min(milli, 120000))


def submit_timeout_ms(rule: Mapping[str, Any]) -> int:
    """第二段「注入 token 并提交」的 evaluate 步骤超时（毫秒）。

    BQL 的 ``evaluate`` 步骤默认 30 秒超时（可用 preferences 改），而脚本自身的等待预算就是
    ``auto_wait``：两者相等时脚本会在等待循环里被步骤超时掐死，窗口里只剩脚本启动瞬间写下的
    初始快照——历史日志里那句「未拿到 Turnstile token / 页面没有 data-callback 回调」就是这么
    来的假结论。这里固定比脚本预算多留 ``INJECT_STEP_MARGIN_MS``。
    """
    return max(15000, min(auto_wait_ms(rule) + INJECT_STEP_MARGIN_MS, 120000))


def reconnect_wait_ms(rule: Mapping[str, Any]) -> int:
    """BrowserQL 会话复用的空闲保留毫秒数（10~300 秒，可用 reconnect_wait 覆盖）。

    solve 生成的 token 只能在同一个浏览器会话里注入，所以第一段结束后要把会话留住，
    第二段才来得及用 ``browserQLEndpoint`` 接回同一个浏览器。
    """
    try:
        milli = int(float(rule.get("reconnect_wait") or DEFAULT_RECONNECT_WAIT))
    except (TypeError, ValueError):
        milli = DEFAULT_RECONNECT_WAIT
    return max(10000, min(milli, 300000))


def navigation_wait_ms(rule: Mapping[str, Any]) -> int:
    """打开页面后留给 Cloudflare 交互挑战自行放行的毫秒数（非 cloudflare 模式只等 2 秒）。"""
    return cloudflare_wait_ms(rule) if str(rule.get("mode")) == "cloudflare" else 2000


def bql_wait_budget_ms(rule: Mapping[str, Any]) -> int:
    """估算一次签到的 BQL 页面内等待总时长（毫秒）。

    BrowserQL 是「整段执行完才返回响应」的整包请求：如果 HTTP 读超时比页面内等待预算还短，
    插件会自己先掐断连接，报出 The read operation timed out 这种自伤式失败。``auto`` 提交的
    站点会跑两段请求（第一段 solve 拿 token、第二段注入提交），这里按两段之和上估，宁大勿小。
    """
    total = 0
    if str(rule.get("mode")) == "cloudflare":
        total += cloudflare_wait_ms(rule)
    if str(rule.get("submit_method") or "").lower() == "turnstile_click":
        total += turnstile_wait_ms(rule)
    if str(rule.get("mode")) == "trigger_image":
        try:
            total += max(0, int(rule.get("trigger_wait") or 500))
        except (TypeError, ValueError):
            total += 500
    if solver_enabled(rule):
        total += solve_timeout_ms(rule)
    if secondary_solve_enabled(rule):
        total += solve2_wait_ms(rule)
        total += solve2_timeout_ms(rule)
    try:
        total += max(0, int(rule.get("wait") or 3500))
    except (TypeError, ValueError):
        total += 3500
    if str(rule.get("submit_method") or "").lower() in AUTO_SUBMIT_METHODS:
        # 第二段：导航等待 + 注入脚本预算（含 evaluate 余量）+ 提交后的等待
        total += navigation_wait_ms(rule)
        total += submit_timeout_ms(rule)
        try:
            total += max(0, int(rule.get("wait") or 3500))
        except (TypeError, ValueError):
            total += 3500
    if rule.get("verify_success_path"):
        try:
            total += max(1000, min(int(rule.get("verify_wait") or 8000), 20000))
        except (TypeError, ValueError):
            total += 8000
    return total



def solver_enabled(rule: Mapping[str, Any]) -> bool:
    """Browserless cloudflare / image 模式是否启用 solve。

    solve_count 缺省为启用；``solve_count=0`` 可显式关闭（用于纯等待页面自行放行的站点）。
    观众 Audiences 的 Turnstile 在 closed shadow DOM 里，CSS / 深度选择器都命不中，
    必须交给官方 solve（它内置支持 iframe / shadow DOM 内的验证码）。
    """
    if str(rule.get("mode") or "") not in {"image", "trigger_image", "cloudflare"}:
        return False
    if rule.get("solve_count") is None:
        return True
    try:
        return int(float(rule.get("solve_count"))) > 0
    except (TypeError, ValueError):
        return True


def secondary_solve_enabled(rule: Mapping[str, Any]) -> bool:
    """Cloudflare 模式是否需要对进入真实页面后的第二段 Turnstile 再 solve 一次。"""
    if str(rule.get("mode") or "") != "cloudflare" or not solver_enabled(rule):
        return False
    try:
        return int(float(rule.get("solve_count") or 1)) >= 2
    except (TypeError, ValueError):
        return False


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


def step_error_hint(errors: Any) -> str:
    """把 BrowserQL 顶层步骤的失败信息翻译成一句诊断。

    官方文档明确：一个步骤失败（含 waitForSelector / solve 超时）不会中止整个 mutation，
    失败步骤在 data 里返回 null，其余步骤照常执行。因此这些错误只是诊断信息——
    solve 超时并不代表页面没有签到成功，不能把它当成整轮失败。
    """
    if not isinstance(errors, (list, tuple)):
        return ""
    parts: List[str] = []
    for item in errors:
        if not isinstance(item, Mapping):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        path = str(item.get("path") or "").strip()
        parts.append(f"{path}: {message}" if path else message)
        if len(parts) >= 2:
            break
    return "Browserless 步骤：" + "；".join(parts) if parts else ""


def solver_hint(first: Any, second: Any = None) -> str:
    """把 Browserless 两次 solve 的返回结果翻译成简短诊断。"""
    parts: List[str] = []
    for label, value in (("首次", first), ("二次", second)):
        if not isinstance(value, Mapping):
            continue
        details: List[str] = []
        if "found" in value:
            details.append("found=" + str(bool(value.get("found"))).lower())
        if "solved" in value:
            details.append("solved=" + str(bool(value.get("solved"))).lower())
        if value.get("time") is not None:
            details.append("time=" + str(value.get("time")))
        if details:
            parts.append(label + " solve " + "/".join(details))
    return "解题诊断：" + "；".join(parts) if parts else ""


def solve_token(*results: Any) -> str:
    """取出 solve / solve2 响应里的 Turnstile token。

    官方 ``CaptchaResponse`` 里本来就有 ``token`` 字段：``solve`` 只负责生成 token，不会触发
    站点挂在 Turnstile 上的提交逻辑。所以这个 token 必须由插件带回页面注入（见
    ``build_inject_query``）；只取 found/solved/time 等于把唯一能用的东西扔了。
    """
    for item in results:
        if isinstance(item, Mapping):
            value = str(item.get("token") or "").strip()
            if value:
                return value
    return ""


def auto_submit_hint(value: Any) -> str:
    """把页面里 ``window.__ptsignin_auto`` 的现场诊断翻译成一句中文提示。

    用于「页面仍停在人机验证」这类失败：token 到底有没有拿到、从哪儿拿到的、站点回调有没有
    触发、表单有没有提交、兜底 POST 返回什么、页面是不是真的还在等人机验证，直接写在失败原因
    里，省得来回猜。
    """
    if not value or value == "null":
        return ""
    try:
        state = json.loads(str(value))
    except (TypeError, ValueError):
        return ""
    if not isinstance(state, Mapping):
        return ""
    source_label = {"solve": "来自 solve 返回的 token", "input": "来自页面输入框",
                    "turnstile": "来自 turnstile.getResponse()",
                    "late": "页面后来自行产出"}.get(str(state.get("src") or ""), "")
    length = state.get("len")
    if state.get("token"):
        detail = f"（{source_label}，长度 {length}）" if source_label and length else ""
        parts = [f"已拿到 Turnstile token{detail}"]
    else:
        parts = ["未拿到 Turnstile token"]
    name = str(state.get("cb") or "").strip()
    called = str(state.get("called") or "").strip()
    action = str(state.get("act") or "")
    if called:
        parts.append(f"已调用站点回调 {called}")
    elif name:
        if action.startswith("callback-error"):
            failed = action.split(":", 1)[-1] or name
            parts.append(f"站点回调 {failed} 执行失败")
        elif action.startswith("callback-missing"):
            parts.append(f"页面声明的站点回调 {name} 不存在")
        elif action.startswith("callback"):
            parts.append(f"已调用站点回调 {name}")
        else:
            parts.append(f"站点回调 {name} 未调用（{action or '原因未知'}）")
    else:
        candidates = [str(item) for item in (state.get("cands") or []) if str(item).strip()]
        parts.append("页面没有 data-callback 回调"
                     + (f"（{len(candidates)} 个常见回调名都不存在）" if candidates else ""))
        if action and action != "callback-absent":
            parts.append(f"注入结果：{action}")
    if state.get("form"):
        parts.append("已提交页面表单")
    post = state.get("post")
    if isinstance(post, int) and not isinstance(post, bool):
        parts.append(f"兜底 POST 返回 HTTP {post}")
    elif post:
        parts.append("兜底 POST 失败")
    if state.get("still") is True:
        parts.append("页面仍停在人机验证")
    scene: List[str] = []
    inputs = state.get("inputs")
    url = str(state.get("url") or "").strip()
    if state.get("widget") is False and inputs == 0:
        scene.append("页面没有 Turnstile widget / 响应输入框"
                     + (f"（注入时页面 {url[:70]}）" if url else ""))
    elif inputs == 0:
        scene.append("widget 在 closed shadow DOM 内，页面 JS 取不到响应输入框")
    if state.get("turnstile") is False:
        scene.append("window.turnstile 不可用")
    if scene:
        parts.append("现场：" + "、".join(scene))
    text = str(state.get("text") or "").strip()
    if text and state.get("still"):
        parts.append("页面文字「" + text[:60] + "」")
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


def _browserless_error_message(operation: str, exc: BaseException, elapsed: float,
                               timeout: int) -> str:
    """把 Browserless 的网络层异常翻译成可执行的排查建议。"""
    step = "签到主流程" if operation == "CheckIn" else "签到页预检"
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return (f"Browserless 请求超时（{step} {timeout} 秒内未收到响应，已等待 {elapsed:.0f} 秒）："
                "常见原因是本机 / 服务器到 Browserless 的链路不稳定，或该节点执行时间过长；"
                "请检查网络 / 代理，或调大「Browserless 超时」、改用更近的区域地址"
                "（如 production-ams.browserless.io）后重试")
    return f"Browserless 请求失败（{step}，已等待 {elapsed:.0f} 秒）：{exc}"


class BrowserlessSigner:
    """通过 Browserless BrowserQL 执行一次签到。"""

    def __init__(self, address: str, token: str, timeout: int = 100):
        self.endpoint = bql_endpoint(address, token)
        # reconnect 返回的会话地址不带 token，第二段请求要靠它复用同一个浏览器
        self.token = str(token or "").strip()
        self.timeout = max(15, min(int(timeout), 300))

    def request_timeout(self, rule: Mapping[str, Any]) -> int:
        """HTTP 读超时：至少覆盖一次 BQL 的页面内等待预算 + 45 秒余量。

        BrowserQL 是「整段执行完才返回响应」的整包请求。如果读超时比页面内等待预算还短，
        插件会自己先掐断连接，报出 The read operation timed out 这种自伤式失败；这里保证
        读超时永远不短于内部预算 + 导航 / 浏览器启动余量。
        """
        needed = (bql_wait_budget_ms(rule) + 45000 + 999) // 1000
        return max(15, min(max(self.timeout, needed), 300))

    def _recheck_after_transient(self, base_url: str, cookies: List[Dict[str, Any]],
                                 user_agent: str,
                                 rule: Mapping[str, Any]) -> Optional[SignResult]:
        """主流程请求在网络层失败（响应丢失）时补一次只读回查。

        签到可能已经在 Browserless 的浏览器里完成、只是响应没回到插件；直接报请求超时会把
        「已经签到」误判成失败。这里重新打开签到页，只有确凿看到成功 / 已签到才改判。
        """
        path = str(rule.get("verify_success_path") or rule["path"])
        variables: Dict[str, Any] = {"cookies": cookies, "url": target_url(base_url, path)}
        if user_agent:
            variables["userAgent"] = user_agent
        data = self._request(variables, "CheckInPreflight",
                             build_preflight_query(user_agent, 8000),
                             timeout=self.request_timeout(rule), retry_transient=True)
        if isinstance(data, SignResult):
            return None
        html = str((data.get("before") or {}).get("html") or "")
        if not html or cloudflare_pending(html) or login_page(html):
            return None
        result = classify(html, rule)
        if result.status in {"success", "already"}:
            return SignResult("success", _bonus_message(
                "主流程响应丢失，但重新打开签到页确认签到成功", page_text(html), rule))
        return None

    def _inject_submit(self, base_url: str, cookies: List[Dict[str, Any]], user_agent: str,
                       rule: Mapping[str, Any], token: str,
                       first: Mapping[str, Any]) -> Any:
        """第二段：把 solve 拿到的 token 回填签到页并触发站点提交。

        返回 ``dict``（第二段的 BrowserQL 数据，交给调用方判定）、``SignResult``（已经能确定的
        结论，例如回查确认已签到、Browserless 鉴权 / 额度错误）或 ``None``（拿不到可用结果，
        调用方回落到第一段数据判定）。
        """
        session = first.get("session") if isinstance(first, Mapping) else None
        endpoint = session_endpoint(str((session or {}).get("browserQLEndpoint") or ""), self.token)
        variables: Dict[str, Any] = {
            "cookies": cookies,
            "url": target_url(base_url, str(rule["path"])),
            # 实测：复用会话只保住浏览器与 cookie / cf_clearance，页面是新的（第一段 solve 过的
            # 那个页面并不保留），所以第二段必须重新导航到签到页再注入。
            "beforeWait": navigation_wait_ms(rule),
            "wait": int(rule.get("wait") or 3500),
            "autoSubmit": _turnstile_inject_script(rule, token),
            "autoTimeout": submit_timeout_ms(rule),
        }
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
        data = self._request(variables, "CheckInSubmit",
                             build_inject_query(rule, user_agent),
                             timeout=self.request_timeout(rule), endpoint=endpoint or None)
        if isinstance(data, SignResult):
            if not data.transient:
                return data
            # 响应丢失时结果未知：补一次只读回查，别把已签到报成网络失败
            rechecked = self._recheck_after_transient(base_url, cookies, user_agent, rule)
            if rechecked is not None:
                return rechecked
            return SignResult(
                "failed",
                f"{data.message}；token 注入阶段的响应丢失，已重新打开签到页核对，"
                "未能确认为已签到")
        return data

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
                                  build_preflight_query(user_agent, preflight_wait),
                                  timeout=self.request_timeout(rule), retry_transient=True)
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
        if submit_method in {"click", "turnstile_click"}:
            variables["selector"] = rule["submit_selector"]
        elif submit_method in AUTO_SUBMIT_METHODS:
            # 第一段只负责 solve 拿 token（token 由插件带回，第二段注入提交，见 _inject_submit）；
            # 这里只把会话多留一会儿，保证第二段还能接回同一个浏览器。
            variables["reconnectTimeout"] = reconnect_wait_ms(rule)
        elif submit_method in {"none", "skip"}:
            # 页面在人机验证通过后自己提交签到，不需要点任何按钮
            pass
        else:
            variables["submit"] = (_altcha_submit_script(rule) if submit_method == "altcha_click"
                                   else _submit_script(rule["submit_selector"], submit_method,
                                                       rule.get("captcha_input_selector"),
                                                       rule.get("submit_text")))
        if submit_method == "turnstile_click":
            variables["turnstileWait"] = turnstile_wait_ms(rule)
        if submit_method == "altcha_click":
            variables["altchaSelector"] = rule["altcha_checkbox_selector"]
        if rule["mode"] == "trigger_image":
            variables["trigger"] = _trigger_script(str(rule["trigger_selector"]))
            variables["triggerWait"] = int(rule.get("trigger_wait") or 500)
        if solver_enabled(rule):
            variables["solveTimeout"] = solve_timeout_ms(rule)
        if secondary_solve_enabled(rule):
            variables["solve2Wait"] = solve2_wait_ms(rule)
            variables["solve2Timeout"] = solve2_timeout_ms(rule)
        if rule["mode"] in {"image", "trigger_image"}:
            variables.update({"captchaSelector": rule["captcha_selector"],
                              "captchaInputSelector": rule["captcha_input_selector"]})
        if rule.get("verify_success_path") and submit_method not in AUTO_SUBMIT_METHODS:
            # auto 提交的核验放在第二段（注入 token 之后）做
            variables["verifyUrl"] = target_url(base_url, str(rule["verify_success_path"]))
            if rule.get("verify_wait") is not None:
                try:
                    verify_wait = int(rule.get("verify_wait"))
                except (TypeError, ValueError):
                    verify_wait = 8000
                variables["verifyWait"] = max(1000, min(verify_wait, 20000))
        if user_agent:
            variables["userAgent"] = user_agent

        data = self._request(variables, "CheckIn", build_query(rule, user_agent),
                             timeout=self.request_timeout(rule))
        if (isinstance(data, SignResult) and submit_method in AUTO_SUBMIT_METHODS
                and not data.transient and "reconnect" in data.message.lower()):
            # 自建 / 旧版 Browserless 可能没有 reconnect：去掉会话复用再试一次
            variables.pop("reconnectTimeout", None)
            data = self._request(variables, "CheckIn",
                                 build_query(rule, user_agent, reuse_session=False),
                                 timeout=self.request_timeout(rule))
        if isinstance(data, SignResult):
            if data.transient:
                # 超时 / 连接中断时结果未知：补一次只读回查，别把已签到报成网络失败
                rechecked = self._recheck_after_transient(base_url, cookies, user_agent, rule)
                if rechecked is not None:
                    return rechecked
                return SignResult(
                    "failed",
                    f"{data.message}；已重新打开签到页核对，未能确认为已签到，"
                    "请稍后重试或改用更近的 Browserless 节点")
            return data
        solve = data.get("solve") or {}
        solve2 = data.get("solve2") or {}
        inject_note = ""
        if submit_method in AUTO_SUBMIT_METHODS:
            # 第一段页面就已经是成功 / 已签到：站点自己签完了，别再注入一次
            first_after = str((data.get("after") or {}).get("html") or "")
            settled = classify(first_after, rule)
            if settled.status == "success":
                return settled
            if settled.status == "already":
                # 预检时还停在人机验证、solve 之后变成「今日已签到」：本轮刚签上，按成功上报
                if pre_state_pending:
                    return SignResult("success",
                                      _bonus_message("签到成功", page_text(first_after), rule))
                return settled
            first_errors = list(data.get("_errors") or [])
            outcome = self._inject_submit(base_url, cookies, user_agent, rule,
                                          solve_token(solve, solve2), data)
            if isinstance(outcome, SignResult):
                return outcome
            if isinstance(outcome, dict):
                data = outcome
                data["_errors"] = first_errors + list(outcome.get("_errors") or [])
            else:
                inject_note = "token 注入阶段未拿到可用响应"
        after_html = str((data.get("after") or {}).get("html") or "")
        # data 可能已经换成第二段的结果：solve 诊断仍以第一段为准（第二段没有 solve 步骤）
        solve = data.get("solve") or solve
        solve2 = data.get("solve2") or solve2
        # auto 提交的现场诊断：token 有没有生成、回调有没有触发、表单有没有提交；
        # 再加上 BrowserQL 各步骤自己的报错（如 solve 超时），它只是诊断、不代表整轮失败。
        auto_hint = auto_submit_hint((data.get("autoDiag") or {}).get("value"))
        solve_hint = solver_hint(solve, solve2)
        step_hint = step_error_hint(data.get("_errors"))
        auto_hint = "；".join(part for part in (solve_hint, step_hint, inject_note, auto_hint)
                              if part)
        # 已经等满预算却仍卡在挑战页：给出可操作的提示，而不是笼统的「打开签到页失败」
        if mode == "cloudflare" and cloudflare_pending(after_html):
            return SignResult("failed", append_hint(
                f"Cloudflare 挑战在 {page_wait // 1000} 秒内未自动放行；"
                "可在该站规则里调大 cloudflare_wait 后重试", auto_hint))
        goto = data.get("goto")
        if not isinstance(goto, Mapping) or not navigation_ok(mode, goto.get("status"), after_html):
            return SignResult("failed", append_hint("打开签到页失败", auto_hint))
        if rule["mode"] in {"image", "trigger_image"} and not solve.get("found"):
            return SignResult("failed", "未找到图片验证码；请检查验证码图片选择器")
        # Cloudflare / Turnstile 存在「solve 没解成功、但页面自己跑完 JS 也放行了」的情况
        # （观众 Audiences 就是等一会儿自动过），所以先按页面实际内容判定，最后才回落到
        # 「找到验证码但未能完成验证」。
        solve_failed = (rule["mode"] != "open_page" and any(
            item.get("found") and not item.get("solved") for item in (solve, solve2)))
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

    def _request(self, variables: Mapping[str, Any], operation: str, query: str,
                 timeout: Optional[int] = None, retry_transient: bool = False,
                 endpoint: Optional[str] = None) -> Any:
        payload = json.dumps({"query": query, "operationName": operation,
                              "variables": variables}).encode()
        # endpoint 用于会话复用：打到 reconnect 返回的 browserQLEndpoint 就是同一个浏览器
        url = str(endpoint or self.endpoint)
        # 读超时按调用方给的预算走，缺省才用构造时的配置；上限仍是 300 秒
        read_timeout = max(15, min(int(timeout or self.timeout), 300))
        # 只读的预检请求允许在网络抖动时重试一次；主流程（会真的点提交）不在这里重试，
        # 交给 _recheck_after_transient 用只读回查确认，避免重复提交。
        attempts = 2 if retry_transient else 1
        value: Any = None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=payload,
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=read_timeout) as response:
                    value = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    return SignResult("failed", f"Browserless 鉴权失败（HTTP {exc.code}），请检查 Token")
                if exc.code == 429:
                    return SignResult("failed", "Browserless 额度不足或请求过于频繁（HTTP 429）")
                if exc.code >= 500 and attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return SignResult("failed", f"Browserless HTTP {exc.code}（步骤 {operation}）")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                elapsed = time.monotonic() - started
                if attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return SignResult(
                    "failed",
                    _browserless_error_message(operation, exc, elapsed, read_timeout),
                    # JSON 解析失败不是网络问题，其余（超时 / 连接中断）结果未知
                    transient=not isinstance(exc, ValueError))
        errors = value.get("errors") or []
        data = value.get("data")
        if not isinstance(data, Mapping):
            data = {}
        if errors and not data:
            # 只有整个查询在执行前就被拒绝（字段/参数非法）才会没有 data，这时必须报错。
            # 单个步骤失败时 BQL 仍会返回其它步骤的 data：直接判整轮失败会误伤
            # 「solve 超时，但页面其实已经签到成功」这类场景，所以下面把错误交给上层做诊断。
            first = errors[0] if isinstance(errors[0], Mapping) else {}
            message = str(first.get("message") or "未知错误")
            error_path = ".".join(str(part) for part in (first.get("path") or []))
            if error_path:
                message += f"（步骤：{error_path}）"
            lower = message.lower()
            if "waiting for page navigation" in lower:
                return SignResult("failed", "Browserless 页面导航超时；请检查该站是否可由当前节点访问")
            if ("waitforselector" in lower or "waiting for selector" in lower
                    or "timeout" in lower and "selector" in lower):
                return SignResult("failed", "未等到 Cloudflare Turnstile 复选框；"
                                   "请检查 cloudflare_wait / turnstile_wait 或站点页面结构")
            if "not clickable" in lower or "not an htmlelement" in lower:
                return SignResult("failed", "找到 Turnstile 复选框但无法原生点击；"
                                   "当前 Browserless 节点可能被 Cloudflare 拦截")
            return SignResult("failed", "Browserless 执行失败：" + message)
        result = dict(data)
        result["_errors"] = [
            {"path": ".".join(str(part) for part in (item.get("path") or [])),
             "message": str(item.get("message") or "未知错误")}
            for item in errors if isinstance(item, Mapping)
        ]
        return result
