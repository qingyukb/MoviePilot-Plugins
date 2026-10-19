"""core.py「站点登录 + 最近动向」回归测试：不依赖 MoviePilot 与真实网络。"""

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

    def test_http_sign_auto_posts_form_then_confirms(self):
        # 签到页只有表单按钮（GET 判定不出结果）→ 按表单 POST → 回读签到页看到已签到
        for core in CORES:
            with self.subTest(core=core.__name__):
                state = {"page": 0}
                form_page = ('<html><body>每日签到'
                             '<form action="/attendance.php" method="post">'
                             '<input type="submit" name="signin" value="签到"></form></body></html>')
                signed_page = "<html><body>今日已签到，已连续签到 12 天</body></html>"

                def handler(session, url):
                    method = session.calls[-1][0]
                    if method == "POST":
                        return FakeResponse(url, "<html><body>提交成功</body></html>")
                    state["page"] += 1
                    # 前两次 GET 都是「签到前的页面」（判定 + 取表单），之后回读才看到已签到
                    return FakeResponse(url, form_page if state["page"] <= 2 else signed_page)

                session = FakeSession({"attendance.php": handler})
                rule = core.resolve_rule({"url": "https://pt.example/", "cookie": "c=1"}, {})
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign_auto({"url": "https://pt.example/", "cookie": "c=1"}, rule)
                self.assertEqual(result.status, "already", result.message)
                self.assertIn("回读签到页确认", result.message)
                posted = [call for call in session.calls if call[0] == "POST"]
                self.assertTrue(posted, session.calls)
                self.assertEqual(posted[0][2], {"signin": "签到"})
                self.assertEqual(posted[0][1], "https://pt.example/attendance.php")

    def test_http_two_step_confirms_on_first_page(self):
        # 学校：addbonus 返回的页面里「每日签到」还在（没判定出来），回读首页时入口已消失 → 已签到
        for core in CORES:
            with self.subTest(core=core.__name__):
                state = {"page": 0}
                with_entry = ('<html><body>首页 <a href="index.php?action=addbonus">每日签到</a>'
                              '</body></html>')
                # 签到页本身还是那句「每日签到」（拿不到结论），所以要看回读的首页
                signed_bonus = ('<html><body>首页 <a href="index.php?action=addbonus">每日签到</a>'
                                '</body></html>')
                without_entry = '<html><body>首页 论坛 魔力值 100</body></html>'

                def handler(session, url):
                    method = session.calls[-1][0]
                    if "action=addbonus" in url:
                        # 签到页本身没给结论（正文既没成功词也没入口）
                        return FakeResponse(url, signed_bonus)
                    state["page"] += 1
                    # 第一次读首页：入口在；之后回读：入口消失
                    return FakeResponse(url, with_entry if state["page"] == 1 else without_entry)

                session = FakeSession({"index.php": handler, "addbonus": handler})
                rule = {"engine": "http", "method": "GET", "path": "/index.php",
                        "absent_keywords": ["每日签到"], "absent_status": "already",
                        "signin_path": "/index.php?action=addbonus",
                        "signin_absent_status": "success"}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_sign_auto(
                        {"url": "https://pt.btschool.club/", "cookie": "c=1", "ua": ""}, rule)
                self.assertEqual(result.status, "already", result.message)
                self.assertIn("回读签到页确认", result.message)

    def test_random_choice_retries_other_options(self):
        # 彩虹岛：第一次答错（没有「魔力值」），换一个选项再提交才成功
        for core in CORES:
            with self.subTest(core=core.__name__):
                question = ('<html><body><form>'
                            '<input type="hidden" name="questionid" value="450">'
                            '<input type="checkbox" name="choice[]" value="8">'
                            '<input type="checkbox" name="choice[]" value="4">'
                            '</form></body></html>')
                attempts = {"post": 0}

                def handler(session, url):
                    method = session.calls[-1][0]
                    if method == "GET":
                        return FakeResponse(url, question)
                    attempts["post"] += 1
                    body = ("<html><body>回答错误</body></html>" if attempts["post"] == 1
                            else "<html><body>获得 60 点魔力值</body></html>")
                    return FakeResponse(url, body)

                session = FakeSession({"bakatest.php": handler})
                rule = {"engine": "http", "method": "GET", "path": "/bakatest.php",
                        "signin_path": "/bakatest.php", "signin_method": "POST",
                        "signin_pick": "random", "success": r"\d+\s*点魔力值"}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_two_step(
                        {"url": "https://ptchdbits.co/", "cookie": "c=1", "ua": ""}, rule)
                self.assertIsNotNone(result, session.calls)
                self.assertEqual(result.status, "success", result.message)
                self.assertGreaterEqual(attempts["post"], 2, session.calls)

    def test_http_two_step_picks_random_choice(self):
        # 52pt 式：答题签到，随机挑一个 choice[] 提交，返回「获得 N 点魔力值」即成功
        for core in CORES:
            with self.subTest(core=core.__name__):
                question = ('<html><body><form>'
                            '<input type="hidden" name="questionid" value="450">'
                            '<input type="checkbox" name="choice[]" value="8">'
                            '<input type="checkbox" name="choice[]" value="4">'
                            '</form></body></html>')
                session = FakeSession({
                    "bakatest.php": lambda s, u: FakeResponse(
                        u, question if s.calls[-1][0] == "GET"
                        else "<html><body>获得 20 点魔力值</body></html>"),
                })
                rule = {"engine": "http", "method": "GET", "path": "/bakatest.php",
                        "signin_path": "/bakatest.php", "signin_method": "POST",
                        "signin_pick": "random",
                        "signin_data": {"usercomment": "太难了！", "wantskip": "不会"},
                        "success": r"\d+\s*点魔力值"}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_two_step({"url": "https://52pt.site/", "cookie": "c=1", "ua": ""},
                                                rule, proxies=None)
                self.assertIsNotNone(result, session.calls)
                self.assertEqual(result.status, "success", result.message)
                posted = [call for call in session.calls if call[0] == "POST"][0][2]
                self.assertEqual(posted["questionid"], "450")
                self.assertIn(posted["choice[]"], {"8", "4"})
                self.assertEqual(posted["wantskip"], "不会")

    def test_http_two_step_posts_json_with_header(self):
        # 朱雀式：首页取 x-csrf-token，带 JSON body 和自定义头 POST
        for core in CORES:
            with self.subTest(core=core.__name__):
                home = ('<html><head><meta name="x-csrf-token" content="csrf123"></head>'
                        '<body>首页</body></html>')
                session = FakeSession({
                    "zhuque.in/": lambda s, u: FakeResponse(u, home),
                    "fireGenshinCharacterMagic": lambda s, u: FakeResponse(
                        u, '{"status":200,"data":{"code":"FIRE_GENSHIN_CHARACTER_MAGIC_SUCCESS","bonus":3}}'),
                })
                rule = {"engine": "http", "method": "GET", "path": "/",
                        "signin_path": "/api/gaming/fireGenshinCharacterMagic",
                        "signin_method": "POST",
                        "signin_token_patterns": {"csrf": r'<meta name="x-csrf-token" content="([^"]+)"'},
                        "signin_headers": {"x-csrf-token": "{csrf}"},
                        "signin_json_raw": '{"all": 1, "resetModal": "true"}',
                        "success_keywords": ['"status":200']}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_two_step({"url": "https://zhuque.in/", "cookie": "c=1", "ua": ""},
                                                rule, proxies=None)
                self.assertIsNotNone(result, session.calls)
                self.assertEqual(result.status, "success", result.message)
                self.assertEqual(session.headers.get("x-csrf-token"), "csrf123")
                posted = [call for call in session.calls if call[0] == "POST"][0][2]
                self.assertEqual(posted, b'{"all": 1, "resetModal": "true"}')

    def test_http_two_step_posts_captured_token(self):
        # TTG 式：首页 JS 里有 token，POST 到 /signed.php 才算签到
        for core in CORES:
            with self.subTest(core=core.__name__):
                home = ('<html><body>欢迎<script>signed_timestamp: "1700000000", '
                        'signed_token: "tok123"</script></body></html>')
                session = FakeSession({
                    "index.php": lambda s, u: FakeResponse(u, home),
                    "signed.php": lambda s, u: FakeResponse(u, "<html><body>您已连续签到 12 天</body></html>"),
                })
                rule = {"engine": "http", "method": "GET", "path": "/index.php",
                        "signin_path": "/signed.php", "signin_method": "POST",
                        "signin_token_patterns": {
                            "signed_timestamp": r"signed_timestamp:\s*\"(\d{10})\"",
                            "signed_token": r"signed_token:\s*\"([^\"]+)\"",
                        },
                        "success_keywords": ["您已连续签到"]}
                with mock.patch.dict(sys.modules, {"requests": fake_requests(session)}):
                    result = core.http_two_step({"url": "https://totheglory.im/", "cookie": "c=1", "ua": ""},
                                                rule, proxies=None)
                self.assertIsNotNone(result, session.calls)
                self.assertEqual(result.status, "success", result.message)
                posted = [call for call in session.calls if call[0] == "POST"]
                self.assertTrue(posted, session.calls)
                self.assertEqual(posted[0][2], {"signed_timestamp": "1700000000",
                                                "signed_token": "tok123"})

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
