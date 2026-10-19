"""M-Team 保活与统计：TOTP 生成、浏览器登录、API 读取账号数据。

保活必须是**真实浏览器登录**（M-Team 的「连续 40 天不登录删号」只有真实登录才重置），
所以这里用 MoviePilot 自带的 PlaywrightHelper 在本机登录，不走 API ping。
统计信息优先用 API Key 读（不需要浏览器）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

DEFAULT_SITE = "https://kp.m-team.cc"
DEFAULT_API_BASE = "https://api.m-team.cc"


# ------------------------------------------------------------------ TOTP
def totp_now(secret: str, at: Optional[float] = None, digits: int = 6,
             period: int = 30) -> str:
    """按 RFC 6238 生成 6 位动态验证码（base32 secret，兼容空格与大小写）。"""
    key = "".join(str(secret or "").split()).upper()
    if not key:
        return ""
    padding = "=" * ((8 - len(key) % 8) % 8)
    try:
        raw = base64.b32decode(key + padding, casefold=True)
    except Exception:
        return ""
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ------------------------------------------------------------------ 统计解析
def human_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return ""


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def parse_profile(payload: Any) -> Dict[str, Any]:
    """把 /api/member/profile 的返回解析成页面要用的字段（容错优先）。"""
    body = payload if isinstance(payload, Mapping) else {}
    data = body.get("data") if isinstance(body.get("data"), Mapping) else body
    status = data.get("memberStatus") if isinstance(data.get("memberStatus"), Mapping) else {}
    count = data.get("memberCount") if isinstance(data.get("memberCount"), Mapping) else {}

    ratio = _first(count, "shareRate", "ratio", "shareRatio")
    try:
        ratio_value = float(ratio) if ratio is not None else None
    except (TypeError, ValueError):
        ratio_value = None
    uploaded = _first(count, "uploaded", "upload")
    downloaded = _first(count, "downloaded", "download")
    bonus = _first(count, "bonus", "bonusValue")
    return {
        "id": str(_first(data, "id") or ""),
        "username": str(_first(data, "username", "name") or ""),
        "email": str(_first(data, "email") or ""),
        "level": str(_first(data, "memberLevel", "level", "role") or ""),
        "last_login": str(_first(status, "lastLogin", "last_login") or ""),
        "last_browse": str(_first(status, "lastBrowse", "last_browse") or ""),
        "uploaded": human_size(uploaded),
        "downloaded": human_size(downloaded),
        "bonus": str(bonus if bonus is not None else ""),
        "ratio": (f"{ratio_value:.3f}" if ratio_value is not None else ""),
        "ratio_value": ratio_value,
        "warning": str(_first(count, "warning", "warnings") or ""),
    }


# ------------------------------------------------------------------ 最近动向
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M:%S", "%Y-%m-%d")


def parse_datetime(value: Any) -> Optional[datetime]:
    """把 M-Team 返回的时间（字符串 / 毫秒时间戳）解析成 datetime（无时区按本地时区）。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / (1000 if float(value) > 1e11 else 1)
        try:
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def last_activity(profile: Mapping[str, Any]) -> Tuple[Optional[datetime], str]:
    """取「最近动向」：优先 lastBrowse（最近浏览），退回 lastLogin。"""
    browse = parse_datetime(profile.get("last_browse"))
    if browse is not None:
        return browse, "最近浏览"
    login = parse_datetime(profile.get("last_login"))
    if login is not None:
        return login, "最近登录"
    return None, ""


def activity_minutes_ago(profile: Mapping[str, Any]) -> Optional[float]:
    """最近动向距今多少分钟；读不到返回 None。"""
    moment, _ = last_activity(profile)
    if moment is None:
        return None
    delta = datetime.now() - moment
    return delta.total_seconds() / 60


