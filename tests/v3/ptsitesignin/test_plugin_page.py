"""插件 __init__.py 的页面 / 配置逻辑回归测试。

MoviePilot 运行时不可用，这里用最小桩替换掉 app.plugins、SiteOper、apscheduler 等依赖，
只验证「详情页怎么分组、怎么统计」「设置页选项与配置回写」这些纯逻辑。
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
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


def table_rows(page):
    """把方框表格的行抽成 [(站点单元格文本, 状态, 结果)]。"""
    rows = []
    for node in nodes(page, "tr"):
        cells = [cell for cell in (node.get("content") or []) if cell.get("component") == "td"]
        if len(cells) != 3:
            continue
        site_cell, status_cell, result_cell = cells
        site = " ".join(str(item.get("text") or "") for item in (site_cell.get("content") or []))
        status = " ".join(str(item.get("text") or "") for item in (status_cell.get("content") or []))
        rows.append((site.strip(), status.strip(), str(result_cell.get("text") or "").strip()))
    return rows


def row_pairs(page):
    """(站点名, 状态, 结果) —— 站点单元格里的说明文字去掉，只留站点名。"""
    return [(site.split(" ")[0], status, result) for site, status, result in table_rows(page)]


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

    def test_get_page_renders_boxed_tables(self):
        latest = [
            {"site": "观众", "status": "success", "message": "签到成功（获得 20 粒爆米花）",
             "engine": "browserless", "ran_at": "2026-10-17 08:30:00", "login": "",
             "activity": ""},
            {"site": "学校", "status": "login", "message": "成功（已换用新 Cookie）",
             "engine": "http", "ran_at": "2026-10-17 08:30:00", "login": "",
             "activity": "2026-10-17 07:12:03 ( < 1分前 )"},
        ]
        page, _ = self._page(latest, history=latest)
        # 两块各一张表，表头固定为「站点 / 状态 / 结果」
        self.assertEqual([node.get("text") for node in nodes(page, "th")],
                         ["站点", "状态", "结果"] * 2)
        wrappers = [node for node in nodes(page, "div")
                    if "ptsignin-table-wrap" in str(node.get("props", {}).get("class"))]
        self.assertEqual(len(wrappers), 2)               # 每块都用一个方框包起来
        tables = [node.get("props") or {} for node in nodes(page, "VTable")]
        self.assertEqual(len(tables), 2)
        for props in tables:
            self.assertEqual(props.get("class"), "ptsignin-table")   # 两张表共用固定列宽
        pairs = row_pairs(page)
        self.assertIn(("观众", "签到成功", "获得 20 粒爆米花"), pairs)
        self.assertIn(("学校", "登录成功", "2026-10-17 07:12:03 ( < 1分前 )"), pairs)
        self.assertIn(("学校", "未记录", "—"), pairs)      # 纯登录站点不进签到状态
        self.assertIn(("红豆饭", "未记录", "—"), pairs)    # 今天没记录 → 未记录

    def test_table_columns_are_aligned_by_css(self):
        # 固定列宽保证上下两张表的「站点 / 状态 / 结果」逐列对齐，行高也一致
        style = self.plugin_module.PAGE_STYLE
        self.assertIn(".ptsignin-table{width:100%;table-layout:fixed}", style)
        self.assertIn(".ptsignin-table th:nth-child(1),.ptsignin-table td:nth-child(1){width:30%}", style)
        self.assertIn("-webkit-line-clamp:3", style)

    def test_signin_result_prefers_bonus_field(self):
        latest = [{"site": "AGSVPT", "status": "success", "message": "签到成功（65,）",
                   "bonus": "获得 65", "ran_at": "2026-10-17 03:20:02", "engine": "http"}]
        page, _ = self._page(latest, history=latest)
        self.assertIn(("AGSVPT", "签到成功", "获得 65"), row_pairs(page))

    def test_dual_site_shows_in_both_tables(self):
        latest = [{"site": "观众", "status": "already", "ran_at": "2026-10-17 03:11:49",
                   "message": "今日已经签到（获得 20 粒爆米花）", "bonus": "获得 20 粒爆米花",
                   "login": "复用站点管理 Cookie", "activity": "2026-10-17 03:11:47",
                   "dual": True, "login_status": "login"}]
        page, _ = self._page(latest, history=latest)
        pairs = row_pairs(page)
        self.assertIn(("观众", "今日已签到", "获得 20 粒爆米花"), pairs)   # 签到那块的「结果」
        self.assertIn(("观众", "登录成功", "2026-10-17 03:11:47"), pairs)  # 登录那块的「结果」= 最近动向

    def test_dual_login_failure_marks_login_section(self):
        latest = [{"site": "观众", "status": "success", "message": "签到成功", "bonus": "获得 6",
                   "ran_at": "2026-10-17 08:30:00", "login": "失败：账号或密码错误",
                   "dual": True, "login_status": "login_failed"}]
        page, _ = self._page(latest, history=latest)
        pairs = row_pairs(page)
        self.assertIn(("观众", "登录失败", "失败：账号或密码错误"), pairs)
        self.assertIn(("观众", "签到成功", "获得 6"), pairs)

    def test_summary_counts_and_missing_alert(self):
        latest = [{"site": "观众", "status": "success", "message": "签到成功"},
                  {"site": "学校", "status": "failed",
                   "message": "签到后二次核验未显示签到成功状态"}]
        page, _ = self._page(latest)
        counters = [node.get("text") for node in nodes(page, "VChip")]
        self.assertEqual(counters[:5], ["站点 2", "成功 1", "失败 0", "未确认 1", "未记录 1"])
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
        captions = [site for site, _status, _result in table_rows(page)]
        self.assertTrue(any("共 12 条记录" in caption for caption in captions))

    def test_fallback_uses_discovered_signin_entry(self):
        # 撞盾时先按首页「每日签到」表单修正签到入口，再交给浏览器（别让浏览器开 404 的地址）
        plugin = self._make({})
        plugin._timeout = 5
        plugin._proxy_host = lambda: ""
        home = ('<html><body><form method="post" action="/signin.php">'
                '<input type="submit" name="signin" value="每日签到"></form></body></html>')
        self.plugin_module.fetch_page = lambda *a, **k: home
        captured = {}

        class _Signer:
            def sign(self, info, rule):
                captured["rule"] = dict(rule)
                return plugin_module.SignResult("success", "签到成功")

        plugin_module = self.plugin_module
        outcome, engine = plugin._maybe_fallback(
            {"url": "https://pt.example/", "cookie": "c=1"},
            {"engine": "http", "path": "/attendance.php", "auto_fallback": True},
            plugin_module.SignResult("failed", "站点返回 Cloudflare 验证页", challenge="cloudflare"),
            _Signer())
        self.assertEqual(engine, "browserless")
        self.assertEqual(captured["rule"]["path"], "/signin.php")
        self.assertEqual(outcome.status, "success")

    def test_activity_entities_decoded_on_display(self):
        # 修复前存进历史的记录里是 `&lt;`，显示时要解码（不用等下一轮重抓）
        latest = [{"site": "观众", "status": "already", "ran_at": "2026-10-17 03:52:21",
                   "message": "今日已经签到（获得 20 粒爆米花）", "bonus": "获得 20 粒爆米花",
                   "login": "复用站点管理 Cookie",
                   "activity": "2026-10-17 03:52:21 ( &lt; 1分前 )",
                   "dual": True, "login_status": "login"}]
        page, _ = self._page(latest, history=latest)
        pairs = row_pairs(page)
        self.assertIn(("观众", "登录成功", "2026-10-17 03:52:21 ( < 1分前 )"), pairs)
        self.assertNotIn("&lt;", str(page))

    def test_notify_switch_and_message(self):
        plugin = self._make({}, _notify_enabled=True)
        sent = []
        plugin.post_message = lambda **kwargs: sent.append(kwargs)
        results = [
            {"site": "观众", "status": "success", "message": "签到成功（获得 20 粒爆米花）",
             "login": "成功（已换用新 Cookie）", "dual": True, "login_status": "login"},
            {"site": "红豆饭", "status": "failed", "message": "Cookie 无效或已过期"},
        ]
        plugin._notify(results)
        self.assertEqual(len(sent), 1)
        text = sent[0]["text"]
        self.assertTrue(text.startswith("签到成功 1｜登录成功 1｜失败 1"))
        self.assertIn("· 红豆饭：Cookie 无效或已过期", text)
        self.assertNotIn("观众", text)                    # 成功站点不逐条罗列
        plugin._notify_enabled = False                    # 关掉开关 → 不发送
        plugin._notify(results)
        self.assertEqual(len(sent), 1)
        plugin._notify_enabled = True                     # 全部成功 → 只有一行
        plugin._notify([{"site": "PT时间", "status": "already", "message": "今日已签到"}])
        self.assertEqual(sent[-1]["text"], "签到成功 1｜登录成功 0｜失败 0")

    def test_notify_counts_login_only_sites(self):
        # 只勾在「登录站点」里的站点：签到成功记 0，登录成功记 1；双选站点两边各记一次
        plugin = self._make({}, _notify_enabled=True)
        sent = []
        plugin.post_message = lambda **kwargs: sent.append(kwargs)
        plugin._notify([{"site": "皇后", "status": "login", "message": "成功（已换用新 Cookie）"}])
        self.assertEqual(sent[-1]["text"], "签到成功 0｜登录成功 1｜失败 0")
        plugin._notify([{"site": "观众", "status": "already", "message": "今日已签到",
                         "dual": True, "login_status": "login"}])
        self.assertEqual(sent[-1]["text"], "签到成功 1｜登录成功 1｜失败 0")

    def test_notify_switch_backward_compatible(self):
        parse = self.plugin_module.PTSiteSignIn._parse_notify_switch
        self.assertFalse(parse({"notify": False}))
        self.assertTrue(parse({"notify": True}))
        self.assertFalse(parse({"notify_mode": "off"}))    # 1.3.x 的三档配置
        self.assertTrue(parse({"notify_mode": "failed"}))
        self.assertTrue(parse({}))

    def test_proxy_follows_site_switch(self):
        # 代理不再由插件开关控制，直接跟随「站点管理 → 使用代理访问」
        plugin = self._make({})
        plugin._proxy_host = lambda: ""
        logs = []
        self.plugin_module.logger.warning = lambda *a, **k: logs.append(str(a[0]))
        self.assertIsNone(plugin._proxies_for({"proxy": False}))     # 站点没勾 → 直连
        self.assertIsNone(plugin._proxies_for({}))                   # 记录里没有该字段 → 直连
        self.assertIsNone(plugin._proxies_for({"proxy": True}))      # 勾了但没配全局代理 → 直连 + 写日志
        self.assertTrue(any("直连" in item for item in logs), logs)
        plugin._proxy_host = lambda: "http://user:pw@127.0.0.1:7890"
        proxies = plugin._proxies_for({"proxy": True})
        self.assertEqual(proxies["http"], "http://user:pw@127.0.0.1:7890")
        self.assertEqual(proxies["https"], "http://user:pw@127.0.0.1:7890")
        self.assertEqual(self.plugin_module.mask_proxy("http://user:pw@127.0.0.1:7890"),
                         "http://***@127.0.0.1:7890")

    def test_skip_already_signed_sites_by_default(self):
        # 今天已签到成功的站点一律不再跑（Cron 与手动「保存后立即执行一次」同样规则）
        stamp = datetime.now().strftime("%Y-%m-%d")
        key = "history_" + datetime.now().strftime("%Y%m%d")
        data = {key: [{"site": "观众", "status": "already", "ran_at": f"{stamp} 09:00:00"},
                      {"site": "红豆饭", "status": "failed", "ran_at": f"{stamp} 09:00:00"}]}
        plugin = self._make(data)
        plugin.get_data = lambda key_: data.get(key_)
        keep, skipped = plugin._sites_to_run(
            [{"name": "观众"}, {"name": "红豆饭"}, {"name": "学校"}])
        self.assertEqual([item["name"] for item in keep], ["红豆饭", "学校"])   # 失败/未跑照常跑
        self.assertEqual([row["site"] for row in skipped], ["观众"])

    def test_clear_today_cache_removes_today_records(self):
        stamp = datetime.now().strftime("%Y-%m-%d")
        key = "history_" + datetime.now().strftime("%Y%m%d")
        data = {
            "latest": [{"site": "观众", "ran_at": f"{stamp} 09:00:00"},
                       {"site": "红豆饭", "ran_at": "2026-10-16 09:00:00"}],
            key: [{"site": "观众", "ran_at": f"{stamp} 09:00:00"}],
            "history_index": [key, "history_20261016"],
        }
        saved = {}
        plugin = self._make(data)
        plugin.get_data = lambda key_: data.get(key_)
        plugin.save_data = lambda key_, value: saved.__setitem__(key_, value)
        plugin._delete_data = lambda key_: saved.__setitem__(key_, None)
        plugin._clear_today_cache()
        self.assertEqual([row["site"] for row in saved["latest"]], ["红豆饭"])
        self.assertIsNone(saved[key])                        # 今天的记录已经删掉
        self.assertEqual(saved["history_index"], ["history_20261016"])

    def test_proxy_host_falls_back_to_env(self):
        import os
        from unittest import mock

        plugin = self._make({})
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://env.example:8080"}, clear=True):
            self.assertEqual(plugin._proxy_host(), "http://env.example:8080")

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

    def test_get_form_layout_and_all_option(self):
        plugin = self._make({})
        form, config = plugin.get_form()
        # 顶部是开关行，然后是两列字段，没有分区大标题（参考 MP 官方插件的扁平布局）
        switches = [node.get("props") or {} for node in nodes(form, "VSwitch")]
        models = {props.get("model") for props in switches}
        for expected in ("enabled", "notify", "run_once", "clear_today", "read_activity"):
            self.assertIn(expected, models)
        self.assertNotIn("skip_signed_today", models)      # 跳过今天已签到是默认行为，不给开关
        self.assertNotIn("fallback_browserless", models)   # 自动回退恒为开启，不再给开关
        self.assertNotIn("proxy", models)      # 代理改成跟随站点管理里的「使用代理访问」
        select_models = {node.get("props", {}).get("model") for node in nodes(form, "VSelect")}
        self.assertNotIn("notify_mode", select_models)     # 通知模式下拉已由「发送通知」开关取代
        selects = [node.get("props") or {} for node in nodes(form, "VSelect")]
        select_models = {props.get("model") for props in selects}
        self.assertIn("site_ids", select_models)
        self.assertIn("login_site_ids", select_models)
        for props in selects:
            if props.get("model") in {"site_ids", "login_site_ids"}:
                self.assertEqual(props["items"][0]["value"], "all")
                self.assertIn("全部站点", props["items"][0]["title"])
                self.assertIn("选择全部后自动包含后续新增站点", props["hint"])
        hints = " ".join(str((node.get("props") or {}).get("hint") or "") for node in walk(form))
        self.assertIn("同时选中", hints)             # 双选说明放在登录站点的提示里
        self.assertNotIn("ptsignin-form-title", str(form))
        self.assertIsInstance(config, dict)


if __name__ == "__main__":
    unittest.main()
