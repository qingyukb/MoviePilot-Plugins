"""好学（HXPT，hxpt.org）签到模块回归测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

from tests.v3.ptsitesignin.test_core import CORES, ROOT


class _Cookies:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_dict(self):
        return dict(self.values)


class _Response:
    def __init__(self, payload=None, status_code=200, text=None, cookies=None):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload or {})
        self.cookies = _Cookies(cookies)


class _Context:
    def __init__(self, home, csrf=None, checkin=None):
        self.responses = {
            "https://www.hxpt.org/api/v1/home": home,
            "https://www.hxpt.org/api/v1/me/csrf": csrf,
            "https://www.hxpt.org/api/v1/attendance/checkin": checkin,
        }
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self.responses.get(url)

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.responses.get(url)


def _modules():
    modules = []
    for generation in ("v2", "v3"):
        path = ROOT / f"plugins.{generation}" / "ptsitesignin" / "sites" / "hxpt.py"
        name = f"hxpt_module_{generation}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


class HxptModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = _modules()

    def test_discovered_by_core(self):
        for core in CORES:
            core._SITE_MODULES = None
            module = core.site_module_for("https://www.hxpt.org/")
            self.assertIsNotNone(module)
            self.assertEqual(module.site_url, "hxpt.org")

    def test_success_posts_with_csrf_cookie(self):
        for module in self.modules:
            ctx = _Context(
                _Response({"code": 0, "data": {"attendance": {"checked_today": False}}}),
                checkin=_Response({"code": 0, "data": {"reward": 20, "streak": 3}}),
            )
            ok, message = module.Hxpt().signin(
                {"cookie": "flux.session=session-1; flux_csrf=csrf-1"}, ctx)
            self.assertTrue(ok)
            self.assertIn("签到成功", message)
            self.assertIn("20", message)
            self.assertIn("3", message)
            self.assertEqual(ctx.post_calls[0]["headers"]["x-csrf-token"], "csrf-1")
            self.assertEqual(ctx.post_calls[0]["headers"]["Cookie"],
                             "flux.session=session-1; flux_csrf=csrf-1")

    def test_missing_csrf_is_fetched_from_me_csrf(self):
        for module in self.modules:
            ctx = _Context(
                _Response({"code": 0, "data": {"attendance": {"checked_today": False}}}),
                csrf=_Response({"code": 0, "data": None}, cookies={"flux_csrf": "csrf-new"}),
                checkin=_Response({"code": 0, "data": {"reward": 10, "streak": 1}}),
            )
            ok, message = module.Hxpt().signin(
                {"cookie": "flux.session=session-2"}, ctx)
            self.assertTrue(ok)
            self.assertIn("签到成功", message)
            self.assertEqual([call["url"] for call in ctx.get_calls], [
                "https://www.hxpt.org/api/v1/home",
                "https://www.hxpt.org/api/v1/me/csrf",
            ])
            self.assertEqual(ctx.post_calls[0]["headers"]["x-csrf-token"], "csrf-new")

    def test_already_signed_does_not_post(self):
        for module in self.modules:
            ctx = _Context(_Response({
                "code": 0,
                "data": {"attendance": {"checked_today": True, "streak": 9}},
            }))
            ok, message = module.Hxpt().signin(
                {"cookie": "flux.session=session-3; flux_csrf=csrf-3"}, ctx)
            self.assertTrue(ok)
            self.assertEqual(message, "今日已签到")
            self.assertEqual(ctx.post_calls, [])

    def test_unauthorized_cookie_fails(self):
        for module in self.modules:
            ctx = _Context(_Response({"code": 2001, "message": "未认证"}, status_code=401))
            ok, message = module.Hxpt().signin({"cookie": "flux.session=bad"}, ctx)
            self.assertFalse(ok)
            self.assertIn("Cookie 已失效", message)
            self.assertEqual(ctx.post_calls, [])

    def test_post_business_error_is_failure(self):
        for module in self.modules:
            ctx = _Context(
                _Response({"code": 0, "data": {"attendance": {"checked_today": False}}}),
                checkin=_Response({"code": 5000, "message": "签到失败"}),
            )
            ok, message = module.Hxpt().signin(
                {"cookie": "flux.session=s; flux_csrf=c"}, ctx)
            self.assertFalse(ok)
            self.assertIn("签到失败", message)

    def test_default_rule_detects_checked_today(self):
        html = '{"code":0,"data":{"attendance":{"checked_today":true}}}'
        ui = '<html><body><div>签到得魔力</div><span>今日已签到</span></body></html>'
        for core in CORES:
            rule = core.resolve_rule({"url": "https://www.hxpt.org/", "cookie": "x=1"}, {})
            self.assertEqual(core.judge_http(html, rule).status, "already")
            self.assertEqual(core.judge_http(ui, rule).status, "already")


if __name__ == "__main__":
    unittest.main()
