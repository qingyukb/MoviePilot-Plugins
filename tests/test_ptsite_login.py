"""core.py「站点登录 + 最近动向」回归测试：不依赖 MoviePilot 与真实网络。"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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


CORES = [load_core(directory, f"ptsignin_login_core_{index}")
         for index, directory in enumerate(PLUGIN_DIRS)]

LOGIN_HTML = (
    '<html><head><title>登录</title></head><body>'
    '<form action="takelogin.php" method="post">'
    '<input type="hidden" name="returnto" value="index.php">'
    '<input name="username"><input type="password" name="password">'
    '<input type="submit" name="login" value="登录">'
    "</form></body></html>")
HOME_HTML = ("<html><head><title>首页</title></head><body>"
             '<a href="logout.php">退出</a>'
             '<a href="userdetails.php?id=123">我的资料</a></body></html>')
INDEX_HTML = ("<html><head><title>首页</title></head><body>"
              '<a href="userdetails.php?id=123">我的资料</a>'
              '<a href="logout.php">退出</a></body></html>')
DETAIL_HTML = ("<html><body><table><tr><td>最近动向</td>"
               "<td>上次访问：2026-10-16 08:12:03</td></tr></table></body></html>")
LOGIN_REQUIRED_HTML = ("<html><head><title>登录</title></head><body>"
                       '<a href="login.php?returnto=attendance.php">请先登录</a></body></html>')
FAIL_HTML = ('<html><body><form action="takelogin.php" method="post">'
             '<input name="username"><input type="password" name="password"></form>'
             "<div>用户名或密码错误</div></body></html>")


class FakeCookie:
    def __init__(self, name: str, value: str):
        self.name, self.value = name, value


class FakeResponse:
    def __init__(self, url: str, text: str = "", status_code: int = 200):
        self.url, self.text, self.status_code = url, text, status_code


class FakeSession:
    """按 URL 片段分发的假 requests.Session。"""

    def __init__(self, routes, cookies=None):
        self.routes = routes                       # {url: handler(session, url) -> FakeResponse}
        self.cookies = [FakeCookie(name, value) for name, value in (cookies or [])]
        self.headers = {}
        self.calls = []

    def _handle(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs.get("data")))
        for target, handler in sorted(self.routes.items(), key=lambda item: -len(item[0])):
            if target in url:
                return handler(self, url)
        raise AssertionError(f"未匹配到路由：{method} {url}")

    def get(self, url, **kwargs):
        return self._handle("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._handle("POST", url, **kwargs)


def fake_requests(session: FakeSession):
    module = types.ModuleType("requests")
    module.Session = lambda: session
    return module


class LoginCoreTests(unittest.TestCase):
    def test_parse_accounts_filters_incomplete_entries(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                parsed = core.parse_accounts(
                    '{"audiences.me":{"username":"u","password":"p"},'
                    '"bad":{"username":"x"},"3":{"username":"a","password":"b"}}')
                self.assertEqual(set(parsed), {"audiences.me", "3"})
                self.assertEqual(parsed["audiences.me"]["password"], "p")
                with self.assertRaises(ValueError):
                    core.parse_accounts("[1,2,3]")
                with self.assertRaises(ValueError):
                    core.parse_accounts("{bad json")

    def test_account_for_matches_id_domain_and_www(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                accounts = {"7": {"username": "u", "password": "p"},
                            "audiences.me": {"username": "u2", "password": "p2"}}
                self.assertEqual(core.account_for({"id": 7, "url": "https://x.me/"},
                                                  accounts)["username"], "u")
                self.assertEqual(core.account_for({"url": "https://www.audiences.me/"},
                                                  accounts)["username"], "u2")
                self.assertIsNone(core.account_for({"url": "https://other.me/"}, accounts))
                self.assertIsNone(core.account_for({"url": "https://other.me/"}, {}))

    def test_find_login_form_collects_hidden_fields(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                form = core.find_login_form(LOGIN_HTML)
                self.assertEqual(form["action"], "takelogin.php")
                self.assertEqual(form["fields"], {"returnto": "index.php", "username": ""})
                self.assertNotIn("login", form["fields"])
                self.assertEqual(core.find_login_form("<html>no form</html>"),
                                 {"action": "", "fields": {}})

    def test_extract_activity_returns_time_and_snippet(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                found = core.extract_activity(DETAIL_HTML)
                self.assertEqual(found["last_seen"], "2026-10-16 08:12:03")
                self.assertIn("最近动向", found["snippet"])
                self.assertEqual(core.extract_activity("<html><body>nothing</body></html>"), {})

    def test_login_success_returns_new_cookie(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                def submit(session, url):
                    session.cookies.append(FakeCookie("c_secure_uid", "123"))
                    session.cookies.append(FakeCookie("c_secure_pass", "abcdef"))
                    return FakeResponse(url, HOME_HTML)

                session = FakeSession({"login.php": lambda s, u: FakeResponse(u, LOGIN_HTML),
                                       "takelogin.php": submit})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.login({"url": "https://audiences.me/"},
                                        {"username": "u", "password": "p"}, timeout=5)
                self.assertEqual(result.status, "success")
                self.assertIn("c_secure_uid=123", result.cookie)
                method, url, data = session.calls[-1]
                self.assertEqual(method, "POST")
                self.assertTrue(url.endswith("/takelogin.php"))
                self.assertEqual(data["username"], "u")
                self.assertEqual(data["password"], "p")
                self.assertEqual(data["returnto"], "index.php")

    def test_login_failure_reports_credentials(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({"login.php": lambda s, u: FakeResponse(u, LOGIN_HTML),
                                       "takelogin.php": lambda s, u: FakeResponse(u, FAIL_HTML)})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.login({"url": "https://audiences.me/"},
                                        {"username": "u", "password": "bad"}, timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("账号或密码", result.message)

    def test_login_respects_custom_field_and_extra_fields(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                html = ('<form action="/takelogin.php" method="post">'
                        '<input name="user"><input type="password" name="pass"></form>')
                def submit(session, url):
                    session.cookies.append(FakeCookie("c_secure_uid", "1"))
                    return FakeResponse(url, HOME_HTML)

                session = FakeSession({"login.php": lambda s, u: FakeResponse(u, html),
                                       "takelogin.php": submit})
                account = {"username": "u", "password": "p", "username_field": "user",
                           "password_field": "pass", "fields": {"2fa": "123456"}}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.login({"url": "https://audiences.me/"}, account, timeout=5)
                self.assertEqual(result.status, "success")
                data = session.calls[-1][2]
                self.assertEqual(data["user"], "u")
                self.assertEqual(data["pass"], "p")
                self.assertEqual(data["2fa"], "123456")

    def test_read_activity_uses_login_cookie(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({"index.php": lambda s, u: FakeResponse(u, INDEX_HTML),
                                       "userdetails.php?id=123": lambda s, u: FakeResponse(u, DETAIL_HTML)})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    # c_secure_uid 是 base64(123)，和首页链接指向同一个用户
                    result = core.read_activity({"url": "https://audiences.me/"},
                                                "c_secure_uid=MTIz", timeout=5)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.last_seen, "2026-10-16 08:12:03")
                self.assertTrue(session.calls[0][1].endswith("/index.php"))
                self.assertIn("userdetails.php?id=123", session.calls[1][1])

    def test_read_activity_detects_expired_login(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({"index.php": lambda s, u: FakeResponse(u, LOGIN_REQUIRED_HTML)})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://audiences.me/"}, "c_secure_uid=1", timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("登录态无效", result.message)

    def test_read_activity_skips_cloudflare_challenge_page(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                challenge = ("<html><head><title>Just a moment...</title></head><body>"
                             "<script>window._cf_chl_opt={};</script>"
                             "<div>最近动向：2020-01-01 00:00:00</div></body></html>")
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(u, INDEX_HTML),
                    "attendance.php": lambda s, u: FakeResponse(u, challenge),
                    "userdetails.php?id=123": lambda s, u: FakeResponse(u, DETAIL_HTML),
                })
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://audiences.me/"}, "c_secure_uid=MTIz",
                                                timeout=5, paths=["/attendance.php"])
                self.assertEqual(result.status, "success")
                # 挑战页里的「最近动向」是假的，必须跳过并换下一个候选页
                self.assertIn("userdetails.php?id=123", result.source)
                self.assertEqual(result.last_seen, "2026-10-16 08:12:03")

    def test_read_activity_reports_cloudflare_on_index(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({"index.php": lambda s, u: FakeResponse(
                    u, "<html><head><title>Just a moment...</title></head>"
                       "<script>window._cf_chl_opt={};</script></html>")})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://audiences.me/"},
                                                "c_secure_uid=1", timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("Cloudflare", result.message)

    def test_read_activity_reports_each_candidate(self):
        # 读不到时把每个候选页的真实情况写进提示，方便按站点配 activity_path
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(u, "<html><body>首页 公告</body></html>"),
                    "userdetails.php": lambda s, u: FakeResponse(
                        u, '<html><body>用户登录<form action="takelogin.php">'
                           '<input type="password" name="password"></form></body></html>'),
                })
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://pt.example/"},
                                                "c_secure_pass=x", timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("userdetails.php：被要求登录", result.message)
                self.assertIn("Cookie 里没解析到 uid", result.message)
                self.assertIn("index.php", result.message)

    def test_read_activity_prefers_cookie_uid_over_index_links(self):
        # 首页里的 userdetails.php?id= 链接可能指向别人（论坛作者 / 最新注册用户），
        # 必须优先用 Cookie 里的 c_secure_uid 打开「自己」的资料页
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(
                        u, '<html><body><a href="userdetails.php?id=999">别人</a>'
                           '<a href="torrents.php">种子</a></body></html>'),
                    "userdetails.php?id=12345": lambda s, u: FakeResponse(u, DETAIL_HTML),
                })
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://open.cd/"},
                                                "c_secure_uid=MTIzNDU%3D; c_secure_pass=x", timeout=5)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.last_seen, "2026-10-16 08:12:03")
                self.assertIn("userdetails.php?id=12345", result.source)
                self.assertNotIn("id=999", result.source)

    def test_read_activity_falls_back_to_cookie_uid(self):
        # 自定义主题的站点首页不挂 userdetails 链接时，用 Cookie 里的 c_secure_uid 兜底
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(u, "<html><body>首页 没有用户页链接</body></html>"),
                    "userdetails.php?id=12345": lambda s, u: FakeResponse(u, DETAIL_HTML),
                })
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://queen.example/"},
                                                "c_secure_uid=MTIzNDU%3D; c_secure_pass=x", timeout=5)
                self.assertEqual(result.status, "success")
                self.assertEqual(result.last_seen, "2026-10-16 08:12:03")
                self.assertIn("userdetails.php?id=12345", result.source)

    def test_read_activity_ignores_bare_dates(self):
        # 首页只有种子列表里的裸日期时不能当成「最近动向」，如实报未找到
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({"index.php": lambda s, u: FakeResponse(
                    u, "<html><body>种子 2026-09-18 某电影 2026-09-17 另一部</body></html>")})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://queen.example/"}, "a=1", timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("最近动向", result.message)

    def test_http_sign_auto_posts_home_signin_form(self):
        # 首页「每日签到」表单：按表单 action + method 提交，并带上按钮字段
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(
                        u, '<html><body><form method="post" action="/signin.php">'
                           '<input type="submit" name="signin" value="每日签到"></form></body></html>'),
                    "attendance.php": lambda s, u: FakeResponse(u, "not found", status_code=404),
                    "signin.php": lambda s, u: FakeResponse(u, "<html><body>签到成功！</body></html>"),
                })
                rule = core.resolve_rule({"url": "https://pt.example/"}, {})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign_auto({"url": "https://pt.example/", "cookie": "c=1"}, rule)
                self.assertEqual(result.status, "success")
                self.assertIn("签到入口：/signin.php", result.message)
                posted = [call for call in session.calls if call[0] == "POST"]
                self.assertTrue(posted, session.calls)
                self.assertIn("signin.php", posted[0][1])
                self.assertEqual(posted[0][2], {"signin": "每日签到"})

    def test_http_sign_auto_discovers_signin_path(self):
        # 站点没有 /attendance.php（学校那种 404）时，从头版「签到」链接自动发现入口
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(
                        u, '<html><body><a href="/signin.php">签到</a></body></html>'),
                    "attendance.php": lambda s, u: FakeResponse(u, "not found", status_code=404),
                    "signin.php": lambda s, u: FakeResponse(u, "<html><body>签到成功！</body></html>"),
                })
                rule = core.resolve_rule({"url": "https://pt.example/"}, {})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign_auto({"url": "https://pt.example/", "cookie": "c=1"}, rule)
                self.assertEqual(result.status, "success")
                self.assertIn("签到入口：/signin.php", result.message)

    def test_http_sign_auto_reports_tried_paths(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(u, "<html><body>没有签到链接</body></html>"),
                    "attendance.php": lambda s, u: FakeResponse(u, "not found", status_code=404),
                    "signin.php": lambda s, u: FakeResponse(u, "not found", status_code=404),
                })
                rule = core.resolve_rule({"url": "https://pt.example/"}, {})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}), \
                        mock.patch.object(core, "FALLBACK_SIGNIN_PATHS", ("/signin.php",)):
                    result = core.http_sign_auto({"url": "https://pt.example/", "cookie": "c=1"}, rule)
                self.assertEqual(result.status, "failed")
                self.assertIn("签到入口", result.message)
                self.assertIn("/attendance.php", result.message)
                self.assertIn("/signin.php", result.message)

    def test_read_activity_requires_cookie(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                result = core.read_activity({"url": "https://audiences.me/"}, "", timeout=5)
                self.assertEqual(result.status, "failed")
                self.assertIn("Cookie", result.message)

    def test_extract_activity_keeps_site_relative_time(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                html = ("<html><body><div>最近动向：2026-10-16 23:44:21 ( < 1分前 )</div>"
                        "<div>上次签到：2026-10-15 09:00:00</div></body></html>")
                found = core.extract_activity(html)
                self.assertEqual(found["last_seen"], "2026-10-16 23:44:21 ( < 1分前 )")
                full_width = core.extract_activity("<html><body>最近動向：2026-10-16 23:44:21（3 小时前）</body></html>")
                self.assertEqual(full_width["last_seen"], "2026-10-16 23:44:21 （3 小时前）")
                self.assertEqual(core.relative_suffix("2026-10-16 23:44:21", 19), "")

    def test_account_from_site_reads_note_and_record_fields(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                v2_note = json.dumps({"username": "u", "password": "p",
                                      "activity_path": "/userdetails.php?id=9"})
                account = core.account_from_site({"url": "https://audiences.me/",
                                                  "cookie": "c=1", "note": v2_note})
                self.assertTrue(core.account_ready(account))
                self.assertEqual(account["username"], "u")
                self.assertEqual(account["password"], "p")
                self.assertEqual(account["activity_path"], "/userdetails.php?id=9")
                v3_account = core.account_from_site({"note": {"username": "u2", "password": "p2"}})
                self.assertEqual(v3_account["password"], "p2")
                self.assertTrue(core.account_ready(core.account_from_site(
                    {"username": "u", "password": "p"})))
                # 只有 Cookie / 只有用户名都不算可登录凭证
                self.assertFalse(core.account_ready(core.account_from_site(
                    {"url": "https://audiences.me/", "cookie": "c=1"})))
                self.assertFalse(core.account_ready(core.account_from_site({"username": "u"})))
                self.assertFalse(core.account_ready(core.account_from_site({"note": "纯文本备注"})))

    def test_merge_accounts_prefers_configured_one(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                merged = core.merge_accounts(
                    {"username": "site", "password": "site-pw", "activity_path": "/a"},
                    {"username": "cfg", "password": "cfg-pw"}, None)
                self.assertEqual(merged["username"], "cfg")
                self.assertEqual(merged["password"], "cfg-pw")
                self.assertEqual(merged["activity_path"], "/a")
                self.assertEqual(core.merge_accounts(None, {}), {})

    def test_read_activity_uses_extra_paths(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                attendance = ("<html><body>最近动向：2026-10-16 23:44:21 ( < 1分前 )"
                              "</body></html>")
                session = FakeSession({"index.php": lambda s, u: FakeResponse(u, INDEX_HTML),
                                       "/attendance.php": lambda s, u: FakeResponse(u, attendance)})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.read_activity({"url": "https://audiences.me/"}, "c_secure_uid=MTIz",
                                                timeout=5, paths=["/attendance.php"])
                self.assertEqual(result.status, "success")
                self.assertEqual(result.last_seen, "2026-10-16 23:44:21 ( < 1分前 )")
                self.assertIn("/attendance.php", session.calls[1][1])
    def test_cookie_header_joins_pairs(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                session = FakeSession({}, cookies=[("a", "1"), ("b", "2")])
                self.assertEqual(core.cookie_header(session), "a=1; b=2")


if __name__ == "__main__":
    unittest.main()
