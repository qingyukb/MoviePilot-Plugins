"""海棠PT（htpt.cc）：attendance.php 的表单签到（POST action=sign），无需浏览器。

实测流程（用真实账号验证过）：
1. GET  /attendance.php          未签到时页面有「立即签到」按钮表单（hidden action=sign）
2. POST /attendance.php  action=sign   响应是空页，没有提示语
3. 再 GET 一次 /attendance.php   变成「今日已签到 ✓（连续 N 天，今日获得 X 魔力值）」

所以判定必须靠第 3 步回读，不能看 POST 的响应体。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Tuple

BASE = "https://www.htpt.cc"
ATTENDANCE = "/attendance.php"
BONUS_RE = re.compile(r"今日获得\s*([\d,]+)\s*魔力值")
SIGNED_WORDS = ("今日已签到", "今日已经签到", "今天已签到")
INSERT_WORDS = ("立即签到", "補签", "补签")


class Htpt:
    """海棠PT：每日签到（HTTP 表单）。"""

    site_url = "htpt.cc"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        base = str(site.get("url") or BASE).rstrip("/") or BASE
        page = ctx.page(f"{base}{ATTENDANCE}") or ""
        if not page:
            return False, "签到失败：打不开签到页"
        if any(word in page for word in SIGNED_WORDS):
            return True, "今日已签到"
        if "立即签到" not in page:
            return False, "签到失败：签到页没有「立即签到」入口（站点可能改版）"

        response = ctx.post(f"{base}{ATTENDANCE}", data={"action": "sign"},
                            headers={"Referer": f"{base}{ATTENDANCE}"})
        status = getattr(response, "status_code", 0)
        if response is None or (isinstance(status, int) and status >= 400):
            return False, f"签到失败：提交签到请求异常（HTTP {status or '无响应'}）"

        # POST 的响应是空页，必须回读签到页才知道结果
        after = ctx.page(f"{base}{ATTENDANCE}") or ""
        if any(word in after for word in SIGNED_WORDS):
            match = BONUS_RE.search(after)
            if match:
                return True, f"签到成功（今日获得 {match.group(1)} 魔力值）"
            return True, "签到成功"
        if "立即签到" not in after and after:
            # 入口消失也算签上了（与学校那套判定一致）
            return True, "签到成功（签到入口已消失）"
        return False, "签到失败：提交后页面仍是「立即签到」"
