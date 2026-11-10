"""好学（HXPT，hxpt.org）：首页 API 读取签到状态，CSRF 后调用签到接口。

站点是 FluxTorrent / Next.js 前后端分离架构，签到不在 HTML 表单里：

1. GET ``/api/v1/home``，读取 ``data.attendance.checked_today``；
2. CSRF token 在 ``flux_csrf`` Cookie 中，缺失时先 GET ``/api/v1/me/csrf``；
3. POST ``/api/v1/attendance/checkin``，请求头带 ``x-csrf-token``；
4. 接口返回 JSON ``code == 0`` 且 data 中有奖励/连续天数时确认成功。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import unquote


BASE = "https://www.hxpt.org"
HOME_URL = f"{BASE}/api/v1/home"
CSRF_URL = f"{BASE}/api/v1/me/csrf"
CHECKIN_URL = f"{BASE}/api/v1/attendance/checkin"

_SESSION_COOKIE = "flux.session"
_CSRF_COOKIE = "flux_csrf"
_COOKIE_PAIR_RE = re.compile(r"(?:^|;\s*)([^=;\s]+)=([^;]*)")


def _cookie_dict(raw: str) -> Dict[str, str]:
    """把 Cookie 请求头拆成字典。"""
    values: Dict[str, str] = {}
    for name, value in _COOKIE_PAIR_RE.findall(str(raw or "")):
        values[name] = value
    return values


def _cookie_header(values: Mapping[str, str]) -> str:
    """把 Cookie 字典拼回请求头。"""
    return "; ".join(f"{name}={value}" for name, value in values.items() if name)


def _merge_response_cookies(cookie: str, response: Any) -> str:
    """合并响应 Set-Cookie 中的 flux.session / flux_csrf。"""
    values = _cookie_dict(cookie)
    cookies = getattr(response, "cookies", None)
    getter = getattr(cookies, "get_dict", None)
    if callable(getter):
        try:
            for name, value in getter().items():
                if str(name) in {_SESSION_COOKIE, _CSRF_COOKIE}:
                    values[str(name)] = str(value)
        except Exception:
            pass
    return _cookie_header(values)


def _csrf_token(cookie: str) -> str:
    """从 Cookie 中取 CSRF；前端使用 decodeURIComponent 解码。"""
    value = _cookie_dict(cookie).get(_CSRF_COOKIE, "")
    return unquote(value) if value else ""


def _response_payload(response: Any) -> Mapping[str, Any]:
    """解析 API 的统一 JSON 响应。"""
    try:
        value = json.loads(str(getattr(response, "text", "") or ""))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else {}


def _attendance(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    attendance = _data(payload).get("attendance")
    return attendance if isinstance(attendance, Mapping) else {}


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes"}


def _already_message(payload: Mapping[str, Any]) -> bool:
    text = str(payload.get("message") or payload.get("msg") or "").lower()
    return any(word in text for word in ("已经签到", "已签到", "重复签到", "今天签过", "今日签过"))


class Hxpt:
    site_url = "hxpt.org"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        cookie = str(site.get("cookie") or "").strip()
        if not cookie:
            return False, "签到失败：缺少 Cookie"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE}/",
            "Origin": BASE,
        }
        response = ctx.get(HOME_URL, headers={**headers, "Cookie": cookie})
        if response is None:
            return False, "签到失败：打不开好学首页接口"
        if response.status_code == 401:
            return False, "签到失败：Cookie 已失效"
        if response.status_code >= 400:
            return False, f"签到失败：首页接口 HTTP {response.status_code}"
        cookie = _merge_response_cookies(cookie, response)
        payload = _response_payload(response)
        if str(payload.get("code") or "") not in {"", "0", "0.0"}:
            if str(payload.get("code")) == "2001":
                return False, "签到失败：Cookie 已失效"
            return False, f"签到失败：{payload.get('message') or '首页接口返回错误'}"
        attendance = _attendance(payload)
        if _truthy(attendance.get("checked_today")):
            return True, "今日已签到"
        if _truthy(attendance.get("off")):
            return False, "签到失败：站点签到功能未开启"

        csrf = _csrf_token(cookie)
        if not csrf:
            csrf_response = ctx.get(CSRF_URL, headers={**headers, "Cookie": cookie})
            if csrf_response is None:
                return False, "签到失败：获取 CSRF 失败"
            if csrf_response.status_code == 401:
                return False, "签到失败：Cookie 已失效"
            if csrf_response.status_code >= 400:
                return False, f"签到失败：CSRF 接口 HTTP {csrf_response.status_code}"
            cookie = _merge_response_cookies(cookie, csrf_response)
            csrf = _csrf_token(cookie)
        if not csrf:
            return False, "签到失败：未能获取 flux_csrf"

        checked = ctx.post(
            CHECKIN_URL,
            json_body={},
            headers={
                **headers,
                "Cookie": cookie,
                "Content-Type": "application/json",
                "x-csrf-token": csrf,
            },
        )
        if checked is None:
            return False, "签到失败：签到接口请求失败"
        if checked.status_code == 401:
            return False, "签到失败：Cookie 已失效"
        if checked.status_code >= 400:
            return False, f"签到失败：签到接口 HTTP {checked.status_code}"
        result = _response_payload(checked)
        code = str(result.get("code") or "")
        if code in {"", "0", "0.0"}:
            data = _data(result)
            reward = data.get("reward")
            streak = data.get("streak")
            detail = []
            if reward not in (None, ""):
                detail.append(f"获得 {reward} 魔力值")
            if streak not in (None, ""):
                detail.append(f"连续 {streak} 天")
            return True, "签到成功" + (f"（{'，'.join(detail)}）" if detail else "")
        if code == "2001":
            return False, "签到失败：Cookie 已失效"
        if _already_message(result):
            return True, "今日已签到"
        return False, f"签到失败：{result.get('message') or '签到接口返回错误'}"
