"""U2 showup 四选一签到模块回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.v3.ptsitesignin.test_core import CORES, ROOT


UNSIGNED_PAGE = """
<html><body>
  <a href="logout.php">退出</a>
  <form action="showup.php?action=show" method="post">
    <input type="hidden" name="req" value="req-token">
    <input type="hidden" name="hash" value="hash-token">
    <input type="hidden" name="form" value="form-token">
    <input type="hidden" name="_csrf" value="csrf-token">
    <input type="submit" name="answer0" value="0">
    <input type="submit" name="answer1" value="1">
    <input type="submit" name="answer2" value="2">
    <input type="submit" name="answer3" value="3">
  </form>
</body></html>
"""

SIGNED_PAGE = """
<html><body>
  <a href="logout.php">退出</a>
  <a href="showup.php">已签到</a>
</body></html>
"""

LOGIN_PAGE = """
<html><body>
  <form action="takelogin.php" method="post">
    <input name="username"><input type="password" name="password">
  </form>
</body></html>
"""


class _Response:
    """最小 HTTP 响应桩。"""

    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _Context:
    """按顺序返回页面，并记录一次 POST。"""

    def __init__(self, pages, response=None):
        self.pages = list(pages)
        self.response = response
        self.posts = []

    def page(self, url, **kwargs):
        return self.pages.pop(0) if self.pages else ""

    def post(self, url, data=None, headers=None, **kwargs):
        self.posts.append({"url": url, "data": dict(data or {}), "headers": dict(headers or {})})
        if self.response is None:
            raise AssertionError(f"未预期的 POST：{url}")
        return self.response


def _modules():
    """加载仓库内 V2 / V3 两份 U2 模块。"""

    modules = []
    for generation in ("v2", "v3"):
        path = ROOT / f"plugins.{generation}" / "ptsitesignin" / "sites" / "u2.py"
        name = f"u2_module_{generation}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


class U2ModuleTests(unittest.TestCase):
    """验证 U2 一次性字段解析、随机答题和回读确认。"""

    @classmethod
    def setUpClass(cls):
        cls.modules = _modules()

    def test_discovered_by_core(self):
        for core in CORES:
            core._SITE_MODULES = None
            module = core.site_module_for("https://u2.dmhy.org/showup.php")
            self.assertIsNotNone(module)
            self.assertEqual(module.site_url, "u2.dmhy.org")

    def test_parse_hidden_fields_and_answers(self):
        for module in self.modules:
            hidden, options = module._showup_fields(UNSIGNED_PAGE)
            self.assertEqual(
                hidden,
                {"req": "req-token", "hash": "hash-token",
                 "form": "form-token", "_csrf": "csrf-token"},
            )
            self.assertEqual(
                options,
                [("answer0", "0"), ("answer1", "1"),
                 ("answer2", "2"), ("answer3", "3")],
            )

    def test_success_requires_readback_after_post(self):
        response = _Response(200, "<script>window.location.href='showup.php';</script>")
        for module in self.modules:
            ctx = _Context([UNSIGNED_PAGE, SIGNED_PAGE], response)
            with mock.patch.object(module.random, "choice", return_value=("answer2", "2")):
                ok, message = module.U2().signin(
                    {"url": "https://u2.dmhy.org", "cookie": "u2=1"}, ctx)
            self.assertTrue(ok)
            self.assertEqual(message, "签到成功")
            self.assertEqual(len(ctx.posts), 1)
            self.assertEqual(ctx.posts[0]["url"], "https://u2.dmhy.org/showup.php?action=show")
            self.assertEqual(
                ctx.posts[0]["data"],
                {"req": "req-token", "hash": "hash-token", "form": "form-token",
                 "_csrf": "csrf-token", "message": "一切随缘~", "answer2": "2"},
            )
            self.assertEqual(ctx.posts[0]["headers"],
                             {"Referer": "https://u2.dmhy.org/showup.php",
                              "Origin": "https://u2.dmhy.org"})

    def test_redirect_without_signed_state_is_failure(self):
        response = _Response(200, "<script>window.location.href='showup.php';</script>")
        for module in self.modules:
            ctx = _Context([UNSIGNED_PAGE, UNSIGNED_PAGE], response)
            with mock.patch.object(module.random, "choice", return_value=("answer0", "0")):
                ok, message = module.U2().signin(
                    {"url": "https://u2.dmhy.org", "cookie": "u2=1"}, ctx)
            self.assertFalse(ok)
            self.assertIn("随机答案未通过", message)
            self.assertEqual(len(ctx.posts), 1)

    def test_already_signed_does_not_post(self):
        for module in self.modules:
            ctx = _Context([SIGNED_PAGE])
            ok, message = module.U2().signin(
                {"url": "https://u2.dmhy.org", "cookie": "u2=1"}, ctx)
            self.assertTrue(ok)
            self.assertEqual(message, "今日已签到")
            self.assertEqual(ctx.posts, [])

    def test_login_page_does_not_post(self):
        for module in self.modules:
            ctx = _Context([LOGIN_PAGE])
            ok, message = module.U2().signin(
                {"url": "https://u2.dmhy.org", "cookie": "u2=1"}, ctx)
            self.assertFalse(ok)
            self.assertIn("Cookie 已失效", message)
            self.assertEqual(ctx.posts, [])


if __name__ == "__main__":
    unittest.main()
