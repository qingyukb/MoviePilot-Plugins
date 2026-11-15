"""M-Team 保活插件：定时用真实浏览器登录（账号密码 + TOTP），避免长期不登录被删号。

参考 KnowsWlf/MTeam-CLI 的做法：保活必须是真实登录（API ping 不重置「40 天不登录删号」），
登录页选择器 #username / #password / button[type=submit]，2FA 是 #otp-code。
账号统计走官方 API（API Key，不需要浏览器）。
"""
from __future__ import annotations

from datetime import datetime
from threading import Timer
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from app.schemas.types import EventType, NotificationType

from .mteam import (DEFAULT_API_BASE, DEFAULT_SITE, activity_minutes_ago, fetch_profile,
                    format_activity, keepalive_login, last_activity, parse_datetime,
                    parse_profile, totp_now)

HISTORY_LIMIT = 30
PAGE_STYLE = (
    ".mteam-page{background:rgb(var(--v-theme-surface));border-radius:14px;padding:14px 14px 6px;"
    "box-sizing:border-box;max-width:100%;overflow-wrap:anywhere}"
    ".mteam-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}"
    ".mteam-stat{display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:10px;"
    "border:1px solid rgba(var(--v-theme-on-surface),.10);background:rgba(var(--v-theme-on-surface),.03)}"
    ".mteam-stat-num{font-size:1.05rem;font-weight:700;line-height:1.2;"
    "font-variant-numeric:tabular-nums}"
    ".mteam-stat-label{font-size:.72rem;color:rgba(var(--v-theme-on-surface),.6)}"
    ".mteam-num-ok{color:#4caf50}.mteam-num-warn{color:#fb8c00}.mteam-num-grey{color:"
    "rgba(var(--v-theme-on-surface),.38)}"
    ".mteam-line{margin:10px 0 4px;font-size:.78rem;color:rgba(var(--v-theme-on-surface),.62)}"
    ".mteam-section{display:flex;align-items:center;gap:8px;margin:14px 0 8px;font-weight:600}"
    ".mteam-row{display:flex;flex-wrap:wrap;align-items:center;gap:2px 10px;width:100%;"
    "box-sizing:border-box;margin-bottom:8px;padding:9px 12px;border-radius:10px;"
    "border:1px solid rgba(var(--v-theme-on-surface),.10);background:rgba(var(--v-theme-on-surface),.025)}"
    ".mteam-row-head{display:flex;align-items:center;gap:8px;width:100%;min-width:0}"
    ".mteam-row-time{flex:1 1 auto;min-width:0;font-weight:600;font-size:.9rem;white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis}"
    ".mteam-row-status{flex:0 0 auto}"
    ".mteam-row-msg{width:100%;font-size:.8rem;line-height:1.45;word-break:break-word;"
    "color:rgba(var(--v-theme-on-surface),.7)}"
)


