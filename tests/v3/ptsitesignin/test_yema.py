"""YemaPT API Key 登录检测回归测试。"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents if (parent / "package.json").is_file()), Path(__file__).resolve().parents[1])
PLUGIN_DIRS = [ROOT / "plugins.v2" / "ptsitesignin", ROOT / "plugins.v3" / "ptsitesignin"]


class Response:
    status_code = 200
    def __init__(self): self.closed = False
    def json(self): return {"success": True, "data": {"id": 4514, "status": "enable"}}
    def close(self): self.closed = True


class Session:
    def __init__(self): self.headers = {}; self.calls = []
    def post(self, url, **kwargs): self.calls.append((url, kwargs)); return Response()
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return Response()


class Context:
    timeout = 20
    proxies = None
    def __init__(self): self.session = Session()
    @staticmethod
    def response_json(response, default=None):
        value = response.json() if response is not None else None
        return value if isinstance(value, dict) else ({} if default is None else default)
    @staticmethod
    def close_response(response):
        if response is not None: response.close()
    def _session(self): return self.session
    def page(self, url): return "退出"


def load_yema(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "sites" / "yema.py")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module.YemaPT


class YemaLoginTests(unittest.TestCase):
    def test_api_key_uses_raw_authorization_header_without_cookie(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                Yema = load_yema(directory, f"yema_login_{index}")
                ctx = Context()
                ok, message = Yema().login(
                    {"url": "https://www.yemapt.org/", "apikey": "synthetic-key"}, ctx)
                self.assertTrue(ok, message)
                self.assertEqual(message, "模拟登录成功")
                self.assertEqual(ctx.session.calls[0][1]["headers"]["Authorization"], "synthetic-key")
                self.assertNotIn("Cookie", ctx.session.headers)

    def test_login_only_module_is_discoverable_but_not_used_for_signin(self):
        for index, directory in enumerate(PLUGIN_DIRS):
            with self.subTest(plugin=str(directory)):
                core_spec = importlib.util.spec_from_file_location(f"yema_core_{index}", directory / "core.py")
                core = importlib.util.module_from_spec(core_spec); sys.modules[core_spec.name] = core; core_spec.loader.exec_module(core)
                module = core.site_module_for("https://www.yemapt.org/")
                self.assertIsNotNone(module)
                self.assertTrue(hasattr(module, "login"))
                self.assertFalse(hasattr(module, "signin"))


if __name__ == "__main__":
    unittest.main()
