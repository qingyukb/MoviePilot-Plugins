"""HDSky（hdsky.me）：AJAX 取 imagehash → MP OCR → POST showup.php。

照 tmzg0000 captchasignin 的流程补齐两处（原实现遇到就放弃）：
1. 接口回 `invalid_imagehash` 时**重新取挑战**再识别，不重复提交同一份失效答案（最多 3 轮）；
2. 提交超时 / 响应异常时**只读回查首页一次**，首页显示已签到就直接算成功；
3. 只有确实没签上才返回失败，交给上层用 Browserless 弹窗流程兜底。
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, Tuple

BASE = "https://hdsky.me"
MAX_ROUNDS = 3


class Hdsky:
    site_url = "hdsky.me"

    def _signed(self, ctx: Any) -> bool:
        """回查首页判断是否已签到（只读，安全）。"""
        page = ctx.page(f"{BASE}/") or ""
        return bool(page) and "已签到" in page and "立即签到" not in page

    def _new_hash(self, ctx: Any) -> str:
        for _ in range(4):
            response = ctx.post(f"{BASE}/image_code_ajax.php", data={"action": "new"},
                                headers={"Referer": f"{BASE}/index.php"})
            if response is not None and getattr(response, "status_code", 0) == 200:
                try:
                    payload = json.loads(response.text or "{}")
                except ValueError:
                    payload = {}
                if payload.get("success"):
                    code = str(payload.get("code") or "")
                    if code:
                        return code
            time.sleep(1)
        return ""

    def _recognize(self, ctx: Any, image_url: str) -> str:
        for _ in range(4):
            code = (ctx.ocr(image_url) or "").strip()
            if len(code) == 6:
                return code
            time.sleep(1)
        return ""

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        page = ctx.page(f"{BASE}/")
        if not page:
            return False, "签到失败：打不开首页"
        if "login.php" in page:
            return False, "签到失败：Cookie 已失效"
        if "已签到" in page and "立即签到" not in page:
            return True, "今日已签到"

        last_message = ""
        for round_index in range(MAX_ROUNDS):
            img_hash = self._new_hash(ctx)
            if not img_hash:
                return False, "签到失败：取不到验证码"
            code = self._recognize(ctx, f"{BASE}/image.php?action=regimage&imagehash={img_hash}")
            if not code:
                last_message = "OCR 没识别出 6 位验证码"
                continue

            response = ctx.post(f"{BASE}/showup.php",
                                data={"action": "showup", "imagehash": img_hash,
                                      "imagestring": code},
                                headers={"Referer": f"{BASE}/index.php"})
            if response is None or getattr(response, "status_code", 0) != 200:
                # 提交结果未知：只读回查一次首页，签上了就不必再折腾
                if self._signed(ctx):
                    return True, "签到成功（提交响应异常，但首页显示已签到）"
                last_message = "签到接口请求失败"
                continue
            try:
                payload = json.loads(response.text or "{}")
            except ValueError:
                if self._signed(ctx):
                    return True, "签到成功（接口返回异常，但首页显示已签到）"
                last_message = "签到接口返回异常"
                continue
            if payload.get("success"):
                return True, "签到成功"
            message = str(payload.get("message") or "")
            if message == "date_unmatch":
                return True, "今日已签到"
            if message == "invalid_imagehash":
                # 验证码过期/被消费：换一张重来，不要重复提交同一份答案
                last_message = "验证码已失效（invalid_imagehash）"
                time.sleep(1)
                continue
            last_message = message or "未知原因"
            break
        return False, f"签到失败：{last_message or '未知原因'}"
