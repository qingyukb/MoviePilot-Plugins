"""PT 站点签到插件（MoviePilot V2/V3）

双引擎：
  1. HTTP 引擎 —— 直接请求站点签到接口（如 NexusPHP attendance.php），轻量快速；
  2. Browserless 引擎 —— 通过 Browserless BrowserQL 打开真实浏览器完成
     图片验证码、Cloudflare / Turnstile 挑战、弹窗签到与 Altcha 验证。

参考 tmzg0000/moviepilot-plugins 的插件规范编写。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
    plugin_version = "1.4.2"
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
    _browserless_url = DEFAULT_BROWSERLESS_URL
    _browserless_token = ""
    _browserless_timeout = 100
    _site_rules = ""
    _run_once = False

    def init_plugin(self, config: Optional[dict] = None) -> None:
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
        self._browserless_url = str(config.get("browserless_url") or DEFAULT_BROWSERLESS_URL).strip()
        self._browserless_token = str(config.get("browserless_token") or "").strip()
        self._browserless_timeout = max(15, min(int(config.get("browserless_timeout") or 100), 300))
        self._site_rules = str(config.get("site_rules") or "")
        self._run_once = bool(config.get("run_once", False))
        if self._run_once:
            self._run_once = False
            self.update_config(self._config())
            self.run_sign_in()

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
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
                 "content": [{"component": "VSwitch", "props": {"model": "proxy", "label": "HTTP 引擎使用代理"}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4},
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
             "content": [{"component": "span", "text": "引擎按站点规则自动判定：需要图片验证码、Cloudflare / Turnstile 挑战、弹窗签到或 Altcha 的站点自动走 Browserless（内置预设已声明，也可在下方自定义规则中用 engine 指定），其余站点一律走 HTTP 直连、不消耗额度。首次使用 Browserless 请先 "},
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
                                     'Browserless：{"engine":"browserless","mode":"cloudflare|image|trigger_image|altcha|open_page", ...}',
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

    @staticmethod
    def _needs_browserless(infos: List[Dict[str, Any]],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """按最终生效的站点规则判断是否需要初始化 Browserless 引擎。"""
        for info in infos:
            try:
                if str(resolve_rule(info, rules).get("engine")) == "browserless":
                    return True
            except ValueError:
                continue
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

    def run_sign_in(self) -> None:
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
        if self._needs_browserless(infos, rules):
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
        for attempt in range(1, self._retry_count + 1):
            failed_infos = [info for info, result in zip(infos, results)
                            if result.get("status") == "failed"]
            if not failed_infos:
                break
            logger.info("PT站点签到：%s 个失败站点将在 %s 分钟后重试（第 %s/%s 次）",
                        len(failed_infos), self._retry_delay_minutes, attempt, self._retry_count)
            time.sleep(self._retry_delay_minutes * 60)
            for info, result in zip(failed_infos, self._run_batch(failed_infos, rules, signer)):
                result_by_key[result.get("state_key") or result.get("site")] = result
        self._finish(list(result_by_key.values()))

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _finish(self, results: List[Dict[str, str]]) -> None:
        ran_at = self._now()
        results = [dict(item, ran_at=item.get("ran_at") or ran_at)
                   for item in results if isinstance(item, dict)]
        self.save_data("latest", results)
        self.save_data("history_" + datetime.now().strftime("%Y%m%d"), results)
        signed = sum(item.get("status") == "success" for item in results)
        already = sum(item.get("status") == "already" for item in results)
        failed = len(results) - signed - already
        logger.info("PT站点签到：完成，新签到 %s，今日已签到 %s，失败 %s", signed, already, failed)
        self._notify(results, signed, already, failed)

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
        return None
