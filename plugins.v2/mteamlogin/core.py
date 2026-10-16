# -*- coding: utf-8 -*-
"""M-Team（馒头）真实登录 + 保活心跳 + 「最近动向」查询核心逻辑。

本模块不依赖 MoviePilot，可单独测试与复用。

接口契约（2026-10 从 kp.m-team.cc 前端 bundle 逆向并用真实请求验证）：

* 前端 kp.m-team.cc 与后端前后端分离；浏览器实际使用 https://api.m-team.io/api，
  插件默认优先该地址，api.m-team.cc / api2.m-team.cc 仅作兼容镜像。
* 签名算法 base64(HMAC-SHA1(secret, "METHOD&path&ts_ms"))，其中 path **包含** /api 前缀；
  字段名沿用站点自身的拼写 _timestamp / _sgin。
* GET 把 _timestamp / _sgin 放在 URL query，POST 放在 multipart 表单里。
* 其它请求头：ts（秒）、version、webVersion、visitorId；登录态走 authorization。
* 关键结论（实网验证）：**只有真实登录拿到的 authorization（JWT）才算活跃**。
  - POST /api/login 成功时站点只在**响应头**返回 authorization，前端把它写进 localStorage.auth；
  - POST /api/member/updateLastBrowse 用 authorization 调用才是「真实登录后的心跳」，
    站点会把「最近动向」（/api/member/profile 的 data.memberStatus.lastBrowse）刷成当前时间；
  - 用 API Key（x-api-key）调用同一接口一律返回 401
    "Full authentication is required to access this resource"，
    **API Key 只能读数据，不能保活**。
* 凭证全部由插件配置页手填（不再读 MoviePilot「站点管理」），本模块只认下面几个键：
  - username / password -> 真实登录：POST /api/login 换回新的 authorization（推荐）
  - otp_secret / otp_code / email_code / tg_code -> 二次验证材料（可选）
  - token   -> 直接复用浏览器登录后的 authorization（形如 JWT），可省掉账号密码
  - api_key -> x-api-key 请求头，只用于读取数据，绝不用于保活
  - cookie  -> 可从里面解析 authorization / token，或整体作为 Cookie 头
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

DEFAULT_API_HOST = "https://api.m-team.io/api"
API_HOSTS = (
    "https://api.m-team.io/api",
    "https://api.m-team.cc/api",
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
# 登录响应 code=1001（需要两步验证）时，用这个接口把验证码发到账号注册邮箱。
SEND_LOGIN_EMAIL_CODE_PATH = "/member/sendLoginEmailVerifyCode"
# 令牌 / API Key 失效时的提示特征。
INVALID_API_KEY_KEYWORDS = ("key無效", "key无效", "無效的key", "invalid api key", "invalid key",
                            "令牌無效", "令牌无效", "token invalid", "unauthorized")
# 只有真实登录态（authorization / Cookie）能保活；API Key 只能读。
KEEPALIVE_FIX_HINT = (
    "馒头（M-Team）的保活心跳只认真实登录态 authorization（JWT）：API Key 的 x-api-key 调用"
    "不算活跃。请在插件配置里填「账号 + 密码」（插件会 POST /api/login 真实登录并保活），"
    "或直接填浏览器登录 kp.m-team.cc 后 localStorage.auth 里那串 authorization。")
# 登录接口返回 authorization 的响应头名（大小写不敏感）。
AUTH_HEADER_NAMES = ("authorization", "x-authorization", "x-auth-token", "token", "auth")
# 少数镜像把登录令牌放在响应体里时的候选键名。
AUTH_BODY_KEYS = ("authorization", "auth", "token", "accessToken", "access_token", "jwt")
# 前端 loginTo 的判定：1001 需要二步验证、1002 需要邮箱验证码。
LOGIN_NEED_OTP_CODES = (1001,)
LOGIN_NEED_EMAIL_CODES = (1002,)
# 凭证不可用 / 未登录时的提示特征。
PROFILE_NOT_FOUND_KEYWORDS = ("無效的憑證", "无效的凭证", "full authentication",
                              "not authenticated", "unauthorized", "登錄已過期", "登录已过期")
# 请求方法不被接口接受（前后端分离的站点把只读接口也改成 POST-only 之类）。
METHOD_NOT_SUPPORTED_KEYWORDS = ("not supported", "not_supported", "method not allowed",
                                 "请求方法", "請求方法", "方法不支持")
# 手填的 Cookie / Token 里可能藏着登录令牌的字段名。
TOKEN_COOKIE_KEYS = ("authorization", "auth", "token", "jwt", "access_token", "accesstoken")
# 判断一个字符串是不是 JWT（M-Team 登录后返回的 authorization 就是 JWT）。
JWT_RE = re.compile(r"^[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}$")

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
class SendCodeResult:
    """一次「发送登录邮箱验证码」的结果。"""

    ok: bool = False
    code: int = -1
    message: str = ""
    endpoint: str = ""


@dataclass
class AuthPlan:
    """一种认证方式：把手填凭证展开成单一口径的请求头。"""

    name: str = ""
    token: str = ""
    api_key: str = ""
    cookie: str = ""
    # jwt（真实登录态，可保活）/ api_key（只读）/ cookie（兜底）
    scheme: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.token or self.api_key or self.cookie)

    @property
    def kind(self) -> str:
        """认证类型；未显式指定时按实际携带的凭证推断。"""
        if self.scheme:
            return self.scheme
        if self.token:
            return "jwt"
        if self.api_key:
            return "api_key"
        if self.cookie:
            return "cookie"
        return ""

    @property
    def keepalive_capable(self) -> bool:
        """只有真实登录态能刷新「最近动向」；API Key 只读。"""
        return self.kind in ("jwt", "cookie")


@dataclass
class Credentials:
    """插件配置页里手填的一组馒头凭证，绝不写入日志。"""

    token: str = ""
    api_key: str = ""
    cookie: str = ""
    # 真实登录用的账号密码（插件配置页填的），以及二次验证材料。
    username: str = ""
    password: str = ""
    otp_secret: str = ""
    otp_code: str = ""
    email_code: str = ""
    tg_code: str = ""
    turnstile: str = ""
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
    def can_login(self) -> bool:
        """是否同时填了账号与密码，可以执行真实登录。"""
        return bool(self.username and self.password)

    @property
    def usable(self) -> bool:
        return bool(self.auth_plans()) or self.can_login

    def login_otp_code(self, now: Optional[float] = None) -> str:
        """动态码：优先用手填的 otp_code，否则用 otp_secret 现算一个。"""
        if self.otp_code:
            return self.otp_code
        if self.otp_secret:
            return totp(self.otp_secret, now)
        return ""

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
            _add(AuthPlan(name="手填 authorization", token=token, scheme="jwt"))
            if not JWT_RE.match(token):
                # 令牌字段里填的其实是 API Key 时，也让它当一次 x-api-key 用（只读）。
                _add(AuthPlan(name="手填 Token 当 API Key", api_key=token,
                              scheme="api_key"))
        if cookie_token:
            _add(AuthPlan(name="手填 Cookie 令牌", token=cookie_token, scheme="jwt"))
        if api_key:
            _add(AuthPlan(name="手填 API Key", api_key=api_key, scheme="api_key"))
        if api_key and JWT_RE.match(api_key):
            # 长得像 JWT 的存取令牌按 authorization 再试一次。
            _add(AuthPlan(name="手填 API Key(Bearer)", token=api_key, scheme="jwt"))
        if cookie:
            _add(AuthPlan(name="手填 Cookie", cookie=cookie, scheme="cookie"))
        return plans

    def keepalive_plans(self) -> List[AuthPlan]:
        """能用于保活心跳的候选方案：真实登录态 authorization / Cookie。

        API Key 只读，永远不参与保活，避免出现「看着成功其实不算活跃」的假象。
        """
        return [plan for plan in self.auth_plans() if plan.keepalive_capable]

    def read_plans(self) -> List[AuthPlan]:
        """只读接口的候选方案：先用 API Key 之类只读凭证，省掉一次注定失败的请求。"""
        plans = self.auth_plans()
        return ([plan for plan in plans if plan.kind == "api_key"]
                + [plan for plan in plans if plan.kind != "api_key"])


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
    # 本次请求的接口地址与凭证来源，便于详情页展示。
    site: str = ""
    credential: str = ""
    # 保活心跳结果：成功即表示站点已经把「最近动向」刷新成本次执行时间。
    keepalive_ok: bool = False
    keepalive_code: int = -1
    keepalive_message: str = ""
    # 实际命中的接口地址（METHOD URL）与是否拿到了 M-Team 的结构化 JSON。
    endpoint: str = ""
    api_ok: bool = False
    # 本次是否走了「真实登录」（POST /login 换取 authorization）。
    login_ok: bool = False
    login_used: bool = False
    login_message: str = ""
    # 本次真实登录换回的 authorization；只用于回写插件配置，绝不写日志/详情。
    new_token: str = ""


@dataclass
class LoginResult:
    """一次真实登录（POST /login）的结果。"""

    ok: bool = False
    code: int = -1
    message: str = ""
    # 站点在响应头里返回的 authorization（JWT），绝不写入日志。
    token: str = ""
    endpoint: str = ""
    # 还需要哪种二次验证："" / "otp" / "email"。
    need: str = ""
    username: str = ""


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


def totp(secret: Optional[str], now: Optional[float] = None,
         digits: int = 6, period: int = 30) -> str:
    """用 Base32 密钥现算一个 TOTP 动态码（RFC 6238 / SHA1），无第三方依赖。"""
    text = re.sub(r"[\s\-]", "", str(secret or "")).upper().rstrip("=")
    if not text or not re.fullmatch(r"[A-Z2-7]+", text):
        return ""
    padded = text + "=" * (-len(text) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception:  # noqa: BLE001 - 密钥格式不对时不给动态码
        return ""
    counter = int(now if now is not None else time.time()) // max(1, int(period))
    digest = hmac.new(key, int(counter).to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(number % (10 ** int(digits))).zfill(int(digits))


def jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    """解析 JWT 的载荷段；不是 JWT 时返回空字典。"""
    parts = str(token or "").strip().split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment.encode("ascii"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 非法 JWT 不参与过期判断
        return {}
    return data if isinstance(data, Mapping) else {}


def jwt_expires_at(token: Optional[str]) -> Optional[int]:
    """取 JWT 的 exp（毫秒）；解析不出时返回 None。"""
    value = jwt_payload(token).get("exp")
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number * 1000 if number < 100000000000 else number


def jwt_expired(token: Optional[str], now_ms: Optional[int] = None,
                leeway_seconds: int = 60) -> bool:
    """判断 JWT 是否已过期（解析不出有效期时按未过期处理，交给站点裁决）。"""
    expires = jwt_expires_at(token)
    if expires is None:
        return False
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return expires - max(0, int(leeway_seconds)) * 1000 <= now


def extract_token_from_headers(headers: Optional[Mapping[str, str]]) -> str:
    """从登录响应头里取 authorization（少数镜像改名为 token / auth）。"""
    for key, value in (headers or {}).items():
        if str(key).strip().lower() in AUTH_HEADER_NAMES and str(value or "").strip():
            return str(value).strip()
    return ""


def extract_token_from_body(body: Optional[Mapping[str, Any]]) -> str:
    """少数镜像把登录令牌放在响应体里（data.authorization / data.token ...）。"""
    if not isinstance(body, Mapping):
        return ""
    containers: List[Mapping[str, Any]] = []
    data = body.get("data")
    if isinstance(data, Mapping):
        containers.append(data)
    containers.append(body)
    for container in containers:
        for key in AUTH_BODY_KEYS:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


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
    """由站点地址推导 M-Team API 基址（api.<注册域名>/api）。

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


