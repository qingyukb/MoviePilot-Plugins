from __future__ import annotations

import re
import threading
import unittest
from unittest import mock

import requests

from tests.v3.ptsitesignin.test_core import CORES
from tests.v3.ptsitesignin.test_plugin_page import PLUGINS


QUIZ_FORM = """
<html><body>
<div>当前魔力值：13140 点魔力值</div>
<form action="/bakatest.php" method="post">
  <input type="hidden" name="formhash" value="hash-1">
  <input type="hidden" name="questionid" value="42">
  <div>请问：NexusPHP 的签到入口是哪一个？</div>
  <label><input type="radio" name="choice[]" value="A"> A. 首页</label>
  <label><input type="radio" name="choice[]" value="B"> B. bakatest.php</label>
  <label><input type="radio" name="choice[]" value="C"> C. 下载页</label>
  <input type="submit" name="submit" value="提交">
</form>
</body></html>
"""

QUIZ_SIGNED = """
<html><body><div>今天已经签过到了</div><div>获得 120 点魔力值</div></body></html>
"""


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _RecordingSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.headers = {}
        self.get_calls = []
        self.post_calls = []
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError(f"未预期的 GET：{url}")
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_responses:
            raise AssertionError(f"未预期的 POST：{url}")
        return self.post_responses.pop(0)


class QuizSignTests(unittest.TestCase):
    """彩虹岛 bakatest 答题签到的解析、提交与回读确认。"""

    rule = {
        "engine": "http",
        "form_mode": "quiz",
        "path": "/bakatest.php",
        "verify_success_path": "/bakatest.php",
        "success": r"\d+\s*点魔力值",
        "already": r"今天已经签过到了?",
        "quiz_failure": r"(回答错误|答案错误|请重新|不正确|失去)",
        "quiz_fields": {"usercomment": "太难了！", "wantskip": "不会"},
    }
    site = {
        "id": 12,
        "name": "彩虹岛",
        "url": "https://ptchdbits.co/",
        "cookie": "c_secure_uid=1",
    }

    def each_core(self):
        self.assertTrue(CORES)
        yield from CORES

    def test_parse_form_fields_choices_and_question(self):
        for core in self.each_core():
            form = core.extract_quiz_form(QUIZ_FORM)
            self.assertIsNotNone(form)
            self.assertEqual(form["questionid"], "42")
            self.assertEqual(form["choices"], ["A", "B", "C"])
            self.assertEqual(form["fields"]["formhash"], "hash-1")
            self.assertEqual(form["question"], "NexusPHP 的签到入口是哪一个？")

    def test_fixed_answer_and_question_mapping(self):
        for core in self.each_core():
            choices = ["A", "B", "C"]
            self.assertEqual(
                core.select_quiz_choices({"quiz_answer": "B"}, "题目", choices), ["B"])
            mapped = {"quiz_answers": {"NexusPHP 的签到入口是哪一个？": "B"}}
            self.assertEqual(
                core.select_quiz_choices(mapped, "NexusPHP 的签到入口是哪一个？", choices),
                ["B"])

    def test_random_answer_is_used_when_not_configured(self):
        for core in self.each_core():
            with mock.patch.object(core.random, "choice", side_effect=lambda values: values[-1]):
                self.assertEqual(core.select_quiz_choices({}, "题目", ["A", "B"]), ["B"])

    def test_payload_contains_hidden_and_quiz_fields(self):
        for core in self.each_core():
            form = core.extract_quiz_form(QUIZ_FORM)
            payload = core.build_quiz_payload(form, ["B"], self.rule)
            self.assertEqual(payload["questionid"], "42")
            self.assertEqual(payload["formhash"], "hash-1")
            self.assertEqual(payload["choice[]"], "B")
            self.assertEqual(payload["usercomment"], "太难了！")
            self.assertEqual(payload["wantskip"], "不会")

    def test_already_signed_page_does_not_post(self):
        for core in self.each_core():
            instances = []
            def session_factory():
                created = _RecordingSession([_Response(QUIZ_SIGNED)], [])
                instances.append(created)
                return created
            with mock.patch.object(requests, "Session", session_factory):
                result = core.http_sign(self.site, self.rule, timeout=3)
            self.assertEqual(result.status, "already")
            self.assertFalse(instances[0].post_calls)

    def test_success_requires_get_readback(self):
        for core in self.each_core():
            session = _RecordingSession([_Response(QUIZ_SIGNED)], [_Response(QUIZ_FORM)])
            result = core.http_quiz_sign(
                session, "https://ptchdbits.co/bakatest.php", QUIZ_FORM,
                "https://ptchdbits.co", self.rule, 3, None)
            self.assertEqual(result.status, "success")
            self.assertEqual(len(session.post_calls), 1)
            self.assertEqual(len(session.get_calls), 1)
            self.assertIn(session.post_calls[0][1]["data"]["choice[]"], ["A", "B", "C"])

    def test_bonus_in_post_response_does_not_bypass_readback(self):
        for core in self.each_core():
            session = _RecordingSession(
                [_Response(QUIZ_FORM)], [_Response("<html>获得 500 点魔力值</html>")])
            result = core.http_quiz_sign(
                session, "https://ptchdbits.co/bakatest.php", QUIZ_FORM,
                "https://ptchdbits.co", self.rule, 3, None)
            self.assertEqual(result.status, "failed")
            self.assertIn("quiz_answer", result.message)

    def test_quiz_failure_never_runs_generic_fallback(self):
        for core in self.each_core():
            calls = []
            original_sign = core.http_sign
            try:
                core.http_sign = lambda *args, **kwargs: core.SignResult("failed", "答题错误")
                core.http_two_step = lambda *args, **kwargs: calls.append("two_step")
                core.discover_signin_target = lambda *args, **kwargs: calls.append("discover")
                result = core.http_sign_auto(self.site, self.rule, timeout=3)
            finally:
                core.http_sign = original_sign
            self.assertEqual(result.status, "failed")
            self.assertEqual(calls, [])

    def test_52pt_default_success_regex_still_matches(self):
        for core in self.each_core():
            rule = core.DEFAULT_RULES["52pt.site"]
            self.assertIsNotNone(re.search(rule["success"], "获得 20 点魔力值"))


