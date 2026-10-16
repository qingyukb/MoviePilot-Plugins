"""core.py 回归测试：不依赖 MoviePilot 运行时。"""

from __future__ import annotations

import importlib.util
import json
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
# 观众签到页未签到状态（正文取自站点实际页面：每日签到卡片 + Turnstile 人机验证）
AUDIENCES_PENDING_HTML = (
    "<html><head><title>Audiences :: 签到 - Powered by NexusPHP</title></head><body>"
    "<div>每日签到</div><p>完成人机验证即可领取今日爆米花奖励</p>"
    "<div>人机验证</div><p>验证通过后将自动完成签到</p>"
    "<div class=\"cf-turnstile\" data-sitekey=\"0x4AAAAAA\"></div>"
    "<script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js\"></script>"
    "</body></html>")
# 签到完成状态
AUDIENCES_SIGNED_HTML = (
    "<html><head><title>Audiences :: 签到 - Powered by NexusPHP</title></head><body>"
    "<div>今日已签到</div><p>已获得 22 粒爆米花</p></body></html>")
# 签到成功后站点可能跳转到的首页（正文取自插件日志）
AUDIENCES_HOME_HTML = (
    "<html><head><title>Audiences - Powered by NexusPHP</title></head><body>"
    "上一页 下一页 返回顶部 签到 收件箱 发件箱</body></html>")
# Cookie 失效时 NexusPHP 会把 attendance.php 302 到 login.php?returnto=attendance.php
LOGIN_PAGE_HTML = (
    "<html><head><title>Audiences :: 登录 - Powered by NexusPHP</title></head><body>"
    "<form action=\"takelogin.php\" method=\"post\">"
    "<input name=\"username\"><input name=\"password\" type=\"password\">"
    "<a href=\"login.php?returnto=attendance.php\">登录</a></form></body></html>")
AUDIENCES_SITE = {"url": "https://audiences.me", "cookie": "a=1; b=2", "ua": ""}


def make_stub_signer(core, responses):
    """返回一个不联网的 BrowserlessSigner，按顺序吐出预置响应。"""
    class _StubSigner(core.BrowserlessSigner):
        def __init__(self):
            # 不联网：只补齐 request_timeout() 需要的属性，_request 由下面的桩接管
            self.endpoint = "https://example.test/bql"
            self.timeout = 100
            self.responses = list(responses)
            self.calls = []
            self.queries = []

        def _request(self, variables, operation, query, **kwargs):
            self.calls.append((operation, dict(variables)))
            self.queries.append(query)
            return self.responses.pop(0)

    return _StubSigner()


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.core = CORES[0]
        self.audiences = self.core.DEFAULT_RULES["audiences.me"]

    def test_v2_and_v3_core_load(self):
        for core in CORES:
            self.assertIn("audiences.me", core.DEFAULT_RULES)
            self.assertEqual(core.DEFAULT_RULES["audiences.me"]["engine"], "browserless")

    def test_audiences_success_not_misreported_as_already(self):
        html = "<html><body>签到成功！您已连续签到 3 天，获得 22 粒爆米花</body></html>"
        result = self.core.judge_http(html, self.audiences)
        self.assertEqual(result.status, "success")
        self.assertIn("22", result.message)

    def test_audiences_already_message(self):
        result = self.core.judge_http("<html><body>您今天已经签到过了</body></html>",
                                      self.audiences)
        self.assertEqual(result.status, "already")

    def test_audiences_preset_uses_solver_and_auto_submit(self):
        # 实测：观众站 Turnstile 渲染在 closed shadow DOM 里，DOM/CSS 与 BrowserQL 深度
        # 选择器都命不中 checkbox，所以必须交给官方 solve（支持 iframe / shadow DOM），
        # 再由 auto 提交脚本触发站点 cfCallback / 表单兜底 POST。
        self.assertEqual(self.audiences["mode"], "cloudflare")
        self.assertEqual(self.audiences["submit_method"], "auto")
        self.assertEqual(self.audiences["solve_count"], 2)
        self.assertEqual(self.audiences["solve_timeout"], 40000)
        self.assertEqual(self.audiences["solve2_timeout"], 50000)
        self.assertEqual(self.audiences["solve2_wait"], 5000)
        self.assertEqual(self.audiences["cloudflare_wait"], 15)
        self.assertEqual(self.audiences["auto_wait"], 30000)
        self.assertNotIn("submit_selector", self.audiences)
        self.assertIn("签到成功", self.audiences["success_keywords"])
        self.assertIn("已签到", self.audiences["already_keywords"])
        self.assertTrue(self.audiences["pending_keywords"])

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

    def test_turnstile_page_asks_for_browser_engine(self):
        result = self.core.judge_http(AUDIENCES_PENDING_HTML, {})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.challenge, "cloudflare")

    def test_turnstile_page_with_success_text_wins(self):
        html = ("<html><body>签到成功<div class=\"cf-turnstile\"></div>"
                "<script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js\"></script>"
                "</body></html>")
        result = self.core.judge_http(html, {})
        self.assertEqual(result.status, "success")

    def test_page_hint_includes_snippet_for_long_page(self):
        filler = "页头" * 80
        hint = self.core.page_hint("<html><body>" + filler
                                   + " 验证通过后将自动完成签到</body></html>")
        self.assertIn("片段「", hint)
        self.assertIn("自动完成签到", hint)

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
        self.assertEqual(preset["engine"], "browserless")
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
        rule = self.core.fallback_rule(
            {"engine": "http", "path": "/attendance.php"}, "cloudflare")
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
        rule = self.core.fallback_rule(
            {"engine": "http", "path": "/attendance.php"}, "cloudflare")
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


