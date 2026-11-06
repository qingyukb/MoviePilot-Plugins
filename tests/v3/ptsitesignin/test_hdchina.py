"""瓷器（HDChina）站点模块回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest

from tests.v3.ptsitesignin.test_core import CORES, ROOT


INDEX_UNSIGNED = """
<html><head><meta name="x-csrf" content="csrf-123"></head>
<body><a href="logout.php">退出</a><a href="plugin_sign-in.php">签到</a></body></html>
"""

INDEX_SIGNED = """
<html><head><meta name="x-csrf" content="csrf-123"></head>
<body><a href="logout.php">退出</a>
<a class="label label-default" href="#">已签到</a></body></html>
"""


class _Cookies:
    """模拟 requests 响应 CookieJar。"""

    def __init__(self, values):
        self.values = dict(values)

    def get_dict(self):
        return dict(self.values)


class _Response:
    """最小 HTTP 响应桩。"""

    def __init__(self, text="", status_code=200, cookies=None):
        self.text = text
        self.status_code = status_code
        self.cookies = _Cookies(cookies or {})


class _Context:
    """记录首页读取和签到提交。"""

    def __init__(self, index_response, sign_response=None):
        self.index_response = index_response
        self.sign_response = sign_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self.index_response

    def post(self, url, data=None, headers=None, **kwargs):
        self.post_calls.append({"url": url, "data": dict(data or {}), "headers": dict(headers or {})})
        if self.sign_response is None:
            raise AssertionError(f"未预期的 POST：{url}")
        return self.sign_response


def _modules():
    """加载 V2 / V3 两份瓷器模块。"""

    modules = []
    for generation in ("v2", "v3"):
        path = ROOT / f"plugins.{generation}" / "ptsitesignin" / "sites" / "hdchina.py"
        name = f"hdchina_module_{generation}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


class HDChinaModuleTests(unittest.TestCase):
    """验证 Cookie 过滤、CSRF 请求和严格 JSON 成功判定。"""

    @classmethod
    def setUpClass(cls):
        cls.modules = _modules()

    def test_discovered_by_core(self):
        for core in CORES:
            core._SITE_MODULES = None
            module = core.site_module_for("https://hdchina.org/index.php")
            self.assertIsNotNone(module)
            self.assertEqual(module.site_url, "hdchina.org")

    def test_sign_uses_refreshed_hdchina_cookie(self):
        for module in self.modules:
            ctx = _Context(
                _Response(INDEX_UNSIGNED, cookies={"hdchina": "new-cookie"}),
                _Response('{"state":"success","signindays":10,"integral":20}'),
            )
            ok, message = module.HDChina().signin(
                {"cookie": "foo=bar; hdchina=old-cookie; baz=qux"}, ctx)
            self.assertTrue(ok)
            self.assertIn("签到成功", message)
            self.assertIn("连续 10 天", message)
            self.assertEqual(ctx.get_calls[0]["headers"], {"Cookie": "hdchina=old-cookie"})
            self.assertEqual(ctx.post_calls[0]["data"], {"csrf": "csrf-123"})
            self.assertEqual(ctx.post_calls[0]["headers"]["Cookie"], "hdchina=new-cookie")
            self.assertEqual(ctx.post_calls[0]["headers"]["X-Requested-With"], "XMLHttpRequest")

    def test_default_rule_recognizes_signed_html(self):
        for core in CORES:
            rule = core.resolve_rule({"url": "https://hdchina.org/", "cookie": "hdchina=x"}, {})
            self.assertEqual(core.classify(INDEX_SIGNED, rule).status, "already")

    def test_already_signed_does_not_post(self):
        for module in self.modules:
            ctx = _Context(_Response(INDEX_SIGNED))
            ok, message = module.HDChina().signin({"cookie": "hdchina=old"}, ctx)
            self.assertTrue(ok)
            self.assertEqual(message, "今日已签到")
            self.assertEqual(ctx.post_calls, [])

    def test_missing_hdchina_cookie_fails_before_request(self):
        for module in self.modules:
            ctx = _Context(_Response(INDEX_UNSIGNED))
            ok, message = module.HDChina().signin({"cookie": "foo=bar"}, ctx)
            self.assertFalse(ok)
            self.assertIn("缺少 hdchina=", message)
            self.assertEqual(ctx.get_calls, [])
            self.assertEqual(ctx.post_calls, [])

    def test_non_success_state_is_failure(self):
        for module in self.modules:
            ctx = _Context(_Response(INDEX_UNSIGNED), _Response('{"state":"error","msg":"签到失败"}'))
            ok, message = module.HDChina().signin({"cookie": "hdchina=old"}, ctx)
            self.assertFalse(ok)
            self.assertIn("签到失败", message)

    def test_non_object_json_is_failure(self):
        for module in self.modules:
            ctx = _Context(_Response(INDEX_UNSIGNED), _Response("[]"))
            ok, message = module.HDChina().signin({"cookie": "hdchina=old"}, ctx)
            self.assertFalse(ok)
            self.assertIn("结构不正确", message)

    def test_blocked_or_missing_csrf_is_failure(self):
        for module in self.modules:
            blocked = _Context(_Response("<html>阻断页面</html>"))
            ok, message = module.HDChina().signin({"cookie": "hdchina=old"}, blocked)
            self.assertFalse(ok)
            self.assertIn("阻断页面", message)
            missing = _Context(_Response("<html><body>无令牌</body></html>"))
            ok, message = module.HDChina().signin({"cookie": "hdchina=old"}, missing)
            self.assertFalse(ok)
            self.assertIn("x-csrf", message)


if __name__ == "__main__":
    unittest.main()
