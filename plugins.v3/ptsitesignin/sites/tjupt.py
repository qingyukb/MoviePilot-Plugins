"""TJUPT（tjupt.org）：验证码是「选海报」，靠豆瓣海报做图像相似度匹配。

流程和 thsrite 的实现一致：
1. 打开 attendance.php，页面有「今日已签到」就结束；
2. 取验证码图片算 hash，先查本地答案缓存（按验证码图片 hash 存）；
3. 缓存没有再逐个选项去豆瓣检索海报，算相似度 > 0.9 的即为答案；
4. 提交 answer + 验证码，成功后把「验证码 hash → 选项文字」写回缓存。
"""
from __future__ import annotations

import json
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

BASE = "https://www.tjupt.org"
CACHE = Path("data") / "signin" / "tjupt.json"
THRESHOLD = 0.9
_PAGE_RE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.IGNORECASE)
_OPTION_RE = re.compile(r"<input[^>]+name=\"answer\"[^>]+value=\"([^\"]*)\"[^>]*>([^<]*)",
                        re.IGNORECASE)


def _image_hash(data: bytes) -> str:
    """感知哈希：先缩成 8x8 灰度，再按平均值二值化。"""
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        with Image.open(BytesIO(data)) as image:
            small = image.convert("L").resize((8, 8))
            pixels = list(small.getdata())
    except Exception:
        return ""
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):016x}"


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_bin = bin(int(left, 16))[2:].zfill(64)
    right_bin = bin(int(right, 16))[2:].zfill(64)
    same = sum(1 for a, b in zip(left_bin, right_bin) if a == b)
    return same / 64


class Tjupt:
    site_url = "tjupt.org"

    def _load_cache(self) -> dict:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self, cache: dict) -> None:
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _submit(self, ctx: Any, answer: str, image_hash: str) -> Tuple[bool, str]:
        response = ctx.post(f"{BASE}/attendance.php",
                            data={"answer": answer, "submit": "提交"},
                            headers={"Referer": f"{BASE}/attendance.php"})
        if response is None or response.status_code != 200:
            return False, "签到失败：签到接口请求失败"
        text = response.text or ""
        if "签到成功" in text or "获得" in text and "魔力值" in text:
            cache = self._load_cache()
            if image_hash:
                cache[image_hash] = answer
                self._save_cache(cache)
            return True, "签到成功"
        if "已签到" in text:
            return True, "今日已签到"
        if "验证码" in text and ("错误" in text or "不正确" in text):
            return False, "签到失败：验证码错误"
        return False, "签到失败：页面没有成功提示"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        page = ctx.page(f"{BASE}/attendance.php")
        if not page:
            return False, "签到失败：打不开签到页"
        if "login.php" in page:
            return False, "签到失败：Cookie 已失效"
        if "今日已签到" in page:
            return True, "今日已签到"

        match = _PAGE_RE.search(page)
        if not match:
            return False, "签到失败：未找到验证码图片"
        image_url = match.group(1)
        if image_url.startswith("/"):
            image_url = BASE + image_url

        options = [(value, text.strip()) for value, text in _OPTION_RE.findall(page)
                   if text.strip()]
        if not options:
            return False, "签到失败：未找到答案选项"

        captcha_hash = _image_hash(ctx.image(image_url, referer=f"{BASE}/attendance.php"))

        cache = self._load_cache()
        cached = cache.get(captcha_hash) if captcha_hash else None
        if cached:
            for value, text in options:
                if str(cached) == text:
                    return self._submit(ctx, value, captcha_hash)

        for value, text in options:
            suggestion = ctx.get(f"https://movie.douban.com/j/subject_suggest?q={text}")
            if suggestion is None or suggestion.status_code != 200:
                continue
            try:
                items = json.loads(suggestion.text or "[]")
            except ValueError:
                continue
            if isinstance(items, dict):
                items = [items]
            for item in items:
                poster = str(item.get("img") or "")
                if not poster:
                    continue
                poster_hash = _image_hash(ctx.image(poster, referer="https://movie.douban.com"))
                score = _similarity(captcha_hash, poster_hash)
                if score > THRESHOLD:
                    return self._submit(ctx, value, captcha_hash)
            time.sleep(5)
        return False, "签到失败：豆瓣比对未找到答案"
