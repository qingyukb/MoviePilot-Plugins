"""PT 站点签到插件（MoviePilot V3）

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
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, Timer
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
try:  # MoviePilot V3 稳定 SDK
    from app.sdk.plugin import _PluginBase  # type: ignore[no-redef]
except ImportError:  # MoviePilot V2
    from app.plugins import _PluginBase

try:  # MoviePilot V3
    from app.db.oper.site import SiteOper
except ImportError:  # MoviePilot V2
    from app.db.site_oper import SiteOper

try:  # MoviePilot V3 日志门面
    from app.sdk.logging import logger
except ImportError:  # MoviePilot V2
    from app.log import logger

try:  # V3：一次性 / 延迟任务交给宿主调度器，避免自建常驻线程
    from app.sdk import scheduler as scheduler_sdk
except ImportError:  # V2 没有这个门面
    scheduler_sdk = None

from app.schemas import NotificationType

from .core import (
    DEFAULT_BROWSERLESS_URL,
    BrowserlessPool,
    BrowserlessSigner,
    LoginResult,
    SignResult,
    account_from_site,
    account_ready,
    fallback_rule,
    DEFAULT_INDEX_PATH,
    decode_entities,
    discover_signin_target,
    fetch_page,
    http_check_api_key,
    generic_api_key_rule,
    http_sign_auto,
    is_api_key_rule,
    playwright_sign,
    playwright_read_activity,
    run_site_module,
    run_site_module_login,
    is_login_record,
    is_signin_record,
    login as site_login,
    message_bonus,
    normalize_site_ids,
    parse_rules,
    read_activity,
    record_meta,
    resolve_rule,
    site_record_counts,
    site_module_for,
    site_state_key,
    summarize_records,
    target_url,
)

# 日志里的失败原因最多保留多少字。
LOG_TEXT_LIMIT = 120


def mask_proxy(host: str) -> str:
    """代理地址可能带账号密码，日志里把凭据打码。"""
    return re.sub(r"//[^@/]+@", "//***@", str(host or ""))


def compact_text(value: Any, limit: int = LOG_TEXT_LIMIT) -> str:
    """
    把日志文本压成单行，并限制最大长度。

    :param value: 待压缩的日志内容
    :param limit: 单条日志允许保留的最大字符数
    :return: 去除多余空白并截断后的单行文本
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------- 常量
# 站点选择里的哨兵值：勾选它等于「全部站点」，并且以后新增的站点自动包含进来。
ALL_SITES = "all"
# 每个站点面板里最多展示多少条历史记录（历史本身按天保留最近 30 天）。
HISTORY_SHOW = 10
# 通知里每个失败原因最多显示多少字，避免长诊断把通知撑成一堵墙。
NOTIFY_REASON_LIMIT = 90      # 通知里每条失败原因最多这么长
NOTIFY_LIMIT = 6              # 通知里最多列几个失败类别
NOTIFY_SITE_LIMIT = 6         # 同一失败类别最多列几个站点

