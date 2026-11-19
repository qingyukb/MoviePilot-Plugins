"""U2（u2.dmhy.org）：showup.php 四选一签到。

U2 的 showup 页面不是普通 attendance 表单，而是带一次性 ``req`` / ``hash`` /
``form`` / ``_csrf`` 的四选一提交。流程与 MoviePilot 官方 AutoSignIn 保持一致：

1. 打开 showup.php，已有签到链接时直接返回“今日已签到”；
2. 从 showup 表单读取 hidden 字段和四个提交按钮，随机选择一个答案；
3. POST 到 showup.php?action=show；
4. 无论返回页结果如何，都重新读取 showup.php 确认状态，不能只凭 JS 跳转报成功。
"""
from __future__ import annotations

import random
import re
import time
from html import unescape
from typing import Any, Dict, List, Mapping, Tuple


BASE = "https://u2.dmhy.org"
SHOWUP_URL = f"{BASE}/showup.php"

_SIGNED_LINK_RE = re.compile(
    r"<a\b(?=[^>]*\bhref\s*=\s*[\"'](?:https?://u2\.dmhy\.org)?/?showup\.php"
    r"(?:\?[^\"']*)?[\"'])[^>]*>\s*(?:已签到|已簽到|Show Up|Показать)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_SIGNED_STATUS_RE = re.compile(
    r"(?:感谢|感謝)[，,]?\s*(?:今天|今日)\s*已(?:签到|簽到)|"
    r"(?:今天|今日)\s*已(?:签到|簽到)",
    re.IGNORECASE,
)
_LOGIN_FORM_RE = re.compile(
    r"<form\b(?=[^>]*\baction\s*=\s*[\"'][^\"']*(?:takelogin|login)\.php[^\"']*[\"'])"
    r"[^>]*>.*?<input\b[^>]*\btype\s*=\s*[\"']password[\"']",
    re.IGNORECASE | re.DOTALL,
)


def _attrs(tag: str) -> Dict[str, str]:
    return {name.lower(): value for name, _, value in _ATTR_RE.findall(tag or "")}


def _answer_form_inner(page: str) -> str:
    """只返回带 req 隐藏字段的真实答题表单，忽略历史查询等其他 showup 表单。"""
    for _attrs_text, inner in _FORM_RE.findall(page or ""):
        if re.search(r"<input\b[^>]*\bname\s*=\s*[\"']req[\"']",
                     inner, re.IGNORECASE):
            return inner
    return ""


def _has_signin_form(page: str) -> bool:
    return bool(_answer_form_inner(page))


def _is_signed(page: str) -> bool:
    """已签到页必须明确写“今天/今日已签到”，且不能残留答题表单。"""
    page = page or ""
    if "logout.php" not in page.casefold() or _has_signin_form(page):
        return False
    visible = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page,
                     flags=re.IGNORECASE | re.DOTALL)
    visible = unescape(re.sub(r"<[^>]+>", " ", visible))
    visible = re.sub(r"\s+", "", visible)
    return bool(_SIGNED_STATUS_RE.search(visible))


def _is_login_page(page: str) -> bool:
    return bool(_LOGIN_FORM_RE.search(page or ""))


def _showup_fields(page: str) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """返回 showup 表单的 hidden 字段和 submit 选项。"""
    inner = _answer_form_inner(page)
    if not inner:
        return {}, []
    hidden: Dict[str, str] = {}
    options: List[Tuple[str, str]] = []
    for tag in _INPUT_RE.findall(inner):
        attrs = _attrs(tag)
        name = str(attrs.get("name") or "").strip()
        if not name:
            continue
        kind = str(attrs.get("type") or "text").strip().lower()
        value = str(attrs.get("value") or "")
        if kind == "hidden":
            hidden[name] = value
        elif kind in {"submit", "button", "image"}:
            options.append((name, value))
    return hidden, options


class U2:
    site_url = "u2.dmhy.org"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        page = ctx.page(SHOWUP_URL)
        if not page:
            return False, "签到失败：打不开 U2 的 showup 页面"
        if _is_login_page(page):
            return False, "签到失败：Cookie 已失效"
        if _is_signed(page):
            return True, "今日已签到"

        hidden, options = _showup_fields(page)
        required = ("req", "hash", "form", "_csrf")
        if any(not hidden.get(name) for name in required) or not options:
            return False, "签到失败：未找到 U2 签到参数"

        answer_name, answer_value = random.choice(options)
        data = {name: hidden[name] for name in required}
        data["message"] = "一切随缘~"
        data[answer_name] = answer_value
        response = ctx.post(
            f"{SHOWUP_URL}?action=show",
            data=data,
            headers={"Referer": SHOWUP_URL, "Origin": BASE},
        )
        if response is None or response.status_code != 200:
            return False, "签到失败：签到接口请求失败"

        # 不依赖提交响应的跳转脚本是否可识别，始终回读 showup.php。
        # 若站点写库较慢，只重复读取，不重复提交答题。
        checked = ""
        for attempt in range(3):
            checked = ctx.page(SHOWUP_URL)
            if _is_signed(checked):
                return True, "签到成功"
            if _is_login_page(checked):
                return False, "签到失败：Cookie 已失效"
            if attempt < 2:
                time.sleep(2)
        if _has_signin_form(checked):
            return False, "签到失败：随机答案未通过，站点仍显示签到表单"
        return False, "签到失败：已提交，但多次回读仍未确认当天签到状态"
