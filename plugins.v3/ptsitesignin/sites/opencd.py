"""OpenCD（皇后 open.cd）：plugin_sign-in.php 的图片验证码签到。

站点签到是「图片验证码 + 表单 POST」，规则表达不了，所以放站点模块：
GET 签到页取 imagehash → OCR 验证码图 → POST plugin_sign-in.php?cmd=signin → 解析 JSON。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping, Tuple

BASE = "https://open.cd"
HASH_RE = re.compile(r'name="imagehash"\s+value="([0-9a-fA-F]+)"')
# 签到成功后站点会把验证码表单换成「[查看簽到記錄]」，据此也能判成已签到
ALREADY_WORDS = ("已签到", "已簽到", "已经签到", "已經簽到", "今日已签", "今日已簽",
                 "查看簽到記錄", "查看签到记录")
CAPTCHA_WORDS = ("验证码", "驗證碼", "验证码错误", "驗證碼錯誤")


class OpenCD:
    """皇后：验证码签到。"""

    site_url = "open.cd"

    def _fetch_form(self, ctx: Any) -> Tuple[str, str]:
        """打开签到页，返回 (imagehash, 页面 HTML)。"""
        page = ctx.page(f"{BASE}/plugin_sign-in.php") or ""
        match = HASH_RE.search(page)
        return (match.group(1) if match else ""), page

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        image_hash, page = self._fetch_form(ctx)
        if not page:
            return False, "签到失败：打不开签到页"
        if not image_hash:
            # 签到页没有验证码表单：多半是今天已经签过了
            index = ctx.page(f"{BASE}/index.php") or ""
            for text in (page, index):
                if any(word in text for word in ALREADY_WORDS):
                    return True, "今日已签到"
            return False, "签到失败：签到页没有验证码表单（可能已签到或站点改版）"

        last_message = ""
        for attempt in range(4):
            if attempt:
                image_hash, page = self._fetch_form(ctx)
                if not image_hash:
                    break
            code = (ctx.ocr(f"{BASE}/image.php?action=regimage&imagehash={image_hash}") or "").strip()
            if not code:
                last_message = "OCR 没识别出验证码"
                time.sleep(1)
                continue
            response = ctx.post(f"{BASE}/plugin_sign-in.php?cmd=signin",
                                data={"imagehash": image_hash, "imagestring": code},
                                headers={"Referer": f"{BASE}/plugin_sign-in.php"})
            if response is None or response.status_code != 200:
                return False, f"签到失败：签到接口 HTTP {getattr(response, 'status_code', '无响应')}"
            try:
                payload = json.loads(response.text or "{}")
            except ValueError:
                body = (response.text or "")[:120].replace("\n", " ")
                return False, f"签到失败：签到接口返回异常（{body}）"
            state = str(payload.get("state") or "").strip().lower()
            message = str(payload.get("msg") or "").strip()
            last_message = message or last_message
            if state == "true":
                return True, f"签到成功（{message}）" if message else "签到成功"
            if any(word in message for word in ALREADY_WORDS):
                return True, "今日已签到"
            if any(word in message for word in CAPTCHA_WORDS):
                time.sleep(1)
                continue
            return False, f"签到失败：{message or '未知原因'}"
        return False, f"签到失败：{last_message or '验证码连续识别错误'}"