class MTeamAlive(_PluginBase):
    plugin_name = "馒头保活"
    plugin_desc = "定时用真实浏览器登录 M-Team（账号密码 + TOTP），避免长期不登录被删号；顺带读取分享率与魔力。"
    plugin_icon = "https://raw.githubusercontent.com/qingyukb/MoviePilot-Plugins/main/icons/mteamalive.png"
    plugin_version = "2.0.1"
    plugin_author = "qingyukb"
    author_url = "https://blog.qyus.cn"
    plugin_config_prefix = "mteamalive_"
    plugin_order = 30
    auth_level = 1

    _enabled = False
    _notify = True
    _run_once = False
    _cron = "0 9 * * *"
    _site_url = DEFAULT_SITE
    _username = ""
    _password = ""
    _totp_secret = ""
    _api_key = ""
    _api_base = DEFAULT_API_BASE
    _ratio_warn = 7
    _verify_minutes = 10
    _interval_days = 20
    _api_verify = True
    _run_once_timer: Optional[Timer] = None

    def init_plugin(self, config: Optional[dict] = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._run_once = bool(config.get("run_once"))
            self._cron = str(config.get("cron") or "0 9 * * *").strip()
            self._username = str(config.get("username") or "").strip()
            self._password = str(config.get("password") or "")
            self._totp_secret = str(config.get("totp_secret") or "").strip()
            self._api_key = str(config.get("api_key") or "").strip()
            try:
                ratio_warn = float(config.get("ratio_warn") or 7)
                self._ratio_warn = int(ratio_warn) if ratio_warn.is_integer() else ratio_warn
            except (TypeError, ValueError):
                self._ratio_warn = 7
            try:
                self._verify_minutes = int(config.get("verify_minutes") or 10)
            except (TypeError, ValueError):
                self._verify_minutes = 10
            try:
                self._interval_days = max(1, int(config.get("interval_days") or 20))
            except (TypeError, ValueError):
                self._interval_days = 20
            self._api_verify = bool(config.get("api_verify", True))
        if self._run_once:
            self._run_once = False
            self._save_config()
            add_once = getattr(scheduler_sdk, "add_plugin_once_job", None) if scheduler_sdk else None
            accepted = False
            if callable(add_once):
                try:
                    accepted = add_once(
                        self.__class__.__name__, "mteam_alive_once", self.keepalive,
                        "M-Team 保活（立即执行）", delay_seconds=3)
                except Exception:
                    logger.exception("M-Team 保活：登记一次性任务失败，改用本地 Timer")
            if accepted is False:
                timer = Timer(3, self.keepalive)
                timer.daemon = True
                self._run_once_timer = timer
                timer.start()

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "run_once": False,
            "cron": self._cron, "username": self._username,
            "password": self._password, "totp_secret": self._totp_secret,
            "api_key": self._api_key,
            "ratio_warn": self._ratio_warn, "verify_minutes": self._verify_minutes,
 "api_verify": self._api_verify,
            "interval_days": self._interval_days,
        })

    @staticmethod
    def _site_oper() -> Any:
        """返回 MoviePilot 站点操作对象；旧版本路径仅作兼容。"""
        try:
            from app.db.oper.site import SiteOper
        except Exception:
            try:
                from app.db.site_oper import SiteOper
            except Exception:
                return None
        try:
            return SiteOper()
        except Exception:
            return None

    def site_url(self) -> str:
        """从 MoviePilot 站点管理读取 M-Team 站点地址。"""
        oper = self._site_oper()
        if oper is None:
            return str(self._site_url or DEFAULT_SITE).rstrip("/")
        try:
            sites = oper.list_order_by_pri()
        except Exception:
            sites = []
        for site in sites or []:
            domain = str(getattr(site, "domain", "") or getattr(site, "url", "") or "").lower()
            if "m-team" not in domain:
                continue
            value = str(getattr(site, "url", "") or "").strip().rstrip("/")
            if value:
                self._site_url = value
                return value
        return str(self._site_url or DEFAULT_SITE).rstrip("/")

    def site_apikey(self) -> str:
        """从 MoviePilot 站点管理读取 M-Team API Key。"""
        oper = self._site_oper()
        if oper is None:
            return ""
        try:
            sites = oper.list_order_by_pri()
        except Exception:
            return ""
        for site in sites or []:
            domain = str(getattr(site, "domain", "") or getattr(site, "url", "") or "").lower()
            if "m-team" not in domain:
                continue
            for attr in ("apikey", "api_key"):
                value = str(getattr(site, attr, "") or "").strip()
                if value:
                    return value
        return ""

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令：/mteam_keepalive 立即保活一次。"""
        return [{
            "cmd": "/mteam_keepalive",
            "event": EventType.PluginAction,
            "desc": "立即执行一次 M-Team 保活",
            "category": "插件命令",
            "data": {"action": "mteam_keepalive"},
        }]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """处理插件命令事件（只认自己的 action）。"""
        payload = (getattr(event, "event_data", None) or {})
        if str(payload.get("action") or "") != "mteam_keepalive":
            return
        record = self.keepalive()
        logger.info("M-Team 保活：命令执行完成，%s", record.get("message", ""))

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/keepalive",
            "endpoint": self.api_keepalive,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "立即执行 M-Team 保活",
            "description": "浏览器登录 M-Team 保活，并返回账号统计",
        }]

    def api_keepalive(self) -> Dict[str, Any]:
        record = self.keepalive()
        return {"success": bool(record.get("ok")), "message": record.get("message", "")}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error("M-Team 保活：Cron 表达式无效：%s", self._cron)
            return []
        return [{"id": f"{self.__class__.__name__}.Keepalive", "name": "M-Team 保活",
                 "trigger": trigger, "func": self.scheduled_keepalive, "kwargs": {}}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def field(kind: str, model: str, label: str, md: int = 6, **props: Any) -> Dict[str, Any]:
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": kind,
                                 "props": {"model": model, "label": label, **props}}]}

        def row(*cols: Dict[str, Any]) -> Dict[str, Any]:
            return {"component": "VRow", "content": list(cols)}

        def section(title: str, icon: str) -> Dict[str, Any]:
            """紧凑分区标题。"""
            return {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12},
                    "content": [{"component": "VChip",
                                 "props": {"size": "small", "variant": "tonal", "color": "primary",
                                           "prepend-icon": icon}, "text": title}]}]}

        return [{"component": "VForm", "content": [
            row(field("VSwitch", "enabled", "启用插件", 4),
                field("VSwitch", "notify", "发送通知", 4),
                field("VSwitch", "run_once", "保存后立即执行一次", 4)),
            section("账户", "mdi-account-key-outline"),
            row(field("VTextField", "username", "M-Team 账号", 4),
                field("VTextField", "password", "密码", 4, type="password"),
                field("VTextField", "totp_secret", "TOTP 密钥", 4,
                      hint="32 位；未开启 2FA 可留空",
                      **{"persistent-hint": True})),
            section("执行", "mdi-clock-outline"),
            row(field("VTextField", "interval_days", "执行间隔（天）", 4,
                      type="number", min=1, max=60, hint="默认 20 天",
                      **{"persistent-hint": True}),
                field("VCronField", "cron", "检查时间", 4,
                      placeholder="0 9 * * *", hint="默认每天 9 点",
                      **{"persistent-hint": True}),
                field("VTextField", "ratio_warn", "分享率提醒阈值", 4,
                      type="number", min=0, step=1, hint="默认 7",
                      **{"persistent-hint": True})),
            section("校验", "mdi-shield-check-outline"),
            row(field("VSwitch", "api_verify", "用「最近动向」校验登录", 6,
                      hint="关闭后仅按浏览器结果判定",
                      **{"persistent-hint": True}),
                field("VTextField", "verify_minutes", "校验窗口（分钟）", 6,
                      type="number", min=1, max=180, hint="默认 10 分钟",
                      **{"persistent-hint": True})),
            row({"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                                 "density": "compact"},
                "text": "M-Team 连续 40 天未登录会删号，只有真实网页登录才重置计时。"
            }]}),
        ]}], {
            "enabled": self._enabled, "notify": self._notify, "run_once": False,
            "cron": self._cron, "username": self._username,
            "password": self._password, "totp_secret": self._totp_secret,
            "api_key": self._api_key,
            "ratio_warn": self._ratio_warn, "verify_minutes": self._verify_minutes,
            "api_verify": self._api_verify, "interval_days": self._interval_days,
        }

    # ------------------------------------------------------------------ 保活
    def scheduled_keepalive(self) -> Dict[str, Any]:
        """定时入口：距上次成功保活不足「执行间隔（天）」就跳过。"""
        last = self.get_data("last") or {}
        if last.get("ok") and last.get("time"):
            try:
                done_at = datetime.strptime(str(last["time"]), "%Y-%m-%d %H:%M:%S")
                gap = (datetime.now() - done_at).total_seconds() / 86400
                if gap < self._interval_days:
                    logger.info("M-Team 保活：距上次成功 %.1f 天，未满 %s 天，本次跳过",
                                gap, self._interval_days)
                    return dict(last)
            except ValueError:
                pass
        return self.keepalive()

    def keepalive(self) -> Dict[str, Any]:
        """执行一次保活：浏览器登录 + 读统计 + 记历史 + 通知。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("M-Team 保活：开始浏览器登录（账号 %s，每次真实登录）", self._username)
        site_url = self.site_url()
        api_key = self.site_apikey()
        before_profile: Dict[str, Any] = {}
        if api_key:
            before_profile, _ = fetch_profile(api_key, self._api_base)
        ok, message = keepalive_login(
            self._username, self._password, self._totp_secret,
            site_url=site_url, logger=logger)
        logger.info("M-Team 保活：浏览器登录结果 - %s", message)
        profile: Dict[str, Any] = {}
        if api_key:
            logger.info("M-Team 保活：使用 MoviePilot 站点管理里 m-team 的 API Key")
        else:
            logger.info("M-Team 保活：站点管理里没有 m-team 的 API Key，跳过统计与最近动向校验")
        if not self._api_verify:
            logger.info("M-Team 保活：已关闭 API 校验，仅按浏览器结果判定")
        if api_key and self._api_verify:
            profile, error = fetch_profile(api_key, self._api_base)
            if error:
                logger.warning("M-Team 保活：读取统计失败 - %s", error)
                profile = {}
        # 配了 API Key 就以「最近动向」为准：登录完 lastBrowse 必须是刚刚。
        # localStorage 里留着旧 token 也能被这一步揪出来（浏览器看着"登录成功"其实没真登录）。
        if profile:
            minutes = activity_minutes_ago(profile)
            moment, label = last_activity(profile)
            shown = format_activity(profile) if moment else "未读到"
            browser_message = message
            before_time = parse_datetime(str(before_profile.get("last_browse")
                                            or before_profile.get("last_login") or ""))
            after_time = moment
            changed = (before_time is None and after_time is not None) or (
                before_time is not None and after_time is not None and after_time > before_time)
            if minutes is None:
                message = f"{browser_message}；读不到最近动向，无法二次确认"
            elif changed and minutes <= self._verify_minutes:
                ok = True
                message = f"登录成功（{label} {shown}）"
            elif not changed:
                # 站点时间没变 = 这次并没有真的产生新访问（可能被自己的手动浏览掩盖）
                earlier = before_time.strftime("%Y-%m-%d %H:%M:%S") if before_time else "（未知）"
                ok = False
                message = (f"浏览器侧：{browser_message}｜站点侧：{label}没有变新"
                           f"（执行前 {earlier}，执行后 {shown}），判定本次没真正登录")
            else:
                ok = False
                message = (f"浏览器侧：{browser_message}｜站点侧：{label}变成了 {shown}，"
                           f"但距现在 {minutes:.0f} 分钟，超过 {self._verify_minutes} 分钟窗口")
        record = {"time": now, "ok": ok, "message": message,
                  "ratio": (profile or {}).get("ratio", ""),
                  "bonus": (profile or {}).get("bonus", "")}
        history = list(self.get_data("history") or [])
        history.insert(0, record)
        self.save_data("history", history[:HISTORY_LIMIT])
        if profile:
            self.save_data("profile", dict(profile, updated=now))
        self.save_data("last", record)
        if ok:
            logger.info("M-Team 保活：%s", message)
        else:
            logger.warning("M-Team 保活失败：%s", message)
        self._notify_result(record, profile)
        return record

    def _notify_result(self, record: Dict[str, Any], profile: Dict[str, Any]) -> None:
        if not self._notify:
            return
        lines = [("保活成功" if record.get("ok") else "保活失败") + f" · {record.get('time', '')[5:16]}"]
        if profile:
            detail = []
            if profile.get("last_browse") or profile.get("last_login"):
                detail.append(f"最近动向 {format_activity(profile)}")
            if profile.get("ratio"):
                detail.append(f"分享率 {profile['ratio']}")
            if profile.get("bonus"):
                detail.append(f"魔力 {profile['bonus']}")
            if profile.get("uploaded"):
                detail.append(f"上传 {profile['uploaded']}")
            if profile.get("downloaded"):
                detail.append(f"下载 {profile['downloaded']}")
            if detail:
                lines.append(" · ".join(detail))
            ratio = profile.get("ratio_value")
            if ratio is not None and ratio < self._ratio_warn:
                lines.append(f"⚠ 分享率低于 {self._ratio_warn}，注意保号")
        if not record.get("ok"):
            lines.append("原因：" + str(record.get("message") or "未知"))
        self.post_message(mtype=NotificationType.Plugin,
                          title="M-Team 保活", text="\n".join(lines))

    # ------------------------------------------------------------------ 页面
    def get_page(self) -> List[dict]:
        history = list(self.get_data("history") or [])
        profile = self.get_data("profile") or {}
        last = self.get_data("last") or {}
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                     "text": "还没有保活记录；保存配置后点「保存后立即执行一次」，或等 Cron 触发。"}}]

        def stat(label: str, value: str, tone: str = "") -> Dict[str, Any]:
            tone = tone or ("ok" if value and value != "—" else "grey")
            return {"component": "div", "props": {"class": "mteam-stat"}, "content": [
                {"component": "div",
                 "props": {"class": f"mteam-stat-num mteam-num-{tone}"}, "text": value or "—"},
                {"component": "div", "props": {"class": "mteam-stat-label"}, "text": label}]}

        ratio = str(profile.get("ratio") or "")
        ratio_value = profile.get("ratio_value")
        ratio_tone = "ok"
        if isinstance(ratio_value, (int, float)) and ratio_value < self._ratio_warn:
            ratio_tone = "warn"
        body: List[dict] = [{
            "component": "div", "props": {"class": "mteam-stats"}, "content": [
                stat("分享率", ratio, ratio_tone),
                stat("魔力", str(profile.get("bonus") or "")),
                stat("上传", str(profile.get("uploaded") or "")),
                stat("下载", str(profile.get("downloaded") or "")),
            ]}]
        info = []
        if not self._api_key and not profile:
            info.append("未读到账号统计（站点管理里也没有 m-team 的 API Key）")
        if profile.get("username"):
            info.append(f"账号 {profile['username']}")
        if profile.get("last_browse") or profile.get("last_login"):
            info.append(f"最近动向 {format_activity(profile)}")
        if profile.get("last_login"):
            info.append(f"上次登录 {profile['last_login']}")
        if profile.get("updated"):
            info.append(f"统计更新 {str(profile['updated'])[5:16]}")
        if info:
            body.append({"component": "div", "props": {"class": "mteam-line"},
                         "text": " · ".join(info)})
        body.append({"component": "div", "props": {"class": "mteam-section"},
                     "content": [{
                         "component": "VChip",
                         "props": {"size": "small", "variant": "tonal", "color": "primary",
                                   "prepend-icon": "mdi-shield-check-outline"},
                         "text": f"保活记录（{len(history)}）"}]})
        rows = [{"component": "div", "props": {"class": "mteam-row"}, "content": [
            {"component": "div", "props": {"class": "mteam-row-head"}, "content": [
                {"component": "div", "props": {"class": "mteam-row-time"},
                 "text": str(item.get("time") or "")},
                {"component": "div", "props": {"class": "mteam-row-status"}, "content": [{
                    "component": "VChip",
                    "props": {"size": "x-small", "variant": "tonal",
                              "color": "success" if item.get("ok") else "error",
                              "prepend-icon": "mdi-check-circle" if item.get("ok")
                              else "mdi-alert-circle"},
                    "text": "成功" if item.get("ok") else "失败"}]}]},
            {"component": "div", "props": {"class": "mteam-row-msg"},
             "text": str(item.get("message") or "")},
        ]} for item in history[:10]]
        body.extend(rows)
        if last.get("time"):
            body.append({"component": "div", "props": {"class": "mteam-line"},
                         "text": f"最近一次：{last.get('time')} · "
                                 f"{'成功' if last.get('ok') else '失败'}"})
        return [{"component": "style", "text": PAGE_STYLE},
                {"component": "div", "props": {"class": "mteam-page"}, "content": body}]

    def stop_service(self) -> None:
        """停用或重载时取消本实例的一次性任务并释放资源。"""
        self._enabled = False
        timer = getattr(self, "_run_once_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                logger.warning("M-Team 保活：取消立即执行定时器时出错")
            self._run_once_timer = None
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None) if scheduler_sdk else None
        if callable(remove_once):
            try:
                remove_once(self.__class__.__name__, "mteam_alive_once")
            except Exception:
                logger.debug("M-Team 保活：宿主立即执行任务已执行或不存在")
