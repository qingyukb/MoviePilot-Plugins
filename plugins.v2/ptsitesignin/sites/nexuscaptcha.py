"""NexusPHP「验证码签到」通用模块（可撸 our.kelu.one、自然 naturept.top 等）。

这类站点的 /attendance.php：
- 今天已签到：页面直接写「签到成功 … 本次签到获得 X 个魔力值」+ 日历，没有任何表单；
- 今天未签到：页面是一个带图片验证码的表单（hidden imagehash + 验证码输入框 + 提交按钮），
  验证码图片走 image.php?action=regimage&imagehash=…

所以流程是：GET 签到页 → 已签到就直接结束 → 否则解析表单与验证码图片 → MP OCR 识别 →
POST 表单 → 回读确认（验证码错就换一张重试，最多 3 轮）。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Tuple
from urllib.parse import urljoin

SIGNED_WORDS = ("签到成功", "本次签到获得", "签到獲得", "簽到成功")
ALREADY_WORDS = ("今天已经签到", "今日已签到", "已经签到", "今天已签到")
CAPTCHA_WRONG_WORDS = ("验证码错误", "驗證碼錯誤", "验证码不正确", "验证码不对")
FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.IGNORECASE | re.DOTALL)
ACTION_RE = re.compile(r"""action\s*=\s*["']?([^"'>\s]+)""", re.IGNORECASE)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
IMG_RE = re.compile(r"""<img\b[^>]*src\s*=\s*["']([^"']*image\.php[^"']*)["']""", re.IGNORECASE)
CAPTCHA_NAME_RE = re.compile(r"(imagestring|code|captcha|verify|seccode)", re.IGNORECASE)
BONUS_RE = re.compile(r"本次签到获得\s*([\d,]+)\s*个魔力值")
HOSTS = ("kelu.one", "naturept.top")


def _decode(response: Any) -> str:
    """按 NexusPHP 常见编码解码（站点多为 GBK，但 headers 常写成 utf-8）。"""
    if response is None:
        return ""
    content = getattr(response, "content", None)
    if isinstance(content, bytes) and content:
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                text = content.decode(encoding)
                if "签到" in text or "簽到" in text or "attendance" in text:
                    return text
            except Exception:
                continue
    return str(getattr(response, "text", "") or "")


def _attrs(tag: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in ATTR_RE.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3) if match.group(3) is not None else match.group(4) or ""
        found[name] = value
    return found


class NexusCaptcha:
    """NexusPHP 图片验证码签到。"""

    site_url = "kelu.one"

    @staticmethod
    def match(url: str) -> bool:
        try:
            from urllib.parse import urlparse
            host = (urlparse(str(url or "")).hostname or "").lower()
        except Exception:
            return False
        return any(host == item or host.endswith("." + item) for item in HOSTS)

    def _load(self, ctx: Any, url: str) -> str:
        response = ctx.get(url)
        return _decode(response)

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        base = str(site.get("url") or "https://our.kelu.one").rstrip("/")
        target = urljoin(base + "/", "attendance.php")
        page = self._load(ctx, target)
        if not page:
            return False, "签到失败：打不开签到页"
        if "login.php" in page and "签到" not in page:
            return False, "签到失败：Cookie 已失效"
        if any(word in page for word in SIGNED_WORDS):
            return True, "今日已签到"

        last_reason = ""
        for attempt in range(3):
            if attempt:
                page = self._load(ctx, target)
                if any(word in page for word in SIGNED_WORDS):
                    return True, "今日已签到"
            form, fields, captcha_name, image_url = self._parse_form(page, target)
            if not form:
                if any(word in page for word in ALREADY_WORDS):
                    return True, "今日已签到"
                return False, "签到失败：签到页没有找到验证码表单"

            code = (ctx.ocr(image_url) or "").strip() if image_url else ""
            if not code:
                last_reason = "OCR 没识别出验证码"
                continue
            fields[captcha_name] = code

            response = ctx.post(form, data=fields, headers={"Referer": target})
            body = _decode(response)
            if getattr(response, "status_code", 0) >= 400:
                last_reason = f"签到接口 HTTP {getattr(response, 'status_code', '无响应')}"
                continue
            if any(word in body for word in SIGNED_WORDS):
                match = BONUS_RE.search(body)
                if match:
                    return True, f"签到成功（本次获得 {match.group(1)} 个魔力值）"
                return True, "签到成功"
            if any(word in body for word in ALREADY_WORDS):
                return True, "今日已签到"
            if any(word in body for word in CAPTCHA_WRONG_WORDS):
                last_reason = "验证码错误"
                continue
            # 有的站点提交后返回空页/跳转：回读签到页确认
            after = self._load(ctx, target)
            if any(word in after for word in SIGNED_WORDS):
                return True, "签到成功"
            if any(word in after for word in ALREADY_WORDS):
                return True, "今日已签到"
            last_reason = "提交后未确认到签到成功"
        return False, f"签到失败：{last_reason or '未知原因'}"

    @staticmethod
    def _parse_form(page: str, target: str) -> Tuple[str, dict[str, str], str, str]:
        """解析签到页里的验证码表单，返回 (action 绝对地址, 字段, 验证码字段名, 验证码图片地址)。"""
        for match in FORM_RE.finditer(page):
            block = match.group(1)
            inputs = INPUT_RE.findall(block)
            captcha_name = ""
            fields: dict[str, str] = {}
            for tag in inputs:
                attrs = _attrs(tag)
                name = attrs.get("name") or ""
                if not name:
                    continue
                if CAPTCHA_NAME_RE.search(name):
                    captcha_name = name
                    continue
                fields[name] = attrs.get("value", "")
            if not captcha_name:
                continue
            action = ACTION_RE.search(match.group(0))
            form_action = urljoin(target, action.group(1)) if action else target
            image = IMG_RE.search(block) or IMG_RE.search(page)
            image_url = urljoin(target, image.group(1)) if image else ""
            return form_action, fields, captcha_name, image_url
        return "", {}, "", ""
