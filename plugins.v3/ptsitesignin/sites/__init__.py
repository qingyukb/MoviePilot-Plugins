"""站点模块基类与运行上下文。

规则表达不了的站点（图片验证码、答题选图、API Token）放在这个目录里，一个站一个文件。
模块只需要定义一个带 ``site_url`` 和 ``signin(site, ctx)`` 的类，插件会自动发现并优先使用它。

示例：

    class Hdsky:
        site_url = "hdsky.me"

        def signin(self, site, ctx):
            page = ctx.page("https://hdsky.me/")
            if "已签到" in page:
                return True, "今日已签到"
            return False, "签到失败"
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class SignContext:
    """给站点模块用的小工具：带 Cookie 的 GET / POST、取图片、OCR。"""

    def __init__(self, site: Mapping[str, Any], timeout: int = 30,
                 proxies: Optional[Mapping[str, str]] = None, logger: Any = None) -> None:
        self.site = site
        self.cookie = str(site.get("cookie") or "").strip()
        self.ua = str(site.get("ua") or "").strip() or DEFAULT_UA
        self.timeout = timeout
        self.proxies = dict(proxies) if proxies else None
        self.logger = logger

    # ---------------- 基础请求 ----------------
    def _session(self):
        import requests
        session = requests.Session()
        session.headers.update({"User-Agent": self.ua, "Cookie": self.cookie,
                                "Accept": "text/html,application/json,*/*;q=0.8"})
        return session

    def get(self, url: str, **kwargs):
        try:
            return self._session().get(url, timeout=self.timeout, allow_redirects=True,
                                       proxies=self.proxies, **kwargs)
        except Exception:
            return None

    def post(self, url: str, data=None, json_body=None, headers=None, **kwargs):
        try:
            return self._session().post(url, data=data, json=json_body, headers=headers,
                                        timeout=self.timeout, allow_redirects=True,
                                        proxies=self.proxies, **kwargs)
        except Exception:
            return None

    def page(self, url: str, **kwargs) -> str:
        response = self.get(url, **kwargs)
        return (response.text if response is not None and response.status_code == 200 else "")

    def image(self, url: str, referer: str = "") -> bytes:
        headers = {"Referer": referer} if referer else None
        response = self.get(url, headers=headers)
        if response is None or response.status_code != 200:
            return b""
        return response.content or b""

    # ---------------- OCR（用 MoviePilot 自带的识别器）----------------
    def ocr(self, image_url: str) -> str:
        try:
            from app.helper.ocr import OcrHelper
        except Exception:
            if self.logger:
                self.logger.warning("PT站点签到：当前版本没有 OcrHelper，无法识别验证码")
            return ""
        try:
            return (OcrHelper().get_captcha_text(image_url=image_url, cookie=self.cookie,
                                                 ua=self.ua) or "").strip()
        except Exception:
            return ""


def domain_matches(url: str, site_url: str) -> bool:
    """站点 URL 是否属于模块声明的域名（含子域名）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    site_url = str(site_url or "").lower()
    if not host or not site_url:
        return False
    return host == site_url or host.endswith("." + site_url)


def sign_via_module(module: Any, site: Mapping[str, Any], ctx: SignContext) -> Tuple[bool, str]:
    """调用模块的 signin，异常统一转成失败文案。"""
    try:
        state, message = module().signin(site, ctx)
        return bool(state), str(message or "")
    except Exception as error:  # 模块 bug 不该把整站点搞崩
        return False, f"站点模块出错：{error}"
