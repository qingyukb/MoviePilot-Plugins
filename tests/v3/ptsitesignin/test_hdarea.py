"""高清视界 API Key / Cookie 双路径回归测试。"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = next((parent for parent in Path(__file__).resolve().parents if (parent / "package.json").is_file()), Path(__file__).resolve().parents[1])
PLUGIN_DIRS = [ROOT / "plugins.v2" / "ptsitesignin", ROOT / "plugins.v3" / "ptsitesignin"]


def load_hdarea(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "sites" / "hdarea.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.HDArea


class Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response


class Context:
    def __init__(self, response):
        self.response = response
        self.timeout = 15
        self.proxies = None
        self.post_calls = []

    @staticmethod
    def response_json(response, default=None):
        value = response.json() if response is not None else None
        return value if isinstance(value, dict) else ({} if default is None else default)

    @staticmethod
    def close_response(response):
        if response is not None:
            response.close()

    def _session(self):
        return Session(self.response)

    def post(self, url, data=None, **kwargs):
        self.post_calls.append((url, data, kwargs))
        return self.response

    def page(self, url):
        return self.response.text


class HDAreaTests(unittest.TestCase):
    def test_api_key_signing_and_login(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                HDArea = load_hdarea(directory, f"hdarea_api_{index}")
                response = Response(payload={"success": True, "code": 0, "message": "OK",
                                             "data": {"already_signed": False}})
                ctx = Context(response)
                site = {"url": "https://hdarea.club/", "apikey": "secret", "cookie": "old"}
                self.assertEqual(HDArea().signin(site, ctx), (True, "签到成功"))
                self.assertEqual(ctx.post_calls, [])
                # login 使用 profile 接口；同一测试中替换响应数据
                response._payload = {"success": True, "code": 0, "message": "OK",
                                     "data": {"id": 42, "enabled": True}}
                self.assertEqual(HDArea().login(site, ctx), (True, "模拟登录成功"))

    def test_api_failure_does_not_fallback_to_cookie(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                HDArea = load_hdarea(directory, f"hdarea_fail_{index}")
                response = Response(status=500, payload={"success": False, "code": 500, "message": "bad"})
                ctx = Context(response)
                ok, message = HDArea().signin(
                    {"url": "https://hdarea.club/", "apikey": "secret", "cookie": "old"}, ctx)
                self.assertFalse(ok)
                self.assertIn("HTTP 500", message)
                self.assertEqual(ctx.post_calls, [])

    def test_malformed_api_payload_is_rejected(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                HDArea = load_hdarea(directory, f"hdarea_malformed_{index}")
                ctx = Context(Response(payload="not-json"))
                ok, message = HDArea().signin(
                    {"url": "https://hdarea.club/", "apikey": "secret"}, ctx)
                self.assertFalse(ok)
                self.assertIn("响应结构", message)

    def test_cookie_fallback_and_login(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                HDArea = load_hdarea(directory, f"hdarea_cookie_{index}")
                response = Response(text="此次签到您获得100魔力值")
                ctx = Context(response)
                self.assertEqual(
                    HDArea().signin({"url": "https://hdarea.club/", "cookie": "old"}, ctx),
                    (True, "签到成功"))
                self.assertEqual(len(ctx.post_calls), 1)
                response.text = '<a href="logout.php">退出</a>'
                self.assertEqual(
                    HDArea().login({"url": "https://hdarea.club/", "cookie": "old"}, ctx),
                    (True, "模拟登录成功"))


if __name__ == "__main__":
    unittest.main()