# 通知失败原因语义分类。原始详情仍保存在插件记录里，这里只影响通知摘要和分组。
_FAILURE_CATEGORY_RULES = (
    ("站点配置缺失", r"(?:未配置|未填写|未设置|未提供|缺少)[^；;]*(?:站点(?:地址|信息)|cookie|token|api[-_]?key)|(?:站点(?:地址|信息)|cookie|token|api[-_]?key)(?:为空|未配置|缺失|未填写)"),
    ("站点页面或接口解析异常", r"csrf"),
    ("Cookie / Token / 登录状态异常", r"(?:cookie|(?<!csrf[-_])token|authorization|api[-_]?key).*(?:失效|过期|无效|不正确|不对|错误|权限不足)|(?:登录态|登录状态|登录凭据).*(?:失效|过期|无效)|未(?:登录|登陆)|请先登录|需要登录|unauthorized|authenticationrequired|状态码[:：]?(?:401)|http(?:status(?:code)?)?[:：]?401"),
    ("Cloudflare / WAF 防护验证失败", r"cloudflare|waf|雷池|safeline|人机验证|turnstile"),
    ("验证码获取或校验失败", r"验证码|captcha|invalid_imagehash"),
    ("网络请求超时", r"超时|timeout|timedout|状态码[:：]?408|http(?:status(?:code)?)?[:：]?408"),
    ("网络连接失败", r"无法(?:打开|访问)(?:网站|签到页)|站点连通性|请求无响应|连接失败|域名解析|dns|connection(?:error|refused|reset|aborted)|failedtoresolve"),
    ("最近动向读取失败", r"最近动向|activity"),
    ("站点访问被拒绝（HTTP 403）", r"状态码[:：]?403|http(?:status(?:code)?)?[:：]?403"),
    ("站点访问频率受限（HTTP 429）", r"状态码[:：]?429|http(?:status(?:code)?)?[:：]?429"),
    ("站点服务异常（HTTP 5xx）", r"状态码[:：]?5\d{2}|http(?:status(?:code)?)?[:：]?5\d{2}"),
    ("HTTP 状态异常", r"状态码[:：]?[1-4]\d{2}|http(?:status(?:code)?)?[:：]?[1-4]\d{2}"),
    ("站点页面或接口解析异常", r"空页面|空响应|接口.*(?:异常|无效)|响应结构|未获取到.*(?:签到|验证码|参数|图片)"),
    ("签到结果未确认", r"未确认|未能确认|响应丢失|未显示签到成功|未返回明确的(?:签到|成功)|签到状态未知|重试后仍未确认"),
)
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
    plugin_desc = ("PT 站点自动签到，支持 Cookie 登录并读取最近动向；验证码或 Cloudflare 可按规则改用浏览器。")
    plugin_icon = "signin.png"
    plugin_version = "3.1.3"
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
    _concurrency = 6
    _timeout = 30
    _retry_delay_minutes = 10
    _retry_count = 1
    _browserless_url = DEFAULT_BROWSERLESS_URL
    _browserless_token = ""
    _browserless_token2 = ""
    _browserless_token3 = ""
    _browserless_token4 = ""
    _browserless_timeout = 100
    _site_rules = ""
    _read_activity = True
    _run_once = False
    _piggo_username = ""
    _piggo_password = ""
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
        clear_today = bool(config.get("clear_today", False))
        self._clear_today = False
        self._cron = str(config.get("cron") or "25 8 * * *")
        self._site_ids, self._all_sites = self._parse_site_ids(config.get("site_ids"))
        self._login_site_ids, self._all_login_sites = self._parse_site_ids(
            config.get("login_site_ids"))
        self._concurrency = max(1, min(int(config.get("concurrency") or 6), 12))
        self._timeout = max(5, min(int(config.get("timeout") or 30), 120))
        retry_count = config.get("retry_count")
        self._retry_count = max(0, min(int(1 if retry_count in (None, "") else retry_count), 5))
        self._retry_delay_minutes = max(1, min(int(config.get("retry_delay_minutes") or 10), 1440))
        self._browserless_url = str(config.get("browserless_url") or DEFAULT_BROWSERLESS_URL).strip()
        self._browserless_token = str(config.get("browserless_token") or "").strip()
        self._browserless_token2 = str(config.get("browserless_token2") or "").strip()
        self._browserless_token3 = str(config.get("browserless_token3") or "").strip()
        self._browserless_token4 = str(config.get("browserless_token4") or "").strip()
        self._browserless_timeout = max(15, min(int(config.get("browserless_timeout") or 100), 300))
        self._site_rules = str(config.get("site_rules") or "")
        self._piggo_username = str(config.get("piggo_username") or "").strip()
        self._piggo_password = str(config.get("piggo_password") or "")
        self._read_activity = bool(config.get("read_activity", True))
        run_once = bool(config.get("run_once", False))
        self._run_once = run_once
        if clear_today:
            # 一次性动作：清空今天的记录，下一轮（含本次的「立即执行」）会重新签一遍
            self._clear_today_cache()
        if clear_today or run_once:
            # 一次性开关必须写回关闭状态，否则界面会一直显示开启并在重载后重复执行
            self._run_once = False
            self.update_config(self._config())
        if run_once:
            self._start_run_once()

    def _start_run_once(self) -> None:
        """「保存后立即执行一次」：延后几秒交给后台调度器执行，不阻塞保存配置的请求。

        与官方插件（alidnsddns、hdbluesignin）做法一致——不在 init_plugin 里同步跑长任务，
        否则 MP 保存配置的请求会被签到（尤其是失败重试的等待）一直占住，表现为设置页卡死。
        """
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None) if scheduler_sdk else None
        if callable(add_once):
            try:
                add_once(self.__class__.__name__, "signin_once", self._run_once_task,
                         "PT站点签到 立即执行一次", delay_seconds=3)
                logger.info("PT站点签到：保存后立即执行一次，将在 3 秒后于后台开始")
                return
            except Exception:
                logger.exception("PT站点签到：登记一次性任务失败，改用本地调度器")
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
        """解析站点选择：返回（去重站点 ID，是否勾了「全部站点」）。

        MoviePilot 不同版本/导入路径可能把多选值保存成 ``"12"``、JSON 数组或
        分隔文本；统一交给 ``normalize_site_ids``，避免把 12 拆成 1 和 2。
        """
        values: Any = raw
        if isinstance(raw, str):
            text_value = raw.strip()
            try:
                values = json.loads(text_value) if text_value else []
            except (TypeError, ValueError):
                values = re.split(r"[\s,;，；]+", text_value)
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        all_selected = any(str(value).strip().lower() == ALL_SITES for value in values)
        return normalize_site_ids(raw), all_selected

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
            "browserless_token2": self._browserless_token2,
            "browserless_token3": self._browserless_token3,
            "browserless_token4": self._browserless_token4,
            "browserless_timeout": self._browserless_timeout,
            "site_rules": self._site_rules,
            "piggo_username": self._piggo_username,
            "piggo_password": self._piggo_password,
            "read_activity": self._read_activity,
            "run_once": self._run_once,
        }

    def _browserless_tokens(self) -> List[str]:
        """收集配置里的 Browserless Token（最多 4 个，去空去重）。"""
        tokens: List[str] = []
        for value in (self._browserless_token, self._browserless_token2,
                      self._browserless_token3, self._browserless_token4):
            token = str(value or "").strip()
            if token and token not in tokens:
                tokens.append(token)
        return tokens

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
        logger.info(f"PT站点签到：已清理本日缓存（{stamp} 的记录），下一轮会重新签到")

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
            logger.error(f"PT站点签到：Cron 表达式无效：{self._cron}")
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
        def has_auth(site: Any) -> bool:
            return any(str(getattr(site, field, None) or "").strip()
                       for field in ("cookie", "apikey", "token"))

        options: List[Dict[str, Any]] = [{"title": "全部站点（含后续新增）", "value": ALL_SITES}]
        options += [{"title": str(site.name), "value": site.id}
                    for site in sites
                    if has_auth(site)]

        # 只有勾选猪猪（或全部站点）时才显示账号密码，避免污染其它站点配置。
        piggo_ids = [str(site.id) for site in sites
                     if "piggo.me" in f"{getattr(site, 'url', '') or ''} "
                                      f"{getattr(site, 'domain', '') or ''}".lower()]

        def piggo_selected_expression(model_name: str) -> str:
            if not piggo_ids:
                return "false"
            ids = json.dumps(piggo_ids, ensure_ascii=False)
            return (f"Array.isArray({model_name}) && ("
                    f"{model_name}.map(String).indexOf('all') >= 0 || "
                    f"{ids}.some(function(value) {{ return "
                    f"{model_name}.map(String).indexOf(value) >= 0; }}))")

        piggo_show = ("(" + piggo_selected_expression("site_ids") + " || "
                      + piggo_selected_expression("login_site_ids") + ")")

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
                      'API Key：{"api_key_source":"auto","engine":"http|playwright","path":"/api/checkin",'
                      '"credential_check_path":"/api/profile"}（纯 Key 站点会自动识别，不再要求填写 auth；'
                      '固定端点，不探测、不回退云端 Browserless）；'
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
                hint_field("VTextField", "concurrency", "最大并发数",
                           "普通站点建议 4–8；Browserless / Playwright 每个 Token 最多同时 2 个",
                           type="number", min=1, max=12)),
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
            {"component": "VRow", "props": {"show": "{{ " + piggo_show + " }}"},
             "content": [
                 hint_field("VTextField", "piggo_username", "猪猪账号",
                            "仅勾选猪猪或全部站点时显示；用于本机 Playwright 自动登录", 6,
                            autocomplete="username"),
                 hint_field("VTextField", "piggo_password", "猪猪密码",
                            "仅用于自动登录，不写入日志", 6, type="password",
                            autocomplete="current-password"),
             ]},
            row(field("VTextField", "browserless_url", "Browserless 地址", 4,
                      placeholder=DEFAULT_BROWSERLESS_URL, hint="根地址或完整 /stealth/bql 地址",
                      **{"persistent-hint": True}),
                field("VTextField", "browserless_token", "Browserless Token 1", 6,
                      type="password",
                      hint="仅 Browserless 引擎需要，不会写入日志", **{"persistent-hint": True}),
                field("VTextField", "browserless_token2", "Token 2（可选）", 6,
                      type="password", hint="额度用完自动换下一个", **{"persistent-hint": True}),
                field("VTextField", "browserless_token3", "Token 3（可选）", 6,
                      type="password", hint="额度用完自动换下一个", **{"persistent-hint": True}),
                field("VTextField", "browserless_token4", "Token 4（可选）", 6,
                      type="password", hint="额度用完自动换下一个", **{"persistent-hint": True}),
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
        selected_names = self._selected_site_names()
        selected = set(selected_names)
        # latest 会跨配置保留旧站点记录；详情页只能展示当前勾选的站点。
        visible_latest = [row for row in latest if str(row.get("site") or "") in selected]
        today = self._today_records(latest)
        summary = summarize_records(visible_latest, selected_names)
        last_run = max((str(row.get("ran_at") or "") for row in visible_latest), default="")[5:16]
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
                                   self._section_site_names(False, visible_latest),
                                   history, today, login_view=False)
        body += self._status_table("登录状态", "mdi-login-variant",
                                   self._section_site_names(True, visible_latest),
                                   history, today, login_view=True)
        page: List[dict] = [{"component": "style", "text": PAGE_STYLE},
                            {"component": "div", "props": {"class": "ptsignin-page"},
                             "content": body}]
        recorded = {str(row.get("site") or "") for row in visible_latest}
        missing = [name for name in selected_names if name not in recorded]
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

    def _section_site_names(self, login_view: bool,
                            latest: List[Dict[str, Any]]) -> List[str]:
        """该区块要显示的站点，以当前勾选集合为上限。"""
        try:
            sites = {site.id: site for site in SiteOper().list_order_by_pri()}
        except Exception:
            sites = {}
        if login_view:
            ids = list(sites) if self._all_login_sites else self._login_site_ids
        else:
            ids = list(sites) if self._all_sites else self._site_ids
        names = [str(sites[sid].name) for sid in ids if sid in sites]
        # 双选站点可能只勾了签到，但仍会产生登录记录；只从当前选中的 latest 补齐。
        belongs = ((lambda row: is_login_record(row, strict=True)) if login_view
                   else is_signin_record)
        selected = set(self._selected_site_names())
        for row in latest:
            name = str(row.get("site") or "")
            if name in selected and belongs(row) and name not in names:
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

    def _with_plugin_account(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """把插件设置里的猪猪账号密码合并到站点信息，不覆盖其他站点。"""
        result = dict(info)
        target = f"{result.get('url') or ''} {result.get('domain') or ''}".lower()
        if "piggo.me" not in target:
            return result
        username = str(getattr(self, "_piggo_username", "") or "").strip()
        password = str(getattr(self, "_piggo_password", "") or "")
        if username:
            result["username"] = username
        if password:
            result["password"] = password
        return result

    @staticmethod
    def _site_id(info: Dict[str, Any]) -> Optional[int]:
        """返回正整数站点 ID；字段异常时按未选择处理。"""
        try:
            site_id = int(str(info.get("id") or "").strip())
        except (TypeError, ValueError):
            return None
        return site_id if site_id > 0 else None

    @staticmethod
    def _key_only_site(info: Dict[str, Any]) -> bool:
        """站点只有 API Key / Token、没有 Cookie。是否可执行由显式 API 规则决定。"""
        cookie = str(info.get("cookie") or "").strip()
        credential = (str(info.get("apikey") or "").strip()
                      or str(info.get("token") or "").strip())
        return bool(credential and not cookie)

    def _is_run_current(self, run_id: Optional[int]) -> bool:
        """配置重载会递增 run_id；旧批次不得继续登录、签到或保存。"""
        event = getattr(self, "_stop_event", None)
        if event is not None and event.is_set():
            return False
        return run_id is None or run_id == getattr(self, "_run_id", run_id)

    def _site_guard(self, info: Dict[str, Any], run_id: Optional[int],
                    allowed_site_ids: Optional[Set[int]], action: str) -> str:
        """统一校验轮次与站点白名单；API Key 是否可执行由 _sign_one/_login_one 判断。"""
        if not self._is_run_current(run_id):
            return "任务已停止或被新配置取代，未执行" + action
        if allowed_site_ids is not None and self._site_id(info) not in allowed_site_ids:
            return f"站点不在本次{action}选择中，已跳过"
        return ""

    def _site_result(self, info: Dict[str, Any], *, engine: str, status: str,
                     message: str, login: str = "", activity: str = "",
                     activity_source: str = "", bonus: str = "", dual: bool = False,
                     login_status: str = "") -> Dict[str, Any]:
        """
        统一构造站点结果。

        :param info: MoviePilot 站点信息
        :param engine: 实际执行的签到引擎
        :param status: 签到或登录状态
        :param message: 结果说明
        :param login: 登录补充说明
        :param activity: 最近动向
        :param activity_source: 最近动向来源
        :param bonus: 奖励信息
        :param dual: 是否同时执行登录和签到
        :param login_status: 双任务站点的登录状态
        :return: 可保存和展示的统一结果字典
        """
        return {"site": str(info.get("name") or info.get("url") or "未知站点"),
                "site_id": str(info.get("id") or ""), "state_key": site_state_key(info),
                "engine": engine, "status": status, "message": message,
                "login": login, "activity": activity, "activity_source": activity_source,
                "ran_at": self._now(), "bonus": bonus, "dual": dual,
                "login_status": login_status}

    @staticmethod
    def _log_failure(site_name: str, message: str) -> None:
        """
        输出单行失败日志。

        :param site_name: 站点名称
        :param message: 失败原因，完整内容仍保存在结果页
        """
        logger.warning(f"PT站点签到：{site_name} 失败：{compact_text(message)}")

    def _skip_result(self, info: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return self._site_result(info, engine="-", status="failed", message=reason,
                                 dual=bool(info.get("dual")),
                                 login_status="login_failed" if info.get("dual") else "")

    def _finish_current(self, run_id: int, results: List[Dict[str, str]]) -> None:
        if not self._is_run_current(run_id):
            logger.info("PT站点签到：本轮结果已被新配置取代，不再保存或通知")
            return
        self._finish(results)

    def _sign_with_engine(self, info: Dict[str, Any], rule: Dict[str, Any], engine: str,
                          signer: Optional[BrowserlessSigner],
                          site_name: str) -> Optional[SignResult]:
        """按规则里的 engine 执行一次签到（站点模块失败后的回落路径）。"""
        try:
            if engine == "playwright":
                outcome = playwright_sign(info, rule, timeout=max(90, self._timeout * 2),
                                          proxies=self._proxies_for(info))
                if outcome is not None and outcome.status in {"success", "already"}:
                    return outcome
                # 本机成功时绝不消耗 Browserless 额度；只有浏览器助手缺失或本机流程失败，
                # 且规则允许时才回退。猪猪需要专用 Browserless 流程，不能套通用 HTTP 规则。
                allow_fallback = bool(rule.get("browserless_fallback")) or outcome is None
                if allow_fallback and signer is not None:
                    reason = ("本机未提供 Playwright" if outcome is None
                              else f"本机 Playwright 失败：{outcome.message}")
                    logger.info(f"PT站点签到：{site_name} {reason}，改用 Browserless")
                    fallback = signer.sign(info, rule)
                    if fallback.status in {"success", "already"}:
                        return SignResult(fallback.status,
                                          f"{reason}；Browserless：{fallback.message}")
                    return SignResult("failed",
                                      f"{reason}；Browserless 回退：{fallback.message}")
                if outcome is not None:
                    if allow_fallback:
                        return SignResult(outcome.status,
                                          f"{outcome.message}；未配置 Browserless，无法回退")
                    return outcome
                if rule.get("browser_auth_mode"):
                    return SignResult(
                        "failed",
                        "该站点需要本机 Playwright 完成浏览器内登录，"
                        "当前 MoviePilot 环境没有可用的浏览器助手")
                return SignResult("failed", "本机缺少 Playwright，且未配置 Browserless")
            if engine == "browserless":
                if signer is None:
                    return SignResult("failed", "未配置 Browserless 地址或 Token")
                return signer.sign(info, rule)
            outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                     proxies=self._proxies_for(info))
            if outcome.status == "failed" and self._should_fallback(rule, outcome):
                outcome, _ = self._maybe_fallback(info, rule, outcome, signer)
            return outcome
        except ValueError as error:
            return SignResult("failed", str(error))

    def _should_init_signer(self, infos: List[Dict[str, Any]],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """按最终生效的站点规则判断是否需要初始化 Browserless 引擎。

        彩虹岛等 quiz 规则只走一次 HTTP 提交与回读确认，失败后禁止改用
        Browserless 或通用表单，避免重复答题。显式 API Key 规则只访问固定
        端点；遇到 Cloudflare / 验证码时可用 Browserless 在浏览器上下文内重试。
        """
        for info in infos:
            try:
                rule = resolve_rule(info, rules)
            except ValueError:
                continue
            if is_api_key_rule(rule):
                if rule.get("browserless_fallback", True):
                    return True
                continue
            if self._key_only_site(info):
                module = site_module_for(str(info.get("url") or ""))
                if module is None or not hasattr(module, "signin"):
                    return True
                continue
            if str(rule.get("form_mode") or "").lower() == "quiz":
                continue
            if str(rule.get("engine")) == "browserless":
                return True
            if rule.get("auto_fallback") or rule.get("browserless_fallback"):
                return True
        return False

    def _sign_api_key_one(self, info: Dict[str, Any], rule: Dict[str, Any],
                          run_id: Optional[int], allowed_site_ids: Optional[Set[int]],
                          signer: Optional[Any] = None) -> Dict[str, Any]:
        """
        执行显式 API Key 签到。

        仅访问规则声明的端点，不触发站点登录或站点模块。
        API Key 规则可选 HTTP、本机 Playwright 或 Browserless 防护回退；
        HTTP / 本机 Playwright 检测到 Cloudflare 或验证码时，可由 Browserless
        在同一浏览器上下文内带上 API Key 重试。

        :param info: MoviePilot 站点信息
        :param rule: 已解析的 API Key 规则
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一签到结果
        """
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT站点签到：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)
        dual = bool(info.get("dual"))
        engine = str(rule.get("engine") or "http").strip().lower()
        try:
            if engine == "playwright":
                outcome = playwright_sign(info, rule, timeout=max(90, self._timeout * 2),
                                          proxies=self._proxies_for(info))
                if outcome is None:
                    outcome = SignResult(
                        "failed",
                        "当前 MoviePilot 环境没有可用的本机 Playwright，"
                        "无法先通过 Cloudflare 再调用 API Key 接口")
            elif engine == "http":
                outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                         proxies=self._proxies_for(info))
            else:
                outcome = SignResult("failed", "API Key 规则引擎只能是 http 或 playwright")
        except Exception:
            # 异常文本可能带站点回显，不把异常内容写进日志或结果。
            logger.error(f"PT站点签到：{site_name} API Key 签到异常")
            outcome = SignResult("failed", "API Key 签到出现未预期错误")
        if (outcome.status not in {"success", "already"} and outcome.challenge
                and signer is not None and hasattr(signer, "sign_api")):
            previous = outcome
            fallback = signer.sign_api(
                info, rule, timeout=max(120, self._timeout * 2))
            if fallback.status in {"success", "already"}:
                outcome = SignResult(
                    fallback.status,
                    f"{previous.message}；Browserless：{fallback.message}",
                )
                engine = "browserless"
            else:
                outcome = SignResult(
                    fallback.status,
                    f"{previous.message}；Browserless：{fallback.message}",
                    challenge=fallback.challenge or previous.challenge,
                    transient=fallback.transient,
                )
                engine = "browserless"
        if outcome.status not in {"success", "already"}:
            self._log_failure(site_name, outcome.message)
        activity_text, activity_source = (
            self._read_site_activity(info, rule, run_id, allowed_site_ids)
            if (outcome.status in {"success", "already"} and self._read_activity) else ("", ""))
        return self._site_result(
            info, engine=engine, status=outcome.status, message=outcome.message,
            login="API Key 已用于签到" if outcome.status in {"success", "already"}
            else "API Key 签到失败",
            activity=activity_text, activity_source=activity_source,
            bonus=message_bonus(outcome.message), dual=dual,
            login_status=(("login" if outcome.status in {"success", "already"}
                           else "login_failed") if dual else ""))

    def _sign_one(self, info: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                  signer: Optional[BrowserlessSigner], run_id: Optional[int] = None,
                  allowed_site_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """
        执行单个站点的登录、签到和最近动向读取。

        :param info: MoviePilot 站点信息
        :param rules: 已解析的全部自定义规则
        :param signer: Browserless 执行器
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一签到结果
        """
        info = self._with_plugin_account(info)
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT站点签到：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            rule = resolve_rule(info, rules)
        except ValueError as error:
            reason = f"API Key 规则无效，未发起请求：{error}" if self._key_only_site(info) else str(error)
            self._log_failure(site_name, reason)
            return self._skip_result(info, reason)

        if is_api_key_rule(rule):
            return self._sign_api_key_one(info, rule, run_id, allowed_site_ids, signer=signer)
        module_for_site = site_module_for(str(info.get("url") or ""))
        if self._key_only_site(info) and (
                module_for_site is None or not hasattr(module_for_site, "signin")):
            # 没有专用签到模块的纯 Key 站点使用通用 HTTP 兼容逻辑；
            # 有专用 signin 模块的站点仍优先走模块。
            try:
                generic_rule = generic_api_key_rule(info, rule)
            except ValueError as error:
                reason = f"通用 API Key 规则无效：{error}"
                self._log_failure(site_name, reason)
                return self._skip_result(info, reason)
            return self._sign_api_key_one(info, generic_rule, run_id, allowed_site_ids, signer=signer)

        engine = "http"
        login_text = ""
        login_broken = False
        dual = bool(info.get("dual"))
        account = account_from_site(info)
        browser_auth = str(rule.get("browser_auth_mode") or "").strip().lower()
        playwright_info: Dict[str, Any] = info

        if browser_auth:
            if account:
                playwright_info = dict(info)
                playwright_info.update(account)
            if account_ready(account):
                login_text = "浏览器内自动登录"
            elif account:
                login_text = "账号信息不完整，将尝试复用站点管理 Cookie"
            elif str(info.get("cookie") or "").strip():
                login_text = "复用站点管理 Cookie" if dual else ""
            else:
                login_text = "未配置账号密码或 Cookie"
        elif account_ready(account):
            try:
                login_result = site_login(info, account, timeout=self._timeout,
                                          proxies=self._proxies_for(info))
            except Exception as error:
                logger.error(f"PT站点签到：{site_name} 登录异常：{compact_text(error)}")
                login_text = "异常：真实登录出现未预期错误"
            else:
                if login_result.status == "success":
                    info = dict(info, cookie=login_result.cookie)
                    login_text = "成功（已换用新 Cookie）"
                else:
                    login_text = "失败：" + login_result.message
                    login_broken = True
                    self._log_failure(site_name, login_text)
        elif account:
            login_text = "未提供可用的账号密码"
            login_broken = True
        elif str(info.get("cookie") or "").strip():
            login_text = "复用站点管理 Cookie" if dual else ""

        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT站点签到：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            engine = str(rule["engine"])
            quiz_mode = str(rule.get("form_mode") or "").lower() == "quiz"
            module_outcome = (None if quiz_mode else
                              run_site_module(info, timeout=self._timeout,
                                              proxies=self._proxies_for(info), logger=logger,
                                              rule=rule))
            if module_outcome is not None and module_outcome.status != "failed":
                outcome = module_outcome
                engine = "module"
            elif module_outcome is not None:
                module_for_site = site_module_for(str(info.get("url") or ""))
                if getattr(module_for_site, "authoritative", False)                         or str(info.get("apikey") or "").strip():
                    outcome = module_outcome
                    engine = "module"
                else:
                    module_note = f"模块：{module_outcome.message}"
                    outcome = self._sign_with_engine(playwright_info, rule, engine, signer, site_name)
                    if outcome is None:
                        outcome = module_outcome
                        engine = "module"
                    elif outcome.status in {"success", "already"}:
                        outcome = SignResult(outcome.status,
                                             f"{outcome.message}（{module_note} 后由规则引擎完成）")
                        engine = str(rule["engine"])
                    else:
                        outcome = SignResult(outcome.status,
                                             f"{module_note}；规则引擎：{outcome.message}")
                        engine = str(rule["engine"])
            elif engine == "playwright":
                outcome = self._sign_with_engine(playwright_info, rule, engine, signer, site_name)
                engine = "playwright"
            elif engine == "browserless":
                outcome = (SignResult("failed", "未配置 Browserless 地址或 Token")
                           if signer is None else signer.sign(info, rule))
            else:
                outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                         proxies=self._proxies_for(info))
                if outcome.status == "failed" and self._should_fallback(rule, outcome):
                    outcome, engine = self._maybe_fallback(info, rule, outcome, signer)
        except ValueError as error:
            outcome = SignResult("failed", str(error))
        except Exception as error:
            logger.error(f"PT站点签到：{site_name} 异常：{compact_text(error)}")
            outcome = SignResult("failed", "签到执行出现未预期错误")

        if outcome.status not in {"success", "already"}:
            self._log_failure(site_name, outcome.message)
        if browser_auth and dual:
            if outcome.status in {"success", "already"}:
                login_text = ("浏览器内登录成功" if account_ready(account)
                              else "浏览器会话可用")
            else:
                login_broken = True

        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT站点签到：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)
        read_activity = (self._read_activity
                         and (outcome.status in {"success", "already"} or dual))
        activity_text, activity_source = (
            self._read_site_activity(info, rule, run_id, allowed_site_ids)
            if read_activity else ("", ""))
        return self._site_result(
            info, engine=engine, status=outcome.status, message=outcome.message,
            login=login_text, activity=activity_text, activity_source=activity_source,
            bonus=message_bonus(outcome.message), dual=dual,
            login_status=("login_failed" if login_broken else "login") if dual else "")

    def _login_one(self, info: Dict[str, Any], run_id: Optional[int] = None,
                   allowed_site_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """
        执行仅登录站点。

        真实登录或复用 Cookie 后读取最近动向，不触发签到。

        :param info: MoviePilot 站点信息
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一登录结果
        """
        info = self._with_plugin_account(info)
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
        if guard:
            logger.debug(f"PT站点签到：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            login_rule = resolve_rule(info, parse_rules(self._site_rules))
        except ValueError as error:
            if self._key_only_site(info):
                return self._skip_result(
                    info, f"API Key 规则无效，未发起请求：{error}")
            login_rule = {}

        module_login = run_site_module_login(
            info, timeout=self._timeout, proxies=self._proxies_for(info),
            logger=logger, rule=login_rule)
        if module_login is not None:
            login_ok = module_login.status == "success"
            login_text = module_login.message if login_ok else "失败：" + module_login.message
            if login_ok and module_login.cookie:
                info = dict(info, cookie=module_login.cookie)
            if not login_ok:
                self._log_failure(site_name, login_text)
            guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
            if guard:
                return self._skip_result(info, guard)
            activity_text, activity_source = (
                self._read_site_activity(info, login_rule, run_id, allowed_site_ids)
                if (login_ok and self._read_activity) else ("", ""))
            return self._site_result(
                info, engine="http", status="login" if login_ok else "login_failed",
                message=login_text, activity=activity_text,
                activity_source=activity_source)

        if is_api_key_rule(login_rule):
            try:
                check = http_check_api_key(info, login_rule, timeout=self._timeout,
                                           proxies=self._proxies_for(info))
            except Exception:
                # 不记录异常原文，避免站点回显 API Key 时进入日志。
                logger.error(f"PT站点签到：{site_name} API Key 验证异常")
                check = LoginResult("failed", "API Key 验证出现未预期错误")
            login_ok = check.status == "success"
            if not login_ok:
                self._log_failure(site_name, check.message)
            guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
            if guard:
                return self._skip_result(info, guard)
            activity_text, activity_source = (
                self._read_site_activity(info, login_rule, run_id, allowed_site_ids)
                if (login_ok and self._read_activity) else ("", ""))
            return self._site_result(
                info, engine="http", status="login" if login_ok else "login_failed",
                message=check.message, activity=activity_text,
                activity_source=activity_source)
        if self._key_only_site(info):
            return self._skip_result(
                info, "站点仅有 API Key / Token，但未声明 auth=api_key 登录规则，已跳过且未发起请求")

        login_text = ""
        login_ok = False
        account = account_from_site(info)
        if account_ready(account):
            try:
                login_result = site_login(info, account, timeout=self._timeout,
                                          proxies=self._proxies_for(info))
            except Exception as error:
                logger.error(f"PT站点签到：{site_name} 登录异常：{compact_text(error)}")
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
        if not login_ok:
            self._log_failure(site_name, login_text)

        guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
        if guard:
            return self._skip_result(info, guard)
        activity_text, activity_source = (
            self._read_site_activity(info, run_id=run_id,
                                     allowed_site_ids=allowed_site_ids)
            if (login_ok and self._read_activity) else ("", ""))
        return self._site_result(
            info, engine="http", status="login" if login_ok else "login_failed",
            message=login_text, activity=activity_text, activity_source=activity_source)

    def _run_login_batch(self, infos: List[Dict[str, Any]],
                         run_id: Optional[int] = None,
                         allowed_site_ids: Optional[Set[int]] = None) -> List[Dict[str, str]]:
        if not infos:
            return []
        workers = min(self._concurrency, len(infos))
        if workers <= 1:
            return [self._login_one(info, run_id, allowed_site_ids) for info in infos]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(
                lambda info: self._login_one(info, run_id, allowed_site_ids), infos))

    def _read_site_activity(self, info: Dict[str, Any],
                            rule: Optional[Dict[str, Any]] = None,
                            run_id: Optional[int] = None,
                            allowed_site_ids: Optional[Set[int]] = None) -> Tuple[str, str]:
        """用登录态 Cookie 打开站点页面，返回（「最近动向」原文, 来源页面）。

        来源页面写进记录；读不到时把原因（含试过的页面）也带回去，
        方便按站点用 activity_path 指定真正的动向页面。网络请求前统一校验
        轮次与站点白名单，避免配置重载或误选后继续访问站点。
        """
        guard = self._site_guard(info, run_id, allowed_site_ids, "读取最近动向")
        if guard:
            return "", guard
        rule = rule or {}
        if not rule:
            try:
                rule = resolve_rule(info, parse_rules(self._site_rules))
            except ValueError:
                rule = {}
        api_mode = is_api_key_rule(rule)
        cookie = str(info.get("cookie") or "").strip()
        activity_engine = str(rule.get("activity_engine") or "").strip().lower()
        account = account_from_site(info)
        # 猪猪这类站点可以直接用插件里的账号密码启动本机浏览器，
        # 不要求站点管理里另外保存一份可能已经失效的 Cookie。
        if not api_mode and not cookie and not (
                activity_engine == "playwright" and account_ready(account)):
            return "", ""
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        if activity_engine == "playwright":
            outcome = playwright_read_activity(
                info, rule, timeout=max(90, self._timeout * 2),
                proxies=self._proxies_for(info))
            if outcome is not None:
                if outcome.status == "success":
                    return outcome.last_seen or outcome.snippet or "已读取", outcome.source
                logger.debug(f"PT站点签到：{site_name} 本机浏览器读取最近动向失败"
                             f"（{compact_text(outcome.message)}）")
                return "", outcome.message
        # 站点常把「最近动向」放在签到页 / 校验页（例如观众 attendance.php），一并作为候选
        paths = [str(rule.get("path") or ""), str(rule.get("verify_success_path") or "")]
        try:
            activity = read_activity(info, cookie, timeout=self._timeout,
                                     proxies=self._proxies_for(info),
                                     path=str(account.get("activity_path") or "") or None,
                                     paths=paths, rule=rule)
        except Exception:
            # 异常原文可能包含站点回显，API Key 模式下尤其不能写进日志。
            logger.debug(f"PT站点签到：{site_name} 读取最近动向异常")
            return "", ""
        if activity.status != "success":
            logger.debug(f"PT站点签到：{site_name} 未读到最近动向（{compact_text(activity.message)}）")
            return "", activity.message
        return activity.last_seen or activity.snippet or "已读取", activity.source

    def _should_retry_site(self, info: Dict[str, Any],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """纯 Key 站点只有声明了 API 规则才重试；无规则时禁止盲试。"""
        if not self._key_only_site(info):
            return True
        try:
            return is_api_key_rule(resolve_rule(info, rules))
        except ValueError:
            return False

    @staticmethod
    def _should_fallback(rule: Dict[str, Any], outcome: SignResult) -> bool:
        """这一轮失败要不要改用浏览器再试一次。

        两种情况：① 撞上 Cloudflare / 验证码；② 拿到了页面但没匹配到成功标志——
        后者常见于只能靠浏览器点击的站点，未写规则的站点也给它一次机会。
        404（找不到签到入口）、Cookie 失效这类重试没用，不折腾。
        """
        if is_api_key_rule(rule):
            return False
        if str(rule.get("form_mode") or "").lower() == "quiz":
            # quiz 的提交结果只能靠回读确认；重试或换引擎会重复提交，必须禁用。
            return False
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
        if is_api_key_rule(rule):
            return outcome, "http"
        if str(rule.get("form_mode") or "").lower() == "quiz":
            return outcome, "http"
        if not rule.get("auto_fallback"):
            return outcome, "http"
        label = ("Cloudflare 验证" if outcome.challenge == "cloudflare"
                 else "验证码" if outcome.challenge
                 else "未匹配到签到结果")
        if signer is None:
            return SignResult("failed", f"{outcome.message}；检测到{label}，但未配置 Browserless 地址或 Token"), "http"
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        logger.info(f"PT站点签到：{site_name} 检测到{label}，自动改用 Browserless 重试")
        try:
            # 验证码站点要按页面推导选择器交给 Browserless 的 solveImageCaptcha
            page_html = ""
            if outcome.challenge == "captcha":
                page_html = fetch_page(target_url(str(info.get("url") or "").rstrip("/"),
                                                  str(rule.get("path") or DEFAULT_HTTP_PATH)),
                                       str(info.get("cookie") or ""), self._timeout,
                                       self._proxies_for(info), str(info.get("ua") or ""))
            fallback = fallback_rule(rule, outcome.challenge, page_html)
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
            logger.info(f"PT站点签到：{site_name} 的签到入口改为 {discovered['path']}")
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
            if not getattr(self, "_proxy_missing_logged", False):
                self._proxy_missing_logged = True
                logger.warning("PT站点签到：站点勾选代理，但 MoviePilot 未配置代理，本次直连")
            return None
        self._proxy_missing_logged = False
        if host != getattr(self, "_proxy_logged", ""):
            self._proxy_logged = host
            logger.info(f"PT站点签到：站点勾选「使用代理访问」，HTTP 引擎走代理 {mask_proxy(host)}")
        return {"http": host, "https": host}

    @staticmethod
    def _proxy_host() -> str:
        """取 MoviePilot 的全局代理地址：不同版本的模块路径与配置项名都试一遍，最后看环境变量。"""
        for module_name in ("app.sdk.config", "app.core.config", "app.core.settings",
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

    @staticmethod
    def _uses_browser_engine(info: Dict[str, Any], rules: Dict[str, Dict[str, Any]]) -> bool:
        """判断站点是否会进入本地浏览器或 Browserless 流程。"""
        try:
            rule = resolve_rule(info, rules)
        except ValueError:
            return False
        if is_api_key_rule(rule):
            return False
        if str(rule.get("browser_auth_mode") or "").strip():
            return True
        return str(rule.get("engine") or "http").lower() in {"browserless", "playwright"}

    def _run_site_batch(self, infos: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
                        signer: Optional[BrowserlessSigner], run_id: Optional[int],
                        allowed_site_ids: Optional[Set[int]], workers: int) -> List[Dict[str, str]]:
        """使用指定并发数执行一组站点。"""
        if not infos:
            return []
        if workers <= 1:
            return [self._sign_one(info, rules, signer, run_id, allowed_site_ids)
                    for info in infos]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(
                lambda info: self._sign_one(info, rules, signer, run_id, allowed_site_ids),
                infos))

    def _run_batch(self, infos: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
                   signer: Optional[BrowserlessSigner], run_id: Optional[int] = None,
                   allowed_site_ids: Optional[Set[int]] = None) -> List[Dict[str, str]]:
        """HTTP 与浏览器任务分组并发，避免慢速浏览器站点阻塞大批普通站点。"""
        if not infos:
            return []
        http_infos: List[Dict[str, Any]] = []
        browser_infos: List[Dict[str, Any]] = []
        for info in infos:
            target = browser_infos if self._uses_browser_engine(info, rules) else http_infos
            target.append(info)
        groups = []
        if http_infos:
            groups.append((http_infos, min(self._concurrency, len(http_infos))))
        if browser_infos:
            browser_capacity = int(getattr(signer, "max_concurrency", 2) or 2)
            browser_workers = min(self._concurrency, max(1, browser_capacity),
                                  len(browser_infos))
            groups.append((browser_infos, browser_workers))
        if len(groups) == 1:
            group, workers = groups[0]
            return self._run_site_batch(group, rules, signer, run_id, allowed_site_ids, workers)
        with ThreadPoolExecutor(max_workers=len(groups)) as coordinator:
            futures = [
                coordinator.submit(self._run_site_batch, group, rules, signer,
                                   run_id, allowed_site_ids, workers)
                for group, workers in groups
            ]
            grouped = [future.result() for future in futures]
        by_key = {
            str(result.get("site_id") or result.get("state_key")): result
            for group in grouped for result in group
        }
        return [by_key[str(info.get("id") or site_state_key(info))] for info in infos]

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
            all_sites: Dict[int, Any] = {}
            for site in SiteOper().list_order_by_pri():
                site_id = self._site_id({"id": getattr(site, "id", None)})
                if site_id is not None:
                    all_sites[site_id] = site
        except Exception as error:
            self._finish_current(run_id, [{"site": "配置", "status": "failed", "engine": "-",
                                           "message": f"读取站点失败：{error}",
                                           "ran_at": self._now()}])
            return

        # 每轮开始时固定站点快照。「全部站点」也在这里展开一次；后续并发线程、
        # 批次和重试只使用这组 ID，不再读取可能已被设置页重载的 self._*_ids。
        signin_requested = list(all_sites) if self._all_sites else list(self._site_ids)
        login_requested = (list(all_sites) if self._all_login_sites
                           else list(self._login_site_ids))
        signin_order = [sid for sid in signin_requested if sid in all_sites]
        login_order = [sid for sid in login_requested if sid in all_sites]
        signin_ids = set(signin_order)
        login_ids = set(login_order)
        dual_ids = signin_ids & login_ids
        login_only_ids = login_ids - dual_ids
        selected = [all_sites[sid] for sid in signin_order]
        login_selected = [all_sites[sid] for sid in login_order if sid in login_only_ids]

        missing_ids = sorted((set(signin_requested) | set(login_requested)) - set(all_sites))
        if missing_ids:
            logger.warning(f"PT站点签到：配置中的站点 ID 已不存在，已忽略：{missing_ids}")
        logger.info(f"PT站点签到：开始，签到 {len(selected)} 个，仅登录 {len(login_selected)} 个")
        if not selected and not login_selected:
            message = (f"未选择可用的 MoviePilot 站点（签到 IDs={sorted(signin_ids)}，"
                       f"仅登录 IDs={sorted(login_only_ids)}）")
            if missing_ids:
                message += f"；站点不存在 IDs={missing_ids}"
            self._finish_current(run_id, [{"site": "配置", "status": "failed", "engine": "-",
                                           "message": message, "ran_at": self._now()}])
            return

        try:
            rules = parse_rules(self._site_rules)
        except ValueError as error:
            self._finish_current(run_id, [{"site": "配置", "status": "failed", "engine": "-",
                                           "message": str(error), "ran_at": self._now()}])
            return

        infos = [dict(self._site_dict(site), dual=sid in dual_ids)
                 for site, sid in zip(selected, signin_order)]
        infos, skipped_rows = self._sites_to_run(infos)
        if skipped_rows:
            logger.info(f"PT站点签到：跳过今日已签到 {len(skipped_rows)} 个")
        if not infos and not login_selected:
            logger.info("PT站点签到：今天的站点都已签到，本轮跳过（要重跑请打开一次「清理本日缓存」）")
            return
        login_infos = [self._site_dict(site) for site in login_selected]
        signer: Optional[BrowserlessSigner] = None
        if self._should_init_signer(infos, rules):
            tokens = self._browserless_tokens()
            if not self._browserless_url:
                logger.warning("PT站点签到：未配置 Browserless 地址，相关站点将失败")
            else:
                if not tokens:
                    # 自建 Browserless 常关掉鉴权：留空 Token 也照常走，只是不能多 Key 轮换
                    logger.info("PT站点签到：Browserless 未填 Token，按自建无鉴权实例处理")
                try:
                    signer = BrowserlessPool(self._browserless_url, tokens,
                                             self._browserless_timeout, logger=logger)
                    if len(tokens) > 1:
                        logger.info(f"PT站点签到：已配置 {len(tokens)} 个 Browserless Token，额度用尽时自动切换")
                except ValueError as error:
                    self._finish_current(run_id, [{"site": "配置", "status": "failed", "engine": "-",
                                                   "message": str(error),
                                                   "ran_at": self._now()}])
                    return

        results = self._run_batch(infos, rules, signer, run_id, signin_ids)
        login_results = self._run_login_batch(login_infos, run_id, login_only_ids)
        if not self._is_run_current(run_id):
            logger.info("PT站点签到：本轮已被新配置取代，不再保存结果或安排重试")
            return
        # 跳过的站点把今天的记录原样带上，避免落盘时把它们的记录冲掉
        result_by_key = {r.get("state_key") or r.get("site"): r
                         for r in results + login_results + skipped_rows}
        retry_times = self._retry_count if allow_retry else 0
        failed_infos = [info for info, result in zip(infos, results)
                        if result.get("status") == "failed" and self._should_retry_site(info, rules)]
        if not failed_infos or retry_times <= 0:
            self._finish_current(run_id, list(result_by_key.values()))
            return
        # 先落盘首轮结果；重试完成后以最终结果覆盖同一天历史并统一通知。
        self._save_results(list(result_by_key.values()))
        self._schedule_retry(1, run_id, infos, rules, signer, signin_ids, result_by_key)

    def _schedule_retry(self, attempt: int, run_id: int, infos: List[Dict[str, Any]],
                        rules: Dict[str, Dict[str, Any]], signer: Optional[BrowserlessSigner],
                        signin_ids: Set[int],
                        result_by_key: Dict[str, Dict[str, str]]) -> None:
        """用后台 Timer 延迟执行下一轮失败重试，避免 sleep 占用执行线程。"""
        if not self._is_run_current(run_id):
            logger.info("PT站点签到：任务已停止或被新配置取代，不再安排重试")
            return
        if attempt > self._retry_count:
            self._finish_current(run_id, list(result_by_key.values()))
            return
        old_timer = getattr(self, "_retry_timer", None)
        if old_timer is not None:
            try:
                old_timer.cancel()
            except Exception:
                pass
        delay = max(1, self._retry_delay_minutes) * 60
        logger.info(f"PT站点签到：失败站点将在 {self._retry_delay_minutes} 分钟后重试（第 {attempt}/{self._retry_count} 次）")
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None) if scheduler_sdk else None
        if callable(add_once):
            try:
                add_once(self.__class__.__name__, f"retry_{attempt}", self._retry_task,
                         f"PT站点签到 失败重试（第 {attempt} 轮）", delay_seconds=int(delay),
                         kwargs={"attempt": attempt, "run_id": run_id, "infos": infos,
                                 "rules": rules, "signer": signer, "signin_ids": signin_ids,
                                 "result_by_key": result_by_key})
                return
            except TypeError:
                # 老版本接口不接受 kwargs：退回本进程 Timer
                pass
            except Exception:
                logger.exception("PT站点签到：登记重试任务失败，改用本地 Timer")
        timer = Timer(delay, self._retry_task,
                      args=(attempt, run_id, infos, rules, signer, signin_ids, result_by_key))
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _retry_task(self, attempt: int, run_id: int, infos: List[Dict[str, Any]],
                    rules: Dict[str, Dict[str, Any]], signer: Optional[BrowserlessSigner],
                    signin_ids: Set[int],
                    result_by_key: Dict[str, Dict[str, str]]) -> None:
        event = getattr(self, "_stop_event", None)
        if event is not None and event.is_set():
            return
        if not self._is_run_current(run_id):
            logger.info("PT站点签到：本轮重试已被更新的任务取代，跳过")
            return
        # 重试是后台线程；若刚好有新一轮在跑，最多等 5 分钟再决定是否放弃。
        if not PTSiteSignIn._run_lock.acquire(timeout=300):
            logger.warning(f"PT站点签到：执行线程繁忙，跳过第 {attempt} 次失败重试")
            return
        try:
            if not self._is_run_current(run_id):
                logger.info("PT站点签到：任务已停止或被新一轮取代，取消重试")
                return
            failed_infos = [info for info in infos
                            if result_by_key.get(site_state_key(info), {}).get("status") == "failed"
                            and self._should_retry_site(info, rules)]
            logger.info(f"PT站点签到：开始第 {attempt}/{self._retry_count} 次失败重试"
                        f"（{len(failed_infos)} 个站点）")
            if not failed_infos:
                self._finish_current(run_id, list(result_by_key.values()))
                return
            for result in self._run_batch(failed_infos, rules, signer, run_id, signin_ids):
                result_by_key[result.get("state_key") or result.get("site")] = result
            if not self._is_run_current(run_id):
                logger.info("PT站点签到：重试期间配置已重载，丢弃旧批次结果")
                return
            still_failed = any(result_by_key.get(site_state_key(info), {}).get("status") == "failed"
                               for info in infos if self._should_retry_site(info, rules))
            if still_failed and attempt < self._retry_count:
                self._save_results(list(result_by_key.values()))
                self._schedule_retry(attempt + 1, run_id, infos, rules, signer,
                                     signin_ids, result_by_key)
                return
            self._finish_current(run_id, list(result_by_key.values()))
        except Exception:
            logger.exception("PT站点签到：失败重试出现未预期错误")
            try:
                self._finish_current(run_id, list(result_by_key.values()))
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
        logger.info(f"PT站点签到：完成，新签到 {signed}，今日已签到 {already}，仅登录 {login_ok}，失败 {failed}")
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
                logger.warning(f"PT站点签到：删除历史数据 {key} 失败，将改为清空")
        self.save_data(key, None)

    def _notify(self, results: List[Dict[str, str]]) -> None:
        """推送结果（由「发送通知」开关控制）。

        版式固定三块：一行统计、一行执行时间、失败时按语义分类列站点与原因，
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
            grouped: Dict[str, List[str]] = {}
            for item in failures:
                category = self._failure_category(item)
                grouped.setdefault(category, []).append(str(item.get("site") or "未知站点"))
            for category, site_names in list(grouped.items())[:NOTIFY_LIMIT]:
                shown = site_names[:NOTIFY_SITE_LIMIT]
                suffix = f" 等 {len(site_names)} 个站点" if len(site_names) > NOTIFY_SITE_LIMIT else ""
                lines.append(f"· {category}：{'、'.join(shown)}{suffix}")
            if len(grouped) > NOTIFY_LIMIT:
                lines.append(f"…另有 {len(grouped) - NOTIFY_LIMIT} 类失败，详见插件日志")
        self.post_message(mtype=NotificationType.SiteMessage,
                          title="PT站点签到",
                          text="\n".join(lines))

    @staticmethod
    def _is_failed(item: Dict[str, Any]) -> bool:
        """这条记录算不算失败（成功 / 今日已签到 / 登录成功 之外的都算）。"""
        return str(item.get("status") or "") not in {"success", "already", "login"}

    @staticmethod
    def _failure_category(item: Dict[str, Any]) -> str:
        """把原始失败原因归入稳定语义类别；未知业务错误保留原文。"""
        detail = str(item.get("message") or "").strip() or str(item.get("login") or "").strip()
        detail = decode_entities(detail)
        for marker in ("；诊断：", "；标题", "；正文", "；片段"):
            if marker in detail:
                detail = detail.split(marker, 1)[0]
                break
        key = re.sub(r"\s+", "", detail).lower()
        for label, pattern in _FAILURE_CATEGORY_RULES:
            if re.search(pattern, key, re.IGNORECASE):
                return label
        if len(detail) > NOTIFY_REASON_LIMIT:
            detail = detail[:NOTIFY_REASON_LIMIT].rstrip() + "…"
        return detail or "未返回具体失败原因"

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
