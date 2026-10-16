"""插件 __init__.py 的页面 / 配置逻辑回归测试。

MoviePilot 运行时不可用，这里用最小桩替换掉 app.plugins、SiteOper、apscheduler 等依赖，
只验证「详情页怎么分组、怎么统计」「设置页选项与配置回写」这些纯逻辑。
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIRS = (
    ROOT / "plugins.v2" / "ptsitesignin",
    ROOT / "plugins.v3" / "ptsitesignin",
)


class _Logger:
    def _noop(self, *args, **kwargs):
        pass

    info = warning = error = debug = exception = _noop


class _PluginBase:
    def __init__(self, *args, **kwargs):
        pass

    def get_data(self, key):
        return None

    def save_data(self, key, value):
        pass

    def del_data(self, key):
        pass

    def update_config(self, config):
        pass

    def post_message(self, **kwargs):
        pass


class _Site:
    def __init__(self, site_id: int, name: str, cookie: str = "c_secure_uid=1"):
        self.id = site_id
        self.name = name
        self.cookie = cookie
        self.url = f"https://{name.lower()}.example"
        self.domain = f"{name.lower()}.example"
        self.ua = ""
        self.proxy = False
        self.rule = None
        self.apikey = None
        self.token = None
        self.note = None
        self.timeout = 30


SITES = [_Site(1, "观众"), _Site(2, "学校"), _Site(3, "红豆饭")]


class _SiteOper:
    def list_order_by_pri(self):
        return list(SITES)


def _install_stubs() -> None:
    """把 MoviePilot / apscheduler 的最小桩塞进 sys.modules。"""
    app = types.ModuleType("app")
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = _PluginBase
    log = types.ModuleType("app.log")
    log.logger = _Logger()
    schemas = types.ModuleType("app.schemas")
    schemas.NotificationType = types.SimpleNamespace(SiteMessage="SiteMessage")
    db = types.ModuleType("app.db")
    db_oper = types.ModuleType("app.db.oper")
    db_oper_site = types.ModuleType("app.db.oper.site")
    db_oper_site.SiteOper = _SiteOper
    db_site_oper = types.ModuleType("app.db.site_oper")
    db_site_oper.SiteOper = _SiteOper
    sched = types.ModuleType("apscheduler")
    sched_bg = types.ModuleType("apscheduler.schedulers")
    sched_bg_bg = types.ModuleType("apscheduler.schedulers.background")
    sched_bg_bg.BackgroundScheduler = object
    sched_trig = types.ModuleType("apscheduler.triggers")
    sched_trig_cron = types.ModuleType("apscheduler.triggers.cron")
    sched_trig_cron.CronTrigger = types.SimpleNamespace(from_crontab=lambda expr: expr)
    sys.modules.update({
        "app": app, "app.plugins": plugins, "app.log": log, "app.schemas": schemas,
        "app.db": db, "app.db.oper": db_oper, "app.db.oper.site": db_oper_site,
        "app.db.site_oper": db_site_oper,
        "apscheduler": sched, "apscheduler.schedulers": sched_bg,
        "apscheduler.schedulers.background": sched_bg_bg,
        "apscheduler.triggers": sched_trig, "apscheduler.triggers.cron": sched_trig_cron,
    })


_install_stubs()


def load_plugin(directory: Path, name: str):
    """把插件目录当包加载，让 ``from .core import ...`` 能解析。"""
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PLUGINS = [load_plugin(directory, f"ptsignin_plugin_{index}")
           for index, directory in enumerate(PLUGIN_DIRS)]


def walk(node):
    """遍历页面 JSON，产出所有组件节点（text 与 props 同级，别只看 props）。"""
    if isinstance(node, dict):
        if "component" in node:
            yield node
        for child in node.get("content") or []:
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def nodes(page, component):
    return [node for node in walk(page) if node.get("component") == component]


def texts(page):
    return " ".join(str(node.get("text") or "") for node in walk(page))


def panel_titles(page):
    return [node.get("props", {}).get("title") for node in nodes(page, "VExpansionPanel")]


def chip_labels(page):
    return [node.get("text") for node in nodes(page, "VChip")]


class PagePluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin_module = PLUGINS[0]
        self.plugin = self._make({})

    def _make(self, data, **attrs):
        plugin = self.plugin_module.PTSiteSignIn.__new__(self.plugin_module.PTSiteSignIn)
        plugin._data = data
        plugin._site_ids = [1, 2]
        plugin._login_site_ids = [2, 3]
        plugin._all_sites = False
        plugin._all_login_sites = False
        for key, value in attrs.items():
            setattr(plugin, key, value)
        return plugin

    def _page(self, latest, history=None):
        data = {"latest": latest}
        if history:
            data["history_index"] = ["history_20261016"]
            data["history_20261016"] = history
        plugin = self._make(data)
        original = plugin.get_data
        plugin.get_data = lambda key: data.get(key)          # type: ignore[method-assign]
        try:
            return plugin.get_page(), plugin
        finally:
            plugin.get_data = original                        # type: ignore[method-assign]

    def test_get_page_splits_signin_and_login(self):
        latest = [
            {"site": "观众", "status": "success", "message": "签到成功（获得 20 粒爆米花）",
             "engine": "browserless", "ran_at": "2026-10-17 08:30:00", "login": "",
             "activity": ""},
            {"site": "学校", "status": "login", "message": "成功（已换用新 Cookie）",
             "engine": "http", "ran_at": "2026-10-17 08:30:00", "login": "",
             "activity": "2026-10-17 07:12:03 ( < 1分前 )"},
            {"site": "红豆饭", "status": "failed", "message": "Cookie 无效或已过期",
             "engine": "http", "ran_at": "2026-10-17 08:30:00"},
        ]
        page, _ = self._page(latest, history=latest)
        titles = panel_titles(page)
        self.assertIn("观众 · 1 条记录", titles)      # 只在签到状态里
        self.assertIn("学校 · 1 条记录", titles)      # 只在登录状态里
        self.assertIn("红豆饭 · 1 条记录", titles)
        headers = [node.get("props", {}).get("class") for node in page
                   if isinstance(node, dict) and node.get("component") == "div"]
        self.assertIn("ptsignin-section", headers)
        self.assertTrue(nodes(page, "VExpansionPanels"))
        self.assertIn("最近动向：2026-10-17 07:12:03", texts(page))

    def test_dual_site_shows_in_both_sections(self):
        latest = [{"site": "观众", "status": "success", "message": "签到成功",
                   "engine": "browserless", "ran_at": "2026-10-17 08:30:00",
                   "login": "成功（已换用新 Cookie）", "activity": "2026-10-17 08:29:00",
                   "dual": True, "login_status": "login"}]
        page, plugin = self._page(latest, history=latest)
        self.assertEqual(panel_titles(page).count("观众 · 1 条记录"), 2)   # 签到 + 登录 各一份
        labels = chip_labels(page)
        self.assertIn("签到成功", labels)
        self.assertIn("登录成功", labels)

    def test_dual_login_failure_marks_login_section(self):
        latest = [{"site": "观众", "status": "success", "message": "签到成功",
                   "ran_at": "2026-10-17 08:30:00", "login": "失败：账号或密码错误",
                   "dual": True, "login_status": "login_failed"}]
        page, _ = self._page(latest)
        labels = chip_labels(page)
        self.assertIn("登录失败", labels)
        self.assertIn("签到成功", labels)

    def test_summary_counts_and_missing_alert(self):
        latest = [{"site": "观众", "status": "success", "message": "签到成功"},
                  {"site": "学校", "status": "failed",
                   "message": "签到后二次核验未显示签到成功状态"}]
        page, _ = self._page(latest)
        values = [node.get("text") for node in nodes(page, "div")
                  if str(node.get("props", {}).get("class", "")).startswith("ptsignin-stat__value")]
        self.assertEqual(values[:5], ["2", "1", "0", "1", "1"])   # 站点/成功/失败/未确认/未记录
        alerts = [node.get("text") for node in nodes(page, "VAlert")
                  if "本轮没有记录的站点" in str(node.get("text"))]
        self.assertTrue(alerts)
        self.assertIn("红豆饭", alerts[0])

    def test_empty_state(self):
        page, _ = self._page([])
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["component"], "VAlert")

    def test_history_counts_accumulate(self):
        history = [{"site": "观众", "status": "success", "ran_at": f"2026-10-{day:02d} 08:00:00"}
                   for day in range(1, 13)]
        page, _ = self._page([history[-1]], history=history)
        self.assertIn("观众 · 12 条记录", panel_titles(page))

    def test_parse_site_ids_handles_all_sentinel(self):
        parse = self.plugin_module.PTSiteSignIn._parse_site_ids
        self.assertEqual(parse(["all", 3, "5"]), ([3, 5], True))
        self.assertEqual(parse([1, "2"]), ([1, 2], False))
        self.assertEqual(parse(None), ([], False))
        self.assertEqual(parse("all"), ([], True))

    def test_config_round_trip_keeps_all_sentinel(self):
        plugin = self._make({}, _all_sites=True, _all_login_sites=False)
        config = plugin._config()
        self.assertEqual(config["site_ids"], [1, 2, "all"])
        self.assertEqual(config["login_site_ids"], [2, 3])

    def test_get_form_has_sections_and_all_option(self):
        plugin = self._make({})
        form, config = plugin.get_form()
        titles = [node.get("text") for node in walk(form)
                  if "ptsignin-form-title" in str(node.get("props", {}).get("class"))]
        for expected in ("基础", "站点选择", "执行与重试", "通知", "高级"):
            self.assertIn(expected, titles)
        selects = [node.get("props") or {} for node in nodes(form, "VSelect")]
        models = {props.get("model") for props in selects}
        self.assertIn("site_ids", models)
        self.assertIn("login_site_ids", models)
        for props in selects:
            if props.get("model") in {"site_ids", "login_site_ids"}:
                self.assertEqual(props["items"][0]["value"], "all")
                self.assertIn("全部站点", props["items"][0]["title"])
        self.assertIsInstance(config, dict)
        self.assertIn("同时", texts(form))       # 双选说明提示


if __name__ == "__main__":
    unittest.main()
