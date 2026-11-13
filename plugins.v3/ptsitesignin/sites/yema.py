"""YemaPT：API Key 用于 openApi 登录检测；签到仍由现有 Cookie + Altcha 规则处理。"""
from __future__ import annotations

from typing import Any, Mapping, Tuple


class YemaPT:
    """YemaPT openApi 登录检测。"""

    site_url = "yemapt.org"

    def login(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        apikey = str(site.get("apikey") or "").strip()
        if apikey:
            url = str(site.get("url") or "https://www.yemapt.org/").rstrip("/") + "/openApi/user/fetchBasicInfo.json"
            try:
                session = ctx._session()
                try:
                    session.headers.pop("Cookie", None)
                except Exception:
                    pass
                response = session.post(
                    url,
                    headers={"Authorization": apikey, "Content-Type": "application/json",
                             "Accept": "application/json"},
                    json={}, timeout=ctx.timeout, allow_redirects=False,
                )
            except Exception:
                return False, "模拟登录失败，无法连接 YemaPT openApi"
            try:
                if response is None:
                    return False, "模拟登录失败，无法连接 YemaPT openApi"
                payload = ctx.response_json(response)
                if response.status_code == 200 and payload.get("success") is True                         and isinstance(payload.get("data"), dict):
                    return True, "模拟登录成功"
                message = str(payload.get("errorMessage") or "").strip()
                return False, "模拟登录失败" + (f"：{message}" if message else "")
            finally:
                ctx.close_response(response)
        page = ctx.page(str(site.get("url") or "https://www.yemapt.org/"))
        if not page:
            return False, "模拟登录失败，无法打开 YemaPT"
        lower = page.lower()
        if "login" in lower and "退出" not in page and "logout" not in lower:
            return False, "模拟登录失败，Cookie 已失效"
        if "退出" in page or "logout" in lower:
            return True, "模拟登录成功"
        return False, "模拟登录失败，未识别到登录状态"
