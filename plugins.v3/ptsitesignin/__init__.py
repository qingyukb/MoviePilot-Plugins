"""PT 站点签到插件（MoviePilot V2/V3）

双引擎：
  1. HTTP 引擎 —— 直接请求站点签到接口（如 NexusPHP attendance.php），轻量快速；
  2. Browserless 引擎 —— 通过 Browserless BrowserQL 打开真实浏览器完成
     图片验证码、Cloudflare / Turnstile 挑战、弹窗签到与 Altcha 验证。

引擎默认按站点规则自动判定；未显式声明引擎的站点在 HTTP 直连撞上 Cloudflare
或验证码时，会自动改用 Browserless 重试一次（可在配置中关闭）。

站点登录凭证直接读 MoviePilot「站点管理」里该站的「附加信息」(note)；没有账号密码时
复用站点管理里保存的 Cookie。每次签到前先用账号密码真实登录站点、换回新的 Cookie 再
交给签到引擎；签到结束后再打开站点页面读取
站点展示的「最近动向」（含站点自带的相对时间，如 2026-10-16 23:44:21 ( < 1分前 )），
写入结果页与通知。

参考 tmzg0000/moviepilot-plugins 的插件规范编写。
"""

from __future__ import annotations

import importlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, Timer
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
    account_from_site,
    account_ready,
    fallback_rule,
    DEFAULT_INDEX_PATH,
    decode_entities,
    discover_signin_target,
    fetch_page,
    http_sign_auto,
    playwright_sign,
    run_site_module,
    is_login_record,
    is_signin_record,
    login as site_login,
    message_bonus,
    parse_rules,
    read_activity,
    record_meta,
    resolve_rule,
    site_record_counts,
    site_state_key,
    summarize_records,
    target_url,
)

def mask_proxy(host: str) -> str:
    """代理地址可能带账号密码，日志里把凭据打码。"""
    return re.sub(r"//[^@/]+@", "//***@", str(host or ""))


# ---------------------------------------------------------------- 常量
# 站点选择里的哨兵值：勾选它等于「全部站点」，并且以后新增的站点自动包含进来。
ALL_SITES = "all"
# 每个站点面板里最多展示多少条历史记录（历史本身按天保留最近 30 天）。
HISTORY_SHOW = 10
# 通知里每个失败原因最多显示多少字，避免长诊断把通知撑成一堵墙。
NOTIFY_REASON_LIMIT = 90      # 通知里每条失败原因最多这么长
NOTIFY_LIMIT = 6              # 通知里最多列几条失败
# 详情页样式（MoviePilot 插件页支持内联 style 组件）。
PAGE_STYLE = (
    # 整页实底卡片：手机上的弹层是半透明的，不铺底会透出后面的市场列表
    ".ptsignin-page{background:rgb(var(--v-theme-surface));border-radius:14px;padding:14px 14px 6px;"
    "max-width:100%;box-sizing:border-box;overflow-x:hidden;overflow-wrap:anywhere}"
    # 顶部统计：桌面用卡片；手机用一行胶囊（见 @media）
    ".ptsignin-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));"
    "gap:8px;margin:2px 0 10px}"
    ".ptsignin-summary-line{display:none;flex-wrap:wrap;gap:6px;margin:2px 0 8px}"
    # 手机放不下就横向滑动，别裁字
    "@media (max-width:640px){"
    ".ptsignin-summary-line{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;"
    "-webkit-overflow-scrolling:touch;padding-bottom:2px}"
    ".ptsignin-summary-line::-webkit-scrollbar{height:0}"
    "}"
    ".ptsignin-stat{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;"
    "border:1px solid rgba(var(--v-theme-on-surface),.10);background:rgba(var(--v-theme-on-surface),.03)}"
    ".ptsignin-stat-num{font-size:1.12rem;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}"
    ".ptsignin-stat-label{font-size:.72rem;line-height:1.2;color:rgba(var(--v-theme-on-surface),.62)}"
    ".ptsignin-updated{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:.75rem;"
    "color:rgba(var(--v-theme-on-surface),.55)}"
    # 区块标题：一个带图标的胶囊
    ".ptsignin-section{display:flex;align-items:center;gap:8px;margin:18px 0 8px}"
    # 统计数字配色（插件页里独立 VIcon 不渲染，颜色靠文字自己带）
    ".ptsignin-num-info{color:rgb(var(--v-theme-info,33,150,243))}"
    ".ptsignin-num-success{color:#4caf50}"
    ".ptsignin-num-error{color:#ef5350}"
    ".ptsignin-num-warning{color:#fb8c00}"
    ".ptsignin-num-grey{color:rgba(var(--v-theme-on-surface),.38)}"
    # 表格：圆角 + 轻阴影 + 斑马纹 + 悬停高亮，两张表列宽一致、逐列对齐
    ".ptsignin-table-wrap{border:1px solid rgba(var(--v-theme-on-surface),.10);border-radius:12px;"
    "overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.05)}"
    ".ptsignin-table{width:100%;table-layout:fixed}"
    ".ptsignin-table th:nth-child(1),.ptsignin-table td:nth-child(1){width:30%}"
    ".ptsignin-table th:nth-child(2),.ptsignin-table td:nth-child(2){width:17%}"
    ".ptsignin-table th:nth-child(3),.ptsignin-table td:nth-child(3){width:53%}"
    ".ptsignin-table thead th{background:rgba(var(--v-theme-on-surface),.04);font-size:.78rem;"
    "color:rgba(var(--v-theme-on-surface),.7);white-space:nowrap}"
    ".ptsignin-table tbody tr:nth-child(even){background:rgba(var(--v-theme-on-surface),.02)}"
    ".ptsignin-table tbody tr:hover{background:rgba(var(--v-theme-primary),.06)}"
    ".ptsignin-table td{vertical-align:middle;padding:10px 14px}"
    ".ptsignin-table td:nth-child(2){white-space:nowrap}"
    ".ptsignin-site{font-weight:600;line-height:1.25}"
    ".ptsignin-caption{font-size:.72rem;color:rgba(var(--v-theme-on-surface),.55);margin-top:2px}"
    # 结果列：时间用等宽数字，超长诊断最多 3 行
    ".ptsignin-result{font-size:.84rem;line-height:1.45;word-break:break-word;"
    "font-variant-numeric:tabular-nums;display:-webkit-box;-webkit-line-clamp:3;"
    "-webkit-box-orient:vertical;overflow:hidden}"
    # 手机专用卡片列表：和桌面表格是两份渲染，CSS 切换，避免把 table 硬掰成 flex
    ".ptsignin-list{display:none;width:100%;max-width:100%;box-sizing:border-box;"
    "overflow-x:auto;-webkit-overflow-scrolling:touch}"
    ".ptsignin-row{display:flex;flex-direction:column;gap:2px;"
    "width:100%;max-width:100%;box-sizing:border-box;"
    "margin-bottom:8px;padding:9px 12px;border-radius:10px;"
    "border:1px solid rgba(var(--v-theme-on-surface),.10);"
    "background:rgba(var(--v-theme-on-surface),.025)}"
    ".ptsignin-row-head{display:flex;align-items:center;gap:8px;width:100%;min-width:0}"
    ".ptsignin-row-site{flex:1 1 auto;min-width:0;font-weight:600;font-size:.92rem;"
    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".ptsignin-row-status{flex:0 0 auto}"
    ".ptsignin-row-result{width:100%;min-width:0;margin-top:2px;font-size:.8rem;line-height:1.45;"
    "word-break:break-word;font-variant-numeric:tabular-nums;display:-webkit-box;"
    "-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}"
    "@media (max-width:640px){"
    # 手机：统计换成一行胶囊，避免 5 张卡片换行排成 3+2
    ".ptsignin-stats{display:none}"
    ".ptsignin-summary-line{display:flex}"
    ".ptsignin-page{padding:12px 10px 4px;border-radius:12px}"
    ".ptsignin-updated{margin-bottom:6px;font-size:.7rem}"
    ".ptsignin-section{margin:14px 0 8px}"
    # 桌面表格与手机卡片列表二选一
    ".ptsignin-table-wrap{display:none;overflow-x:auto;-webkit-overflow-scrolling:touch}"
    ".ptsignin-list{display:block}"
    ".ptsignin-site{font-size:.92rem;line-height:1.3}"
    ".ptsignin-caption{margin-top:1px;font-size:.68rem;white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis}"
    "}"
)


