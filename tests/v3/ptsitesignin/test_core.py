"""core.py 回归测试：不依赖 MoviePilot 运行时。"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
# 本仓库 V2/V3 两份都有；提交官方仓库时只有 plugins.v3，所以按实际存在的目录取
PLUGIN_DIRS = tuple(directory for directory in (
    ROOT / "plugins.v2" / "ptsitesignin",
    ROOT / "plugins.v3" / "ptsitesignin",
) if directory.is_dir())


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
            self.token = "token"
            self.timeout = 100
            self.responses = list(responses)
            self.calls = []
            self.queries = []
            self.endpoints = []

        def _request(self, variables, operation, query, **kwargs):
            self.calls.append((operation, dict(variables)))
            self.queries.append(query)
            self.endpoints.append(kwargs.get("endpoint"))
            return self.responses.pop(0)

    return _StubSigner()


def inject_payload(after_html, verify_html=None, diag=None, goto=200):
    """第二段（CheckInSubmit）的预置响应：注入脚本现场诊断 + 提交后的页面。

    第二段一定会重新 goto 打开签到页（复用会话拿到的也是新页面），所以这里默认带上导航状态。
    """
    state = {"token": True, "src": "solve", "len": 1180, "cb": None, "cands": [],
             "called": None, "form": False, "act": "callback-absent", "post": None,
             "still": True, "turnstile": True, "inputs": 0, "widget": True, "frames": 2,
             "text": "每日签到 完成人机验证即可领取今日爆米花奖励"}
    if diag:
        state.update(diag)
    body = json.dumps(state, ensure_ascii=False)
    payload = {
        "submit": {"value": body},
        "autoDiag": {"value": body},
        "response": {"value": None},
        "after": {"html": after_html},
    }
    if goto is not None:
        payload["goto"] = {"status": goto}
    if verify_html is not None:
        payload["verify"] = {"status": 200}
        payload["verifyPage"] = {"html": verify_html}
    return payload


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

    def test_audiences_preset_waits_for_page_auto_submit(self):
        # 打开 /attendance.php 后一次 solve 处理进站 Cloudflare 挑战；solve 只生成 token，
        # 不会触发站点回调，所以 submit_method=auto 补一次显式提交把签到真正发出去。
        self.assertEqual(self.audiences["mode"], "cloudflare")
        self.assertEqual(self.audiences["submit_method"], "auto")
        self.assertEqual(self.audiences["auto_wait"], 30000)
        self.assertEqual(self.audiences["solve_count"], 1)
        self.assertEqual(self.audiences["solve_timeout"], 40000)
        self.assertEqual(self.audiences["cloudflare_wait"], 15)
        self.assertEqual(self.audiences["wait"], 15000)
        self.assertNotIn("solve2_timeout", self.audiences)
        self.assertNotIn("solve2_wait", self.audiences)
        self.assertEqual(self.audiences["verify_success_path"], "/attendance.php")
        self.assertEqual(self.audiences["verify_wait"], 15000)
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

    def test_activity_text_unescapes_html_entities(self):
        # 站点源码里常把 < 写成 &lt;：「2026-10-17 03:52:21 ( &lt; 1分前 )」
        html = ("<html><body><div>最近动向：2026-10-17 03:52:21 "
                "(&lt; 1分前)</div></body></html>")
        found = self.core.extract_activity(html)
        self.assertIn("2026-10-17 03:52:21", found["last_seen"])
        self.assertNotIn("&lt;", found["last_seen"])
        self.assertEqual(self.core.plain_text("&lt;1分前&gt; &amp; &quot;签到成功&quot;"),
                         '<1分前> & "签到成功"')
        self.assertEqual(self.core.page_text("<div>a &lt; b &amp; c</div>"), "a < b & c")

    def test_btschool_preset_two_step(self):
        # 学校：和 thsrite 的站点模块一致——先看首页有没有「每日签到」入口，
        # 没有就是已签到；有就访问 addbonus，入口消失即签到成功
        rule = self.core.resolve_rule({"url": "https://pt.btschool.club/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "http")
        self.assertEqual(rule["method"], "GET")
        self.assertEqual(rule["path"], "/index.php")
        self.assertEqual(rule["absent_keywords"], ["每日签到"])
        self.assertEqual(rule["absent_status"], "already")
        self.assertEqual(rule["signin_path"], "/index.php?action=addbonus")
        self.assertEqual(rule["signin_absent_status"], "success")
        # 第二段页面上入口消失 → 判成功（用 signin_absent_status 覆盖 absent_status）
        probe = dict(rule, absent_status=rule["signin_absent_status"])
        self.assertEqual(
            self.core.judge_http("<html><body>首页 论坛 魔力值</body></html>", probe).status,
            "success")

    def test_summarize_records_separates_uncertain_and_missing(self):
        rows = [
            {"site": "A", "status": "success", "message": "签到成功"},
            {"site": "B", "status": "already", "message": "今日已签到"},
            {"site": "C", "status": "failed", "message": "Cookie 无效或已过期"},
            {"site": "D", "status": "failed",
             "message": "签到后二次核验未显示签到成功状态；诊断：…"},
            {"site": "E", "status": "login", "message": "成功（已换用新 Cookie）"},
        ]
        summary = self.core.summarize_records(rows, ["A", "B", "C", "D", "E", "F"])
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["success"], 3)     # success / already / login
        self.assertEqual(summary["failed"], 1)      # Cookie 失效这类明确失败
        self.assertEqual(summary["uncertain"], 1)   # 二次核验没确认 → 需要人工核对
        self.assertEqual(summary["missing"], 1)     # 选了 F 但没有记录

    def test_record_meta_marks_uncertain_failure(self):
        self.assertEqual(self.core.record_meta({"status": "failed", "transient": True})["label"],
                         "未确认")
        self.assertEqual(self.core.record_meta(
            {"status": "failed", "message": "Browserless 请求超时（响应丢失）"})["label"], "未确认")
        self.assertEqual(self.core.record_meta(
            {"status": "failed", "message": "Cookie 无效或已过期"})["label"], "签到失败")
        self.assertEqual(self.core.record_meta({"status": "already"})["label"], "今日已签到")
        self.assertEqual(self.core.record_meta({"status": "login"})["label"], "登录成功")

    def test_dual_record_enters_both_sections(self):
        dual = {"site": "观众", "status": "success", "dual": True}
        self.assertTrue(self.core.is_login_record(dual))
        self.assertFalse(self.core.is_login_record({"site": "B", "status": "success"}))
        self.assertTrue(self.core.is_login_record({"site": "C", "status": "login"}))
        self.assertTrue(self.core.is_login_record({"site": "D", "status": "login_failed"}))
        # 1.4.0 之前的记录没有 dual 标记：带了登录结论 / 最近动向同样算登录记录
        self.assertTrue(self.core.is_login_record(
            {"site": "E", "status": "success", "login": "成功（已换用新 Cookie）"}))
        self.assertTrue(self.core.is_login_record(
            {"site": "F", "status": "already", "activity": "2026-10-16 23:44:21"}))
        self.assertTrue(self.core.is_signin_record({"site": "观众", "status": "success", "dual": True}))
        self.assertFalse(self.core.is_signin_record({"site": "C", "status": "login"}))

    def test_group_and_count_records(self):
        rows = [{"site": "A", "status": "success"}, {"site": "B", "status": "failed"},
                {"site": "A", "status": "already"}]
        grouped = self.core.group_records_by_site(rows)
        self.assertEqual(list(grouped), ["A", "B"])          # 保持首次出现顺序
        self.assertEqual(len(grouped["A"]), 2)
        self.assertEqual(self.core.site_record_counts(rows), {"A": 2, "B": 1})

    def test_agsvpt_preset_uses_post_and_click_fallback(self):
        # AGSVPT 的签到页是个「立即签到」提交按钮：GET 不签到，必须 POST；
        # 自动回退到浏览器时要沿用这个按钮选择器与按钮文字。
        rule = self.core.resolve_rule({"url": "https://www.agsvpt.com/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "http")
        self.assertEqual(rule["method"], "POST")
        self.assertEqual(rule["path"], "/attendance.php")
        self.assertTrue(rule["auto_fallback"])
        fallback = self.core.fallback_rule(rule, "captcha")
        self.assertEqual(fallback["engine"], "browserless")
        self.assertEqual(fallback["mode"], "open_page")
        self.assertIn("立即签到", fallback["submit_text"])
        self.assertIn("button", fallback["submit_selector"])

    def test_extract_activity_ignores_bare_dates(self):
        # 种子列表 / 公告里的裸日期到处都是，不能当成「最近动向」（以前会误报 2026-09-18）
        self.assertEqual(self.core.extract_activity(
            "<html><body>种子 2026-09-18 某电影 2026-09-17 另一部</body></html>"), {})
        self.assertEqual(self.core.extract_activity("<html><body>2026-09-18</body></html>"), {})

    def test_extract_activity_prefers_relative_suffix(self):
        # 皇后的用户页：加入日期在前、最近动向在后，两个时间都在附近，必须取后面的那个
        html = ("<table><tr><td>加入日期</td><td>2025-10-01 23:57:35 (1年0月前,54周)</td></tr>"
                "<tr><td>最近动向</td><td>2026-10-18 21:19:12 (40分钟前)</td></tr>"
                "<tr><td>目前IP</td><td>5.180.78.12</td></tr></table>")
        info = self.core.extract_activity(html)
        self.assertTrue(info["last_seen"].startswith("2026-10-18 21:19:12"), info)
        # 页面里另有一处「最近动向」跟着旧日期（公告/说明），也要取带「(x分钟前)」的那个
        noisy = ("<div>最近动向说明：2026-05-06 00:00:00</div>"
                 "<div>最近动向</div><div>2026-10-18 21:19:12 (40分钟前)</div>")
        info = self.core.extract_activity(noisy)
        self.assertTrue(info["last_seen"].startswith("2026-10-18 21:19:12"), info)

    def test_extract_activity_picks_nearest_keyword_time(self):
        # 关键词后面没时间就换下一个关键词，并取距离最近的那个
        found = self.core.extract_activity(
            "<html><body>上次访问 未公开 上次签到：2026-10-16 23:44:21</body></html>")
        self.assertEqual(found["keyword"], "上次签到")
        self.assertEqual(found["last_seen"], "2026-10-16 23:44:21")
        # 新增的关键词（最后活跃 / 最近访问）也能命中
        self.assertEqual(self.core.extract_activity(
            "<html><body>最后活跃于 2026-10-17 08:00:12</body></html>")["last_seen"],
            "2026-10-17 08:00:12")

    def test_cookie_uid_decodes_nexusphp_cookie(self):
        self.assertEqual(self.core.cookie_uid("c_secure_uid=Mjk1NTI%3D; c_secure_pass=x"), "29552")
        self.assertEqual(self.core.cookie_uid("c_secure_uid=abc"), "")
        self.assertEqual(self.core.cookie_uid(""), "")

    def test_discover_signin_path_from_index_links(self):
        html = ('<html><body><a href="/index.php">首页</a>'
                '<a href="/torrents.php">种子</a>'
                '<a href="https://other.example/attendance.php">签到</a>'
                '<a href="javascript:void(0)">签到</a>'
                '<a href="/signin.php?from=nav">每 日 签 到</a></body></html>')
        # 跨域、javascript: 链接丢掉，只认同站的「签到」入口
        self.assertEqual(self.core.discover_signin_path(html, "https://pt.example/"),
                         "/signin.php?from=nav")
        self.assertEqual(self.core.discover_signin_path("<html><body>没有链接</body></html>",
                                                        "https://pt.example/"), "")
        self.assertEqual(self.core.same_site_path("https://pt.example/", "../x.php"), "/x.php")
        self.assertEqual(self.core.same_site_path("https://pt.example/", "#top"), "")

    def test_random_form_fields_picks_one_option(self):
        # 52pt 的 bakatest：隐藏 questionid + 两个 choice[]，随便挑一个
        html = ('<form><input type="hidden" name="questionid" value="450">'
                '<input type="checkbox" name="choice[]" value="8">'
                '<input type="checkbox" name="choice[]" value="4"></form>')
        fields = self.core.random_form_fields(html)
        self.assertEqual(fields["questionid"], "450")
        self.assertIn(fields["choice[]"], {"8", "4"})
        # U2 的四选一：随机挑一个提交按钮
        u2 = ('<form><input type="hidden" name="req" value="r1">'
              '<input type="hidden" name="hash" value="h1">'
              '<input type="submit" name="a" value="选项一">'
              '<input type="submit" name="b" value="选项二"></form>')
        picked = self.core.random_form_fields(u2)
        self.assertEqual(picked["req"], "r1")
        self.assertTrue({"a", "b"} & set(picked))

    def test_site_modules_loaded(self):
        # 插件目录 sites/ 下的模块应能自动发现（图片验证码 / 答题 / API Token 这几类站点）
        modules = self.core.load_site_modules()
        names = {getattr(module, "site_url", "") for module in modules}
        self.assertIn("hdsky.me", names)
        self.assertIn("tjupt.org", names)
        self.assertIn("m-team", names)
        self.assertIsNotNone(self.core.site_module_for("https://hdsky.me/"))
        self.assertIsNotNone(self.core.site_module_for("https://www.tjupt.org/"))
        self.assertIsNotNone(self.core.site_module_for("https://kp.m-team.cc/"))
        # 没有模块的站点返回 None，交给规则引擎
        self.assertIsNone(self.core.site_module_for("https://open.cd/"))

    def test_run_site_module_dispatch(self):
        fake = type("FakeModule", (), {
            "site_url": "fake-module.example",
            "signin": lambda self, site, ctx: (True, "签到成功"),
        })
        original = self.core._SITE_MODULES
        self.core._SITE_MODULES = [fake]
        try:
            result = self.core.run_site_module({"url": "https://fake-module.example/", "cookie": "c=1"})
            self.assertIsNotNone(result)
            self.assertEqual(result.status, "success")
            self.assertEqual(result.message, "签到成功")
            # 没 Cookie 时不执行
            missing = self.core.run_site_module({"url": "https://fake-module.example/"})
            self.assertEqual(missing.status, "failed")
        finally:
            self.core._SITE_MODULES = original

    def test_hddolby_already_preset(self):
        # 高清杜比：已签到时首页写「鲸币 [ 使用 ]: 131,943.3 (签到已得64)」
        rule = self.core.resolve_rule({"url": "https://www.hddolby.com/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "http")
        self.assertEqual(rule["path"], "/attendance.php")
        signed = ('<html><body>HDDolby<br>鲸币 [ 使用 ]: 131,943.3 (签到已得64)'
                  '<br>分享率： 3.080 上传量： 101.83 GB</body></html>')
        self.assertEqual(self.core.classify(signed, rule).status, "already")
        # 没签过的页面不能误判
        pending = "<html><body>HDDolby 签到 鲸币 [ 使用 ]: 131,943.3</body></html>"
        self.assertNotEqual(self.core.classify(pending, rule).status, "already")

    def test_find_signin_form(self):
        # 签到页里是「点按钮」触发的表单：要能取出 action / method / 字段
        html = ('<html><body><div>每日签到</div>'
                '<form action="/attendance.php" method="post">'
                '<input type="hidden" name="site" value="1">'
                '<input type="submit" name="signin" value="立即签到"></form></body></html>')
        form = self.core.find_signin_form(html, "https://pt.example/")
        self.assertEqual(form["action"], "/attendance.php")
        self.assertEqual(form["method"], "POST")
        self.assertEqual(form["data"].get("signin"), "立即签到")
        self.assertEqual(form["data"].get("site"), "1")
        self.assertIsNone(self.core.find_signin_form(
            "<html><body>今日已签到</body></html>", "https://pt.example/"))

    def test_discover_signin_target_from_home_form(self):
        # 学校（BTSCHOOL）这种：首页上挂一个「每日签到」提交按钮，没有独立签到页
        home = ('<html><body><form method="post" action="/index.php">'
                '<input type="submit" name="signin" value="每日签到"></form></body></html>')
        self.assertEqual(self.core.discover_signin_target(home, "https://pt.example/"),
                         {"path": "/index.php", "method": "POST", "data": {"signin": "每日签到"}})
        # <button> 形式：文字取标签内容，没有 name 时不带 data
        home2 = ('<html><body><form method="post">'
                 '<button type="submit">每 日 签 到</button></form></body></html>')
        self.assertEqual(self.core.discover_signin_target(home2, "https://pt.example/"),
                         {"path": "/", "method": "POST"})
        # 没有签到表单时退回找链接
        home3 = '<html><body><a href="/signin.php">签到</a></body></html>'
        self.assertEqual(self.core.discover_signin_target(home3, "https://pt.example/"),
                         {"path": "/signin.php", "method": "GET"})
        # 普通登录表单不会被误认成签到入口
        home4 = ('<html><body><form method="post" action="/takelogin.php">'
                 '<input name="username"><input type="password" name="password">'
                 '<input type="submit" value="登录"></form></body></html>')
        self.assertEqual(self.core.discover_signin_target(home4, "https://pt.example/"), {})

    def test_hhanclub_calendar_preset(self):
        # 憨憨：日历式签到，点「签到」后当天按钮变「已领取」，奖励是「本次签到获得 N 个憨豆」
        rule = self.core.resolve_rule({"url": "https://hhanclub.net/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "browserless")
        self.assertEqual(rule["path"], "/attendance.php")
        self.assertEqual(rule["submit_text"], "签到")
        page = ("<html><body><div>这是您的第80次签到，已连续签到80天，本次签到获得405个憨豆。</div>"
                "<button>已领取</button><button>签到</button></body></html>")
        result = self.core.classify(page, rule)
        self.assertEqual(result.status, "already")
        self.assertIn("获得405个憨豆", result.message)

    def test_playwright_engine_presets(self):
        # 猪猪有异地登录校验、SUNNYPT 是 Next.js：都改用本机 Playwright（IP 与家宽一致）
        for url in ("https://piggo.me/", "https://sunnypt.top/"):
            rule = self.core.resolve_rule({"url": url, "cookie": "c=1"}, {})
            self.assertEqual(rule["engine"], "playwright", url)
        piggo = self.core.resolve_rule({"url": "https://piggo.me/", "cookie": "c=1"}, {})
        self.assertEqual(piggo["path"], "/index.php")
        self.assertEqual(piggo["submit_text"], "签到")

    def test_playwright_sign_without_helper(self):
        # 本机没有 Playwright 时返回 None，交给上层换引擎（不报错、不误判）
        self.assertIsNone(self.core.playwright_sign(
            {"url": "https://piggo.me/", "cookie": "c=1"},
            {"engine": "playwright", "path": "/index.php"}))

    def test_piggo_preset(self):
        # 猪猪有异地登录校验：改用本机 Playwright（家宽 IP），勾选级 CF 由本机浏览器点过
        rule = self.core.resolve_rule({"url": "https://piggo.me/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "playwright")
        self.assertEqual(rule["path"], "/index.php")
        self.assertEqual(rule["submit_text"], "签到")
        self.assertEqual(rule["submit_method"], "dom_click")
        self.assertEqual(rule["wait"], 15000)

    def test_waf_page_is_treated_as_challenge(self):
        # 雷池（SafeLine）WAF 安全检测页：按挑战处理，交给浏览器重试
        waf = ("<html><head><title>安全检测</title></head><body>正在进行安全检测"
               "<div>安全检测能力由 雷池 WAF 驱动</div></body></html>")
        result = self.core.judge_http(waf, {})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.challenge, "cloudflare")
        self.assertIn("WAF", result.message)
        self.assertTrue(self.core.cloudflare_pending(waf))

    def test_ttg_and_hdkyl_presets(self):
        # TTG：首页有绿色「已签到」就是签过了，否则取 token POST /signed.php（HTTP 就能签）
        ttg = self.core.resolve_rule({"url": "https://totheglory.im/", "cookie": "c=1"}, {})
        self.assertEqual(ttg["engine"], "http")
        self.assertEqual(ttg["path"], "/index.php")
        self.assertEqual(ttg["signin_path"], "/signed.php")
        self.assertEqual(ttg["signin_method"], "POST")
        self.assertTrue(ttg["signin_token_patterns"]["signed_timestamp"])
        self.assertTrue(ttg["signin_token_patterns"]["signed_token"])
        # 首页那段「已签到」标记 → already；普通首页 → 不误判
        signed = ('<html><body><b style="color:green;">已签到</b>'
                  '<script>signed_timestamp: "1700000000"</script></body></html>')
        self.assertEqual(self.core.judge_http(signed, ttg).status, "already")
        pending = ('<html><body>欢迎<script>signed_timestamp: "1700000000", '
                   'signed_token: "abc"</script></body></html>')
        self.assertEqual(self.core.judge_http(pending, ttg).status, "failed")
        for url in ("https://www.hdkyl.in/", "https://hdkyl.in/"):
            hdk = self.core.resolve_rule({"url": url, "cookie": "c=1"}, {})
            self.assertEqual(hdk["engine"], "browserless")
            # 签到入口在首页，点完跳雷池 WAF 检测页，等它放行后再读结果
            self.assertEqual(hdk["path"], "/index.php")
            self.assertEqual(hdk["submit_text"], "签到")
            self.assertEqual(hdk["wait"], 12000)

    def test_two_step_click_for_menu_items(self):
        # 菜单项要先点头像才渲染出来：pre_click 生成前置点击脚本，submit 里会带上
        self.assertEqual(self.core.pre_click_script('', '', 800), '')
        pre = self.core.pre_click_script('[class*=avatar]', '', 800)
        self.assertIn('avatar', pre)
        submit = self.core._submit_script('a', 'dom_click', submit_text='立即签到', pre_click=pre)
        self.assertIn('avatar', submit)
        self.assertIn('立即签到', submit)

    def test_sunnypt_attendance_page_preset(self):
        # SUNNYPT：站点自带签到页 /user/attendance，不用再点头像菜单
        rule = self.core.resolve_rule({"url": "https://sunnypt.top/", "cookie": "c=1"}, {})
        self.assertEqual(rule["engine"], "playwright")
        self.assertEqual(rule["path"], "/user/attendance")
        self.assertEqual(rule["submit_text"], "立即签到")
        self.assertEqual(rule["submit_method"], "dom_click")
        # 已签到的页面（今日签到已完成 / 今日已签到）：成功或已签到都表示今天签过了，都算通过
        signed = ("<html><body>签到日历<div>今日签到已完成</div>"
                  "<div>本次获得 12 魔力，已连续签到 3 天</div>"
                  "<button disabled>今日已签到</button></body></html>")
        self.assertIn(self.core.classify(signed, rule).status, {"success", "already"})
        # 点完页面出现「今日签到已完成 / 本次获得 12 魔力」→ 本次签到成功
        clicked = self.core.classify(
            "<html><body>今日签到已完成，本次获得 12 魔力</body></html>", rule)
        self.assertEqual(clicked.status, "success")
        self.assertIn("12 魔力", self.core.extract_bonus("本次获得 12 魔力", rule))
        # 点到但页面没变化（比如点的是灰色「今日已签到」按钮）→ 不能算签到成功
        self.assertFalse(rule.get("success_on_click"))
        stale = self.core.classify(
            "<html><body>一键完成今日签到，也可以查看记录或补签</body></html>", rule)
        self.assertNotEqual(stale.status, "success")
        self.assertIsNone(self.core.judge_after_click(rule, True, "<html><body>无变化</body></html>"))

    def test_hhanclub_success_on_click(self):
        # 憨憨点完没有任何提示，只有按钮变「已领取」：点到就算成功，没点到交给文案判定
        rule = self.core.resolve_rule({"url": "https://hhanclub.net/", "cookie": "c=1"}, {})
        self.assertTrue(rule.get("success_on_click"))
        page = "<html><body><div>本次签到获得410个憨豆</div></body></html>"
        clicked = self.core.judge_after_click(rule, True, page)
        self.assertEqual(clicked.status, "success")
        self.assertIn("获得410个憨豆", clicked.message)
        self.assertIsNone(self.core.judge_after_click(rule, False, page))
        self.assertIsNone(self.core.judge_after_click({}, True, page))

    def test_btschool_silent_success_is_already(self):
        # 学校：签到成功后站点不给任何提示，只是把「每日签到」入口拿掉
        rule = self.core.resolve_rule({"url": "https://pt.btschool.club/", "cookie": "c=1"}, {})
        signed = ('<html><head><title>BTSCHOOL 比特校园PT小乐园 - Powered by NexusPHP</title></head>'
                  '<body>首页 论坛 种子 控制面板 排行榜 魔力值 [使用]: 242,141.0'
                  "<script>alert('今天已签到，您获得87点魔力值')</script></body></html>")
        result = self.core.judge_http(signed, rule)
        self.assertEqual(result.status, "already")
        self.assertIn("获得87点魔力值", result.message)
        # 入口还在 → 没签到，不能误报成功
        pending = ('<html><body>首页 论坛 <form method="post">'
                   '<input type="submit" value="每日签到"></form></body></html>')
        self.assertEqual(self.core.judge_http(pending, rule).status, "failed")
        # 登录页里当然也没有这个入口，不能当成已签到
        login = ('<html><body>用户登录 <a href="login.php">登录</a>'
                 '<form action="takelogin.php"><input name="username"></form></body></html>')
        self.assertEqual(self.core.judge_http(login, rule).status, "failed")

    def test_extract_reward_sentence_ignores_sidebar_stats(self):
        self.assertEqual(self.core.extract_reward_sentence(
            "今日已签到，您获得 24 点魔力值"), "获得 24 点魔力值")
        # 侧栏统计不是奖励
        self.assertEqual(self.core.extract_reward_sentence(
            "魔力值 [使用]: 242,141.0 上传量: 1.049 TB"), "")

    def test_cookie_uid_handles_common_variants(self):
        # id= 后面必须是自己：NexusPHP 惯例是 c_secure_uid（base64），也有站用明文 / 别的名字
        uid = "84656"
        encoded = base64.b64encode(uid.encode()).decode()
        for cookie in (f"c_secure_uid={encoded}; c_secure_pass=x",
                       "c_secure_uid=ODQ2NTY%3D; c_secure_pass=x",
                       "c_secure_uid=ODQ2NTY%253D",
                       "uid=84656",
                       "c_uid=84656",
                       "user_id=" + encoded):
            self.assertEqual(self.core.cookie_uid(cookie), uid, cookie)
        self.assertEqual(self.core.cookie_uid("c_secure_pass=x; c_secure_site=1"), "")

    def test_activity_allows_wide_label_gap(self):
        # 有的主题把「最近动向」和值隔开（图标 / 说明文字），60 字窗口会漏
        html = ("<html><body>最近动向" + "&nbsp;图标说明" * 12
                + " 2026-10-17 11:20:33 ( < 1分钟前 )</body></html>")
        self.assertIn("2026-10-17 11:20:33", self.core.extract_activity(html)["last_seen"])

    def test_activity_prefers_nearest_keyword_over_neighbour_field(self):
        # 窗口放宽后仍取「离时间最近」的关键词，不会把加入日期的时间当成本站动向
        html = ("<html><body>加入日期 2025-10-01 23:57:35 "
                "最近动向 2026-10-17 11:20:33 ( 9分钟前 )</body></html>")
        self.assertEqual(self.core.extract_activity(html)["last_seen"],
                         "2026-10-17 11:20:33 ( 9分钟前 )")

    def test_judge_http_falls_back_to_state_hint(self):
        # 页面没有标准成功文案，但出现了签到后的状态字样 → 按已签到处理，别误报失败
        result = self.core.judge_http("<html><body><div>已领取</div></body></html>", {})
        self.assertEqual(result.status, "already")
        self.assertIn("已领取", result.message)

        # 带上奖励文案时一起取出来
        result = self.core.judge_http(
            "<html><body><div>这是您的第81次签到，本次签到获得410个憨豆</div></body></html>", {})
        self.assertEqual(result.status, "already")
        self.assertIn("410个憨豆", result.message)

        # 页面上还留着「立即签到」按钮，说明这次没签上，不能按已签到处理
        result = self.core.judge_http(
            "<html><body><div>已领取</div><button>立即签到</button></body></html>", {})
        self.assertEqual(result.status, "failed")

        # 规则说明里的字样不算
        result = self.core.judge_http(
            "<html><body><div>签到规则：连续签到第 30 天额外奖励 1000 憨豆</div>"
            "<button>立即签到</button></body></html>", {})
        self.assertEqual(result.status, "failed")

    def test_btschool_absent_keywords_in_both_engines(self):
        # 学校：签到成功后「每日签到」入口消失，HTTP 与浏览器两种判定都要认
        rule = self.core.resolve_rule({"url": "https://pt.btschool.club/", "cookie": "c=1"}, {})
        signed = "<html><body><div>欢迎回来</div><div>控制面板 论坛 种子</div></body></html>"
        pending = ('<html><body><form method="post" action="/index.php">'
                   '<input type="submit" value="每日签到"></form></body></html>')
        self.assertEqual(self.core.judge_http(signed, rule).status, "already")
        self.assertEqual(self.core.judge_http(pending, rule).status, "failed")
        self.assertEqual(self.core.classify(signed, rule).status, "already")
        self.assertEqual(self.core.classify(pending, rule).status, "failed")

    def test_absent_keywords_ignore_script_and_template_text(self):
        # 脚本 / 隐藏模板里带「每日签到」不算入口还在，否则签完了也会被判成没签
        rule = self.core.resolve_rule({"url": "https://pt.btschool.club/", "cookie": "c=1"}, {})
        script_only = ("<html><body><div>欢迎回来</div>"
                       "<script>var tpl = \"每日签到\";</script></body></html>")
        self.assertEqual(self.core.classify(script_only, rule).status, "already")
        self.assertEqual(self.core.judge_http(script_only, rule).status, "already")
        # 真正的控件还在就不能算已签
        button = ('<html><body><a href="/index.php?action=addbonus">每日签到</a></body></html>')
        self.assertEqual(self.core.classify(button, rule).status, "failed")
        self.assertEqual(self.core.judge_http(button, rule).status, "failed")

    def test_control_text_extracts_button_labels(self):
        self.assertIn("每日签到", self.core.control_text(
            '<form><input type="submit" value="每日签到"></form>'))
        self.assertIn("立即签到", self.core.control_text('<button>立即签到</button>'))
        self.assertEqual(self.core.control_text("<script>var a='签到';</script>"), "")

    def test_judge_http_honours_keyword_lists(self):
        # 规则里写了 success_keywords / already_keywords 时，HTTP 判定也要认
        rule = {"success_keywords": ["领取成功"], "already_keywords": ["今日已領取"]}
        self.assertEqual(self.core.judge_http(
            "<html><body><div>领取成功</div></body></html>", rule).status, "success")
        self.assertEqual(self.core.judge_http(
            "<html><body><div>今日已領取</div></body></html>", rule).status, "already")

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
        # 第一段：cloudflare_wait 15s + solve 40s + wait 15s
        # 第二段：导航等待 15s + 注入脚本预算 42s（auto_wait 30s + evaluate 余量 12s）+ wait 15s
        # 再加回查等待 verify_wait 15s
        self.assertEqual(budget, 157000)
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

    def test_query_solves_and_keeps_session_for_injection(self):
        # 观众预设：第一段只「打开签到页 + solve 拿 token + reconnect 保留会话」，
        # 提交与核验都在第二段（token 回填页面之后）做。
        rule = self._rule()
        query = self.core.build_query(rule)
        self.assertIn("solve:solve(timeout:$solveTimeout){found solved time token}", query)
        self.assertNotIn("solve2:solve", query)
        self.assertIn("$solveTimeout", query)
        self.assertNotIn("$solve2Timeout", query)
        self.assertNotIn("$solve2Wait", query)
        self.assertIn("session:reconnect(timeout:$reconnectTimeout){browserQLEndpoint}", query)
        self.assertIn("$reconnectTimeout", query)
        # 第一段没有提交动作，也不在这里核验
        self.assertNotIn("$autoSubmit", query)
        self.assertNotIn("submit:", query)
        self.assertNotIn("verify:goto", query)
        self.assertNotIn("$verifyWait", query)
        # 不复用会话时（自建 / 旧版 Browserless）不能声明用不到的变量，否则 GraphQL 整段报错
        self.assertNotIn("reconnect", self.core.build_query(rule, None, reuse_session=False))
        # 只有 auto 提交才索取 token 字段：旧版 / 自建 Browserless 的 schema 里可能没有
        # CaptchaResponse.token，写了就会整段查询被拒，非 auto 站点保持原样
        plain = {"engine": "browserless", "mode": "cloudflare", "path": "/attendance.php"}
        plain_query = self.core.build_query(plain)
        self.assertIn("solve:solve(timeout:$solveTimeout){found solved time}", plain_query)
        self.assertNotIn("token", plain_query)

    def test_inject_query_shape(self):
        rule = self._rule()
        query = self.core.build_inject_query(rule)
        self.assertIn("mutation CheckInSubmit(", query)
        self.assertIn("submit:evaluate(content:$autoSubmit,timeout:$autoTimeout)", query)
        self.assertIn("verify:goto(url:$verifyUrl,waitUntil:domContentLoaded)", query)
        # 复用会话拿到的也是新页面：第二段必须自己导航打开签到页
        self.assertIn("goto(url:$url,waitUntil:domContentLoaded){status}", query)
        self.assertIn("waitBefore:waitForTimeout(time:$beforeWait)", query)
        self.assertIn("$url:String!", query)
        self.assertIn("$beforeWait:Float!", query)

    def test_solve_token_is_injected_in_same_session(self):
        signer = self._signer([
            # 预检：页面还是「未签到 + 等人机验证」
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            # 第一段：solve 拿 token，并申请复用同一个浏览器会话
            {"goto": {"status": 200},
             "solve": {"found": True, "solved": True, "token": "TURNSTILE-TOKEN"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint":
                         "https://production-sfo.browserless.io/e/abc/chromium/bql/xyz"}},
            # 第二段：注入 token 后站点签上了，回查看到成功页
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_SIGNED_HTML),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("22", result.message)
        # 第一段：只 solve 拿 token + 留住会话
        self.assertEqual(signer.calls[1][1]["beforeWait"], 15000)
        self.assertEqual(signer.calls[1][1]["solveTimeout"], 40000)
        self.assertEqual(signer.calls[1][1]["reconnectTimeout"], 60000)
        self.assertNotIn("solve2Timeout", signer.calls[1][1])
        self.assertNotIn("solve2Wait", signer.calls[1][1])
        self.assertEqual(signer.calls[1][1]["wait"], 15000)
        self.assertNotIn("selector", signer.calls[1][1])
        self.assertNotIn("turnstileWait", signer.calls[1][1])
        self.assertNotIn("autoSubmit", signer.calls[1][1])
        self.assertIsNone(signer.endpoints[1])
        # 第二段：打到 reconnect 返回的会话地址，token 由插件带回页面注入
        self.assertEqual(signer.calls[2][0], "CheckInSubmit")
        self.assertEqual(signer.endpoints[2],
                         "https://production-sfo.browserless.io/e/abc/chromium/bql/xyz?token=token")
        self.assertIn('"TURNSTILE-TOKEN"', signer.calls[2][1]["autoSubmit"])
        self.assertEqual(signer.calls[2][1]["autoTimeout"], 42000)
        # 复用会话只保住浏览器与 cookie：第二段仍然自己导航打开签到页
        self.assertIn("goto(url:$url", signer.queries[2])
        self.assertEqual(signer.calls[2][1]["beforeWait"], 15000)
        self.assertEqual(signer.calls[2][1]["verifyWait"], 15000)
        self.assertEqual(signer.calls[2][1]["verifyUrl"],
                         "https://audiences.me/attendance.php")
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
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_PENDING_HTML,
                           diag={"post": 200, "form": True,
                                 "act": "callback-absent+fallback"}),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("人机验证", result.message)
        self.assertIn("自动完成签到", result.message)
        # 现场诊断必须说清 token 来源、回调、兜底 POST 与页面残留状态
        self.assertIn("已拿到 Turnstile token", result.message)
        self.assertIn("来自 solve 返回的 token", result.message)
        self.assertIn("没有 data-callback", result.message)
        self.assertIn("兜底 POST 返回 HTTP 200", result.message)
        self.assertIn("页面仍停在人机验证", result.message)

    def test_pending_after_injection_but_signed_verify_is_success(self):
        # 兜底 POST 不会刷新页面（after 仍是「等人机验证」），但站点已经签上了：
        # 回查 attendance.php 看到签到状态就按成功上报。
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_SIGNED_HTML),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("已重新打开签到页确认签到成功", result.message)

    def test_homepage_and_pending_verify_is_failure(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_HOME_HTML, verify_html=AUDIENCES_PENDING_HTML),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("二次核验", result.message)
        self.assertIn("人机验证", result.message)

    def test_homepage_and_homepage_verify_is_failure(self):
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_HOME_HTML, verify_html=AUDIENCES_HOME_HTML),
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

    def test_auto_inject_script_and_failure_diagnostics(self):
        # solve 只生成 token、不触发站点提交：token 必须由插件带回第二段注入页面再提交
        rule = self._rule()
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "TOKEN-X"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_PENDING_HTML,
                           diag={"post": 200, "form": True,
                                 "act": "callback-absent+fallback"}),
        ])
        result = signer.sign(self.site, rule)
        self.assertEqual(result.status, "failed")
        # 第一段只有 solve + 会话复用
        self.assertIn("solve:solve(timeout:$solveTimeout){found solved time token}",
                      signer.queries[1])
        self.assertIn("session:reconnect", signer.queries[1])
        self.assertNotIn("$autoSubmit", signer.queries[1])
        # 第二段：注入 + 提交 + 回查，evaluate 必须显式带 timeout
        self.assertIn("mutation CheckInSubmit", signer.queries[2])
        self.assertIn("submit:evaluate(content:$autoSubmit,timeout:$autoTimeout)",
                      signer.queries[2])
        self.assertIn("verify:goto", signer.queries[2])
        script = signer.calls[2][1]["autoSubmit"]
        self.assertIn('"TOKEN-X"', script)
        self.assertIn("cf-turnstile-response", script)
        self.assertIn("data-callback", script)
        self.assertIn("method: 'POST'", script)
        self.assertIn("credentials: 'same-origin'", script)
        self.assertIn("getAttribute('action')", script)
        self.assertIn("sessionStorage", script)
        self.assertIn("return JSON.stringify(diag)", script)
        self.assertIn("__ptsignin_auto", script)
        self.assertNotIn("%", script)   # %-格式化后不应残留占位符
        self.assertIn("解题诊断", result.message)
        self.assertIn("二次核验", result.message)

    def test_script_budget_and_step_timeout_are_ordered(self):
        script = self.core._turnstile_inject_script(dict(self._rule(), auto_wait=15000), "T")
        self.assertIn("const waitBudget = 15000;", script)
        self.assertIn("Date.now() < deadline", script)
        # 步骤超时必须严格大于脚本自身预算：两者相等时脚本会被 evaluate 默认的 30 秒超时
        # 掐死在等待循环里，窗口里只剩启动瞬间的初始快照（历史那句假诊断就是这么来的）。
        self.assertEqual(self.core.auto_wait_ms({"auto_wait": 30000}), 30000)
        self.assertGreater(self.core.submit_timeout_ms({"auto_wait": 30000}), 30000)
        self.assertEqual(self.core.submit_timeout_ms({"auto_wait": 30000}), 42000)
        self.assertEqual(self.core.submit_timeout_ms({}), 32000)
        self.assertEqual(self.core.reconnect_wait_ms({}), 60000)
        self.assertEqual(self.core.reconnect_wait_ms({"reconnect_wait": 1000}), 10000)
        self.assertEqual(self.core.reconnect_wait_ms({"reconnect_wait": 999999}), 300000)
        self.assertEqual(self.core.navigation_wait_ms({"mode": "cloudflare"}), 10000)
        self.assertEqual(self.core.navigation_wait_ms({"mode": "open_page"}), 2000)
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
        solve_sourced = self.core.auto_submit_hint(json.dumps(
            {"token": True, "src": "solve", "len": 1180, "cb": None, "cands": ["cfCallback"],
             "act": "callback-absent"}, ensure_ascii=False))
        self.assertIn("来自 solve 返回的 token", solve_sourced)
        self.assertIn("长度 1180", solve_sourced)
        self.assertIn("页面没有 data-callback", solve_sourced)

        called = self.core.auto_submit_hint(json.dumps(
            {"token": True, "cb": "cfCallback", "called": "cfCallback", "act": "callback"},
            ensure_ascii=False))
        self.assertIn("已调用站点回调 cfCallback", called)

        stuck = self.core.auto_submit_hint(json.dumps(
            {"token": True, "cb": None, "act": "no-token", "still": True, "inputs": 0,
             "widget": True, "turnstile": False, "text": "验证通过后将自动完成签到"},
            ensure_ascii=False))
        self.assertIn("页面仍停在人机验证", stuck)
        self.assertIn("closed shadow DOM", stuck)
        self.assertIn("window.turnstile 不可用", stuck)

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
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=LOGIN_PAGE_HTML),
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
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML},
             "session": {"browserQLEndpoint": "https://example.test/e/1/bql/2"}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_PENDING_HTML,
                           diag={"token": False, "src": None, "len": 0, "still": True,
                                 "act": "no-token"}),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "failed")
        self.assertIn("Browserless 步骤", result.message)
        self.assertIn("solve", result.message)
        self.assertIn("Captcha solving timed out", result.message)
        # 第一段没拿到 token 时第二段照样跑：注入的是空 token，诊断如实说「未拿到」
        self.assertIn('const solveToken = "";', signer.calls[2][1]["autoSubmit"])
        self.assertIn("未拿到 Turnstile token", result.message)

    def test_first_phase_already_signed_skips_injection(self):
        # 第一段页面就已经是成功状态（例如今天早先已签）：不再注入一次，避免重复提交
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_SIGNED_HTML}},
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertEqual(len(signer.calls), 2)

    def test_reconnect_unsupported_retries_without_session_reuse(self):
        # 自建 / 旧版 Browserless 没有 reconnect 时整段查询会被拒：去掉会话复用再试一次
        signer = self._signer([
            {"goto": {"status": 200}, "before": {"html": AUDIENCES_PENDING_HTML}},
            self.core.SignResult(
                "failed",
                'Browserless 执行失败：Cannot query field "reconnect" on type "Mutation"'),
            {"goto": {"status": 200}, "solve": {"found": True, "solved": True, "token": "T"},
             "response": {},
             "before": {"html": AUDIENCES_PENDING_HTML},
             "after": {"html": AUDIENCES_PENDING_HTML}},
            inject_payload(AUDIENCES_PENDING_HTML, verify_html=AUDIENCES_SIGNED_HTML, goto=200),
        ])
        result = signer.sign(self.site, self._rule())
        self.assertEqual(result.status, "success")
        self.assertIn("session:reconnect", signer.queries[1])
        self.assertNotIn("reconnect", signer.queries[2])
        self.assertNotIn("reconnectTimeout", signer.calls[2][1])
        # 没有会话可复用：第二段自己导航打开签到页
        self.assertIn("goto(url:$url", signer.queries[3])
        self.assertIn("beforeWait", signer.calls[3][1])
        self.assertIsNone(signer.endpoints[3])


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

    def test_request_honours_session_endpoint(self):
        # 会话复用：第二段请求必须真的打到 reconnect 返回的 browserQLEndpoint。
        # 桩 signer 用 **kwargs 吞参数，抓不到这个漏改，所以这里直接打真实的 _request。
        from unittest import mock

        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return self._response({"data": {"after": {"html": AUDIENCES_SIGNED_HTML}}})

        with mock.patch.object(self.core.urllib.request, "urlopen", side_effect=fake_urlopen):
            data = self.signer._request(
                {}, "CheckInSubmit", "query",
                endpoint="https://session.example/e/1/chromium/bql/2?token=t")
        self.assertEqual(seen["url"], "https://session.example/e/1/chromium/bql/2?token=t")
        self.assertIsInstance(data, dict)

    def test_request_defaults_to_configured_endpoint(self):
        from unittest import mock

        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return self._response({"data": {"before": {"html": AUDIENCES_PENDING_HTML}}})

        with mock.patch.object(self.core.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.signer._request({}, "CheckIn", "query")
        self.assertEqual(seen["url"], self.signer.endpoint)

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


class ZeroTimeActivityTests(unittest.TestCase):
    """整零点时间是被补零的日期字段，不能当最近动向。"""

    def test_zero_time_is_ignored(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.extract_activity(
                    "<div>最近动向: 2026-05-06 00:00:00</div>"), {})
                self.assertEqual(core.extract_activity(
                    "<div>加入日期 2026-05-06 00:00:00</div>"), {})

    def test_real_time_still_wins_over_zero_time(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                found = core.extract_activity(
                    "<div>最近动向 2026-05-06 00:00:00</div><div>2026-10-20 19:02:11</div>")
                self.assertTrue(found.get("last_seen", "").startswith("2026-10-20"))

    def test_relative_suffix_kept(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                found = core.extract_activity(
                    "<div>最近动向: 2026-10-20 20:31:12 ( 2分钟前 )</div>")
                self.assertIn("2分钟前", found.get("last_seen", ""))


if __name__ == "__main__":
    unittest.main()
