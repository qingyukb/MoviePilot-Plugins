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
from typing import Any, Dict, List, Mapping, Tuple


BASE = "https://u2.dmhy.org"
SHOWUP_URL = f"{BASE}/showup.php"

_SIGNED_LINK_RE = re.compile(
    r"<a\b(?=[^>]*\bhref\s*=\s*[\"'](?:https?://u2\.dmhy\.org)?/?showup\.php"
    r"(?:\?[^\"']*)?[\"'])[^>]*>\s*(?:已签到|已簽到|Show Up|Показать)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_SHOWUP_FORM_RE = re.compile(
    r"<form\b(?=[^>]*\baction\s*=\s*[\"'][^\"']*showup\.php[^\"']*[\"'])[^>]*>"
    r"(.*?)</form>",
    re.IGNORECASE | re.DOTALL,
)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_LOGIN_FORM_RE = re.compile(
    r"<form\b(?=[^>]*\baction\s*=\s*[\"'][^\"']*(?:takelogin|login)\.php[^\"']*[\"'])"
    r"[^>]*>.*?<input\b[^>]*\btype\s*=\s*[\"']password[\"']",
    re.IGNORECASE | re.DOTALL,
)


def _attrs(tag: str) -> Dict[str, str]:
    return {name.lower(): value for name, _, value in _ATTR_RE.findall(tag or "")}


def _has_signin_form(page: str) -> bool:
    return bool(_SHOWUP_FORM_RE.search(page or "")
                or re.search(r"<input\b[^>]*\bname\s*=\s*[\"']req[\"']",
                             page or "", re.IGNORECASE))


def _is_signed(page: str) -> bool:
    """已签到页必须同时有已登录痕迹、签到状态链接，且不能再有答题表单。"""
    page = page or ""
    return ("logout.php" in page.casefold()
            and not _has_signin_form(page)
            and bool(_SIGNED_LINK_RE.search(page)))


def _is_login_page(page: str) -> bool:
    return bool(_LOGIN_FORM_RE.search(page or ""))


def _showup_fields(page: str) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """返回 showup 表单的 hidden 字段和 submit 选项。"""
    match = _SHOWUP_FORM_RE.search(page or "")
    if not match:
        return {}, []
    hidden: Dict[str, str] = {}
    options: List[Tuple[str, str]] = []
    for tag in _INPUT_RE.findall(match.group(1)):
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

        # 官方实现也会重新读取 showup.php。提交响应可能只是 JS 跳转，不能据此报成功。
        checked = ctx.page(SHOWUP_URL)
        if _is_signed(checked):
            return True, "签到成功"
        if _is_login_page(checked):
            return False, "签到失败：Cookie 已失效"
        if _has_signin_form(checked):
            return False, "签到失败：随机答案未通过，站点仍显示签到表单"
        return False, "签到失败：已提交，但未回读到已签到状态"
