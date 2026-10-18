"""M-Team（m-team）：没有签到，只需用 API Token 刷新「最后访问时间」。"""
from __future__ import annotations

from typing import Any, Mapping, Tuple
from urllib.parse import urlparse


class MTeam:
    site_url = "m-team"

    @classmethod
    def match(cls, url: str) -> bool:
        """m-team 的域名有 .cc / .io / .xyz 多种，按关键字匹配。"""
        return "m-team" in str(url or "").lower()

    @staticmethod
    def _token(site: Mapping[str, Any]) -> str:
        for key in ("token", "apikey", "api_key"):
            value = str(site.get(key) or "").strip()
            if value:
                return value
        # MoviePilot 站点管理里可能把 API Key 写在附加信息/备注里
        for key in ("note", "extra", "passkey"):
            text = str(site.get(key) or "")
            for line in text.splitlines():
                if "token" in line.lower() or "apikey" in line.lower():
                    parts = line.split(":", 1)
                    if len(parts) == 2 and parts[1].strip():
                        return parts[1].strip()
        return ""

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        token = self._token(site)
        if not token:
            return False, "签到失败：M-Team 需要 API Token（站点管理里没找到）"
        host = (urlparse(str(site.get("url") or "")).hostname or "").lower()
        domain = host.split(".", 1)[-1] if host else "m-team.cc"
        referer = str(site.get("url") or "")
        response = ctx.post(f"https://api.{domain}/api/member/updateLastBrowse",
                            headers={"Authorization": token,
                                     "Content-Type": "application/json",
                                     "Accept": "application/json, text/plain, */*",
                                     "Referer": f"{referer}index"})
        if response is None:
            return False, "签到失败：无法连接 API"
        if response.status_code == 200:
            return True, "模拟登录成功"
        return False, f"签到失败：状态码 {response.status_code}"
