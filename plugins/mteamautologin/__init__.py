"""M-Team 自动登录 / 账号保活插件（MoviePilot V2、V3 兼容）。

本插件通过 M-Team 官方 Web 接口执行 **真实账号登录**（用户名 + 密码 + 动态验证码 TOTP），
由服务端签发真实的 Authorization 令牌，再用该令牌调用会员接口刷新"最后访问时间"，
从而达到定时保活的目的。它不是模拟点击或伪造 Cookie。

核心流程（与 M-Team 网页端一致）：
1. POST /api/login            真实登录，成功后从响应头 Authorization 取出令牌
2. POST /api/member/profile   校验令牌有效性并读取账号数据
3. POST /api/member/updateLastBrowse  刷新最后访问时间（保活关键动作）

鉴权签名算法：_sgin = base64(HMAC-SHA1("{METHOD}&{PATH}&{毫秒时间戳}", "HLkPcWmycL57mfJt"))
"""

import base64
import hashlib
import hmac
import random
import struct
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

try:  # MoviePilot V3 命名空间
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
except Exception:  # MoviePilot V1/V2 命名空间
    from app.core.event import Event, eventmanager
    from app.log import logger

from app.plugins import _PluginBase
from app.schemas.types import EventType


class MTeamAutoLogin(_PluginBase):
    """M-Team 定时真实登录与账号保活插件。"""

    # 插件名称
    plugin_name = "M-Team 自动登录"
    # 插件描述
    plugin_desc = "定时使用账号密码 + 动态验证码真实登录 M-Team，并刷新最后访问时间保活。"
    # 插件图标
    plugin_icon = "Moviepilot_A.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "local"
    # 作者主页
    author_url = ""
    # 插件配置项 ID 前缀
    plugin_config_prefix = "mteamautologin_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # -------- 固定常量 --------
    # 接口签名密钥（取自 M-Team 网页端 main.xxxxxx.js）
    _SIGN_KEY = "HLkPcWmycL57mfJt"
    # 默认浏览器 UA，避免因 UA 异常被风控
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    _CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"

    def __init__(self):
        """初始化插件运行时变量。"""
        super().__init__()
        # 配置项
        self._enabled: bool = False
        self._notify: bool = True
        self._username: str = ""
        self._password: str = ""
        self._totp_secret: str = ""
        self._manual_token: str = ""
        self._login_cron: str = "0 */6 * * *"
        self._host: str = "api.m-team.io"
        self._referer: str = "https://kp.m-team.cc/"
        self._proxy: str = ""
        self._ua: str = self._DEFAULT_UA
        self._version: str = ""
        self._web_version: str = ""
        self._random_delay: int = 0
        self._keep_alive: bool = True
        self._force_login: bool = False
        self._warmup: bool = False
        self._run_once: bool = False
        # 运行时状态
        self._token: str = ""
        self._did: str = ""
        self._visitor_id: str = ""
        self._running: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并初始化插件运行状态。"""
        self.stop_service()
        self._enabled = False
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._totp_secret = str(config.get("totp_secret") or "").strip()
        self._manual_token = str(config.get("manual_token") or "").strip()
        self._login_cron = str(config.get("login_cron") or "").strip()
        self._host = self._normalize_host(config.get("host"))
        self._referer = str(config.get("referer") or "https://kp.m-team.cc/").strip()
        self._proxy = str(config.get("proxy") or "").strip()
        self._ua = str(config.get("ua") or "").strip() or self._DEFAULT_UA
        self._version = str(config.get("version") or "").strip()
        self._web_version = str(config.get("web_version") or "").strip()
        self._random_delay = int(config.get("random_delay") or 0)
        self._keep_alive = bool(config.get("keep_alive", True))
        self._force_login = bool(config.get("force_login"))
        self._warmup = bool(config.get("warmup"))
        self._run_once = bool(config.get("run_once"))

        # 恢复持久化的登录态
        self._token = self.get_data("token") or ""
        self._did = self.get_data("did") or ""
        self._visitor_id = self.get_data("visitor_id") or ""

        if self._random_delay > 30:
            self._random_delay = 30
        # 说明：_run_once 保持为 True，宿主随后会调用 get_service() 注册一次性任务；
        # 任务执行完毕后由 do_login(once=True) 写回配置关闭该开关。

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """停用插件时清理运行时状态。"""
        self._running = False

    # ------------------------------------------------------------------ #
    # 配置表单 / 详情页
    # ------------------------------------------------------------------ #
    @staticmethod
    def _col(component: dict, cols: int = 12, md: int = 4) -> dict:
        """构造一个栅格列。"""
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [component],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页的 JSON 与默认配置。"""
        return (
            [
                {
                    "component": "VForm",
                    "content": [
                        # 运行提示
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "warning",
                                                "variant": "tonal",
                                                "title": "使用前提",
                                                "text": (
                                                    "1) 账号二次验证需为动态验证码(TOTP)，"
                                                    "TOTP 密钥从认证器二维码链接的 secret 参数中获取；"
                                                    "2) 本插件为真实登录，会按配置周期使用账号密码登录站点，"
                                                    "请合理设置周期，过于频繁可能触发站点风控；"
                                                    "3) 站点官方建议第三方工具使用 API 令牌，请自行评估账号风险。"
                                                ),
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        # 开关
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "keep_alive",
                                            "label": "登录后刷新最后访问时间",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "force_login",
                                            "label": "每次强制重新登录",
                                        },
                                    },
                                    md=3,
                                ),
                            ],
                        },
                        # 账号
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username",
                                            "label": "用户名 / 邮箱",
                                        },
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "type": "password",
                                        },
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "totp_secret",
                                            "label": "动态验证码密钥 (TOTP Secret)",
                                            "placeholder": "未开二次验证可留空",
                                            "hint": "账号未开启二次验证：留空即可；"
                                                    "已开启：必须填写，否则无法自动登录",
                                            "persistent-hint": True,
                                        },
                                    },
                                    md=4,
                                ),
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "manual_token",
                                            "label": "手动令牌（拿不到 TOTP 密钥时的替代方案）",
                                            "placeholder": "从浏览器请求头复制 Authorization 的值",
                                            "hint": "填了就只用它保活，不再用账号密码登录；"
                                                    "令牌失效后需要重新复制粘贴",
                                            "persistent-hint": True,
                                        },
                                    },
                                    md=8,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "random_delay",
                                            "label": "随机延迟（分钟，0-30）",
                                            "type": "number",
                                        },
                                    },
                                    md=4,
                                ),
                            ],
                        },
                        # 周期
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "login_cron",
                                            "label": "登录周期 Cron 表达式",
                                            "placeholder": "0 */6 * * *",
                                            "hint": (
                                                "五段式 Cron：分 时 日 月 周。"
                                                "0 */6 * * * = 每 6 小时；0 */2 * * * = 每 2 小时；"
                                                "0 9,21 * * * = 每天 9 点与 21 点；30 8 * * * = 每天 8:30"
                                            ),
                                            "persistent-hint": True,
                                        },
                                    },
                                    md=6,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "proxy",
                                            "label": "代理地址（可选）",
                                            "placeholder": "http://192.168.1.2:7890",
                                        },
                                    },
                                    md=6,
                                ),
                            ],
                        },
                        # 高级
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "host",
                                            "label": "API 域名",
                                            "placeholder": "api.m-team.io",
                                        },
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "referer",
                                            "label": "Referer",
                                            "placeholder": "https://kp.m-team.cc/",
                                        },
                                    },
                                    md=4,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ua",
                                            "label": "User-Agent（可选）",
                                            "placeholder": "留空使用默认浏览器 UA",
                                        },
                                    },
                                    md=4,
                                ),
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "version",
                                            "label": "version 请求头（可选）",
                                            "placeholder": "如 1.1.2",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "web_version",
                                            "label": "webversion 请求头（可选）",
                                            "placeholder": "如 1120",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "warmup",
                                            "label": "登录后预热访问",
                                        },
                                    },
                                    md=3,
                                ),
                                self._col(
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_once",
                                            "label": "立即运行一次",
                                        },
                                    },
                                    md=3,
                                ),
                            ],
                        },
                    ],
                }
            ],
            {
                "enabled": False,
                "notify": True,
                "username": "",
                "password": "",
                "totp_secret": "",
                "manual_token": "",
                "login_cron": "0 */6 * * *",
                "host": "api.m-team.io",
                "referer": "https://kp.m-team.cc/",
                "proxy": "",
                "ua": "",
                "version": "",
                "web_version": "",
                "random_delay": 0,
                "keep_alive": True,
                "force_login": False,
                "warmup": False,
                "run_once": False,
            },
        )

    def get_page(self) -> List[dict]:
        """返回插件详情页的 JSON。"""
        history = self.get_data("history") or []
        lines = ["暂无执行记录"] if not history else [
            f"{item.get('time', '')}  {item.get('status', '')}  {item.get('message', '')}"
            for item in history[:20]
        ]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "运行状态",
                    "text": f"插件状态：{'已启用' if self._enabled else '未启用'}；"
                            f"登录周期：{self._login_cron or '未设置'}；"
                            f"当前令牌：{'有效缓存' if self._token else '无'}",
                },
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "最近执行记录",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-body-2"},
                        "text": "\n".join(lines),
                    },
                ],
            },
        ]

    # ------------------------------------------------------------------ #
    # 服务 / 命令 / API
    # ------------------------------------------------------------------ #
    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时登录服务。"""
        services: List[Dict[str, Any]] = []
        # 立即运行一次
        if self._run_once:
            services.append(
                {
                    "id": "MTeamAutoLogin.Once",
                    "name": "M-Team 自动登录（立即运行一次）",
                    "trigger": DateTrigger(run_date=datetime.now() + timedelta(seconds=5)),
                    "func": self.do_login,
                    "kwargs": {"force": True, "once": True},
                }
            )
        # 定时任务
        if self._enabled and self._login_cron:
            try:
                trigger = CronTrigger.from_crontab(self._login_cron)
            except Exception as err:
                logger.error(f"M-Team 自动登录：Cron 表达式不合法 {self._login_cron} - {err}")
                trigger = None
            if trigger:
                services.append(
                    {
                        "id": "MTeamAutoLogin.Login",
                        "name": "M-Team 自动登录保活",
                        "trigger": trigger,
                        "func": self.do_login,
                        "kwargs": {},
                    }
                )
        return services

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/mteam_login",
                "event": EventType.PluginAction,
                "desc": "手动执行 M-Team 登录保活",
                "category": "插件命令",
                "data": {"action": "mteam_login"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def mteam_login_command(self, event: Event) -> None:
        """响应远程命令触发。"""
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "mteam_login":
            return
        self.do_login(force=True)

    def get_api(self) -> List[Dict[str, Any]]:
        """声明插件 API。"""
        return [
            {
                "path": "/login_now",
                "endpoint": self.api_login_now,
                "methods": ["POST", "GET"],
                "auth": "bear",
                "summary": "立即执行一次 M-Team 真实登录",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询插件运行状态",
            },
        ]

    def api_login_now(self) -> dict:
        """API：立即执行一次登录。"""
        if self._running:
            return {"success": False, "message": "任务正在执行中"}
        ok = self.do_login(force=True)
        return {"success": ok, "message": "执行完成，详情见插件日志"}

    def api_status(self) -> dict:
        """API：返回运行状态。"""
        return {
            "success": True,
            "enabled": self._enabled,
            "running": self._running,
            "cron": self._login_cron,
            "has_token": bool(self._token),
            "history": self.get_data("history") or [],
        }

    # ------------------------------------------------------------------ #
    # 核心逻辑
    # ------------------------------------------------------------------ #
    def do_login(self, force: bool = False, once: bool = False) -> bool:
        """执行一次真实登录 + 保活流程。

        :param force: 是否忽略本地缓存的令牌，强制走一次真实登录
        :param once: 是否为"立即运行一次"任务（执行后清除开关）
        """
        if self._running:
            logger.warn("M-Team 自动登录：上一次任务尚未结束，本次跳过")
            return False
        if not self._manual_token and (not self._username or not self._password):
            logger.warn("M-Team 自动登录：既未配置账号密码，也未填写手动令牌，跳过执行")
            return False

        self._running = True
        try:
            if self._random_delay > 0:
                delay = random.randint(0, self._random_delay * 60)
                logger.info(f"M-Team 自动登录：随机延迟 {delay} 秒执行")
                time.sleep(delay)

            session = self._create_session()
            use_force = force or self._force_login

            # 手动令牌优先：只做保活，不做账号密码登录
            if self._manual_token and not use_force:
                self._token = self._manual_token

            # 1. 优先复用缓存令牌校验，避免每次都用账号密码登录
            if not use_force and self._token:
                ok, summary = self._fetch_profile(session)
                if ok:
                    mode = "手动令牌校验" if self._token == self._manual_token else "缓存令牌校验"
                    self._after_success(session, summary, mode=mode)
                    return True
                logger.info("M-Team 自动登录：令牌已失效")
                self._token = ""
                self.save_data("token", "")
                if not self._username or not self._password:
                    self._after_failure(
                        "手动令牌已失效，且未配置账号密码无法自动续期，"
                        "请重新从浏览器复制 Authorization 到「手动令牌」"
                    )
                    return False

            # 2. 真实登录
            token = self._real_login(session)
            if not token:
                self._after_failure("登录失败，请检查账号密码与动态验证码密钥")
                return False
            self._token = token
            self.save_data("token", token)

            # 3. 登录后拉取用户信息并保活
            ok, summary = self._fetch_profile(session)
            if not ok:
                self._after_failure(summary or "登录成功但用户信息校验失败")
                return False

            self._after_success(session, summary, mode="真实登录")
            return True
        except Exception as err:
            logger.error(f"M-Team 自动登录：执行异常 - {err}")
            self._after_failure(f"执行异常：{err}")
            return False
        finally:
            self._running = False
            if once:
                self.update_config({**self._current_config(), "run_once": False})

    def _real_login(self, session) -> str:
        """调用 /api/login 完成真实登录，返回服务端令牌。

        流程与 M-Team 网页端一致：先不带验证码请求，若返回 code=1001
        则说明账号开启了二次验证，此时使用 TOTP 密钥生成验证码重试。
        """
        username, password = self._username, self._password

        # 第一次尝试：不带动态验证码
        resp = self._post(session, "/api/login", {
            "username": username,
            "password": password,
            "turnstile": "",
        })
        result = self._parse(resp)
        if result is None:
            logger.error("M-Team 自动登录：登录接口无有效响应")
            return ""

        if result.get("message") == "SUCCESS":
            return self._extract_token(resp)

        # 需要二次验证
        if int(result.get("code") or 0) == 1001:
            if not self._totp_secret:
                logger.error(
                    "M-Team 自动登录：账号开启了二次验证，服务端要求提交动态验证码，"
                    "但未配置 TOTP 密钥。可选处理："
                    "① 填写 TOTP 密钥；② 在站点关闭二次验证后留空此字段；"
                    "③ 改用「手动令牌」模式，从浏览器复制 Authorization 粘贴。"
                )
                return ""
            try:
                otp = self._generate_totp(self._totp_secret)
            except Exception as err:
                logger.error(f"M-Team 自动登录：TOTP 密钥解析失败 - {err}")
                return ""
            logger.info("M-Team 自动登录：站点要求二次验证，使用动态验证码重试")
            resp = self._post(session, "/api/login", {
                "username": username,
                "password": password,
                "otpCode": otp,
                "turnstile": "",
            })
            result = self._parse(resp)
            if result and result.get("message") == "SUCCESS":
                return self._extract_token(resp)
            logger.error(f"M-Team 自动登录：带验证码登录失败 - {result}")
            return ""

        logger.error(f"M-Team 自动登录：登录失败 - {result}")
        return ""

    def _extract_token(self, resp) -> str:
        """从登录响应头中取出真实令牌并持久化 did。"""
        token = ""
        did = ""
        try:
            for key, value in resp.headers.items():
                low = key.lower()
                if low == "authorization":
                    token = value
                elif low == "did":
                    did = value
        except Exception:
            pass
        if did:
            self._did = did
            self.save_data("did", did)
        if not token:
            logger.error("M-Team 自动登录：登录返回成功但未取到 Authorization 令牌")
        return token

    def _fetch_profile(self, session) -> Tuple[bool, str]:
        """调用 /api/member/profile 校验令牌并读取账号数据。"""
        if self._warmup:
            self._warmup_visit(session)
        resp = self._post(session, "/api/member/profile", {}, token=self._token, origin=True)
        if resp is None:
            return False, "网络请求失败"
        if resp.status_code != 200:
            return False, f"令牌校验失败 status={resp.status_code}"
        result = self._parse(resp)
        if not result:
            return False, "响应解析失败"
        if result.get("message") != "SUCCESS":
            return False, f"令牌无效：{result.get('message')}"

        data = result.get("data") or {}
        count = data.get("memberCount") or {}
        status = data.get("memberStatus") or {}
        uploaded = self._to_gb(count.get("uploaded"))
        downloaded = self._to_gb(count.get("downloaded"))
        bonus = count.get("bonus", "-")
        username = data.get("username", "-")
        last_browse = status.get("lastBrowse", "-")
        summary = (
            f"账号：{username}；上传 {uploaded}；下载 {downloaded}；"
            f"魔力值 {bonus}；最后访问 {last_browse}"
        )

        if self._keep_alive:
            self._update_last_browse(session)
        return True, summary

    def _update_last_browse(self, session) -> bool:
        """调用 /api/member/updateLastBrowse 刷新最后访问时间（保活）。"""
        resp = self._post(session, "/api/member/updateLastBrowse", {}, token=self._token, origin=True)
        result = self._parse(resp) if resp is not None else None
        if result and result.get("message") == "SUCCESS":
            logger.info("M-Team 自动登录：最后访问时间已刷新")
            return True
        logger.warn(f"M-Team 自动登录：刷新最后访问时间失败 - {result}")
        return False

    def _warmup_visit(self, session) -> None:
        """模拟一次正常页面访问，预热站点状态。

        部分账号在登录后立即调用会员接口会被判定为"未登录"，
        先访问若干公开接口可让服务端完成会话初始化。
        """
        for method, path in (
            ("GET", "/api/system/unix"),
            ("GET", "/ping"),
            ("POST", "/api/system/state"),
        ):
            try:
                resp = self._post(session, path, {}, token=self._token, method=method)
                logger.debug(f"M-Team 自动登录：预热 {method} {path} -> "
                             f"{getattr(resp, 'status_code', 'N/A')}")
            except Exception as err:
                logger.debug(f"M-Team 自动登录：预热 {method} {path} 异常 - {err}")

    # ------------------------------------------------------------------ #
    # HTTP 支撑
    # ------------------------------------------------------------------ #
    def _create_session(self):
        """创建 HTTP 会话，优先使用具备浏览器指纹能力的库以绕过 Cloudflare。"""
        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            sess = curl_requests.Session(impersonate="chrome")
            logger.debug("M-Team 自动登录：使用 curl_cffi 指纹会话")
            return sess
        except Exception:
            pass
        try:
            import cloudscraper  # type: ignore

            logger.debug("M-Team 自动登录：使用 cloudscraper 会话")
            return cloudscraper.create_scraper()
        except Exception:
            logger.debug("M-Team 自动登录：使用 requests 会话")
            return requests.Session()

    def _post(self, session, path: str, extra: dict, token: str = "", origin: bool = False,
              method: str = "POST"):
        """发送带签名与鉴权头的接口请求。"""
        ts_ms = int(time.time() * 1000)
        body = dict(extra)
        body["_timestamp"] = str(ts_ms)
        body["_sgin"] = self._sign(method, path, ts_ms)

        url = f"https://{self._host}{path}"
        headers = {
            "User-Agent": self._ua,
            "referer": self._referer,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json;charset=UTF-8",
            "Ts": str(int(time.time())),
            "Did": self._did or self._random_string(16),
            "visitorid": self._visitor_id or self._ensure_visitor_id(),
        }
        if origin:
            headers["origin"] = self._referer
        if self._version:
            headers["version"] = self._version
        if self._web_version:
            headers["webversion"] = self._web_version
        if token:
            headers["Authorization"] = token

        proxies = None
        if self._proxy:
            proxies = {"http": self._proxy, "https": self._proxy}

        try:
            resp = session.request(
                method,
                url,
                data=body,
                headers=headers,
                proxies=proxies,
                timeout=30,
            )
        except Exception as err:
            logger.error(f"M-Team 自动登录：请求 {path} 失败 - {err}")
            return None

        # 同步服务端可能下发的 Did
        did = resp.headers.get("Did") or resp.headers.get("did")
        if did and did != self._did:
            self._did = did
            self.save_data("did", did)
        return resp

    @staticmethod
    def _parse(resp) -> Optional[dict]:
        """安全解析响应 JSON。"""
        if resp is None:
            return None
        try:
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _sign(self, method: str, path: str, ts_ms: int) -> str:
        """计算接口签名：base64(HMAC-SHA1("METHOD&path&时间戳"))。"""
        message = f"{method}&{path}&{ts_ms}".encode("utf-8")
        digest = hmac.new(self._SIGN_KEY.encode("utf-8"), message, hashlib.sha1).digest()
        return base64.b64encode(digest).decode("utf-8")

    @classmethod
    def _random_string(cls, length: int) -> str:
        """生成随机小写字母数字串。"""
        return "".join(random.choice(cls._CHARSET) for _ in range(length))

    def _ensure_visitor_id(self) -> str:
        """获取或生成持久化的 visitorid。"""
        if self._visitor_id:
            return self._visitor_id
        self._visitor_id = self._random_string(32)
        self.save_data("visitor_id", self._visitor_id)
        return self._visitor_id

    @staticmethod
    def _generate_totp(secret: str, digits: int = 6, interval: int = 30) -> str:
        """根据 Base32 密钥生成当前 TOTP 动态验证码（RFC 6238）。"""
        clean = secret.strip().replace(" ", "").replace("-", "").upper()
        padding = "=" * ((8 - len(clean) % 8) % 8)
        key = base64.b32decode(clean + padding, casefold=True)
        counter = int(time.time()) // interval
        message = struct.pack(">Q", counter)
        digest = hmac.new(key, message, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % (10 ** digits)).zfill(digits)

    @staticmethod
    def _to_gb(value: Any) -> str:
        """字节转换为 GB 展示。"""
        try:
            return f"{int(value) / 1073741824:.2f} GB"
        except Exception:
            return "-"

    @staticmethod
    def _normalize_host(host: Any) -> str:
        """规范化 API 域名配置。"""
        text = str(host or "").strip() or "api.m-team.io"
        text = text.replace("https://", "").replace("http://", "")
        return text.rstrip("/")

    # ------------------------------------------------------------------ #
    # 结果处理
    # ------------------------------------------------------------------ #
    def _current_config(self) -> dict:
        """返回当前生效的配置字典。"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "username": self._username,
            "password": self._password,
            "totp_secret": self._totp_secret,
            "manual_token": self._manual_token,
            "login_cron": self._login_cron,
            "host": self._host,
            "referer": self._referer,
            "proxy": self._proxy,
            "ua": self._ua if self._ua != self._DEFAULT_UA else "",
            "version": self._version,
            "web_version": self._web_version,
            "random_delay": self._random_delay,
            "keep_alive": self._keep_alive,
            "force_login": self._force_login,
            "warmup": self._warmup,
            "run_once": False,
        }

    def _after_success(self, session, summary: str, mode: str) -> None:
        """记录并通知成功结果。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"{mode}成功；{summary}"
        logger.info(f"M-Team 自动登录：{message}")
        self._append_history({"time": now, "status": "成功", "message": message})
        if self._notify:
            self.post_message(title="【M-Team 自动登录】成功", text=f"{now}\n{message}")

    def _after_failure(self, message: str) -> None:
        """记录并通知失败结果。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.error(f"M-Team 自动登录：{message}")
        self._append_history({"time": now, "status": "失败", "message": message})
        if self._notify:
            self.post_message(title="【M-Team 自动登录】失败", text=f"{now}\n{message}")

    def _append_history(self, item: dict) -> None:
        """追加执行历史（仅保留最近 20 条）。"""
        history = self.get_data("history") or []
        history.insert(0, item)
        self.save_data("history", history[:20])
