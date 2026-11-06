"""瓷器（HDChina）：首页取 x-csrf，调用签到接口并刷新站点 Cookie。

HDChina 的登录 Cookie 中只有 ``hdchina=`` 这一段在签到后仍然有效。站点在打开首页时会
下发新的 ``hdchina`` 值，因此签到请求必须使用响应里的新 Cookie，而不是继续使用站点管理
中的整套旧 Cookie。签到接口返回 JSON，只接受 ``state == "success"``。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Tuple


BASE = "https://hdchina.org"
INDEX_URL = f"{BASE}/index.php"
SIGNIN_URL = f"{BASE}/plugin_sign-in.php?cmd=signin"

_COOKIE_RE = re.compile(r"(?:^|;\s*)(hdchina=[^;]*)", re.IGNORECASE)
_SIGNED_RE = re.compile(
    r"<a\b(?=[^>]*\bclass\s*=\s*[\"'][^\"']*\blabel\b[^\"']*\blabel-default\b[^\"']*[\"'])"
    r"[^>]*>\s*(?:已签到|已簽到)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_CSRF_RE = re.compile(
    r"<meta\b(?=[^>]*\bname\s*=\s*[\"']x-csrf[\"'])"
    r"(?=[^>]*\bcontent\s*=\s*[\"']([^\"']+)[\"'])[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_LOGIN_RE = re.compile(
    r"<form\b(?=[^>]*\baction\s*=\s*[\"'][^\"']*(?:takelogin|login)\.php[^\"'])"
    r"[^>]*>.*?<input\b[^>]*\btype\s*=\s*[\"']password[\"']",
    re.IGNORECASE | re.DOTALL,
)


def _hdchina_cookie(raw: str) -> str:
    """只保留 HDChina 签到后仍然有效的 hdchina Cookie。"""
    match = _COOKIE_RE.search(str(raw or ""))
    return match.group(1) if match else ""


def _response_cookie(response: Any, fallback: str) -> str:
    """优先使用首页响应下发的新 hdchina Cookie。"""
    cookies = getattr(response, "cookies", None)
    try:
        values = cookies.get_dict() if cookies is not None else {}
    except Exception:
        values = {}
    value = values.get("hdchina") if isinstance(values, Mapping) else ""
    return f"hdchina={value}" if value else fallback


def _is_login_page(page: str) -> bool:
    return bool(_LOGIN_RE.search(page or ""))


class HDChina:
    site_url = "hdchina.org"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        cookie = _hdchina_cookie(str(site.get("cookie") or ""))
        if not cookie:
            return False, "签到失败：Cookie 已失效（缺少 hdchina=）"

        response = ctx.get(INDEX_URL, headers={"Cookie": cookie})
        if response is None or response.status_code != 200:
            return False, "签到失败：请检查站点连通性"
        page = str(getattr(response, "text", "") or "")
        if _is_login_page(page) or ("login.php" in page.casefold()
                                    and "logout.php" not in page.casefold()):
            return False, "签到失败：Cookie 已失效"
        if "阻断页面" in page:
            return False, "签到失败：站点返回阻断页面，请更换网络后重试"
        if _SIGNED_RE.search(page):
            return True, "今日已签到"

        match = _CSRF_RE.search(page)
        if not match:
            return False, "签到失败：未获取到 x-csrf"
        csrf = str(match.group(1) or "").strip()
        if not csrf:
            return False, "签到失败：未获取到 x-csrf"

        updated_cookie = _response_cookie(response, cookie)
        signed = ctx.post(
            SIGNIN_URL,
            data={"csrf": csrf},
            headers={
                "Cookie": updated_cookie,
                "Referer": INDEX_URL,
                "Origin": BASE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if signed is None or signed.status_code != 200:
            return False, "签到失败：签到接口请求失败"
        body = str(getattr(signed, "text", "") or "")
        if _is_login_page(body):
            return False, "签到失败：Cookie 已失效"
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return False, "签到失败：签到接口返回内容无法解析"
        if not isinstance(payload, Mapping):
            return False, "签到失败：签到接口返回结构不正确"
        if str(payload.get("state") or "").lower() != "success":
            message = str(payload.get("msg") or payload.get("message") or "").strip()
            return False, "签到失败" + (f"：{message}" if message else "")

        days = payload.get("signindays")
        points = payload.get("integral")
        extra = []
        if days not in (None, ""):
            extra.append(f"连续 {days} 天")
        if points not in (None, ""):
            extra.append(f"获得 {points} 积分")
        return True, "签到成功" + (f"（{'，'.join(extra)}）" if extra else "")