def extract_token_from_cookie(cookie: Optional[str]) -> str:
    """从手填的 Cookie 字符串里解析登录令牌（authorization / token / jwt）。"""
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


def credentials_from_config(config: Optional[Mapping[str, Any]]) -> Credentials:
    """把插件配置页里**手填**的凭证转成馒头凭证。

    只认这些配置键（大小写沿用 MoviePilot 表单的 model 名）：
    username / password / otp_secret / otp_code / email_code / tg_code /
    token（就是 authorization）/ api_key / cookie；空值一律视为未填。
    """
    config = config if isinstance(config, Mapping) else {}

    def _get(*names: str) -> str:
        for name in names:
            value = config.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    token = _get("token", "authorization")
    api_key = _get("api_key", "apikey")
    cookie = _get("cookie")
    username = _get("username")
    password = _get("password")
    sources: List[str] = []
    if token:
        sources.append("手填 authorization")
    if extract_token_from_cookie(cookie):
        sources.append("手填 Cookie 令牌")
    elif cookie:
        sources.append("手填 Cookie")
    if api_key:
        sources.append("手填 API Key（只读）")
    if username and password:
        sources.append("手填账号密码（真实登录）")
    return Credentials(token=token, api_key=api_key, cookie=cookie,
                       username=username, password=password,
                       otp_secret=_get("otp_secret", "otpsecret"),
                       otp_code=_get("otp_code", "otpcode"),
                       email_code=_get("email_code", "emailcode"),
                       tg_code=_get("tg_code", "tgcode"),
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
        return ("（凭证无效或已过期：请在插件配置里重新填「账号 + 密码」或新的 authorization —— "
                "必须是浏览器真实登录后的 authorization（JWT）；API Key 只能读数据、不能保活）")
    if any(word.lower() in text for word in METHOD_NOT_SUPPORTED_KEYWORDS):
        return ("（接口地址可能不对：前后端分离的站点后端在 api.<域名>/api，"
                "请检查本插件的「API 基址」）")
    if any(word.lower() in text for word in PROFILE_NOT_FOUND_KEYWORDS):
        return ("（登录凭证（登录态）已失效：请在插件配置里重新填「账号 + 密码」"
                "（插件会真实登录换新令牌），或填一个新的 authorization）")
    return ""


def login_need_from_body(body: Mapping[str, Any], code: int) -> str:
    """判断登录响应还缺哪种两步验证："" / "otp" / "email" / "tg"。

    馒头前端在 code=1001 时会看 data 里的 ["email"] / ["tg"] / ["otp"]
    决定渲染哪种验证码输入框；插件据此自动发送对应验证码。
    """
    data = (body or {}).get("data")
    items = [str(item).strip().lower() for item in data] if isinstance(data, (list, tuple)) else []
    if "email" in items:
        return "email"
    if "tg" in items or "telegram" in items:
        return "tg"
    if "otp" in items or "app" in items:
        return "otp"
    if code in LOGIN_NEED_EMAIL_CODES:
        return "email"
    if code in LOGIN_NEED_OTP_CODES:
        return "otp"
    return ""


def login_hint_for(code: int, need: str, message: str = "") -> str:
    """把真实登录的失败原因翻译成可操作提示。"""
    if need == "otp":
        return ("（站点要求二步验证：请在插件配置里填「二步验证密钥」（Base32 密钥）"
                "或临时的「动态码」）")
    if need == "email":
        return ("（站点要求邮箱验证码：请把账号注册邮箱收到的 6 位验证码填到插件配置的"
                "「邮箱验证码」后重试；也可以直接填浏览器登录后的 authorization）")
    if need == "tg":
        return "（站点要求 Telegram 验证码：请在插件配置里填「Telegram 验证码」）"
    text = str(message or "").lower()
    if any(word.lower() in text for word in INVALID_API_KEY_KEYWORDS):
        return "（插件配置里的账号或密码不正确）"
    if code == 1 and ("用户名" in str(message) or "密码" in str(message)):
        return "（插件配置里的账号或密码不正确）"
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
    """M-Team API 客户端：真实登录 + 保活心跳（updateLastBrowse）+ 读取最近动向。

    只会调用三个接口：
    * POST /login——只有填了账号密码时才调用，换成真实登录态 authorization；
    * POST /member/updateLastBrowse——保活心跳，只带真实登录态（authorization / Cookie）；
    * GET|POST /member/profile——只读，读取 data.memberStatus.lastBrowse。
    绝不调用 logout / revokeSession 等其它写接口。
    """

    def __init__(self, host: str = DEFAULT_API_HOST, timeout: int = DEFAULT_TIMEOUT,
                 proxy: bool = False, user_agent: str = DEFAULT_USER_AGENT,
                 secret: str = SIGN_SECRET, did: str = "") -> None:
        self.host = normalize_host(host)
        self.timeout = max(5, int(timeout or DEFAULT_TIMEOUT))
        self.proxy = bool(proxy)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.secret = secret or SIGN_SECRET
        # 设备标识：站点把登录态 authorization 绑定到 did（浏览器 localStorage.did），
        # 登录与后续所有请求必须携带同一个 did，否则会被判定为登录态失效。
        self.did = str(did or "").strip() or os.urandom(16).hex()
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
            "did": self.did,
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
        """POST /member/updateLastBrowse：真实登录态下的保活心跳。

        只有带 authorization（真实登录拿到的 JWT）或浏览器 Cookie 的调用才可能成功；
        API Key 打这个接口一律 401，所以这里直接拒掉，避免制造"已保活"的假象。
        """
        plan = plan or AuthPlan()
        if not plan.usable:
            raise MTeamError("缺少可用的登录凭证（真实登录态 authorization / Cookie）")
        if not plan.keepalive_capable:
            raise MTeamError("API Key 只能读取数据，不能用于保活心跳")
        status, _headers, body = self.request("POST", UPDATE_LAST_BROWSE_PATH, fields=[],
                                              plan=plan, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        message = str(body.get("message") or "")
        if status >= 400 and not message:
            message = "HTTP " + str(status)
        ok = status < 400 and code == 0
        if not ok and message:
            message = message + hint_for(message)
        return KeepAliveResult(ok=ok, code=code, message=message,
                               endpoint=self.last_request, auth=plan.name)

    def profile(self, plan: Optional[AuthPlan] = None,
                ts_ms: Optional[int] = None) -> Dict[str, Any]:
        """GET /member/profile，返回原始响应体（405 时自动回退 POST）。"""
        plan = plan or AuthPlan()
        if not plan.usable:
            raise MTeamError("缺少读取凭证（authorization / API Key / Cookie）")
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

    def login(self, credentials: Optional[Credentials] = None,
              ts_ms: Optional[int] = None) -> LoginResult:
        """真实登录：POST /login，从**响应头**取回 authorization（JWT）。

        这是与浏览器完全一致的真实登录：前端登录成功后同样把响应头里的
        authorization 存进 localStorage，并立刻调用 /member/updateLastBrowse。
        """
        credentials = credentials or Credentials()
        if not credentials.can_login:
            raise MTeamError("缺少账号密码，无法执行真实登录")
        fields: List[Tuple[str, Any]] = [("username", credentials.username),
                                        ("password", credentials.password)]
        otp_code = credentials.login_otp_code()
        if otp_code:
            fields.append(("otpCode", otp_code))
        if credentials.email_code:
            fields.append(("emailCode", credentials.email_code))
        if credentials.tg_code:
            fields.append(("tgCode", credentials.tg_code))
        if credentials.turnstile:
            fields.append(("turnstile", credentials.turnstile))
        status, headers, body = self.request("POST", LOGIN_PATH, fields=fields, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        message = str(body.get("message") or "")
        if status >= 400 and not message:
            message = "HTTP " + str(status)
        token = extract_token_from_headers(headers) or extract_token_from_body(body)
        ok = status < 400 and code == 0 and bool(token)
        need = login_need_from_body(body, code) if not ok else ""
        result = LoginResult(ok=ok, code=code, message=message, token=token,
                             endpoint=self.last_request, need=need,
                             username=str(credentials.username or ""))
        if ok:
            identity = extract_member_identity(body)
            if identity.get("username"):
                result.username = str(identity.get("username"))
        return result

    def send_login_email_code(self, username: str,
                              ts_ms: Optional[int] = None) -> SendCodeResult:
        """把登录两步验证码发到账号注册邮箱。

        对应浏览器登录页的「获取验证码」按钮：POST
        /member/sendLoginEmailVerifyCode，只需要 username，站点自己查注册邮箱。
        """
        username = str(username or "").strip()
        if not username:
            raise MTeamError("缺少账号，无法发送邮箱验证码")
        status, _headers, body = self.request(
            "POST", SEND_LOGIN_EMAIL_CODE_PATH,
            fields=[("username", username)], ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        message = str(body.get("message") or "")
        if status >= 400 and not message:
            message = "HTTP " + str(status)
        return SendCodeResult(ok=status < 400 and code == 0, code=code,
                              message=message, endpoint=self.last_request)

    # ---------------------------------------------------------- 组合流程

    def _keepalive_with_plan(self, plan: AuthPlan, failures: List[str]
                             ) -> Optional[KeepAliveResult]:
        """跑一次保活心跳，失败时把原因追加到 failures（不抛异常）。"""
        try:
            result = self.keepalive(plan=plan)
        except MTeamError as error:
            failures.append(plan.name + "：" + str(error))
            return None
        if not result.ok:
            failures.append(plan.name + "：" + (result.message or ("code=" + str(result.code))))
        return result

    def _run_keepalive(self, credentials: Credentials, failures: List[str]
                       ) -> Tuple[Optional[KeepAliveResult], Optional[AuthPlan],
                                  Optional[LoginResult]]:
        """先试已填的真实登录态 authorization，再（必要时）真实登录换新令牌。

        返回值：(保活结果, 命中的认证方案, 真实登录结果或 None)。
        """
        plans = credentials.keepalive_plans()
        for plan in plans:
            token = plan.token.strip()
            if token and credentials.can_login and jwt_expired(token):
                # 令牌已经过期，而且填了账号密码：直接走真实登录换新的。
                failures.append(plan.name + "：authorization 已过期，改用账号密码真实登录")
                continue
            result = self._keepalive_with_plan(plan, failures)
            if result is not None and result.ok:
                return result, plan, None

        if not credentials.can_login:
            if not plans and credentials.api_key:
                failures.append("只填了 API Key：馒头只认真实登录 "
                                "authorization（JWT）的保活心跳，API Key 不能保活")
            return None, None, None

        # 没有可用的登录态：用填写的账号密码真实登录换 authorization。
        try:
            login_result = self.login(credentials=credentials)
        except MTeamError as error:
            failures.append("真实登录：" + str(error))
            return None, None, None
        if not login_result.ok:
            hint = login_hint_for(login_result.code, login_result.need,
                                  login_result.message)
            if login_result.need == "email":
                # 浏览器登录页同样会在这一步点「获取验证码」；插件直接把验证码
                # 发到账号注册邮箱，用户只需把收到的 6 位码填进「邮箱验证码」。
                try:
                    sent = self.send_login_email_code(credentials.username)
                except MTeamError as error:
                    hint = "（站点要求邮箱验证码，自动发送失败：" + str(error) + "）"
                else:
                    if sent.ok:
                        hint = ("（站点要求邮箱验证码：已把 6 位验证码发到账号注册邮箱，"
                                "请填到插件配置的「邮箱验证码」后重试）")
                    else:
                        hint = ("（站点要求邮箱验证码，但发送失败：" +
                                (sent.message or ("code=" + str(sent.code))) + "）")
            failures.append("真实登录：" + (login_result.message or ("code=" + str(login_result.code)))
                            + hint)
            return None, None, login_result
        plan = AuthPlan(name="真实登录 authorization", token=login_result.token, scheme="jwt")
        result = self._keepalive_with_plan(plan, failures)
        if result is not None and result.ok:
            return result, plan, login_result
        return None, None, login_result

    def _fill_from_profile(self, report: LoginReport, plan: AuthPlan) -> LastBrowse:
        """读 /member/profile，把账号信息与最近动向填进 report。"""
        browse = self.last_browse(plan=plan)
        report.last_browse = browse
        # 保活命中的接口优先展示；没有保活时再退回本次读取接口。
        report.endpoint = report.endpoint or self.last_request
        report.api_ok = report.api_ok or self._api_ok
        identity = extract_member_identity(self.last_body)
        if identity.get("id") not in (None, ""):
            report.user_id = identity.get("id")
        if identity.get("username"):
            report.username = str(identity.get("username"))
        return browse

    def fetch_last_browse(self, credentials: Optional[Credentials] = None,
                          site_label: str = "") -> LoginReport:
        """真实登录 + 保活心跳 + 读取最近动向，返回结构化报表。

        流程（与浏览器完全一致）：
        1. 用配置里手填的 authorization（或浏览器 Cookie）调
           POST /member/updateLastBrowse；令牌过期且填了账号密码时直接跳过；
        2. 没有可用登录态时，用配置里填的账号密码 POST /login
           真实登录，把响应头里的 authorization 用于保活；
        3. 保活成功后再读 /member/profile，此时「最近动向」应当就是刚刚；
        4. 保活失败时仍然读一次 profile，把账号**真实的（很可能是几天前的）**
           最近动向展示出来，并说明 API Key 为什么不能保活。
        """
        credentials = credentials or Credentials()
        report = LoginReport(ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             site=site_label, credential=credentials.source)
        if not credentials.usable:
            report.message = ("还没有填任何可用的凭证：" + KEEPALIVE_FIX_HINT)
            return report

        failures: List[str] = []
        keepalive, winner, login_result = self._run_keepalive(credentials, failures)
        if login_result is not None:
            report.login_used = True
            report.login_ok = bool(login_result.ok)
            report.login_message = login_result.message
            if login_result.endpoint:
                report.endpoint = login_result.endpoint
            if login_result.ok and login_result.token:
                report.new_token = login_result.token

        if keepalive is not None and keepalive.ok and winner is not None:
            report.method = "login+keepalive" if report.login_used else "keepalive"
            report.keepalive_ok = True
            report.keepalive_code = keepalive.code
            report.keepalive_message = keepalive.message
            report.code = keepalive.code
            report.endpoint = keepalive.endpoint or report.endpoint
            report.credential = (credentials.source + " · " + winner.name).strip(" ·")
            try:
                browse = self._fill_from_profile(report, winner)
            except MTeamError as error:
                report.ok = False
                report.message = "保活心跳已成功，但读取最近动向失败：" + str(error)
                return report
            if report.user_id is None and report.username == "":
                report.username = login_result.username if login_result else ""
            if browse.empty:
                report.ok = False
                report.message = "保活心跳已成功，但站点未返回最近动向"
                return report
            report.ok = True
            report.message = ("已真实登录并保活，最近动向：" + browse.text
                              if report.login_used else "已保活，最近动向：" + browse.text)
            if browse.ms is not None:
                age = int(time.time() * 1000) - int(browse.ms)
                if age > 5 * 60 * 1000:
                    # 保活接口返回成功但时间没刷新：站点缓存，或该登录态没有保活权限。
                    prefix = ("已真实登录并保活，但" if report.login_used
                              else "保活心跳返回成功，但")
                    report.message = (prefix + "最近动向仍是 "
                                      + (browse.relative or browse.text)
                                      + "；站点可能缓存了该接口，或该凭证没有保活权限。")
            return report

        report.method = "keepalive-failed"
        report.keepalive_ok = False
        report.keepalive_code = keepalive.code if keepalive is not None else -1
        report.keepalive_message = keepalive.message if keepalive is not None else ""
        report.code = report.keepalive_code
        if keepalive is not None and keepalive.endpoint:
            report.endpoint = keepalive.endpoint

        # 保活失败也要把站点里的**真实**最近动向读出来，方便确认账号到底活没活。
        for plan in credentials.read_plans():
            try:
                browse = self._fill_from_profile(report, plan)
            except MTeamError as error:
                failures.append("读取最近动向（" + plan.name + "）：" + str(error))
                continue
            report.credential = (credentials.source + " · " + plan.name).strip(" ·")
            if not browse.empty:
                report.message = ("保活心跳未成功（" + ("；".join(failures) or "未知原因") + "）："
                                  "账号当前最近动向仍是 " + (browse.relative or browse.text)
                                  + "。" + KEEPALIVE_FIX_HINT)
            break
        if not report.message:
            report.message = ("保活心跳未成功（" + ("；".join(failures) or "未知原因") + "）："
                              + KEEPALIVE_FIX_HINT)
        if not report.username and login_result is not None:
            report.username = login_result.username
        return report
