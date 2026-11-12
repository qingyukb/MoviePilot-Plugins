"""PT 签到核心逻辑：HTTP 引擎 + Browserless(BrowserQL) 引擎 + 站点登录 / 最近动向。

本模块不含任何 MoviePilot 依赖，方便单独测试与复用。
Browserless 相关实现参考 tmzg0000/moviepilot-plugins 的 captchasignin 插件
（AGPL/MIT 见原仓库），在其基础上精简并与 HTTP 引擎合并为双引擎结构。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import re
from pathlib import Path
import time
import urllib.parse
from dataclasses import dataclass
from html import unescape as _html_unescape
from threading import BoundedSemaphore, Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence
# 内置规则与猪猪专用脚本拆成独立模块；按文件路径加载 core.py 时（测试 / 工具）
# 没有包上下文，因此提供同目录动态加载兜底。
def _load_sibling_module(name: str) -> Any:
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"{__name__}_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件模块：{path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __package__:
    from .http import new_http_session
    from .default_rules import DEFAULT_RULES
    from .piggo import (
        _piggo_click_script,
        _piggo_inject_script,
        _piggo_login_script,
        _piggo_state_script,
        _piggo_step_hint,
        _piggo_unlock_script,
        _piggo_value,
        build_piggo_prepare_query,
        build_piggo_solve_query,
        build_piggo_submit_query,
    )
else:
    _http_module = _load_sibling_module("http")
    new_http_session = _http_module.new_http_session
    _default_rules_module = _load_sibling_module("default_rules")
    _piggo_module = _load_sibling_module("piggo")
    DEFAULT_RULES = _default_rules_module.DEFAULT_RULES
    _piggo_value = _piggo_module._piggo_value
    _piggo_state_script = _piggo_module._piggo_state_script
    _piggo_login_script = _piggo_module._piggo_login_script
    _piggo_unlock_script = _piggo_module._piggo_unlock_script
    _piggo_click_script = _piggo_module._piggo_click_script
    _piggo_inject_script = _piggo_module._piggo_inject_script
    build_piggo_prepare_query = _piggo_module.build_piggo_prepare_query
    build_piggo_solve_query = _piggo_module.build_piggo_solve_query
    build_piggo_submit_query = _piggo_module.build_piggo_submit_query
    _piggo_step_hint = _piggo_module._piggo_step_hint



DEFAULT_HTTP_PATH = "/attendance.php"
DEFAULT_SUCCESS_PATTERN = (r"(state['\"]?\s*[:=]\s*['\"]?success|签到成功|簽到成功|签到 成功|"
                          r"成功签到|签到完成|簽到完成|领取成功|領取成功|签到已成功)")
# 注意：不要在这里放「已连续签到」——TTG 用「您已连续签到 N 天」表示**签到成功**，
# 憨憨那类站点需要它时在自己的 already_keywords 里单独声明。
DEFAULT_ALREADY_PATTERN = (r"(今日已签|今天已签|已经签到|已签到过|已签到|已簽到|"
                           # NexusPHP 常见写法：鲸币 [ 使用 ]: 131,943.3 (签到已得64)
                           r"签到已得|已领取过|不要重复签到|无需重复签到|already|signed in today)")
COOKIE_INVALID_PATTERN = r"(用户登录|會員登入|请先登录|请登录|login\.php)"
# Cloudflare 挑战页特征（出现即说明请求未穿透盾，需改用 Browserless）
CLOUDFLARE_PATTERN = r"(just a moment|cf-chl-widget|cf_chl_opt|challenge-platform)"
# 雷池（SafeLine）WAF 的安全检测页：hdkyl.in、piggo.me 这类站点点签到后会先跳这里，
# 由页面自己的 JS 跑几秒后自动放行，行为和 Cloudflare 交互挑战一样，只是文案不同。
WAF_PATTERN = r"(正在进行安全检测|安全检测能力由|雷池\s*WAF|SafeLine)"
# 站点要求「二级验证 / 异地登录验证」：猪猪（piggo.me）在非常用网络或新浏览器上会先出这一页，
# 必须输入 2FA 动态码才能继续——插件没法自动完成，要明确提示而不是报「找不到签到按钮」。
SECOND_FACTOR_PATTERN = r"(异地登录安全验证|異地登錄安全驗證|二级验证|二級驗證|两步验证|兩步驗證|忘记二级验证|忘記二級驗證|2FA)"
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
LOGIN_PAGE_PATTERN = (r"(takelogin\.php|login\.php\?returnto|"
                      r"auth/sign-in|auth/login|shouldClearAuth|"
                      r"/login\?redirect|请先登录|請先登入|session expired)")
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
# 签到奖励：单位 / 数量 / 动词抽成常量共用，新增币种只改 BONUS_UNITS 一处
BONUS_UNITS = "魔力值|魔力|爆米花|积分|做种积分|金币|憨豆|金贝|鲸币|奖励"
BONUS_AMOUNT = r"[\d,]+(?:\.\d+)?"
BONUS_COUNTERS = "个|粒|枚|点|個|顆"
BONUS_VERBS = "获得|獲得|獲取"
BONUS_PATTERNS = (
    rf"((?:{BONUS_VERBS})\s*{BONUS_AMOUNT}\s*(?:{BONUS_COUNTERS})?\s*(?:{BONUS_UNITS}))",
    rf"({BONUS_AMOUNT}\s*(?:{BONUS_COUNTERS})\s*(?:{BONUS_UNITS}))",
    rf"((?:{BONUS_UNITS})\s*[:：]?\s*\+?\s*{BONUS_AMOUNT})",
    rf"\+({BONUS_AMOUNT})\s*(?:{BONUS_UNITS})",
    rf"(?:签到已得|簽到已得)\s*({BONUS_AMOUNT})",
)
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SUCCESS_WORDS = ("签到成功", "簽到成功", "成功签到", "打卡成功")
ALREADY_WORDS = ("已签到", "已经签到", "今日已签", "今天已签", "已打卡", "already")
LOGIN_WORDS = ("用户登录", "會員登入", "login")

# Browserless 官方区域端点。实测（本机直连、每端点 6 次取中位，2026-10-17）：
#   sfo 美西  TCP+TLS ~400ms / API ~720ms   ← 最快
#   lon 伦敦  TCP+TLS ~520ms / API ~1160ms
#   ams 阿姆斯特丹 TCP+TLS ~505ms / API ~1230ms
# 所以默认用 sfo；换区域直接在设置里改地址即可（fra 等端点并不存在）。
DEFAULT_BROWSERLESS_URL = "https://production-sfo.browserless.io"

# ---------------------------------------------------------------- 内置站点规则
# mode 仅对 Browserless 引擎有效：
#   http          直接 HTTP 请求签到接口（默认引擎，轻量、不消耗 Browserless 额度）
#   open_page     打开页面并点击签到按钮（无需验证码）
#   cloudflare    打开页面并自动通过 Cloudflare / Turnstile 挑战
#   image         图片验证码签到
#   trigger_image 先点开弹窗再处理图片验证码（HDSky 类）
#   altcha        需先完成 Altcha 人机验证再点击提交（YemaPT 类）


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


# --------------------------------------------------------------- API Key 规则
API_KEY_AUTH = "api_key"
_API_KEY_SOURCES = {"auto", "apikey", "token"}
_API_KEY_ENDPOINT_KEYS = {"path", "credential_check_path"}
_API_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_API_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class ApiRequestError(RuntimeError):
    """API 请求的安全错误；消息里不会包含凭证。"""


def is_api_key_rule(rule: Mapping[str, Any]) -> bool:
    return str(rule.get("auth") or "").strip().lower() == API_KEY_AUTH


def is_api_key_site(site: Mapping[str, Any]) -> bool:
    """站点是否为纯 API 凭证：没有 Cookie，但配置了 ApiKey 或 Token。"""
    cookie = str(site.get("cookie") or "").strip()
    credential = (str(site.get("apikey") or "").strip()
                  or str(site.get("token") or "").strip())
    return bool(credential and not cookie)


def _validate_site_path(value: Any, label: str) -> str:
    path = str(value or "").strip()
    parsed = urllib.parse.urlsplit(path)
    if (not path or not path.startswith("/") or path.startswith("//")
            or "\\" in path or "#" in path or parsed.scheme or parsed.netloc):
        raise ValueError(f"{label}必须是以 / 开头的站内路径，不能是绝对 URL、//、反斜杠或 # 片段")
    if any(char in path for char in ("\r", "\n")):
        raise ValueError(f"{label}不能包含换行")
    return path


def _api_key_config(rule: Mapping[str, Any]) -> Dict[str, str]:
    source = str(rule.get("api_key_source") or "auto").strip().lower()
    if source not in _API_KEY_SOURCES:
        raise ValueError("api_key_source 只能是 auto、apikey 或 token")
    header = str(rule.get("api_key_header") or "Authorization").strip()
    if not _API_HEADER_NAME_RE.fullmatch(header):
        raise ValueError("api_key_header 必须是合法的 HTTP Header 名")
    if "api_key_prefix" in rule:
        prefix = str(rule.get("api_key_prefix") or "")
    else:
        prefix = "" if header.lower() == "x-api-key" else "Bearer "
    if "\r" in prefix or "\n" in prefix:
        raise ValueError("api_key_prefix 不能包含换行")
    return {"source": source, "header": header, "prefix": prefix}


def _api_key_value(site: Mapping[str, Any], rule: Mapping[str, Any]) -> str:
    config = _api_key_config(rule)
    sources = ("apikey", "token") if config["source"] == "auto" else (config["source"],)
    for source in sources:
        value = str(site.get(source) or "").strip()
        if value:
            return value
    return ""


def _api_headers(site: Mapping[str, Any], rule: Mapping[str, Any]) -> Dict[str, str]:
    config = _api_key_config(rule)
    key = _api_key_value(site, rule)
    if not key:
        label = "ApiKey 或 Token" if config["source"] == "auto" else config["source"]
        raise ValueError(f"站点规则使用 API Key，但站点管理未配置 {label}")
    if "\r" in key or "\n" in key:
        raise ValueError("API Key 不能包含换行")
    headers = {
        "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, text/html, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    headers[config["header"]] = config["prefix"] + key
    return headers


def _redact_api_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    key = str(secret or "")
    if key:
        text = text.replace(key, "***")
    return text


def _same_origin(left: str, right: str) -> bool:
    first = urllib.parse.urlsplit(left)
    second = urllib.parse.urlsplit(right)
    def normalized(parts: Any) -> tuple:
        scheme = str(parts.scheme or "").lower()
        port = parts.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return scheme, str(parts.hostname or "").lower(), port
    return normalized(first) == normalized(second)


def _api_request(session: Any, method: str, url: str, headers: Mapping[str, str],
                 timeout: int, proxies: Optional[Dict[str, str]],
                 data: Optional[Mapping[str, Any]] = None,
                 json_body: Optional[Mapping[str, Any]] = None,
                 params: Optional[Mapping[str, Any]] = None) -> Any:
    """发送 API 请求；关闭自动重定向，仅手动跟随同源重定向。"""
    current_url = url
    current_method = method.upper()
    current_data = data
    current_json = json_body
    current_params = params
    redirects = 0
    while True:
        response = session.request(
            current_method, current_url, headers=dict(headers), timeout=timeout,
            allow_redirects=False, proxies=proxies, data=current_data,
            json=current_json, params=current_params)
        status = int(getattr(response, "status_code", 0) or 0)
        location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
        if status not in _API_REDIRECT_STATUSES or not location:
            return response
        if redirects >= 3:
            raise ApiRequestError("API 请求重定向超过 3 次，已停止")
        next_url = urllib.parse.urljoin(current_url, location)
        if not _same_origin(current_url, next_url):
            raise ApiRequestError("API 请求被重定向到站外，已停止且未携带 API Key 继续访问")
        redirects += 1
        if status in {301, 302, 303} and current_method != "GET":
            current_method = "GET"
            current_data = None
            current_json = None
            current_params = None
        current_url = next_url


def normalize_site_ids(raw: Any) -> List[int]:
    """把 MoviePilot 多选配置统一成去重后的正整数站点 ID 列表。

    某些版本或导入方式会把多选保存成字符串。直接遍历字符串会把 ``"12"``
    错拆成站点 1、2；这里兼容 JSON 数组以及逗号、分号、中英文标点、空白分隔
    的文本，并过滤布尔值和非正整数。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            raw = re.split(r"[\s,;，；]+", text)
    if isinstance(raw, dict):
        return []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]

    result: List[int] = []
    for value in raw:
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            if not value.is_integer():
                continue
            value = int(value)
        try:
            site_id = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if site_id > 0 and site_id not in result:
            result.append(site_id)
    return result


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


# 站点换域名时把新域名指到已有规则的键，避免同一条规则维护两份。
# 实测 2026-10：hddolby.com 已无法访问（URLError），新域名 www.uhddolby.com 正常。
DOMAIN_ALIASES = {
    "uhddolby.com": "hddolby.com",      # 高清杜比换域名
}


def preset_for_host(host: str) -> Optional[Dict[str, Any]]:
    """按域名取内置预设：先精确匹配，再按「以 .<预设域名> 结尾」兜底。

    站点常用 `pt.` / `www.` / `bbs.` 前缀（例如学校是 `pt.btschool.club`，预设键写的是
    `btschool.club`），这样不必为每个子域各写一条；`open.cd` 的子域也一并覆盖。
    """
    preset = DEFAULT_RULES.get(host)
    if preset is not None:
        return preset
    target = DOMAIN_ALIASES.get(host)
    if target and target in DEFAULT_RULES:
        return DEFAULT_RULES[target]
    for key, value in DEFAULT_RULES.items():
        if host.endswith("." + key):
            return value
    for alias, target_key in DOMAIN_ALIASES.items():
        if host.endswith("." + alias) and target_key in DEFAULT_RULES:
            return DEFAULT_RULES[target_key]
    return None


def resolve_rule(site: Mapping[str, Any], custom_rules: Mapping[str, Mapping[str, Any]],
                 default_engine: str = "http") -> Dict[str, Any]:
    host = normalize_host(str(site.get("url") or ""))
    custom = (custom_rules.get(str(site.get("id"))) or custom_rules.get(host)
              or custom_rules.get("www." + host) or {})
    # 站点管理里的凭证二选一：Cookie 站点走页面流程，纯 ApiKey / Token 站点走 API。
    # 纯 Key 站点只有在自定义规则明确给出固定端点时才自动切到 API 模式，避免把 Key
    # 发到默认的 /attendance.php 或内置 Cookie 签到路径。
    auto_api = (is_api_key_site(site)
                and not str(custom.get("auth") or "").strip()
                and any(custom.get(name) for name in _API_KEY_ENDPOINT_KEYS))
    if is_api_key_rule(custom) or auto_api:
        # API Key 规则只使用用户显式声明的固定端点，不继承内置站点的普通签到路径。
        rule = dict(custom)
        if auto_api:
            rule["auth"] = API_KEY_AUTH
    else:
        preset = preset_for_host(host)
        rule = dict(preset or {})
        rule.update(custom)
    # 只有用户在自定义规则里显式声明 engine 才算「手工指定」；内置预设声明的 http
    # 仍允许在遇盾 / 验证码时自动回退到浏览器引擎
    user_engine = str(custom.get("engine") or "").strip().lower()
    declared_engine = str(rule.get("engine") or "").strip().lower()
    rule["engine"] = declared_engine or str(default_engine or "http").lower()
    if rule["engine"] not in {"http", "browserless", "playwright"}:
        raise ValueError("签到引擎只能是 http、browserless 或 playwright")
    rule["auto_fallback"] = not user_engine
    if is_api_key_rule(rule):
        # API Key 模式只访问显式配置的固定端点。Cloudflare 面前可以指定本机
        # Playwright，在同一出口 IP 的浏览器上下文内调用接口；禁止回退到云端
        # Browserless，避免站点把机房 IP 判成异地登录。
        api_engine = str(rule.get("engine") or "http").strip().lower()
        if api_engine not in {"http", "playwright"}:
            raise ValueError("API Key 规则引擎只能是 http 或 playwright")
        rule["engine"] = api_engine
        rule["auto_fallback"] = False
        rule.setdefault("success", DEFAULT_SUCCESS_PATTERN)
        rule.setdefault("already", DEFAULT_ALREADY_PATTERN)
    elif rule["engine"] in {"http", "playwright"}:
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
    auth = str(result.get("auth") or "").strip().lower()
    if auth and auth != API_KEY_AUTH:
        raise ValueError("auth 目前只支持 api_key")
    if auth == API_KEY_AUTH:
        _validate_api_key_rule(result)
        return result
    if result["engine"] in {"http", "playwright"}:
        result["path"] = _validate_site_path(result.get("path"), "签到路径")
        signin_path = str(result.get("signin_path") or "")
        if signin_path:
            result["signin_path"] = _validate_site_path(signin_path, "第二段签到地址")
        patterns = result.get("signin_token_patterns")
        if patterns is not None and not isinstance(patterns, Mapping):
            raise ValueError("signin_token_patterns 必须是 {字段: 正则} 形式的对象")
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


