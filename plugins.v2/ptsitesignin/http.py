"""插件 HTTP 会话兼容层。

V3 优先使用官方 ``app.sdk.network.RequestUtils``；V2 没有该 SDK 时回退到
``requests.Session``。业务代码只依赖 ``get`` / ``post`` / ``headers`` / ``cookies``
这组最小会话接口，避免在插件正文散落第三方客户端实现。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator


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
    RequestUtils 的最小会话兼容包装。

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
        return self._client.request(method=method, url=url,
                                    raise_exception=True, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        """
        发送 GET 请求。

        :param url: 请求地址
        :param kwargs: requests 兼容请求参数
        :return: HTTP 响应
        """
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """
        发送 POST 请求。

        :param url: 请求地址
        :param kwargs: requests 兼容请求参数
        :return: HTTP 响应
        """
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """
        释放 RequestUtils 持有的会话。
        """
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


def new_http_session() -> Any:
    """
    创建插件 HTTP 会话。

    V3 优先使用官方 SDK，V2 没有 SDK 时回退到 requests。

    :return: 带 requests 兼容接口的 HTTP 会话
    """
    try:
        from app.sdk.network import RequestUtils
    except ImportError:
        import requests

        return requests.Session()
    return _SdkSession(RequestUtils(use_session=True))
