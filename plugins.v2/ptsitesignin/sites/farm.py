"""自由农场（0ff.cc / pt.0ff.cc）：直连签到，不消耗 Browserless / OCR。

参考 tmzg0000 的 captchasignin 农场流程（实测可用）：
1. GET /attendance.php   → 页面脚本里找一个 /slide_check_xxx.js
2. GET 那个脚本          → 里面有一串 set_access_token-xxx 接口地址
3. GET 该接口（同源）     → 服务端据此记录「已验证」
4. 等 3 秒后再 GET /attendance.php → 页面不再出现滑块脚本 = 签到成功
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping, Tuple
from urllib.parse import urljoin

SLIDE_SCRIPT_RE = re.compile(r"""/slide_check_[^/"'\s]+\.js(?:\?[^"'\s]*)?$""")
TOKEN_RE = re.compile(r"""["']([^"'\s]*set_access_token-[^"'\s]+)["']""")
SLIDER_WORDS = ("slide_check", "拖动滑块验证", "拖動滑塊驗證")
SIGNED_WORDS = ("今日已签到", "今日已经签到", "今天已签到", "已签到", "已簽到", "签到成功", "簽到成功")


class Farm:
    """自由农场：直连滑块接口完成签到。"""

    site_url = "0ff.cc"

    @staticmethod
    def match(url: str) -> bool:
        """0ff.cc 及其子域（pt.0ff.cc）都归这个模块。"""
        try:
            from urllib.parse import urlparse
            host = (urlparse(str(url or "")).hostname or "").lower()
        except Exception:
            return False
        return host == "0ff.cc" or host.endswith(".0ff.cc")

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        base = str(site.get("url") or "https://pt.0ff.cc").rstrip("/")
        target = urljoin(base + "/", "attendance.php")
        page = ctx.page(target) or ""
        if not page:
            return False, "签到失败：打不开农场签到页"
        if not any(word in page for word in SLIDER_WORDS) and any(word in page for word in SIGNED_WORDS):
            return True, "今日已签到"

        source = ""
        for candidate in re.findall(r"""src=["']([^"']+)["']""", page):
            if "/slide_check_" in candidate:
                source = candidate
                break
        if not source:
            return False, "签到失败：页面没有滑块验证脚本（站点可能改版）"

        script = ctx.page(urljoin(target, source)) or ""
        match = TOKEN_RE.search(script)
        if not match:
            return False, "签到失败：滑块脚本里没有找到验证接口"

        endpoint = urljoin(target, match.group(1))
        response = ctx.get(endpoint, headers={"Referer": target})
        if response is None or getattr(response, "status_code", 0) >= 400:
            return False, f"签到失败：验证接口请求异常（HTTP {getattr(response, 'status_code', '无响应')}）"

        time.sleep(3)
        after = ctx.page(target) or ""
        if any(word in after for word in SLIDER_WORDS):
            return False, "签到失败：直连后仍要求滑块验证"
        if any(word in after for word in SIGNED_WORDS):
            return True, "签到成功（农场直连）"
        return False, "签到失败：农场直连后未确认到签到成功"