class SiteSelectionTests(unittest.TestCase):
    """未选站点、key-only 站点和旧配置轮次都不能继续访问站点。"""

    def each_plugin(self):
        self.assertTrue(PLUGINS)
        yield from PLUGINS

    @staticmethod
    def plugin(module, run_id=7):
        item = module.PTSiteSignIn.__new__(module.PTSiteSignIn)
        item._run_id = run_id
        item._stop_event = threading.Event()
        item._read_activity = True
        item._timeout = 3
        return item

    def test_site_id_string_is_not_split(self):
        for module in self.each_plugin():
            parse = module.PTSiteSignIn._parse_site_ids
            self.assertEqual(parse("12"), ([12], False))
            self.assertEqual(parse("[12,13,13]"), ([12, 13], False))
            self.assertEqual(parse("12，13;15 12"), ([12, 13, 15], False))
            self.assertEqual(parse([12, True, 0, -1, "13", 12]), ([12, 13], False))

    def test_unselected_site_does_not_login_sign_or_read_activity(self):
        for module in self.each_plugin():
            item = self.plugin(module)
            calls = []
            info = {"id": 12, "name": "彩虹岛", "url": "https://ptchdbits.co/",
                    "cookie": "c=1"}
            with mock.patch.object(module, "site_login", side_effect=lambda *a, **k: calls.append("login")), \
                    mock.patch.object(module, "http_sign_auto", side_effect=lambda *a, **k: calls.append("sign")), \
                    mock.patch.object(module, "run_site_module", side_effect=lambda *a, **k: calls.append("module")), \
                    mock.patch.object(module, "read_activity", side_effect=lambda *a, **k: calls.append("activity")):
                sign = item._sign_one(info, {}, None, run_id=7, allowed_site_ids={13})
                login = item._login_one(info, run_id=7, allowed_site_ids={13})
                activity = item._read_site_activity(
                    info, run_id=7, allowed_site_ids={13})
            self.assertEqual(sign["status"], "failed")
            self.assertEqual(login["status"], "failed")
            self.assertIn("不在本次", sign["message"])
            self.assertIn("不在本次", login["message"])
            self.assertIn("不在本次", activity[1])
            self.assertEqual(calls, [])

    def test_key_only_site_does_not_issue_network_request(self):
        for module in self.each_plugin():
            item = self.plugin(module)
            calls = []
            info = {"id": 12, "name": "Key站", "url": "https://key.example/",
                    "apikey": "api-key", "cookie": ""}
            with mock.patch.object(module, "site_login", side_effect=lambda *a, **k: calls.append("login")), \
                    mock.patch.object(module, "http_sign_auto", side_effect=lambda *a, **k: calls.append("sign")), \
                    mock.patch.object(module, "read_activity", side_effect=lambda *a, **k: calls.append("activity")):
                sign = item._sign_one(info, {}, None, run_id=7, allowed_site_ids={12})
                login = item._login_one(info, run_id=7, allowed_site_ids={12})
            self.assertEqual(sign["status"], "failed")
            self.assertEqual(login["status"], "failed")
            self.assertIn("API Key", sign["message"])
            self.assertEqual(calls, [])

    def test_quiz_rule_skips_site_module_and_browser_fallback(self):
        for module in self.each_plugin():
            item = self.plugin(module)
            calls = []
            info = {"id": 12, "name": "彩虹岛", "url": "https://ptchdbits.co/",
                    "cookie": "c=1"}
            rules = {"ptchdbits.co": {
                "engine": "http", "form_mode": "quiz", "path": "/bakatest.php"}}
            with mock.patch.object(module, "run_site_module", side_effect=lambda *a, **k: calls.append("module")), \
                    mock.patch.object(module, "http_sign_auto", return_value=module.SignResult("failed", "答题错误")), \
                    mock.patch.object(item, "_maybe_fallback", side_effect=AssertionError("不得回退")):
                result = item._sign_one(info, rules, None, run_id=7, allowed_site_ids={12})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(calls, [])
            self.assertFalse(item._should_fallback(rules["ptchdbits.co"],
                                                    module.SignResult("failed", "答题错误")))

    def test_stale_batch_does_not_save_or_notify(self):
        for module in self.each_plugin():
            item = self.plugin(module, run_id=8)
            calls = []
            item._finish = lambda results: calls.append(results)
            item._finish_current(7, [{"site": "旧批次", "status": "success"}])
            item._retry_task(1, 7, [], {}, None, {12}, {})
            self.assertEqual(calls, [])

    def test_quiz_sites_do_not_initialize_browserless(self):
        for module in self.each_plugin():
            item = self.plugin(module)
            infos = [{"id": 12, "name": "彩虹岛", "url": "https://ptchdbits.co/",
                      "cookie": "c=1"}]
            rules = {"ptchdbits.co": {
                "engine": "http", "form_mode": "quiz", "auto_fallback": True,
                "path": "/bakatest.php"}}
            self.assertFalse(item._should_init_signer(infos, rules))


if __name__ == "__main__":
    unittest.main()
