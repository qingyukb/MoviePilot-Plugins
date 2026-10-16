# -*- coding: utf-8 -*-
"""馒头（M-Team）登录插件（MoviePilot V2/V3）。

登录 kp.m-team.cc 后读取站点真实的「最近动向」（lastBrowse）时间。

站点凭证默认取自 MoviePilot 的「站点管理」：自动识别馒头站点，读取其中的 API Key、
User-Agent、代理与超时，并按站点地址推导 API 基址；也允许手动填写账号密码或 API Key 覆盖。

与其它登录脚本最大的区别：M-Team 前端在登录成功后会**立刻**调用
/member/updateLastBrowse 把「最近动向」刷成当前时间；本插件只读不写，拿到的是
账号真实的最后活跃时间。登录失败的常见原因（2FA、人机验证）都会写明处理建议。
"""

from __future__ import annotations

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

try:  # MoviePilot V2：站点操作层的旧导入路径
    from app.db.site_oper import SiteOper
except ImportError:  # MoviePilot V3：公开操作层
    try:
        from app.db.oper.site import SiteOper
    except ImportError:  # 极端情况下缺少站点模块，插件降级为「仅手动填写」
        SiteOper = None  # type: ignore[assignment]

from .core import (
    API_HOSTS,
    DEFAULT_API_HOST,
    DEFAULT_USER_AGENT,
    LoginReport,
    MTeamClient,
    MTeamError,
    api_host_candidates,
    derive_api_host,
    mask_account,
    pick_mteam_site,
)


