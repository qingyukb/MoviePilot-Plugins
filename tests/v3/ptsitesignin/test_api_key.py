"""API Key 登录验证与签到回归测试；全部使用假 requests，不访问真实站点。"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
PLUGIN_DIRS = tuple(directory for directory in (
    ROOT / "plugins.v2" / "ptsitesignin",
    ROOT / "plugins.v3" / "ptsitesignin",
) if directory.is_dir())

SECRET = "test-api-key-not-real"
TOKEN = "test-token-not-real"
BASE_URL = "https://api.example.test"


def load_core(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "core.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORES = [load_core(directory, f"ptsignin_api_core_{index}")
         for index, directory in enumerate(PLUGIN_DIRS)]


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = dict(headers or {})


class FakeSession:
    """只实现 API 流程用到的 request，并记录每次请求。"""

    def __init__(self, responder):
        self.responder = responder
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        call = dict(kwargs)
        call.update({"method": method, "url": url,
                     "headers": dict(kwargs.get("headers") or {})})
        self.calls.append(call)
        response = self.responder(method, url, kwargs)
        if isinstance(response, Exception):
            raise response
        return response


def fake_requests(session: FakeSession):
    module = types.ModuleType("requests")
    created = {"count": 0}

    def factory():
        created["count"] += 1
        return session

    module.Session = factory
    module.created = created
    return module


def make_site(**values):
    return {"id": 1, "name": "API 站点", "url": BASE_URL + "/", "ua": "",
            "apikey": SECRET, **values}


def api_rule(core, site, **values):
    custom = {"auth": "api_key", **values}
    return core.resolve_rule(site, {"api.example.test": custom})


class ApiKeyCoreTests(unittest.TestCase):
    def test_validation_search_and_sign_with_explicit_endpoints(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                rule = api_rule(
                    core, site,
                    credential_check_path="/api/profile",
                    credential_check_success="profile-ok",
                    path="/api/checkin",
                    method="POST",
                    json={"source": "moviepilot"},
                    success="NEVER_AUTO_SUCCESS",
                    already="NEVER_AUTO_ALREADY",
                )
                session = FakeSession(lambda method, url, kwargs: FakeResponse(
                    "{\"success\":true,\"message\":\"profile-ok\"}"
                    if url.endswith("/api/profile")
                    else "{\"success\":true,\"message\":\"签到成功\"}"))

                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    checked = core.http_check_api_key(site, rule, timeout=5)
                    signed = core.http_sign(site, rule, timeout=5)

                self.assertEqual(checked.status, "success")
                self.assertEqual(signed.status, "success")
                self.assertEqual(rule["engine"], "http")
                self.assertFalse(rule["auto_fallback"])
                self.assertEqual([call["url"] for call in session.calls], [
                    BASE_URL + "/api/profile", BASE_URL + "/api/checkin"])
                self.assertTrue(all(call["headers"]["Authorization"] == "Bearer " + SECRET
                                    for call in session.calls))
                self.assertEqual(session.calls[1]["json"], {"source": "moviepilot"})
                self.assertNotIn(SECRET, checked.message + signed.message)

    def test_authorization_x_api_key_and_token_sources(self):
        cases = [
            ("apikey", {}, "Authorization", "Bearer " + SECRET),
            ("apikey", {"api_key_header": "X-API-Key"}, "X-API-Key", SECRET),
            ("token", {"api_key_source": "token"}, "Authorization", "Bearer " + TOKEN),
        ]
        for core in CORES:
            for label, extra, header, expected in cases:
                with self.subTest(core=core.__name__, source=label, header=header):
                    site = make_site(token=TOKEN)
                    rule = api_rule(core, site, path="/api/checkin", **extra)
                    session = FakeSession(lambda method, url, kwargs: FakeResponse(
                        "{\"success\":true,\"message\":\"ok\"}"))
                    with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                        result = core.http_sign(site, rule, timeout=5)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(session.calls[0]["headers"][header], expected)

    def test_auto_key_source_uses_apikey_then_token(self):
        cases = [
            ({"apikey": SECRET, "token": TOKEN}, {}, "Bearer " + SECRET),
            ({"apikey": "", "token": TOKEN}, {}, "Bearer " + TOKEN),
            ({"apikey": SECRET, "token": TOKEN}, {"api_key_source": "token"},
             "Bearer " + TOKEN),
        ]
        for core in CORES:
            for values, extra, expected in cases:
                with self.subTest(core=core.__name__, extra=extra, values=values):
                    site = make_site(**values)
                    rule = api_rule(core, site, path="/api/checkin", **extra)
                    session = FakeSession(lambda method, url, kwargs: FakeResponse(
                        "{\"success\":true}"))
                    with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                        result = core.http_sign(site, rule, timeout=5)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(session.calls[0]["headers"]["Authorization"], expected)

    def test_key_only_site_auto_enables_api_mode_for_explicit_endpoint(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site(cookie="")
                rule = core.resolve_rule(site, {
                    "api.example.test": {"path": "/api/checkin"}})
                self.assertEqual(rule["auth"], "api_key")
                self.assertEqual(rule["api_key_source"], "auto")
                self.assertEqual(rule["engine"], "http")
                self.assertFalse(rule["auto_fallback"])

    def test_cookie_site_keeps_cookie_mode_even_if_key_is_present(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site(cookie="c=1")
                rule = core.resolve_rule(site, {
                    "api.example.test": {"path": "/custom-signin"}})
                self.assertNotEqual(rule.get("auth"), "api_key")
                self.assertEqual(rule["path"], "/custom-signin")

    def test_key_only_site_without_explicit_endpoint_does_not_auto_switch(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site(cookie="")
                rule = core.resolve_rule(site, {
                    "api.example.test": {"success": "ok"}})
                self.assertNotEqual(rule.get("auth"), "api_key")

    def test_login_only_and_sign_only_rules(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                login_rule = api_rule(core, site, credential_check_path="/api/profile",
                                      credential_check_success="ok")
                self.assertNotIn("path", login_rule)
                check_session = FakeSession(
                    lambda method, url, kwargs: FakeResponse("{\"ok\":true}"))
                with mock.patch.dict(sys.modules,
                                     {"requests": fake_requests(check_session)}):
                    checked = core.http_check_api_key(site, login_rule, timeout=5)
                self.assertEqual(checked.status, "success")

                sign_rule = api_rule(core, site, path="/api/checkin")
                self.assertNotIn("credential_check_path", sign_rule)
                sign_session = FakeSession(
                    lambda method, url, kwargs: FakeResponse("{\"success\":true}"))
                with mock.patch.dict(sys.modules,
                                     {"requests": fake_requests(sign_session)}):
                    signed = core.http_sign(site, sign_rule, timeout=5)
                self.assertEqual(signed.status, "success")

    def test_missing_operation_endpoint_fails_before_request(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                login_only = api_rule(core, site, credential_check_path="/api/profile")
                sign_session = FakeSession(lambda *args: self.fail("不应发起签到请求"))
                with mock.patch.dict(sys.modules,
                                     {"requests": fake_requests(sign_session)}):
                    signed = core.http_sign(site, login_only, timeout=5)
                self.assertEqual(signed.status, "failed")
                self.assertIn("未配置 path", signed.message)
                self.assertEqual(sign_session.calls, [])

                sign_only = api_rule(core, site, path="/api/checkin")
                check_session = FakeSession(lambda *args: self.fail("不应发起验证请求"))
                with mock.patch.dict(sys.modules,
                                     {"requests": fake_requests(check_session)}):
                    checked = core.http_check_api_key(site, sign_only, timeout=5)
                self.assertEqual(checked.status, "failed")
                self.assertIn("credential_check_path", checked.message)
                self.assertEqual(check_session.calls, [])

    def test_key_is_redacted_from_response_and_exception(self):
        for core in CORES:
            with self.subTest(core=core.__name__, case="response"):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin",
                                success="NEVER_AUTO_SUCCESS", already="NEVER_AUTO_ALREADY")
                session = FakeSession(lambda method, url, kwargs: FakeResponse(
                    json.dumps({"success": False, "message": "rejected " + SECRET})))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign(site, rule, timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertNotIn(SECRET, result.message)
                self.assertIn("***", result.message)

            with self.subTest(core=core.__name__, case="exception"):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin")
                session = FakeSession(
                    lambda method, url, kwargs: RuntimeError("denied " + SECRET))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign(site, rule, timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertNotIn(SECRET, result.message)
                self.assertIn("***", result.message)

    def test_http_sign_auto_uses_only_the_explicit_endpoint(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin")
                with mock.patch.object(core, "http_sign",
                                       return_value=core.SignResult("success", "签到成功")) as sign, \
                        mock.patch.object(core, "fetch_page",
                                          side_effect=AssertionError("API 模式不应读取首页")), \
                        mock.patch.object(core, "discover_signin_target",
                                          side_effect=AssertionError("API 模式不应发现路径")):
                    result = core.http_sign_auto(site, rule, timeout=5)
                self.assertEqual(result.status, "success")
                sign.assert_called_once_with(site, rule, timeout=5, proxies=None)

    def test_activity_without_explicit_path_does_not_fetch_index(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin")
                session = FakeSession(lambda *args: self.fail("不应读取首页"))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity(site, "", timeout=5, rule=rule)
                self.assertEqual(result.status, "failed")
                self.assertIn("activity_path", result.message)
                self.assertEqual(session.calls, [])

    def test_cross_origin_redirect_stops_without_second_request(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin")
                session = FakeSession(lambda method, url, kwargs: FakeResponse(
                    status_code=302, headers={"Location": "https://evil.example/collect"}))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign(site, rule, timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("站外", result.message)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.calls[0]["url"], BASE_URL + "/api/checkin")

    def test_api_key_with_newline_is_rejected(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site(apikey=SECRET + "\nInjected: yes")
                rule = api_rule(core, site, path="/api/checkin")
                session = FakeSession(lambda *args: self.fail("换行 Key 不应联网"))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign(site, rule, timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("换行", result.message)
                self.assertEqual(session.calls, [])

    def test_get_json_empty_object_is_sent_as_params(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = make_site()
                rule = api_rule(core, site, path="/api/checkin", method="GET", json={})
                session = FakeSession(lambda method, url, kwargs: FakeResponse(
                    "{\"success\":true}"))
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign(site, rule, timeout=5)
                self.assertEqual(result.status, "success")
                self.assertEqual(session.calls[0]["params"], {})

    def test_api_rule_does_not_inherit_builtin_signin_path(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site = {"url": "https://audiences.me/", "cookie": "c=1",
                        "apikey": SECRET}
                custom = {"audiences.me": {
                    "auth": "api_key", "credential_check_path": "/api/profile"}}
                rule = core.resolve_rule(site, custom)
                self.assertEqual(rule["auth"], "api_key")
                self.assertNotIn("path", rule)
                self.assertFalse(rule["auto_fallback"])

                with self.assertRaises(ValueError):
                    core.resolve_rule(site, {"audiences.me": {"auth": "api_key"}})


class ApiKeyPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.v3.ptsitesignin import test_plugin_page
        cls.page = test_plugin_page

    @staticmethod
    def _plugin(module, **attrs):
        plugin = module.PTSiteSignIn.__new__(module.PTSiteSignIn)
        plugin._run_id = 1
        plugin._stop_event = None
        plugin._read_activity = False
        plugin._timeout = 5
        plugin._now = lambda: "2026-11-04 12:00:00"  # type: ignore[method-assign]
        plugin._proxies_for = lambda info: None  # type: ignore[method-assign]
        for key, value in attrs.items():
            setattr(plugin, key, value)
        return plugin

    def test_only_key_site_without_rule_makes_no_request(self):
        info = make_site(apikey=SECRET, cookie="")
        for module in self.page.PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                with mock.patch.object(module, "http_sign_auto",
                                       side_effect=AssertionError("不应联网")) as sign, \
                        mock.patch.object(module, "run_site_module",
                                          side_effect=AssertionError("不应加载模块")) as run:
                    row = plugin._sign_one(info, {}, None, run_id=1)
                self.assertEqual(row["status"], "failed")
                self.assertIn("未声明", row["message"])
                sign.assert_not_called()
                run.assert_not_called()

    def test_api_key_rules_skip_browserless_initialization(self):
        info = make_site(cookie="c=1")
        rules = {"api.example.test": {
            "auth": "api_key", "path": "/api/checkin"}}
        for module in self.page.PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                self.assertFalse(plugin._should_init_signer([info], rules))

    def test_unselected_site_makes_no_request(self):
        info = make_site(apikey=SECRET, cookie="")
        rule = {"auth": "api_key", "engine": "http", "auto_fallback": False,
                "path": "/api/checkin"}
        for module in self.page.PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                with mock.patch.object(module, "http_sign_auto",
                                       side_effect=AssertionError("不应联网")) as sign:
                    row = plugin._sign_api_key_one(info, rule, run_id=1,
                                                   allowed_site_ids={99})
                self.assertEqual(row["status"], "failed")
                self.assertIn("不在本次", row["message"])
                sign.assert_not_called()

    def test_settings_page_lists_site_auth_type(self):
        key_site = self.page._Site(99, "纯Key站点", cookie="")
        key_site.apikey = SECRET
        cookie_site = self.page._Site(100, "纯Cookie站点", cookie="session=1")
        original = list(self.page.SITES)
        try:
            self.page.SITES[:] = [key_site, cookie_site]
            for module in self.page.PLUGINS:
                with self.subTest(plugin=module.__name__):
                    plugin = self._plugin(module, _site_ids=[], _login_site_ids=[],
                                          _all_sites=False, _all_login_sites=False)
                    form, _config = plugin.get_form()
                    selects = [node.get("props") or {}
                               for node in self.page.nodes(form, "VSelect")]
                    site_options = next(props["items"] for props in selects
                                        if props.get("model") == "site_ids")
                    titles = {item["value"]: item["title"] for item in site_options}
                    self.assertEqual(titles[99], "纯Key站点（ApiKey）")
                    self.assertEqual(titles[100], "纯Cookie站点（Cookie）")
        finally:
            self.page.SITES[:] = original


if __name__ == "__main__":
    unittest.main()
