"""插件 HTTP 会话兼容层。

V3 优先使用 ``app.sdk.network.RequestUtils``；V2 使用宿主自带的
``app.utils.http.RequestUtils``；两者都不存在时才回退到 ``requests.Session``。
业务代码只依赖 ``get`` / ``post`` / ``headers`` / ``cookies`` 这组最小会话接口。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator, Mapping


class _CookieSnapshot:
    """把普通 Cookie 字典包装成 requests 风格的可迭代对象。"""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for name, value in self.get_dict().items():
            yield SimpleNamespace(name=name, value=value)

    def get_dict(self) -> dict[str, str]:
        """返回当前 Cookie 快照。"""
        return {str(name): str(value) for name, value in self._values.items()}


class _SdkCookies:
    """
    把 RequestUtils 的 Cookie 快照适配成可迭代对象。

    :param client: RequestUtils 实例
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for name, value in self.get_dict().items():
            yield SimpleNamespace(name=name, value=value)

    def get_dict(self) -> dict[str, str]:
        """
        返回当前会话 Cookie。

        :return: Cookie 名值字典
        """
        getter = getattr(self._client, "get_cookies", None)
        return dict(getter() if callable(getter) else {})


class _SdkSession:
    """
    新版 RequestUtils 的最小会话兼容包装。

    :param client: RequestUtils 实例
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.headers: dict[str, str] = {}

    @property
    def cookies(self) -> _SdkCookies:
        """
        返回当前持久会话的 Cookie 视图。

        :return: 可迭代 Cookie 视图
        """
        return _SdkCookies(self._client)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """
        发送请求。

        :param method: HTTP 方法
        :param url: 请求地址
        :param kwargs: requests 兼容请求参数
        :return: HTTP 响应
        :raises Exception: request 失败且 SDK 要求抛出时透传异常
        """
        headers = dict(self.headers)
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update(extra)
        if headers:
            kwargs["headers"] = headers
        raise_exception = bool(kwargs.pop("raise_exception", True))
        return self._client.request(method=method, url=url,
                                    raise_exception=raise_exception, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        """发送 GET 请求。"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """发送 POST 请求。"""
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """释放 RequestUtils 持有的会话。"""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


class _LegacySdkSession:
    """MoviePilot V2 的 RequestUtils 兼容包装。

    V2 的 ``RequestUtils`` 主要提供 ``get_res`` / ``post_res``，不一定支持
    ``request(raise_exception=...)``。这里把插件统一使用的 requests 风格接口转换成
    V2 宿主接口，同时维护请求头和响应 Cookie 快照。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.headers: dict[str, str] = {}
        self._cookies: dict[str, str] = {}
        getter = getattr(client, "get_cookies", None)
        if callable(getter):
            try:
                self._cookies.update({str(k): str(v) for k, v in getter().items()})
            except Exception:
                pass

    @property
    def cookies(self) -> _CookieSnapshot:
        """返回当前持久会话的 Cookie 视图。"""
        return _CookieSnapshot(self._cookies)

    def _remember_cookies(self, response: Any) -> None:
        cookies = getattr(response, "cookies", None)
        getter = getattr(cookies, "get_dict", None)
        if not callable(getter):
            return
        try:
            self._cookies.update({str(k): str(v) for k, v in getter().items()})
        except Exception:
            return

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """通过 V2 的 get_res / post_res 发送请求。"""
        raise_exception = bool(kwargs.pop("raise_exception", True))
        headers = dict(self.headers)
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        if headers:
            kwargs["headers"] = headers
        if self._cookies:
            kwargs.setdefault("cookies", dict(self._cookies))

        method_upper = str(method or "GET").upper()
        getter = getattr(self._client, "get_res", None)
        poster = getattr(self._client, "post_res", None)
        if method_upper == "GET" and callable(getter):
            sender = getter
        elif method_upper == "POST" and callable(poster):
            sender = poster
        else:
            generic = getattr(self._client, "request", None)
            if not callable(generic):
                raise ValueError(f"V2 RequestUtils 不支持 {method_upper} 请求")
            response = generic(method=method, url=url, raise_exception=raise_exception, **kwargs)
            self._remember_cookies(response)
            return response

        try:
            response = sender(url, raise_exception=raise_exception, **kwargs)
        except TypeError as error:
            # 老版 V2 RequestUtils 不声明 raise_exception，去掉该参数后重试。
            if "raise_exception" not in str(error):
                raise
            response = sender(url, **kwargs)
        if response is None:
            if raise_exception:
                raise ConnectionError("MoviePilot V2 RequestUtils 未返回响应")
            return None
        self._remember_cookies(response)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        """发送 GET 请求。"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """发送 POST 请求。"""
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """释放 V2 RequestUtils 持有的会话。"""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


def new_http_session() -> Any:
    """
    创建插件 HTTP 会话。

    优先使用当前 MoviePilot 版本提供的 RequestUtils；V2 / V3 都缺失时才回退到
    requests.Session，避免 V2 绕过宿主已有的代理、证书与 TLS 兼容处理。

    :return: 带 requests 兼容接口的 HTTP 会话
    """
    try:
        from app.sdk.network import RequestUtils
    except ImportError:
        try:
            from app.utils.http import RequestUtils as LegacyRequestUtils
        except ImportError:
            import requests

            return requests.Session()
        return _LegacySdkSession(LegacyRequestUtils())
    return _SdkSession(RequestUtils(use_session=True))
