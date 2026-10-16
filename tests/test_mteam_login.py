"""mteamlogin/core.py 回归测试：全程离线，用假的 HTTP 传输层驱动。

签名向量取自对 kp.m-team.cc 前端 bundle 的逆向与真实请求抓包
（POST&/api/login&1792147233564 的 _sgin 实测值）。
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
OTP_SECRET = "JBSWY3DPEHPK3PXP"
# /member/updateLastBrowse 会把「最近动向」刷成当前时间，插件必须永远不碰它。
FORBIDDEN_PATH = "/member/updateLastBrowse"


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


def multipart_fields(body: str):
    """从 multipart 请求体里取出 (字段名, 值) 列表。"""
    fields = []
    for part in body.split("\r\n"):
        if part.startswith("Content-Disposition:"):
            name = part.split('name="', 1)[1].split('"', 1)[0]
            fields.append(name)
    return fields


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


def login_ok(token="TOKEN", **data):
    payload = {"id": 12345, "username": "tester"}
    payload.update(data)
    return 200, {"authorization": token, "did": "did-1"}, {"code": 0, "message": "ok",
                                                          "data": payload}


def profile_ok(last_browse):
    return 200, {}, {"code": 0, "message": "ok",
                     "data": {"id": 12345, "memberStatus": {"lastBrowse": last_browse}}}


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

    def test_totp_matches_rfc6238_vector(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                # RFC 6238 附录 B：secret = "12345678901234567890"
                secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
                self.assertEqual(core.totp(secret, at=59, digits=8), "94287082")
                self.assertEqual(len(core.totp(OTP_SECRET, at=59)), 6)


class RequestShapeTests(unittest.TestCase):
    def test_login_request_is_signed_multipart_post(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [login_ok()])
                client.login("tester", "secret", ts_ms=1792147233564)
                request = recorded(opener)[0]
                self.assertEqual(request["method"], "POST")
                self.assertEqual(request["path"], "/api/login")
                self.assertIn("multipart/form-data; boundary=", request["headers"]["content-type"])
                fields = multipart_values(request["body"])
                self.assertEqual(list(fields)[:3], ["username", "password", "turnstile"])
                self.assertEqual(fields["username"], "tester")
                self.assertEqual(fields["password"], "secret")
                self.assertEqual(fields["turnstile"], "")
                self.assertEqual(fields["_timestamp"], "1792147233564")
                self.assertEqual(fields["_sgin"], LOGIN_VECTOR[3])

    def test_login_headers_carry_client_metadata(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [login_ok()])
                client.login("tester", "secret", ts_ms=1792147233564)
                headers = recorded(opener)[0]["headers"]
                self.assertEqual(headers["ts"], "1792147233")
                self.assertEqual(headers["version"], core.CLIENT_VERSION)
                self.assertEqual(headers["webversion"], core.WEB_VERSION)
                self.assertIn("Mozilla/5.0", headers["user-agent"])

    def test_profile_request_signs_query_and_sends_authorization(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [profile_ok(1792147191401)])
                client.last_browse(token="TOKEN", ts_ms=1792147191401)
                request = recorded(opener)[0]
                self.assertEqual(request["method"], "GET")
                self.assertEqual(request["path"], "/api/member/profile")
                self.assertEqual(request["query"]["_timestamp"], "1792147191401")
                self.assertEqual(request["query"]["_sgin"],
                                 core.sign("GET", "/api/member/profile", 1792147191401))
                self.assertEqual(request["headers"]["authorization"], "TOKEN")
                self.assertFalse(request["body"])

    def test_api_key_uses_header_not_authorization(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [profile_ok(1792147191401)])
                client.last_browse(api_key="APIKEY")
                headers = recorded(opener)[0]["headers"]
                self.assertEqual(headers["x-api-key"], "APIKEY")
                self.assertNotIn("authorization", headers)


class LoginFlowTests(unittest.TestCase):
    def test_fetch_last_browse_reads_real_last_browse(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [login_ok(), profile_ok(1792147191401)])
                report = client.fetch_last_browse(username="tester", password="secret")
                self.assertTrue(report.ok)
                self.assertEqual(report.username, "tester")
                self.assertEqual(report.user_id, 12345)
                self.assertEqual(report.last_browse.raw, 1792147191401)
                self.assertEqual(report.last_browse.text, "2026-10-16 18:39:51")
                self.assertEqual(report.code, 0)

    def test_flow_only_touches_login_and_profile(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [login_ok(), profile_ok(1792147191401)])
                client.fetch_last_browse(username="tester", password="secret")
                paths = [item["path"] for item in recorded(opener)]
                self.assertEqual(paths, ["/api/login", "/api/member/profile"])
                self.assertNotIn(FORBIDDEN_PATH, paths)

    def test_otp_secret_is_used_when_site_asks_for_2fa(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                ask_otp = 200, {}, {"code": 1001, "message": "need otp",
                                    "data": ["otp", "email"]}
                client, opener = build_client(core, [ask_otp, login_ok(),
                                                     profile_ok(1792147191401)])
                report = client.fetch_last_browse(username="tester", password="secret",
                                                 otp_secret=OTP_SECRET)
                self.assertTrue(report.ok, report.message)
                requests = recorded(opener)
                self.assertEqual(len(requests), 3)
                second = multipart_values(requests[1]["body"])
                self.assertEqual(second["otpCode"], core.totp(OTP_SECRET))
                self.assertNotIn("emailCode", second)

    def test_2fa_without_code_returns_actionable_message(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                ask_otp = 200, {}, {"code": 1001, "message": "need otp", "data": ["otp"]}
                client, opener = build_client(core, [ask_otp])
                report = client.fetch_last_browse(username="tester", password="secret")
                self.assertFalse(report.ok)
                self.assertIn("二次验证", report.message)
                self.assertIn("动态验证码", report.message)
                self.assertEqual(len(opener.requests), 1)

    def test_email_verify_code_is_submitted(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                ask_email = 200, {}, {"code": 1002, "message": "need email", "data": None}
                client, opener = build_client(core, [ask_email, login_ok(),
                                                     profile_ok(1792147191401)])
                report = client.fetch_last_browse(username="tester", password="secret",
                                                 email_code="654321")
                self.assertTrue(report.ok, report.message)
                second = multipart_values(recorded(opener)[1]["body"])
                self.assertEqual(second["emailCode"], "654321")

    def test_api_key_path_skips_login(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, opener = build_client(core, [profile_ok(1792147191401)])
                report = client.fetch_last_browse(api_key="APIKEY")
                self.assertTrue(report.ok)
                self.assertEqual(report.method, "api_key")
                self.assertEqual(report.credential, "API Key")
                paths = [item["path"] for item in recorded(opener)]
                self.assertEqual(paths, ["/api/member/profile"])

    def test_method_not_supported_falls_back_to_post(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                denied = 405, {}, {"timestamp": "2026-10-16T19:16:29", "status": 405,
                                   "error": "Method Not Allowed", "path": "/api/member/profile",
                                   "message": "Request method 'GET' is not supported"}
                client, opener = build_client(core, [denied, profile_ok(1792147191401)])
                report = client.fetch_last_browse(api_key="KEY")
                self.assertTrue(report.ok, report.message)
                methods = [item["method"] for item in recorded(opener)]
                self.assertEqual(methods, ["GET", "POST"])
                paths = [item["path"] for item in recorded(opener)]
                self.assertNotIn(FORBIDDEN_PATH, paths)

    def test_report_tracks_endpoint_and_api_ok(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                ok_client, _o = build_client(core, [profile_ok(1792147191401)])
                ok = ok_client.fetch_last_browse(api_key="KEY")
                self.assertTrue(ok.api_ok)
                self.assertIn(core.DEFAULT_API_HOST + "/member/profile", ok.endpoint)
                broken = 405, {}, {"timestamp": "2026-10-16T19:16:29", "status": 405,
                                   "error": "Method Not Allowed", "path": "/api/member/profile",
                                   "message": "Request method 'GET' is not supported"}
                bad_client, _o2 = build_client(core, [broken, broken])
                bad = bad_client.fetch_last_browse(api_key="KEY")
                self.assertFalse(bad.api_ok)
                self.assertIn("not supported", bad.message)

    def test_invalid_api_key_returns_failed_report(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                denied = 200, {}, {"code": 1, "message": "key無效", "data": None}
                client, _opener = build_client(core, [denied])
                report = client.fetch_last_browse(api_key="BOGUS", site_label="馒头",
                                                  credential="API Key")
                self.assertFalse(report.ok)
                self.assertEqual(report.site, "馒头")
                self.assertEqual(report.credential, "API Key")
                self.assertIn("无效或已过期", report.message)

    def test_bad_credentials_return_message_without_raising(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                denied = 200, {}, {"code": 1,
                                   "message": "此帳號不存在，請核對輸入是否正確或使用“封禁査詢”",
                                   "data": None}
                client, _opener = build_client(core, [denied])
                report = client.fetch_last_browse(username="ghost", password="secret")
                self.assertFalse(report.ok)
                self.assertIn("不存在", report.message)

    def test_missing_credentials_raise(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [])
                with self.assertRaises(core.MTeamError):
                    client.fetch_last_browse()

    def test_invalid_token_maps_to_hint(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                invalid = 200, {}, {"code": 1, "message": "無效的憑證", "data": None}
                client, _opener = build_client(core, [invalid])
                with self.assertRaises(core.MTeamError) as ctx:
                    client.last_browse(token="bogus")
                self.assertIn("凭证", str(ctx.exception))

    def test_empty_last_browse_is_reported(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                empty = 200, {}, {"code": 0, "message": "ok", "data": {"memberStatus": {}}}
                client, _opener = build_client(core, [login_ok(), empty])
                report = client.fetch_last_browse(username="tester", password="secret")
                self.assertTrue(report.ok)
                self.assertTrue(report.last_browse.empty)
                self.assertIn("未返回最近动向", report.message)


class HelperTests(unittest.TestCase):
    def test_format_last_browse_handles_ms_and_strings(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.format_last_browse(None).text, "")
                self.assertEqual(core.format_last_browse(1792147191401).text, "2026-10-16 18:39:51")
                self.assertEqual(core.format_last_browse(1792147191).text, "2026-10-16 18:39:51")
                self.assertEqual(core.format_last_browse("2026-10-16T17:32:10").text,
                                 "2026-10-16 17:32:10")
                self.assertEqual(core.format_last_browse("1594828800").text,
                                 "2020-07-16 00:00:00")
                self.assertEqual(core.format_last_browse("刚刚").text, "刚刚")

    def test_humanize(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                now = 1792147191401
                self.assertEqual(core.humanize(now - 30_000, now), "刚刚")
                self.assertEqual(core.humanize(now - 300_000, now), "5 分钟前")
                self.assertEqual(core.humanize(now - 7_200_000, now), "2 小时前")
                self.assertEqual(core.humanize(now - 86_400_000 * 3, now), "3 天前")
                self.assertEqual(core.humanize(None), "")

    def test_extract_last_browse_fallbacks(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.extract_last_browse(
                    {"data": {"memberStatus": {"lastBrowse": 5}}}), 5)
                self.assertEqual(core.extract_last_browse(
                    {"data": {"lastBrowse": 7}}), 7)
                self.assertIsNone(core.extract_last_browse({"data": {}}))
                self.assertIsNone(core.extract_last_browse({"data": None}))
                self.assertIsNone(core.extract_last_browse({}))

    def test_normalize_host(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.normalize_host(""), core.DEFAULT_API_HOST)
                self.assertEqual(core.normalize_host("https://api.m-team.cc/api/"),
                                 "https://api.m-team.cc/api")
                self.assertEqual(core.normalize_host("api.m-team.io"),
                                 "https://api.m-team.io/api")

    def test_mask_hides_secrets(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.mask(""), "")
                self.assertEqual(core.mask("abcd"), "****")
                self.assertNotIn("secret", core.mask("secret-token-value"))
                self.assertNotIn("longpassword", core.mask("longpassword"))

    def test_encode_multipart_body(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                content_type, body = core.encode_multipart(
                    [("username", "u"), ("password", "p")], boundary="B")
                self.assertEqual(content_type, "multipart/form-data; boundary=B")
                text = body.decode("utf-8")
                self.assertTrue(text.startswith("--B\r\n"))
                self.assertTrue(text.endswith("--B--\r\n"))
                self.assertEqual(list(multipart_values(text)), ["username", "password"])

    def test_core_never_requests_update_last_browse(self):
        for directory in PLUGIN_DIRS:
            with self.subTest(directory=directory.name):
                source = (directory / "core.py").read_text(encoding="utf-8")
                # 该路径只允许作为常量定义出现，绝不能被拼进任何请求 URL。
                self.assertEqual(source.count('"' + FORBIDDEN_PATH + '"'), 1)
                definitions = [line.strip() for line in source.splitlines()
                               if line.strip().startswith("UPDATE_LAST_BROWSE_PATH")]
                self.assertEqual(definitions,
                                 ['UPDATE_LAST_BROWSE_PATH = "' + FORBIDDEN_PATH + '"'])


class SiteManagementTests(unittest.TestCase):
    """凭证来自 MoviePilot「站点管理」：API 基址推导 + 馒头站点识别。"""

    def test_derive_api_host_from_site_url(self):
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

    def test_pick_mteam_site_prefers_configured_then_autodetect(self):
        sites = [
            {"id": 1, "name": "某站", "domain": "example.com", "url": "https://example.com"},
            {"id": 2, "name": "馒头", "domain": "m-team.cc", "url": "https://kp.m-team.cc",
             "apikey": "KEY"},
            {"id": 3, "name": "Other", "domain": "other.net", "url": "https://other.net"},
        ]
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertEqual(core.pick_mteam_site(sites)["id"], 2)
                self.assertEqual(core.pick_mteam_site(sites, "other.net")["id"], 3)
                self.assertEqual(core.pick_mteam_site(sites, "3")["id"], 3)
                self.assertIsNone(core.pick_mteam_site([sites[0]]))
                self.assertTrue(core.is_mteam_site({"name": "馒头"}))
                self.assertFalse(core.is_mteam_site({"domain": "example.com"}))
                self.assertIsNone(core.pick_mteam_site(None))

    def test_report_carries_site_and_credential(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                client, _opener = build_client(core, [profile_ok(1792147191401)])
                report = client.fetch_last_browse(api_key="KEY", site_label="m-team.cc",
                                                  credential="站点管理 API Key")
                self.assertTrue(report.ok)
                self.assertEqual(report.site, "m-team.cc")
                self.assertEqual(report.credential, "站点管理 API Key")

    def test_api_host_candidates_prefers_primary_then_known(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                hosts = core.api_host_candidates("https://api.m-team.io/api")
                self.assertEqual(hosts[0], "https://api.m-team.io/api")
                self.assertIn(core.DEFAULT_API_HOST, hosts)
                self.assertEqual(len(hosts), len(set(hosts)))
                self.assertEqual(core.api_host_candidates(None)[0], core.DEFAULT_API_HOST)

    def test_looks_like_api_response(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                self.assertTrue(core.looks_like_api_response({"code": 1}))
                self.assertFalse(core.looks_like_api_response({"error": "x"}))
                self.assertFalse(core.looks_like_api_response({}))
                # 原始响应体：真 JSON 才算命中，HTML（如 403）不算
                self.assertTrue(core.looks_like_api_response(b'{"code":1,"message":"bogus"}'))
                self.assertFalse(core.looks_like_api_response(b"<html><title>403 Forbidden</title></html>"))
                self.assertFalse(core.looks_like_api_response(b""))

    def test_plugin_reads_credentials_from_site_management(self):
        for directory in PLUGIN_DIRS:
            with self.subTest(directory=directory.name):
                source = (directory / "__init__.py").read_text(encoding="utf-8")
                # 必须从站点操作层读取站点列表，而不是只依赖手动填写
                self.assertIn("SiteOper", source)
                self.assertIn(".list()", source)
                self.assertIn('config.get("site")', source)
                # 站点缺失 / 缺 API Key 时给出指向站点管理的可操作提示
                self.assertIn("站点管理", source)
                # 前后端分离：按站点地址推导 + 多候选自动回退
                self.assertIn("api_host_candidates", source)
                self.assertIn("api_ok", source)


if __name__ == "__main__":
    unittest.main()
