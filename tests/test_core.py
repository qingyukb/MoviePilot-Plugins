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


# Cloudflare「Just a moment...」交互挑战页（真实抓取自 audiences.me/attendance.php）
CF_INTERSTITIAL_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<script>window._cf_chl_opt={cvId:'3'};var s='/cdn-cgi/challenge-platform"
    "/h/b/orchestrate/chl_page/v1';</script></body></html>")
# 挑战放行后的真实签到页（GET 即完成签到，页面同时含「签到成功」与「已连续签到 N 天」）
REAL_ATTENDANCE_HTML = (
    "<html><body>签到成功！您已连续签到 3 天，获得 22 粒爆米花</body></html>")
AUDIENCES_SITE = {"url": "https://audiences.me", "cookie": "a=1; b=2", "ua": ""}


def make_stub_signer(core, responses):
    """返回一个不联网的 BrowserlessSigner，按顺序吐出预置响应。"""
    class _StubSigner(core.BrowserlessSigner):
        def __init__(self):
            self.responses = list(responses)
            self.calls = []

        def _request(self, variables, operation, query):
            self.calls.append((operation, dict(variables)))
            return self.responses.pop(0)

    return _StubSigner()


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


    def test_cloudflare_interstitial_detection(self):
        interstitial = ("<html><head><title>Just a moment...</title></head><body>"
                        "<script>window._cf_chl_opt={cvId:'3'};var s='/cdn-cgi/"
                        "challenge-platform/h/b/orchestrate/chl_page/v1';</script></body></html>")
        # 正常页面里的 Turnstile 组件（含 challenge-platform）不能当成卡在挑战页
        turnstile = ("<html><body><div class='cf-turnstile' data-sitekey='k'></div>"
                     "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'>"
                     "</script></body></html>")
        real = "<html><body>签到成功！您已连续签到 3 天</body></html>"
        self.assertTrue(self.core.cloudflare_pending(interstitial))
        self.assertFalse(self.core.cloudflare_pending(turnstile))
        self.assertFalse(self.core.cloudflare_pending(real))
        self.assertFalse(self.core.cloudflare_pending(""))

    def test_cloudflare_wait_ms_defaults_and_clamps(self):
        self.assertEqual(self.core.cloudflare_wait_ms({"mode": "cloudflare"}), 10000)
        self.assertEqual(self.core.cloudflare_wait_ms({"cloudflare_wait": "20"}), 20000)
        self.assertEqual(self.core.cloudflare_wait_ms({"cloudflare_wait": 1}), 3000)
        self.assertEqual(self.core.cloudflare_wait_ms({"cloudflare_wait": 999}), 45000)
        self.assertEqual(self.core.cloudflare_wait_ms({"cloudflare_wait": "bad"}), 10000)

    def test_navigation_ok_handles_cloudflare_challenge_status(self):
        interstitial = "<html><title>Just a moment...</title>_cf_chl_opt</html>"
        real = "<html><body>签到成功</body></html>"
        self.assertTrue(self.core.navigation_ok("cloudflare", 200, real))
        self.assertFalse(self.core.navigation_ok("cloudflare", 403, interstitial))
        # 挑战已自动放行：goto 记录的仍是挑战页的 403，但页面已是真实页面
        self.assertTrue(self.core.navigation_ok("cloudflare", 403, real))
        self.assertTrue(self.core.navigation_ok("cloudflare", 503, real))
        self.assertFalse(self.core.navigation_ok("open_page", 403, real))
        self.assertFalse(self.core.navigation_ok("cloudflare", None, real))

    def test_fallback_rule_carries_cloudflare_wait(self):
        fallback = self.core.fallback_rule(
            {"engine": "http", "path": "/attendance.php", "cloudflare_wait": 25}, "cloudflare")
        self.assertEqual(fallback["engine"], "browserless")
        self.assertEqual(fallback["mode"], "cloudflare")
        self.assertEqual(fallback["path"], "/attendance.php")
        self.assertEqual(fallback["cloudflare_wait"], 25)


    def test_browserless_waits_out_cloudflare_and_reports_success(self):
        rule = self.core.fallback_rule(self.audiences, "cloudflare")
        signer = make_stub_signer(self.core, [
            # 预检仍停在挑战页：不能就此判定「打开签到页失败」
            {"goto": {"status": 403}, "before": {"html": CF_INTERSTITIAL_HTML}},
            # 主流程等满预算后拿到真实页面
            {"goto": {"status": 403}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": CF_INTERSTITIAL_HTML},
             "after": {"html": REAL_ATTENDANCE_HTML}},
        ])
        result = signer.sign(AUDIENCES_SITE, rule)
        self.assertEqual(result.status, "success")
        self.assertIn("22", result.message)
        self.assertEqual([call[0] for call in signer.calls], ["CheckInPreflight", "CheckIn"])
        # 默认在页面上等 10 秒等挑战自动放行，而不是旧的 2 秒
        self.assertEqual(signer.calls[1][1]["beforeWait"], 10000)
        self.assertEqual(signer.calls[1][1]["wait"], 3500)

    def test_browserless_preflight_success_skips_submit(self):
        rule = self.core.fallback_rule(self.audiences, "cloudflare")
        signer = make_stub_signer(self.core, [
            {"goto": {"status": 403}, "before": {"html": REAL_ATTENDANCE_HTML}},
        ])
        result = signer.sign(AUDIENCES_SITE, rule)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(signer.calls), 1)

    def test_browserless_cloudflare_timeout_reports_actionable_message(self):
        rule = self.core.fallback_rule(self.audiences, "cloudflare")
        signer = make_stub_signer(self.core, [
            {"goto": {"status": 403}, "before": {"html": CF_INTERSTITIAL_HTML}},
            {"goto": {"status": 403}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": CF_INTERSTITIAL_HTML},
             "after": {"html": CF_INTERSTITIAL_HTML}},
        ])
        result = signer.sign(AUDIENCES_SITE, rule)
        self.assertEqual(result.status, "failed")
        self.assertIn("Cloudflare", result.message)
        self.assertIn("10 秒", result.message)
        self.assertIn("cloudflare_wait", result.message)

    def test_browserless_cloudflare_wait_override_is_honoured(self):
        rule = self.core.fallback_rule(
            dict(self.audiences, cloudflare_wait=20), "cloudflare")
        signer = make_stub_signer(self.core, [
            {"goto": {"status": 403}, "before": {"html": CF_INTERSTITIAL_HTML}},
            {"goto": {"status": 403}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": CF_INTERSTITIAL_HTML},
             "after": {"html": REAL_ATTENDANCE_HTML}},
        ])
        result = signer.sign(AUDIENCES_SITE, rule)
        self.assertEqual(result.status, "success")
        self.assertEqual(signer.calls[1][1]["beforeWait"], 20000)


if __name__ == "__main__":
    unittest.main()
