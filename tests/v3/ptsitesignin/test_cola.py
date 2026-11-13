"""ColaPT 每日登录 / 每周签到模块测试。"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents if (parent / "package.json").is_file()), Path(__file__).resolve().parents[1])
PLUGIN_DIRS = [ROOT / "plugins.v2" / "ptsitesignin", ROOT / "plugins.v3" / "ptsitesignin"]


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""
    def json(self): return self._payload


class Context:
    def __init__(self, gets, posts):
        self._gets = list(gets); self._posts = list(posts); self.get_calls = []; self.post_calls = []
    @staticmethod
    def response_json(response, default=None):
        value = response.json() if response is not None else None
        return value if isinstance(value, dict) else ({} if default is None else default)
    @staticmethod
    def close_response(response):
        return None
    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs)); return self._gets.pop(0)
    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs)); return self._posts.pop(0)


def load_cola(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "sites" / "cola.py")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module.ColaPT


class ColaTests(unittest.TestCase):
    def test_daily_and_weekly_are_claimed_from_cookie_session(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                Cola = load_cola(directory, f"cola_{index}")
                ctx = Context([
                    Response({"status": "active", "session": {"csrf_token": "csrf"}}),
                    Response({"today": {"logged_in": False}, "week": {"checked_in": False}}),
                ], [Response({"earned": 3}), Response({"earned": 10})])
                ok, message = Cola().signin({"url": "https://www.colapt.com/", "cookie": "c=1"}, ctx)
                self.assertTrue(ok, message)
                self.assertIn("每日登录 +3", message)
                self.assertIn("签到成功", message)
                self.assertEqual(len(ctx.post_calls), 2)
                self.assertEqual(ctx.post_calls[0][1]["headers"]["X-CSRF-Token"], "csrf")
                self.assertTrue(Cola.authoritative)

    def test_already_claimed_state_is_reported_without_posts(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                Cola = load_cola(directory, f"cola_already_{index}")
                ctx = Context([
                    Response({"status": "active", "session": {"csrf_token": "csrf"}}),
                    Response({"today": {"logged_in": True}, "week": {"checked_in": True}}),
                ], [])
                ok, message = Cola().signin({"url": "https://www.colapt.com/", "cookie": "c=1"}, ctx)
                self.assertTrue(ok, message)
                self.assertIn("每日登录已领取", message)
                self.assertIn("本周已签到", message)
                self.assertEqual(ctx.post_calls, [])

    def test_api_key_only_is_not_reported_as_signin_success(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                Cola = load_cola(directory, f"cola_key_{index}")
                ok, message = Cola().signin(
                    {"url": "https://www.colapt.com/", "apikey": "key"}, Context([], []))
                self.assertFalse(ok)
                self.assertIn("API Key", message)

    def test_login_uses_cookie_session(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                Cola = load_cola(directory, f"cola_login_{index}")
                ctx = Context([Response({"status": "active"})], [])
                self.assertEqual(Cola().login({"url": "https://www.colapt.com/", "cookie": "c=1"}, ctx),
                                 (True, "模拟登录成功"))


if __name__ == "__main__":
    unittest.main()
