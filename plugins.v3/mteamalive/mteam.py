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
import re
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
    # 站点可能带相对时间后缀：2026-10-19 17:31:48 (1 分鐘前)
    text = re.sub(r"\s*[（(][^()（）]*[）)]\s*$", "", text).strip()
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


def format_activity(profile: Mapping[str, Any]) -> str:
    """按站点写法展示最近动向：2026-10-19 17:31:48（1 分鐘前）。"""
    raw = str(profile.get("last_browse") or profile.get("last_login") or "").strip()
    moments = parse_datetime(raw)
    if moments is None:
        return raw
    minutes = int((datetime.now() - moments).total_seconds() // 60)
    if minutes < 1:
        relative = "剛剛"
    elif minutes < 60:
        relative = f"{minutes} 分鐘前"
    elif minutes < 60 * 24:
        relative = f"{minutes // 60} 小時前"
    else:
        relative = f"{minutes // (60 * 24)} 天前"
    return f"{moments.strftime('%Y-%m-%d %H:%M:%S')}（{relative}）"


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
    try:
        import requests
    except ImportError:
        # 极少见的精简环境没有 requests：保活照常，只是读不到统计
        return None, "当前环境缺少 requests 库，读不到账号统计"

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
  const setValue = (node, value) => {
    const setter = Object.getOwnPropertyDescriptor(
      node.constructor.prototype, 'value').set;
    setter.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const visible = (node) => Boolean(node) &&
    (node.offsetParent !== null || node.getClientRects().length > 0);

  // 密码框最稳：type=password
  const passInput = [...document.querySelectorAll('input[type=password]')].find(visible);
  if (!passInput) return false;

  // 用户名框：同表单里密码框前面的文本框，或按 placeholder / autocomplete 猜
  const form = passInput.form || document;
  const candidates = [
    form.querySelector('input#username'),
    form.querySelector('input[name=username]'),
    form.querySelector('input[autocomplete=username]'),
    form.querySelector('input[type=email]'),
    form.querySelector('input[type=text]'),
    ...[...form.querySelectorAll('input')].filter((node) =>
      /帳號|账号|用戶|用户|郵箱|邮箱|email|account|username/i.test(node.placeholder || '')),
  ].filter((node) => node && node !== passInput && visible(node));

  const userInput = candidates[0];
  if (!userInput) return false;

  setValue(userInput, username);
  setValue(passInput, password);

  const scope = passInput.form || document;
  const button = scope.querySelector('button[type=submit]') ||
                 [...scope.querySelectorAll('button, [role=button]')].find((node) =>
                   /登錄|登录|登入|login|sign in/i.test((node.textContent || '').trim()));
  if (!button) return false;
  button.click();
  return true;
}
"""

OTP_JS_FILL = """
(code) => {
  const visible = (node) => Boolean(node) &&
    (node.offsetParent !== null || node.getClientRects().length > 0);
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  const target = inputs.find((node) => /驗證碼|验证码|verification|authenticator|otp/i
                    .test(node.placeholder || '')) ||
                 inputs.find((node) => node.autocomplete === 'one-time-code') ||
                 inputs.find((node) => node.maxLength === 6 && node.type !== 'password') ||
                 document.querySelector('input#otp-code, input[name=otp-code]');
  if (!target) return false;
  const setter = Object.getOwnPropertyDescriptor(
    target.constructor.prototype, 'value').set;
  setter.call(target, code);
  target.dispatchEvent(new Event('input', { bubbles: true }));
  target.dispatchEvent(new Event('change', { bubbles: true }));

  const form = target.form || document;
  const button = form.querySelector('button[type=submit]') ||
                 [...form.querySelectorAll('button, [role=button]')].find((node) =>
                   /登錄|登录|登入|確認|确认|下一步|next/i.test((node.textContent || '').trim()));
  if (!button) return false;
  button.click();
  return true;
}
"""

HAS_LOGIN_FORM_JS = """
() => {
  const pick = (selector) => [...document.querySelectorAll(selector)]
    .some((node) => node.offsetParent !== null || node.getClientRects().length > 0);
  return pick('input[type=password]') || pick('input#username') || pick('input[name=username]')
    || pick('input[placeholder*="帳號"]') || pick('input[placeholder*="账号"]');
}
"""

CHALLENGE_JS = """
() => {
  const text = (document.body && document.body.innerText || '').toLowerCase();
  return /just a moment|checking your browser|正在进行安全检测|正在验证您是否是真人|verify you are human/.test(text)
    || Boolean(document.querySelector('iframe[src*=challenges.cloudflare.com], .cf-turnstile, #cf-chl-widget'));
}
"""

CLICK_CF_JS = """
() => {
  const frame = document.querySelector('iframe[src*=challenges.cloudflare.com]');
  if (frame) {
    try {
      const doc = frame.contentDocument;
      const box = doc && doc.querySelector('input[type=checkbox]');
      if (box) { box.click(); return true; }
    } catch (error) {}
  }
  const turnstile = document.querySelector('.cf-turnstile, #cf-chl-widget');
  if (turnstile) { turnstile.click(); return true; }
  return false;
}
"""

PAGE_SNAPSHOT_JS = """
() => {
  const body = (document.body && document.body.innerText) || '';
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    text: body.replace(/\\s+/g, ' ').slice(0, 300),
    hasForm: Boolean(document.querySelector('input#username, input[name=username], input#password')),
  });
}
"""


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

        # M-Team 是纯前端 SPA：先出骨架，再从 static.m-team.cc 拉 JS 才渲染登录表单。
        # 所以这里等选择器（最多 40 秒），期间遇到 CF/雷池 检测页就点一下勾选框继续等。
        ready = False
        waited_ms = 0
        for _ in range(20):
            try:
                if page.evaluate(LOGGED_IN_JS):
                    ready = True
                    break
                if page.evaluate(HAS_LOGIN_FORM_JS):
                    ready = True
                    break
                if page.evaluate(CHALLENGE_JS):
                    try:
                        page.evaluate(CLICK_CF_JS)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break
            waited_ms += 2000
        if not ready:
            snapshot = {}
            try:
                raw = page.evaluate(PAGE_SNAPSHOT_JS) or "{}"
                snapshot = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                snapshot = {}
            text_body = str(snapshot.get("text") or "")
            title = str(snapshot.get("title") or "")
            note.append(f"等了 {waited_ms // 1000} 秒没等到登录表单")
            if "資源檔載入失敗" in text_body or "资源档加载失败" in text_body or "Resource file loading failed" in text_body:
                note.append("站点提示前端资源加载失败：容器访问 static.m-team.cc 不通，"
                            "请在容器里放行该域名或配代理")
            elif any(word in text_body.lower() for word in ("just a moment", "checking your browser",
                                                            "安全检测", "verify you are human")):
                note.append("页面停在 Cloudflare / 雷池 检测页，没能自动放行")
            elif title and not snapshot.get("hasForm"):
                note.append("页面停在 SPA 骨架（前端 JS 没跑起来）：容器访问 static.m-team.cc 可能超时")
            if snapshot:
                note.append(f"页面现场：{json.dumps(snapshot, ensure_ascii=False)[:400]}")
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
        if ok:
            # 登录成功后再真正访问几个页面：M-Team 的「最近浏览」是服务端按请求记录的，
            # 只停在登录页有时不会刷新（日志里出现过「登录成功但最近浏览没变」）。
            for path in ("/index", "/profile"):
                try:
                    page.goto(base + path, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1500)
                except Exception:
                    continue
        session_payload = capture_session(page) if ok else {}
        note_text = "；".join(note)
        if ok:
            note_text = (note_text + "；已访问首页确认登录").strip("；")
        return {"ok": ok, "note": note_text, "title": title,
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