def _validate_api_key_rule(result: Dict[str, Any]) -> None:
    engine = str(result.get("engine") or "http").strip().lower()
    if engine not in {"http", "playwright"}:
        raise ValueError("API Key 规则只能使用 http 或 playwright 引擎")
    result["engine"] = engine
    # 签到与登录可以分开配置：只登录的站点可以仅提供 credential_check_path，
    # 只签到的站点可以仅提供 path。缺少对应端点时由执行函数在联网前明确失败。
    path = str(result.get("path") or "").strip()
    if path:
        result["path"] = _validate_site_path(path, "API 签到 path")
    else:
        result.pop("path", None)
    method = str(result.get("method") or "GET").strip().upper()
    if method not in {"GET", "POST"}:
        raise ValueError("API Key 签到 method 只能是 GET 或 POST")
    result["method"] = method
    for name in ("success", "already"):
        try:
            re.compile(str(result.get(name) or ""))
        except re.error:
            raise ValueError(f"API Key 规则的 {name} 不是有效正则") from None
    config = _api_key_config(result)
    result.update({"api_key_source": config["source"], "api_key_header": config["header"],
                   "api_key_prefix": config["prefix"]})
    data = result.get("data")
    json_body = result.get("json")
    if data is not None and not isinstance(data, Mapping):
        raise ValueError("API Key 规则的 data 必须是 JSON 对象")
    if json_body is not None and not isinstance(json_body, Mapping):
        raise ValueError("API Key 规则的 json 必须是 JSON 对象")
    if data is not None and json_body is not None:
        raise ValueError("API Key 规则的 data 与 json 不能同时配置")
    check_path = str(result.get("credential_check_path") or "").strip()
    if check_path:
        result["credential_check_path"] = _validate_site_path(
            check_path, "credential_check_path ")
    if not path and not check_path:
        raise ValueError("API Key 规则至少需要 path（签到）或 credential_check_path（登录验证）")
    check_method = str(result.get("credential_check_method") or "GET").strip().upper()
    if check_method not in {"GET", "POST"}:
        raise ValueError("credential_check_method 只能是 GET 或 POST")
    result["credential_check_method"] = check_method
    activity_path = str(result.get("activity_path") or "").strip()
    if activity_path:
        result["activity_path"] = _validate_site_path(activity_path, "activity_path ")
    result["auto_fallback"] = False


# 挑战类型 → Browserless 签到方式
FALLBACK_MODES = {"cloudflare": "cloudflare", "captcha": "open_page"}


# 验证码表单里的输入框名字（NexusPHP 多为 imagestring，其它站有 code / verify / captcha）
# name 的取值可能带引号也可能不带（NexusPHP 模板里两种都见过）
_CAPTCHA_INPUT_NAME_RE = re.compile(r"""name\s*=\s*["']?([^"'\s>]+)["']?""", re.IGNORECASE)
_CAPTCHA_NAME_HINT_RE = re.compile(r"(imagestring|code|captcha|verify|seccode)", re.IGNORECASE)


def captcha_image_rule(html: str) -> Dict[str, Any]:
    """从签到页 HTML 里推导 Browserless 图片验证码参数（selector 三件套）。

    Browserless 的 BrowserQL 自带 ``solveImageCaptcha``，OCR 认不出时用它再试一次就够了；
    这里只需要把「验证码输入框 / 验证码图片 / 提交按钮」三个选择器凑出来。
    """
    page = str(html or "")
    name = ""
    for candidate in _CAPTCHA_INPUT_NAME_RE.findall(page):
        if _CAPTCHA_NAME_HINT_RE.search(candidate):
            name = candidate
            break
    if not name:
        return {}
    escaped = name.replace("'", "")
    rule: Dict[str, Any] = {
        "mode": "image",
        "captcha_input_selector": f"input[name='{escaped}']",
        "submit_selector": "form input[type='submit'], form button[type='submit']",
        "submit_method": "click",
    }
    if "image.php" in page or "regimage" in page:
        rule["captcha_selector"] = "form:has(input[name='%s']) img" % escaped
    else:
        rule["captcha_selector"] = "form img"
    return rule

def fallback_rule(rule: Mapping[str, Any], challenge: str,
                  page_html: str = "") -> Dict[str, Any]:
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
    # 验证码站点：能从页面推导出验证码表单参数时，走 Browserless 自带的 solveImageCaptcha，
    # 这样 OCR 认不出的验证码也有第二次机会（否则只是重新打开页面，照样卡在验证码）
    if str(challenge) == "captcha" and page_html:
        derived = captcha_image_rule(page_html)
        if derived:
            fallback.update(derived)
    if rule.get("route_fragment"):
        fallback["route_fragment"] = rule["route_fragment"]
    # 让用户在 HTTP 规则里写的等待参数（如 cloudflare_wait）与点击目标（submit_selector /
    # submit_text，例如 AGSVPT 的「立即签到」）对自动回退同样生效
    for name in ("cloudflare_wait", "wait", "preflight_wait", "submit_selector", "submit_text",
                 "pre_click_selector", "pre_click_text"):
        if rule.get(name) is not None:
            fallback[name] = rule[name]
    return validate_rule(fallback)


# =============================================================== HTTP 引擎

def _is_transient(error: Exception) -> bool:
    """读超时 / 连接重置这类抖动错误：值得加长超时重试一次。"""
    name = type(error).__name__.lower()
    text_value = str(error).lower()
    if "timeout" in name or "timed out" in text_value:
        return True
    return any(key in text_value for key in ("connection aborted", "connection reset",
                                             "remotedisconnected", "remote end closed"))


def _quiz_already_result(content: str, rule: Mapping[str, Any]) -> Optional[SignResult]:
    """识别 bakatest 的今日已签到状态。"""
    pattern = str(rule.get("already") or DEFAULT_ALREADY_PATTERN)
    if not re.search(pattern, plain_text(content), re.IGNORECASE):
        return None
    bonus = extract_bonus(content, rule)
    return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")


