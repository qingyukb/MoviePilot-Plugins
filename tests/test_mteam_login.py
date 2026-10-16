"""mteamlogin/core.py 回归测试：全程离线，用假的 HTTP 传输层驱动。

签名向量取自对 kp.m-team.cc 前端 bundle 的逆向与真实请求抓包
（POST&/api/login&1792147233564 的 _sgin 实测值）。

本轮重点：插件必须**真实认证并调用保活心跳** POST /api/member/updateLastBrowse，
然后才读 GET /api/member/profile 的 memberStatus.lastBrowse 作校验，
且永不触碰 revokeSession 等其它写接口。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIRS = (
    ROOT / "plugins.v2" / "mteamlogin",
    ROOT / "plugins.v3" / "mteamlogin",
)

# 抓包得到的真实签名向量
LOGIN_VECTOR = ("POST", "/api/login", 1792147233564, "vbx6OSsNwBNBzq+BrzI6WZ0Tlo0=")
UNIX_VECTOR = ("GET", "/api/system/unix", 1792147191401, "FyDVc1yB4xNZwd554jpZXRn+d5E=")
# 保活心跳接口：插件每一轮都必须调用它，否则「最近动向」永远不会刷新。
KEEPALIVE_PATH = "/member/updateLastBrowse"
# JWT 形状的令牌（三段、每段足够长），用来保证只展开出一种认证方案
JWT_TOKEN = "headerxx.payloadyy.signature"
# 插件绝对不该碰的其它写接口（/login 只在插件配置页手填了账号密码时才会调用）。
FORBIDDEN_PATHS = ("/member/revokeSession", "/member/logout",
                   "/member/updateLastBrowse2")


def load_core(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "core.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORES = [load_core(directory, f"mteam_core_{index}")
         for index, directory in enumerate(PLUGIN_DIRS)]


class FakeResponse:
    def __init__(self, status: int, headers: dict, body):
        self.status = status
        self.headers = dict(headers or {})
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    """替换 urllib 的 opener，既能录制真实请求，也免去网络依赖。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):  # noqa: ARG002 - 与 urllib 签名保持一致
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"没有预置响应，却收到请求：{request.full_url}")
        status, headers, body = self.responses.pop(0)
        return FakeResponse(status, headers, body)


class RawOpener:
    """直接返回预置响应对象（可以是非 JSON）。"""

    def __init__(self, responses):
        self.responses = list(responses)

    def open(self, request, timeout=None):  # noqa: ARG002 - 与 urllib 签名保持一致
        return self.responses.pop(0)


def build_client(core, responses):
    client = core.MTeamClient(host=core.DEFAULT_API_HOST, timeout=10)
    opener = FakeOpener(responses)
    client._opener = opener
    return client, opener


def recorded(opener):
    """把录制到的请求整理成便于断言的字典列表。"""
    items = []
    for request in opener.requests:
        parsed = urllib.parse.urlsplit(request.full_url)
        headers = {key.lower(): value for key, value in request.header_items()}
        items.append({
            "method": request.get_method(),
            "path": parsed.path,
            "query": dict(urllib.parse.parse_qsl(parsed.query)),
            "headers": headers,
            "body": (request.data or b"").decode("utf-8"),
        })
    return items


def multipart_values(body: str) -> dict:
    """从 multipart 请求体里取出字段名 -> 值。"""
    values = {}
    parts = body.split("\r\n--")
    for part in parts:
        if "Content-Disposition:" not in part:
            continue
        head, _, tail = part.partition("\r\n\r\n")
        name = head.split('name="', 1)[1].split('"', 1)[0]
        values[name] = tail.rstrip("-\r\n")
    return values


def login_ok(token=JWT_TOKEN):
    """真实登录成功的响应：authorization 只在**响应头**返回（前端存进 localStorage.auth）。"""
    return 200, {"authorization": token}, {
        "code": 0, "message": "success",
        "data": {"member": {"id": 12345, "username": "tester"}}}


def login_needs_otp():
    return 200, {}, {"code": 1001, "message": "需要二次验证", "data": ["otp"]}


def keepalive_ok():
    """保活心跳成功的真实响应形状。"""
    return 200, {}, {"code": 0, "message": "success", "data": None}


def keepalive_denied(message="Full authentication is required to access this resource",
                     code=401):
    return 200, {}, {"code": code, "message": message, "data": None}