# ------------------------------------------------------------------ API 读取
def fetch_profile(api_key: str, api_base: str = DEFAULT_API_BASE,
                  timeout: int = 20) -> Tuple[Optional[Dict[str, Any]], str]:
    """用 API Key 读账号详情，返回 (profile, 错误说明)。"""
    import requests

    key = str(api_key or "").strip()
    if not key:
        return None, "未配置 API Key"
    url = str(api_base or DEFAULT_API_BASE).rstrip("/") + "/api/member/profile"
    try:
        response = requests.post(url, headers={"x-api-key": key,
                                               "Content-Type": "application/json",
                                               "Accept": "application/json"},
                                 json={}, timeout=timeout)
    except Exception as error:
        return None, f"请求异常：{error}"
    if response.status_code in {401, 403}:
        return None, f"API Key 无效（HTTP {response.status_code}）"
    if response.status_code >= 400:
        return None, f"接口返回 HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, "接口返回的不是 JSON"
    code = payload.get("code") if isinstance(payload, dict) else None
    if code not in (None, 0, "0"):
        return None, f"接口返回异常：{payload.get('message') or code}"
    parsed = parse_profile(payload)
    if not parsed.get("username") and not parsed.get("id"):
        return None, "接口没有返回账号信息"
    return parsed, ""


# ------------------------------------------------------------------ 浏览器保活
LOGIN_JS_FILL = """
(args) => {
  const [username, password] = args;
  const userInput = document.querySelector('input#username, input[name=username]');
  const passInput = document.querySelector('input#password, input[name=password]');
  if (!userInput || !passInput) return false;
  const setValue = (node, value) => {
    const setter = Object.getOwnPropertyDescriptor(
      node.constructor.prototype, 'value').set;
    setter.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  };
  setValue(userInput, username);
  setValue(passInput, password);
  const form = passInput.form || document;
  const button = form.querySelector('button[type=submit]') ||
                 document.querySelector('button[type=submit]');
  if (button) { button.click(); return true; }
  return false;
}
"""

OTP_JS_FILL = """
(code) => {
  const input = document.querySelector(
    'input#otp-code, input[name=otp-code], input[id*=otp], input[name*=otp]');
  if (!input) return false;
  const setter = Object.getOwnPropertyDescriptor(
    input.constructor.prototype, 'value').set;
  setter.call(input, code);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const form = input.form || document;
  const button = form.querySelector('button[type=submit]') ||
                 document.querySelector('button[type=submit]') ||
                 [...document.querySelectorAll('button')].find((node) =>
                    (node.textContent || '').includes('確認'));
  if (button) { button.click(); return true; }
  return false;
}
"""

# 登录失败时页面上的提示 → 给用户看得懂的结论
_LOGIN_HINTS = (
    (("郵箱驗證", "邮箱验证", "驗證碼已發送", "验证码已发送", "check your email",
      "verification code has been sent"), "站点要求邮箱验证码（风控触发），插件无法自动完成"),
    (("安全問題", "安全问题", "security question"), "站点要求回答安全问题，插件无法自动完成"),
    (("密碼錯誤", "密码错误", "incorrect password", "帳號或密碼"), "账号或密码错误"),
    (("帳號已被封", "账号已被封", "banned"), "账号被封禁"),
)

LOGGED_IN_JS = """
() => {
  try {
    const auth = window.localStorage.getItem('auth');
    return Boolean(auth) || location.pathname.startsWith('/index');
  } catch (error) {
    return location.pathname.startsWith('/index');
  }
}
"""


CAPTURE_SESSION_JS = """
() => {
  const out = {};
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key) out[key] = localStorage.getItem(key);
    }
  } catch (error) {}
  return out;
}
"""

RESTORE_SESSION_JS = """
(items) => {
  try {
    Object.entries(items || {}).forEach(([key, value]) => {
      if (value !== null && value !== undefined) localStorage.setItem(key, value);
    });
    return true;
  } catch (error) {
    return false;
  }
}
"""


def capture_session(page: Any) -> Dict[str, str]:
    """把当前页面的 localStorage 抓下来（M-Team 的登录态就存这里）。"""
    try:
        items = page.evaluate(CAPTURE_SESSION_JS) or {}
    except Exception:
        return {}
    if not isinstance(items, dict):
        return {}
    return {str(key): str(value) for key, value in items.items() if value is not None}


def restore_session(page: Any, base_url: str, items: Mapping[str, Any],
                    wait_ms: int = 3000) -> bool:
    """先打开站点，再把上次保存的 localStorage 写回去并刷新，尽量伪装成同一台设备。"""
    if not items:
        return False
    base = str(base_url or DEFAULT_SITE).rstrip("/")
    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    try:
        applied = bool(page.evaluate(RESTORE_SESSION_JS, dict(items)))
    except Exception:
        return False
    if not applied:
        return False
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(max(800, int(wait_ms)))
    except Exception:
        pass
    return True


