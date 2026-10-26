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

import json
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

    # ---------------- OCR（MP 的 OcrHelper → 本地 ddddocr 兜底）----------------
    def ocr(self, image_url: str, referer: str = "") -> str:
        """识别验证码图片。

        MP 的 OcrHelper 走 ``OCR_HOST``（默认 https://movie-pilot.org），容器访问不了
        外网时它一律返回空字符串，日志里就只剩「OCR 没识别出验证码」。所以这里：
        1) 自己带着站点 Cookie 把图片下下来（避免 OcrHelper 下载时丢登录态）；
        2) 先交给 MP 的 OcrHelper（传 image_data，跳过它自己的下载）；
        3) 还不行再试本地 ddddocr（装了就能用，不依赖任何外部服务）。
        """
        data = self.image(image_url, referer=referer)
        if not data:
            if self.logger:
                self.logger.warning("PT站点签到：验证码图片下载失败（%s）", image_url)
            return ""
        text = self._ocr_via_moviepilot(data)
        if text:
            return text
        text = self._ocr_via_ddddocr(data)
        if text:
            if self.logger:
                self.logger.info("PT站点签到：MP 的 OCR 没结果，已改用本地 ddddocr 识别成功")
            return text
        if self.logger:
            self.logger.warning(
                "PT站点签到：验证码识别失败——MP 的 OCR 服务（OCR_HOST，默认 "
                "https://movie-pilot.org）不可用，且本机没有安装 ddddocr；"
                "请在 MP 设置里把 OCR_HOST 指向可访问的 OCR 服务，或 pip install ddddocr")
        return ""


    @staticmethod
    def _clean(text: Any) -> str:
        """去掉识别结果里的空白与常见噪声字符。"""
        return "".join(str(text or "").split())

    def _ocr_via_moviepilot(self, data: bytes) -> str:
        try:
            from app.helper.ocr import OcrHelper
        except Exception:
            try:
                from app.core.ocr import OcrHelper  # 老版本路径
            except Exception:
                return ""
        try:
            result = OcrHelper().get_captcha_text(image_data=data)
        except Exception:
            return ""
        return self._clean(result)

    @staticmethod
    def _ocr_via_ddddocr(data: bytes) -> str:
        try:
            import ddddocr
        except Exception:
            return ""
        try:
            engine = ddddocr.DdddOcr(show_ad=False)
            return "".join(str(engine.classification(data) or "").split())
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
