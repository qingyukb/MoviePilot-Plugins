"""M-Team 保活插件：定时用真实浏览器登录（账号密码 + TOTP），避免长期不登录被删号。

参考 KnowsWlf/MTeam-CLI 的做法：保活必须是真实登录（API ping 不重置「40 天不登录删号」），
登录页选择器 #username / #password / button[type=submit]，2FA 是 #otp-code。
账号统计走官方 API（API Key，不需要浏览器）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 官方 V3 规范：优先用稳定 SDK；V2 环境回退到旧路径
try:  # MoviePilot V3
    from app.sdk.plugin import _PluginBase  # type: ignore[no-redef]
except ImportError:  # V2
    from app.plugins import _PluginBase

try:
    from app.sdk.logging import logger
except ImportError:
    from app.log import logger

try:
    from app.sdk.config import settings
except ImportError:
    from app.core.config import settings

try:  # 一次性任务交给宿主调度器（V3）
    from app.sdk import scheduler as scheduler_sdk
except ImportError:
    scheduler_sdk = None

try:
    from app.sdk.events import Event, EventType, eventmanager
except ImportError:
    from app.core.event import Event, eventmanager
    from app.schemas.types import EventType

from app.schemas.types import NotificationType

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
    plugin_icon = "mteamalive.png"
    plugin_version = "1.3.4"
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
    _ratio_warn = 1.0
    _verify_minutes = 10
    _interval_days = 20
    _api_verify = True
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._run_once = bool(config.get("run_once"))
            self._cron = str(config.get("cron") or "0 9 * * *").strip()
            self._site_url = str(config.get("site_url") or DEFAULT_SITE).strip().rstrip("/")
            self._username = str(config.get("username") or "").strip()
            self._password = str(config.get("password") or "")
            self._totp_secret = str(config.get("totp_secret") or "").strip()
            self._api_key = str(config.get("api_key") or "").strip()
            self._api_base = str(config.get("api_base") or DEFAULT_API_BASE).strip().rstrip("/")
            try:
                self._ratio_warn = float(config.get("ratio_warn") or 1.0)
            except (TypeError, ValueError):
                self._ratio_warn = 1.0
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
            # 官方规范：一次性任务交给宿主调度器，不自建 BackgroundScheduler
            add_once = getattr(scheduler_sdk, "add_plugin_once_job", None) if scheduler_sdk else None
            if callable(add_once):
                try:
                    add_once(self.__class__.__name__, "mteam_alive_once", self.keepalive,
                             "M-Team 保活（立即执行）", delay_seconds=3)
                except Exception:
                    logger.exception("M-Team 保活：登记一次性任务失败")
            else:
                # 老版本没有该接口时退回本地调度器
                scheduler = BackgroundScheduler(timezone=settings.TZ)
                scheduler.add_job(func=self.keepalive, trigger="date",
                                  run_date=datetime.now(), name="M-Team 保活（立即执行）")
                scheduler.start()
                self._scheduler = scheduler

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "run_once": False,
            "cron": self._cron, "site_url": self._site_url, "username": self._username,
            "password": self._password, "totp_secret": self._totp_secret,
            "api_key": self._api_key, "api_base": self._api_base,
            "ratio_warn": self._ratio_warn, "verify_minutes": self._verify_minutes,
 "api_verify": self._api_verify,
            "interval_days": self._interval_days,
        })

    @staticmethod
    def site_apikey() -> str:
        """从 MoviePilot 站点管理里找 M-Team 的 API Key（站点里填过就不用再单独配）。"""
        try:
            from app.db.site_oper import SiteOper
        except Exception:
            try:
                from app.db.oper.site import SiteOper  # 兼容旧路径
            except Exception:
                return ""
        try:
            sites = SiteOper().list_order_by_pri()
        except Exception:
            return ""
        for site in sites or []:
            domain = str(getattr(site, "domain", "") or getattr(site, "url", "") or "").lower()
            if "m-team" not in domain:
                continue
            # 只认「令牌（API Key）」；站点管理里的 Authorization 是网页 JWT，不能当 API Key 用
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
        return [{"id": "MTeamKeepAlive.Keepalive", "name": "M-Team 保活",
                 "trigger": trigger, "func": self.scheduled_keepalive, "kwargs": {}}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def field(kind: str, model: str, label: str, md: int = 6, **props: Any) -> Dict[str, Any]:
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": kind,
                                 "props": {"model": model, "label": label, **props}}]}

        def hint(kind: str, model: str, label: str, text: str, md: int = 6,
                 **props: Any) -> Dict[str, Any]:
            return field(kind, model, label, md, hint=text,
                         **{"persistent-hint": True, **props})

        def row(*cols: Dict[str, Any]) -> Dict[str, Any]:
            return {"component": "VRow", "content": list(cols)}

        return [{"component": "VForm", "content": [
            row(field("VSwitch", "enabled", "启用插件", 4),
                hint("VSwitch", "notify", "发送通知", "每次保活结果都会推送，含分享率与魔力", 4),
                hint("VSwitch", "run_once", "保存后立即执行一次",
                     "保存后马上登录一次，验证账号密码与 TOTP 是否可用", 4)),
            row(field("VCronField", "cron", "执行周期",
                      placeholder="5 位 Cron；默认 0 9 * * *（每天 9 点）"),
                hint("VTextField", "interval_days", "执行间隔（天）",
                     "定时任务每天检查一次，距上次成功保活不满这么多天就跳过，默认 20 天",
                     type="number", min=1, max=60),
                hint("VTextField", "ratio_warn", "分享率提醒阈值",
                     "低于该值时通知里会带上提醒，默认 1.0", type="number", min=0, step=0.1)),
            row(hint("VSwitch", "api_verify", "用 API 校验最近浏览",
                     "关掉就只按浏览器登录结果判定，不读 API", md=4),
                hint("VTextField", "verify_minutes", "最近动向校验窗口（分钟）",
                     "登录后用 API 读「最近动向」（lastBrowse），在这个时间窗内才算保活成功，默认 10 分钟",
                     type="number", min=1, max=180)),
            row(field("VTextField", "site_url", "站点地址",
                      placeholder=DEFAULT_SITE,
                      hint="默认 https://kp.m-team.cc，也可填 m-team.io 镜像",
                      **{"persistent-hint": True}),
                hint("VTextField", "api_base", "API 地址",
                     "读取统计用，默认 https://api.m-team.cc", placeholder=DEFAULT_API_BASE)),
            row(field("VTextField", "username", "M-Team 账号", 4),
                field("VTextField", "password", "密码", 4, type="password"),
                field("VTextField", "totp_secret", "TOTP 密钥", 4,
                      hint="二次验证的 32 位密钥（去掉空格），用于自动填 2FA",
                      **{"persistent-hint": True})),
            row({"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                                 "density": "compact", "class": "mb-2"},
                "text": "API Key 不用填：统计与「最近动向」校验自动使用 MoviePilot 站点管理里 "
                        "m-team 站点的「令牌（API Key）」。"}]}),
            row({"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                                                 "density": "compact"},
                "text": "M-Team 连续 40 天不登录会删号，且只有「真实登录」才重置计时（API 访问不算）。"
                        "本插件用 MoviePilot 自带的浏览器在家宽 IP 上登录，请确保容器里有 Playwright。"}]}),
        ]}], {
            "enabled": self._enabled, "notify": self._notify, "run_once": False,
            "cron": self._cron, "site_url": self._site_url, "username": self._username,
            "password": self._password, "totp_secret": self._totp_secret,
            "api_key": self._api_key, "api_base": self._api_base,
            "ratio_warn": self._ratio_warn, "verify_minutes": self._verify_minutes,
 "api_verify": self._api_verify,
            "interval_days": self._interval_days,
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
        api_key = self.site_apikey()
        before_profile: Dict[str, Any] = {}
        if api_key:
            before_profile, _ = fetch_profile(api_key, self._api_base)
        ok, message = keepalive_login(
            self._username, self._password, self._totp_secret,
            site_url=self._site_url, logger=logger)
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

    def stop_service(self):
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is not None:
            try:
                scheduler.remove_all_jobs()
                scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