def keepalive_login(username: str, password: str, totp_secret: str,
                    site_url: str = DEFAULT_SITE, timeout: int = 90,
                    logger: Any = None,
                    saved_session: Optional[Mapping[str, Any]] = None
                    ) -> Tuple[bool, str, Dict[str, str]]:
    """用本机浏览器登录 M-Team 保活，返回 (是否成功, 说明, 本次的登录态)。

    给了 ``saved_session`` 就先尝试恢复上次的登录态（同一「设备」、也不再天天登录，
    能显著降低异地/新设备风控）；恢复失败再退回账号密码 + TOTP。
    """
    try:
        from app.helper.browser import PlaywrightHelper
    except Exception:
        return False, "本机没有 Playwright（MoviePilot 浏览器依赖缺失），无法保活", {}
    if not hasattr(PlaywrightHelper, "action"):
        return False, "当前 MoviePilot 版本的 PlaywrightHelper 不支持 action()，无法保活", {}

    base = str(site_url or DEFAULT_SITE).rstrip("/")
    login_url = f"{base}/login"
    saved_cookie = str((saved_session or {}).get("__cookie__") or "").strip()
    user = str(username or "").strip()
    if not user or not str(password or "").strip():
        return False, "未配置 M-Team 账号或密码", {}

    def callback(page: Any) -> Dict[str, Any]:
        note = []
        reused = False
        if saved_session and restore_session(page, base, saved_session):
            if page.evaluate(LOGGED_IN_JS):
                note.append("复用上次登录态")
                reused = True
            else:
                note.append("上次登录态已失效")
        if reused:
            session_payload = capture_session(page)
            return {"ok": True, "note": "；".join(note), "reused": True,
                    "session": session_payload, "title": "", "url": ""}
        try:
            page.goto(login_url, wait_until="domcontentloaded",
                      timeout=max(20000, int(timeout * 1000)))
        except Exception as error:
            note.append(f"打开登录页失败：{error}")
            return {"ok": False, "note": "；".join(note)}
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if not page.evaluate(LOGGED_IN_JS):
            filled = False
            try:
                filled = bool(page.evaluate(LOGIN_JS_FILL, [user, str(password or "")]))
            except Exception as error:
                note.append(f"填写账号失败：{error}")
            if not filled:
                note.append("没找到账号密码输入框")
                return {"ok": False, "note": "；".join(note)}
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass
        # 2FA：需要时填 TOTP
        for _ in range(2):
            if page.evaluate(LOGGED_IN_JS):
                break
            code = totp_now(totp_secret)
            if not code:
                note.append("需要 2FA 但没有配置 TOTP 密钥")
                break
            try:
                if page.evaluate(OTP_JS_FILL, code):
                    note.append("已提交 TOTP")
                    page.wait_for_timeout(4000)
                else:
                    page.wait_for_timeout(2000)
            except Exception as error:
                note.append(f"提交 2FA 失败：{error}")
                break
        ok = False
        try:
            ok = bool(page.evaluate(LOGGED_IN_JS))
        except Exception:
            ok = False
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass
        if not ok:
            try:
                body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                body = ""
            lowered = str(body)
            for keywords, reason in _LOGIN_HINTS:
                if any(word in lowered for word in keywords):
                    note.append(reason)
                    break
        session_payload = capture_session(page) if ok else {}
        return {"ok": ok, "note": "；".join(note), "title": title,
                "url": getattr(page, "url", ""), "session": session_payload}

    try:
        outcome = PlaywrightHelper().action(url="about:blank", callback=callback,
                                            cookies=saved_cookie or None,
                                            headless=True, timeout=max(60, int(timeout)))
    except Exception as error:
        return False, f"启动本机浏览器失败：{error}", {}
    if not outcome:
        return False, "本机浏览器没有返回结果", {}
    session_out = outcome.get("session") if isinstance(outcome.get("session"), dict) else {}
    if outcome.get("ok"):
        note = outcome.get("note") or ""
        prefix = "登录成功" if not outcome.get("reused") else "登录态复用成功"
        stamp = datetime.now().strftime("%m-%d %H:%M")
        return True, f"{prefix}（{stamp}）{('；' + note) if note else ''}", session_out
    return (False, "登录未成功" + (f"：{outcome.get('note')}" if outcome.get("note") else ""),
            session_out)