def http_quiz_sign(session: Any, url: str, content: str, base_url: str,
                   rule: Mapping[str, Any], timeout: int,
                   proxies: Optional[Dict[str, str]]) -> SignResult:
    """完成 bakatest 选择题签到，并在提交后强制回读页面确认结果。"""
    form = extract_quiz_form(content)
    already = _quiz_already_result(content, rule)
    if already:
        return already
    checked = judge_http(content, rule)
    if checked.challenge:
        return checked
    if login_page(content) or "Cookie 无效" in checked.message:
        return SignResult("failed", "Cookie 无效或已过期")
    if not form:
        return SignResult("failed", f"未找到彩虹岛答题表单；{page_hint(content)}")

    # 答题页头部通常显示账户当前魔力值，不能把「N 点魔力值」当成签到成功；
    # 只要表单还在，就必须先选题提交。
    try:
        answer = select_quiz_choices(rule, str(form.get("question") or ""),
                                     list(form.get("choices") or []))
    except ValueError as error:
        return SignResult("failed", str(error))
    payload = build_quiz_payload(form, answer, rule)
    post_error = ""
    result_content = ""
    try:
        response = session.post(url, headers={"Referer": url}, data=payload,
                                timeout=timeout, allow_redirects=True, proxies=proxies)
        result_content = response.text or ""
        if response.status_code >= 400:
            post_error = f"答题提交失败（HTTP {response.status_code}）"
    except Exception as error:
        post_error = f"答题提交异常：{error}"

    # 无论 POST 返回什么，都以 /bakatest.php 的只读回读为准。POST 响应里的
    # 「已签到」或魔力值可能是页面头部状态，不能直接判定本次提交成功。
    verify_path = str(rule.get("verify_success_path") or rule.get("path") or "/bakatest.php")
    verify_url = target_url(base_url, verify_path)
    try:
        verify_response = session.get(verify_url, headers={"Referer": url}, timeout=timeout,
                                      allow_redirects=True, proxies=proxies)
    except Exception as error:
        detail = f"答题已提交，但回读签到状态异常：{error}"
        return SignResult("failed", f"{post_error}；{detail}" if post_error else detail,
                          transient=True)
    verify_content = verify_response.text or ""
    if verify_response.status_code >= 400:
        detail = f"答题已提交，但回读签到状态失败（HTTP {verify_response.status_code}）"
        return SignResult("failed", f"{post_error}；{detail}" if post_error else detail)

    verified_already = _quiz_already_result(verify_content, rule)
    if verified_already:
        bonus = extract_bonus((result_content or "") + "\n" + (verify_content or ""), rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")

    if extract_quiz_form(verify_content):
        return SignResult("failed",
                          "答题错误，签到未完成；可在规则 quiz_answer / quiz_answers 中配置正确选项")

    verified = judge_http(verify_content, rule)
    if verified.challenge:
        return verified
    if login_page(verify_content) or "Cookie 无效" in verified.message:
        return SignResult("failed", "Cookie 无效或已过期")
    detail = ("答题已提交，但回读页面未出现「今天已经签过到了」，无法确认签到成功；"
              + page_hint(verify_content))
    return SignResult("failed", f"{post_error}；{detail}" if post_error else detail)

def judge_api_response(content: str, rule: Mapping[str, Any],
                       secret: str = "") -> SignResult:
    """判定固定 API 端点响应；不复用 Cookie 页面的失效提示。"""
    text = str(content or "")[:200000]
    visible = plain_text(text)
    success = str(rule.get("success") or DEFAULT_SUCCESS_PATTERN)
    already = str(rule.get("already") or DEFAULT_ALREADY_PATTERN)
    try:
        if re.search(success, visible, re.IGNORECASE):
            bonus = extract_bonus(text, rule)
            return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
        if re.search(already, visible, re.IGNORECASE):
            bonus = extract_bonus(text, rule)
            return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")
    except re.error:
        return SignResult("failed", "API Key 规则的 success / already 不是有效正则")
    try:
        payload = json.loads(text.strip())
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        state = str(payload.get("state") or payload.get("status") or "").strip().lower()
        message = str(payload.get("sign") or payload.get("msg")
                      or payload.get("message") or "").strip()
        flag = payload.get("success")
        ok = state in {"success", "ok", "true", "1", "already", "signed"}
        ok = ok or flag is True or str(flag).strip().lower() in {"true", "1", "ok", "success"}
        if ok:
            status = "already" if state in {"already", "signed"} else "success"
            message = message or ("今日已签到" if status == "already" else "签到成功")
            return SignResult(status, _redact_api_secret(message, secret))
        failed = state in {"false", "fail", "failed", "error", "invalid"}
        failed = failed or flag is False or str(flag).strip().lower() in {"false", "0", "error"}
        if failed:
            detail = message or str(payload.get("error") or "站点返回 API 签到失败")
            return SignResult("failed", _redact_api_secret(detail, secret))
    return SignResult("failed",
                      _redact_api_secret(f"未匹配到 API 签到成功标志；{page_hint(text)}", secret))


def judge_api_http_response(content: str, status_code: int, rule: Mapping[str, Any],
                            secret: str = "") -> SignResult:
    """统一判定 API 响应，供 HTTP 与 Playwright 两条请求路径共用。"""
    if cloudflare_pending(content):
        return SignResult("failed", "站点返回 Cloudflare 验证页", challenge="cloudflare")
    judged = judge_api_response(content, rule, secret)
    if status_code < 400:
        return judged
    # RousiPro 等 PeerGo 站点用 HTTP 400 + code=1 表示今日已签到。
    if status_code in {400, 409} and judged.status == "already":
        return judged
    if status_code in {401, 403}:
        return SignResult("failed", f"API Key 无效或权限不足（HTTP {status_code}）")
    return SignResult("failed", f"请求失败（HTTP {status_code}）",
                      missing_path=status_code in {404, 405})


def http_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
              proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """直接 HTTP 请求签到接口；API Key 规则只访问显式固定端点。"""

    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return SignResult("failed", "站点地址为空")
    api_mode = is_api_key_rule(rule)
    secret = ""
    if not api_mode and not str(site.get("cookie") or "").strip():
        return SignResult("failed", "站点地址或 Cookie 为空")
    try:
        path = str(rule.get("path") or "").strip()
        if api_mode:
            if not path:
                return SignResult("failed", "API Key 规则未配置 path，无法签到")
            secret = _api_key_value(site, rule)
            path = _validate_site_path(path, "API 签到 path")
        elif not path:
            path = DEFAULT_HTTP_PATH
        url = target_url(base_url, path)
        if api_mode:
            headers = _api_headers(site, rule)
        else:
            headers = {
                "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
                "Referer": f"{base_url}/index.php",
                "Cookie": str(site.get("cookie") or "").strip(),
                "Accept": "application/json, text/javascript, text/html, */*",
                "X-Requested-With": "XMLHttpRequest",
            }
        session = new_http_session()
        session.headers.update(headers)
        method = str(rule.get("method") or "GET").upper()
        if api_mode and method not in {"GET", "POST"}:
            return SignResult("failed", "API Key 签到 method 只能是 GET 或 POST")
        data = rule.get("data") if isinstance(rule.get("data"), Mapping) else None
        json_body = rule.get("json") if isinstance(rule.get("json"), Mapping) else None
        if api_mode:
            if method == "GET":
                response = _api_request(session, method, url, headers, timeout, proxies,
                                        params=data if data is not None else json_body)
            else:
                response = _api_request(session, method, url, headers, timeout, proxies,
                                        data=data, json_body=json_body)
        elif method == "POST":
            if json_body is not None:
                response = session.post(url, json=json_body, timeout=timeout,
                                        allow_redirects=True, proxies=proxies)
            else:
                response = session.post(url, data=data, timeout=timeout,
                                        allow_redirects=True, proxies=proxies)
        else:
            response = session.get(url, params=data, timeout=timeout,
                                   allow_redirects=True, proxies=proxies)
    except ApiRequestError as error:
        return SignResult("failed", _redact_api_secret(error, secret))
    except ValueError as error:
        return SignResult("failed", _redact_api_secret(error, secret))
    except Exception as error:
        if _is_transient(error):
            try:
                longer = max(15, int(timeout * 1.5))
                session = new_http_session()
                session.headers.update(headers)
                if api_mode:
                    if method == "GET":
                        response = _api_request(session, method, url, headers, longer, proxies,
                                                params=data if data is not None else json_body)
                    else:
                        response = _api_request(session, method, url, headers, longer, proxies,
                                                data=data, json_body=json_body)
                elif method == "POST" and json_body is not None:
                    response = session.post(url, json=json_body, timeout=longer,
                                            allow_redirects=True, proxies=proxies)
                elif method == "POST":
                    response = session.post(url, data=data, timeout=longer,
                                            allow_redirects=True, proxies=proxies)
                else:
                    response = session.get(url, params=data, timeout=longer,
                                           allow_redirects=True, proxies=proxies)
            except Exception as retry_error:
                return SignResult("failed",
                                  _redact_api_secret(f"请求异常：{retry_error}", secret))
        else:
            return SignResult("failed", _redact_api_secret(f"请求异常：{error}", secret))
    content = str(getattr(response, "text", "") or "")
    status_code = int(getattr(response, "status_code", 0) or 0)
    if api_mode:
        return judge_api_http_response(content, status_code, rule, secret)
    if status_code >= 400:
        return SignResult("failed", f"请求失败（HTTP {status_code}）",
                          missing_path=status_code in {404, 405})
    if str(rule.get("form_mode") or "").lower() == "quiz":
        return http_quiz_sign(session, url, content, base_url, rule, timeout, proxies)
    return judge_http(content, rule)


def find_signin_form(html: str, base_url: str,
                     submit_text: str = "") -> Optional[Dict[str, Any]]:
    """从签到页里找签到表单，返回 ``{"action", "method", "data"}``；找不到返回 None。

    NexusPHP 的签到是「点表单按钮」触发的，光 GET 页面只会渲染按钮、不产生签到记录。
    这里优先找提交按钮文字含「签到 / 打卡 / 领取」（或规则的 submit_text）的表单，
    实在没有就退到「带提交按钮且 action 像签到」的那个。
    """
    wanted = str(submit_text or "").strip()
    best: Optional[Dict[str, Any]] = None
    fallback: Optional[Dict[str, Any]] = None
    for raw_attrs, inner in _FORM_RE.findall(html or ""):
        control = _submit_control(inner)
        if not control:
            continue
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        action = same_site_path(base_url, attrs.get("action") or "") or \
            (urllib.parse.urlparse(base_url).path or "/")
        method = (attrs.get("method") or "POST").strip().upper()
        label = re.sub(r"\s+", "", plain_text(inner) + str(control.get("value") or ""))
        fields: Dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", inner, re.IGNORECASE):
            item = _tag_attrs(tag)
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            kind = (item.get("type") or "text").lower()
            if kind in {"submit", "button", "image", "reset"} and \
                    not any(word in label for word in SIGNIN_LINK_WORDS):
                continue
            fields[name] = str(item.get("value") or "")
        if str(control.get("name") or "").strip():
            fields[str(control["name"])] = str(control.get("value") or "")
        form = {"action": action, "method": "POST" if method != "GET" else "GET",
                "data": fields}
        if wanted and wanted in label:
            return form
        if any(word in label for word in SIGNIN_LINK_WORDS):
            best = best or form
        elif "attendance" in action or "signin" in action or "checkin" in action:
            fallback = fallback or form
    return best or fallback


def http_sign_form(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                   proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """打开签到页、按页面上的表单 POST 提交一次，再用返回页判定。

    找不到表单（或表单是 GET 链）时返回 None，交给上层继续用 GET 结果。
    """

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    path = str(rule.get("path") or DEFAULT_HTTP_PATH)
    page_url = target_url(base_url, path)
    headers = {
        "User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
        "Referer": f"{base_url}/index.php",
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    try:
        session = new_http_session()
        session.headers.update(headers)
        page = session.get(page_url, timeout=timeout, allow_redirects=True, proxies=proxies)
        if page.status_code >= 400:
            return None
        form = find_signin_form(page.text or "", base_url, str(rule.get("submit_text") or ""))
        if not form or form["method"] != "POST":
            return None
        post_url = target_url(base_url, form["action"])
        response = session.post(post_url, data=form["data"], timeout=timeout,
                                allow_redirects=True, proxies=proxies)
    except Exception:
        return None
    if response.status_code >= 400:
        return None
    return judge_http(response.text or "", rule)


def random_form_fields(html: str) -> Dict[str, str]:
    """从签到页表单里取隐藏字段 + 随机挑一个选项/按钮。

    对应两类「答题式」签到：

    * 52pt / CHDBits 的 bakatest：隐藏字段 ``questionid`` + 若干 ``choice[]``，随便选一个提交；
    * U2 的 showup：隐藏字段 ``req`` / ``hash`` / ``form`` + 若干提交按钮（选项），随机点一个。
    """
    hidden: Dict[str, str] = {}
    options: List[tuple] = []
    for tag in re.findall(r"<input\b[^>]*>", html or "", re.IGNORECASE):
        attrs = _tag_attrs(tag)
        name = str(attrs.get("name") or "").strip()
        if not name:
            continue
        kind = str(attrs.get("type") or "text").lower()
        value = str(attrs.get("value") or "")
        if kind in {"submit", "button", "image"}:
            options.append((name, value))
        elif kind in {"radio", "checkbox"} or "choice" in name:
            options.append((name, value))
        elif kind == "hidden":
            hidden[name] = value
    if not options:
        return hidden
    name, value = random.choice(options)
    hidden[name] = value
    return hidden


DEFAULT_QUIZ_FAILURE_PATTERN = r"(回答错误|答案错误|请重新|不正确|失去)"


def _normalize_quiz_question(value: Any) -> str:
    return re.sub(r"[\s:：?？]+", "", str(value or "")).lower()


def _answer_values(value: Any) -> List[str]:
    """把单个选项或逗号分隔的多选答案统一成去重列表。"""
    items = value if isinstance(value, (list, tuple, set)) else [value]
    result: List[str] = []
    for item in items:
        for part in re.split(r"[,，;；\s]+", str(item or "")):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return result


def extract_quiz_form(content: str) -> Optional[Dict[str, Any]]:
    """从 NexusPHP bakatest 页面提取题号、选择项、题目和隐藏字段。"""
    question_id = ""
    choices: List[str] = []
    fields: Dict[str, str] = {}
    for tag in _INPUT_RE.findall(content or ""):
        attrs = _tag_attrs(tag)
        name = str(attrs.get("name") or "").strip()
        value = str(attrs.get("value") or "")
        kind = str(attrs.get("type") or "text").lower()
        if name == "questionid" and value:
            question_id = value
        elif name == "choice[]" and value:
            choices.append(value)
        elif name and value and kind not in {
                "submit", "button", "reset", "image", "file", "radio", "checkbox"}:
            fields[name] = value
    if not question_id or not choices:
        return None

    question = ""
    match = re.search(
        r"请问\s*[:：]\s*(.+?)(?:</td\s*>|<br\s*/?>|</div\s*>|</p\s*>|</li\s*>)",
        content or "",
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        question = plain_text(match.group(1))
    else:
        visible = plain_text(content or "")
        match = re.search(r"请问\s*[:：]\s*(.{1,300})", visible)
        if match:
            question = match.group(1).strip()
    return {"questionid": question_id, "choices": choices,
            "question": question, "fields": fields}


def select_quiz_choices(rule: Mapping[str, Any], question: str,
                        choices: Sequence[str]) -> List[str]:
    """返回要提交的 choice[]；固定答案缺失或未命中时沿用随机策略。"""
    if not choices:
        return []
    answer: List[str] = []
    direct = rule.get("quiz_answer")
    if direct is not None and str(direct).strip():
        answer = _answer_values(direct)
    if not answer and isinstance(rule.get("quiz_answers"), Mapping):
        target = _normalize_quiz_question(question)
        for key, value in rule["quiz_answers"].items():
            candidate = _normalize_quiz_question(key)
            if candidate and (candidate == target or candidate in target):
                answer = _answer_values(value)
                break
    if not answer:
        answer = [random.choice(list(choices))]
    invalid = [value for value in answer if value not in choices]
    if invalid:
        raise ValueError("quiz_answer 中包含页面不存在的选项值：" + ", ".join(invalid))
    return answer


def build_quiz_payload(form: Mapping[str, Any], answer: Sequence[str],
                       rule: Mapping[str, Any]) -> Dict[str, Any]:
    """合并隐藏字段、题号、选择和可配置的附加表单字段。"""
    data: Dict[str, Any] = dict(form.get("fields") or {})
    data["questionid"] = str(form.get("questionid") or "")
    data["choice[]"] = list(answer) if len(answer) > 1 else answer[0]
    custom = rule.get("quiz_fields")
    if isinstance(custom, Mapping):
        data.update({str(key): value for key, value in custom.items()})
    return data

def http_two_step(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                  proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """第二段签到：先读首页，按规则抓 token / 直接访问签到地址，再判定。

    对应两类站点（都是 thsrite 的「站点自动签到」里验证过的流程）：

    * TTG 这种：首页 JS 里有 ``signed_timestamp`` / ``signed_token``，要 POST 到 ``/signed.php``；
    * 学校这种：首页有「每日签到」入口，访问 ``/index.php?action=addbonus`` 后入口消失即签到成功。

    规则字段：``signin_path``（第二段地址）、``signin_method``（默认有参数就 POST）、
    ``signin_data``（固定字段）、``signin_token_patterns``（{字段: 正则}，从首页抓）、
    ``signin_absent_status``（第二段页面上 absent_keywords 消失时判什么状态）。
    """

    signin_path = str(rule.get("signin_path") or "").strip()
    if not signin_path:
        return None
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    ua = str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT
    first_url = target_url(base_url, str(rule.get("path") or DEFAULT_HTTP_PATH))
    first = fetch_page(first_url, cookie, timeout, proxies, ua)
    if not first:
        return None

    data: Dict[str, str] = {}
    static = rule.get("signin_data")
    if isinstance(static, Mapping):
        data.update({str(key): str(value) for key, value in static.items()})
    patterns = rule.get("signin_token_patterns")
    if isinstance(patterns, Mapping):
        for name, pattern in patterns.items():
            match = re.search(str(pattern), first)
            if not match:
                # 抓不到 token 就别盲目提交，交给上层继续报原始失败原因
                return None
            data[str(name)] = match.group(1) if match.groups() else match.group(0)
    picks: Optional[Dict[str, str]] = None
    if str(rule.get("signin_pick") or "").lower() == "random":
        picks = random_form_fields(first)
        data.update(picks)

    method = str(rule.get("signin_method") or ("POST" if data else "GET")).upper()
    url = target_url(base_url, signin_path)
    headers = {"User-Agent": ua, "Cookie": cookie, "Referer": first_url,
               "Accept": "text/html,application/json,*/*;q=0.8"}
    extra_headers = rule.get("signin_headers")
    if isinstance(extra_headers, Mapping):
        for name, value in extra_headers.items():
            try:
                headers[str(name)] = str(value).format(**{k: v for k, v in data.items()
                                                         if isinstance(k, str) and k.isidentifier()})
            except (KeyError, IndexError, ValueError):
                headers[str(name)] = str(value)
    raw_body = rule.get("signin_json_raw")
    try:
        session = new_http_session()
        session.headers.update(headers)
        if raw_body:
            response = session.post(url, data=str(raw_body).encode("utf-8"),
                                    timeout=timeout, allow_redirects=True, proxies=proxies)
        elif method == "POST":
            response = session.post(url, data=data or None, timeout=timeout,
                                    allow_redirects=True, proxies=proxies)
        else:
            response = session.get(url, params=data or None, timeout=timeout,
                                   allow_redirects=True, proxies=proxies)
    except Exception:
        return None
    if response.status_code >= 400:
        return None

    # 第二段页面上「签到入口消失」也是一种成功信号（学校：addbonus 后首页不再有「每日签到」）
    probe = dict(rule)
    if rule.get("signin_absent_status"):
        probe["absent_status"] = rule["signin_absent_status"]
    judged = judge_http(response.text or "", probe)
    if judged.status in {"success", "already"}:
        return SignResult(judged.status, f"{judged.message}（{signin_path}）")
    # 答题式签到（52PT / CHDBits / U2）：答错不算签到成功，换一个选项再来，最多试 3 次
    if picks and str(rule.get("signin_pick") or "").lower() == "random":
        for _ in range(2):
            page = fetch_page(first_url, cookie, timeout, proxies, ua)
            if not page:
                break
            retry_data = {key: value for key, value in data.items() if key not in picks}
            retry_picks = random_form_fields(page)
            if not retry_picks:
                break
            retry_data.update(retry_picks)
            try:
                session = new_http_session()
                session.headers.update(headers)
                retry = session.post(url, data=retry_data or None, timeout=timeout,
                                     allow_redirects=True, proxies=proxies)
            except Exception:
                break
            if retry.status_code >= 400:
                break
            judged = judge_http(retry.text or "", probe)
            if judged.status in {"success", "already"}:
                return SignResult(judged.status, f"{judged.message}（{signin_path}）")
    return None


def http_confirm(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                 proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """回读签到页确认结果：有的站点提交后返回 302 / 空白页，只有再打开一次才看得到状态。"""
    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url or not cookie:
        return None
    path = str(rule.get("path") or DEFAULT_HTTP_PATH)
    html = fetch_page(target_url(base_url, path), cookie, timeout, proxies,
                      str(site.get("ua") or ""))
    if not html:
        return None
    return judge_http(html, rule)


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

    session = new_http_session()
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


# ---------------------------------------------------------------- 本地浏览器引擎
# 用 MoviePilot 自带的 PlaywrightHelper 在**本机**开浏览器签到：
# IP 就是运营商/家里的 IP，站点不会判「异地登录」，也不消耗 Browserless 额度。
_PAGE_CLICK_JS = """
(args) => {
  const [selector, text, excludeTexts] = args;
  const nodes = Array.from(document.querySelectorAll(
    selector || "button, a, [role='button'], [role='menuitem'], input[type='submit'], li, span"));
  const excluded = (excludeTexts || []).filter(Boolean);
  const hits = nodes
    .filter((node) => {
      const label = (node.value || node.textContent || "").trim();
      const visible = node.offsetParent !== null || node.getClientRects().length > 0;
      return visible && label.includes(text)
        && !excluded.some((word) => label.includes(word));
    })
    .sort((left, right) => {
      const a = (left.value || left.textContent || "").trim().length;
      const b = (right.value || right.textContent || "").trim().length;
      return a - b;
    });
  if (!hits.length) return false;
  const target = hits[0];
  target.scrollIntoView({block: "center"});
  target.click();
  return true;
}
"""


# 先给最合适的签到按钮打标记，再用 Playwright 的真实 click 点它。
# 用 JS 直接 node.click() 只是派发 DOM 事件，部分站点的原生提交 / 跳转不认
# （tmzg 的 captchasignin 也是用 page.click 才在猪猪上点成功的）。
_MARK_CLICK_JS = """
(args) => {
  const [selector, text, excludeTexts] = args;
  const nodes = Array.from(document.querySelectorAll(
    selector || "input[type='submit'], button, a, [role='button']"));
  const wanted = (text || "").trim();
  const excluded = (excludeTexts || []).filter(Boolean);
  const hits = nodes.filter((node) => {
    if (node.disabled) return false;
    const label = ((node.value || node.textContent || "") + " " + (node.getAttribute("aria-label") || "")
                   + " " + (node.getAttribute("title") || "")).trim();
    const visible = node.offsetParent !== null || node.getClientRects().length > 0;
    return visible && (!wanted || label.includes(wanted))
      && !excluded.some((word) => label.includes(word));
  });
  if (!hits.length) return false;
  hits.sort((left, right) => {
    const a = (left.value || left.textContent || "").trim();
    const b = (right.value || right.textContent || "").trim();
    return Number(b === wanted) - Number(a === wanted) || a.length - b.length;
  });
  hits[0].setAttribute("data-ptsignin-target", "1");
  hits[0].scrollIntoView({block: "center"});
  return true;
}
"""

_CLICK_MARKED_SELECTOR = '[data-ptsignin-target="1"]'


# 只读取当天的签到控件，不能拿整页文字判断签到状态：日历里历史日期也会显示
# 「已签到」，整页 contains("已签到") 会把今天尚未签到误判成已完成。
_PLAYWRIGHT_CONTROL_STATE_JS = r"""
(args) => {
  const [selector, alreadyTexts, skipTexts, submitText] = args;
  const node = selector ? document.querySelector(selector) : null;
  if (!node) return {
    found: false, present: false, disabled: false, actionable: false,
    text: "", already: false, skip: false,
  };
  const label = ((node.innerText || node.textContent || node.value || "") + " "
    + (node.getAttribute("aria-label") || "") + " " + (node.getAttribute("title") || ""))
    .replace(/\s+/g, " ").trim();
  const visible = node.offsetParent !== null || node.getClientRects().length > 0;
  const disabled = !!node.disabled || node.getAttribute("aria-disabled") === "true";
  const already = (alreadyTexts || []).some((word) => label.includes(word));
  const skipped = (skipTexts || []).some((word) => label.includes(word));
  const actionText = String(submitText || "");
  return {
    found: true,
    present: visible,
    disabled: disabled,
    actionable: visible && !disabled && !already && !skipped
      && (!actionText || label.startsWith(actionText)),
    text: label,
    already: already,
    skip: skipped,
  };
}
"""


def playwright_control_result(state: Mapping[str, Any], clicked: bool,
                              submit_text: str) -> Optional[SignResult]:
    """判定当天签到控件状态；禁用、补签或文字不符都不能算成功。"""
    if not state or not state.get("found") or state.get("present") is False:
        return None
    if state.get("skip"):
        return SignResult("failed", "当天签到状态为补签，未执行自动签到")
    if state.get("already"):
        if clicked:
            return SignResult("success", "签到成功")
        return SignResult("already", "今日已经签到")
    text = str(state.get("text") or "").strip()
    if state.get("disabled"):
        prefix = "签到请求提交后控件仍不可用" if clicked else "当天签到控件不可用"
        return SignResult("failed", f"{prefix}（{text[:40] or '已禁用'}），未确认签到成功")
    if submit_text and not text.startswith(submit_text):
        return SignResult("failed", f"当天签到控件状态未识别（{text[:60] or '无文字'}）")
    return None


def captcha_response_result(status: int, payload: Any) -> Optional[SignResult]:
    """识别签到接口的人机验证失败，不输出响应中的 token 或其它敏感数据。"""
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    data = data if isinstance(data, Mapping) else {}
    need_captcha = any(bool(data.get(key) or payload.get(key))
                       for key in ("need_captcha", "site_captcha"))
    message = next((str(value) for value in (
        data.get("message"), data.get("msg"), payload.get("msg"),
        payload.get("message"), payload.get("error"),
    ) if value), "")
    captcha_message = any(word in message for word in ("人机验证", "人機驗證", "captcha"))
    if 200 <= int(status) < 300 and not need_captcha:
        return None
    if not need_captcha and not captcha_message:
        return None
    return SignResult(
        "failed",
        f"签到接口要求完成人机验证（Cloudflare Turnstile，HTTP {status}）；"
        "当前浏览器未通过验证，未确认签到成功",
        challenge="captcha",
    )


def playwright_response_matches_path(expected_path: str, response: Any) -> bool:
    """只比较响应 URL 路径，不读取响应正文。"""
    try:
        raw_url = response.url
        url = raw_url() if callable(raw_url) else raw_url
        actual_path = urllib.parse.urlsplit(str(url or "")).path.rstrip("/")
        expected_path = str(expected_path or "").rstrip("/")
        return bool(expected_path) and actual_path == expected_path
    except Exception:
        return False


def playwright_captcha_response_result(expected_path: str,
                                      response: Any) -> Optional[SignResult]:
    """只从签到接口响应提取状态码和 JSON 判定字段，不保存响应正文。"""
    if not playwright_response_matches_path(expected_path, response):
        return None
    try:
        raw_status = response.status
        status = int(raw_status() if callable(raw_status) else raw_status)
        payload = response.json()
    except Exception:
        return None
    return captcha_response_result(status, payload)


_PLAYWRIGHT_CAPTCHA_TOKEN_STATE_JS = r"""
(selector) => {
  const node = selector ? document.querySelector(selector) : null;
  const value = node ? String(node.value || node.textContent || "").trim() : "";
  return {found: !!node, ready: value.length > 0};
}
"""


def _wait_playwright_captcha_token(page: Any, selector: str, timeout_ms: int) -> bool:
    """等待页面 JS 写入 Turnstile token；不读取或返回 token 内容。"""
    if not selector:
        return False
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while True:
        try:
            state = page.evaluate(_PLAYWRIGHT_CAPTCHA_TOKEN_STATE_JS, selector)
        except Exception:
            state = None
        if isinstance(state, Mapping) and state.get("ready"):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            page.wait_for_timeout(min(500, max(1, int(remaining * 1000))))
        except Exception:
            time.sleep(min(0.5, remaining))


# 登录页和空闲锁定页都由站点前端自己处理：这里只负责识别、填密码、点提交，
# 后续 SRP / token / 请求签名全部交给页面 JS，避免复制站点鉴权协议。
_PLAYWRIGHT_AUTH_STATE_JS = """
() => {
  const visible = (node) => !!node && (node.offsetParent !== null || node.getClientRects().length > 0);
  const loginUser = document.querySelector("input[name='username']");
  const loginPassword = document.querySelector("input[name='password']");
  const loginSubmit = document.querySelector("button.btn");
  const locked = document.querySelector(".denied-page, #idle-lock-title");
  const unlockPassword = document.querySelector("form.step-up input[name='password']");
  const unlockSubmit = document.querySelector("form.step-up button[type='submit']");
  const bodyText = (document.body && document.body.innerText) || "";
  return {
    login: visible(loginUser) && visible(loginPassword) && visible(loginSubmit),
    locked: visible(locked) || bodyText.includes("页面已锁定")
      || bodyText.includes("空闲过久，请验证是你本人"),
    stepUp: visible(unlockPassword) && visible(unlockSubmit),
  };
}
"""

_PLAYWRIGHT_PAGE_READY_JS = r"""
(args) => {
  const [readySelector, calendarSelector, loadingSelector, stateSelector] = args;
  const visible = (node) => !!node
    && (node.offsetParent !== null || node.getClientRects().length > 0);
  const visibleMatch = (selector) => selector
    ? visible(document.querySelector(selector)) : false;
  const calendarNode = calendarSelector ? document.querySelector(calendarSelector) : null;
  const loadingNode = loadingSelector ? document.querySelector(loadingSelector) : null;
  const controlNode = stateSelector ? document.querySelector(stateSelector) : null;
  const loginUser = document.querySelector("input[name='username']");
  const loginPassword = document.querySelector("input[name='password']");
  const loginSubmit = document.querySelector("button.btn");
  const unlockPassword = document.querySelector("form.step-up input[name='password']");
  const unlockSubmit = document.querySelector("form.step-up button[type='submit']");
  const lockedNode = document.querySelector(".denied-page, #idle-lock-title");
  const bodyText = (document.body && document.body.innerText) || "";
  const login = visible(loginUser) && visible(loginPassword) && visible(loginSubmit);
  const locked = visible(lockedNode) || bodyText.includes("页面已锁定")
    || bodyText.includes("空闲过久，请验证是你本人");
  const stepUp = visible(unlockPassword) && visible(unlockSubmit);
  const calendarFound = !!calendarNode;
  const calendarLoading = visible(loadingNode);
  const controlFound = !!controlNode;
  const controlPresent = visible(controlNode);
  return {
    ready: visibleMatch(readySelector) || login || locked || stepUp,
    login: login,
    locked: locked,
    stepUp: stepUp,
    calendarFound: calendarFound,
    calendarLoading: calendarLoading,
    calendarReady: visible(calendarNode) && !calendarLoading,
    controlFound: controlFound,
    controlPresent: controlPresent,
    control: {found: controlFound, present: controlPresent},
    url: location.href,
  };
}
"""


_CHALLENGE_PATTERN = re.compile(r"(just a moment|cf-chl|_cf_chl_opt|chl_page|"
                                r"正在进行安全检测|安全检测能力由|雷池\s*WAF|SafeLine|"
                                r"正在验证您是否是真人|verify you are human|checking your browser)",
                                re.IGNORECASE)
_CF_FRAME_RE = "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"


def _page_has_challenge(html: str) -> bool:
    """页面是否还停在 Cloudflare / 雷池 的检测页。"""
    return bool(html) and bool(_CHALLENGE_PATTERN.search(html))


def _click_cf_checkbox(page: Any) -> bool:
    """试着点 Cloudflare/Turnstile 的「验证您是真人」勾选框（在 iframe 里）。"""
    try:
        frames = getattr(page, "frames", None)
        targets = [frame for frame in (frames or []) if "challenges.cloudflare.com" in
                   str(getattr(frame, "url", "") or "")]
        for frame in targets:
            for selector in ("input[type='checkbox']", "label", "#challenge-stage"):
                try:
                    frame.click(selector, timeout=1500)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    # 退化方案：用 frame_locator 点 iframe 内的勾选框
    try:
        page.frame_locator(_CF_FRAME_RE).locator("input[type='checkbox']").first.click(timeout=1500)
        return True
    except Exception:
        return False


def _wait_challenge_clear(page: Any, budget_ms: int) -> str:
    """等检测页自己放行，返回最后的页面 HTML。"""
    import time as _time

    deadline = _time.monotonic() + max(3, budget_ms / 1000)
    html = ""
    while _time.monotonic() < deadline:
        try:
            html = page.content() or ""
        except Exception:
            html = ""
        if not _page_has_challenge(html):
            return html
        _click_cf_checkbox(page)
        try:
            page.wait_for_timeout(1500)
        except Exception:
            _time.sleep(1.5)
    return html


def _playwright_auth_state(page: Any) -> Dict[str, Any]:
    """读取登录页 / 空闲锁定页状态；页面尚未就绪时返回空状态。"""
    try:
        value = page.evaluate(_PLAYWRIGHT_AUTH_STATE_JS)
    except Exception:
        value = None
    return dict(value) if isinstance(value, Mapping) else {}


def _wait_playwright_auth_state(page: Any, predicate: Any, timeout_ms: int) -> Dict[str, Any]:
    """在同一个浏览器会话里等登录或解锁完成，不读取也不输出任何凭据。"""
    import time as _time

    deadline = _time.monotonic() + max(1, timeout_ms / 1000)
    state = _playwright_auth_state(page)
    while _time.monotonic() < deadline:
        if predicate(state):
            return state
        try:
            page.wait_for_timeout(500)
        except Exception:
            _time.sleep(0.5)
        state = _playwright_auth_state(page)
    return state


_PLAYWRIGHT_API_FETCH_JS = r"""
async ({url, method, headers, body}) => {
  try {
    const options = {
      method,
      headers,
      credentials: "include",
      cache: "no-store",
      redirect: "error",
    };
    if (body !== null && body !== undefined) options.body = body;
    const response = await fetch(url, options);
    return {
      status: response.status,
      url: response.url || url,
      text: await response.text(),
    };
  } catch (error) {
    return {error: String((error && error.message) || error || "fetch failed")};
  }
}
"""


def playwright_api_fetch(site: Mapping[str, Any], rule: Mapping[str, Any],
                         path: str, method: str = "GET", timeout: int = 90,
                         proxies: Optional[Dict[str, str]] = None,
                         include_rule_body: bool = True) -> Optional[Dict[str, Any]]:
    """在 MoviePilot 本机浏览器里调用同源 API。

    先打开站点并等待 Cloudflare/WAF 放行，再在同一个浏览器上下文内用 fetch
    请求固定端点。这样 API Key 只在浏览器会话里发送，出口 IP 与 MoviePilot
    一致，也不会经过云端 Browserless 的机房 IP。
    """
    PlaywrightHelper = _load_playwright_helper()
    if PlaywrightHelper is None or not hasattr(PlaywrightHelper, "action"):
        return None

    secret = ""
    try:
        base_url = str(site.get("url") or "").rstrip("/")
        if not base_url:
            return {"error": "站点地址为空"}
        method = str(method or "GET").upper()
        if method not in {"GET", "POST"}:
            return {"error": "API Key 浏览器请求 method 只能是 GET 或 POST"}
        path = _validate_site_path(path, "API 签到 path")
        secret = _api_key_value(site, rule)
        headers = {
            name: value for name, value in _api_headers(site, rule).items()
            if name.lower() not in {"user-agent", "cookie", "host", "content-length"}
        }
        data = (rule.get("data") if include_rule_body
                and isinstance(rule.get("data"), Mapping) else None)
        json_body = (rule.get("json") if include_rule_body
                     and isinstance(rule.get("json"), Mapping) else None)
        if data is not None and json_body is not None:
            return {"error": "API Key 规则的 data 与 json 不能同时配置"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        elif data is not None:
            encoded = urllib.parse.urlencode(data, doseq=True)
            if method == "GET":
                path += ("&" if "?" in path else "?") + encoded
            else:
                body = encoded
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        url = target_url(base_url, path)
    except (TypeError, ValueError) as error:
        return {"error": _redact_api_secret(error, secret)}

    cookie = str(site.get("cookie") or "").strip()
    try:
        challenge_budget = int(float(rule.get("cloudflare_wait") or 20)) * 1000
    except (TypeError, ValueError):
        challenge_budget = 20000
    result: Dict[str, Any] = {}

    def callback(page: Any) -> Dict[str, Any]:
        nonlocal result
        if cookie:
            try:
                items = cookie_list_for_context(cookie, base_url)
                if items:
                    page.context.add_cookies(items)
            except Exception:
                pass
        try:
            page.goto(base_url + "/", wait_until="domcontentloaded",
                      timeout=max(15000, int(timeout * 1000)))
        except Exception:
            pass
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        html = _wait_challenge_clear(page, challenge_budget)
        if _page_has_challenge(html):
            result = {"status": 403, "text": html, "challenge_page": True}
            return result
        auth_state = _playwright_auth_state(page)
        if auth_state.get("stepUp") or auth_state.get("locked"):
            try:
                html = page.content() or html
            except Exception:
                pass
            result = {"status": 403, "text": html, "second_factor": True}
            return result
        try:
            value = page.evaluate(_PLAYWRIGHT_API_FETCH_JS, {
                "url": url,
                "method": method,
                "headers": headers,
                "body": body,
            })
        except Exception as error:
            result = {"error": _redact_api_secret(error, secret)}
            return result
        result = dict(value) if isinstance(value, Mapping) else {
            "error": "本地浏览器没有返回 API 响应"}
        return result

    try:
        outcome = PlaywrightHelper().action(url="about:blank", callback=callback,
                                            cookies=None,
                                            ua=str(site.get("ua") or "").strip() or None,
                                            proxies=dict(proxies) if proxies else None,
                                            headless=True,
                                            timeout=max(60, int(timeout)))
    except Exception as error:
        return {"error": _redact_api_secret(f"本地浏览器 API 请求异常：{error}", secret)}
    if not result and isinstance(outcome, Mapping):
        result = dict(outcome)
    if not result:
        return {"error": "本地浏览器没有返回 API 响应"}
    if result.get("error"):
        result["error"] = _redact_api_secret(result["error"], secret)
    return result


def _playwright_page_ready_state(page: Any, rule: Mapping[str, Any]) -> Dict[str, Any]:
    """读取单页应用是否已渲染出日历、登录页或空闲锁定页。"""
    try:
        value = page.evaluate(_PLAYWRIGHT_PAGE_READY_JS, [
            str(rule.get("playwright_page_ready_selector") or ""),
            str(rule.get("playwright_calendar_selector") or ""),
            str(rule.get("playwright_calendar_loading_selector") or ""),
            str(rule.get("playwright_state_selector") or ""),
        ])
    except Exception:
        value = None
    return dict(value) if isinstance(value, Mapping) else {}


def _wait_playwright_page_ready(page: Any, rule: Mapping[str, Any],
                                timeout_ms: int = 20000) -> Dict[str, Any]:
    """等 SPA 完成首屏渲染；骨架屏、登录页和解锁页都算可处理状态。"""
    import time as _time

    deadline = _time.monotonic() + max(0.5, timeout_ms / 1000)
    state = _playwright_page_ready_state(page, rule)
    while _time.monotonic() < deadline:
        if state.get("ready"):
            return state
        try:
            page.wait_for_timeout(500)
        except Exception:
            _time.sleep(0.5)
        state = _playwright_page_ready_state(page, rule)
    return state


def _page_ready_timeout_message(state: Mapping[str, Any]) -> str:
    """把页面就绪超时翻译成可执行的失败原因，不输出页面脚本变量。"""
    if state.get("calendarLoading"):
        return "等待签到页加载超时：日历仍在显示加载骨架屏，未读取签到控件"
    if not state.get("calendarFound"):
        return "等待签到页就绪超时：未出现日历容器，也未识别到登录或解锁页"
    return "等待签到页就绪超时：页面状态未稳定，未读取签到控件"


def _prepare_browser_auth(page: Any, site: Mapping[str, Any],
                          rule: Mapping[str, Any], timeout: int = 90) -> Tuple[SignResult, bool]:
    """处理猪猪这类站点的浏览器内登录与空闲解锁。

    返回 ``(结果, 是否发生登录/解锁)``。只把密码填进页面，不自行调用登录 API，
    也不把密码拼进异常、日志或返回消息。没有密码时，只有页面确实要求登录/解锁才报错；
    已有有效 Cookie 的站点仍可直接签到。
    """
    state = _playwright_auth_state(page)
    changed = False
    username = str(site.get("username") or site.get("user") or site.get("account") or "").strip()
    password = str(site.get("password") or site.get("passwd") or site.get("pwd") or "")
    wait_ms = min(max(int(timeout) * 1000, 15000), 45000)

    if state.get("login"):
        if not username or not password:
            return SignResult(
                "failed",
                "站点要求登录，请在站点附加信息配置账号和当前密码以自动签到"), changed
        try:
            page.fill(str(rule.get("browser_login_username_selector")
                          or "input[name='username']"), username)
            page.fill(str(rule.get("browser_login_password_selector")
                          or "input[name='password']"), password)
            page.click(str(rule.get("browser_login_submit_selector") or "button.btn"),
                       timeout=10000)
        except Exception:
            return SignResult("failed", "浏览器内自动登录失败，请检查登录页是否改版"), changed
        changed = True
        state = _wait_playwright_auth_state(
            page, lambda value: not bool(value.get("login")), wait_ms)
        if state.get("login"):
            return SignResult(
                "failed",
                "浏览器内自动登录未完成，请检查账号密码或站点登录页是否改版"), changed

    # 登录后页面可能立即进入空闲锁定页；也可能一开始就是锁定页。
    state = _playwright_auth_state(page)
    if state.get("locked") or state.get("stepUp"):
        if not password:
            return SignResult(
                "failed",
                "站点页面已锁定，请在站点附加信息配置当前密码以自动解锁"), changed
        try:
            page.fill(str(rule.get("browser_unlock_password_selector")
                          or "form.step-up input[name='password']"), password)
            page.click(str(rule.get("browser_unlock_submit_selector")
                           or "form.step-up button[type='submit']"), timeout=10000)
        except Exception:
            return SignResult("failed", "页面解锁失败，请检查锁定页是否改版"), changed
        changed = True
        state = _wait_playwright_auth_state(
            page, lambda value: not value.get("locked") and not value.get("stepUp"), wait_ms)
        if state.get("locked") or state.get("stepUp"):
            return SignResult("failed", "页面解锁未完成，请检查当前密码是否正确"), changed
    return SignResult("success", ""), changed


def _load_playwright_helper():
    """取宿主浏览器助手：V3 优先稳定 SDK，V2 回退旧路径；都没有返回 None。"""
    try:  # MoviePilot V3
        from app.sdk.browser import PlaywrightHelper
        return PlaywrightHelper
    except ImportError:
        pass
    try:  # MoviePilot V2
        from app.helper.browser import PlaywrightHelper
        return PlaywrightHelper
    except ImportError:
        return None


def playwright_available() -> bool:
    """MoviePilot 是否带 PlaywrightHelper（官方镜像默认带）。"""
    return _load_playwright_helper() is not None


def cookie_list_for_context(cookie: str, url: str) -> List[Dict[str, Any]]:
    """把 "a=1; b=2" 转成 Playwright ``context.add_cookies`` 需要的列表。

    猪猪这类双盾站点在验证过程中会下发新的 cf_clearance / sl_* Cookie：只有把账号 Cookie
    写进浏览器 Cookie 存储，这些新 Cookie 才能随后续请求一起发出去；用 MP 的固定
    ``extra_http_headers`` 注入则永远只带旧值，验证会一直循环。
    """
    from urllib.parse import urlparse

    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return []
    items: List[Dict[str, Any]] = []
    for chunk in str(cookie or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if not name:
            continue
        items.append({"name": name, "value": value.strip(), "url": f"{parsed.scheme}://{parsed.netloc}/"})
    return items


def playwright_paths(rule: Mapping[str, Any]) -> List[str]:
    """本机浏览器要依次尝试的页面：主 path 在前，retry_paths 依次兜底。

    有的站点（猪猪）首页只有「[签到已得…]」链接，真正的签到表单在 attendance.php，
    所以允许规则再给几个备用页面，找不到按钮就换下一个。
    """
    paths: List[str] = []
    primary = str(rule.get("path") or DEFAULT_HTTP_PATH).strip()
    candidates = [primary] + list(rule.get("retry_paths") or [])
    for value in candidates:
        item = str(value or "").strip()
        if item and item not in paths:
            paths.append(item)
    return paths or [DEFAULT_HTTP_PATH]


def playwright_sign(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 90,
                    proxies: Optional[Dict[str, str]] = None) -> Optional[SignResult]:
    """用本机浏览器完成签到。

    普通规则走页面按钮流程；API Key 规则先在浏览器里通过 Cloudflare/WAF，
    再在同一个页面上下文内调用固定 API 端点。返回 None 表示这台机器没有
    PlaywrightHelper（交回上层处理）。
    """
    PlaywrightHelper = _load_playwright_helper()
    if PlaywrightHelper is None:
        return None
    # 老版本 MoviePilot 的 PlaywrightHelper 只有 get_page_source，没有 action：
    # 这种情况返回 None，让上层退回 Browserless，而不是硬报错
    if not hasattr(PlaywrightHelper, "action"):
        return None

    base_url = str(site.get("url") or "").rstrip("/")
    cookie = str(site.get("cookie") or "").strip()
    if not base_url:
        return None
    if is_api_key_rule(rule):
        path = str(rule.get("path") or "").strip()
        if not path:
            return SignResult("failed", "API Key 规则未配置 path，无法签到")
        method = str(rule.get("method") or "GET").upper()
        secret = ""
        try:
            secret = _api_key_value(site, rule)
            response = playwright_api_fetch(
                site, rule, path, method=method, timeout=timeout, proxies=proxies)
        except (TypeError, ValueError) as error:
            return SignResult("failed", _redact_api_secret(error, secret))
        if response is None:
            return None
        if response.get("second_factor"):
            return SignResult(
                "failed",
                "本地浏览器仍要求异地登录 / 二级验证；请先在 MoviePilot 所在网络完成一次验证",
                challenge="captcha")
        if response.get("challenge_page"):
            return SignResult("failed", "本地浏览器仍卡在 Cloudflare / WAF 验证页",
                              challenge="cloudflare")
        if response.get("error"):
            return SignResult("failed", _redact_api_secret(response["error"], secret))
        content = str(response.get("text") or "")
        try:
            status_code = int(response.get("status") or 0)
        except (TypeError, ValueError):
            status_code = 0
        return judge_api_http_response(content, status_code, rule, secret)
    paths = playwright_paths(rule)
    selector = str(rule.get("submit_selector") or "")
    submit_text = str(rule.get("submit_text") or "")
    state_selector = str(rule.get("playwright_state_selector") or "").strip()
    already_texts = [str(item) for item in (rule.get("playwright_already_texts") or [])
                     if str(item).strip()]
    skip_texts = [str(item) for item in (rule.get("playwright_skip_texts") or [])
                  if str(item).strip()]
    excluded_texts = list(dict.fromkeys(skip_texts + already_texts))
    wait_ms = int(rule.get("wait") or 4000)

    # CF / 雷池 都靠页面自己的 JS 跑完才放行：勾选式还要点一下 iframe 里的勾选框。
    # 预算取规则里的 cloudflare_wait（秒），没写就按 20 秒。
    try:
        challenge_budget = int(float(rule.get("cloudflare_wait") or 20)) * 1000
    except (TypeError, ValueError):
        challenge_budget = 20000
    # 挑战页本身也要能点勾选，给足超时
    wait_ms = max(wait_ms, 4000)

    cookie_mode = str(rule.get("cookie_mode") or "header").lower()
    browser_auth_mode = str(rule.get("browser_auth_mode") or "").strip().lower()
    captcha_response_path = str(rule.get("captcha_response_path") or "").strip()
    captcha_token_selector = str(
        rule.get("playwright_captcha_token_selector") or "").strip()
    try:
        captcha_wait_ms = int(rule.get("playwright_captcha_wait") or 30000)
    except (TypeError, ValueError):
        captcha_wait_ms = 30000
    wait_for_ready = bool(rule.get("playwright_wait_for_ready"))
    try:
        ready_wait_ms = int(rule.get("playwright_ready_wait") or 20000)
    except (TypeError, ValueError):
        ready_wait_ms = 20000
    ready_wait_ms = max(500, min(ready_wait_ms, 60000))

    def control_state(page: Any) -> Dict[str, Any]:
        if not state_selector:
            return {}
        try:
            value = page.evaluate(_PLAYWRIGHT_CONTROL_STATE_JS,
                                  [state_selector, already_texts, skip_texts, submit_text])
        except Exception:
            value = None
        return dict(value) if isinstance(value, Mapping) else {}

    def control_result(state: Mapping[str, Any], clicked: bool) -> Optional[SignResult]:
        return playwright_control_result(state, clicked, submit_text)

    def judge_browser_page(html: str) -> SignResult:
        """有当天控件规则时，只信控件状态或明确的成功提示，不信整页「已签到」。"""
        if not state_selector:
            return classify(html, rule)
        text = page_text(html)
        lower = text.lower()
        if any(str(word).lower() in lower
               for word in (rule.get("success_keywords") or []) if str(word).strip()):
            return SignResult("success", _bonus_message("签到成功", text, rule))
        return SignResult("failed", "未识别到当天签到控件的完成状态；" + page_hint(html))

    def callback(page: Any) -> Dict[str, Any]:
        # 有的站点（猪猪）必须把 Cookie 写进浏览器存储：验证过程中新下发的
        # cf_clearance / sl_* 才能随后续请求发出，否则验证会一直循环。
        if cookie_mode == "browser" and cookie:
            try:
                items = cookie_list_for_context(cookie, base_url)
                if items:
                    page.context.add_cookies(items)
            except Exception:
                pass
        # MoviePilot 的 action() 里是 page.goto(url) 等 load：Cloudflare/雷池盾页永远等不到
        # load，30 秒必超时（日志里的 Page.goto: Timeout 30000ms 就是这个）。所以让它先打开
        # about:blank，真正的导航放在这里，只等 domcontentloaded，再自己轮询盾页是否放行。
        tried: List[str] = []
        last: Dict[str, Any] = {}
        for path in paths:
            url = target_url(base_url, path)
            tried.append(path)
            page_ready: Dict[str, Any] = {}
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=max(15000, int(timeout * 1000)))
            except Exception:
                pass
            try:
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            before = _wait_challenge_clear(page, challenge_budget)

            # 猪猪的鉴权在页面 JS + localStorage 中完成。登录页、锁定页和日历容器都
            # 由前端异步渲染；先等页面进入可处理状态，再读取鉴权控件。
            if before and _page_has_challenge(before):
                last = {"before": before, "after": before, "clicked": False,
                        "path": path, "stuck": "challenge"}
                continue
            if wait_for_ready:
                page_ready = _wait_playwright_page_ready(page, rule, ready_wait_ms)
                if not page_ready.get("ready"):
                    last = {"before": before, "after": before, "clicked": False,
                            "path": path, "stuck": "page_ready", "ready": page_ready,
                            "result": SignResult("failed", _page_ready_timeout_message(page_ready))}
                    break
                try:
                    before = page.content() or before
                except Exception:
                    pass

            # 猪猪的鉴权在页面 JS + localStorage 中完成。先在同一页面完成登录/解锁，
            # 再重新打开签到页；不自行请求 /v1 API，避免缺少前端签名头而得到 401。
            if browser_auth_mode:
                auth_result, auth_changed = _prepare_browser_auth(
                    page, site, rule, timeout=timeout)
                if auth_result.status != "success":
                    try:
                        after = page.content() or before
                    except Exception:
                        after = before
                    return {"before": before, "after": after, "clicked": False,
                            "path": path, "stuck": "browser_auth",
                            "message": auth_result.message, "tried": tried, "paths": paths}
                if auth_changed:
                    try:
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=max(15000, int(timeout * 1000)))
                        page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    before = _wait_challenge_clear(page, challenge_budget)
                    if before and _page_has_challenge(before):
                        last = {"before": before, "after": before, "clicked": False,
                                "path": path, "stuck": "challenge"}
                        continue
                    if wait_for_ready:
                        page_ready = _wait_playwright_page_ready(page, rule, ready_wait_ms)
                        if not page_ready.get("ready"):
                            last = {"before": before, "after": before, "clicked": False,
                                    "path": path, "stuck": "page_ready", "ready": page_ready,
                                    "result": SignResult(
                                        "failed", _page_ready_timeout_message(page_ready))}
                            break
                        try:
                            before = page.content() or before
                        except Exception:
                            pass
            clicked = False
            stuck = ""
            if before and _page_has_challenge(before):
                # 还停在雷池 / Cloudflare 检测页：这时候页面上根本没有签到按钮，
                # 直接去点只会得到「没找到按钮」的误导性结论。
                stuck = "challenge"
                last = {"before": before, "after": before, "clicked": False,
                        "path": path, "stuck": stuck}
                continue

            state_before = control_state(page)
            state_before_result = control_result(state_before, False)
            if state_before_result is not None:
                after = before
                try:
                    after = page.content() or before
                except Exception:
                    pass
                last = {"before": before, "after": after, "clicked": False,
                        "path": path, "stuck": "control_state", "ready": page_ready,
                        "state": state_before, "result": state_before_result}
                if state_before_result.status in {"success", "already"}:
                    break
                if state_before_result.message:
                    return last

            judged_before = (judge_browser_page(before)
                             if not state_before_result or not state_before.get("found")
                             else SignResult("failed", ""))
            if judged_before.status == "failed" and "二级验证" in str(judged_before.message or ""):
                # 异地登录验证页：需要人工/2FA，点按钮没用
                return {"before": before, "after": before, "clicked": False,
                        "path": path, "stuck": "second_factor",
                        "message": judged_before.message}
            loop_state: Dict[str, Any] = {}
            loop_result: Optional[SignResult] = None
            last_captcha_failure: Optional[SignResult] = None
            if judged_before.status not in {"success", "already"}:
                after = before
                # 某些站点点击后先跳转再出按钮，保留两轮点击机会；排除文本确保
                # 「已签到 / 补签」永远不会被当成可点的「签到」。猪猪若首次返回 422
                # 人机验证，只有页面自己写入 Turnstile token 后才允许再点一次。
                for _attempt in range(2):
                    try:
                        marked = bool(page.evaluate(
                            _MARK_CLICK_JS, [selector, submit_text, excluded_texts]))
                    except Exception:
                        marked = False
                    if not marked:
                        break

                    captcha_failures: List[SignResult] = []
                    response_listener: Any = None

                    def observe_captcha_response(response: Any) -> None:
                        result = playwright_captcha_response_result(
                            captcha_response_path, response)
                        if result is not None:
                            captcha_failures.append(result)

                    if captcha_response_path:
                        try:
                            on_response = getattr(page, "on", None)
                            if callable(on_response):
                                on_response("response", observe_captcha_response)
                                response_listener = observe_captcha_response
                        except Exception:
                            response_listener = None
                    try:
                        try:
                            page.click(_CLICK_MARKED_SELECTOR, timeout=10000)
                            clicked = True
                        except Exception:
                            # 真实点击失败（遮罩/动画挡住）时退回 JS 点击
                            try:
                                clicked = bool(page.evaluate(
                                    _PAGE_CLICK_JS, [selector, submit_text, excluded_texts]))
                            except Exception:
                                clicked = False
                        try:
                            page.wait_for_timeout(max(1000, wait_ms))
                        except Exception:
                            pass
                    finally:
                        if response_listener is not None:
                            for method_name in ("remove_listener", "off"):
                                remover = getattr(page, method_name, None)
                                if not callable(remover):
                                    continue
                                try:
                                    remover("response", response_listener)
                                    break
                                except Exception:
                                    continue

                    captcha_failure = captcha_failures[0] if captcha_failures else None
                    if captcha_failure is not None:
                        last_captcha_failure = captcha_failure
                        token_ready = False
                        if _attempt == 0:
                            token_ready = _wait_playwright_captcha_token(
                                page, captcha_token_selector, captcha_wait_ms)
                        if token_ready:
                            # 验证 token 已由页面 JS 生成：仅重试这一次。
                            continue
                        message = captcha_failure.message
                        if _attempt > 0:
                            message += "；页面已生成验证 token，仅重试一次后仍未确认签到成功"
                        return {"before": before, "after": after, "clicked": clicked,
                                "path": path, "stuck": "captcha", "result": captcha_failure,
                                "message": message, "tried": tried, "paths": paths}

                    # 点完签到可能被踢到雷池 / Cloudflare 检测页，再等一次放行
                    after = _wait_challenge_clear(page, challenge_budget)
                    if browser_auth_mode:
                        auth_result, auth_changed = _prepare_browser_auth(
                            page, site, rule, timeout=timeout)
                        if auth_result.status != "success":
                            return {"before": before, "after": after, "clicked": clicked,
                                    "path": path, "stuck": "browser_auth",
                                    "message": auth_result.message, "tried": tried,
                                    "paths": paths}
                        if auth_changed:
                            try:
                                page.goto(url, wait_until="domcontentloaded",
                                          timeout=max(15000, int(timeout * 1000)))
                                page.wait_for_load_state("domcontentloaded")
                            except Exception:
                                pass
                            after = _wait_challenge_clear(page, challenge_budget)
                    if after and _page_has_challenge(after):
                        stuck = "challenge"
                        break
                    if wait_for_ready:
                        page_ready = _wait_playwright_page_ready(page, rule, ready_wait_ms)
                        if not page_ready.get("ready"):
                            loop_result = SignResult(
                                "failed", _page_ready_timeout_message(page_ready))
                            stuck = "page_ready"
                            break
                        try:
                            after = page.content() or after
                        except Exception:
                            pass
                    state_after = control_state(page)
                    state_after_result = control_result(state_after, clicked)
                    if state_after_result is not None:
                        loop_state = state_after
                        loop_result = state_after_result
                        if state_after_result.status in {"success", "already"}:
                            break
                        # 人机验证后的第二次提交已是最后一次；控件仍不可用时
                        # 不再触发第三次请求。
                        if last_captcha_failure is not None:
                            break
                    if not state_selector and classify(after, rule).status in {"success", "already"}:
                        break
                    if after and _page_has_challenge(after):
                        stuck = "challenge"
                        break
            else:
                after = before
                try:
                    after = page.content() or after
                except Exception:
                    pass
            last = {"before": before, "after": after, "clicked": clicked,
                    "path": path, "ready": page_ready}
            if loop_state:
                last["state"] = loop_state
            if loop_result is not None:
                last["result"] = loop_result
            if (last_captcha_failure is not None
                    and (loop_result is None
                         or loop_result.status not in {"success", "already"})):
                last["result"] = SignResult(
                    "failed",
                    f"{last_captcha_failure.message}；页面已生成验证 token，"
                    "仅重试一次后仍未确认签到成功",
                    challenge="captcha")
                last["stuck"] = "captcha"
            if "result" not in last:
                judged_after = judge_browser_page(after)
                if judged_after.status in {"success", "already"}:
                    last["result"] = judged_after
            if clicked or (last.get("result") and last["result"].status in {"success", "already"}):
                break
            # 这一页既没点成也不像已签到：换下一个备用页面
        last["tried"] = tried
        last["paths"] = paths
        return last

    try:
        outcome = PlaywrightHelper().action(url="about:blank", callback=callback,
                                            cookies=None if cookie_mode == "browser" else cookie,
                                            ua=str(site.get("ua") or "").strip() or None,
                                            proxies=dict(proxies) if proxies else None,
                                            headless=True,
                                            timeout=max(60, int(timeout)))
    except Exception as error:
        return SignResult("failed", f"本地浏览器签到异常：{error}")
    if not outcome:
        return SignResult("failed", "本地浏览器没拿到页面（检查 MoviePilot 的浏览器依赖）")

    after = str(outcome.get("after") or "")
    judged = outcome.get("result")
    if not isinstance(judged, SignResult):
        judged = judge_browser_page(after)
    if judged.status in {"success", "already"}:
        prefix = "" if outcome.get("clicked") else "（页面已是签到完成状态）"
        return SignResult(judged.status, f"{judged.message}{prefix}")
    if not outcome.get("clicked"):
        if outcome.get("stuck") == "page_ready":
            return SignResult("failed", str(outcome.get("message")
                                            or (outcome.get("result").message
                                                if isinstance(outcome.get("result"), SignResult)
                                                else "等待签到页加载超时")))
        if outcome.get("stuck") in {"browser_auth", "control_state"}:
            return SignResult("failed", str(outcome.get("message")
                                            or judged.message
                                            or "浏览器内登录或页面解锁未完成"))
        if outcome.get("stuck") == "second_factor":
            return SignResult("failed", str(outcome.get("message")
                                            or "站点要求二级验证（异地登录验证）"))
        tried = "、".join(str(item) for item in (outcome.get("tried") or []))
        ready = outcome.get("ready") if isinstance(outcome.get("ready"), Mapping) else {}
        if (rule.get("playwright_calendar_selector")
                and ready.get("calendarReady") and not ready.get("controlFound")):
            return SignResult(
                "failed",
                str(rule.get("playwright_missing_control_message")
                    or "签到日历已加载，但今日没有签到入口或控件状态异常"),
            )
        if outcome.get("stuck") == "challenge":
            return SignResult("failed",
                              f"本地浏览器仍卡在安全验证页面（已试：{tried or '默认页面'}）："
                              f"检查 Cookie 里是否带 cf_clearance / sl-*，或把 cloudflare_wait 调大")
        return SignResult("failed",
                          f"本地浏览器没找到可点的签到按钮（已试：{tried or '默认页面'}；"
                          f"可在这条规则的 retry_paths 里补页面）")
    if outcome.get("stuck") == "captcha":
        return SignResult("failed", str(outcome.get("message") or judged.message),
                          challenge="captcha")
    if outcome.get("stuck") == "control_state":
        return SignResult("failed", str(outcome.get("message") or judged.message))
    return judged


# ---------------------------------------------------------------- 站点模块
# 规则表达不了的站点（图片验证码、答题选海报、API Token）放在插件目录的 sites/ 下，
# 一个站一个文件。这里按文件路径加载，不依赖包名，测试里单独加载 core.py 也能用。
SITE_MODULE_DIR = Path(__file__).resolve().parent / "sites"
_SITE_MODULES: Optional[List[Any]] = None
_SITES_API: Any = None


def _sites_api() -> Any:
    """加载 sites/__init__.py 里的 SignContext 等辅助（避免相对导入）。"""
    global _SITES_API
    if _SITES_API is None:
        path = SITE_MODULE_DIR / "__init__.py"
        if not path.is_file():
            _SITES_API = False
        else:
            spec = importlib.util.spec_from_file_location("ptsitesignin_sites_api", path)
            module = importlib.util.module_from_spec(spec) if spec else None
            if module is None or spec.loader is None:
                _SITES_API = False
            else:
                spec.loader.exec_module(module)
                _SITES_API = module
    return _SITES_API or None


def load_site_modules(refresh: bool = False) -> List[Any]:
    """扫描 sites/ 目录，返回带 site_url + signin 的模块类（带缓存）。"""
    global _SITE_MODULES
    if _SITE_MODULES is not None and not refresh:
        return _SITE_MODULES
    modules: List[Any] = []
    if SITE_MODULE_DIR.is_dir():
        for path in sorted(SITE_MODULE_DIR.glob("*.py")):
            if path.name.startswith("__"):
                continue
            name = "ptsitesignin_site_" + re.sub(r"\W+", "_", path.stem)
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                continue
            for obj in vars(module).values():
                if isinstance(obj, type) and hasattr(obj, "site_url") and hasattr(obj, "signin"):
                    modules.append(obj)
                    break
    _SITE_MODULES = modules
    return modules


def site_module_for(url: str) -> Optional[Any]:
    """按站点 URL 找匹配的模块类。"""
    api = _sites_api()
    if api is None:
        return None
    for module in load_site_modules():
        matcher = getattr(module, "match", None)
        if callable(matcher):
            try:
                if matcher(url):
                    return module
            except Exception:
                continue
            continue
        if api.domain_matches(url, str(getattr(module, "site_url", "") or "")):
            return module
    return None


def run_site_module(site: Mapping[str, Any], timeout: int = 30,
                    proxies: Optional[Dict[str, str]] = None,
                    logger: Any = None,
                    rule: Optional[Mapping[str, Any]] = None) -> Optional[SignResult]:
    """有匹配的站点模块就执行它；API Key 规则始终跳过模块。"""
    if rule is not None and is_api_key_rule(rule):
        return None
    api = _sites_api()
    if api is None:
        return None
    module = site_module_for(str(site.get("url") or ""))
    if module is None:
        return None
    if not str(site.get("cookie") or "").strip() and not str(site.get("token") or "").strip():
        return SignResult("failed", "站点没有 Cookie，无法签到")
    context = api.SignContext(site, timeout=timeout, proxies=proxies, logger=logger)
    ok, message = api.sign_via_module(module, site, context)
    if ok:
        return SignResult("already" if "已签到" in message else "success", message or "签到成功")
    return SignResult("failed", message or "签到失败")


def http_sign_auto(site: Mapping[str, Any], rule: Mapping[str, Any], timeout: int = 30,
                   proxies: Optional[Dict[str, str]] = None) -> SignResult:
    """HTTP 签到：默认路径 404 / 405 时自动换签到入口，并把找法写进失败原因。

    站点用的签到路径五花八门（`/attendance.php`、`/signin.php`、自定义路由……）。以前路径写死，
    碰上没有 `/attendance.php` 的站点只会得到一句「请求失败（HTTP 404）」。现在的顺序是：
    先试规则里的 path → 从站点首页「签到」链接自动发现 → 再试几个常见路径。
    """
    if is_api_key_rule(rule):
        return http_sign(site, rule, timeout=timeout, proxies=proxies)
    result = http_sign(site, rule, timeout=timeout, proxies=proxies)
    if str(rule.get("form_mode") or "").lower() == "quiz":
        # 彩虹岛答题只允许一条确定路径：POST 后 GET 回读确认。失败时不能
        # 再走二次提交、首页猜链接或 Browserless，否则会在答题结果未知时重复签到。
        return result
    if result.status in {"success", "already"}:
        return result
    # 页面能打开但没判定出结果：很多站点签到是「点表单按钮」触发的，
    # 这里按页面上的表单 POST 一次，再回读签到页确认，避免「签上了却报失败」。
    if not result.missing_path and not result.challenge:
        # 有的站点签到在第二个地址上（TTG 的 signed.php、学校的 addbonus），先按规则走完
        two_step = http_two_step(site, rule, timeout=timeout, proxies=proxies)
        if two_step is not None:
            return two_step
        # 第二段不认输时回读第一页再判一次：学校 addbonus 后首页入口消失、海胆之家首页变「已经打卡」
        if str(rule.get("signin_path") or "").strip():
            time.sleep(1.5)
            confirmed = http_confirm(site, rule, timeout=timeout, proxies=proxies)
            if confirmed is not None and confirmed.status in {"success", "already"}:
                return SignResult(confirmed.status, f"{confirmed.message}（回读签到页确认）")
        posted = http_sign_form(site, rule, timeout=timeout, proxies=proxies)
        if posted is not None and posted.status in {"success", "already"}:
            return SignResult(posted.status, f"{posted.message}（按页面表单 POST 提交）")
        confirmed = http_confirm(site, rule, timeout=timeout, proxies=proxies)
        if confirmed is not None and confirmed.status in {"success", "already"}:
            return SignResult(confirmed.status, f"{confirmed.message}（回读签到页确认）")
        if posted is not None and posted.status == "failed":
            return posted
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
    if re.search(WAF_PATTERN, text, re.IGNORECASE):
        return SignResult("failed", "站点返回雷池 WAF 安全检测页", challenge="cloudflare")
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
    # 规则里写了关键词时（浏览器引擎用的那套），HTTP 判定也认，免得同一个预设两种引擎结论不同
    lower_visible = visible.lower()
    if any(str(word).lower() in lower_visible for word in (rule.get("success_keywords") or [])):
        bonus = extract_bonus(text, rule)
        return SignResult("success", f"签到成功（{bonus}）" if bonus else "签到成功")
    if any(str(word).lower() in lower_visible for word in (rule.get("already_keywords") or [])):
        bonus = extract_bonus(text, rule)
        return SignResult("already", f"今日已签到（{bonus}）" if bonus else "今日已签到")
    already_html = str(rule.get("already_html") or "")
    if already_html and re.search(already_html, text, re.IGNORECASE | re.DOTALL):
        return SignResult("already", "今日已签到")
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
    control = control_text(text)
    if absent and not login_page(content) and not cloudflare_pending(content) \
            and not any(word in visible or word in control for word in absent):
        status = "success" if str(rule.get("absent_status") or "").lower() == "success" else "already"
        prefix = "签到成功" if status == "success" else "今日已签到"
        bonus = extract_reward_sentence(text)
        return SignResult(status, f"{prefix}（{bonus}）" if bonus else prefix)
    # 兜底：主正则没命中，但页面出现了「已领取 / 本次签到 / 签到获得」这类签到后的状态字样，
    # 且页面上已经没有「立即签到」之类的按钮，就按已签到处理，别让正常签到报失败。
    if not login_page(content) and not cloudflare_pending(content) \
            and not re.search(SIGNIN_ACTION_PATTERN, visible):
        for match in re.finditer(STATE_HINT_PATTERN, visible, re.IGNORECASE):
            window = visible[max(0, match.start() - 50):match.start()]
            if re.search(RULE_CONTEXT_PATTERN, window):
                continue
            bonus = extract_reward_sentence(text) or extract_bonus(text, rule)
            note = f"页面出现「{match.group(1)}」"
            return SignResult("already",
                              f"今日已签到（{note}，{bonus}）" if bonus else f"今日已签到（{note}）")
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
# 用户页里「最近动向」相关关键词（含繁体、各站的不同叫法）
ACTIVITY_KEYWORDS = ("最近动向", "最近動向", "最新动向", "最新動向", "最近活动", "最近活動",
                     "最近访问", "最近訪問", "上次访问", "上次訪問", "本次访问", "上次签到",
                     "上次簽到", "上次活动", "上次活動", "上次登入", "上次登录", "上次造访",
                     "上次造訪", "最后访问", "最後訪問", "最后活动", "最後活動", "最后上线",
                     "最後上線", "最后活跃", "最後活躍", "最后造访", "最後造訪", "最近登入",
                     "最近登錄", "最近上线", "最近上線", "最近活跃", "最近活躍", "最后在线",
                     "最後在線")
# 兜底判定：主正则都没命中时，再看一遍页面里有没有「签到后的状态」字样。
# 很多站点用「已领取 / 本次签到 / 签到获得 / 已签」这类写法，或者把文案放在说明里，
# 所以下面会排除「规则 / 说明 / 首次 / 若 / 如果」附近的命中，避免把规则当成结果。
STATE_HINT_PATTERN = (r"(已签到|已簽到|今日已签|今天已签|已领取|已領取|领取成功|領取成功|"
                      r"签到成功|簽到成功|成功签到|签到完成|本次签到|签到获得|已签到过|"
                      r"不要重复签到|无需重复签到|already|signed in)")
RULE_CONTEXT_PATTERN = r"(规则|規則|說明|说明|首次|连续签到第|若|如果|条件|條件|奖励说明|奖励规则)"
# 页面上还留着「立即签到」这类按钮，说明这次没签上，不能按已签到处理
SIGNIN_ACTION_PATTERN = r"(立即签到|点击签到|點擊簽到|去签到|前往签到|马上签到|开始签到)"

# 站点常见的时间格式（NexusPHP 多为 YYYY-MM-DD HH:MM:SS）
DATETIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)"
# 没有关键词命中时用的严格时间格式：必须带时分。裸日期（2026-09-18）在种子列表、
# 公告、排行榜里到处都是，拿它当「最近动向」只会给出一个看起来很旧的假值。
ACTIVITY_TIME_PATTERN = r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)"
# 关键词后面允许多长的间隔才算「紧跟着时间」：多数站点写成「上次访问：2026-…」，
# 但有的主题把标签和值分在两行、中间夹着图标 / 说明文字，60 字会漏掉，放宽到 160。
# 多个关键词都命中时只取「离时间最近」的那个，所以放宽窗口不会把旁边字段（加入日期之类）
# 的时间算成这个关键词的值。
ACTIVITY_WINDOW = 160
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
REWARD_SENTENCE_PATTERN = (rf"((?:{BONUS_VERBS}|签到已得|簽到已得)\s*{BONUS_AMOUNT}"
                           rf"\s*(?:{BONUS_COUNTERS})?\s*(?:{BONUS_UNITS}))")


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


def http_check_api_key(site: Mapping[str, Any], rule: Mapping[str, Any],
                       timeout: int = 30,
                       proxies: Optional[Dict[str, str]] = None) -> LoginResult:
    """用显式 credential_check_path 验证 API Key；不会访问任何猜测路径。"""
    if not is_api_key_rule(rule):
        return LoginResult("failed", "站点规则未声明 auth=api_key，无法用 API Key 验证登录")
    path = str(rule.get("credential_check_path") or "").strip()
    if not path:
        return LoginResult("failed", "API Key 规则未配置 credential_check_path，无法验证登录")
    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return LoginResult("failed", "站点地址为空")
    secret = ""
    content = ""
    status_code = 0
    try:
        secret = _api_key_value(site, rule)
        path = _validate_site_path(path, "credential_check_path ")
        method = str(rule.get("credential_check_method") or "GET").upper()
        if method not in {"GET", "POST"}:
            return LoginResult("failed", "API Key 验证 method 只能是 GET 或 POST")
        if str(rule.get("engine") or "http").strip().lower() == "playwright":
            response = playwright_api_fetch(
                site, rule, path, method=method, timeout=timeout, proxies=proxies,
                include_rule_body=False)
            if response is None:
                return LoginResult("failed", "当前 MoviePilot 环境没有可用的本机 Playwright")
            if response.get("second_factor"):
                return LoginResult("failed", "本地浏览器仍要求异地登录 / 二级验证")
            if response.get("challenge_page"):
                return LoginResult("failed", "本地浏览器仍卡在 Cloudflare / WAF 验证页")
            if response.get("error"):
                return LoginResult("failed", _redact_api_secret(response["error"], secret))
            status_code = int(response.get("status") or 0)
            content = str(response.get("text") or "")
        else:
            url = target_url(base_url, path)
            headers = _api_headers(site, rule)
            session = new_http_session()
            session.headers.update(headers)
            response = _api_request(session, method, url, headers, timeout, proxies)
            status_code = int(getattr(response, "status_code", 0) or 0)
            content = str(getattr(response, "text", "") or "")
    except (ApiRequestError, ValueError) as error:
        return LoginResult("failed", _redact_api_secret(error, secret))
    except Exception as error:
        return LoginResult("failed", _redact_api_secret(f"API Key 验证请求异常：{error}", secret))
    if cloudflare_pending(content):
        return LoginResult("failed", "站点返回 Cloudflare 验证页，API Key 请求未到达应用层")
    if status_code in {401, 403}:
        return LoginResult("failed", "API Key 无效或没有权限")
    if status_code >= 400:
        return LoginResult("failed", f"API Key 验证失败（HTTP {status_code}）")
    expected = str(rule.get("credential_check_success") or "").strip()
    if expected:
        try:
            matched = bool(re.search(expected, content, re.IGNORECASE))
        except re.error:
            matched = expected in content
        if not matched:
            return LoginResult("failed", "API Key 验证失败：响应未匹配 credential_check_success")
    return LoginResult("success", "API Key 验证成功")


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
    """把会话 Cookie 拼成请求头字符串。"""
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
    login_path = str(account.get("login_path") or DEFAULT_LOGIN_PATH)
    login_action = str(account.get("login_action") or DEFAULT_LOGIN_ACTION)
    username_field = str(account.get("username_field") or DEFAULT_USERNAME_FIELD)
    password_field = str(account.get("password_field") or DEFAULT_PASSWORD_FIELD)
    headers = {"User-Agent": str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    session = new_http_session()
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


def is_zero_time(value: str) -> bool:
    """判断时间是不是被站点补成整零点（``2026-05-06 00:00:00``）。

    这种值多半是「只有日期」的字段（注册日期、加入日期、模板默认值）被补了零，
    不是真的最近动向；皇后那条假时间就是它。
    """
    moments = str(value or "").strip()
    return moments.endswith("00:00:00") or moments.endswith("00:00")


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
    2. 没有关键词命中就直接返回空，宁可页面提示「未读取到」（可以配 activity_path），
       也不把公告 / 活动通知里的日期当成最近动向；整零点时间同样不算——那是被补零的
       日期字段（注册日期、模板默认值）。
    """
    text = plain_text(html or "")
    best: Any = None
    for order, keyword in enumerate(ACTIVITY_KEYWORDS):
        # 同一个关键词可能出现在多处（导航、图例、说明里也会有），逐处检查
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            start = index + 1
            match = _DATETIME_RE.search(text, index, index + ACTIVITY_WINDOW)
            if match is None:
                continue
            # 站点自己写的「(40分钟前)」最可信：新闻、公告里的裸日期没有这个后缀
            has_suffix = 0 if relative_suffix(text, match.end()) else 1
            # 被补成 00:00:00 的日期不是真实动向（皇后把注册日期补零当成了最近动向），
            # 这种命中直接跳过，宁可这页返回空，交给下一个候选页
            if is_zero_time(match.group(1)):
                continue
            gap = match.start() - (index + len(keyword))
            candidate = (has_suffix, gap, order, index, keyword, match)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
    if best is not None:
        _, _, _, index, keyword, match = best
        snippet = text[max(0, index - 12):index + 140].strip()
        return {"keyword": keyword, "snippet": snippet,
                "last_seen": activity_value(text, match)}
    # 没有关键词命中时**不再退化成「全页第一个时间」**：站点首页的公告 / 活動通知里到处是
    # 日期——皇后的 index.php 就有「北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59
    # 开启全站Free」，被当成最近动向显示成了 2026-05-06 00:00:00。宁可返回空，让调用方
    # 换下一个候选页（userdetails.php 上有真正的「最近動向」）。
    return {}


def _uid_from_jwt(value: str) -> str:
    """从 JWT 或 `base64(payload.签名)` 这类凭据里取 user_id（烧包的 c_secure_pass 是后者）。"""
    token = str(value or '').strip()
    if not token:
        return ''
    candidates = [token.split('.')[0], token]
    for candidate in candidates:
        candidate = candidate.strip()
        if len(candidate) < 8:
            continue
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(candidate + '=' * (-len(candidate) % 4)).decode('utf-8', 'ignore')
            except Exception:
                continue
            match = re.search(r'"?user_?id"?\s*[:=]\s*"?(\d+)', decoded, re.IGNORECASE)
            if match:
                return match.group(1)
            if decoded.strip().isdigit():
                return decoded.strip()
    return ''


def cookie_uid(cookie: str) -> str:
    """从 Cookie 里解出自己的用户 ID（NexusPHP 惯例：``c_secure_uid`` 是 base64 过的 uid）。

    自定义主题的站点首页常常不挂 ``userdetails.php?id=`` 链接，这时插件拿不到 uid、也就打不开
    用户页（「最近动向」自然读不到）——所以优先用 Cookie 里这份 uid。
    Cookie 名不全统一，``c_secure_uid`` 之外还认 ``uid`` / ``c_uid`` / ``user_id`` 等；
    值可能是 base64（带或不带 ``=``、可能被 URL 编码一到两次），也可能直接是数字。
    """
    text = str(cookie or "")
    for name in ("c_secure_uid", "uid", "c_uid", "userid", "user_id", "c_secure_pass"):
        match = re.search(rf"(?:^|;\s*){name}=([^;]+)", text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        # 可能被 URL 编码一到两次（%3D / %253D）
        for _ in range(3):
            unquoted = urllib.parse.unquote(value)
            if unquoted == value:
                break
            value = unquoted
        if value.isdigit():
            return value
        # 烧包的 c_secure_pass 是 "base64(payload).签名" 的 JWT 变体，payload 里有 user_id
        jwt_uid = _uid_from_jwt(value)
        if jwt_uid:
            return jwt_uid
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(value + "=" * (-len(value) % 4)).decode("utf-8", "ignore")
            except Exception:
                continue
            decoded = decoded.strip()
            if decoded.isdigit():
                return decoded
            jwt_uid = _uid_from_jwt(decoded)
            if jwt_uid:
                return jwt_uid
    return ""


def _activity_from_api_payload(content: str) -> Dict[str, str]:
    found = extract_activity(content)
    if found:
        return found
    try:
        payload = json.loads(str(content or "").strip())
    except (TypeError, ValueError):
        return {}
    candidates: List[Mapping[str, Any]] = []
    def collect(value: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, Mapping):
            candidates.append(value)
            for nested in value.values():
                collect(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value[:10]:
                collect(nested, depth + 1)
    collect(payload)
    keys = ("last_seen", "last_active", "last_activity", "last_visit", "activity",
            "最近动向", "最近動向", "上次访问", "上次訪問")
    for item in candidates:
        for key in keys:
            if key not in item:
                continue
            value = str(item.get(key) or "").strip()
            if value:
                return {"keyword": key, "snippet": value, "last_seen": value}
    return {}


def read_activity(site: Mapping[str, Any], cookie: str, timeout: int = 30,
                  proxies: Optional[Dict[str, str]] = None,
                  path: Optional[str] = None,
                  paths: Optional[Sequence[str]] = None,
                  rule: Optional[Mapping[str, Any]] = None) -> ActivityResult:
    """读取最近动向；API Key 规则只访问显式 activity_path，不访问首页。"""
    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return ActivityResult("failed", "站点地址为空")
    api_rule = rule or {}
    if is_api_key_rule(api_rule):
        activity_path = str(api_rule.get("activity_path") or "").strip()
        if not activity_path:
            return ActivityResult("failed", "API Key 规则未配置 activity_path，未读取最近动向")
        secret = ""
        try:
            secret = _api_key_value(site, api_rule)
            activity_path = _validate_site_path(activity_path, "activity_path ")
            url = target_url(base_url, activity_path)
            headers = _api_headers(site, api_rule)
            session = new_http_session()
            session.headers.update(headers)
            response = _api_request(session, "GET", url, headers, timeout, proxies)
        except (ApiRequestError, ValueError) as error:
            return ActivityResult("failed", _redact_api_secret(error, secret))
        except Exception as error:
            return ActivityResult("failed",
                                  _redact_api_secret(f"读取 API 最近动向异常：{error}", secret))
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            return ActivityResult("failed", f"读取 API 最近动向失败（HTTP {status_code}）")
        found = _activity_from_api_payload(str(getattr(response, "text", "") or ""))
        if found:
            return ActivityResult("success", f"已读取最近动向（{activity_path}）",
                                  last_seen=found.get("last_seen", ""),
                                  snippet=found.get("snippet", ""),
                                  source=activity_path)
        return ActivityResult("failed",
                              _redact_api_secret(
                                  f"API 响应里没有「最近动向」字段（{activity_path}）", secret))
    cookie = str(cookie or "").strip()
    if not cookie:
        return ActivityResult("failed", "站点地址或 Cookie 为空")
    session = new_http_session()
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
    rule_activity_path = str(api_rule.get("activity_path") or "").strip()
    if rule_activity_path:
        candidates.append(rule_activity_path)
    for extra in paths or ():
        value = str(extra or "").strip()
        if value:
            candidates.append(value)
    match = _UID_RE.search(index_html)
    # 优先用 Cookie 里的 c_secure_uid：那才是「你自己」；首页里的 userdetails.php?id= 链接
    # 常常指向**别人**（论坛帖作者、最新注册用户、版主），拿它去开资料页会读到别人的「最近动向」
    # （皇后那条 2026-05-06 00:00:00 就是这么来的）。Cookie 里解不出 uid 时才退回首页链接。
    own_uid = cookie_uid(cookie)
    uid = own_uid or (match.group(1) if match else "")
    if uid:
        candidates.append(f"{DEFAULT_USERDETAILS_PATH}?id={uid}")
    # NexusPHP 的 userdetails.php 不带 id 时显示当前登录用户自己的资料——uid 解不出来
    # （自定义主题、Cookie 里没有 c_secure_uid）时靠这条兜底
    candidates.append(DEFAULT_USERDETAILS_PATH)
    candidates.append(DEFAULT_INDEX_PATH)
    seen: set = set()
    notes: List[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            page = session.get(target_url(base_url, candidate),
                               timeout=timeout, allow_redirects=True, proxies=proxies)
        except Exception as error:
            notes.append(f"{candidate}：请求失败（{type(error).__name__}）")
            continue
        body = page.text or ""
        if page.status_code >= 400:
            notes.append(f"{candidate}：HTTP {page.status_code}")
            continue
        if cloudflare_pending(body):
            # 撞盾时页面正文是挑战页，从中提取的「最近动向」是假的：直接换下一个候选页
            notes.append(f"{candidate}：Cloudflare 挑战页")
            continue
        if login_page(body):
            notes.append(f"{candidate}：被要求登录（Cookie 可能失效）")
            continue
        found = extract_activity(body)
        if found:
            return ActivityResult("success", f"已读取最近动向（{candidate}）",
                                  last_seen=found.get("last_seen", ""),
                                  snippet=found.get("snippet", ""),
                                  source=str(candidate))
        notes.append(f"{candidate}：{len(plain_text(body))} 字，没有「最近动向」")
    origin = (f"自己的 uid={own_uid}（取自 Cookie）" if own_uid
              else f"uid={uid}（取自首页链接）" if uid
              else "Cookie 里没解析到 uid 也没有首页链接")
    return ActivityResult("failed",
                          f"未在站点页面找到「最近动向」，{origin}（" + "；".join(notes) + "）")


def playwright_read_activity(site: Mapping[str, Any], rule: Mapping[str, Any],
                             timeout: int = 90,
                             proxies: Optional[Dict[str, str]] = None) -> Optional[ActivityResult]:
    """在本机浏览器里读取站点「最近动向」。

    部分 SPA（例如猪猪）的用户资料页必须经过站点自身的前端登录态和
    Cloudflare / WAF 校验，直接 HTTP 请求只会得到 403。这里复用签到引擎的
    浏览器登录流程，打开 activity_path 后从页面正文提取最近动向。
    返回 None 表示当前 MoviePilot 没有可用的 PlaywrightHelper。
    """
    PlaywrightHelper = _load_playwright_helper()
    if PlaywrightHelper is None or not hasattr(PlaywrightHelper, "action"):
        return None

    base_url = str(site.get("url") or "").rstrip("/")
    if not base_url:
        return ActivityResult("failed", "站点地址为空")
    path = str(rule.get("activity_path") or "/userdetails").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
        return ActivityResult("failed", "最近动向路径必须是以 / 开头的站内路径")

    cookie = str(site.get("cookie") or "").strip()
    cookie_mode = str(rule.get("cookie_mode") or "header").lower()
    browser_auth_mode = str(rule.get("browser_auth_mode") or "").strip().lower()
    try:
        challenge_budget = int(float(rule.get("cloudflare_wait") or 20)) * 1000
    except (TypeError, ValueError):
        challenge_budget = 20000
    try:
        activity_wait = int(rule.get("activity_wait") or 8000)
    except (TypeError, ValueError):
        activity_wait = 8000
    activity_wait = max(1000, min(activity_wait, 30000))

    target = target_url(base_url, path)

    def callback(page: Any) -> Dict[str, Any]:
        if cookie_mode == "browser" and cookie:
            try:
                items = cookie_list_for_context(cookie, base_url)
                if items:
                    page.context.add_cookies(items)
            except Exception:
                pass

        try:
            page.goto(target, wait_until="domcontentloaded",
                      timeout=max(15000, int(timeout * 1000)))
        except Exception:
            pass
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        html = _wait_challenge_clear(page, challenge_budget)
        if _page_has_challenge(html):
            return {"status": "failed",
                    "message": "本机浏览器仍卡在 Cloudflare / WAF 验证页，无法读取最近动向"}

        if browser_auth_mode:
            auth_result, auth_changed = _prepare_browser_auth(
                page, site, rule, timeout=timeout)
            if auth_result.status != "success":
                return {"status": "failed", "message": auth_result.message}
            if auth_changed:
                try:
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=max(15000, int(timeout * 1000)))
                    page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                html = _wait_challenge_clear(page, challenge_budget)
                if _page_has_challenge(html):
                    return {"status": "failed",
                            "message": "完成登录后仍卡在 Cloudflare / WAF 验证页，无法读取最近动向"}

        deadline = time.monotonic() + activity_wait / 1000
        last_html = html
        auth_retry = 0
        while time.monotonic() < deadline:
            try:
                current = page.content() or last_html
            except Exception:
                current = last_html
            if current:
                last_html = current
            found = extract_activity(current)
            if found:
                source = str(getattr(page, "url", "") or target)
                return {"status": "success", "activity": found, "source": source}

            # 页面可能在异步渲染后才跳到登录页；再给一次登录机会。
            if browser_auth_mode and auth_retry < 2 and login_page(current):
                auth_result, auth_changed = _prepare_browser_auth(
                    page, site, rule, timeout=timeout)
                if auth_result.status != "success":
                    return {"status": "failed", "message": auth_result.message}
                if auth_changed:
                    auth_retry += 1
                    try:
                        page.goto(target, wait_until="domcontentloaded",
                                  timeout=max(15000, int(timeout * 1000)))
                        page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    _wait_challenge_clear(page, challenge_budget)
                    continue
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        return {"status": "failed",
                "message": f"已打开 {path}，但没有找到最近动向"}

    try:
        outcome = PlaywrightHelper().action(
            url="about:blank", callback=callback,
            cookies=None if cookie_mode == "browser" else cookie,
            ua=str(site.get("ua") or "").strip() or None,
            proxies=dict(proxies) if proxies else None,
            headless=True, timeout=max(60, int(timeout)))
    except Exception:
        return ActivityResult("failed", "本机浏览器读取最近动向出现异常")

    if not isinstance(outcome, Mapping):
        return ActivityResult("failed", "本机浏览器没有返回最近动向页面")
    if outcome.get("status") == "success":
        found = outcome.get("activity") if isinstance(outcome.get("activity"), Mapping) else {}
        source = str(outcome.get("source") or path)
        return ActivityResult("success", f"已读取最近动向（{source}）",
                              last_seen=str(found.get("last_seen") or ""),
                              snippet=str(found.get("snippet") or ""),
                              source=source)
    return ActivityResult("failed", str(outcome.get("message") or "本机浏览器未读取到最近动向"))


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


def is_login_record(row: Mapping[str, Any], strict: bool = False) -> bool:
    """这条记录要不要进「登录状态」。

    strict=True：只认真正的登录记录（纯登录，或签到站点同时也勾了登录站点）——详情页展示用，
    否则配了账号密码的纯签到站点会混进登录表，状态却显示「签到成功」，看着很怪。
    strict=False：还兼容 1.4.0 之前没有 dual 标记的历史记录（只要那一轮做过登录或读过动向就算）。
    """
    if bool(row.get("dual")) or str(row.get("status") or "") in LOGIN_STATUSES:
        return True
    if strict:
        return False
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


def bql_endpoint(address: str, token: str = "") -> str:
    """把配置的地址规范成 BrowserQL 端点。

    支持私有部署：地址写根地址（如 ``http://192.168.1.10:3000``）会自动补
    ``/stealth/bql``；已经以 ``/bql`` 结尾的完整端点原样保留（自建实例常用
    ``/chromium/bql``、``/stealth/bql`` 等路径）。Token 可留空——自建实例常关掉鉴权。
    """
    address, token = str(address or "").strip(), str(token or "").strip()
    if not address:
        raise ValueError("Browserless 地址不能为空")
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Browserless 地址必须以 http:// 或 https:// 开头")
    path = parsed.path.rstrip("/")
    if not path.endswith("/bql"):
        path = (path + "/bql") if path.endswith("/stealth") else (path + "/stealth/bql")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if token:
        query["token"] = token
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "",
                                    urllib.parse.urlencode(query), ""))


def session_endpoint(address: str, token: str) -> str:
    """给 BrowserQL ``reconnect`` 返回的会话地址补上 API token（该地址本身不含 token）。

    只补查询参数、不改路径：会话地址自带 ``/e/<uuid>/...`` 前缀，重写路径会让它失效。
    """
    address, token = str(address or "").strip(), str(token or "").strip()
    if not address:
        return ""
    # 自建 Browserless 没配 token：会话地址原样用，不追加空参数
    if not token:
        return address
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "",
                                    urllib.parse.urlencode(query), ""))


def pre_click_script(selector: str = "", text: str = "", wait_ms: int = 800) -> str:
    """生成「先点一下某个元素」的 JS：用于点头像展开菜单这类两步操作。

    传文字时按「文字最短的匹配元素」点（同 submit 的找法），只传选择器就直接点第一个。
    返回空串表示没配置。
    """
    wanted = str(text or "").strip()
    target = str(selector or "").strip()
    if not wanted and not target:
        return ""
    wanted_json = json.dumps(wanted, ensure_ascii=False)
    selector_json = json.dumps(target, ensure_ascii=False)
    return """(async () => {
      const wanted = %s;
      let node = null;
      if (wanted) {
        const nodes = [...document.querySelectorAll(
          "a, button, input, [role='button'], [role='menuitem'], li, span, div, img")];
        const hits = nodes
          .map((item) => ({
            node: item,
            text: ((item.value || item.textContent || '') + '').replace(/\\s+/g, ' ').trim(),
          }))
          .filter((item) => item.text && item.text.includes(wanted) && item.text.length <= 40);
        if (hits.length) {
          hits.sort((left, right) => left.text.length - right.text.length);
          node = hits[0].node;
        }
      }
      if (!node && %s) node = document.querySelector(%s);
      if (!node) return false;
      const isLink = node.tagName === 'A' && !!node.getAttribute('href');
      node.click();
      // 链接点击会触发导航；立即返回，避免执行上下文被销毁后 evaluate 报错。
      if (isLink) return true;
      await new Promise((resolve) => setTimeout(resolve, %d));
      return true;
    })()""" % (wanted_json, selector_json, selector_json, max(0, int(wait_ms)))


def _submit_script(selector: str, method: str, captcha_input: Optional[str] = None,
                   submit_text: Optional[str] = None, pre_click: str = "",
                   require_text: bool = False) -> str:
    selector_json = json.dumps(selector)
    input_json = json.dumps(captcha_input or "")
    text_json = json.dumps(submit_text or "", ensure_ascii=False)
    require_json = "true" if require_text else "false"
    target_script = """(() => { const wanted = %s.trim();
      if (wanted) {
        // 头像菜单、下拉项这类元素常是 div / li / span，而且默认是隐藏的；
        // 取「文字最短」的那个，避免点到包着一大片内容的外层容器。
        const nodes = [...document.querySelectorAll(
          "input, button, a, [role='button'], [role='menuitem'], [role='link'], li, span, div")];
        const hits = nodes
          .map((node) => ({
            node,
            text: ((node.value || node.textContent || '') + '').replace(/\\s+/g, ' ').trim(),
          }))
          .filter((item) => item.text && item.text.includes(wanted) && item.text.length <= 40);
        if (hits.length) {
          hits.sort((left, right) => left.text.length - right.text.length);
          return hits[0].node;
        }
        if (%s) return null;
      }
      return document.querySelector(%s);
    })()""" % (text_json, require_json, selector_json)
    if method in {"click", "dom_click", "confirm_click"}:
        confirm = "" if method != "confirm_click" else """
          await new Promise((resolve) => setTimeout(resolve, 150));
          const confirm = [...document.querySelectorAll('.layui-layer-btn0, .layui-layer-btn a')]
            .find((node) => node.offsetParent !== null && /确定|确认/.test((node.textContent || '').trim()));
          if (!confirm) return false; confirm.click();"""
        pre = ""
        if pre_click:
            pre = """
          await %s;""" % pre_click
        return """(async () => { const input = %s ? document.querySelector(%s) : null;
          if (input && !input.value.trim()) return false;%s
          const button = %s; if (!button) return false; button.click(); %s return true;
        })()""" % (input_json, input_json, pre, target_script, confirm)
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
    submit = _submit_script(str(rule["submit_selector"]), "dom_click",
                            submit_text=rule.get("submit_text"),
                            require_text=bool(rule.get("require_submit_text")))
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
    pre_action = pre_click_script(str(rule.get("pre_click_selector") or ""),
                                  str(rule.get("pre_click_text") or ""),
                                  int(rule.get("pre_click_wait") or 800))
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
    if pre_action:
        variable_suffix += " $preAction:String!"
    pre_action_step = "preAction:evaluate(content:$preAction){value}\n      " if pre_action else ""
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
      %s
      waitAfter:waitForTimeout(time:$wait){time}
      response:evaluate(content:"JSON.stringify(window.__ptsignin_response || null)"){value}
      %s
      after:html{html}%s
    }""" % (variable_suffix, set_user_agent, pre_action_step,
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
    pre_action = pre_click_script(str(rule.get("pre_click_selector") or ""),
                                  str(rule.get("pre_click_text") or ""),
                                  int(rule.get("pre_click_wait") or 800))
    # 只声明真正用到的变量：BQL 是 GraphQL，多声明一个就会整段查询报错
    variables = ("$cookies:[CookieInput!]! $url:String! $beforeWait:Float! $wait:Float!"
                 " $autoSubmit:String! $autoTimeout:Float!")
    if pre_action:
        variables += " $preAction:String! $preActionWait:Float!"
    steps: List[str] = [
        "cookies(cookies:$cookies){cookies{name}}",
        "goto(url:$url,waitUntil:domContentLoaded){status}",
        "waitBefore:waitForTimeout(time:$beforeWait){time}",
        "before:html{html}",
    ]
    if pre_action:
        steps.append("preAction:evaluate(content:$preAction){value}")
        steps.append("waitForPreAction:waitForTimeout(time:$preActionWait){time}")
    steps.append("submit:evaluate(content:$autoSubmit,timeout:$autoTimeout){value}")
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
    """页面是否仍停留在 Cloudflare / 雷池 WAF 的检测页（自动放行后即不再命中）。"""
    body = html or ""
    if re.search(CF_INTERSTITIAL_PATTERN, body, re.IGNORECASE):
        return True
    return bool(re.search(WAF_PATTERN, body, re.IGNORECASE))


def control_text(html: str) -> str:
    """抽出页面上的「控件文字」：input 的 value、button/a/option 的文本等。

    签到入口大多是 `<input type=submit value="每日签到">` 或 `<a>签到</a>`，
    plain_text 会把标签连同属性一起去掉，直接用原始 HTML 判断又会被脚本 / 隐藏模板里的
    同样字样骗到。这个函数只取真正会显示在按钮上的文字。
    """
    body = str(html or "")
    parts = [match.group(1) for match in re.finditer(
        r"<input\b[^>]*\bvalue\s*=\s*[\"']([^\"']{1,40})[\"']", body, re.IGNORECASE)]
    for pattern in (r"<button\b[^>]*>(.{0,40}?)</button>", r"<a\b[^>]*>(.{0,40}?)</a>",
                    r"<option\b[^>]*>(.{0,40}?)</option>"):
        parts.extend(match.group(1) for match in re.finditer(
            pattern, body, re.IGNORECASE | re.DOTALL))
    return re.sub(r"\s+", " ", plain_text(" ".join(parts)))


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


def browserless_calendar_wait_ms(rule: Mapping[str, Any]) -> int:
    """Browserless 等待猪猪 Vue 日历进入可操作状态的毫秒数（3~60 秒）。"""
    try:
        milli = int(float(rule.get("browserless_calendar_wait") or 20000))
    except (TypeError, ValueError):
        milli = 20000
    return max(3000, min(milli, 60000))


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
        if rule.get("pre_click_selector") or rule.get("pre_click_text"):
            try:
                total += max(0, min(int(rule.get("pre_click_wait") or 800), 15000))
            except (TypeError, ValueError):
                total += 800
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
    if str(rule.get("browserless_fallback_mode") or "").lower() == "piggo":
        # 猪猪专用流程分三次请求：每段都等待日历；登录/解锁两段都要等鉴权；
        # solve 与 token 重试各等待一次点击结果；solve / 提交两段还要算导航等待。
        total += browserless_calendar_wait_ms(rule) * 3
        total += solve_timeout_ms(rule)
        for key, fallback in (("browserless_click_wait", 8000),
                              ("browserless_auth_wait", 12000)):
            try:
                value = int(float(rule.get(key) or fallback))
            except (TypeError, ValueError):
                value = fallback
            total += max(1000, min(value, 60000)) * 2
        total += navigation_wait_ms(rule) * 2
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
    if re.search(SECOND_FACTOR_PATTERN, full_text, re.IGNORECASE):
        return SignResult("failed",
                          "站点要求二级验证（异地登录验证）：请在常用网络下打开站点完成一次验证，"
                          "或换一份完整的 Cookie（含 cf_clearance / 会话）再试")
    if any(word.lower() in lower for word in LOGIN_WORDS) and "logout" not in lower:
        return SignResult("failed", "Cookie 无效或已过期")
    success_pattern = str(rule.get("success") or "")
    if success_pattern and re.search(success_pattern, full_text, re.IGNORECASE):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if any(word.lower() in lower for word in rule.get("success_keywords", [])):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if any(word.lower() in lower for word in rule.get("already_keywords", [])):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    already_html = str(rule.get("already_html") or "")
    if already_html and re.search(already_html, html, re.IGNORECASE | re.DOTALL):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    # 静默成功的站点（学校这种）：入口消失即视为完成，浏览器引擎也要认
    absent = [str(item).strip() for item in (rule.get("absent_keywords") or [])
              if str(item).strip()]
    # 按钮文字常写在 value="..." 里，正文看不到；但脚本 / 隐藏模板里的字样不能算数，
    # 所以用「可见文字 + 控件文字」判断入口还在不在
    if absent and not any(word in full_text or word in control_text(html) for word in absent):
        status = "success" if str(rule.get("absent_status") or "").lower() == "success" else "already"
        label = "签到成功" if status == "success" else "今日已经签到"
        return SignResult(status, _bonus_message(label, full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in SUCCESS_WORDS):
        return SignResult("success", _bonus_message("签到成功", full_text, rule))
    if not rule.get("strict_status_keywords") and any(word in lower for word in ALREADY_WORDS):
        return SignResult("already", _bonus_message("今日已经签到", full_text, rule))
    return SignResult("failed", "未识别到签到成功结果；" + page_hint(html))


def judge_after_click(rule: Mapping[str, Any], submitted: Any,
                      after_html: str) -> Optional[SignResult]:
    """按钮点击式的站点（憨憨 hhanclub 这种）：点完页面没有任何提示，只有按钮变成「已领取」。

    开 `success_on_click` 时，只要真的点到了签到按钮就算签到成功；没点到（按钮早就是
    「已领取」）返回 None，交给后面的文案判定走「今日已签到」。
    """
    if not rule.get("success_on_click"):
        return None
    if submitted in (None, False, "false"):
        return None
    return SignResult("success", _bonus_message("签到成功", page_text(after_html), rule))


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


# 额度 / 鉴权类失败：换下一个 Token 再试，其它失败（站点本身的问题）不轮换
_TOKEN_SWITCH_HINTS = ("鉴权失败", "额度不足", "HTTP 401", "HTTP 403", "HTTP 429")


def _needs_token_switch(result: SignResult) -> bool:
    """判断这次失败是不是「换 Token 就能解决」的那种。"""
    if result.status != "failed":
        return False
    message = str(result.message or "")
    return any(hint in message for hint in _TOKEN_SWITCH_HINTS)


class BrowserlessPool:
    """多个 Browserless Token 轮换使用：前一个额度用尽或鉴权失败时自动换下一个。

    只暴露 ``sign()``：调用方把池子当成单个 signer 用，内部记住上次成功的序号，
    下一轮从它开始，避免每次都从第一个（已耗尽）重试。
    """

    def __init__(self, address: str, tokens: Sequence[str], timeout: int = 100,
                 logger: Any = None, max_concurrency: int = 2):
        cleaned: List[str] = []
        for token in tokens:
            value = str(token or "").strip()
            if value and value not in cleaned:
                cleaned.append(value)
        # 自建 Browserless 常常不开鉴权：只给了地址、没给 Token 时也要能建出实例
        if not cleaned and str(address or "").strip():
            cleaned = [""]
        self.tokens = cleaned
        self._logger = logger
        self._index = 0
        self._next_index = 0
        self._index_lock = Lock()
        self._token_concurrency = max(1, int(max_concurrency))
        self._request_slots = [
            BoundedSemaphore(self._token_concurrency) for _ in cleaned
        ]
        self._signers = [(token, BrowserlessSigner(address, token, timeout))
                         for token in cleaned]

    def __bool__(self) -> bool:
        return bool(self._signers)

    def __len__(self) -> int:
        return len(self._signers)

    @property
    def active_index(self) -> int:
        """当前正在用的 Token 序号（从 1 开始，给日志用）。"""
        with self._index_lock:
            return self._index + 1

    def _log(self, message: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.info(message, *args)

    @property
    def max_concurrency(self) -> int:
        """所有 Token 合计可用的浏览器并发数。"""
        return len(self._request_slots) * self._token_concurrency

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        """按 Token 轮转并在各自并发槽内执行签到。"""
        if not self._signers:
            return SignResult("failed", "没有可用的 Browserless Token")
        with self._index_lock:
            start_index = self._next_index
            self._next_index = (self._next_index + 1) % len(self._signers)
        return self._sign_rotating(site, rule, start_index)

    def _sign_rotating(self, site: Mapping[str, Any], rule: Mapping[str, Any],
                       start_index: int) -> SignResult:
        total = len(self._signers)
        last: Optional[SignResult] = None
        for offset in range(total):
            index = (start_index + offset) % total
            token, signer = self._signers[index]
            with self._request_slots[index]:
                result = signer.sign(site, rule)
            if not _needs_token_switch(result):
                with self._index_lock:
                    self._index = index
                return result
            last = result
            if total > 1:
                self._log("PT站点签到：Browserless Token %s/%s 不可用（%s），换下一个",
                          index + 1, total, result.message)
        return last or SignResult("failed", "Browserless Token 全部不可用")


def _browserless_error_message(operation: str, exc: BaseException, elapsed: float,
                               timeout: int) -> str:
    """把 Browserless 的网络层异常翻译成可执行的排查建议。"""
    step = {"CheckIn": "签到主流程", "CheckInSubmit": "token 注入提交"}.get(
        operation, "签到页预检")
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
        pre_action = pre_click_script(str(rule.get("pre_click_selector") or ""),
                                      str(rule.get("pre_click_text") or ""),
                                      int(rule.get("pre_click_wait") or 800))
        if pre_action:
            variables["preAction"] = pre_action
            variables["preActionWait"] = max(
                0, min(int(rule.get("pre_click_wait") or 800), 15000))
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

    def _sign_piggo(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        """猪猪专用 Browserless 流程：登录/解锁 -> 点击并 solve -> 注入 token 重试一次。"""
        base_url = str(site.get("url") or "").rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return SignResult("failed", "猪猪站点地址无效")

        account = account_from_site(site)
        username = str(account.get("username") or "").strip()
        password = str(account.get("password") or "")
        cookie = str(site.get("cookie") or "").strip()
        if not cookie and not account_ready(account):
            return SignResult("failed", "猪猪签到需要账号密码或 Cookie（Browserless 流程两者均未提供）")

        calendar_url = target_url(base_url, str(rule.get("path") or "/calendar"))
        cookies = cookie_objects(cookie, calendar_url) if cookie else []
        if cookie and not cookies:
            return SignResult("failed", "站点 Cookie 格式无效")
        user_agent = str(site.get("ua") or "").strip()
        state_script = _piggo_state_script(rule)
        ready_selector = str(rule.get("playwright_page_ready_selector")
                             or rule.get("playwright_calendar_selector") or "")
        calendar_wait = browserless_calendar_wait_ms(rule)
        login_url = target_url(base_url, str(rule.get("browser_login_url")
                                           or "/login?redirect=/calendar"))
        auth_wait = max(1000, min(int(rule.get("browserless_auth_wait") or 12000), 60000))
        click_wait = max(1000, min(int(rule.get("browserless_click_wait") or 8000), 30000))
        reconnect_wait = reconnect_wait_ms(rule)

        prepare_variables: Dict[str, Any] = {
            "cookies": cookies,
            "loginUrl": login_url,
            "calendarUrl": calendar_url,
            "waitLogin": 2000,
            "authWait": auth_wait,
            "loginScript": _piggo_login_script(rule, username, password),
            "unlockScript": _piggo_unlock_script(rule, password),
            "stateScript": state_script,
            "readySelector": ready_selector,
            "calendarWait": calendar_wait,
            "reconnectTimeout": reconnect_wait,
        }
        if user_agent:
            prepare_variables["userAgent"] = user_agent
        prepared = self._request(
            prepare_variables, "PiggoPrepare", build_piggo_prepare_query(user_agent),
            timeout=self.request_timeout(rule),
        )
        if isinstance(prepared, SignResult):
            return prepared

        login_state = _piggo_value(prepared, "login")
        unlock_state = _piggo_value(prepared, "unlock")
        state = _piggo_value(prepared, "state")
        control = state.get("control") if isinstance(state.get("control"), Mapping) else {}
        calendar = state.get("calendar") if isinstance(state.get("calendar"), Mapping) else {}
        prepared_html = str((prepared.get("html") or {}).get("html") or "")

        if state.get("login") or (not control and login_page(prepared_html)):
            if not account_ready(account):
                return SignResult(
                    "failed",
                    "Browserless 中的 Cookie 已失效，登录页要求账号密码；"
                    "请补充站点账号密码后重试",
                )
            if login_state.get("submitted"):
                return SignResult(
                    "failed",
                    "Browserless 自动登录失败：提交账号密码后仍停留在登录页；"
                    "请检查账号密码或 Browserless 节点是否被人机验证拦截",
                )
            return SignResult("failed", "Browserless 未能在登录页提交账号密码")
        if state.get("stepUp") or state.get("locked"):
            if not password:
                return SignResult("failed", "签到页处于空闲锁定状态，但未配置当前密码，无法解锁")
            if unlock_state.get("submitted"):
                return SignResult(
                    "failed",
                    "Browserless 空闲解锁失败：提交密码后页面仍处于锁定状态，请检查密码是否正确",
                )
            return SignResult("failed", "Browserless 检测到空闲锁定页，但未找到可提交的解锁表单")
        if not state:
            hint = _piggo_step_hint(prepared)
            return SignResult("failed", "Browserless 打开猪猪签到页失败" +
                              (f"（{hint}）" if hint else ""))
        if control.get("already"):
            return SignResult("already", "Browserless 确认今日已签到")
        if control.get("skip"):
            return SignResult("failed", "当天签到控件显示「补签」状态，未执行签到")
        if not control.get("present"):
            hint = _piggo_step_hint(prepared)
            suffix = f"（{hint}）" if hint else ""
            if calendar.get("loading"):
                return SignResult(
                    "failed",
                    "Browserless 等待猪猪签到日历加载完成超时：页面仍在显示加载骨架屏，"
                    "未读取签到控件" + suffix,
                )
            if calendar.get("found"):
                return SignResult(
                    "failed",
                    "Browserless 签到日历已加载，但今日没有签到入口或控件状态异常" + suffix,
                )
            return SignResult(
                "failed",
                "Browserless 未识别到猪猪签到日历或登录/解锁页面，无法确认签到状态" + suffix,
            )
        if control.get("disabled"):
            return SignResult("failed", "当天签到控件当前不可用")
        if not control.get("actionable"):
            text = str(control.get("text") or "无文字")[:60]
            return SignResult("failed", f"当天签到控件状态未识别（{text}）")

        session = prepared.get("session") if isinstance(prepared.get("session"), Mapping) else {}
        endpoint = session_endpoint(str(session.get("browserQLEndpoint") or ""), self.token)
        if not endpoint:
            return SignResult(
                "failed",
                "Browserless 未返回可复用的会话地址，无法在 solve 后注入 token；未执行点击",
            )

        solve_variables: Dict[str, Any] = {
            "calendarUrl": calendar_url,
            "readySelector": ready_selector,
            "calendarWait": calendar_wait,
            "beforeWait": navigation_wait_ms(rule),
            "clickWait": click_wait,
            "stateScript": state_script,
            "clickScript": _piggo_click_script(rule),
            "solveTimeout": solve_timeout_ms(rule),
            "reconnectTimeout": reconnect_wait,
        }
        if user_agent:
            solve_variables["userAgent"] = user_agent
        solved = self._request(
            solve_variables, "PiggoSolve", build_piggo_solve_query(user_agent),
            timeout=self.request_timeout(rule), endpoint=endpoint,
        )
        if isinstance(solved, SignResult):
            if solved.transient:
                return SignResult(
                    "failed",
                    f"{solved.message}；猪猪点击后的响应丢失，未再次点击，未能确认签到结果",
                    transient=True,
                )
            return solved

        before = _piggo_value(solved, "stateBefore")
        before_control = before.get("control") if isinstance(before.get("control"), Mapping) else {}
        before_calendar = before.get("calendar") if isinstance(before.get("calendar"), Mapping) else {}
        if before.get("login"):
            return SignResult("failed", "Browserless 会话登录状态已失效，请重新登录后重试")
        if before.get("stepUp") or before.get("locked"):
            return SignResult("failed", "Browserless 会话再次被空闲锁定，未执行签到点击")
        if before_control.get("already"):
            return SignResult("already", "Browserless 确认今日已签到")
        if before_control.get("skip"):
            return SignResult("failed", "当天签到控件显示「补签」状态，未执行签到")
        if not before_control.get("actionable"):
            hint = _piggo_step_hint(solved)
            suffix = f"（{hint}）" if hint else ""
            if before_calendar.get("loading"):
                return SignResult(
                    "failed",
                    "Browserless 重新打开签到页后等待日历加载超时：页面仍在显示骨架屏，" +
                    "未执行签到点击" + suffix,
                )
            if before_calendar.get("found"):
                return SignResult(
                    "failed",
                    "Browserless 签到日历已加载，但今日没有可执行的签到控件，" +
                    "未执行签到点击" + suffix,
                )
            return SignResult("failed", "重新打开签到页后，当天签到控件不可用" + suffix)

        click_state = _piggo_value(solved, "click")
        after = _piggo_value(solved, "stateAfter")
        after_control = after.get("control") if isinstance(after.get("control"), Mapping) else {}
        if after_control.get("already"):
            return SignResult("success", "Browserless 点击签到后已确认今日签到成功")
        if not click_state.get("clicked"):
            return SignResult("failed", "点击时签到控件状态发生变化，未提交签到请求")

        solve = solved.get("solve") if isinstance(solved.get("solve"), Mapping) else {}
        token = str(solve.get("token") or "").strip()
        if not token:
            hint = _piggo_step_hint(solved)
            return SignResult(
                "failed",
                "点击签到后未取得 Turnstile token，未进行第二次提交；"
                "请检查 Browserless 节点的 solve 能力或稍后重试" +
                (f"（{hint}）" if hint else ""),
                challenge="captcha",
            )

        solve_session = solved.get("session") if isinstance(solved.get("session"), Mapping) else {}
        submit_endpoint = session_endpoint(
            str(solve_session.get("browserQLEndpoint") or session.get("browserQLEndpoint") or ""),
            self.token,
        )
        if not submit_endpoint:
            return SignResult("failed", "Browserless solve 后未返回可复用会话，未执行 token 重试")

        submit_variables: Dict[str, Any] = {
            "calendarUrl": calendar_url,
            "readySelector": ready_selector,
            "calendarWait": calendar_wait,
            "beforeWait": navigation_wait_ms(rule),
            "stateScript": state_script,
            "injectScript": _piggo_inject_script(rule, token),
            "waitAfter": click_wait,
        }
        if user_agent:
            submit_variables["userAgent"] = user_agent
        submitted = self._request(
            submit_variables, "PiggoSubmit", build_piggo_submit_query(user_agent),
            timeout=self.request_timeout(rule), endpoint=submit_endpoint,
        )
        if isinstance(submitted, SignResult):
            if submitted.transient:
                return SignResult(
                    "failed",
                    f"{submitted.message}；token 重试响应丢失，未再次点击，未能确认签到结果",
                    transient=True,
                )
            return submitted

        inject_state = _piggo_value(submitted, "inject")
        final = _piggo_value(submitted, "stateAfter")
        final_control = final.get("control") if isinstance(final.get("control"), Mapping) else {}
        if final_control.get("already"):
            return SignResult("success", "Browserless 注入 Turnstile token 后签到成功")
        if final.get("login"):
            return SignResult("failed", "token 重试阶段登录状态失效，未确认签到成功")
        if final.get("stepUp") or final.get("locked"):
            return SignResult("failed", "token 重试阶段页面被空闲锁定，未确认签到成功")
        if not inject_state.get("injected"):
            return SignResult("failed", "已取得 Turnstile token，但未能写入签到页，未再次点击",
                              challenge="captcha")
        if not inject_state.get("clicked"):
            return SignResult("failed", "Turnstile token 已注入，但签到按钮不可用，未再次点击",
                              challenge="captcha")
        hint = _piggo_step_hint(submitted)
        return SignResult(
            "failed",
            "已使用 Turnstile token 重试一次，但当天签到控件仍未显示已签到" +
            (f"（{hint}）" if hint else ""),
            challenge="captcha",
        )

    def sign(self, site: Mapping[str, Any], rule: Mapping[str, Any]) -> SignResult:
        fallback_mode = str(rule.get("browserless_fallback_mode") or "").strip().lower()
        if fallback_mode == "piggo":
            try:
                return self._sign_piggo(site, rule)
            except Exception:
                # 登录脚本变量里可能有账号密码，绝不能把底层异常原样写进结果或日志。
                return SignResult("failed", "Browserless 猪猪签到执行出现未预期错误（异常详情已隐藏）")
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
                                                       rule.get("submit_text"),
                                                       require_text=bool(rule.get("require_submit_text"))))
        pre_action = pre_click_script(str(rule.get("pre_click_selector") or ""),
                                      str(rule.get("pre_click_text") or ""),
                                      int(rule.get("pre_click_wait") or 800))
        if pre_action:
            variables["preAction"] = pre_action
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
        clicked = judge_after_click(rule, submitted, after_html)
        if clicked:
            return clicked
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
            started = time.monotonic()
            session: Any = None
            try:
                # V2 / V3 都走宿主提供的 HTTP 层，复用其代理、证书和 TLS 兼容处理。
                # 直接 urllib 会绕过 MoviePilot 的请求实现，V2 上容易出现 SSL EOF。
                session = new_http_session()
                session.headers.update({"Content-Type": "application/json",
                                        "Accept": "application/json"})
                response = session.post(url, data=payload, timeout=read_timeout,
                                        allow_redirects=True, raise_exception=False)
                if response is None:
                    raise ConnectionError("Browserless 请求未返回响应")
                status = int(getattr(response, "status_code", 0) or 0)
                if status in {401, 403}:
                    return SignResult("failed", f"Browserless 鉴权失败（HTTP {status}），请检查 Token")
                if status == 429:
                    return SignResult("failed", "Browserless 额度不足或请求过于频繁（HTTP 429）")
                if status >= 500 and attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                if status >= 400:
                    return SignResult("failed", f"Browserless HTTP {status}（步骤 {operation}）")
                value = json.loads(str(getattr(response, "text", "") or ""))
                break
            except (TimeoutError, OSError, ValueError, RuntimeError) as exc:
                elapsed = time.monotonic() - started
                if attempt < attempts:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return SignResult(
                    "failed",
                    _browserless_error_message(operation, exc, elapsed, read_timeout),
                    # JSON 解析失败不是网络问题，其余（超时 / 连接中断）结果未知
                    transient=not isinstance(exc, ValueError))
            finally:
                closer = getattr(session, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass
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