def profile_ok(last_browse, **member):
    """最近动向是刚被保活刷新的时间。"""
    data = {"id": 12345, "username": "tester",
            "memberStatus": {"lastBrowse": last_browse}}
    data.update(member)
    return 200, {}, {"code": 0, "message": "ok", "data": data}


class SignatureTests(unittest.TestCase):
    def test_signature_matches_captured_vectors(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                for method, path, ts, expected in (LOGIN_VECTOR, UNIX_VECTOR):
                    self.assertEqual(core.sign(method, path, ts), expected)

    def test_auth_params_use_site_typo_sgin(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                params = core.auth_params("POST", "/api/login", 1792147233564)
                self.assertEqual(set(params), {"_timestamp", "_sgin"})
                self.assertEqual(params["_timestamp"], "1792147233564")
                self.assertEqual(params["_sgin"], LOGIN_VECTOR[3])


class CredentialsTests(unittest.TestCase):
    def test_credentials_from_config_reads_manual_fields_basic(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                creds = core.credentials_from_config({
                    "username": "tester", "password": "secret",
                    "token": "TK", "api_key": "AK",
                    "cookie": "cf_clearance=x; authorization=COOKIETOK"})
                self.assertEqual(creds.username, "tester")
                self.assertEqual(creds.password, "secret")
                self.assertEqual(creds.token, "TK")
                self.assertEqual(creds.api_key, "AK")
                self.assertEqual(creds.cookie, "cf_clearance=x; authorization=COOKIETOK")
                self.assertIn("authorization", creds.source)
                self.assertIn("API Key", creds.source)
                self.assertTrue(creds.usable)
                self.assertTrue(creds.can_login)
                self.assertEqual(core.credentials_from_config(None).usable, False)
                self.assertEqual(core.credentials_from_config({}).usable, False)

    def test_extract_token_from_cookie_handles_common_shapes(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(
                    core.extract_token_from_cookie("a=1; authorization=AAA.BBB.CCC"), "AAA.BBB.CCC")
                self.assertEqual(
                    core.extract_token_from_cookie("token=shorthand; x=2"), "shorthand")
                self.assertEqual(core.extract_token_from_cookie("a=1;b=2"), "")
                self.assertEqual(core.extract_token_from_cookie(""), "")
                self.assertEqual(core.extract_token_from_cookie(None), "")

    def test_auth_plans_are_single_credential_and_ordered(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                creds = core.Credentials(token="TK", api_key="AK", cookie="authorization=CK")
                plans = creds.auth_plans()
                self.assertTrue(plans)
                # 每个方案只带一种认证方式，避免把互斥的头部混在一起发出去
                for plan in plans:
                    used = [bool(plan.token), bool(plan.api_key), bool(plan.cookie)]
                    self.assertEqual(sum(used), 1, plan)
                # Token 优先，其次 Cookie 里的令牌，再是 API Key、Cookie
                self.assertEqual(plans[0].token, "TK")
                self.assertIn("CK", [plan.token for plan in plans])
                self.assertIn("AK", [plan.api_key for plan in plans])
                self.assertEqual(core.Credentials().auth_plans(), [])

class RequestShapeTests(unittest.TestCase):
    def test_keepalive_is_signed_multipart_post(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [keepalive_ok()])
                client.keepalive(plan=core.AuthPlan(name="t", token="TOKEN"),
                                 ts_ms=1792147233564)
                request = recorded(opener)[0]
                self.assertEqual(request["method"], "POST")
                self.assertEqual(request["path"], "/api/member/updateLastBrowse")
                self.assertIn("multipart/form-data; boundary=",
                              request["headers"]["content-type"])
                fields = multipart_values(request["body"])
                self.assertEqual(fields["_timestamp"], "1792147233564")
                self.assertEqual(fields["_sgin"],
                                 core.sign("POST", "/api/member/updateLastBrowse",
                                           1792147233564))
                self.assertEqual(request["headers"]["authorization"], "TOKEN")

    def test_keepalive_refuses_api_key_plan(self):
        """API Key 调用不算活跃：保活接口直接拒绝只读凭证，连请求都不发。"""
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [])
                with self.assertRaises(core.MTeamError):
                    client.keepalive(plan=core.AuthPlan(name="k", api_key="AK"))
                self.assertEqual(opener.requests, [])

    def test_login_is_signed_multipart_post_with_account(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [login_ok("NEWTOKEN"),
                                                     keepalive_ok(),
                                                     profile_ok(1792147191401)])
                creds = core.Credentials(username="tester", password="secret",
                                         otp_secret="JBSWY3DPEHPK3PXP")
                report = client.fetch_last_browse(credentials=creds)
                self.assertTrue(report.ok, report.message)
                self.assertTrue(report.login_used)
                self.assertTrue(report.login_ok)
                requests = recorded(opener)
                self.assertEqual([item["path"] for item in requests],
                                 ["/api/login", "/api/member/updateLastBrowse",
                                  "/api/member/profile"])
                login = requests[0]
                self.assertEqual(login["method"], "POST")
                self.assertIn("multipart/form-data; boundary=",
                              login["headers"]["content-type"])
                fields = multipart_values(login["body"])
                self.assertEqual(fields["username"], "tester")
                self.assertEqual(fields["password"], "secret")
                self.assertEqual(fields["_sgin"],
                                 core.sign("POST", "/api/login", int(fields["_timestamp"])))
                # 登录换到的 authorization 必须原样用在保活心跳上
                self.assertEqual(requests[1]["headers"]["authorization"], "NEWTOKEN")
                self.assertNotIn("x-api-key", requests[1]["headers"])
                self.assertIn("真实登录", report.message)

    def test_profile_request_signs_query_and_sends_authorization(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [profile_ok(1792147191401)])
                client.last_browse(plan=core.AuthPlan(name="t", token="TOKEN"),
                                   ts_ms=1792147191401)
                request = recorded(opener)[0]
                self.assertEqual(request["method"], "GET")
                self.assertEqual(request["path"], "/api/member/profile")
                self.assertEqual(request["query"]["_timestamp"], "1792147191401")
                self.assertEqual(request["query"]["_sgin"],
                                 core.sign("GET", "/api/member/profile", 1792147191401))
                self.assertEqual(request["headers"]["authorization"], "TOKEN")
                self.assertFalse(request["body"])

    def test_headers_carry_client_metadata(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [keepalive_ok()])
                client.keepalive(plan=core.AuthPlan(name="t", token="TOKEN"),
                                 ts_ms=1792147233564)
                headers = recorded(opener)[0]["headers"]
                self.assertEqual(headers["ts"], "1792147233")
                self.assertEqual(headers["version"], core.CLIENT_VERSION)
                self.assertEqual(headers["webversion"], core.WEB_VERSION)
                self.assertIn("Mozilla/5.0", headers["user-agent"])
                self.assertEqual(headers["origin"], "https://kp.m-team.cc")


class KeepAliveFlowTests(unittest.TestCase):
    def test_fetch_last_browse_keeps_alive_then_reads_recent_time(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [keepalive_ok(),
                                                     profile_ok(1792147191401)])
                creds = core.Credentials(token="TOKEN")
                report = client.fetch_last_browse(credentials=creds, site_label="m-team.cc")
                self.assertTrue(report.ok, report.message)
                self.assertTrue(report.keepalive_ok)
                self.assertEqual(report.keepalive_code, 0)
                self.assertEqual(report.last_browse.raw, 1792147191401)
                self.assertEqual(report.last_browse.text, "2026-10-16 18:39:51")
                self.assertEqual(report.site, "m-team.cc")
                # 先保活、后读最近动向
                self.assertEqual([item["path"] for item in recorded(opener)],
                                 ["/api/member/updateLastBrowse", "/api/member/profile"])

    def test_flow_never_touches_other_write_endpoints(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [keepalive_ok(),
                                                     profile_ok(1792147191401)])
                client.fetch_last_browse(credentials=core.Credentials(token="TOKEN"))
                paths = [item["path"] for item in recorded(opener)]
                for path in FORBIDDEN_PATHS:
                    self.assertNotIn("/api" + path, paths)
                self.assertEqual(paths.count("/api" + KEEPALIVE_PATH), 1)

    def test_keepalive_failure_is_reported_with_plugin_config_hint(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [keepalive_denied(),
                                                      profile_ok(1600000000000)])
                # 用 JWT 形状的令牌，保证只展开出一种认证方案
                report = client.fetch_last_browse(
                    credentials=core.Credentials(token=JWT_TOKEN), site_label="m-team.cc")
                self.assertFalse(report.ok)
                self.assertFalse(report.keepalive_ok)
                self.assertIn("保活", report.message)
                self.assertIn("插件配置", report.message)
                # 账号真实的最近动向要如实展示出来（而不是假装成功）
                self.assertEqual(report.last_browse.raw, 1600000000000)

    def test_api_key_only_reads_and_never_keeps_alive(self):
        """Token 被拒 + 有 API Key：保活必须失败，但要用 API Key 读出真实的最近动向。"""
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [keepalive_denied(),
                                                     profile_ok(1600000000000)])
                creds = core.Credentials(token=JWT_TOKEN, api_key="AK")
                report = client.fetch_last_browse(credentials=creds)
                self.assertFalse(report.ok)
                self.assertFalse(report.keepalive_ok)
                # 站点里真实的（很旧的）最近动向必须被读出来，而不是留空
                self.assertEqual(report.last_browse.raw, 1600000000000)
                requests = recorded(opener)
                # 保活只发了一次（用 Token），API Key 只用于只读 profile
                self.assertEqual([item["path"] for item in requests],
                                 ["/api" + KEEPALIVE_PATH, "/api/member/profile"])
                self.assertEqual(requests[1]["headers"]["x-api-key"], "AK")
                self.assertEqual(requests[0]["headers"]["authorization"], JWT_TOKEN)
                self.assertIn("API Key", report.message)

    def test_expired_configured_token_falls_back_to_real_login(self):
        """配置里的 Token 过期且同时填了账号密码时，直接真实登录换新令牌再保活。"""
        for core in CORES:
            with self.subTest(core=core.__name__):
                expired = ("eyJhbGciOiJIUzI1NiJ9."
                           "eyJleHAiOjE2MDAwMDAwMDB9."
                           "c2lnbmF0dXJl")
                self.assertTrue(core.jwt_expired(expired))
                client, opener = build_client(core, [login_ok("NEWTOKEN"), keepalive_ok(),
                                                     profile_ok(1792147191401)])
                creds = core.Credentials(token=expired, username="tester",
                                         password="secret")
                report = client.fetch_last_browse(credentials=creds)
                self.assertTrue(report.ok, report.message)
                self.assertTrue(report.login_used)
                # 过期的 Token 不再白跑一次保活请求
                self.assertEqual([item["path"] for item in recorded(opener)],
                                 ["/api/login", "/api/member/updateLastBrowse",
                                  "/api/member/profile"])

    def test_login_needing_otp_is_reported(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [login_needs_otp()])
                creds = core.Credentials(username="tester", password="secret")
                report = client.fetch_last_browse(credentials=creds)
                self.assertFalse(report.ok)
                self.assertTrue(report.login_used)
                self.assertFalse(report.login_ok)
                self.assertIn("二步验证", report.message)

    def test_stale_last_browse_after_keepalive_is_flagged(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                # 保活接口说成功，但最近动向还是老的（站点缓存 / 只读令牌）
                client, _opener = build_client(core, [keepalive_ok(),
                                                      profile_ok(1600000000000)])
                report = client.fetch_last_browse(credentials=core.Credentials(token="TOKEN"))
                self.assertTrue(report.ok, report.message)
                self.assertTrue(report.keepalive_ok)
                self.assertIn("最近动向", report.message)
                self.assertRegex(report.message, r"缓存|只读|权限")

    def test_profile_method_not_supported_falls_back_to_post(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                denied = 405, {}, {"timestamp": "2026-10-16T19:16:29", "status": 405,
                                   "error": "Method Not Allowed", "path": "/api/member/profile",
                                   "message": "Request method 'GET' is not supported"}
                client, opener = build_client(core, [keepalive_ok(), denied,
                                                     profile_ok(1792147191401)])
                report = client.fetch_last_browse(credentials=core.Credentials(token="TOKEN"))
                self.assertTrue(report.ok, report.message)
                methods = [item["method"] for item in recorded(opener)]
                self.assertEqual(methods, ["POST", "GET", "POST"])

    def test_missing_credentials_raise(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [])
                with self.assertRaises(core.MTeamError):
                    client.keepalive(plan=core.AuthPlan())

    def test_missing_credentials_report_is_not_ok(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [])
                report = client.fetch_last_browse(credentials=core.Credentials())
                self.assertFalse(report.ok)
                self.assertIn("插件配置", report.message)

    def test_report_carries_endpoint_and_api_ok(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [keepalive_ok(),
                                                      profile_ok(1792147191401)])
                report = client.fetch_last_browse(credentials=core.Credentials(token="TOKEN"))
                self.assertTrue(report.api_ok)
                self.assertIn(core.DEFAULT_API_HOST + KEEPALIVE_PATH, report.endpoint)

    def test_non_json_backend_is_marked_not_api_ok(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client = core.MTeamClient(host=core.DEFAULT_API_HOST, timeout=10)
                # 直接给一个 HTML 403 响应，模拟打到了 nginx 前端
                client._opener = RawOpener([FakeHtmlResponse(), FakeHtmlResponse()])
                report = client.fetch_last_browse(
                    credentials=core.Credentials(token=JWT_TOKEN))
                self.assertFalse(report.api_ok)
                self.assertFalse(report.ok)


class FakeHtmlResponse:
    """非 JSON 响应（例如 kp.m-team.cc 的 403 HTML），用来验证接口命中判定。"""

    status = 403

    class _Headers(dict):
        pass

    headers = _Headers({"content-type": "text/html"})

    def read(self) -> bytes:
        return b"<html><head><title>403 Forbidden</title></head></html>"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

class PluginSourceTests(unittest.TestCase):
    """配置表单必须手填凭证，且插件不再读取 MoviePilot「站点管理」。"""

    CREDENTIAL_MODELS = ("username", "password", "otp_secret", "otp_code",
                         "email_code", "tg_code", "token", "api_key", "cookie")

    def test_form_has_all_manual_credential_fields(self):
        for directory in PLUGIN_DIRS:
            with self.subTest(directory=directory.parent.name):
                source = (directory / "__init__.py").read_text(encoding="utf-8")
                form = source.split("def get_form", 1)[1].split("def get_page", 1)[0]
                for model in self.CREDENTIAL_MODELS:
                    self.assertIn('"model": "' + model + '"', form,
                                  f"表单必须提供 {model} 手填项")
                for kept in ("enabled", "proxy", "run_once", "notify_mode",
                             "cron", "api_host", "timeout"):
                    self.assertIn('"model": "' + kept + '"', form)
                # 站点选择已经从配置页删除
                self.assertNotIn('"model": "site"', form)

    def test_plugin_reads_credentials_from_plugin_config(self):
        for directory in PLUGIN_DIRS:
            with self.subTest(directory=directory.parent.name):
                source = (directory / "__init__.py").read_text(encoding="utf-8")
                # 凭证只在插件配置页手填，不再从站点操作层读取
                self.assertIn("credentials_from_config", source)
                self.assertIn('config.get("username")', source)
                self.assertNotIn("SiteOper", source)
                self.assertNotIn("credentials_from_site", source)
                self.assertNotIn("pick_mteam_site", source)
                # 前后端分离：多候选自动回退
                self.assertIn("api_host_candidates", source)
                self.assertIn("api_ok", source)

    def test_plugin_surfaces_keepalive_status(self):
        for directory in PLUGIN_DIRS:
            with self.subTest(directory=directory.parent.name):
                source = (directory / "__init__.py").read_text(encoding="utf-8")
                self.assertIn("keepalive_ok", source)
                self.assertIn("keepalive_code", source)
                self.assertIn("保活", source)


class HelperTests(unittest.TestCase):
    def test_format_last_browse_handles_ms_and_strings(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.format_last_browse(None).text, "")
                self.assertEqual(core.format_last_browse(1792147191401).text,
                                 "2026-10-16 18:39:51")
                self.assertEqual(core.format_last_browse(1792147191).text,
                                 "2026-10-16 18:39:51")
                self.assertEqual(core.format_last_browse("2026-10-16 18:39:51").text,
                                 "2026-10-16 18:39:51")
                self.assertEqual(core.format_last_browse("").text, "")
                self.assertEqual(core.format_last_browse("not-a-time").text, "not-a-time")
                self.assertTrue(core.format_last_browse(None).empty)
                self.assertFalse(core.format_last_browse(1792147191401).empty)

    def test_extract_last_browse_prefers_member_status(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                payload = {"data": {"memberStatus": {"lastBrowse": 42}}}
                self.assertEqual(core.extract_last_browse(payload), 42)
                self.assertIsNone(core.extract_last_browse({"data": {}}))
                self.assertIsNone(core.extract_last_browse(None))
                self.assertIsNone(core.extract_last_browse({"data": "nope"}))

    def test_humanize_relative_time(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                now = 1792147191401
                self.assertEqual(core.humanize(None), "")
                self.assertEqual(core.humanize(now - 30_000, now), "刚刚")
                self.assertEqual(core.humanize(now - 90_000, now), "1 分钟前")
                self.assertEqual(core.humanize(now - 3 * 3600_000, now), "3 小时前")
                self.assertEqual(core.humanize(now - 2 * 86400_000, now), "2 天前")
                self.assertEqual(core.humanize(now + 60_000, now), "1 分钟后")

    def test_mask_helpers_never_leak_secrets(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.mask(""), "")
                self.assertEqual(core.mask("abc"), "***")
                self.assertEqual(core.mask("abcdefghij"), "abcd***ghij")
                self.assertEqual(core.mask_account(""), "")
                self.assertEqual(core.mask_account("ab"), "a*")
                self.assertEqual(core.mask_account("tester"), "t***r")

    def test_api_host_derivation_and_candidates(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.derive_api_host("https://kp.m-team.cc/"),
                                 "https://api.m-team.cc/api")
                self.assertEqual(core.derive_api_host("m-team.cc"),
                                 "https://api.m-team.cc/api")
                self.assertEqual(core.derive_api_host("https://kp.m-team.io/"),
                                 "https://api.m-team.io/api")
                self.assertEqual(core.derive_api_host("api.m-team.cc"),
                                 "https://api.m-team.cc/api")
                self.assertEqual(core.derive_api_host(""), core.DEFAULT_API_HOST)
                self.assertEqual(core.derive_api_host(None), core.DEFAULT_API_HOST)
                hosts = core.api_host_candidates("https://api.m-team.io/api")
                self.assertEqual(hosts[0], "https://api.m-team.io/api")
                self.assertIn(core.DEFAULT_API_HOST, hosts)
                self.assertEqual(len(hosts), len(set(hosts)))
                self.assertEqual(core.api_host_candidates(None)[0], core.DEFAULT_API_HOST)

    def test_credentials_from_config_reads_manual_fields(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                creds = core.credentials_from_config({
                    "username": "tester", "password": "secret",
                    "otp_secret": "JBSWY3DPEHPK3PXP", "otp_code": "123456",
                    "email_code": "mail-code", "tg_code": "tg-code",
                    "token": "jwt-token", "api_key": "api-key", "cookie": "a=b",
                })
                self.assertEqual(creds.username, "tester")
                self.assertEqual(creds.password, "secret")
                self.assertEqual(creds.otp_secret, "JBSWY3DPEHPK3PXP")
                self.assertEqual(creds.otp_code, "123456")
                self.assertEqual(creds.email_code, "mail-code")
                self.assertEqual(creds.tg_code, "tg-code")
                self.assertEqual(creds.token, "jwt-token")
                self.assertEqual(creds.api_key, "api-key")
                self.assertEqual(creds.cookie, "a=b")
                self.assertTrue(creds.usable)
                # 兼容配置页可能使用的别名
                alias = core.credentials_from_config({
                    "authorization": "alias-token", "apikey": "alias-key",
                    "otpsecret": "ALIAS", "otpcode": "654321",
                    "emailcode": "alias-mail", "tgcode": "alias-tg"})
                self.assertEqual(alias.token, "alias-token")
                self.assertEqual(alias.api_key, "alias-key")
                self.assertEqual(alias.otp_secret, "ALIAS")
                self.assertEqual(alias.otp_code, "654321")
                self.assertEqual(alias.email_code, "alias-mail")
                self.assertEqual(alias.tg_code, "alias-tg")
                # 空配置不能伪装成可用凭证
                self.assertFalse(core.credentials_from_config({}).usable)
                self.assertFalse(core.credentials_from_config(None).usable)

    def test_looks_like_api_response(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertTrue(core.looks_like_api_response({"code": 1}))
                self.assertFalse(core.looks_like_api_response({"error": "x"}))
                self.assertFalse(core.looks_like_api_response({}))
                # 原始响应体：真 JSON 才算命中，HTML（如 403）不算
                self.assertTrue(core.looks_like_api_response(b'{"code":1,"message":"bogus"}'))
                self.assertFalse(
                    core.looks_like_api_response(b"<html><title>403 Forbidden</title></html>"))
                self.assertFalse(core.looks_like_api_response(b""))

    def test_hint_for_translates_site_errors(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertIn("插件配置", core.hint_for("key\u7121\u6548"))
                self.assertIn("凭证", core.hint_for("\u7121\u6548\u7684\u6191\u8b49"))
                self.assertIn("api.", core.hint_for("Request method 'GET' is not supported"))
                self.assertEqual(core.hint_for("something else"), "")


if __name__ == "__main__":
    unittest.main()