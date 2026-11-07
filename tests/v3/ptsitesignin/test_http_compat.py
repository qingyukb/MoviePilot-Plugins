"""HTTP 兼容层测试：MoviePilot V2 RequestUtils 适配。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.v3.ptsitesignin.test_core import ROOT


def _modules():
    """加载 V2 / V3 两份 http.py。"""

    modules = []
    for generation in ("v2", "v3"):
        path = ROOT / f"plugins.{generation}" / "ptsitesignin" / "http.py"
        name = f"ptsignin_http_{generation}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


class _Cookies:
    def __init__(self, values):
        self.values = dict(values)

    def get_dict(self):
        return dict(self.values)


class _Response:
    def __init__(self, cookies=None, status_code=200, text="ok"):
        self.cookies = _Cookies(cookies or {})
        self.status_code = status_code
        self.text = text


class _LegacyClient:
    """模拟 MoviePilot V2 的 app.utils.http.RequestUtils。"""

    def __init__(self):
        self.calls = []
        self.closed = False

    def get_cookies(self):
        return {"old": "1"}

    def get_res(self, url, **kwargs):
        self.calls.append(("get_res", url, kwargs))
        return _Response({"hdchina": "new"})

    def post_res(self, url, **kwargs):
        self.calls.append(("post_res", url, kwargs))
        return _Response({"session": "next"})

    def close(self):
        self.closed = True


class HttpCompatTests(unittest.TestCase):
    """验证 V2 get_res/post_res 被正确适配为 requests 风格会话。"""

    @classmethod
    def setUpClass(cls):
        cls.modules = _modules()

    def test_legacy_session_merges_headers_and_remembers_cookies(self):
        for module in self.modules:
            client = _LegacyClient()
            session = module._LegacySdkSession(client)
            session.headers["User-Agent"] = "test-agent"
            response = session.get(
                "https://example.test/index.php",
                headers={"X-Test": "1"},
                timeout=7,
                raise_exception=False,
            )
            self.assertEqual(response.status_code, 200)
            func, url, kwargs = client.calls[0]
            self.assertEqual(func, "get_res")
            self.assertEqual(url, "https://example.test/index.php")
            self.assertEqual(kwargs["headers"]["User-Agent"], "test-agent")
            self.assertEqual(kwargs["headers"]["X-Test"], "1")
            self.assertIs(kwargs["raise_exception"], False)
            self.assertEqual(session.cookies.get_dict(), {"old": "1", "hdchina": "new"})

    def test_legacy_post_uses_post_res(self):
        for module in self.modules:
            client = _LegacyClient()
            session = module._LegacySdkSession(client)
            response = session.post("https://example.test/signin", data={"a": "b"})
            self.assertEqual(response.status_code, 200)
            func, url, kwargs = client.calls[0]
            self.assertEqual(func, "post_res")
            self.assertEqual(url, "https://example.test/signin")
            self.assertEqual(kwargs["data"], {"a": "b"})
            self.assertEqual(session.cookies.get_dict(), {"old": "1", "session": "next"})

    def test_legacy_close_releases_client(self):
        for module in self.modules:
            client = _LegacyClient()
            module._LegacySdkSession(client).close()
            self.assertTrue(client.closed)

    def test_new_session_prefers_v2_request_utils(self):
        from types import ModuleType

        app = ModuleType("app")
        app.__path__ = []
        utils = ModuleType("app.utils")
        utils.__path__ = []
        http_module = ModuleType("app.utils.http")
        http_module.RequestUtils = _LegacyClient
        fake_modules = {
            "app": app,
            "app.utils": utils,
            "app.utils.http": http_module,
        }
        for module in self.modules:
            with mock.patch.dict(sys.modules, fake_modules):
                sys.modules.pop("app.sdk", None)
                sys.modules.pop("app.sdk.network", None)
                session = module.new_http_session()
            self.assertIsInstance(session, module._LegacySdkSession)


if __name__ == "__main__":
    unittest.main()
