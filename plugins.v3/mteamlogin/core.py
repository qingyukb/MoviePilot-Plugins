# -*- coding: utf-8 -*-
"""M-Team（馒头）真实登录 + 保活心跳 + 「最近动向」查询核心逻辑。

本模块不依赖 MoviePilot，可单独测试与复用。

接口契约（2026-10 从 kp.m-team.cc 前端 bundle 逆向并用真实请求验证）：

* 前端 kp.m-team.cc 与后端 api.m-team.cc 前后端分离，API 基址 https://api.m-team.cc/api 。
* 签名算法 base64(HMAC-SHA1(secret, "METHOD&path&ts_ms"))，其中 path **包含** /api 前缀；
  字段名沿用站点自身的拼写 _timestamp / _sgin。
* GET 把 _timestamp / _sgin 放在 URL query，POST 放在 multipart 表单里。
* 其它请求头：ts（秒）、version、webVersion、visitorId；登录态走 authorization。
* 保活：前端在登录成功后**立刻**调用 POST /api/member/updateLastBrowse，
  站点会把「最近动向」（/api/member/profile 里的 data.memberStatus.lastBrowse）刷成当前时间。
  本插件每轮都会调用它，所以保活正常时最近动向应该只有「刚刚 / 1 分钟前」。
* 凭证只从 MoviePilot「站点管理」读取（站点表只有 apikey / token / cookie 三个可用字段）：
  - token      -> authorization 请求头
  - cookie     -> 从中解析 authorization / token，或整体作为 Cookie 头
  - apikey     -> x-api-key 请求头（M-Team 官方索引器同款），同时兜底尝试 authorization
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------- 常量

DEFAULT_API_HOST = "https://api.m-team.cc/api"
API_HOSTS = (
    "https://api.m-team.cc/api",
    "https://api.m-team.io/api",
    "https://api2.m-team.cc/api",
)
# 站点前端 bundle 里硬编码的 HMAC 密钥（各镜像共用）。
SIGN_SECRET = "HLkPcWmycL57mfJt"
CLIENT_VERSION = "2.0.0"
WEB_VERSION = "2000"
DEFAULT_TIMEOUT = 20
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LOGIN_PATH = "/login"
PROFILE_PATH = "/member/profile"
# 前端登录 / 校验通过后立刻调用的保活心跳：把「最近动向」刷成当前时间。
UPDATE_LAST_BROWSE_PATH = "/member/updateLastBrowse"
# 站点管理里的 API Key / 令牌失效时的提示特征。
INVALID_API_KEY_KEYWORDS = ("key無效", "key无效", "無效的key", "invalid api key", "invalid key",
                            "令牌無效", "令牌无效", "token invalid", "unauthorized")
# 凭证不可用 / 未登录时的提示特征。
PROFILE_NOT_FOUND_KEYWORDS = ("無效的憑證", "无效的凭证", "full authentication",
                              "not authenticated", "unauthorized", "登錄已過期", "登录已过期")
# 请求方法不被接口接受（前后端分离的站点把只读接口也改成 POST-only 之类）。
METHOD_NOT_SUPPORTED_KEYWORDS = ("not supported", "not_supported", "method not allowed",
                                 "请求方法", "請求方法", "方法不支持")
# 站点管理的 Cookie / Token 里可能藏着登录令牌的字段名。
TOKEN_COOKIE_KEYS = ("authorization", "auth", "token", "jwt", "access_token", "accesstoken")
# 判断一个字符串是不是 JWT（M-Team 登录后返回的 authorization 就是 JWT）。
JWT_RE = re.compile(r"^[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}$")
# 站点管理里可能用到的馒头站点识别关键字。
MTEAM_SITE_KEYWORDS = ("m-team", "mteam", "馒头")

# --------------------------------------------------------------------- 签名


def sign(method: str, path: str, ts_ms: int, secret: str = SIGN_SECRET) -> str:
    """计算 M-Team 接口签名：base64(HMAC-SHA1(secret, "METHOD&path&ts_ms"))。"""
    message = str(method).upper() + "&" + str(path) + "&" + str(int(ts_ms))
    digest = hmac.new(str(secret).encode("utf-8"), message.encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def auth_params(method: str, path: str, ts_ms: Optional[int] = None,
                secret: str = SIGN_SECRET) -> Dict[str, str]:
    """返回该请求需要附带的 _timestamp / _sgin 字段。"""
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    return {"_timestamp": str(ts), "_sgin": sign(method, path, ts, secret)}


def encode_multipart(fields: Sequence[Tuple[str, Any]],
                     boundary: Optional[str] = None) -> Tuple[str, bytes]:
    """把字段编码成 multipart/form-data 请求体，返回 (Content-Type, body)。"""
    boundary = boundary or "----MoviePilotMTeam" + os.urandom(8).hex()
    chunks: List[bytes] = []
    for name, value in fields:
        chunks.append(("--" + boundary + "\r\n").encode("utf-8"))
        chunks.append(('Content-Disposition: form-data; name="' + str(name) + '"\r\n\r\n'
                       ).encode("utf-8"))
        chunks.append(("" if value is None else str(value)).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(("--" + boundary + "--\r\n").encode("utf-8"))
    return "multipart/form-data; boundary=" + boundary, b"".join(chunks)

# --------------------------------------------------------------------- 结果


class MTeamError(RuntimeError):
    """网络 / 协议层错误（业务错误用返回值表达，不抛异常）。"""


@dataclass
class LastBrowse:
    """归一化后的「最近动向」。"""

    raw: Any = None
    ms: Optional[int] = None
    text: str = ""
    relative: str = ""

    @property
    def empty(self) -> bool:
        return not self.text and self.ms is None and not str(self.raw or "").strip()


@dataclass
class KeepAliveResult:
    """一次保活心跳（POST /member/updateLastBrowse）的结果。"""

    ok: bool = False
    code: int = -1
    message: str = ""
    endpoint: str = ""
    auth: str = ""


@dataclass
class AuthPlan:
    """一种认证方式：把站点管理里的凭证展开成单一口径的请求头。"""

    name: str = ""
    token: str = ""
    api_key: str = ""
    cookie: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.token or self.api_key or self.cookie)


@dataclass
class Credentials:
    """从 MoviePilot「站点管理」读到的一组馒头凭证，绝不写入日志。"""

    token: str = ""
    api_key: str = ""
    cookie: str = ""
    # 人类可读的来源，仅用于展示 / 排错，不含明文凭证。
    source: str = ""

    @property
    def kind(self) -> str:
        if self.token:
            return "token"
        if self.api_key:
            return "api_key"
        if self.cookie:
            return "cookie"
        return ""

    @property
    def usable(self) -> bool:
        return bool(self.auth_plans())

    def auth_plans(self) -> List[AuthPlan]:
        """按优先级展开成候选认证方案（每个方案只带一种凭证）。"""
        plans: List[AuthPlan] = []
        seen: set = set()

        def _add(plan: AuthPlan) -> None:
            key = (plan.name, plan.token, plan.api_key, bool(plan.cookie))
            if plan.usable and key not in seen:
                seen.add(key)
                plans.append(plan)

        token = str(self.token or "").strip()
        api_key = str(self.api_key or "").strip()
        cookie_token = extract_token_from_cookie(self.cookie)
        cookie = str(self.cookie or "").strip()

        if token:
            _add(AuthPlan(name="站点管理 Token", token=token))
            if not JWT_RE.match(token):
                # 令牌字段里填的其实是 API Key 时，也让它当一次 x-api-key 试试。
                _add(AuthPlan(name="站点管理 Token(当作 API Key)", api_key=token))
        if cookie_token:
            _add(AuthPlan(name="站点管理 Cookie 里的令牌", token=cookie_token))
        if api_key:
            _add(AuthPlan(name="站点管理 API Key", api_key=api_key))
        if api_key and JWT_RE.match(api_key):
            # 长得像 JWT 的存取令牌按 authorization 再试一次。
            _add(AuthPlan(name="站点管理 API Key(Bearer)", token=api_key))
        if cookie:
            _add(AuthPlan(name="站点管理 Cookie", cookie=cookie))
        return plans


@dataclass
class LoginReport:
    """一轮「认证 + 保活心跳 + 读取最近动向」的结果。"""

    ok: bool = False
    message: str = ""
    method: str = ""
    username: str = ""
    user_id: Any = None
    last_browse: LastBrowse = field(default_factory=LastBrowse)
    code: int = -1
    ran_at: str = ""
    # 本次使用的馒头站点（站点管理里的 domain）与凭证来源，便于详情页展示。
    site: str = ""
    credential: str = ""
    # 保活心跳结果：成功即表示站点已经把「最近动向」刷新成本次执行时间。
    keepalive_ok: bool = False
    keepalive_code: int = -1
    keepalive_message: str = ""
    # 实际命中的接口地址（METHOD URL）与是否拿到了 M-Team 的结构化 JSON。
    endpoint: str = ""
    api_ok: bool = False
# --------------------------------------------------------------------- 工具


def mask(value: Optional[str], keep: int = 4) -> str:
    """日志脱敏：只保留首尾少量字符。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "***" + text[-keep:]