class BrowserlessAudiencesTests(unittest.TestCase):
    """观众（Audiences）浏览器引擎：原生点击 Turnstile checkbox，再按真实页面状态判定。"""

    def setUp(self):
        self.core = CORES[0]
        self.site = AUDIENCES_SITE

    def _rule(self):
        return self.core.resolve_rule(self.site, {})

    def _signer(self, responses):
        return make_stub_signer(self.core, responses)

    def test_request_timeout_covers_internal_wait_budget(self):
        # BQL 是「整段执行完才返回响应」的整包请求：读超时必须 ≥ 页面内等待预算，
        # 否则插件会自己先掐断连接，报出 The read operation timed out 这种自伤式失败。
        rule = self._rule()
        budget = self.core.bql_wait_budget_ms(rule)
        # cloudflare_wait 15s + solve 40s + solve2_wait 5s + solve2_timeout 50s
        # + wait 8s + verify_wait 8s
        self.assertEqual(budget, 126000)
        signer = self.core.BrowserlessSigner("https://example.test/bql", "token", 100)
        self.assertGreater(signer.request_timeout(rule), 100)
        self.assertGreaterEqual(signer.request_timeout(rule) * 1000, budget + 45000)

    def test_transient_failure_rechecks_and_reports_success(self):
        # 主流程响应丢失（The read operation timed out）但站点其实已经签到：
        # 插件应补一次只读回查，把结果改判成成功，而不是报网络失败。
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            self.core.SignResult(
                "failed",
                "Browserless 请求超时（签到主流程 111 秒内未收到响应，已等待 111 秒）",
                transient=True),
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("重新打开签到页确认签到成功", result.message)
        self.assertEqual([call[0] for call in signer.calls],
                         ["CheckInPreflight", "CheckIn", "CheckInPreflight"])

    def test_transient_failure_without_recheck_still_fails(self):
        # 回查也没看到签到状态时仍如实报失败，并提示换节点 / 重试
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            self.core.SignResult("failed", "Browserless 请求超时（签到主流程）",
                                 transient=True),
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("Browserless 请求超时", result.message)
        self.assertIn("未能确认为已签到", result.message)

    def test_query_solves_turnstile_and_auto_submits(self):
        # 观众预设：两段官方 solve（外层交互挑战 + 签到页内嵌 Turnstile）生成 token，
        # 再由 auto 脚本触发站点回调 / 表单兜底 POST；不再使用命不中的 checkbox 选择器。
        rule = self._rule()
        query = self.core.build_query(rule)
        self.assertIn("solve:solve(timeout:$solveTimeout)", query)
        self.assertIn("solve2:solve(timeout:$solve2Timeout)", query)
        self.assertIn("$solveTimeout", query)
        self.assertIn("$solve2Timeout", query)
        self.assertIn("$solve2Wait", query)
        self.assertIn("$autoSubmit", query)
        self.assertIn("submit:evaluate(content:$autoSubmit)", query)
        self.assertNotIn("waitForTurnstile", query)
        self.assertNotIn("$turnstileWait", query)
        self.assertNotIn("$submit:", query)
        self.assertIn("verify:goto", query)
        self.assertIn("waitForVerify", query)
        self.assertIn("$verifyWait", query)

    def test_solves_turnstile_then_reports_success(self):
        signer = self._signer([
            # 预检：页面还是「未签到 + 等人机验证」
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            # 主流程：solve 生成 token，auto 提交后站点跳回首页
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True},
             "solve2": {"found": True, "solved": True},
             "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_HOME_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("22", result.message)
        self.assertEqual(signer.calls[1][1]["beforeWait"], 15000)
        self.assertEqual(signer.calls[1][1]["solveTimeout"], 40000)
        self.assertEqual(signer.calls[1][1]["solve2Timeout"], 50000)
        self.assertEqual(signer.calls[1][1]["solve2Wait"], 5000)
        self.assertEqual(signer.calls[1][1]["wait"], 8000)
        self.assertEqual(signer.calls[1][1]["verifyWait"], 8000)
        self.assertEqual(signer.calls[1][1]["verifyUrl"],
                         "https://audiences.me/attendance.php")
        self.assertNotIn("selector", signer.calls[1][1])
        self.assertNotIn("turnstileWait", signer.calls[1][1])
        self.assertIn("cf-turnstile-response", signer.calls[1][1]["autoSubmit"])
        self.assertEqual(signer.calls[0][0], "CheckInPreflight")
        self.assertEqual(signer.calls[1][0], "CheckIn")

    def test_already_signed_before_run_short_circuits(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "already")
        self.assertEqual(len(signer.calls), 1)

    def test_still_pending_reports_actionable_failure(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_PENDING_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("人机验证", result.message)
        self.assertIn("自动完成签到", result.message)

    def test_pending_main_but_signed_verify_is_success(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("已重新打开签到页确认签到成功", result.message)

    def test_homepage_and_pending_verify_is_failure(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_HOME_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_PENDING_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("二次核验", result.message)
        self.assertIn("人机验证", result.message)

    def test_homepage_and_homepage_verify_is_failure(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_HOME_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_HOME_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("未显示签到成功状态", result.message)

    def test_solved_but_page_says_signed_is_success(self):
        # solve 报 found 但没解成功，页面其实已经签上了：不能误报失败
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": False},
             "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_SIGNED_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")

    def test_auto_submit_script_and_failure_diagnostics(self):
        # 自定义为 auto 时仍保留 solve -> callback / 表单 POST 的兼容路径
        rule = dict(self._rule(), submit_method="auto", solve_count=2,
                    solve_timeout=20000, solve2_wait=3000)
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True},
             "solve2": {"found": True, "solved": False},
             "submit": {}, "response": {},
             "autoDiag": {"value": json.dumps(
                 {"token": False, "cb": None, "form": False, "act": None, "post": None},
                 ensure_ascii=False)},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_PENDING_HTML}},
        ])
        result = signer.sign(self.site, rule)
        self.assertEqual(result.status, "failed")
        self.assertIn("submit:evaluate(content:$autoSubmit)", signer.queries[1])
        self.assertIn("solve2:solve", signer.queries[1])
        self.assertNotIn("$submit:", signer.queries[1])
        script = signer.calls[1][1]["autoSubmit"]
        self.assertIn("cf-turnstile-response", script)
        self.assertIn("data-callback", script)
        self.assertIn("method: 'POST'", script)
        self.assertIn("credentials: 'same-origin'", script)
        self.assertIn("getAttribute('action')", script)
        self.assertIn("sessionStorage", script)
        self.assertIn("callback-scheduled", script)
        self.assertIn("return JSON.stringify(diag)", script)
        self.assertIn("__ptsignin_auto", script)
        self.assertIn("未拿到 Turnstile token", result.message)
        self.assertIn("页面没有 data-callback 回调", result.message)
        self.assertIn("解题诊断", result.message)
        self.assertIn("二次 solve", result.message)

    def test_auto_submit_script_waits_token_with_rule_budget(self):
        script = self.core._turnstile_auto_script(dict(self._rule(), auto_wait=15000))
        self.assertIn("Date.now() + 15000", script)
        self.assertEqual(self.core.auto_wait_ms({"auto_wait": 1}), 3000)
        self.assertEqual(self.core.auto_wait_ms({}), 20000)
        rule = dict(self._rule(), solve_count=2, solve_timeout=20000, solve2_wait=3000)
        self.assertEqual(self.core.solve_timeout_ms(rule), 20000)
        self.assertEqual(self.core.solve2_wait_ms({"solve2_wait": 1}), 1000)
        self.assertEqual(self.core.solve2_wait_ms({}), 3000)
        self.assertEqual(self.core.turnstile_wait_ms({"turnstile_wait": 1}), 5000)
        self.assertEqual(self.core.turnstile_wait_ms({}), 45000)
        self.assertTrue(self.core.secondary_solve_enabled(rule))
        self.assertTrue(self.core.solver_enabled(self._rule()))
        self.assertTrue(self.core.solver_enabled({"mode": "cloudflare"}))
        self.assertEqual(self.core.solve2_timeout_ms({"solve2_timeout": 45000}), 45000)
        self.assertEqual(self.core.solve2_timeout_ms({"solve_timeout": 20000}), 20000)
        self.assertEqual(self.core.solve2_timeout_ms({}), 60000)
        self.assertEqual(self.core.solve2_timeout_ms({"solve2_timeout": 1}), 5000)
        self.assertEqual(self.core.solve2_timeout_ms({"solve2_timeout": 999999}), 120000)

    def test_diagnostics_report_callback_and_post(self):
        hint = self.core.auto_submit_hint(json.dumps(
            {"token": True, "cb": "onTurnstileOk", "form": True, "act": "callback", "post": 200},
            ensure_ascii=False))
        self.assertIn("已拿到 Turnstile token", hint)
        self.assertIn("onTurnstileOk", hint)
        self.assertIn("已提交页面表单", hint)
        self.assertIn("HTTP 200", hint)

    def test_diagnostics_distinguish_callback_states(self):
        scheduled = self.core.auto_submit_hint(json.dumps(
            {"token": True, "cb": "onTurnstileOk", "act": "callback-scheduled"},
            ensure_ascii=False))
        self.assertIn("已调度站点回调", scheduled)
        self.assertIn("执行结果未确认", scheduled)
        self.assertNotIn("已调用站点回调", scheduled)

        failed = self.core.auto_submit_hint(json.dumps(
            {"token": True, "cb": "onTurnstileOk", "act": "callback-error+post"},
            ensure_ascii=False))
        self.assertIn("执行失败", failed)
        missing = self.core.auto_submit_hint(json.dumps(
            {"token": False, "cb": "onTurnstileOk", "act": "callback-missing"},
            ensure_ascii=False))
        self.assertIn("不存在", missing)

    def test_login_page_preflight_reports_cookie_problem(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": LOGIN_PAGE_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("Cookie", result.message)
        self.assertIn("重新登录", result.message)
        self.assertEqual(len(signer.calls), 1)

    def test_verify_login_page_reports_cookie_problem(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": LOGIN_PAGE_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("重新登录", result.message)
        self.assertIn("Cookie", result.message)

    def test_solve_step_error_still_reports_signed_success(self):
        # 官方文档：单个步骤失败（如 solve 超时）不会中止整个 mutation。页面回查已显示
        # 签到成功时，不能因为 errors 里有一条 solve 报错就整轮判失败。
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": None, "solve2": None,
             "_errors": [{"path": "solve",
                          "message": "Captcha solving timed out after 40000 ms"}],
             "autoDiag": {"value": "null"}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_SIGNED_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")

    def test_solve_step_error_is_reported_in_failure_message(self):
        # 真没签上时，BrowserQL 步骤错误要出现在失败原因里，便于定位是 solve 超时还是
        # 页面结构变化，而不是只给一句「未匹配到签到成功标志」。
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": None, "solve2": None,
             "_errors": [{"path": "solve",
                          "message": "Captcha solving timed out after 40000 ms"}],
             "autoDiag": {"value": "null"}, "submit": {}, "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "verify": {"status": 200},
             "verifyPage": {"html": AUDIENCES_PENDING_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("Browserless 步骤", result.message)
        self.assertIn("solve", result.message)
        self.assertIn("Captcha solving timed out", result.message)


class BrowserlessRequestTests(unittest.TestCase):
    """_request 的响应解析：单步失败不能当整轮失败。"""

    def setUp(self):
        self.core = CORES[0]
        self.signer = self.core.BrowserlessSigner("https://example.test/bql", "token", 100)

    @staticmethod
    def _response(payload):
        class _FakeResponse:
            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _FakeResponse(payload)

    def test_step_errors_do_not_discard_data(self):
        from unittest import mock

        payload = {"data": {"goto": {"status": 200}, "solve": None,
                            "after": {"html": AUDIENCES_SIGNED_HTML}},
                   "errors": [{"message": "Captcha solving timed out after 40000 ms",
                               "path": ["solve"]}]}
        with mock.patch.object(self.core.urllib.request, "urlopen",
                               return_value=self._response(payload)):
            data = self.signer._request({}, "CheckIn", "query")
        self.assertIsInstance(data, dict)
        self.assertEqual(data["_errors"],
                         [{"path": "solve",
                           "message": "Captcha solving timed out after 40000 ms"}])
        self.assertIn("html", data["after"])

    def test_errors_without_any_data_still_fail(self):
        from unittest import mock

        payload = {"data": None,
                   "errors": [{"message": "Field 'solve' is not defined",
                               "path": ["solve"]}]}
        with mock.patch.object(self.core.urllib.request, "urlopen",
                               return_value=self._response(payload)):
            result = self.signer._request({}, "CheckIn", "query")
        self.assertIsInstance(result, self.core.SignResult)
        self.assertEqual(result.status, "failed")
        self.assertIn("Field", result.message)


if __name__ == "__main__":
    unittest.main()
