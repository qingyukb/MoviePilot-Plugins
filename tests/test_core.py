"""core.py 回归测试：不依赖 MoviePilot 运行时。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIRS = (
    ROOT / "plugins.v2" / "ptsitesignin",
    ROOT / "plugins.v3" / "ptsitesignin",
)


def load_core(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "core.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORES = [load_core(directory, f"ptsignin_core_{index}")
         for index, directory in enumerate(PLUGIN_DIRS)]


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.core = CORES[0]
        self.audiences = self.core.DEFAULT_RULES["audiences.me"]

    def test_v2_and_v3_core_load(self):
        for core in CORES:
            self.assertIn("audiences.me", core.DEFAULT_RULES)
            self.assertEqual(core.DEFAULT_RULES["audiences.me"]["engine"], "http")

    def test_audiences_success_not_misreported_as_already(self):
        html = "<html><body>签到成功！您已连续签到 3 天，获得 22 粒爆米花</body></html>"
        result = self.core.judge_http(html, self.audiences)
        self.assertEqual(result.status, "success")
        self.assertIn("22", result.message)

    def test_audiences_already_message(self):
        result = self.core.judge_http("<html><body>您今天已经签到过了</body></html>",
                                      self.audiences)
        self.assertEqual(result.status, "already")

    def test_audiences_rule_does_not_use_bare_already_word(self):
        self.assertNotIn("已签到", self.audiences["already"])
        self.assertIn("签到成功", self.audiences["success"])

    def test_success_is_checked_before_default_already_pattern(self):
        result = self.core.judge_http("<html><body>签到成功，您已连续签到 12 天</body></html>", {})
        self.assertEqual(result.status, "success")

    def test_visible_already_wins_over_script_success_branch(self):
        html = ("<html><body>您今天已经签到过了</body>"
                "<script>function f(){return '签到成功';} var state='success';</script></html>")
        result = self.core.judge_http(html, {})
        self.assertEqual(result.status, "already")

    def test_script_only_state_success_fallback(self):
        html = "<html><head><script>var state='success';</script></head><body>loading</body></html>"
        result = self.core.judge_http(html, {})
        self.assertEqual(result.status, "success")

    def test_cloudflare_page(self):
        result = self.core.judge_http("<html><title>Just a moment...</title></html>", {})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.challenge, "cloudflare")

    def test_captcha_page(self):
        result = self.core.judge_http("<html><body>请输入验证码</body></html>", {})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.challenge, "captcha")

    def test_cookie_invalid_page(self):
        result = self.core.judge_http("<html><body>请先登录</body></html>", {})
        self.assertEqual(result.status, "failed")
        self.assertIn("Cookie", result.message)

    def test_json_success_already_and_failure(self):
        success = self.core.judge_http('{"state":"success","message":"签到成功"}', {})
        self.assertEqual(success.status, "success")
        already = self.core.judge_http('{"state":"fail","message":"您今天已经签到过了"}', {})
        self.assertEqual(already.status, "already")
        failed = self.core.judge_http('{"state":"fail","message":"服务器繁忙"}', {})
        self.assertEqual(failed.status, "failed")

    def test_extract_bonus(self):
        self.assertIn("22", self.core.extract_bonus("签到成功，获得 22 粒爆米花", {}))
        self.assertIn("30", self.core.extract_bonus("签到已得 30 魔力值", {}))

    def test_classify_browserless_keywords(self):
        rule = {"success_keywords": ["签到成功"], "already_keywords": ["已签到"]}
        success = self.core.classify("<html><body>签到成功，获得 5 魔力值</body></html>", rule)
        self.assertEqual(success.status, "success")
        already = self.core.classify("<html><body>今天已签到</body></html>", rule)
        self.assertEqual(already.status, "already")

    def test_resolve_rule_host_and_site_id(self):
        site = {"id": "5", "url": "https://www.audiences.me"}
        preset = self.core.resolve_rule(site, {})
        self.assertEqual(preset["engine"], "http")
        self.assertEqual(preset["path"], "/attendance.php")
        self.assertEqual(self.core.resolve_rule(site, {"audiences.me": {"path": "/signin.php"}})["path"],
                         "/signin.php")
        self.assertEqual(self.core.resolve_rule(site, {"5": {"path": "/id.php"}})["path"], "/id.php")

    def test_validate_rule_rejects_external_path(self):
        with self.assertRaises(ValueError):
            self.core.validate_rule({"engine": "http", "path": "https://evil.example/"})
        with self.assertRaises(ValueError):
            self.core.validate_rule({"engine": "browserless", "mode": "open_page", "path": "//evil"})

    def test_cookie_objects_and_bql_endpoint(self):
        cookies = self.core.cookie_objects("a=1; b=2", "https://audiences.me/attendance.php")
        self.assertEqual([cookie["name"] for cookie in cookies], ["a", "b"])
        self.assertTrue(all(cookie["secure"] for cookie in cookies))
        self.assertEqual(self.core.bql_endpoint("https://example.com", "tok"),
                         "https://example.com/stealth/bql?token=tok")
        with self.assertRaises(ValueError):
            self.core.bql_endpoint("", "tok")

    def test_parse_rules(self):
        rules = self.core.parse_rules('{"Audiences.ME": {"path": "/x"}}')
        self.assertIn("audiences.me", rules)
        with self.assertRaises(ValueError):
            self.core.parse_rules("{bad json")


if __name__ == "__main__":
    unittest.main()