def mask_account(value: Optional[str]) -> str:
    """账号名脱敏：保留首尾字符，便于辨认。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 2:
        return text[0] + "*"
    return text[0] + "***" + text[-1]


def normalize_host(host: Optional[str]) -> str:
    """规整 API 基址：去空格与末尾斜杠，缺路径时补 /api。"""
    text = str(host or DEFAULT_API_HOST).strip().rstrip("/")
    if not text:
        text = DEFAULT_API_HOST
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    path = parsed.path.rstrip("/") or "/api"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def derive_api_host(site_url: Optional[str], default: str = DEFAULT_API_HOST) -> str:
    """由「站点管理」里的站点地址推导 M-Team API 基址（api.<注册域名>/api）。

    例如 kp.m-team.cc / m-team.cc 都推导为 https://api.m-team.cc/api，
    kp.m-team.io 推导为 https://api.m-team.io/api；无法解析时回退到默认值。
    """
    raw = str(site_url or "").strip()
    if not raw:
        return normalize_host(default)
    parsed = urllib.parse.urlsplit(raw if "://" in raw else "//" + raw)
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).strip()
    host = host.split(":")[0].strip(".")
    if not host:
        return normalize_host(default)
    if host.startswith("api."):
        return normalize_host("https://" + host)
    labels = host.split(".")
    registered = ".".join(labels[-2:]) if len(labels) >= 2 else host
    return normalize_host("https://api." + registered)


def api_host_candidates(primary: Optional[str]) -> List[str]:
    """候选接口地址：优先用传入的（按站点地址推导），再回退到已知可用的官方地址。"""
    hosts: List[str] = []

    def _add(value: Optional[str]) -> None:
        try:
            normalized = normalize_host(value)
        except Exception:  # noqa: BLE001 - 任何异常都不应阻断候选列表
            return
        if normalized not in hosts:
            hosts.append(normalized)

    _add(primary)
    _add(derive_api_host(primary))
    for host in API_HOSTS:
        _add(host)
    return hosts


def looks_like_api_response(body: Any) -> bool:
    """判断响应是否为 M-Team 结构化 JSON（带 code 字段）。

    可传入原始响应体（bytes）或已解析的对象。对于 HTML 等非 JSON
    响应一律返回 False，避免把「非法 JSON」的合成错误误判为有效接口。
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(bytes(body).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - 非 JSON 就不是 M-Team 接口
            return False
    return isinstance(body, Mapping) and "code" in body


def is_mteam_site(site: Mapping[str, Any]) -> bool:
    """判断站点管理的某条记录是不是馒头（M-Team）：看域名 / 地址 / 名称。"""
    if not isinstance(site, Mapping):
        return False
    haystack = " ".join(str(site.get(key) or "") for key in ("domain", "url", "name")).lower()
    return any(word.lower() in haystack for word in MTEAM_SITE_KEYWORDS)


def pick_mteam_site(sites: Sequence[Mapping[str, Any]],
                    preferred: str = "") -> Optional[Mapping[str, Any]]:
    """从站点管理列表里挑出馒头站点。

    优先按配置里指定的 domain / id 精确匹配；其次按关键字（m-team / mteam / 馒头）
    自动识别，取第一条命中的站点。
    """
    preferred = str(preferred or "").strip()
    items = [site for site in (sites or []) if isinstance(site, Mapping)]
    if preferred:
        for site in items:
            if str(site.get("domain") or "") == preferred \
                    or str(site.get("id") or "") == preferred:
                return site
    for site in items:
        if is_mteam_site(site):
            return site
    return None


def extract_token_from_cookie(cookie: Optional[str]) -> str:
    """从站点管理的 Cookie 字符串里解析登录令牌（authorization / token / jwt）。"""
    text = str(cookie or "").strip()
    if not text:
        return ""
    fragments = re.split(r"[;\n]+", text)
    for fragment in fragments:
        piece = fragment.strip()
        if not piece:
            continue
        name, _, value = piece.partition("=")
        key = name.strip().lower().lstrip("_-")
        value = value.strip().strip('"')
        if key in TOKEN_COOKIE_KEYS and value:
            return value
    # 没有键名时（直接粘了一串 JWT）兜底判断。
    candidate = text.strip().strip('"')
    if JWT_RE.match(candidate):
        return candidate
    return ""


def credentials_from_site(site: Optional[Mapping[str, Any]]) -> Credentials:
    """把「站点管理」里的一条站点记录转成馒头凭证。

    站点表只有 apikey / token / cookie 三个可用字段，按 token > apikey > cookie 读取。
    """
    if not isinstance(site, Mapping):
        return Credentials()
    token = str(site.get("token") or "").strip()
    api_key = str(site.get("apikey") or "").strip()
    cookie = str(site.get("cookie") or "").strip()
    sources: List[str] = []
    if token:
        sources.append("Token")
    if api_key:
        sources.append("API Key")
    if extract_token_from_cookie(cookie):
        sources.append("Cookie 令牌")
    elif cookie:
        sources.append("Cookie")
    return Credentials(token=token, api_key=api_key, cookie=cookie,
                       source=" + ".join(sources))


def _to_ms(value: Any) -> Optional[int]:
    """把 10 位秒 / 13 位毫秒时间戳统一成毫秒。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number * 1000 if number < 100000000000 else number


def _parse_datetime(text: str, tz_hours: int = 8) -> Optional[datetime]:
    """解析站点可能返回的日期字符串（多为本地时区的 "YYYY-MM-DD HH:MM:SS"）。"""
    value = str(text or "").strip()
    if not value:
        return None
    # 站点多返回不带时区的本地时间，统一按东八区解释。
    local = timezone(timedelta(hours=tz_hours))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=local)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=local)


def humanize(ms: Optional[int], now_ms: Optional[int] = None) -> str:
    """把毫秒时间戳转成「3 小时前」这样的相对描述。"""
    if not ms:
        return ""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    delta = now - int(ms)
    suffix = "前"
    if delta < 0:
        delta, suffix = -delta, "后"
    seconds = delta // 1000
    if seconds < 60:
        return "刚刚" if suffix == "前" else "即将"
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400
    if minutes < 60:
        return str(minutes) + " 分钟" + suffix
    if hours < 24:
        return str(hours) + " 小时" + suffix
    if days < 30:
        return str(days) + " 天" + suffix
    if days < 365:
        return str(days // 30) + " 个月" + suffix
    return str(days // 365) + " 年" + suffix


def format_last_browse(raw: Any, tz_hours: int = 8) -> LastBrowse:
    """把接口返回的 lastBrowse 归一化成「YYYY-MM-DD HH:mm:ss」+ 相对时间。"""
    if raw is None:
        return LastBrowse(raw=raw)
    if isinstance(raw, str) and not raw.strip():
        return LastBrowse(raw=raw)
    text = str(raw).strip()
    ms: Optional[int] = None
    if isinstance(raw, (int, float)) or text.lstrip("-").isdigit():
        ms = _to_ms(raw)
    else:
        parsed = _parse_datetime(text, tz_hours)
        if parsed is not None:
            ms = int(parsed.timestamp() * 1000)
    if ms is None:
        return LastBrowse(raw=raw, text=text)
    zone = timezone(timedelta(hours=tz_hours))
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(zone)
    return LastBrowse(raw=raw, ms=ms, text=moment.strftime("%Y-%m-%d %H:%M:%S"),
                      relative=humanize(ms))


def extract_last_browse(profile: Mapping[str, Any]) -> Any:
    """从 /member/profile 响应里取 data.memberStatus.lastBrowse。"""
    if not isinstance(profile, Mapping):
        return None
    data = profile.get("data")
    if not isinstance(data, Mapping):
        return None
    status = data.get("memberStatus")
    if isinstance(status, Mapping):
        for key in ("lastBrowse", "lastBrowseTime", "lastAccess"):
            if status.get(key) not in (None, ""):
                return status.get(key)
    for key in ("lastBrowse", "lastBrowseTime"):
        if data.get(key) not in (None, ""):
            return data.get(key)
    return None


def extract_member_identity(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """从 /member/profile 里取用户标识，便于详情页展示。"""
    data = (profile or {}).get("data")
    if not isinstance(data, Mapping):
        return {}
    member = data.get("member")
    return member if isinstance(member, Mapping) else {}


def hint_for(message: str) -> str:
    """把站点的原始错误翻译成一句可操作的提示。"""
    text = str(message or "").lower()
    if any(word.lower() in text for word in INVALID_API_KEY_KEYWORDS):
        return ("（凭证无效或已过期：请在 设置 → 站点管理 更新该馒头站点的 Token / API Key，"
                "Token 建议直接粘贴浏览器登录后的 authorization）")
    if any(word.lower() in text for word in METHOD_NOT_SUPPORTED_KEYWORDS):
        return ("（接口地址可能不对：前后端分离的站点后端在 api.<域名>/api，"
                "请检查「站点管理」里的站点地址或本插件的「API 基址」）")
    if any(word.lower() in text for word in PROFILE_NOT_FOUND_KEYWORDS):
        return "（凭证已失效：请在 设置 → 站点管理 重新填写该馒头站点的 Token / API Key）"
    return ""


def _method_not_supported(status: int, body: Mapping[str, Any]) -> bool:
    """判断响应是不是「这个请求方法不被支持」。"""
    if status in (405, 501):
        return True
    text = str((body or {}).get("message") or "").lower()
    return any(word in text for word in ("not supported", "not_supported", "method not allowed"))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _decode_json(raw_body: bytes) -> Dict[str, Any]:
    if not raw_body:
        return {}
    try:
        parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        snippet = raw_body[:200].decode("utf-8", errors="replace")
        return {"code": -1, "message": "响应不是合法 JSON：" + snippet, "data": None}
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {"code": -1, "message": "响应结构异常", "data": parsed}
# --------------------------------------------------------------------- 客户端


class MTeamClient:
    """M-Team API 客户端：认证 + 保活心跳（updateLastBrowse）+ 读取最近动向。

    只会调用三个接口：POST /login（可选，仅用于验证 Token 有效性）、
    POST /member/updateLastBrowse（保活心跳）、GET|POST /member/profile（只读）。
    """

    def __init__(self, host: str = DEFAULT_API_HOST, timeout: int = DEFAULT_TIMEOUT,
                 proxy: bool = False, user_agent: str = DEFAULT_USER_AGENT,
                 secret: str = SIGN_SECRET) -> None:
        self.host = normalize_host(host)
        self.timeout = max(5, int(timeout or DEFAULT_TIMEOUT))
        self.proxy = bool(proxy)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.secret = secret or SIGN_SECRET
        self._base_path = urllib.parse.urlsplit(self.host).path.rstrip("/") or "/api"
        # 最近一次请求的诊断信息（供上层判断是否需要回退其它地址）。
        self.last_request = ""
        self.last_status = 0
        self.last_body: Dict[str, Any] = {}
        self._api_ok = False
        handlers: List[Any] = []
        if not self.proxy:
            # 与 MoviePilot 其它插件一致：未勾选代理时显式绕过系统代理。
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ---------------------------------------------------------- 底层请求

    def request(self, method: str, path: str, fields: Optional[Sequence[Tuple[str, Any]]] = None,
                query: Optional[Mapping[str, Any]] = None, plan: Optional[AuthPlan] = None,
                ts_ms: Optional[int] = None) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        """发一个已签名请求，返回 (HTTP 状态码, 响应头, 响应 JSON)。"""
        method = str(method).upper()
        sign_path = self._base_path + path
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        params = auth_params(method, sign_path, ts, self.secret)

        body: Optional[bytes] = None
        content_type = ""
        if method == "GET":
            merged = dict(query or {})
            merged.update(params)
            url = self.host + path + "?" + urllib.parse.urlencode(merged)
        else:
            payload = list(fields or [])
            payload.extend(params.items())
            content_type, body = encode_multipart(payload)
            suffix = "?" + urllib.parse.urlencode(dict(query)) if query else ""
            url = self.host + path + suffix

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://kp.m-team.cc",
            "Referer": "https://kp.m-team.cc/",
            "ts": str(int(ts / 1000)),
            "version": CLIENT_VERSION,
            "webVersion": WEB_VERSION,
            "visitorId": os.urandom(8).hex(),
        }
        plan = plan or AuthPlan()
        if plan.token:
            headers["authorization"] = plan.token
        if plan.api_key:
            headers["x-api-key"] = plan.api_key
        if plan.cookie:
            headers["Cookie"] = plan.cookie
        if content_type:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        self.last_request = method + " " + url
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw_headers = response.headers
                raw_body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw_headers = error.headers
            raw_body = error.read()
        except urllib.error.URLError as error:
            raise MTeamError("网络请求失败：" + str(error.reason)) from error
        except TimeoutError as error:
            raise MTeamError("网络请求超时，请检查网络或稍后重试") from error

        decoded = _decode_json(raw_body)
        self.last_status = status
        self.last_body = decoded
        # 用原始响应体判定：只有真正的 JSON（带 code）才算命中 M-Team 接口。
        self._api_ok = looks_like_api_response(raw_body) and isinstance(decoded, Mapping)
        header_map = {}
        for key, value in (raw_headers or {}).items():
            header_map[str(key).lower()] = str(value)
        return status, header_map, decoded

    # ---------------------------------------------------------- 业务接口

    def keepalive(self, plan: Optional[AuthPlan] = None, ts_ms: Optional[int] = None
                  ) -> KeepAliveResult:
        """POST /member/updateLastBrowse：真正的保活心跳，把「最近动向」刷成当前时间。"""
        plan = plan or AuthPlan()
        if not plan.usable:
            raise MTeamError("缺少可用的登录凭证（Token / API Key / Cookie）")
        status, _headers, body = self.request("POST", UPDATE_LAST_BROWSE_PATH, fields=[],
                                              plan=plan, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        message = str(body.get("message") or "")
        if status >= 400 and not message:
            message = "HTTP " + str(status)
        ok = status < 400 and code == 0
        if not ok and message and not hint_for(message):
            message = message + hint_for(message)
        return KeepAliveResult(ok=ok, code=code, message=message,
                               endpoint=self.last_request, auth=plan.name)

    def profile(self, plan: Optional[AuthPlan] = None,
                ts_ms: Optional[int] = None) -> Dict[str, Any]:
        """GET /member/profile，返回原始响应体（405 时自动回退 POST）。"""
        plan = plan or AuthPlan()
        if not plan.usable:
            raise MTeamError("缺少 authorization 凭证")
        status, _headers, body = self.request("GET", PROFILE_PATH, plan=plan, ts_ms=ts_ms)
        if _method_not_supported(status, body):
            # 部分镜像把只读接口也改成 POST-only；自动回退一次。
            status, _headers, body = self.request("POST", PROFILE_PATH, fields=[], plan=plan,
                                                  ts_ms=ts_ms)
        if status >= 400 and not body.get("message"):
            body = dict(body)
            body["code"] = _as_int(body.get("code"))
            body["message"] = "HTTP " + str(status)
        return body

    def last_browse(self, plan: Optional[AuthPlan] = None,
                    ts_ms: Optional[int] = None) -> LastBrowse:
        """读取「最近动向」（只读接口）。"""
        body = self.profile(plan=plan, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        if code != 0:
            message = str(body.get("message") or "读取用户资料失败")
            raise MTeamError(message + hint_for(message))
        return format_last_browse(extract_last_browse(body))

    # ---------------------------------------------------------- 组合流程

    def fetch_last_browse(self, credentials: Optional[Credentials] = None,
                          site_label: str = "") -> LoginReport:
        """认证 + 保活心跳 + 读取最近动向，返回结构化报表。

        * 逐个尝试站点管理里的认证方案，第一个能让 updateLastBrowse 返回 code==0 的胜出；
        * 保活成功后立刻读 /member/profile，此时「最近动向」应为刚刚；
        * 保活全部失败时不再空跑只读接口，直接给出指向「站点管理」的可操作提示。
        """
        credentials = credentials or Credentials()
        report = LoginReport(ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             site=site_label, credential=credentials.source)
        plans = credentials.auth_plans()
        if not plans:
            report.message = ("站点管理里的馒头站点没有可用的 Token / API Key / Cookie："
                              "请在 设置 -> 站点管理 补全该站点，再启用本插件。")
            return report

        keepalive: Optional[KeepAliveResult] = None
        winner: Optional[AuthPlan] = None
        failures: List[str] = []
        for plan in plans:
            try:
                result = self.keepalive(plan=plan)
            except MTeamError as error:
                failures.append(plan.name + "：" + str(error))
                continue
            report.endpoint = self.last_request
            report.api_ok = self._api_ok
            if result.ok:
                keepalive, winner = result, plan
                break
            failures.append(plan.name + "：" + (result.message or ("code=" + str(result.code))))

        report.method = "keepalive"
        if keepalive is not None:
            report.keepalive_ok = keepalive.ok
            report.keepalive_code = keepalive.code
            report.keepalive_message = keepalive.message
            report.code = keepalive.code
        report.credential = (credentials.source + " · " + (winner or plans[0]).name).strip(" ·")

        if keepalive is None or not keepalive.ok:
            report.ok = False
            detail = report.keepalive_message or ("；".join(failures) or "未知原因")
            report.message = ("保活心跳未成功（" + detail + "）：请在 设置 -> 站点管理 "
                              "把该馒头站点的 Token 填成浏览器登录后的 authorization，"
                              "或填写站点 API Key。")
            return report

        # 保活成功后读 /member/profile，用 memberStatus.lastBrowse 做校验。
        try:
            browse = self.last_browse(plan=winner)
        except MTeamError as error:
            report.ok = False
            report.message = "保活心跳已成功，但读取最近动向失败：" + str(error)
            return report

        report.last_browse = browse
        report.api_ok = report.api_ok or self._api_ok
        identity = extract_member_identity(self.last_body)
        report.user_id = identity.get("id")
        report.username = str(identity.get("username") or "")

        if browse.empty:
            report.ok = False
            report.message = "保活心跳已成功，但站点未返回最近动向"
            return report

        report.ok = True
        report.message = "已保活，最近动向：" + browse.text
        if browse.ms is not None:
            age = int(time.time() * 1000) - int(browse.ms)
            if age > 5 * 60 * 1000:
                # 保活接口返回成功但时间没刷新：站点缓存，或该凭证只有读取权限。
                report.message = ("保活心跳返回成功，但最近动向仍是 "
                                  + (browse.relative or browse.text)
                                  + "；站点可能缓存了该接口，或该凭证是只读令牌、没有保活权限。")
        return report