class PTSiteSignIn(_PluginBase):
    plugin_name = "PT站点签到"
    plugin_desc = ("PT 站点自动签到，可登录刷新 Cookie 并读取最近动向；遇到验证码或 Cloudflare 会自动改用浏览器。")
    plugin_icon = "signin.png"
    plugin_version = "1.6.12"
    plugin_author = "qingyukb"
    author_url = "https://blog.qyus.cn"
    plugin_config_prefix = "ptsignin_"
    plugin_order = 29
    auth_level = 2

    _enabled = False
    _notify_enabled = True
    _clear_today = False
    _cron = "25 8 * * *"
    _site_ids: List[int] = []
    _login_site_ids: List[int] = []
    _all_sites = False          # 签到站点勾了「全部站点」
    _all_login_sites = False    # 登录站点勾了「全部站点」
    _concurrency = 2
    _timeout = 30
    _retry_delay_minutes = 10
    _retry_count = 1
    _browserless_url = DEFAULT_BROWSERLESS_URL
    _browserless_token = ""
    _browserless_timeout = 100
    _site_rules = ""
    _read_activity = True
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
        self._notify_enabled = self._parse_notify_switch(config)
        self._clear_today = bool(config.get("clear_today", False))
        if self._clear_today:
            # 一次性动作：清空今天的记录，下一轮（含本次的「立即执行」）会重新签一遍
            self._clear_today = False
            self._clear_today_cache()
        self._cron = str(config.get("cron") or "25 8 * * *")
        self._site_ids, self._all_sites = self._parse_site_ids(config.get("site_ids"))
        self._login_site_ids, self._all_login_sites = self._parse_site_ids(
            config.get("login_site_ids"))
        self._concurrency = max(1, min(int(config.get("concurrency") or 2), 5))
        self._timeout = max(5, min(int(config.get("timeout") or 30), 120))
        retry_count = config.get("retry_count")
        self._retry_count = max(0, min(int(1 if retry_count in (None, "") else retry_count), 5))
        self._retry_delay_minutes = max(1, min(int(config.get("retry_delay_minutes") or 10), 1440))
        self._browserless_url = str(config.get("browserless_url") or DEFAULT_BROWSERLESS_URL).strip()
        self._browserless_token = str(config.get("browserless_token") or "").strip()
        self._browserless_timeout = max(15, min(int(config.get("browserless_timeout") or 100), 300))
        self._site_rules = str(config.get("site_rules") or "")
        self._read_activity = bool(config.get("read_activity", True))
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
        """后台任务：只跑一遍签到（跳过失败重试），同样跳过今天已签到的站点。"""
        try:
            self.run_sign_in(allow_retry=False)
        except Exception:
            logger.exception("PT站点签到：保存后立即执行失败")

    @staticmethod
    def _parse_site_ids(raw: Any) -> Tuple[List[int], bool]:
        """解析站点选择：返回（站点 ID 列表，是否勾了「全部站点」）。"""
        values = raw if isinstance(raw, (list, tuple, set)) else ([raw] if raw else [])
        ids: List[int] = []
        all_selected = False
        for value in values:
            text_value = str(value).strip()
            if text_value == ALL_SITES:
                all_selected = True
            elif text_value.isdigit():
                ids.append(int(text_value))
        return ids, all_selected

    def _config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify_enabled,
            "clear_today": self._clear_today,
            "cron": self._cron,
            "site_ids": self._site_ids + ([ALL_SITES] if self._all_sites else []),
            "login_site_ids": self._login_site_ids + ([ALL_SITES] if self._all_login_sites else []),
            "concurrency": self._concurrency,
            "timeout": self._timeout,
            "retry_count": self._retry_count,
            "retry_delay_minutes": self._retry_delay_minutes,
            "browserless_url": self._browserless_url,
            "browserless_token": self._browserless_token,
            "browserless_timeout": self._browserless_timeout,
            "site_rules": self._site_rules,
            "read_activity": self._read_activity,
            "run_once": self._run_once,
        }

    def _clear_today_cache(self) -> None:
        """清空「今天」的签到记录：latest 里今天的行 + 当天的 history 一起删。

        用于手动刷新每日记录：清完之后下一轮不会因为「今天已签到」而跳过。
        """
        stamp = datetime.now().strftime("%Y-%m-%d")
        key = "history_" + datetime.now().strftime("%Y%m%d")
        latest = [row for row in self._records_of("latest")
                  if not str(row.get("ran_at") or "").startswith(stamp)]
        self.save_data("latest", latest)
        self._delete_data(key)
        index = [str(item) for item in (self.get_data("history_index") or [])
                 if isinstance(item, str)]
        self.save_data("history_index", [item for item in index if item != key])
        logger.info("PT站点签到：已清理本日缓存（%s 的记录），下一轮会重新签到", stamp)

    def _sites_to_run(self, infos: List[Dict[str, Any]]
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """过滤掉今天已经签到成功的站点，返回（要跑的, 跳过的今天记录）。

        恒为开启、两种入口一致：今天签到成功的站点本轮不再请求，省时间也省额度；
        纯登录站点、失败站点照常跑。想强行重跑，先打开一次「清理本日缓存」。
        """
        if not infos:
            return list(infos), []
        today = {str(row.get("site") or ""): row for row in self._today_records(
            self._records_of("latest"))}
        keep: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for info in infos:
            name = str(info.get("name") or info.get("url") or "未知站点")
            row = today.get(name)
            if row is not None and str(row.get("status") or "") in {"success", "already"}:
                skipped.append(row)
            else:
                keep.append(info)
        return keep, skipped

    @staticmethod
    def _parse_notify_switch(config: Dict[str, Any]) -> bool:
        """通知开关：新配置用 notify（布尔）。

        兼容旧配置：1.3.x 的 notify_mode（off 视为关闭，其余视为开启）、1.2.x 的 notify。
        """
        if "notify" in config:
            return bool(config.get("notify"))
        mode = str(config.get("notify_mode") or "").lower()
        if mode in {"always", "failed"}:
            return True
        if mode == "off":
            return False
        return True

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
        """配置页：开关一行 + 参数两列 + 站点多选 + Browserless + 自定义规则。"""
        try:
            sites = SiteOper().list_order_by_pri()
        except Exception:
            sites = []
        # 第一个选项是哨兵值 all：等于「全部站点，且自动包含以后新增的站点」
        options: List[Dict[str, Any]] = [{"title": "全部站点（含后续新增）", "value": ALL_SITES}]
        options += [{"title": site.name, "value": site.id}
                    for site in sites if getattr(site, "cookie", None)]

        def field(kind: str, model: str, label: str, md: int = 6,
                  **props: Any) -> Dict[str, Any]:
            """一列表单控件。"""
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": kind,
                                 "props": {"model": model, "label": label, **props}}]}

        def hint_field(kind: str, model: str, label: str, hint: str, md: int = 6,
                       **props: Any) -> Dict[str, Any]:
            """带常驻提示的控件。"""
            return field(kind, model, label, md, hint=hint, **{"persistent-hint": True, **props})

        def row(*cols: Dict[str, Any]) -> Dict[str, Any]:
            return {"component": "VRow", "content": list(cols)}

        browserless_alert = {
            "component": "VAlert",
            "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-2"},
            "content": [
                {"component": "span", "text": "需要图片验证码 / Cloudflare / Turnstile 的站点（内置预设已声明）"
                                              "自动走 Browserless，其余站点走 HTTP 直连、不消耗额度。没有 Token 请先 "},
                {"component": "a", "props": {"href": "https://www.browserless.io/signup/email?plan=free",
                                             "target": "_blank", "rel": "noopener noreferrer"},
                 "text": "注册 Browserless"},
                {"component": "span", "text": "，再到 "},
                {"component": "a", "props": {"href": "https://browserless.io/account/",
                                             "target": "_blank", "rel": "noopener noreferrer"},
                 "text": "账户控制台"},
                {"component": "span", "text": " 复制 API Key。"},
            ]}

        rules_hint = ('键为域名或站点 ID。HTTP：{"path":"/attendance.php","success":"正则","already":"正则"}；'
                      'Browserless：{"engine":"browserless","mode":"cloudflare|image|trigger_image|altcha|open_page", ...}；'
                      '页面内嵌 Turnstile、验证通过后由站点自己提交（如观众）用 {"submit_method":"auto"}；'
                      '点签到后跳 WAF / Cloudflare 检测页时用 {"wait": 12000} 等它放行；'
                      '「最近动向」读不到时可加 {"activity_path":"/userdetails.php?id=1"}')

        return [{"component": "VForm", "content": [
            row(field("VSwitch", "enabled", "启用插件", 4),
                hint_field("VSwitch", "notify", "发送通知",
                           "签到成功 / 登录成功 / 失败三项计数，有失败才附原因", 4),
                hint_field("VSwitch", "run_once", "保存后立即执行一次",
                           "保存后延后 3 秒在后台跑一轮，同样跳过今天已签到的站点", 4),
                hint_field("VSwitch", "clear_today", "清理本日缓存",
                           "打开并保存后清空今天的签到记录（一次性动作，会自动关闭），下一轮重新签", 4)),
            row(field("VCronField", "cron", "执行周期",
                      placeholder="5 位 Cron；例如 25 8 * * *"),
                hint_field("VTextField", "concurrency", "并发数", "Browserless 按额度计费，建议 1–2",
                           type="number", min=1, max=5)),
            row(hint_field("VTextField", "retry_count", "失败重试次数", "0 为关闭，默认 1 次",
                           type="number", min=0, max=5),
                hint_field("VTextField", "retry_delay_minutes", "失败后等待分钟数",
                           "仅失败站点重试前等待，默认 10 分钟", type="number", min=1, max=1440)),
            row(field("VTextField", "timeout", "HTTP 超时（秒）", type="number", min=5, max=120),
                hint_field("VSwitch", "read_activity", "读取「最近动向」",
                           "用站点 Cookie（配置了账号时会真实登录换回新 Cookie）打开用户页 / 签到页，"
                           "把「最近动向」写进登录状态表")),
            row(field("VSelect", "site_ids", "签到站点", 12, items=options, multiple=True,
                      chips=True, clearable=True, hint="选择全部后自动包含后续新增站点",
                      **{"persistent-hint": True})),
            row(field("VSelect", "login_site_ids", "登录站点", 12, items=options, multiple=True,
                      chips=True, clearable=True,
                      hint="选择全部后自动包含后续新增站点；与签到站点同时选中时一轮做完"
                           "（真实登录 + 签到 + 读最近动向，不会重复登录）",
                      **{"persistent-hint": True})),
            row(field("VTextField", "browserless_url", "Browserless 地址", 4,
                      placeholder=DEFAULT_BROWSERLESS_URL, hint="根地址或完整 /stealth/bql 地址",
                      **{"persistent-hint": True}),
                field("VTextField", "browserless_token", "Browserless Token", 4, type="password",
                      hint="仅 Browserless 引擎需要，不会写入日志", **{"persistent-hint": True}),
                field("VTextField", "browserless_timeout", "Browserless 超时（秒）", 4,
                      type="number", min=15, max=300,
                      hint="自动不低于页面内等待预算，建议 120–180", **{"persistent-hint": True})),
            row({"component": "VCol", "props": {"cols": 12}, "content": [browserless_alert]}),
            row({"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VTextarea",
                 "props": {"model": "site_rules", "label": "自定义站点规则 JSON（可留空）",
                           "rows": 6, "hint": rules_hint, "persistent-hint": True}}]}),
        ]}], self._config()

    def get_page(self) -> List[dict]:
        """插件详情页：一行统计 + 「签到状态」「登录状态」两张方框表格（站点 / 今日 / 结果）。"""
        latest = self._records_of("latest")
        history = self._history_records()
        if not latest and not history:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                     "text": "暂无签到记录；保存配置后点「保存后立即执行一次」，或等 Cron 触发。"}}]
        today = self._today_records(latest)
        summary = summarize_records(latest, self._selected_site_names())
        last_run = max((str(row.get("ran_at") or "") for row in latest), default="")[5:16]
        # 同一份统计渲染两种形态：桌面是卡片，手机是胶囊（CSS 控制显示哪套）
        metrics = [("站点", summary["total"], "info"),
                   ("成功", summary["success"], "success"),
                   ("失败", summary["failed"], "error"),
                   ("未确认", summary["uncertain"], "warning"),
                   ("未记录", summary["missing"], "grey")]
        body: List[dict] = [
            {"component": "div", "props": {"class": "ptsignin-stats"},
             "content": [self._stat(label, value, color) for label, value, color in metrics]},
            {"component": "div", "props": {"class": "ptsignin-summary-line"},
             "content": [{"component": "VChip",
                          "props": {"size": "x-small", "variant": "tonal",
                                    "color": color if value else "grey"},
                          "text": f"{label} {value}"} for label, value, color in metrics]},
        ]
        if last_run:
            body.append({"component": "div", "props": {"class": "ptsignin-updated"},
                         "text": f"上次执行 {last_run}"})
        body += self._status_table("签到状态", "mdi-calendar-check",
                                   self._section_site_names(False, history, latest),
                                   history, today, login_view=False)
        body += self._status_table("登录状态", "mdi-login-variant",
                                   self._section_site_names(True, history, latest),
                                   history, today, login_view=True)
        page: List[dict] = [{"component": "style", "text": PAGE_STYLE},
                            {"component": "div", "props": {"class": "ptsignin-page"},
                             "content": body}]
        recorded = {str(row.get("site") or "") for row in latest}
        missing = [name for name in self._selected_site_names() if name not in recorded]
        if missing:
            page.append({"component": "VAlert",
                         "props": {"type": "warning", "variant": "tonal", "density": "compact",
                                   "class": "mt-3"},
                         "text": "本轮没有记录的站点：" + "、".join(missing)
                                 + "（站点管理里没保存 Cookie、或本轮没执行到）"})
        return page

    @staticmethod
    def _stat(label: str, value: int, color: str) -> Dict[str, Any]:
        """顶部统计卡片：数字按状态着色，0 时变灰；插件页里独立图标不渲染，所以只留数字与说明。"""
        tone = color if value else "grey"
        return {"component": "div", "props": {"class": "ptsignin-stat"}, "content": [
            {"component": "div", "content": [
                {"component": "div", "props": {"class": f"ptsignin-stat-num ptsignin-num-{tone}"},
                 "text": str(value)},
                {"component": "div", "props": {"class": "ptsignin-stat-label"}, "text": label}]},
        ]}

    def _status_table(self, title: str, icon: str, names: List[str],
                      history: List[Dict[str, Any]], today: List[Dict[str, Any]],
                      login_view: bool) -> List[dict]:
        """一张方框表格：站点（含记录条数）｜今日｜结果。"""
        belongs = ((lambda row: is_login_record(row, strict=True)) if login_view
                   else is_signin_record)
        counts: Dict[str, int] = {}
        for row in history:
            if belongs(row):
                name = str(row.get("site") or "未知站点")
                counts[name] = counts.get(name, 0) + 1
        current: Dict[str, Dict[str, Any]] = {}
        for row in today:
            if belongs(row):
                current.setdefault(str(row.get("site") or "未知站点"), row)
        # 独立 VIcon 在插件页里不渲染，图标改用 chip 自带的 prepend-icon
        header = {"component": "div", "props": {"class": "ptsignin-section"}, "content": [
            {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "primary",
                                             "prepend-icon": icon},
             "text": f"{title}（{len(names)}）"},
        ]}
        if not names:
            return [header, {"component": "VAlert",
                             "props": {"type": "info", "variant": "tonal", "density": "compact"},
                             "text": "还没有站点；在设置里勾选「签到站点 / 登录站点」后重跑一次。"}]
        body = []
        rows: List[Dict[str, Any]] = []
        for name in names:
            row = current.get(name)
            if row is None:
                chip = {"label": "未记录", "color": "grey", "icon": "mdi-minus-circle-outline"}
                result = "—"
                caption = f"共 {counts.get(name, 0)} 条记录"
            else:
                meta = record_meta(self._login_view(row) if login_view else row)
                chip = {"label": meta["label"], "color": meta["color"], "icon": meta["icon"]}
                result = self._result_text(row, login_view)
                moment = str(row.get("ran_at") or "")[11:16]
                caption = f"今日 {moment} · 共 {counts.get(name, 0)} 条记录" if moment else \
                    f"共 {counts.get(name, 0)} 条记录"
            body.append({"component": "tr", "content": [
                {"component": "td", "content": [
                    {"component": "div", "props": {"class": "ptsignin-site"}, "text": name},
                    {"component": "div", "props": {"class": "ptsignin-caption"}, "text": caption}]},
                {"component": "td", "content": [
                    {"component": "VChip", "props": {"size": "small", "variant": "tonal",
                                                     "color": chip["color"],
                                                     "prepend-icon": chip["icon"]},
                     "text": chip["label"]}]},
                {"component": "td", "props": {"class": "ptsignin-result"}, "text": result},
            ]})
            rows.append({"site": name, "caption": caption, "chip": chip, "result": result})
        table = {"component": "div", "props": {"class": "ptsignin-table-wrap"},
                 "content": [{"component": "VTable",
                              "props": {"density": "comfortable", "hover": True,
                                        "class": "ptsignin-table"},
                              "content": [
                                  {"component": "thead", "content": [
                                      {"component": "tr", "content": [
                                          {"component": "th", "text": "站点"},
                                          {"component": "th", "text": "状态"},
                                          {"component": "th", "text": "结果"}]}]},
                                  {"component": "tbody", "content": body}]}]}
        # 手机：一行一张卡片（站点 + 状态胶囊在左右，结果独占下一行）
        cards = {"component": "div", "props": {"class": "ptsignin-list"},
                 "content": [{"component": "div", "props": {"class": "ptsignin-row"}, "content": [
                     {"component": "div", "props": {"class": "ptsignin-row-head"}, "content": [
                         {"component": "div", "props": {"class": "ptsignin-row-site"},
                          "text": str(item.get("site") or "未知站点")},
                         {"component": "div", "props": {"class": "ptsignin-row-status"}, "content": [
                             {"component": "VChip",
                              "props": {"size": "x-small", "variant": "tonal",
                                        "color": item.get("chip", {}).get("color", "grey"),
                                        "prepend-icon": item.get("chip", {}).get("icon", "")},
                              "text": item.get("chip", {}).get("label", "")}]}]},
                     {"component": "div", "props": {"class": "ptsignin-caption"},
                      "text": str(item.get("caption") or "")},
                     {"component": "div", "props": {"class": "ptsignin-row-result"},
                      "text": str(item.get("result") or "—")},
                 ]} for item in rows]}
        return [header, table, cards]

    def _section_site_names(self, login_view: bool, history: List[Dict[str, Any]],
                            latest: List[Dict[str, Any]]) -> List[str]:
        """该区块要显示的站点：配置里勾选的站点优先，再补上历史 / 本轮里出现过的站点。"""
        try:
            sites = {site.id: site for site in SiteOper().list_order_by_pri()}
        except Exception:
            sites = {}
        if login_view:
            ids = list(sites) if self._all_login_sites else self._login_site_ids
        else:
            ids = list(sites) if self._all_sites else self._site_ids
        names = [str(sites[sid].name) for sid in ids if sid in sites]
        belongs = ((lambda row: is_login_record(row, strict=True)) if login_view
                   else is_signin_record)
        for row in latest + history:
            if not belongs(row):
                continue
            name = str(row.get("site") or "未知站点")
            if name not in names:
                names.append(name)
        return names

    def _today_records(self, latest: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """今天的记录：优先取当天的 history，取不到时用 latest 里 ran_at 是今天的记录。"""
        rows = self._records_of("history_" + datetime.now().strftime("%Y%m%d"))
        if rows:
            return rows
        stamp = datetime.now().strftime("%Y-%m-%d")
        return [row for row in latest if str(row.get("ran_at") or "").startswith(stamp)]

    def _result_text(self, row: Dict[str, Any], login_view: bool) -> str:
        """结果列：登录看「最近动向」，签到看实际到手的东西（魔力值 / 爆米花 / 奖励）。"""
        if login_view:
            activity = decode_entities(row.get("activity")).strip()
            if activity:
                return activity
            # 登录失败 / 没读到最近动向时，把原因写出来比只留一句提示更有用
            if str(self._login_view(row).get("status") or "") == "login_failed":
                return str(row.get("login") or row.get("message") or "").strip() or "登录失败"
            return self._activity_hint(str(row.get("activity_source") or ""))
        bonus = decode_entities(row.get("bonus")).strip()
        if bonus:
            return bonus
        return decode_entities(message_bonus(row.get("message"))
                               or str(row.get("message") or "-")).strip()

    @staticmethod
    def _login_view(row: Dict[str, Any]) -> Dict[str, Any]:
        """「登录状态」用的投影：双选站点取登录结果（login_status），而不是签到结果。"""
        login_status = str(row.get("login_status") or "")
        return dict(row, status=login_status) if login_status else row

    def _records_of(self, key: str) -> List[Dict[str, Any]]:
        data = self.get_data(key) or []
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def _history_records(self) -> List[Dict[str, Any]]:
        """按天读回历史记录（新的在前），用于统计每站「共 N 条记录」。"""
        index = self.get_data("history_index") or []
        keys = [str(key) for key in index if isinstance(key, str)] if isinstance(index, list) else []
        return [row for key in sorted(keys, reverse=True) for row in self._records_of(key)]

    def _selected_site_names(self) -> List[str]:
        """本轮勾选的站点名（「全部站点」按站点管理里的全部站点算）。"""
        try:
            sites = {site.id: site for site in SiteOper().list_order_by_pri()}
        except Exception:
            return []
        if self._all_sites or self._all_login_sites:
            ids = list(sites)
        else:
            ids = self._site_ids + self._login_site_ids
        return [str(sites[sid].name) for sid in ids if sid in sites]

    @staticmethod
    def _activity_hint(reason: str = "") -> str:
        """没读到最近动向时的提示；reason 里是读取出错的说明（含试过的页面）。"""
        detail = str(reason or "").strip()
        if detail:
            return f"未读取到：{detail}"
        return "未读取到（可确认「读取最近动向」开关，或用站点规则 activity_path 指定页面）"

    @staticmethod
    def _status_meta(status: str) -> Dict[str, str]:
        """状态标签（通知与页面共用，保证两处文案一致）。"""
        return record_meta({"status": status})


    # ---------------- 签到核心 ----------------

    @staticmethod
    def _site_dict(site: Any) -> Dict[str, Any]:
        """把 MoviePilot「站点管理」的记录转成普通字典。

        除签到要用的 Cookie / UA / 代理 / 站点规则外，一并带上附加信息(note)、ApiKey、
        Token 与超时，登录凭证可以写在站点管理的附加信息里（见 core.account_from_site）。
        """
        return {key: getattr(site, key, None)
                for key in ("id", "name", "domain", "url", "cookie", "ua", "proxy", "rule",
                            "apikey", "token", "note", "timeout",
                            "username", "user", "account", "password", "passwd", "pwd")}

    def _should_init_signer(self, infos: List[Dict[str, Any]],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """按最终生效的站点规则判断是否需要初始化 Browserless 引擎。

        两类站点需要浏览器：① 规则显式声明 engine=browserless；
        ② 未声明引擎的 HTTP 站点（遇盾 / 验证码时自动改用浏览器，恒为开启）。
        """
        for info in infos:
            try:
                rule = resolve_rule(info, rules)
            except ValueError:
                continue
            if str(rule.get("engine")) == "browserless":
                return True
            if rule.get("auto_fallback"):
                return True
        return False

    def _sign_one(self, info: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                  signer: Optional[BrowserlessSigner]) -> Dict[str, str]:
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info("PT站点签到：开始 %s", site_name)
        engine = "http"
        login_text = ""
        # 同时勾了「签到站点」和「登录站点」的站点：一轮里把真实登录 + 签到 + 读最近动向都做掉，
        # 记录带 dual 标记，详情页的「签到状态」与「登录状态」都能看到它。
        dual = bool(info.get("dual"))
        login_broken = False
        rule: Dict[str, Any] = {}
        # 站点登录：凭证只取 MoviePilot「站点管理」里该站的附加信息 / 账号字段；
        # 没有账号密码时直接复用站点管理保存的 Cookie 签到。登录失败不直接判整站失败
        # ——仍用旧 Cookie 试一次，并如实记录原因。
        account = account_from_site(info)
        if account_ready(account):
            proxies = self._proxies_for(info)
            try:
                login_result = site_login(info, account, timeout=self._timeout, proxies=proxies)
            except Exception:
                logger.exception("PT站点签到：%s 真实登录异常", site_name)
                login_text = "异常：真实登录出现未预期错误"
            else:
                if login_result.status == "success":
                    info = dict(info, cookie=login_result.cookie)
                    login_text = "成功（已换用新 Cookie）"
                    logger.info("PT站点签到：%s 真实登录成功，改用新 Cookie", site_name)
                else:
                    login_text = "失败：" + login_result.message
                    login_broken = True
                    logger.warning("PT站点签到：%s 真实登录失败 - %s", site_name,
                                   login_result.message)
        elif account:
            # 站点管理里只写了半截凭证（比如只有用户名），没法做真实登录
            login_text = "未提供可用的账号密码"
            login_broken = True
            logger.info("PT站点签到：%s 的账号信息不完整，只复用站点管理里的 Cookie", site_name)
        elif str(info.get("cookie") or "").strip():
            # 站点管理里只有 Cookie：直接拿它签到；dual 站点要把结论写进「登录状态」
            login_text = "复用站点管理 Cookie" if dual else ""
        try:
            rule = resolve_rule(info, rules)
            engine = str(rule["engine"])
            # 规则表达不了的站点（图片验证码 / 答题选海报 / API Token）有专门模块，
            # 放在插件目录 sites/ 下，优先于规则执行。
            module_outcome = run_site_module(info, timeout=self._timeout,
                                             proxies=self._proxies_for(info), logger=logger)
            if module_outcome is not None:
                outcome = module_outcome
                engine = "module"
                logger.info("PT站点签到：%s 使用站点模块", site_name)
            elif engine == "playwright":
                # 本机浏览器：IP 与家宽一致，站点不会判异地登录，也不消耗 Browserless 额度
                outcome = playwright_sign(info, rule, timeout=max(90, self._timeout * 2),
                                          proxies=self._proxies_for(info))
                if outcome is None:
                    logger.info("PT站点签到：%s 本机没有 Playwright，改用 Browserless", site_name)
                    if signer is not None:
                        outcome = signer.sign(dict(rule, engine="browserless"))
                        engine = "browserless"
                    else:
                        outcome = SignResult("failed", "本机缺少 Playwright，且未配置 Browserless")
                        engine = "playwright"
            elif engine == "browserless":
                if signer is None:
                    outcome = SignResult("failed", "未配置 Browserless 地址或 Token")
                else:
                    outcome = signer.sign(info, rule)
            else:
                outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                         proxies=self._proxies_for(info))
                if outcome.status == "failed" and self._should_fallback(rule, outcome):
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
        activity_text, activity_source = (self._read_site_activity(info, rule)
                                          if self._read_activity else ("", ""))
        return {"site": site_name, "site_id": str(info.get("id") or ""),
                "state_key": site_state_key(info), "engine": engine,
                "status": outcome.status, "message": outcome.message,
                "login": login_text, "activity": activity_text,
                "activity_source": activity_source, "ran_at": self._now(),
                "bonus": message_bonus(outcome.message),
                "dual": dual,
                # dual 站点在「登录状态」里要显示登录结论，而不是签到结论
                "login_status": ("login_failed" if login_broken else "login") if dual else ""}

    def _login_one(self, info: Dict[str, Any]) -> Dict[str, str]:
        """仅登录站点：真实登录（或复用站点管理 Cookie）后读取最近动向，不触发签到。"""
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info("PT站点签到：仅登录 %s", site_name)
        login_text = ""
        login_ok = False
        account = account_from_site(info)
        if account_ready(account):
            proxies = self._proxies_for(info)
            try:
                login_result = site_login(info, account, timeout=self._timeout, proxies=proxies)
            except Exception:
                logger.exception("PT站点签到：%s 真实登录异常", site_name)
                login_text = "异常：真实登录出现未预期错误"
            else:
                if login_result.status == "success":
                    info = dict(info, cookie=login_result.cookie)
                    login_ok = True
                    login_text = "成功（已换用新 Cookie）"
                else:
                    login_text = "失败：" + login_result.message
        elif account:
            login_text = "未提供可用的账号密码"
        elif str(info.get("cookie") or "").strip():
            login_ok = True
        else:
            login_text = "未配置账号密码或 Cookie"
        activity_text, activity_source = (self._read_site_activity(info)
                                          if (login_ok and self._read_activity) else ("", ""))
        return {"site": site_name, "site_id": str(info.get("id") or ""),
                "state_key": site_state_key(info), "engine": "http",
                "status": "login" if login_ok else "login_failed",
                "message": login_text, "login": "", "activity": activity_text,
                "activity_source": activity_source, "ran_at": self._now()}

    def _run_login_batch(self, infos: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        if not infos:
            return []
        workers = min(self._concurrency, len(infos))
        if workers <= 1:
            return [self._login_one(info) for info in infos]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self._login_one, infos))

    def _read_site_activity(self, info: Dict[str, Any],
                            rule: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """用登录态 Cookie 打开站点页面，返回（「最近动向」原文, 来源页面）。

        来源页面写进记录与日志；读不到时把原因（含试过的页面）也带回去，
        方便按站点用 activity_path 指定真正的动向页面。
        """
        cookie = str(info.get("cookie") or "").strip()
        if not cookie:
            return "", ""
        account = account_from_site(info)
        rule = rule or {}
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        # 站点常把「最近动向」放在签到页 / 校验页（例如观众 attendance.php），一并作为候选
        paths = [str(rule.get("path") or ""), str(rule.get("verify_success_path") or "")]
        try:
            activity = read_activity(info, cookie, timeout=self._timeout,
                                     proxies=self._proxies_for(info),
                                     path=str(account.get("activity_path") or "") or None,
                                     paths=paths)
        except Exception:
            logger.exception("PT站点签到：读取最近动向失败")
            return "", ""
        if activity.status != "success":
            logger.info("PT站点签到：%s 未读到最近动向（%s）", site_name, activity.message)
            return "", activity.message
        value = activity.last_seen or activity.snippet or "已读取"
        logger.info("PT站点签到：%s 最近动向 ← %s：%s", site_name, activity.source, value)
        return value, activity.source

    @staticmethod
    def _should_fallback(rule: Dict[str, Any], outcome: SignResult) -> bool:
        """这一轮失败要不要改用浏览器再试一次。

        两种情况：① 撞上 Cloudflare / 验证码；② 拿到了页面但没匹配到成功标志——
        后者常见于只能靠浏览器点击的站点，未写规则的站点也给它一次机会。
        404（找不到签到入口）、Cookie 失效这类重试没用，不折腾。
        """
        if outcome.challenge:
            return True
        if not rule.get("auto_fallback") or outcome.missing_path:
            return False
        return "未匹配到" in str(outcome.message or "")

    def _maybe_fallback(self, info: Dict[str, Any], rule: Dict[str, Any], outcome: SignResult,
                        signer: Optional[BrowserlessSigner]) -> Tuple[SignResult, str]:
        """HTTP 直连撞上 Cloudflare / 验证码时，自动改用 Browserless 重试一次。

        只对未显式声明引擎的站点生效（rule 的 auto_fallback），并可在配置里整体关闭。
        返回（结果, 实际使用的引擎）。
        """
        # 自动回退恒为开启（设置页不再提供开关）；用户在规则里显式写了 engine 的站点不参与
        if not rule.get("auto_fallback"):
            return outcome, "http"
        label = ("Cloudflare 验证" if outcome.challenge == "cloudflare"
                 else "验证码" if outcome.challenge
                 else "未匹配到签到结果")
        if signer is None:
            return SignResult("failed", f"{outcome.message}；检测到{label}，但未配置 Browserless 地址或 Token"), "http"
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info("PT站点签到：%s 检测到%s，自动改用 Browserless 重试", site_name, label)
        try:
            fallback = fallback_rule(rule, outcome.challenge)
        except ValueError as error:
            return SignResult("failed", f"{outcome.message}；回退规则无效：{error}"), "http"
        # 撞盾时未必知道该站真正的签到入口（首页挂「每日签到」表单的站点没有独立签到页）：
        # 顺手从首页找一次，找到就把它交给浏览器，别让浏览器去开一个 404 的地址
        base_url = str(info.get("url") or "").rstrip("/")
        discovered = discover_signin_target(
            fetch_page(target_url(base_url, DEFAULT_INDEX_PATH), str(info.get("cookie") or ""),
                       self._timeout, self._proxies_for(info), str(info.get("ua") or "")),
            base_url)
        if discovered.get("path"):
            fallback = dict(fallback, path=str(discovered["path"]))
            logger.info("PT站点签到：%s 的签到入口改为 %s", site_name, discovered["path"])
        retry = signer.sign(info, fallback)
        if retry.status in {"success", "already"}:
            return SignResult(retry.status, f"{retry.message}（已自动改用浏览器引擎通过{label}）"), "browserless"
        return SignResult("failed", f"{outcome.message}；浏览器引擎重试未通过：{retry.message}"), "browserless"

    def _proxies_for(self, info: Optional[Mapping[str, Any]] = None
                     ) -> Optional[Dict[str, str]]:
        """这个站点是否走代理——直接用 MoviePilot「站点管理」里的「使用代理访问」开关。

        站点记录勾了才走 MoviePilot 的全局代理（设置 → 系统 → 代理）；勾了但全局代理为空时写一条
        日志说明本次仍直连，避免出现「勾了却看不出效果」。Browserless 引擎走它自己的出口。
        """
        if not (info or {}).get("proxy"):
            return None
        host = self._proxy_host()
        if not host:
            logger.warning("PT站点签到：站点勾选了「使用代理访问」，但 MoviePilot（设置 → 系统 → 代理）"
                           "与 HTTP_PROXY / HTTPS_PROXY 都没有配置，本次仍走直连")
            return None
        if host != getattr(self, "_proxy_logged", ""):
            self._proxy_logged = host
            logger.info("PT站点签到：站点勾选「使用代理访问」，HTTP 引擎走代理 %s", mask_proxy(host))
        return {"http": host, "https": host}

    @staticmethod
    def _proxy_host() -> str:
        """取 MoviePilot 的全局代理地址：不同版本的模块路径与配置项名都试一遍，最后看环境变量。"""
        for module_name in ("app.core.config", "app.sdk.config", "app.core.settings",
                            "app.core.config.base"):
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            for holder_name in ("settings", "config", "Config"):
                holder = getattr(module, holder_name, None)
                if holder is None:
                    continue
                for key in ("PROXY_HOST", "PROXY", "proxy_host", "proxy"):
                    value = getattr(holder, key, None)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = str(os.environ.get(key) or "").strip()
            if value:
                return value
        return ""

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
        allow_retry=False 时跳过失败重试（保存后立即执行用）。两种入口用同一套规则：
        今天已签到成功的站点一律跳过，要重跑先打开一次「清理本日缓存」。
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
        # 「全部站点」哨兵展开成站点管理里的全部站点；两边都勾的站点只跑一次（_sign_one
        # 里已经把登录与读最近动向做掉），带 dual 标记后同时进两块统计。
        signin_order = [sid for sid in (list(all_sites) if self._all_sites else self._site_ids)
                        if sid in all_sites]
        login_order = [sid for sid in (list(all_sites) if self._all_login_sites
                                       else self._login_site_ids) if sid in all_sites]
        dual_ids = set(signin_order) & set(login_order)
        selected = [all_sites[sid] for sid in signin_order]
        login_selected = [all_sites[sid] for sid in login_order if sid not in dual_ids]
        if not selected and not login_selected:
            self._finish([{"site": "配置", "status": "failed", "engine": "-",
                           "message": "未选择可用的 MoviePilot 站点", "ran_at": self._now()}])
            return
        try:
            rules = parse_rules(self._site_rules)
        except ValueError as error:
            self._finish([{"site": "配置", "status": "failed", "engine": "-",
                           "message": str(error), "ran_at": self._now()}])
            return
        infos = [dict(self._site_dict(site), dual=sid in dual_ids)
                 for site, sid in zip(selected, signin_order)]
        infos, skipped_rows = self._sites_to_run(infos)
        if skipped_rows:
            logger.info("PT站点签到：跳过今天已签到的 %s 个站点（%s）",
                        len(skipped_rows),
                        "、".join(str(row.get("site") or "") for row in skipped_rows))
        if not infos and not login_selected:
            logger.info("PT站点签到：今天的站点都已签到，本轮跳过（要重跑请打开一次「清理本日缓存」）")
            return
        login_infos = [self._site_dict(site) for site in login_selected]
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
        login_results = self._run_login_batch(login_infos)
        # 跳过的站点把今天的记录原样带上，避免落盘时把它们的记录冲掉
        result_by_key = {r.get("state_key") or r.get("site"): r
                         for r in results + login_results + skipped_rows}
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
        login_ok = sum(item.get("status") == "login" for item in results)
        failed = len(results) - signed - already - login_ok
        logger.info("PT站点签到：完成，新签到 %s，今日已签到 %s，仅登录 %s，失败 %s",
                    signed, already, login_ok, failed)
        self._notify(results)

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

    def _notify(self, results: List[Dict[str, str]]) -> None:
        """推送结果（由「发送通知」开关控制）。

        版式固定三块：一行统计、一行执行时间、失败时再列站点与原因（最多 6 条，多出来的写条数），
        避免失败多的时候刷出一大段。
        """
        if not self._notify_enabled or not results:
            return
        signed = sum(1 for item in results
                     if str(item.get("status") or "") in {"success", "already"})
        login_ok = sum(1 for item in results
                       if str(self._login_view(item).get("status") or "") == "login")
        failures = [item for item in results if self._is_failed(item)]
        moment = max((str(item.get("ran_at") or "") for item in results), default="")[5:16]
        lines = [f"签到成功 {signed} ｜ 登录成功 {login_ok} ｜ 失败 {len(failures)}"]
        if moment:
            lines.append(f"共 {len(results)} 个站点 · {moment}")
        if failures:
            lines.append("")
            lines.append("失败：")
            lines.extend("· " + self._failure_reason(item) for item in failures[:NOTIFY_LIMIT])
            if len(failures) > NOTIFY_LIMIT:
                lines.append(f"…另有 {len(failures) - NOTIFY_LIMIT} 个站点失败，详见插件日志")
        self.post_message(mtype=NotificationType.SiteMessage,
                          title="PT站点签到",
                          text="\n".join(lines))

    @staticmethod
    def _is_failed(item: Dict[str, Any]) -> bool:
        """这条记录算不算失败（成功 / 今日已签到 / 登录成功 之外的都算）。"""
        return str(item.get("status") or "") not in {"success", "already", "login"}

    @staticmethod
    def _failure_reason(item: Dict[str, Any]) -> str:
        """失败原因：站点名 + 结果文案，过长时截断，别让通知变成一堵墙。"""
        site = str(item.get("site") or "未知站点")
        detail = str(item.get("message") or "").strip() or str(item.get("login") or "").strip()
        # 失败原因只留一句，长了直接截断——通知里看到的够判断就行，细节看日志
        limit = NOTIFY_REASON_LIMIT
        for marker in ("；诊断：", "；标题", "；正文", "；片段"):
            if marker in detail:
                detail = detail.split(marker, 1)[0]
                break
        if len(detail) > limit:
            detail = detail[:limit].rstrip() + "…"
        return decode_entities(f"{site}：{detail}") if detail else site

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