class MTeamLogin(_PluginBase):
    plugin_name = "馒头登录"
    plugin_desc = "从「站点管理」读取馒头（M-Team）凭证，获取真实的「最近动向」时间。"
    plugin_icon = "mteam.png"
    plugin_version = "1.0.2"
    plugin_author = "qingyu"
    author_url = "https://blog.qyus.cn"
    plugin_config_prefix = "mteamlogin_"
    plugin_order = 31
    auth_level = 2

    _enabled = False
    _notify_mode = "always"
    _cron = "0 9 * * *"
    _site = ""
    _api_host = ""
    _username = ""
    _password = ""
    _otp_secret = ""
    _otp_code = ""
    _email_code = ""
    _tg_code = ""
    _api_key = ""
    _timeout = 0
    _proxy = False
    _run_once = False

    _history_days = 30
    _run_lock: Lock = Lock()
    _run_once_scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify_mode = self._parse_notify_mode(config)
        self._cron = str(config.get("cron") or "0 9 * * *")
        self._site = str(config.get("site") or "").strip()
        self._api_host = str(config.get("api_host") or "").strip()
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._otp_secret = str(config.get("otp_secret") or "").strip()
        self._otp_code = str(config.get("otp_code") or "").strip()
        self._email_code = str(config.get("email_code") or "").strip()
        self._tg_code = str(config.get("tg_code") or "").strip()
        self._api_key = str(config.get("api_key") or "").strip()
        self._timeout = max(0, min(int(config.get("timeout") or 0), 120))
        self._proxy = bool(config.get("proxy", False))
        self._run_once = bool(config.get("run_once", False))
        if self._run_once:
            self._run_once = False
            self.update_config(self._config())
            self._start_run_once()

    def _config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify_mode": self._notify_mode,
            "cron": self._cron,
            "site": self._site,
            "api_host": self._api_host,
            "username": self._username,
            "password": self._password,
            "otp_secret": self._otp_secret,
            "otp_code": self._otp_code,
            "email_code": self._email_code,
            "tg_code": self._tg_code,
            "api_key": self._api_key,
            "timeout": self._timeout,
            "proxy": self._proxy,
            "run_once": self._run_once,
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
        site_items = self._site_options()
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
                {"component": "VCol", "props": {"cols": 12, "md": 8},
                 "content": [{"component": "VSelect", "props": {
                             "model": "site", "label": "站点（来自「站点管理」）", "items": site_items,
                             "clearable": True,
                             "hint": "留空自动识别馒头站点；API Key、User-Agent、代理与超时都从站点管理读取",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VCronField", "props": {"model": "cron", "label": "执行周期",
                             "placeholder": "5 位 Cron；例如 0 9 * * *"}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 8},
                 "content": [{"component": "VCombobox", "props": {
                             "model": "api_host", "label": "API 基址（可选）", "items": host_items,
                             "hint": "留空则按站点地址自动推导（馒头默认为 " + DEFAULT_API_HOST + "）",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VTextField", "props": {"model": "timeout",
                             "label": "HTTP 超时（秒，0=用站点设置）", "type": "number",
                             "min": 0, "max": 120}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "username",
                             "label": "M-Team 用户名（可选）",
                             "hint": "站点管理已提供 API Key 时无需填写；仅在没有 API Key 时才用账号密码登录",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "password",
                             "label": "密码（可选）", "type": "password"}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "otp_secret", "label": "OTP 密钥（可选）",
                             "hint": "开启两步验证时填验证器里的 Base32 密钥，插件会自动生成动态码",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "otp_code", "label": "动态码（可选）",
                             "hint": "没有密钥时可临时填 6 位动态码", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "email_code", "label": "邮箱验证码（可选）",
                             "hint": "站点要求邮箱验证时可填，验证码有效期很短",
                             "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "tg_code", "label": "Telegram 验证码（可选）",
                             "hint": "站点要求 Telegram 验证时可填", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12},
                 "content": [{"component": "VTextField", "props": {"model": "api_key",
                             "label": "手动 API Key（可选）",
                             "hint": "留空则使用站点管理里的 API Key；填了则优先用它",
                             "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12},
                 "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                             "text": "凭证默认取自「站点管理」：自动识别馒头站点并读取其中的 API Key"
                                     "（站点「控制台 → 实验室 → 存取令牌」）。插件只访问 /member/profile 的 "
                                     "memberStatus.lastBrowse，不调用 updateLastBrowse，因此拿到的是账号真实的最后活跃时间。"}}]},
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
        browse_text = str(latest.get("last_browse_text") or "-")
        relative = str(latest.get("last_browse_relative") or "")
        if relative:
            browse_text = browse_text + "（" + relative + "）"
        rows = []
        for row in history:
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(row.get("ran_at") or "-")},
                {"component": "td", "text": str(row.get("last_browse_text") or "-")},
                {"component": "td", "text": str(row.get("relative") or "-")},
                {"component": "td", "content": [{"component": "VChip", "props": {
                    "size": "x-small", "variant": "tonal",
                    "color": "success" if row.get("ok") else "error"},
                    "text": "成功" if row.get("ok") else "失败"}]},
                {"component": "td", "text": str(row.get("message") or "-")},
            ]})
        content: List[dict] = [
            {"component": "style", "text":
             ".mteam-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}"
             ".mteam-stat{padding:12px}.mteam-stat__label{color:rgba(var(--v-theme-on-surface),.62);font-size:.78rem}"
             ".mteam-stat__value{margin-top:6px;font-size:1.35rem;font-weight:700}"
             ".mteam-table-wrap{overflow-x:auto;border:1px solid rgba(var(--v-theme-on-surface),.08);border-radius:8px}"},
            {"component": "div", "props": {"class": "mteam-summary"}, "content": [
                self._stat("最近动向", browse_text or "-", "站点真实最后活跃时间",
                           "success" if ok else "error"),
                self._stat("账号", str(latest.get("username") or "-"),
                           "站点：" + str(latest.get("site") or "-")
                           + " · UID：" + str(latest.get("user_id") or "-"), "info"),
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
                                      {"component": "th", "text": "状态"},
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

    # ---------------- 站点管理 ----------------

    @staticmethod
    def _load_sites() -> List[Dict[str, Any]]:
        """读取 MoviePilot「站点管理」里的站点，转换为普通字典（避免 ORM 对象跨线程）。"""
        if SiteOper is None:
            return []
        try:
            records = SiteOper().list() or []
        except Exception:
            logger.exception("馒头登录：读取站点管理失败")
            return []
        sites: List[Dict[str, Any]] = []
        for record in records:
            sites.append({
                "id": getattr(record, "id", None),
                "name": getattr(record, "name", None),
                "domain": getattr(record, "domain", None),
                "url": getattr(record, "url", None),
                "apikey": getattr(record, "apikey", None),
                "cookie": getattr(record, "cookie", None),
                "ua": getattr(record, "ua", None),
                "proxy": getattr(record, "proxy", None),
                "token": getattr(record, "token", None),
                "timeout": getattr(record, "timeout", None),
            })
        return sites

    def _site_options(self) -> List[Dict[str, str]]:
        """配置页下拉项：站点管理里可选的站点。"""
        options: List[Dict[str, str]] = []
        for site in self._load_sites():
            domain = str(site.get("domain") or "").strip()
            if not domain:
                continue
            name = str(site.get("name") or "").strip() or domain
            options.append({"title": name + "（" + domain + "）", "value": domain})
        return options

    def _resolve_site(self) -> Dict[str, Any]:
        """挑出要用的馒头站点：优先配置里指定的，否则按域名 / 名称自动识别。"""
        return pick_mteam_site(self._load_sites(), self._site) or {}

    # ---------------- 核心流程 ----------------

    def run_login(self) -> None:
        """执行一轮：登录 + 读取最近动向（Cron 与「立即执行」共用，进程级互斥）。"""
        if not MTeamLogin._run_lock.acquire(blocking=False):
            logger.warning("馒头登录：上一轮仍在执行，本次跳过")
            return
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

    def _fetch_report(self) -> LoginReport:
        site = self._resolve_site()
        site_domain = str(site.get("domain") or "").strip()
        site_url = str(site.get("url") or site.get("domain") or "").strip()
        # 凭证来源：手动 API Key > 站点管理里的 API Key > 账号密码登录。
        site_api_key = str(site.get("apikey") or "").strip()
        api_key = self._api_key or site_api_key
        if self._api_key:
            credential = "手动 API Key"
        elif site_api_key:
            credential = "站点管理 API Key"
        else:
            credential = "账号密码"
        if not api_key and (not self._username or not self._password):
            if not site:
                raise MTeamError(
                    "未在「站点管理」找到馒头（M-Team）站点：请先在 设置 → 站点管理 添加 M-Team 并填写"
                    "站点 API Key（站点「控制台 → 实验室 → 存取令牌」），再启用本插件。")
            raise MTeamError(
                "站点管理里的馒头站点（" + (site_domain or "未命名") + "）没有填写 API Key："
                "请在 设置 → 站点管理 里补全该站点的 API Key，或在本插件填写手动 API Key。")
        timeout = self._timeout or int(site.get("timeout") or 0) or 20
        timeout = max(5, min(int(timeout), 120))
        proxy = self._proxy or bool(site.get("proxy"))
        user_agent = str(site.get("ua") or "").strip() or DEFAULT_USER_AGENT
        # 前后端分离：优先用按站点地址推导的后端地址，不可用时自动回退已知地址。
        primary_host = self._api_host or derive_api_host(site_url)
        hosts = api_host_candidates(primary_host)
        logger.info("馒头登录：站点 %s，凭证 %s，候选接口 %s",
                    site_domain or "（未指定站点）", credential,
                    " → ".join(hosts))
        report: Optional[LoginReport] = None
        for host in hosts:
            client = MTeamClient(host=host, timeout=timeout, proxy=proxy, user_agent=user_agent)
            try:
                report = client.fetch_last_browse(
                    username=self._username,
                    password=self._password,
                    api_key=api_key,
                    otp_secret=self._otp_secret,
                    otp_code=self._otp_code,
                    email_code=self._email_code,
                    tg_code=self._tg_code,
                    site_label=site_domain,
                    credential=credential,
                )
            except MTeamError as error:
                report = LoginReport(ok=False, message=str(error),
                                     ran_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     site=site_domain, credential=credential,
                                     endpoint=client.last_request)
            if report.api_ok:
                if host != hosts[0]:
                    logger.info("馒头登录：接口 %s 未返回 M-Team 数据，已改用 %s",
                                hosts[0], host)
                return report
            logger.warning("馒头登录：接口 %s 未返回 M-Team 数据（%s），尝试下一个",
                           host, report.message or report.endpoint or "未知响应")
        if report is not None and not report.api_ok and report.endpoint:
            report.message = (report.message + "；最后一次请求 " + report.endpoint).strip("；")
        return report or LoginReport(ok=False, message="没有可用的 M-Team 接口地址。")

    def _finish(self, report: LoginReport) -> None:
        record = self._record(report)
        self._save(record)
        if report.ok:
            logger.info("馒头登录：%s（账号 %s，%s）", report.message,
                        mask_account(record.get("username") or ""),
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
            "last_browse_raw": browse.raw,
            "last_browse_text": browse.text,
            "last_browse_relative": browse.relative,
            "relative": browse.relative,
            "verify_types": list(report.verify_types),
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
            title = "馒头登录成功"
            text = ("账号：" + str(record.get("username") or "-")
                    + "\n最近动向：" + str(record.get("last_browse_text") or "-")
                    + ("（" + str(record.get("last_browse_relative")) + "）"
                       if record.get("last_browse_relative") else "")
                    + "\n检查时间：" + str(record.get("ran_at") or "-"))
        else:
            title = "馒头登录失败"
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
