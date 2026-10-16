# -*- coding: utf-8 -*-
"""M-Team（馒头）登录 + 真实「最近动向」查询核心逻辑。

本模块不依赖 MoviePilot，可单独测试与复用。

接口契约（2026-10 从 kp.m-team.cc 前端 bundle 逆向并用真实请求验证）：

* API 基址 https://api.m-team.cc/api ，备用 https://api.m-team.io/api 。
* 签名算法：base64(HMAC_SHA1(secret, "METHOD&path&ts_ms"))，其中 path **包含** /api 前缀；
  表单字段名用的是站点自身的拼写 _sgin（不是 _sign）。
* GET 把 _timestamp / _sgin 放在 URL query，POST 放在 multipart 表单里。
* 其它请求头：ts（秒）、version、webVersion，登录成功后由响应头返回 authorization。
* 登录成功（code == 0）时 authorization 只在**响应头**里返回，前端把它写进 localStorage；
  /member/profile 的 data.memberStatus.lastBrowse 即页面上的「最近动向」。
* 前端登录成功后**立刻**调用 /member/updateLastBrowse 把该时间刷成当前时间；本插件刻意
  不调用它，从而拿到真实的最近动向。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
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
# 前端登录成功后调用的接口，会把「最近动向」刷成当前时间；本插件永不调用，仅用于自检。
UPDATE_LAST_BROWSE_PATH = "/member/updateLastBrowse"
# 2FA 校验字段：verifyType -> 登录表单字段名。
VERIFY_FIELDS = {"otp": "otpCode", "email": "emailCode", "tg": "tgCode"}
VERIFY_LABELS = {"otp": "动态验证码(OTP)", "email": "邮箱验证码", "tg": "Telegram 验证码"}
# 服务端要求人机验证（Turnstile）时的提示特征。
TURNSTILE_HINT_KEYWORDS = ("人機驗證", "人机验证", "turnstile", "驗證失敗", "验证失败")
PROFILE_NOT_FOUND_KEYWORDS = ("無效的憑證", "无效的凭证", "Full authentication")

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
        chunks.append(("Content-Disposition: form-data; name=\"" + str(name) + "\"\r\n\r\n"
                       ).encode("utf-8"))
        chunks.append(("" if value is None else str(value)).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(("--" + boundary + "--\r\n").encode("utf-8"))
    return "multipart/form-data; boundary=" + boundary, b"".join(chunks)


# --------------------------------------------------------------------- TOTP


def _normalize_base32(secret: str) -> str:
    cleaned = "".join(str(secret).split()).replace("-", "").upper().rstrip("=")
    return cleaned + "=" * ((-len(cleaned)) % 8)


def totp(secret: str, at: Optional[float] = None, digits: int = 6,
         period: int = 30) -> str:
    """按 RFC 6238 计算 TOTP（HMAC-SHA1），用于自动补全 OTP 动态码。"""
    if not secret:
        raise ValueError("OTP 密钥为空")
    key = base64.b32decode(_normalize_base32(secret), casefold=True)
    counter = int((time.time() if at is None else float(at)) // int(period))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** int(digits))).zfill(int(digits))


# --------------------------------------------------------------------- 结果


class MTeamError(RuntimeError):
    """网络 / 协议层错误（登录被拒等业务错误用返回值表达，不抛异常）。"""


@dataclass
class LoginResult:
    ok: bool = False
    code: int = -1
    message: str = ""
    authorization: str = ""
    did: str = ""
    verify_types: List[str] = field(default_factory=list)
    user_id: Any = None
    username: str = ""


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
class LoginReport:
    """一轮「登录 + 读取最近动向」的结果。"""

    ok: bool = False
    message: str = ""
    method: str = ""
    username: str = ""
    user_id: Any = None
    last_browse: LastBrowse = field(default_factory=LastBrowse)
    verify_types: List[str] = field(default_factory=list)
    code: int = -1
    ran_at: str = ""


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


def _to_ms(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # 10 位按秒、13 位按毫秒处理。
    return number * 1000 if number < 10 ** 11 else number


def _parse_datetime(text: str, tz_hours: int = 8) -> Optional[datetime]:
    """解析接口返回的时间字符串；无时区的按站点所在地（UTC+8）理解。"""
    value = text.strip().replace("/", "-")
    local = timezone(timedelta(hours=tz_hours))
    for candidate in (value, value.replace("T", " ").replace("Z", "")):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=local)
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
    """把接口返回的 lastBrowse 归一化成「YYYY-MM-DD HH:mm:ss」+ 相对时间。

    站点可能返回毫秒时间戳或 ISO 字符串，这里都兼容；无法解析时原样回显。
    """
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


def hint_for(message: str) -> str:
    """把站点的原始错误翻译成一句可操作的提示。"""
    text = str(message or "").lower()
    if any(word.lower() in text for word in TURNSTILE_HINT_KEYWORDS):
        return "（站点要求 Cloudflare 人机验证，HTTP 直连无法自动完成；建议改填 API Key 后重试）"
    if any(word.lower() in text for word in PROFILE_NOT_FOUND_KEYWORDS):
        return "（登录凭证已失效，请检查账号密码或重新获取 API Key）"
    return ""


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
    """M-Team API 客户端：登录 + 读取真实「最近动向」。

    刻意只调用 /login 与 /member/profile 两个接口，**永不**调用
    /member/updateLastBrowse，避免把真实的最近动向刷成当前时间。
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
        handlers: List[Any] = []
        if not self.proxy:
            # 与 MoviePilot 其它插件一致：未勾选代理时显式绕过系统代理。
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ---------------------------------------------------------- 底层请求

    def request(self, method: str, path: str, fields: Optional[Sequence[Tuple[str, Any]]] = None,
                query: Optional[Mapping[str, Any]] = None, token: str = "", api_key: str = "",
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
            suffix = ""
            if query:
                suffix = "?" + urllib.parse.urlencode(dict(query))
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
        if token:
            headers["authorization"] = token
        if api_key:
            headers["x-api-key"] = api_key
        if content_type:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
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

        header_map = {}
        for key, value in (raw_headers or {}).items():
            header_map[str(key).lower()] = str(value)
        return status, header_map, _decode_json(raw_body)

    # ---------------------------------------------------------- 业务接口

    def login(self, username: str, password: str, otp_code: str = "", email_code: str = "",
              tg_code: str = "", turnstile: str = "",
              ts_ms: Optional[int] = None) -> LoginResult:
        """POST /login，返回原始业务结果（含 2FA 提示）。"""
        if not username or not password:
            raise MTeamError("缺少账号或密码")
        fields: List[Tuple[str, Any]] = [
            ("username", username),
            ("password", password),
            ("turnstile", turnstile or ""),
        ]
        if otp_code:
            fields.append(("otpCode", otp_code))
        if email_code:
            fields.append(("emailCode", email_code))
        if tg_code:
            fields.append(("tgCode", tg_code))
        status, headers, body = self.request("POST", LOGIN_PATH, fields=fields, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        data = body.get("data")
        verify_types: List[str] = []
        if code == 1001 and isinstance(data, list):
            verify_types = [str(item) for item in data]
        detail = data if isinstance(data, Mapping) else {}
        token = str(headers.get("authorization") or "").strip()
        if not token:
            token = str(detail.get("token") or detail.get("authorization") or "").strip()
        message = str(body.get("message") or "")
        if status >= 400 and not message:
            message = "HTTP " + str(status)
        return LoginResult(
            ok=(status < 400 and code == 0 and bool(token)),
            code=code,
            message=message,
            authorization=token,
            did=str(headers.get("did") or "").strip(),
            verify_types=verify_types,
            user_id=detail.get("id"),
            username=str(detail.get("username") or ""),
        )

    def profile(self, token: str = "", api_key: str = "",
                ts_ms: Optional[int] = None) -> Dict[str, Any]:
        """GET /member/profile，返回原始响应体。"""
        if not token and not api_key:
            raise MTeamError("缺少 authorization 凭证")
        status, _headers, body = self.request("GET", PROFILE_PATH, token=token, api_key=api_key,
                                              ts_ms=ts_ms)
        if status >= 400 and not body.get("message"):
            body = dict(body)
            body["code"] = _as_int(body.get("code"))
            body["message"] = "HTTP " + str(status)
        return body

    def last_browse(self, token: str = "", api_key: str = "",
                    ts_ms: Optional[int] = None) -> LastBrowse:
        """读取「最近动向」——只读接口，绝不触发 updateLastBrowse。"""
        body = self.profile(token=token, api_key=api_key, ts_ms=ts_ms)
        code = _as_int(body.get("code"))
        if code != 0:
            message = str(body.get("message") or "读取用户资料失败")
            raise MTeamError(message + hint_for(message))
        return format_last_browse(extract_last_browse(body))

    # ---------------------------------------------------------- 组合流程

    def fetch_last_browse(self, username: str = "", password: str = "", api_key: str = "",
                          otp_secret: str = "", otp_code: str = "", email_code: str = "",
                          tg_code: str = "") -> LoginReport:
        """登录（或直接用 API Key）+ 读取真实最近动向，返回结构化报表。"""
        report = LoginReport(ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if api_key:
            report.method = "api_key"
            report.username = username
            report.last_browse = self.last_browse(api_key=api_key)
            report.ok = True
            report.code = 0
            report.message = "已通过 API Key 读取最近动向"
            return report

        if not username or not password:
            raise MTeamError("请先填写 M-Team 账号与密码，或填写 API Key")

        report.method = "password"
        report.code = -1
        primary_otp = otp_code or (totp(otp_secret) if otp_secret else "")
        result = self.login(username, password, otp_code=primary_otp,
                            email_code=email_code, tg_code=tg_code)
        if not result.ok and result.code in (1001, 1002):
            report.verify_types = list(result.verify_types)
            if not report.verify_types:
                report.verify_types = ["email"] if result.code == 1002 else ["otp"]
            dynamic = otp_code or (totp(otp_secret) if otp_secret else "")
            retry_codes = {"otpCode": dynamic, "emailCode": email_code, "tgCode": tg_code}
            if any(retry_codes.get(VERIFY_FIELDS[name]) for name in report.verify_types):
                result = self.login(
                    username, password,
                    otp_code=retry_codes["otpCode"] if "otp" in report.verify_types else "",
                    email_code=retry_codes["emailCode"] if "email" in report.verify_types else "",
                    tg_code=retry_codes["tgCode"] if "tg" in report.verify_types else "",
                )
            else:
                labels = "、".join(VERIFY_LABELS.get(name, name) for name in report.verify_types)
                report.code = result.code
                report.message = "站点要求二次验证（" + labels + "），请填写对应验证码或 OTP 密钥"
                return report

        if not result.ok:
            report.code = result.code
            report.message = (result.message or "登录失败") + hint_for(result.message)
            return report

        report.user_id = result.user_id
        report.username = result.username or username
        report.last_browse = self.last_browse(token=result.authorization)
        report.ok = True
        report.code = 0
        if report.last_browse.empty:
            report.message = "登录成功，但站点未返回最近动向"
        else:
            report.message = "最近动向：" + report.last_browse.text
        return report
