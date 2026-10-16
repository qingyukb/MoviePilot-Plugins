"""PT 站点签到插件（MoviePilot V2/V3）

双引擎：
  1. HTTP 引擎 —— 直接请求站点签到接口（如 NexusPHP attendance.php），轻量快速；
  2. Browserless 引擎 —— 通过 Browserless BrowserQL 打开真实浏览器完成
     图片验证码、Cloudflare / Turnstile 挑战、弹窗签到与 Altcha 验证。

引擎默认按站点规则自动判定；未显式声明引擎的站点在 HTTP 直连撞上 Cloudflare
或验证码时，会自动改用 Browserless 重试一次（可在配置中关闭）。

参考 tmzg0000/moviepilot-plugins 的插件规范编写。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, Timer
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase

try:  # MoviePilot V3
    from app.db.oper.site import SiteOper
except ImportError:  # MoviePilot V2
    from app.db.site_oper import SiteOper

try:  # MoviePilot V3 日志门面
    from app.sdk.logging import logger
except ImportError:  # MoviePilot V2
    from app.log import logger

from app.schemas import NotificationType

from .core import (
    DEFAULT_BROWSERLESS_URL,
    BrowserlessSigner,
    SignResult,
    fallback_rule,
    http_sign,
    parse_rules,
    resolve_rule,
    site_state_key,
    target_url,
)


class PTSiteSignIn(_PluginBase):
    plugin_name = "PT站点签到"
    plugin_desc = "双引擎 PT 签到：HTTP 直连签到接口，或经 Browserless 完成验证码 / Cloudflare / Turnstile 挑战。"
    plugin_icon = "signin.png"
    plugin_version = "1.0.10"
    plugin_author = "qingyu"
    author_url = "https://blog.qyus.cn"
    plugin_config_prefix = "ptsignin_"
    plugin_order = 29
    auth_level = 2

    _enabled = False
    _notify_mode = "always"
    _cron = "25 8 * * *"
    _site_ids: List[int] = []
    _concurrency = 2
    _timeout = 30
    _retry_delay_minutes = 10
    _retry_count = 1
    _proxy = False
    _fallback_browserless = True
    _browserless_url = DEFAULT_BROWSERLESS_URL
    _browserless_token = ""
    _browserless_timeout = 100
    _site_rules = ""
    _run_once = False
    _run_once_scheduler: Optional[BackgroundScheduler] = None

    # 进程级互斥：Cron 与「保存后立即执行一次」可能同时触发，避免同一轮重复签到 /
    # 并发覆盖 latest、history 数据。失败重试由后台 Timer 延迟执行，不再阻塞执行线程。
    _run_lock: Lock = Lock()
    _history_days = 30
    _stop_event: Optional[Event] = None
    _retry_timer: Optional[Timer] = None
    _run_id = 0

    def init_plugin(self, config: Optional[dict] = None) -> None:
        self.stop_service()
        self._stop_event = Event()
        self._retry_timer = None
        # 新生命周期会让上一轮尚未触发的重试全部失效。
        self._run_id = getattr(self, "_run_id", 0) + 1
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify_mode = self._parse_notify_mode(config)
        self._cron = str(config.get("cron") or "25 8 * * *")
        self._site_ids = [int(v) for v in (config.get("site_ids") or []) if str(v).isdigit()]
        self._concurrency = max(1, min(int(config.get("concurrency") or 2), 5))
        self._timeout = max(5, min(int(config.get("timeout") or 30), 120))
        retry_count = config.get("retry_count")
        self._retry_count = max(0, min(int(1 if retry_count in (None, "") else retry_count), 5))
        self._retry_delay_minutes = max(1, min(int(config.get("retry_delay_minutes") or 10), 1440))
        self._proxy = bool(config.get("proxy", False))
        self._fallback_browserless = bool(config.get("fallback_browserless", True))
        self._browserless_url = str(config.get("browserless_url") or DEFAULT_BROWSERLESS_URL).strip()
        self._browserless_token = str(config.get("browserless_token") or "").strip()
        self._browserless_timeout = max(15, min(int(config.get("browserless_timeout") or 100), 300))
        self._site_rules = str(config.get("site_rules") or "")
        self._run_once = bool(config.get("run_once", False))
        if self._run_once:
            self._run_once = False
            self.update_config(self._config())
            self._start_run_once()

    def _start_run_once(self) -> None:
        """「保存后立即执行一次」：延后几秒交给后台调度器执行，不阻塞保存配置的请求。

        与官方插件（alidnsddns、hdbluesignin）做法一致——不在 init_plugin 里同步跑长任务，
        否则 MP 保存配置的请求会被签到（尤其是失败重试的等待）一直占住，表现为设置页卡死。
        """
        try:
            scheduler = BackgroundScheduler()
            scheduler.add_job(func=self._run_once_task, trigger="date",
                              run_date=datetime.now() + timedelta(seconds=3),
                              name="PT站点签到 立即执行一次")
            scheduler.start()
        except Exception:
            logger.exception("PT站点签到：后台调度器启动失败，改用线程执行")
            Thread(target=self._run_once_task, daemon=True).start()
            return
        self._run_once_scheduler = scheduler
        logger.info("PT站点签到：保存后立即执行一次，将在 3 秒后于后台开始")

    def _run_once_task(self) -> None:
        """后台任务：只跑一遍签到，跳过失败重试。"""
        try:
            self.run_sign_in(allow_retry=False)
        except Exception:
            logger.exception("PT站点签到：保存后立即执行失败")

    def _config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify_mode": self._notify_mode,
            "cron": self._cron,
            "site_ids": self._site_ids,
            "concurrency": self._concurrency,
            "timeout": self._timeout,
            "retry_count": self._retry_count,
            "retry_delay_minutes": self._retry_delay_minutes,
            "proxy": self._proxy,
            "fallback_browserless": self._fallback_browserless,
            "browserless_url": self._browserless_url,
            "browserless_token": self._browserless_token,
            "browserless_timeout": self._browserless_timeout,
            "site_rules": self._site_rules,
            "run_once": self._run_once,
        }

    @staticmethod
    def _parse_notify_mode(config: Dict[str, Any]) -> str:
        """通知模式：always 每次通知 / failed 仅失败通知 / off 不通知。"""
        mode = str(config.get("notify_mode") or "").lower()
        if mode in {"always", "failed", "off"}:
            return mode
        # 兼容 1.2.x 的布尔开关 notify
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
            logger.error("PT站点签到：Cron 表达式无效：%s", self._cron)
            return []
        return [{"id": "PTSiteSignIn.Run", "name": "PT站点签到", "trigger": trigger,
                 "func": self.run_sign_in, "kwargs": {}}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        try:
            sites = SiteOper().list_order_by_pri()
        except Exception:
            sites = []
        options = [{"title": site.name, "value": site.id}
                   for site in sites if getattr(site, "cookie", None)]
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
                 "content": [{"component": "VSwitch", "props": {"model": "proxy", "label": "HTTP 引擎使用代理"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSwitch", "props": {"model": "fallback_browserless",
                             "label": "遇验证码 / CF 自动用浏览器",
                             "hint": "内置预设或未声明引擎的站点遇盾时生效；手工在规则里写了 engine 则不生效，需填写 Browserless Token", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "保存后立即执行一次"}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VCronField", "props": {"model": "cron", "label": "执行周期",
                             "placeholder": "5 位 Cron；例如 25 8 * * *"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VSelect", "props": {"model": "site_ids", "label": "签到站点",
                             "items": options, "multiple": True, "chips": True,
                             "hint": "仅显示已在 MoviePilot 保存 Cookie 的站点", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VSelect", "props": {"model": "notify_mode", "label": "通知模式",
                             "items": notify_items,
                             "hint": "签到结束后通过 MoviePilot 通知渠道推送结果", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {"model": "concurrency", "label": "并发数",
                             "type": "number", "min": 1, "max": 5,
                             "hint": "建议 2；Browserless 为付费额度，建议 1–2", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3},
                 "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "HTTP 超时（秒）",
                             "type": "number", "min": 5, "max": 120, "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "retry_count",
                             "label": "失败重试次数", "type": "number", "min": 0, "max": 5,
                             "hint": "0 为关闭，默认 1 次", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "retry_delay_minutes",
                             "label": "失败后等待分钟数", "type": "number", "min": 1, "max": 1440,
                             "hint": "仅失败站点重试前等待；默认 10 分钟", "persistent-hint": True}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-4"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact"},
             "content": [{"component": "span", "text": "引擎按站点规则自动判定：需要图片验证码、Cloudflare / Turnstile 挑战、弹窗签到或 Altcha 的站点自动走 Browserless（内置预设已声明，也可在下方自定义规则中用 engine 指定），其余站点一律走 HTTP 直连、不消耗额度；HTTP 直连撞上 Cloudflare 或验证码时会自动改用浏览器引擎重试一次（手工在自定义规则里写了 engine 的站点除外，可在上方关闭）；内置的 Cloudflare「Just a moment...」交互挑战会在浏览器里等待自动放行，默认 10 秒。首次使用 Browserless 请先 "},
                         {"component": "a", "props": {"href": "https://www.browserless.io/signup/email?plan=free",
                                                      "target": "_blank", "rel": "noopener noreferrer"},
                          "text": "注册 Browserless 账号"},
                         {"component": "span", "text": "，在 "},
                         {"component": "a", "props": {"href": "https://browserless.io/account/",
                                                      "target": "_blank", "rel": "noopener noreferrer"},
                          "text": "账户控制台"},
                         {"component": "span", "text": " 的 API Key 区域复制 Token 填入下方。Token 不会写入日志。"}]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "browserless_url",
                             "label": "Browserless 地址", "placeholder": DEFAULT_BROWSERLESS_URL,
                             "hint": "可填服务根地址或完整 /stealth/bql 地址", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "browserless_token",
                             "label": "Browserless Token", "type": "password",
                             "hint": "仅在使用 Browserless 引擎时需要", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6},
                 "content": [{"component": "VTextField", "props": {"model": "browserless_timeout",
                             "label": "Browserless 超时（秒）", "type": "number", "min": 15, "max": 300,
                             "hint": "含验证码求解时间，建议 100–180", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12},
                 "content": [{"component": "VTextarea", "props": {"model": "site_rules",
                             "label": "自定义站点规则 JSON（可留空）", "rows": 10,
                             "hint": '键为域名或站点 ID。HTTP：{"path":"/attendance.php","success":"正则","already":"正则"}；'
                                     'Browserless：{"engine":"browserless","mode":"cloudflare|image|trigger_image|altcha|open_page", ...}；'
                                     'Cloudflare「Just a moment...」交互挑战默认在页面等待 10 秒自动放行，可用 {"cloudflare_wait": 20} 调大',
                             "persistent-hint": True}}]},
            ]},
        ]}], self._config()

    def get_page(self) -> List[dict]:
        records = self.get_data("latest") or []
        if not isinstance(records, list):
            records = []
        records = [row for row in records if isinstance(row, dict)]
        total = len(records)
        success = sum(row.get("status") in {"success", "already"} for row in records)
        failed = total - success
        if not records:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                     "text": "暂无签到记录；保存配置后使用「保存后立即执行一次」开始签到。"}}]
        rows = []
        for row in records:
            meta = self._status_meta(str(row.get("status") or ""))
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(row.get("site") or "未知站点")},
                {"component": "td", "text": "浏览器" if row.get("engine") == "browserless" else "HTTP"},
                {"component": "td", "content": [{"component": "VChip", "props": {
                    "size": "x-small", "variant": "tonal", "color": meta["color"],
                    "prepend-icon": meta["icon"]}, "text": meta["label"]}]},
                {"component": "td", "text": str(row.get("ran_at") or "-")},
                {"component": "td", "text": str(row.get("message") or "-")},
            ]})
        return [
            {"component": "style", "text":
             ".ptsignin-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}"
             ".ptsignin-stat{padding:12px}.ptsignin-stat__label{color:rgba(var(--v-theme-on-surface),.62);font-size:.78rem}"
             ".ptsignin-stat__value{margin-top:6px;font-size:1.35rem;font-weight:700}"
             ".ptsignin-table-wrap{overflow-x:auto;border:1px solid rgba(var(--v-theme-on-surface),.08);border-radius:8px}"},
            {"component": "div", "props": {"class": "ptsignin-summary"}, "content": [
                self._stat("本次站点", str(total), "已选择并完成处理", "info"),
                self._stat("成功或已签到", f"{success}/{total}", "无需重复签到", "success"),
                self._stat("失败", str(failed), "请查看失败原因或更新 Cookie", "error" if failed else "success"),
            ]},
            {"component": "div", "props": {"class": "ptsignin-table-wrap mt-3"},
             "content": [{"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                 {"component": "thead", "content": [{"component": "tr", "content": [
                     {"component": "th", "text": "站点"}, {"component": "th", "text": "方式"},
                     {"component": "th", "text": "状态"}, {"component": "th", "text": "时间"},
                     {"component": "th", "text": "结果"}]}]},
                 {"component": "tbody", "content": rows},
             ]}]},
        ]

    @staticmethod
    def _status_meta(status: str) -> Dict[str, str]:
        if status in {"success", "already"}:
            return {"label": "签到成功", "color": "success", "icon": "mdi-check-circle"}
        return {"label": "签到失败", "color": "error", "icon": "mdi-alert-circle"}

    @staticmethod
    def _stat(label: str, value: str, hint: str, color: str) -> Dict[str, Any]:
        return {"component": "div",
                "props": {"class": "ptsignin-stat app-card-shell app-card-colorful",
                          "style": f"--app-card-accent-rgb: var(--v-theme-{color});"},
                "content": [
                    {"component": "div", "props": {"class": "ptsignin-stat__label"}, "text": label},
                    {"component": "div", "props": {"class": f"ptsignin-stat__value text-{color}"}, "text": value},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "text": hint},
                ]}

    # ---------------- 签到核心 ----------------

    @staticmethod
    def _site_dict(site: Any) -> Dict[str, Any]:
        return {key: getattr(site, key, None)
                for key in ("id", "name", "url", "cookie", "ua", "proxy", "rule")}

    def _should_init_signer(self, infos: List[Dict[str, Any]],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """按最终生效的站点规则判断是否需要初始化 Browserless 引擎。

        两类站点需要浏览器：① 规则显式声明 engine=browserless；
        ② 未声明引擎且开启自动回退的 HTTP 站点（遇盾 / 验证码时会改用浏览器）。
        """
        for info in infos:
            try:
                rule = resolve_rule(info, rules)
            except ValueError:
                continue
            if str(rule.get("engine")) == "browserless":
                return True
            if self._fallback_browserless and rule.get("auto_fallback"):
                return True
        return False

    def _sign_one(self, info: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                  signer: Optional[BrowserlessSigner]) -> Dict[str, str]:
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info("PT站点签到：开始 %s", site_name)
        engine = "http"
        try:
            rule = resolve_rule(info, rules)
            engine = str(rule["engine"])
            if engine == "browserless":
                if signer is None:
                    outcome = SignResult("failed", "未配置 Browserless 地址或 Token")
                else:
                    outcome = signer.sign(info, rule)
            else:
                outcome = http_sign(info, rule, timeout=self._timeout,
                                    proxies=self._proxies() if self._proxy else None)
                if outcome.status == "failed" and outcome.challenge:
                    outcome, engine = self._maybe_fallback(info, rule, outcome, signer)
        except ValueError as error:
            outcome = SignResult("failed", str(error))
        except Exception:
            logger.exception("PT站点签到失败：%s", site_name)
            outcome = SignResult("failed", "签到执行出现未预期错误")
        if outcome.status in {"success", "already"}:
            logger.info("PT站点签到：%s %s", site_name, outcome.message)
        else:
            logger.warning("PT站点签到：%s 失败 - %s", site_name, outcome.message)
        return {"site": site_name, "site_id": str(info.get("id") or ""),
                "state_key": site_state_key(info), "engine": engine,
                "status": outcome.status, "message": outcome.message, "ran_at": self._now()}

    def _maybe_fallback(self, info: Dict[str, Any], rule: Dict[str, Any], outcome: SignResult,
                        signer: Optional[BrowserlessSigner]) -> Tuple[SignResult, str]:
        """HTTP 直连撞上 Cloudflare / 验证码时，自动改用 Browserless 重试一次。

        只对未显式声明引擎的站点生效（rule 的 auto_fallback），并可在配置里整体关闭。
        返回（结果, 实际使用的引擎）。
        """
        if not (self._fallback_browserless and rule.get("auto_fallback")):
            return outcome, "http"
        label = "Cloudflare 验证" if outcome.challenge == "cloudflare" else "验证码"
        if signer is None:
            return SignResult("failed", f"{outcome.message}；检测到{label}，但未配置 Browserless 地址或 Token"), "http"
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info("PT站点签到：%s 检测到%s，自动改用 Browserless 重试", site_name, label)
        try:
            fallback = fallback_rule(rule, outcome.challenge)
        except ValueError as error:
            return SignResult("failed", f"{outcome.message}；回退规则无效：{error}"), "http"
        retry = signer.sign(info, fallback)
        if retry.status in {"success", "already"}:
            return SignResult(retry.status, f"{retry.message}（已自动改用浏览器引擎通过{label}）"), "browserless"
        return SignResult("failed", f"{outcome.message}；浏览器引擎重试未通过：{retry.message}"), "browserless"

    @staticmethod
    def _proxies() -> Optional[Dict[str, str]]:
        try:
            from app.core.config import settings
            host = settings.PROXY_HOST or ""
        except Exception:
            host = ""
        return {"http": host, "https": host} if host else None

    def _run_batch(self, infos: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
                   signer: Optional[BrowserlessSigner]) -> List[Dict[str, str]]:
        workers = min(self._concurrency, len(infos))
        if workers <= 1:
            return [self._sign_one(info, rules, signer) for info in infos]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(lambda info: self._sign_one(info, rules, signer), infos))

    def run_sign_in(self, allow_retry: bool = True) -> None:
        """执行一轮签到。

        Cron 与「保存后立即执行一次」可能同时触发，用进程级互斥锁保证同一时间
        只有一轮在跑；失败重试交给后台 Timer，等待期间不再占用执行线程。
        allow_retry=False 时跳过失败重试（保存后立即执行用）。
        """
        if not PTSiteSignIn._run_lock.acquire(blocking=False):
            logger.warning("PT站点签到：上一轮仍在执行，本次跳过，避免重复签到")
            return
        try:
            self._run_sign_in_locked(allow_retry)
        finally:
            PTSiteSignIn._run_lock.release()

    def _run_sign_in_locked(self, allow_retry: bool) -> None:
        self._run_id += 1
        run_id = self._run_id
        try:
            all_sites = {site.id: site for site in SiteOper().list_order_by_pri()}
        except Exception as error:
            self._finish([{"site": "配置", "status": "failed", "engine": "-",
                           "message": f"读取站点失败：{error}", "ran_at": self._now()}])
            return
        selected = [all_sites[sid] for sid in self._site_ids if sid in all_sites]
        if not selected:
            self._finish([{"site": "配置", "status": "failed", "engine": "-",
                           "message": "未选择可用的 MoviePilot 站点", "ran_at": self._now()}])
            return
        try:
            rules = parse_rules(self._site_rules)
        except ValueError as error:
            self._finish([{"site": "配置", "status": "failed", "engine": "-",
                           "message": str(error), "ran_at": self._now()}])
            return

        infos = [self._site_dict(site) for site in selected]
        signer: Optional[BrowserlessSigner] = None
        if self._should_init_signer(infos, rules):
            if not self._browserless_token or not self._browserless_url:
                logger.warning("PT站点签到：未配置 Browserless 地址或 Token，相关站点将失败")
            else:
                try:
                    signer = BrowserlessSigner(self._browserless_url, self._browserless_token,
                                               self._browserless_timeout)
                except ValueError as error:
                    self._finish([{"site": "配置", "status": "failed", "engine": "-",
                                   "message": str(error), "ran_at": self._now()}])
                    return

        results = self._run_batch(infos, rules, signer)
        result_by_key = {r.get("state_key") or r.get("site"): r for r in results}
        retry_times = self._retry_count if allow_retry else 0
        failed_infos = [info for info, result in zip(infos, results)
                        if result.get("status") == "failed"]
        if not failed_infos or retry_times <= 0:
            self._finish(list(result_by_key.values()))
            return
        # 先落盘首轮结果；重试完成后以最终结果覆盖同一天历史并统一通知。
        self._save_results(list(result_by_key.values()))
        self._schedule_retry(1, run_id, infos, rules, signer, result_by_key)

    def _schedule_retry(self, attempt: int, run_id: int, infos: List[Dict[str, Any]],
                        rules: Dict[str, Dict[str, Any]], signer: Optional[BrowserlessSigner],
                        result_by_key: Dict[str, Dict[str, str]]) -> None:
        """用后台 Timer 延迟执行下一轮失败重试，避免 sleep 占用执行线程。"""
        if attempt > self._retry_count:
            self._finish(list(result_by_key.values()))
            return
        old_timer = getattr(self, "_retry_timer", None)
        if old_timer is not None:
            try:
                old_timer.cancel()
            except Exception:
                pass
        delay = max(1, self._retry_delay_minutes) * 60
        logger.info("PT站点签到：失败站点将在 %s 分钟后重试（第 %s/%s 次）",
                    self._retry_delay_minutes, attempt, self._retry_count)
        timer = Timer(delay, self._retry_task,
                      args=(attempt, run_id, infos, rules, signer, result_by_key))
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _retry_task(self, attempt: int, run_id: int, infos: List[Dict[str, Any]],
                    rules: Dict[str, Dict[str, Any]], signer: Optional[BrowserlessSigner],
                    result_by_key: Dict[str, Dict[str, str]]) -> None:
        event = getattr(self, "_stop_event", None)
        if event is not None and event.is_set():
            return
        if run_id != getattr(self, "_run_id", run_id):
            logger.info("PT站点签到：本轮重试已被更新的任务取代，跳过")
            return
        # 重试是后台线程；若刚好有新一轮在跑，最多等 5 分钟再决定是否放弃。
        if not PTSiteSignIn._run_lock.acquire(timeout=300):
            logger.warning("PT站点签到：执行线程繁忙，跳过第 %s 次失败重试", attempt)
            return
        try:
            if run_id != getattr(self, "_run_id", run_id) or (event is not None and event.is_set()):
                logger.info("PT站点签到：任务已停止或被新一轮取代，取消重试")
                return
            logger.info("PT站点签到：开始第 %s/%s 次失败重试（%s 个站点）",
                        attempt, self._retry_count,
                        sum(1 for info in infos
                            if result_by_key.get(site_state_key(info), {}).get("status") == "failed"))
            failed_infos = [info for info in infos
                            if result_by_key.get(site_state_key(info), {}).get("status") == "failed"]
            if not failed_infos:
                self._finish(list(result_by_key.values()))
                return
            for result in self._run_batch(failed_infos, rules, signer):
                result_by_key[result.get("state_key") or result.get("site")] = result
            still_failed = any(result_by_key.get(site_state_key(info), {}).get("status") == "failed"
                               for info in infos)
            if still_failed and attempt < self._retry_count:
                self._save_results(list(result_by_key.values()))
                self._schedule_retry(attempt + 1, run_id, infos, rules, signer, result_by_key)
                return
            self._finish(list(result_by_key.values()))
        except Exception:
            logger.exception("PT站点签到：失败重试出现未预期错误")
            try:
                self._finish(list(result_by_key.values()))
            except Exception:
                logger.exception("PT站点签到：重试异常后保存结果失败")
        finally:
            PTSiteSignIn._run_lock.release()

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _finish(self, results: List[Dict[str, str]]) -> None:
        results = self._save_results(results)
        signed = sum(item.get("status") == "success" for item in results)
        already = sum(item.get("status") == "already" for item in results)
        failed = len(results) - signed - already
        logger.info("PT站点签到：完成，新签到 %s，今日已签到 %s，失败 %s", signed, already, failed)
        self._notify(results, signed, already, failed)

    def _save_results(self, results: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """保存最新结果与当天历史（重试期间先落盘，最终结果会覆盖同一天）。"""
        ran_at = self._now()
        results = [dict(item, ran_at=item.get("ran_at") or ran_at)
                   for item in results if isinstance(item, dict)]
        self.save_data("latest", results)
        today_key = "history_" + datetime.now().strftime("%Y%m%d")
        self.save_data(today_key, results)
        self._prune_history(today_key)
        return results

    def _prune_history(self, today_key: str) -> None:
        """history_YYYYMMDD 只保留最近 _history_days 天，避免插件数据无限增长。"""
        index = self.get_data("history_index") or []
        keys = [str(key) for key in index if isinstance(key, str)] if isinstance(index, list) else []
        if today_key not in keys:
            keys.append(today_key)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        keep: List[str] = []
        stale: List[str] = []
        for key in keys:
            try:
                day = datetime.strptime(key, "history_%Y%m%d")
            except ValueError:
                continue
            (keep if day >= cutoff else stale).append(key)
        for key in stale:
            self._delete_data(key)
        self.save_data("history_index", sorted(set(keep)))

    def _delete_data(self, key: str) -> None:
        """兼容不同 MoviePilot 版本：优先 del_data，缺失时至少清空数据体。"""
        remover = getattr(self, "del_data", None)
        if callable(remover):
            try:
                remover(key)
                return
            except Exception:
                logger.warning("PT站点签到：删除历史数据 %s 失败，将改为清空", key)
        self.save_data(key, None)

    def _notify(self, results: List[Dict[str, str]], signed: int, already: int,
                failed: int) -> None:
        """按通知模式推送签到结果：always 每次通知 / failed 仅失败通知 / off 不通知。"""
        mode = self._notify_mode
        if mode == "off" or not results:
            return
        if mode == "failed" and not failed:
            return
        head = f"站点 {len(results)} 个｜新签到 {signed}｜今日已签到 {already}"
        if failed:
            head += f"｜失败 {failed}"
        detail = "\n".join(
            f"{item.get('site')}：{self._status_meta(item.get('status'))['label']}（{item.get('message')}）"
            for item in results)
        self.post_message(mtype=NotificationType.SiteMessage,
                          title="PT站点签到",
                          text=f"{head}\n\n{detail}")

    def stop_service(self) -> None:
        """插件停用 / 重载时停止重试，并清理自有的「立即执行」调度器。"""
        event = getattr(self, "_stop_event", None)
        if event is not None:
            event.set()
        timer = getattr(self, "_retry_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                logger.warning("PT站点签到：取消失败重试定时器时出错")
            self._retry_timer = None
        scheduler, self._run_once_scheduler = getattr(self, "_run_once_scheduler", None), None
        if scheduler is not None:
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception:
                logger.warning("PT站点签到：清理立即执行调度器时出错")
        return None
