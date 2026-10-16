# -*- coding: utf-8 -*-
"""馒头（M-Team）登录 + 保活插件（MoviePilot V2/V3）。

前端 kp.m-team.cc / 后端 api.m-team.cc 前后端分离，插件每一轮都走**真实登录**这条链路：

1. 凭证全部由**插件配置页手填**，不再读取 MoviePilot「站点管理」；
   推荐填「账号 + 密码」，插件会 POST /api/login 真实登录换回新的 authorization；
   也可以直接填浏览器登录后的 authorization（JWT），或填能解析出登录令牌的 Cookie；
2. 用 M-Team 自己的签名算法调用后端 api.<域名>/api；
3. 用真实登录态调用 POST /api/member/updateLastBrowse 做**保活心跳**
   （前端登录成功后也是马上调这个接口），把账号的「最近动向」刷新成当前时间；
4. 再读 GET /api/member/profile 的 data.memberStatus.lastBrowse 校验真实结果。

**关键：API Key（x-api-key）调用不算活跃**，站点对 updateLastBrowse 一律返回
"Full authentication is required"，所以 API Key 只用于读取数据，绝不参与保活。

保活正常时，「最近动向」应该永远是「刚刚 / 1 分钟前」；如果它还是几天前，说明真实登录态没生效，
插件会把账号真实的最近动向显示出来，并在通知里写清楚该去插件配置里补哪一项。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase

try:  # MoviePilot V3 日志门面
    from app.sdk.logging import logger
except ImportError:  # MoviePilot V2
    from app.log import logger

from app.schemas import NotificationType

from .core import (
    API_HOSTS,
    DEFAULT_API_HOST,
    DEFAULT_USER_AGENT,
    LoginReport,
    MTeamClient,
    MTeamError,
    api_host_candidates,
    credentials_from_config,
    mask_account,
)


class MTeamLogin(_PluginBase):
    plugin_name = "馒头登录"
    plugin_desc = "插件配置页手填馒头（M-Team）账号密码或 authorization，真实登录保活并校验「最近动向」。"
    plugin_icon = "mteam.png"
    plugin_version = "1.4.0"
    plugin_author = "qingyu"
    author_url = "https://blog.qyus.cn"
    plugin_config_prefix = "mteamlogin_"
    plugin_order = 31
    auth_level = 2

    _enabled = False
    _notify_mode = "always"
    _cron = "0 9 * * *"
    _api_host = ""
    _timeout = 0
    _proxy = False
    _run_once = False

    _username = ""
    _password = ""
    _otp_secret = ""
    _otp_code = ""
    _email_code = ""
    _tg_code = ""
    _token = ""
    _api_key = ""
    _cookie = ""
    _did = ""

    _history_days = 30
    _run_lock: Lock = Lock()
    _run_once_scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify_mode = self._parse_notify_mode(config)
        self._cron = str(config.get("cron") or "0 9 * * *")
        self._api_host = str(config.get("api_host") or "").strip()
        self._timeout = max(0, min(int(config.get("timeout") or 0), 120))
        self._proxy = bool(config.get("proxy", False))
        self._run_once = bool(config.get("run_once", False))

        # 全部凭证只来自插件配置，插件自己保存并用于每一轮真实登录 / 保活。
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._otp_secret = str(config.get("otp_secret") or "").strip()
        self._otp_code = str(config.get("otp_code") or "").strip()
        self._email_code = str(config.get("email_code") or "").strip()
        self._tg_code = str(config.get("tg_code") or "").strip()
        self._token = str(config.get("token") or "").strip()
        self._api_key = str(config.get("api_key") or "").strip()
        self._cookie = str(config.get("cookie") or "").strip()
        # 馒头把登录态 authorization 绑定到 did（浏览器 localStorage.did）：登录与
        # 后续保活必须带同一个 did。留空时自动生成一个并写回配置，保证每轮一致。
        self._did = str(config.get("did") or "").strip()
        if not self._did:
            self._did = os.urandom(16).hex()
            self.update_config(self._config())

        if self._run_once:
            self._run_once = False
            self.update_config(self._config())
            self._start_run_once()

    def _config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify_mode": self._notify_mode,
            "cron": self._cron,
            "api_host": self._api_host,
            "timeout": self._timeout,
            "proxy": self._proxy,
            "run_once": self._run_once,
            "username": self._username,
            "password": self._password,
            "otp_secret": self._otp_secret,
            "otp_code": self._otp_code,
            "email_code": self._email_code,
            "tg_code": self._tg_code,
            "token": self._token,
            "api_key": self._api_key,
            "cookie": self._cookie,
            "did": self._did,
        }

    @staticmethod
    def _parse_notify_mode(config: Dict[str, Any]) -> str:
        """通知模式：always 每次通知 / failed 仅失败通知 / off 不通知。"""
        mode = str(config.get("notify_mode") or "").lower()
        if mode in {"always", "failed", "off"}:
            return mode
        return "always" if config.get("notify", True) else "off"

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error("馒头登录：Cron 表达式无效：%s", self._cron)
            return []
        return [{"id": "MTeamLogin.Run", "name": "馒头登录", "trigger": trigger,
                 "func": self.run_login, "kwargs": {}}]

    def _start_run_once(self) -> None:
        """「保存后立即执行一次」：延后几秒交给后台调度器执行，不阻塞保存配置的请求。"""
        try:
            scheduler = BackgroundScheduler()
            scheduler.add_job(func=self._run_once_task, trigger="date",
                              run_date=datetime.now() + timedelta(seconds=3),
                              name="馒头登录 立即执行一次")
            scheduler.start()
        except Exception:
            logger.exception("馒头登录：后台调度器启动失败，改用线程执行")
            Thread(target=self._run_once_task, daemon=True).start()
            return
        self._run_once_scheduler = scheduler
        logger.info("馒头登录：保存后立即执行一次，将在 3 秒后于后台开始")

    def _run_once_task(self) -> None:
        try:
            self.run_login()
        except Exception:
            logger.exception("馒头登录：立即执行失败")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        host_items = [{"title": host, "value": host} for host in API_HOSTS]
        notify_items = [
            {"title": "每次执行后通知", "value": "always"},
            {"title": "仅失败时通知", "value": "failed"},
            {"title": "不通知", "value": "off"},
        ]
        return [{"component": "VForm", "content": [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSwitch", "props": {"model": "proxy", "label": "使用系统代理"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "保存后立即执行一次"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSelect", "props": {"model": "notify_mode", "label": "通知模式",
                             "items": notify_items}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VTextField", "props": {
                             "model": "username", "label": "账号（推荐）",
                             "placeholder": "M-Team 登录账号 / 邮箱",
                             "hint": "填账号 + 密码后，插件会 POST /api/login 真实登录并保活",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VTextField", "props": {
                             "model": "password", "label": "密码（推荐）", "type": "password",
                             "placeholder": "M-Team 登录密码"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VTextField", "props": {
                             "model": "otp_secret", "label": "二步验证密钥（可选）",
                             "placeholder": "Base32 密钥；填了会自动算动态码"}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {
                             "model": "otp_code", "label": "动态码（可选）",
                             "placeholder": "临时 6 位码"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {
                             "model": "email_code", "label": "邮箱验证码（可选）",
                             "placeholder": "站点要求邮箱验证时填"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {
                             "model": "tg_code", "label": "Telegram 验证码（可选）",
                             "placeholder": "站点要求 TG 验证时填"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {
                             "model": "timeout", "label": "HTTP 超时（秒，0=20）",
                             "type": "number", "min": 0, "max": 120}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8},
                 "content": [{"component": "VTextField", "props": {
                             "model": "token", "label": "authorization（可选，可直接保活）",
                             "type": "password",
                             "placeholder": "浏览器登录 kp.m-team.cc 后 localStorage.auth 的值",
                             "hint": "这是真实登录态 JWT；填了可不用账号密码。过期后请重新填，或补账号密码让插件自动登录",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VTextField", "props": {
                             "model": "api_key", "label": "API Key（可选，只读）",
                             "type": "password",
                             "hint": "只能读最近动向，不能保活；用 x-api-key 调保活接口不算活跃",
                             "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8},
                 "content": [{"component": "VTextarea", "props": {
                             "model": "cookie", "label": "Cookie（可选）", "rows": 2,
                             "placeholder": "能包含 authorization / token / jwt 时也可用于保活"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VCombobox", "props": {
                             "model": "api_host", "label": "API 基址（可选）", "items": host_items,
                             "hint": "留空使用 " + DEFAULT_API_HOST + "；前后端分离时填后端 API，不能填 kp 前端地址",
                             "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12},
                 "content": [{"component": "VTextField", "props": {
                             "model": "did", "label": "设备标识 did（可选，通常留空）",
                             "placeholder": "留空自动生成并固定；也可填浏览器 localStorage.did",
                             "hint": "馒头把登录态绑定到 did：登录和保活必须用同一个 did，"
                                     "否则会被站点判定为登录态失效。留空时插件自动生成并保存。",
                             "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VCronField", "props": {"model": "cron", "label": "执行周期",
                             "placeholder": "5 位 Cron；例如 0 */6 * * *"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                             "text": "保活必须走真实登录态：推荐填「账号 + 密码」，"
                                     "插件会用 POST /api/login 换取 authorization，再调 "
                                     "POST /api/member/updateLastBrowse 刷新「最近动向」；"
                                     "也可以直接填浏览器登录后的 authorization。"
                                     "API Key（x-api-key）只能读取，不算活跃，不能保活。"}}]},
            ]},
        ]}], self._config()

    def get_page(self) -> List[dict]:
        latest = self.get_data("latest") or {}
        if not isinstance(latest, dict):
            latest = {}
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history = [row for row in history if isinstance(row, dict)][:30]
        if not latest:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                     "text": "暂无记录；保存配置后使用「保存后立即执行一次」开始，或在 Cron 周期到点时自动执行。"}}]
        ok = bool(latest.get("ok"))
        keepalive_ok = bool(latest.get("keepalive_ok"))
        browse_text = str(latest.get("last_browse_text") or "-")
        relative = str(latest.get("last_browse_relative") or "")
        if relative:
            browse_text = browse_text + "（" + relative + "）"
        rows = []
        for row in history:
            keep = bool(row.get("keepalive_ok"))
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(row.get("ran_at") or "-")},
                {"component": "td", "text": str(row.get("last_browse_text") or "-")},
                {"component": "td", "text": str(row.get("relative") or "-")},
                {"component": "td", "content": [{"component": "VChip", "props": {
                    "size": "x-small", "variant": "tonal",
                    "color": "success" if keep else "error"},
                    "text": "已保活" if keep else "未保活"}]},
                {"component": "td", "text": str(row.get("message") or "-")},
            ]})
        content: List[dict] = [
            {"component": "style", "text":
             ".mteam-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}"
             ".mteam-stat{padding:12px}.mteam-stat__label{color:rgba(var(--v-theme-on-surface),.62);font-size:.78rem}"
             ".mteam-stat__value{margin-top:6px;font-size:1.35rem;font-weight:700}"
             ".mteam-table-wrap{overflow-x:auto;border:1px solid rgba(var(--v-theme-on-surface),.08);border-radius:8px}"},
            {"component": "div", "props": {"class": "mteam-summary"}, "content": [
                self._stat("最近动向", browse_text or "-", "站点返回的真实最后活跃时间",
                           "success" if keepalive_ok else "error"),
                self._stat("保活心跳", "已保活" if keepalive_ok else "未保活",
                           "POST /member/updateLastBrowse"
                           + (" · code " + str(latest.get("keepalive_code"))
                              if latest.get("keepalive_code") not in (None, "") else "")
                           + (" · 真实登录换新令牌" if latest.get("login_used")
                              else " · 复用配置登录态"),
                           "success" if keepalive_ok else "error"),
                self._stat("账号", str(latest.get("username") or "-"),
                           "接口：" + str(latest.get("site") or DEFAULT_API_HOST)
                           + " · UID：" + str(latest.get("user_id") or "-")
                           + " · 凭证：" + str(latest.get("credential") or "-"), "info"),
                self._stat("本次检查", str(latest.get("ran_at") or "-"),
                           str(latest.get("message") or "-"), "success" if ok else "error"),
            ]},
        ]
        if rows:
            content.append(
                {"component": "div", "props": {"class": "mteam-table-wrap mt-3"},
                 "content": [{"component": "VTable", "props": {"density": "compact", "hover": True},
                              "content": [
                                  {"component": "thead", "content": [{"component": "tr", "content": [
                                      {"component": "th", "text": "检查时间"},
                                      {"component": "th", "text": "最近动向"},
                                      {"component": "th", "text": "距今"},
                                      {"component": "th", "text": "保活"},
                                      {"component": "th", "text": "说明"}]}]},
                                  {"component": "tbody", "content": rows}]}]})
        return content

    @staticmethod
    def _stat(label: str, value: str, hint: str, color: str) -> Dict[str, Any]:
        return {"component": "div",
                "props": {"class": "mteam-stat app-card-shell app-card-colorful",
                          "style": "--app-card-accent-rgb: var(--v-theme-" + color + ");"},
                "content": [
                    {"component": "div", "props": {"class": "mteam-stat__label"}, "text": label},
                    {"component": "div", "props": {"class": "mteam-stat__value text-" + color},
                     "text": value},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"},
                     "text": hint},
                ]}

    # ---------------- 核心流程 ----------------

    def run_login(self) -> None:
        """执行一轮：认证 + 保活 + 读取最近动向（Cron 与「立即执行」共用，进程级互斥）。"""
        if not MTeamLogin._run_lock.acquire(blocking=False):
            logger.warning("馒头登录：上一轮仍在执行，本次跳过")
            return
        try:
            try:
                report = self._fetch_report()
            except MTeamError as error:
                report = LoginReport(ok=False, message=str(error),
                                     ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as error:  # noqa: BLE001 - 兜底，避免调度线程被打断
                logger.exception("馒头登录：执行出现未预期错误")
                report = LoginReport(ok=False, message="执行异常：" + str(error),
                                     ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._finish(report)
        finally:
            MTeamLogin._run_lock.release()

    def _fetch_report(self) -> LoginReport:
        credentials = credentials_from_config(self._config())
        if not credentials.usable:
            raise MTeamError(
                "插件配置里还没有可用凭证：请填「账号 + 密码」让插件真实登录并保活，"
                "或直接填浏览器登录 kp.m-team.cc 后的 authorization。"
                "只有 API Key 时能读取数据，但站点不认为活跃，不能保活。")

        timeout = max(5, min(int(self._timeout or 20), 120))
        primary_host = self._api_host or DEFAULT_API_HOST
        hosts = api_host_candidates(primary_host)
        logger.info("馒头登录：凭证来源 %s，候选接口 %s",
                    credentials.source or "（空）", " → ".join(hosts))
        reports: List[LoginReport] = []
        for host in hosts:
            client = MTeamClient(host=host, timeout=timeout, proxy=self._proxy,
                                 user_agent=DEFAULT_USER_AGENT, did=self._did)
            try:
                report = client.fetch_last_browse(credentials=credentials,
                                                  site_label=host)
            except MTeamError as error:
                report = LoginReport(ok=False, message=str(error),
                                     ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     site=host, credential=credentials.source,
                                     endpoint=client.last_request)
            reports.append(report)
            if report.ok or report.keepalive_ok:
                if host != hosts[0]:
                    logger.info("馒头登录：接口 %s 未完成保活，已改用 %s",
                                hosts[0], host)
                return report
            if not report.api_ok:
                logger.warning("馒头登录：接口 %s 未返回 M-Team 数据（%s），尝试下一个",
                               host, report.message or report.endpoint or "未知响应")
                continue
            # 某些镜像会返回结构化错误（例如 401 / code -1），这不能证明它
            # 是可用接口，继续尝试其它候选地址。
            logger.warning("馒头登录：接口 %s 返回 M-Team 错误（%s），尝试下一个",
                           host, report.message or report.endpoint or "未知响应")

        # 所有接口都无法保活时，优先保留能读出最近动向的响应；否则保留
        # 第一个真正的 M-Team 结构化响应，便于用户看到可诊断的错误。
        if reports:
            fallback = next((item for item in reports if not item.last_browse.empty), None)
            if fallback is None:
                fallback = next((item for item in reports if item.api_ok), reports[0])
            if fallback is not reports[0] and fallback.site:
                logger.info("馒头登录：接口 %s 均未保活，使用 %s 的读取结果",
                            hosts[0], fallback.site)
            if fallback.endpoint:
                fallback.message = (fallback.message + "；最后一次请求 "
                                    + fallback.endpoint).strip("；")
            return fallback
        return LoginReport(ok=False, message="没有可用的 M-Team 接口地址。")

    def _persist_after_report(self, report: LoginReport) -> None:
        """把真实登录换回的新 authorization 与命中的接口地址回写插件配置。

        这样下一轮会直接复用新令牌保活，而不是再次触发邮箱两步验证
        （邮箱验证码是一次性的，不可能每轮都让用户手工填）。
        """
        changed = False
        token = str(getattr(report, "new_token", "") or "").strip()
        if token and token != self._token:
            self._token = token
            # 一次性验证码已经用完，清掉避免下一轮拿旧码重试。
            if self._email_code:
                self._email_code = ""
            if self._otp_code:
                self._otp_code = ""
            changed = True
            logger.info("馒头登录：已保存本次真实登录换回的新 authorization（长度 %d）", len(token))
        if report.ok and report.site:
            current = self._api_host or DEFAULT_API_HOST
            if report.site != current:
                self._api_host = report.site
                changed = True
                logger.info("馒头登录：已把可用接口地址写回配置：%s", report.site)
        if changed:
            self.update_config(self._config())

    def _finish(self, report: LoginReport) -> None:
        self._persist_after_report(report)
        record = self._record(report)
        self._save(record)
        if report.ok:
            logger.info("馒头登录：%s（账号 %s，保活 %s，真实登录 %s，%s）", report.message,
                        mask_account(record.get("username") or ""),
                        "成功" if report.keepalive_ok else "失败",
                        "是" if record.get("login_used") else "复用配置登录态",
                        record.get("last_browse_relative") or "时间未知")
        else:
            logger.warning("馒头登录：失败 - %s", report.message)
        self._notify(record)

    @staticmethod
    def _record(report: LoginReport) -> Dict[str, Any]:
        browse = report.last_browse
        return {
            "ran_at": report.ran_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(report.ok),
            "code": report.code,
            "method": report.method,
            "credential": report.credential,
            "site": report.site,
            "username": report.username,
            "user_id": report.user_id,
            "message": report.message,
            "keepalive_ok": bool(report.keepalive_ok),
            "keepalive_code": report.keepalive_code,
            "keepalive_message": report.keepalive_message,
            "login_used": bool(report.login_used),
            "login_ok": bool(report.login_ok),
            "last_browse_raw": browse.raw,
            "last_browse_text": browse.text,
            "last_browse_relative": browse.relative,
            "relative": browse.relative,
        }

    def _save(self, record: Dict[str, Any]) -> None:
        """保存最近一次结果与历史（按天数裁剪，避免插件数据无限增长）。"""
        self.save_data("latest", record)
        raw_history = self.get_data("history") or []
        history = [row for row in raw_history if isinstance(row, dict)]
        history.insert(0, record)
        cutoff = (datetime.now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d")
        kept = []
        for row in history:
            ran_at = str(row.get("ran_at") or "")
            if ran_at[:10] and ran_at[:10] < cutoff:
                continue
            kept.append(row)
        self.save_data("history", kept[:200])

    def _notify(self, record: Dict[str, Any]) -> None:
        """按通知模式推送：always 每次通知 / failed 仅失败通知 / off 不通知。"""
        mode = self._notify_mode
        if mode == "off":
            return
        ok = bool(record.get("ok"))
        if mode == "failed" and ok:
            return
        if ok:
            title = "馒头保活成功"
            text = ("账号：" + str(record.get("username") or "-")
                    + "\n保活心跳：成功（code " + str(record.get("keepalive_code"))
                    + "，方式：" + ("真实登录换新令牌" if record.get("login_used")
                                    else "复用配置登录态") + "）"
                    + "\n最近动向：" + str(record.get("last_browse_text") or "-")
                    + ("（" + str(record.get("last_browse_relative")) + "）"
                       if record.get("last_browse_relative") else "")
                    + "\n检查时间：" + str(record.get("ran_at") or "-"))
        else:
            title = "馒头保活失败"
            text = ("账号：" + str(record.get("username") or "-")
                    + "\n原因：" + str(record.get("message") or "-")
                    + "\n检查时间：" + str(record.get("ran_at") or "-"))
        self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)

    def stop_service(self) -> None:
        """插件停用 / 重载时清理自有的「立即执行」调度器。"""
        scheduler, self._run_once_scheduler = getattr(self, "_run_once_scheduler", None), None
        if scheduler is not None:
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception:
                logger.warning("馒头登录：清理立即执行调度器时出错")
        return None
