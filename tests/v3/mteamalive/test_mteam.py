"""M-Team 保活插件测试：TOTP、统计解析、API 读取、页面渲染、保活编排。"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
PLUGIN_DIRS = (ROOT / "plugins.v2" / "mteamalive",
               ROOT / "plugins.v3" / "mteamalive")


class _Logger:
    def _noop(self, *args, **kwargs):
        pass

    info = warning = error = debug = exception = _noop


class _PluginBase:
    def __init__(self, *args, **kwargs):
        self._data = {}

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def del_data(self, key):
        self._data.pop(key, None)

    def update_config(self, config):
        self._config = config

    def post_message(self, **kwargs):
        self._last_message = kwargs


def _install_stubs() -> None:
    app = types.ModuleType("app")
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = _PluginBase
    log = types.ModuleType("app.log")
    log.logger = _Logger()
    schemas = types.ModuleType("app.schemas")
    types_mod = types.ModuleType("app.schemas.types")
    types_mod.NotificationType = types.SimpleNamespace(Plugin="Plugin", SiteMessage="SiteMessage")
    types_mod.EventType = types.SimpleNamespace(PluginAction="PluginAction")
    schemas.types = types_mod
    config = types.ModuleType("app.core.config")
    config.settings = types.SimpleNamespace(TZ="Asia/Shanghai", PROXY=None)
    core = types.ModuleType("app.core")
    core.config = config
    event_mod = types.ModuleType("app.core.event")

    class _Event:
        def __init__(self, event_data=None):
            self.event_data = event_data or {}

    class _EventManager:
        @staticmethod
        def register(event_type):
            def decorator(func):
                return func
            return decorator

    event_mod.Event = _Event
    event_mod.eventmanager = _EventManager()
    core.event = event_mod
    sched = types.ModuleType("apscheduler")
    sched_bg = types.ModuleType("apscheduler.schedulers")
    sched_bg_bg = types.ModuleType("apscheduler.schedulers.background")

    class _Scheduler:
        def add_job(self, **kwargs):
            pass

        def start(self):
            pass

        def remove_all_jobs(self):
            pass

        def shutdown(self, wait=False):
            pass

    sched_bg_bg.BackgroundScheduler = _Scheduler
    sched_tr = types.ModuleType("apscheduler.triggers")
    sched_tr_cron = types.ModuleType("apscheduler.triggers.cron")
    sched_tr_cron.CronTrigger = types.SimpleNamespace(
        from_crontab=lambda expr: types.SimpleNamespace(expr=expr))
    pytz = types.ModuleType("pytz")
    pytz.timezone = lambda name: None
    sys.modules.update({
        "app": app, "app.plugins": plugins, "app.log": log, "app.schemas": schemas,
        "app.schemas.types": types_mod, "app.core": core, "app.core.config": config,
        "app.core.event": event_mod,
        "apscheduler": sched, "apscheduler.schedulers": sched_bg,
        "apscheduler.schedulers.background": sched_bg_bg,
        "apscheduler.triggers": sched_tr, "apscheduler.triggers.cron": sched_tr_cron,
        "pytz": pytz,
    })


_install_stubs()


def _load(folder: Path):
    package = "mteamalive_" + folder.parent.name.replace(".", "_")
    spec = importlib.util.spec_from_file_location(
        package, folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


PLUGINS = [_load(folder) for folder in PLUGIN_DIRS]


class TotpTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238 附录 B：密钥 "12345678901234567890"（base32），T=59 时 8 位为 94287082
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.__name__):
                secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
                self.assertEqual(plugin.totp_now(secret, at=59, digits=8), "94287082")
                self.assertEqual(plugin.totp_now(secret, at=59), "287082")
                # 空格/小写也要能解
                self.assertEqual(plugin.totp_now("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59),
                                 "287082")
                self.assertEqual(plugin.totp_now("", at=59), "")


def _recent(minutes_ago: int = 0) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


class ProfileTests(unittest.TestCase):
    PAYLOAD = {"code": 0, "message": "ok", "data": {
        "id": "46599", "username": "fdfox", "email": "a@b.c",
        "memberStatus": {"lastLogin": "2026-10-18 21:00:00",
                         "lastBrowse": _recent(0)},
        "memberCount": {"uploaded": "1099511627776", "downloaded": "356515840000",
                        "bonus": 123456.7, "shareRate": 3.0836, "warning": 0},
    }}

    def test_parse_profile_fields(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.__name__):
                info = plugin.parse_profile(self.PAYLOAD)
                self.assertEqual(info["username"], "fdfox")
                self.assertEqual(info["id"], "46599")
                self.assertEqual(info["ratio"], "3.084")
                self.assertAlmostEqual(info["ratio_value"], 3.0836, places=3)
                self.assertEqual(info["uploaded"], "1.00 TB")
                self.assertEqual(info["bonus"], "123456.7")
                self.assertEqual(info["last_login"], "2026-10-18 21:00:00")
                self.assertTrue(info["last_browse"].startswith("20"))

    def test_parse_profile_tolerates_flat_shape(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.__name__):
                info = plugin.parse_profile({"data": {"username": "x", "memberCount":
                                                      {"ratio": "1.5"}}})
                self.assertEqual(info["ratio"], "1.500")


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeRequests:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FetchProfileTests(unittest.TestCase):
    def test_fetch_profile_ok(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.__name__):
                fake = _FakeRequests(_FakeResponse(ProfileTests.PAYLOAD))
                original = sys.modules.get("requests")
                sys.modules["requests"] = fake
                try:
                    info, error = plugin.fetch_profile("key-1")
                finally:
                    if original is None:
                        sys.modules.pop("requests", None)
                    else:
                        sys.modules["requests"] = original
                self.assertEqual(error, "")
                self.assertEqual(info["username"], "fdfox")
                self.assertEqual(fake.calls[0][1]["headers"]["x-api-key"], "key-1")

    def test_fetch_profile_errors(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.__name__):
                for response, expect in ((_FakeResponse({}, 401), "API Key 无效"),
                                         (_FakeResponse({"code": 1, "message": "err"}, 200),
                                          "接口返回异常")):
                    original = sys.modules.get("requests")
                    sys.modules["requests"] = _FakeRequests(response)
                    try:
                        info, error = plugin.fetch_profile("key")
                    finally:
                        if original is None:
                            sys.modules.pop("requests", None)
                        else:
                            sys.modules["requests"] = original
                    self.assertIsNone(info)
                    self.assertIn(expect, error)


class KeepaliveFlowTests(unittest.TestCase):
    def _plugin(self, module):
        plugin = module.MTeamAlive.__new__(module.MTeamAlive)
        plugin._data = {}
        plugin._enabled = True
        plugin._notify = True
        plugin._username = "fdfox"
        plugin._password = "pwd"
        plugin._totp_secret = "SECRET"
        plugin._api_key = "key"
        plugin._api_base = "https://api.m-team.cc"
        plugin._site_url = "https://kp.m-team.cc"
        plugin._ratio_warn = 1.0
        return plugin

    def test_keepalive_success_records_and_notifies(self):
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                plugin.site_apikey = staticmethod(lambda: "site-key")
                original_login, original_fetch = module.keepalive_login, module.fetch_profile
                module.keepalive_login = lambda *a, **k: (True, "登录成功", {})
                calls = {"n": 0}

                def fetch_two(*args, **kwargs):
                    calls["n"] += 1
                    payload = json.loads(json.dumps(ProfileTests.PAYLOAD))
                    payload["data"]["memberStatus"]["lastBrowse"] = (
                        _recent(5) if calls["n"] == 1 else _recent(0))
                    return module.parse_profile(payload), ""

                module.fetch_profile = fetch_two
                try:
                    record = plugin.keepalive()
                finally:
                    module.keepalive_login, module.fetch_profile = original_login, original_fetch
                self.assertTrue(record["ok"], record["message"])
                self.assertIn("登录成功", record["message"])
                self.assertEqual(record["ratio"], "3.084")
                self.assertEqual(len(plugin.get_data("history")), 1)
                self.assertEqual(plugin.get_data("last")["ok"], True)
                message = plugin._last_message["text"]
                self.assertIn("保活成功", message)
                self.assertIn("分享率", message)
                self.assertIn("最近动向", message)

    def test_keepalive_rejects_stale_activity(self):
        # 浏览器说登录成功，但 API 的最近动向还是 1 天前 → 判定本次没真正登录
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                plugin.site_apikey = staticmethod(lambda: "site-key")
                stale = {"code": 0, "data": {
                    "username": "fdfox",
                    "memberStatus": {"lastBrowse": _recent(60 * 24),
                                     "lastLogin": _recent(60 * 24)},
                    "memberCount": {"shareRate": 3.08}}}
                original_login, original_fetch = module.keepalive_login, module.fetch_profile
                module.keepalive_login = lambda *a, **k: (True, "登录成功", {})
                module.fetch_profile = lambda *a, **k: (module.parse_profile(stale), "")
                try:
                    record = plugin.keepalive()
                finally:
                    module.keepalive_login, module.fetch_profile = original_login, original_fetch
                self.assertFalse(record["ok"], record["message"])
                self.assertIn("判定本次没真正登录", record["message"])

    def test_api_key_falls_back_to_site_management(self):
        # 没手填 API Key 时，从 MoviePilot 站点管理的 m-team 站点里取
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                plugin._api_key = ""
                site = type("FakeSite", (), {"domain": "kp.m-team.cc", "url": "https://kp.m-team.cc",
                                             "apikey": "site-key", "cookie": "c=1"})
                oper = type("FakeSiteOper", (), {"list_order_by_pri": lambda self: [site]})()
                app_db = types.ModuleType("app.db.site_oper")
                app_db.SiteOper = lambda: oper
                original = sys.modules.get("app.db.site_oper")
                sys.modules["app.db.site_oper"] = app_db
                seen = {}

                calls = {"n": 0}

                def fake_fetch(key, base=None, timeout=20):
                    seen["key"] = key
                    calls["n"] += 1
                    payload = json.loads(json.dumps(ProfileTests.PAYLOAD))
                    payload["data"]["memberStatus"]["lastBrowse"] = (
                        _recent(5) if calls["n"] == 1 else _recent(0))
                    return module.parse_profile(payload), ""

                original_login, original_fetch = module.keepalive_login, module.fetch_profile
                module.keepalive_login = lambda *a, **k: (True, "登录成功", {})
                module.fetch_profile = fake_fetch
                try:
                    record = plugin.keepalive()
                finally:
                    module.keepalive_login, module.fetch_profile = original_login, original_fetch
                    if original is None:
                        sys.modules.pop("app.db.site_oper", None)
                    else:
                        sys.modules["app.db.site_oper"] = original
                self.assertEqual(seen.get("key"), "site-key")
                self.assertTrue(record["ok"], record["message"])

    def test_keepalive_without_api_key_uses_login_result(self):
        # 没配 API Key 时只能信浏览器结果
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                plugin._api_key = ""
                original = module.keepalive_login
                module.keepalive_login = lambda *a, **k: (True, "登录成功", {})
                try:
                    record = plugin.keepalive()
                finally:
                    module.keepalive_login = original
                self.assertTrue(record["ok"], record["message"])
                self.assertIn("登录成功", record["message"])

    def test_keepalive_failure_notifies_reason(self):
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = self._plugin(module)
                original = module.keepalive_login
                original_fetch = module.fetch_profile
                module.keepalive_login = lambda *a, **k: (False, "需要 2FA 但没有配置 TOTP 密钥", {})
                module.fetch_profile = lambda *a, **k: (None, "mock")
                try:
                    record = plugin.keepalive()
                finally:
                    module.keepalive_login = original
                    module.fetch_profile = original_fetch
                self.assertFalse(record["ok"])
                self.assertIn("保活失败", plugin._last_message["text"])
                self.assertIn("TOTP", plugin._last_message["text"])


class PageTests(unittest.TestCase):
    def test_empty_page_hint(self):
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = module.MTeamAlive.__new__(module.MTeamAlive)
                plugin._data = {}
                page = plugin.get_page()
                self.assertEqual(page[0]["component"], "VAlert")

    def test_page_renders_stats_and_rows(self):
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = module.MTeamAlive.__new__(module.MTeamAlive)
                plugin._data = {
                    "profile": module.parse_profile(ProfileTests.PAYLOAD),
                    "history": [{"time": "2026-10-19 09:00:00", "ok": True, "message": "登录成功"},
                                {"time": "2026-10-18 09:00:00", "ok": False, "message": "网络异常"}],
                    "last": {"time": "2026-10-19 09:00:00", "ok": True},
                }
                plugin._ratio_warn = 1.0
                page = plugin.get_page()

                def walk(node):
                    if isinstance(node, dict):
                        yield node
                        for child in node.get("content") or []:
                            yield from walk(child)
                    elif isinstance(node, list):
                        for child in node:
                            yield from walk(child)

                nodes = list(walk(page))
                texts = [node.get("text") for node in nodes if node.get("text")]
                self.assertIn("分享率", texts)
                self.assertIn("3.084", texts)
                self.assertIn("成功", texts)
                self.assertIn("失败", texts)
                self.assertIn("2026-10-19 09:00:00", texts)


class FormTests(unittest.TestCase):
    def test_form_has_expected_models(self):
        for module in PLUGINS:
            with self.subTest(plugin=module.__name__):
                plugin = module.MTeamAlive.__new__(module.MTeamAlive)
                form, defaults = plugin.get_form()
                text = str(form)
                for model in ("enabled", "notify", "run_once", "cron", "site_url",
                              "username", "password", "totp_secret",
                              "api_base", "ratio_warn", "verify_minutes", "interval_days"):
                    self.assertIn(f"'model': '{model}'", text)
                self.assertEqual(defaults["site_url"], module.DEFAULT_SITE)
                self.assertEqual(defaults["cron"], "0 9 * * *")


if __name__ == "__main__":
    unittest.main()
