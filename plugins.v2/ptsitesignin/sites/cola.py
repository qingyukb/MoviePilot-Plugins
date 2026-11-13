"""ColaPT（可乐 PT）：每日登录奖励 + 每周签到。

网页会话的 CSRF token 来自 GET /api/v1/me；API Key 没有可乐用户权限，
因此配置 Cookie 时优先使用 Cookie，API Key-only 会明确失败而不会误报。
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple

BASE = "https://www.colapt.com"


class ColaPT:
    site_url = "colapt.com"
    # 该模块已经完整处理可乐的每日登录和每周签到，失败时不应回退到通用 HTTP。
    authoritative = True

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        cookie = str(site.get("cookie") or "").strip()
        if not cookie:
            if str(site.get("apikey") or "").strip():
                return False, "签到失败：Cola API Key 没有用户签到权限，请配置网页 Cookie"
            return False, "签到失败：未配置 Cola Cookie"
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Origin": BASE, "Referer": BASE + "/"}
        me_response = ctx.get(BASE + "/api/v1/me", headers=headers)
        me = ctx.response_json(me_response)
        if getattr(me_response, "status_code", 0) in {401, 403}:
            return False, "签到失败：Cola Cookie 已失效"
        if getattr(me_response, "status_code", 0) != 200 or not isinstance(me.get("session"), Mapping):
            return False, "签到失败：无法读取 Cola 登录会话"
        csrf = str(me.get("session", {}).get("csrf_token") or "").strip()
        if not csrf:
            return False, "签到失败：Cola 会话没有 CSRF token"
        headers["X-CSRF-Token"] = csrf

        state_response = ctx.get(BASE + "/api/v1/me/cola", headers=headers)
        state = ctx.response_json(state_response)
        if getattr(state_response, "status_code", 0) in {401, 403}:
            return False, "签到失败：Cola Cookie 已失效"
        if getattr(state_response, "status_code", 0) != 200:
            return False, "签到失败：无法读取 Cola 可乐状态"

        today = state.get("today") if isinstance(state.get("today"), Mapping) else {}
        week = state.get("week") if isinstance(state.get("week"), Mapping) else {}
        notes = []
        if bool(today.get("logged_in")):
            notes.append("每日登录已领取")
        else:
            daily = ctx.post(BASE + "/api/v1/me/cola/daily-login", json_body={}, headers=headers)
            daily_body = ctx.response_json(daily)
            if getattr(daily, "status_code", 0) >= 400:
                return False, "签到失败：Cola 每日登录领取失败"
            earned = daily_body.get("earned")
            notes.append(f"每日登录 +{earned}" if earned else "每日登录已领取")

        if bool(week.get("checked_in")):
            notes.append("本周已签到")
            return True, "；".join(notes)

        checkin = ctx.post(BASE + "/api/v1/me/cola/checkin", json_body={}, headers=headers)
        checkin_body = ctx.response_json(checkin)
        status = int(getattr(checkin, "status_code", 0) or 0)
        error = checkin_body.get("error") if isinstance(checkin_body.get("error"), Mapping) else {}
        if status == 409 or str(error.get("code") or "") == "state_conflict":
            notes.append("本周已签到")
            return True, "；".join(notes)
        if status >= 400:
            return False, "签到失败：Cola 每周签到失败"
        earned = checkin_body.get("earned")
        notes.append("签到成功" + (f"（+{earned} 可乐）" if earned else ""))
        return True, "；".join(notes)

    def login(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        if not str(site.get("cookie") or "").strip():
            if str(site.get("apikey") or "").strip():
                return False, "模拟登录失败，Cola API Key 没有用户登录权限"
            return False, "模拟登录失败，未配置 Cola Cookie"
        response = ctx.get(BASE + "/api/v1/me", headers={
            "Accept": "application/json", "Origin": BASE, "Referer": BASE + "/"})
        payload = ctx.response_json(response)
        if getattr(response, "status_code", 0) == 200 and payload.get("status") == "active":
            return True, "模拟登录成功"
        return False, "模拟登录失败，Cola Cookie 已失效或无法读取账户"
