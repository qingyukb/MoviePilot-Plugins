"""高清视界（HDArea）：API Key 优先，未配置时保留 Cookie 签到。"""
from __future__ import annotations

from typing import Any, Mapping, Tuple


class HDArea:
    """高清视界签到处理器；配置 API Key 后不向 Cookie 页面回退。"""

    site_url = "hdarea.club"
    _success_text = "此次签到您获得"
    _repeat_text = "请不要重复签到哦"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        if str(site.get("apikey") or "").strip():
            data, message = self._api_request(site, ctx, "user/signin", "post")
            if data is None:
                return False, f"签到失败，{message}"
            if (data.get("already_signed") is True or data.get("signed_in_today") is True
                    or self._repeat_text in message or "今日已签到" in message):
                return True, "今日已签到"
            return True, "签到成功"
        return self._cookie_signin(site, ctx)

    def login(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        if str(site.get("apikey") or "").strip():
            data, message = self._api_request(site, ctx, "user/profile", "get")
            user_id = str(data.get("id") or "") if data is not None else ""
            if data is not None and user_id.isdecimal() and int(user_id) > 0                     and data.get("enabled") is not False:
                return True, "模拟登录成功"
            return False, f"模拟登录失败，{message or 'API 未返回有效用户资料'}"
        if not str(site.get("cookie") or "").strip():
            return False, "模拟登录失败，未配置 Cookie 或 API Key"
        page = ctx.page(str(site.get("url") or "https://hdarea.club/"))
        if not page:
            return False, "模拟登录失败，无法打开站点"
        if "login.php" in page and "退出" not in page and "logout" not in page.lower():
            return False, "模拟登录失败，Cookie 已失效"
        if "退出" in page or "logout" in page.lower() or "登出" in page:
            return True, "模拟登录成功"
        return False, "模拟登录失败，未识别到登录状态"

    def _api_request(self, site: Mapping[str, Any], ctx: Any, path: str,
                     method: str) -> Tuple[Any, str]:
        apikey = str(site.get("apikey") or "").strip()
        base = str(site.get("api_url") or "").strip()
        if not base:
            base = str(site.get("url") or "https://hdarea.club/").rstrip("/") + "/api/v1"
        url = f"{base.rstrip('/')}/{path}"
        headers = {"X-API-Key": apikey, "Accept": "application/json",
                   "Content-Type": "application/json"}
        try:
            session = ctx._session()
            try:
                session.headers.pop("Cookie", None)
            except Exception:
                pass
            if method == "post":
                response = session.post(url, json={}, headers=headers, allow_redirects=False,
                                        timeout=ctx.timeout, proxies=ctx.proxies)
            else:
                response = session.get(url, headers=headers, allow_redirects=False,
                                       timeout=ctx.timeout, proxies=ctx.proxies)
        except Exception:
            return None, "无法连接 HDArea API"
        try:
            if response is None:
                return None, "无法连接 HDArea API"
            payload = ctx.response_json(response)
            message = str(payload.get("message") or "").replace(apikey, "[已隐藏]")
            if response.status_code == 401:
                return None, "HDArea API Key 已过期或无效"
            if response.status_code == 403:
                return None, "HDArea API Key 权限不足或 IP 不在白名单" + (f"：{message}" if message else "")
            if response.status_code != 200:
                return None, f"HDArea API 返回 HTTP {response.status_code}"
            if (payload.get("success") is not True or str(payload.get("code")) != "0"
                    or not isinstance(payload.get("data"), dict)):
                return None, " ".join(message.split())[:300] or "HDArea API 响应结构或业务状态无效"
            return payload["data"], message
        finally:
            ctx.close_response(response)

    def _cookie_signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        response = ctx.post("https://hdarea.club/sign_in.php", data={"action": "sign_in"})
        if response is None or getattr(response, "status_code", 0) != 200:
            return False, "签到失败，请检查站点连通性"
        body = str(getattr(response, "text", "") or "")
        if "login.php" in body and self._success_text not in body:
            return False, "签到失败，Cookie 已失效"
        if self._success_text in body:
            return True, "签到成功"
        if self._repeat_text in body:
            return True, "今日已签到"
        return False, "签到失败，接口未返回明确的签到结果"
